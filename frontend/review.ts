import './review.css';
import { ChangeSet, Compartment, EditorState, StateEffect, StateField } from '@codemirror/state';
import { Decoration, DecorationSet, EditorView, highlightActiveLine, keymap, lineNumbers } from '@codemirror/view';
import { defaultKeymap, history, historyKeymap } from '@codemirror/commands';
import { markdown } from '@codemirror/lang-markdown';
import { defaultHighlightStyle, syntaxHighlighting } from '@codemirror/language';
import { externalPreviewLinks, markdownDocument } from './render-markdown';

type Range = [number, number];
type Region = { page: number; polygon: [number, number][] };
export type ReviewWarning = { id: string; code: string; page: number; message: string; block_ids?: string[];
  source_regions?: Region[]; markdown_ranges?: Range[]; reviewed: boolean; previous_page?: number };
type ReviewDocument = { ok: boolean; task_id?: string; error?: string; markdown: string; sha256: string;
  review_revision: string; warnings: ReviewWarning[]; blocks: { id: string; markdown_ranges: Range[] }[];
  pages: { page: number }[]; source: { available: boolean; changed: boolean; pages: number }; notice?: string;
  reviewed_count: number; pending_count: number; conflict?: boolean; markdown_saved?: boolean; cancelled?: boolean };
type ReviewPage = { ok: boolean; url: string; width: number; height: number; page: number; error?: string };
type Dependencies = { call: <T>(method: string, ...args: unknown[]) => Promise<T>; notify: (text: string) => void;
  changed: () => Promise<void>; bindAction: (id: string, handler: () => void) => void; openExternal: (url: string) => void;
  loading: (label: string, scope?: string) => () => void;
  failLoading: (message: string, retry: () => void, scope: string) => void;
  clearLoading: (scope: string) => void };
type ReviewTask = { id: string; name: string; status: string };
const labels: Record<string, string> = { low_ocr_confidence: '文字识别', invalid_formula_syntax: '公式语法',
  formula_fallback: '公式识别', table_fallback: '表格结构', possible_table_continuation: '跨页续表', empty_page_result: '页面内容' };
const highlight = StateEffect.define<Range[]>();
const highlightField = StateField.define<DecorationSet>({
  create: () => Decoration.none,
  update(value, transaction) {
    value = value.map(transaction.changes);
    for (const effect of transaction.effects) if (effect.is(highlight)) {
      value = Decoration.set(effect.value.filter(([from, to]) => to > from)
        .map(([from, to]) => Decoration.mark({ class: 'review-source-highlight' }).range(from, to)), true);
    }
    return value;
  },
  provide: field => EditorView.decorations.from(field),
});

const markup = `
<section id="review-workspace" class="review-workspace" aria-labelledby="review-heading" hidden>
  <header class="review-header">
    <button id="review-back" onclick="window.pdf2mdAction(event)" class="button outline" type="button">返回转换</button>
    <div class="review-title"><h1 id="review-heading">对照核对</h1><label class="sr-only" for="review-task-select">核对文档</label><select id="review-task-select"></select></div>
    <span id="review-progress" class="review-progress" role="status"></span>
    <span id="review-save-status" class="review-save-status" role="status">正在读取</span>
    <button id="review-copy" onclick="window.pdf2mdAction(event)" class="button outline" type="button">复制草稿</button>
    <button id="review-save" onclick="window.pdf2mdAction(event)" class="button primary" type="button" disabled>保存</button>
  </header>
  <div id="review-notice" class="review-notice" role="status" hidden></div>
  <div id="review-error" class="review-error" role="alert" hidden><span></span><button id="review-reload" onclick="window.pdf2mdAction(event)" class="button outline" type="button">重新载入</button></div>
  <div id="review-loading" class="review-loading"><span class="spinner" aria-hidden="true"></span>正在读取完整正文和核对记录</div>
  <div id="review-body" class="review-body" hidden>
    <aside id="review-issues" class="review-issues" aria-labelledby="review-issues-heading">
      <div class="review-issues-heading"><h2 id="review-issues-heading">问题列表 <span id="review-issue-count"></span></h2><button id="review-issues-toggle" onclick="window.pdf2mdAction(event)" class="text-button" type="button" aria-expanded="true" aria-controls="review-issue-content" hidden>展开</button></div>
      <div id="review-issue-content" class="review-issue-content">
      <div class="review-filter view-switch" role="group" aria-label="问题筛选"><button id="review-filter-pending" onclick="window.pdf2mdAction(event)" type="button" aria-pressed="true">待核对</button><button id="review-filter-all" onclick="window.pdf2mdAction(event)" type="button" aria-pressed="false">全部</button></div>
      <div class="review-nav"><button id="review-previous-issue" onclick="window.pdf2mdAction(event)" class="button outline" type="button">上一项</button><button id="review-next-issue" onclick="window.pdf2mdAction(event)" class="button outline" type="button">下一项</button></div>
      <div class="review-issue-scroll"><ul id="review-issue-list" aria-label="需要核对的问题"></ul><p id="review-issues-empty" class="review-empty" hidden></p><button id="review-more" onclick="window.pdf2mdAction(event)" class="button outline" type="button" hidden>再显示 50 项</button></div>
      <footer class="review-issue-footer"><p id="review-current-description"></p><button id="review-mark" onclick="window.pdf2mdAction(event)" class="button primary" type="button" disabled>标记已核对</button><button id="review-unmark" onclick="window.pdf2mdAction(event)" class="button outline" type="button" hidden>撤销核对</button></footer>
      </div>
    </aside>
    <section class="review-pdf" aria-labelledby="review-pdf-heading">
      <header class="review-pane-heading"><h2 id="review-pdf-heading">原 PDF</h2><button id="review-relink" onclick="window.pdf2mdAction(event)" class="text-button" type="button">重新关联原 PDF</button></header>
      <div class="review-pdf-toolbar"><button id="review-prev-page" onclick="window.pdf2mdAction(event)" class="icon-button" type="button" aria-label="上一页" title="上一页">‹</button><label for="review-page-number">第</label><input id="review-page-number" type="text" inputmode="numeric" value="1" aria-label="PDF 页码" autocomplete="off"><span id="review-page-total">/ 1 页</span><button id="review-next-page" onclick="window.pdf2mdAction(event)" class="icon-button" type="button" aria-label="下一页" title="下一页">›</button><span class="review-toolbar-spacer"></span><button id="review-zoom-out" onclick="window.pdf2mdAction(event)" class="icon-button" type="button" aria-label="缩小 PDF" title="缩小 PDF">−</button><span id="review-zoom-label">适宽</span><button id="review-zoom-in" onclick="window.pdf2mdAction(event)" class="icon-button" type="button" aria-label="放大 PDF" title="放大 PDF">＋</button><button id="review-fit" onclick="window.pdf2mdAction(event)" class="text-button" type="button">适应宽度</button></div>
      <div id="review-pdf-status" class="review-pdf-status" role="status"></div>
      <div id="review-pdf-scroll" class="review-pdf-scroll" tabindex="0" role="region" aria-label="原 PDF 页面滚动区域"><div id="review-pdf-paper" class="review-pdf-paper" hidden><img id="review-pdf-image" alt="原 PDF 页面"><svg id="review-pdf-overlay" viewBox="0 0 1000 1000" preserveAspectRatio="none" aria-label="当前问题在原 PDF 中的位置"></svg></div></div>
      <footer class="review-pdf-footer"><span id="review-region-note">点击问题，查看对应原文</span><button id="review-continuation" onclick="window.pdf2mdAction(event)" class="text-button" type="button" hidden>查看上一页</button></footer>
    </section>
    <section class="review-markdown" aria-labelledby="review-markdown-heading">
      <header class="review-pane-heading"><h2 id="review-markdown-heading">Markdown 源码</h2><span class="review-shortcut">Ctrl / ⌘ + S 保存</span></header>
      <div id="review-editor" class="review-editor"></div>
      <header class="review-pane-heading review-render-heading"><h2>实时预览</h2><span id="review-location-note">按段落同步定位</span></header>
      <iframe id="review-rendered" title="核对中的 Markdown 实时预览" sandbox="allow-same-origin" referrerpolicy="no-referrer"></iframe>
    </section>
  </div>
</section>
<dialog id="review-leave-dialog" aria-labelledby="review-leave-title" aria-describedby="review-leave-description"><div class="dialog-body"><h2 id="review-leave-title">保存 Markdown 修改？</h2><p id="review-leave-description">当前草稿尚未保存。离开前可以保存，也可以放弃本次修改。</p></div><div class="dialog-actions"><button id="review-leave-continue" onclick="window.pdf2mdAction(event)" class="button outline" type="button" autofocus>继续编辑</button><button id="review-leave-discard" onclick="window.pdf2mdAction(event)" class="button outline" type="button">放弃修改</button><button id="review-leave-save" onclick="window.pdf2mdAction(event)" class="button primary" type="button">保存并离开</button></div></dialog>`;

export function setupReview(deps: Dependencies) {
  const shell = document.createElement('div'); shell.innerHTML = markup; document.body.append(...shell.childNodes);
  const $ = <T extends HTMLElement = HTMLElement>(id: string) => document.getElementById(id) as T;
  const workspace = $('review-workspace');
  const main = document.querySelector<HTMLElement>('main.workspace')!;
  const frame = $<HTMLIFrameElement>('review-rendered');
  const leaveDialog = $<HTMLDialogElement>('review-leave-dialog');
  const narrow = window.matchMedia('(max-width:1100px)');
  const arrangeIssues = () => { $('review-issue-content').hidden = narrow.matches;
    $('review-issues-toggle').hidden = !narrow.matches; $('review-issues-toggle').setAttribute('aria-expanded', String(!narrow.matches)); $('review-issues-toggle').textContent = narrow.matches ? '展开' : '收起'; };
  narrow.addEventListener('change', arrangeIssues); arrangeIssues();
  let taskId = '', taskName = '', data: ReviewDocument | null = null, current = '';
  let editor: EditorView | null = null, cumulative = ChangeSet.empty(0), savedText = '';
  let dirty = false, busy = false, loading = false, editorReadOnly = false, pendingOnly = true, visibleCount = 50;
  let page = 1, zoom = 1, fit = true, imageWidth = 0, imageHeight = 0;
  let request = 0, pageRequest = 0, renderTimer = 0, leaveNext: (() => void) | null = null;
  let pageFinish = () => {};
  let documentFinish = () => {}, preparingEditorFinish = () => {};
  let tasks: ReviewTask[] = [];
  const readonly = new Compartment();
  const button = (id: string) => $<HTMLButtonElement>(id);
  const text = () => editor?.state.doc.toString() ?? savedText;
  const warning = () => data?.warnings.find(item => item.id === current);
  const filtered = () => data?.warnings.filter(item => !pendingOnly || !item.reviewed) ?? [];
  const error = (message: string, conflict = false) => {
    $('review-error').hidden = !message;
    $('review-error').querySelector('span')!.textContent = message;
    $('review-reload').hidden = !conflict;
  };
  const mappedRanges = (item?: ReviewWarning): Range[] => (item?.markdown_ranges ?? []).map(([from, to]) =>
    [cumulative.mapPos(from, -1), cumulative.mapPos(to, 1)] as Range).filter(([from, to]) => from >= 0 && to >= from && to <= (editor?.state.doc.length ?? 0));

  function updateControls() {
    const item = warning();
    button('review-save').disabled = busy || loading || !data || !dirty;
    button('review-copy').disabled = loading || !data;
    button('review-mark').disabled = busy || loading || !item || (item.reviewed && !dirty);
    button('review-mark').textContent = dirty ? '保存并标记已核对' : '标记已核对';
    $('review-unmark').hidden = !item?.reviewed;
    button('review-unmark').disabled = busy;
    button('review-back').disabled = busy;
    $<HTMLSelectElement>('review-task-select').disabled = busy;
    $('review-save-status').textContent = busy ? '正在保存…' : dirty ? '有未保存修改' : data ? '已保存' : '正在读取';
    $('review-save-status').classList.toggle('is-dirty', dirty);
    $('review-progress').textContent = data ? `已核对 ${data.reviewed_count} / ${data.warnings.length} 项` : '';
    $('review-progress').classList.toggle('is-complete', Boolean(data?.warnings.length && !data.pending_count));
    if (editor && editorReadOnly !== busy) { editorReadOnly = busy; editor.dispatch({ effects: readonly.reconfigure(EditorState.readOnly.of(busy)) }); }
  }

  function renderIssues() {
    if (!data) return;
    const items = filtered();
    const list = $('review-issue-list'); list.replaceChildren();
    $('review-issue-count').textContent = `${data.pending_count} 项待核对`;
    $('review-filter-pending').setAttribute('aria-pressed', String(pendingOnly));
    $('review-filter-all').setAttribute('aria-pressed', String(!pendingOnly));
    for (const item of items.slice(0, visibleCount)) {
      const li = document.createElement('li'), control = document.createElement('button');
      control.type = 'button'; control.className = 'review-issue'; control.dataset.warningId = item.id;
      control.setAttribute('aria-pressed', String(item.id === current));
      const heading = document.createElement('span'); heading.className = 'review-issue-label';
      const category = document.createElement('strong'); category.textContent = `第 ${item.page} 页 · ${labels[item.code] ?? '内容核对'}`;
      const status = document.createElement('span'); status.className = item.reviewed ? 'review-checked' : 'review-pending'; status.textContent = item.reviewed ? '✓ 已核对' : '待核对';
      heading.append(category, status);
      const description = document.createElement('span'); description.className = 'review-issue-message'; description.textContent = item.message;
      control.append(heading, description); control.addEventListener('click', () => select(item.id, true)); li.append(control); list.append(li);
    }
    $('review-issues-empty').hidden = items.length > 0;
    $('review-issues-empty').textContent = !data.warnings.length ? '此文档没有转换提示，可直接对照编辑。' : pendingOnly ? '全部问题已核对。切换到「全部」可查看或撤销核对。' : '没有可显示的问题。';
    $('review-more').hidden = items.length <= visibleCount;
    const index = items.findIndex(item => item.id === current);
    button('review-previous-issue').disabled = index <= 0;
    button('review-next-issue').disabled = !items.length || index >= items.length - 1;
    $('review-current-description').textContent = warning()?.message ?? '选择一项提示后，对照原文确认内容。';
    updateControls();
  }

  function highlightPreview(scroll: boolean) {
    const doc = frame.contentDocument;
    if (!doc) return;
    const ranges = mappedRanges(warning());
    const matches: HTMLElement[] = [];
    for (const element of doc.querySelectorAll<HTMLElement>('[data-source-start]')) {
      const from = Number(element.dataset.sourceStart), to = Number(element.dataset.sourceEnd);
      const match = ranges.some(([start, end]) => start === end ? start >= from && start < to : start < to && end > from);
      element.classList.toggle('review-preview-highlight', match);
      if (match) matches.push(element);
    }
    if (scroll) matches.sort((a, b) => (Number(a.dataset.sourceEnd) - Number(a.dataset.sourceStart)) - (Number(b.dataset.sourceEnd) - Number(b.dataset.sourceStart)))[0]?.scrollIntoView({ block: 'center' });
  }

  function renderPreview() {
    if (!data || !editor) return;
    frame.srcdoc = markdownDocument(text(), taskId, true);
  }
  frame.addEventListener('load', () => { externalPreviewLinks(frame, deps.openExternal); highlightPreview(true); });

  function drawOverlay() {
    const svg = $('review-pdf-overlay'); svg.replaceChildren();
    const item = warning();
    const regions = item?.source_regions?.filter(region => region.page === page) ?? [];
    for (const region of regions) {
      const polygon = document.createElementNS('http://www.w3.org/2000/svg', 'polygon');
      polygon.setAttribute('points', region.polygon.map(([x, y]) => `${Math.min(1, Math.max(0, x)) * 1000},${Math.min(1, Math.max(0, y)) * 1000}`).join(' '));
      svg.append(polygon);
    }
    $('review-region-note').textContent = regions.length ? '高亮区域对应当前问题' : item ? '此页仅提供页级定位，请对照整页原文。' : '点击问题，查看对应原文';
    $('review-continuation').hidden = !item?.previous_page && item?.code !== 'possible_table_continuation';
    const total = data?.source.pages || 1;
    $<HTMLInputElement>('review-page-number').value = String(page);
    $('review-page-total').textContent = `/ ${total} 页`;
    const unavailable = !data?.source.available || data.source.changed;
    for (const id of ['review-prev-page', 'review-next-page', 'review-zoom-out', 'review-zoom-in', 'review-fit']) button(id).disabled = unavailable;
    button('review-prev-page').disabled ||= page <= 1;
    button('review-next-page').disabled ||= page >= total;
    $<HTMLInputElement>('review-page-number').disabled = unavailable;
  }

  function sizePaper() {
    if (!imageWidth || !imageHeight) return;
    const paper = $('review-pdf-paper'), scroller = $('review-pdf-scroll');
    const width = fit ? Math.max(160, scroller.clientWidth - 32) : imageWidth / 1.5 * zoom;
    paper.style.width = `${width}px`; paper.style.aspectRatio = `${imageWidth} / ${imageHeight}`;
    $('review-zoom-label').textContent = fit ? '适宽' : `${Math.round(zoom * 100)}%`;
  }
  new ResizeObserver(() => { if (!workspace.hidden && fit) sizePaper(); }).observe($('review-pdf-scroll'));

  async function showPage(next: number) {
    if (!data) return;
    pageFinish(); pageFinish = () => {};
    deps.clearLoading('review-page');
    page = Math.max(1, Math.min(data.source.pages || 1, Math.floor(next)));
    drawOverlay();
    const rid = ++pageRequest, id = taskId;
    $('review-pdf-paper').hidden = true;
    if (!data.source.available || data.source.changed) {
      $('review-pdf-status').textContent = data.source.changed ? '原 PDF 内容已变化。请重新关联原文件或重新转换，再查看区域定位。' : '找不到原 PDF。重新关联后即可查看原文，Markdown 仍可编辑保存。';
      deps.failLoading(data.source.changed ? '原 PDF 已变化，请重新关联原文件' : '找不到原 PDF，请重新关联原文件',
        () => { button('review-relink').click(); }, 'review-page');
      return;
    }
    pageFinish = deps.loading(`正在读取原 PDF 第 ${page} 页…`, 'review-page');
    $('review-pdf-status').textContent = `正在读取原 PDF 第 ${page} 页…`;
    try {
      const reply = await deps.call<ReviewPage>('review_page', id, page, 1.5);
      if (rid !== pageRequest || id !== taskId) return;
      if (!reply.ok) throw new Error(reply.error || '无法读取 PDF 页面，请检查源文件。');
      const url = new URL(reply.url, location.origin);
      if (url.origin !== location.origin) throw new Error('PDF 页面资源地址无效。');
      imageWidth = reply.width; imageHeight = reply.height;
      const image = $<HTMLImageElement>('review-pdf-image');
      image.onload = () => {
        if (rid !== pageRequest) return;
        pageFinish(); pageFinish = () => {};
        $('review-pdf-paper').hidden = false; $('review-pdf-status').textContent = '';
        sizePaper(); drawOverlay();
        const region = warning()?.source_regions?.find(item => item.page === page);
        if (region) {
          const xs = region.polygon.map(point => point[0]), ys = region.polygon.map(point => point[1]);
          const scroller = $('review-pdf-scroll'), paper = $('review-pdf-paper');
          scroller.scrollTop = Math.max(0, (Math.min(...ys) + Math.max(...ys)) / 2 * paper.clientHeight - scroller.clientHeight / 2);
          scroller.scrollLeft = Math.max(0, (Math.min(...xs) + Math.max(...xs)) / 2 * paper.clientWidth - scroller.clientWidth / 2);
        } else $('review-pdf-scroll').scrollTop = 0;
      };
      image.onerror = () => { if (rid === pageRequest) { pageFinish(); pageFinish = () => {};
        $('review-pdf-status').textContent = 'PDF 页面载入失败，请重试或重新关联原文件。';
        deps.failLoading('原 PDF 页面载入失败，请重试', () => { void showPage(next); }, 'review-page'); } };
      image.alt = `原 PDF 第 ${page} 页`; image.src = url.href;
    } catch (failure) { if (rid === pageRequest) { pageFinish(); pageFinish = () => {};
      $('review-pdf-status').textContent = failure instanceof Error ? failure.message : String(failure);
      deps.failLoading('原 PDF 页面读取失败，请重试', () => { void showPage(next); }, 'review-page'); } }
  }

  function select(id: string, focus = false) {
    current = id;
    const item = warning();
    if (!item) { renderIssues(); return; }
    const ranges = mappedRanges(item);
    if (editor) {
      editor.dispatch({ effects: highlight.of(ranges), ...(ranges.length ? { selection: { anchor: ranges[0][0], head: ranges[0][1] }, scrollIntoView: true } : {}) });
      if (focus) editor.focus();
    }
    $('review-location-note').textContent = ranges.length ? '已定位对应内容' : '此提示没有可靠的正文定位，请对照该页核对';
    const listIndex = filtered().findIndex(value => value.id === id);
    if (listIndex >= visibleCount) visibleCount = Math.ceil((listIndex + 1) / 50) * 50;
    renderIssues(); highlightPreview(true); void showPage(item.page);
  }

  function createEditor(value: string) {
    editor?.destroy(); $('review-editor').replaceChildren();
    editorReadOnly = false;
    cumulative = ChangeSet.empty(value.length); savedText = value; dirty = false;
    editor = new EditorView({ parent: $('review-editor'), state: EditorState.create({ doc: value, extensions: [
      lineNumbers(), history(), markdown(), syntaxHighlighting(defaultHighlightStyle), highlightActiveLine(), EditorView.lineWrapping,
      highlightField, readonly.of(EditorState.readOnly.of(false)),
      EditorView.contentAttributes.of({ 'aria-label': '可编辑的 Markdown 源码', spellcheck: 'false' }),
      keymap.of([{ key: 'Mod-s', run: view => { if (!view.composing) void save(); return true; } }, ...defaultKeymap, ...historyKeymap]),
      EditorView.updateListener.of(update => {
        if (!update.docChanged) return;
        cumulative = cumulative.compose(update.changes);
        dirty = update.state.doc.toString() !== savedText;
        // No preview replacement during an active IME composition.
        window.clearTimeout(renderTimer);
        renderTimer = window.setTimeout(() => { if (!editor?.composing) renderPreview(); }, 250);
        updateControls();
      }),
    ] }) });
    editor.contentDOM.addEventListener('compositionend', () => { window.clearTimeout(renderTimer); renderTimer = window.setTimeout(renderPreview, 80); });
    renderPreview();
  }

  function acceptDocument(reply: ReviewDocument, replaceEditor = true) {
    data = reply;
    $('review-notice').hidden = !reply.notice; $('review-notice').textContent = reply.notice ?? '';
    if (replaceEditor) createEditor(reply.markdown);
    else { savedText = reply.markdown; dirty = false; cumulative = ChangeSet.empty(reply.markdown.length); }
    $('review-body').hidden = false;
    renderIssues();
  }

  async function load(id: string, name: string, warningId = '') {
    const rid = ++request; ++pageRequest;
    pageFinish(); documentFinish(); preparingEditorFinish(); pageFinish = () => {};
    for (const scope of ['review-page','review-document','review-editor']) deps.clearLoading(scope);
    const finish = deps.loading('正在读取核对文档…', 'review-document'); documentFinish = finish;
    taskId = id; taskName = name; current = warningId; loading = true; data = null;
    workspace.hidden = false; main.hidden = true; document.body.classList.add('is-reviewing');
    document.title = `${name || '文档'} · 对照核对 · PDF2MD`;
    $('review-loading').hidden = false; $('review-body').hidden = true; error('');
    updateTaskSelect(); updateControls();
    try {
      const reply = await deps.call<ReviewDocument>('review_document', id);
      if (rid !== request) return;
      if (!reply.ok) throw new Error(reply.error || '无法读取核对文档。');
      const editorFinish = deps.loading('正在准备 Markdown 编辑器…', 'review-editor'); preparingEditorFinish = editorFinish;
      try {
        await new Promise<void>(resolve => requestAnimationFrame(() => resolve()));
        if (rid !== request) return;
        acceptDocument(reply);
      } finally { editorFinish(); }
      if (!reply.warnings.some(item => item.id === current)) current = reply.warnings.find(item => !item.reviewed)?.id ?? reply.warnings[0]?.id ?? '';
      if (current && warning()?.reviewed) pendingOnly = false;
      if (current) select(current);
      else { renderIssues(); void showPage(1); }
    } catch (failure) { if (rid === request) {
      error(failure instanceof Error ? failure.message : String(failure), true);
      deps.failLoading('核对文档读取失败，请重试', () => { void load(id, name, warningId); }, 'review-document');
    } }
    finally { finish(); if (rid === request) { loading = false; $('review-loading').hidden = true; updateControls(); } }
  }

  async function save(updates: Record<string, boolean> = {}): Promise<boolean> {
    if (!data || !editor || busy || loading || editor.composing) return false;
    const value = text();
    if (new TextEncoder().encode(value).length > 32 * 1024 * 1024) { error('正文超过 32 MiB 保存上限。请复制草稿，缩减内容后重试；原文件未改变。'); return false; }
    const changes: { from: number; to: number; insert: string }[] = [];
    if (dirty) cumulative.iterChanges((from, to, _fromB, _toB, insert) => changes.push({ from, to, insert: insert.toString() }));
    busy = true; error(''); updateControls();
    try {
      const reply = await deps.call<ReviewDocument>('save_review', taskId, { markdown: value, expected_sha256: data.sha256,
        expected_revision: data.review_revision, changes, review_updates: updates });
      if (!reply.ok) {
        if (reply.markdown_saved) { savedText = value; dirty = false; cumulative = ChangeSet.empty(value.length); if (reply.sha256) data.sha256 = reply.sha256; }
        error(reply.error || '保存失败，当前草稿已保留，请重试。', Boolean(reply.conflict || reply.markdown_saved));
        return false;
      }
      acceptDocument(reply, false);
      if (Object.values(updates).some(Boolean) && pendingOnly) {
        const next = data!.warnings.find(item => !item.reviewed);
        current = next?.id ?? current;
      }
      renderIssues();
      if (warning()) select(current);
      deps.notify(reply.warnings.length && !reply.pending_count ? '全部问题已核对，任务状态已更新' : Object.keys(updates).length ? '核对进度已保存' : 'Markdown 已保存');
      await deps.changed();
      return true;
    } catch (failure) { error(`保存失败：${failure instanceof Error ? failure.message : String(failure)}。当前草稿已保留。`); return false; }
    finally { busy = false; updateControls(); }
  }

  function close() {
    ++request; ++pageRequest; workspace.hidden = true; main.hidden = false;
    pageFinish(); documentFinish(); preparingEditorFinish(); pageFinish = documentFinish = preparingEditorFinish = () => {};
    for (const scope of ['review-page','review-document','review-editor']) deps.clearLoading(scope);
    window.clearTimeout(renderTimer); loading = false;
    document.body.classList.remove('is-reviewing'); document.title = 'PDF2MD · PDF 转 Markdown';
    editor?.destroy(); editor = null; taskId = ''; data = null; dirty = false;
    void deps.changed(); document.getElementById('review-button')?.focus();
  }

  function guard(next: () => void) {
    if (busy) return;
    if (!dirty) {
      ++request; ++pageRequest; loading = false;
      pageFinish(); documentFinish(); preparingEditorFinish(); pageFinish = documentFinish = preparingEditorFinish = () => {};
      for (const scope of ['review-page','review-document','review-editor']) deps.clearLoading(scope);
      window.clearTimeout(renderTimer);
      next(); return;
    }
    leaveNext = next;
    if (!leaveDialog.open) { leaveDialog.showModal(); button('review-leave-continue').focus(); }
  }

  function updateTaskSelect() {
    const select = $<HTMLSelectElement>('review-task-select'); select.replaceChildren();
    const options = tasks.filter(task => task.status.startsWith('succeeded'));
    if (taskId && !options.some(task => task.id === taskId)) options.push({ id: taskId, name: taskName || '当前文档', status: 'succeeded' });
    for (const task of options) { const option = document.createElement('option'); option.value = task.id; option.textContent = task.name; select.append(option); }
    select.value = taskId;
  }

  deps.bindAction('review-back', () => guard(close));
  deps.bindAction('review-issues-toggle', () => { const content = $('review-issue-content'); content.hidden = !content.hidden;
    $('review-issues-toggle').setAttribute('aria-expanded', String(!content.hidden)); $('review-issues-toggle').textContent = content.hidden ? '展开' : '收起'; });
  deps.bindAction('review-save', () => { void save(); });
  deps.bindAction('review-mark', () => { if (current) void save({ [current]: true }); });
  deps.bindAction('review-unmark', () => { if (current) void save({ [current]: false }); });
  deps.bindAction('review-copy', async () => {
    try { await navigator.clipboard.writeText(text()); deps.notify('已复制完整草稿'); }
    catch { editor?.focus(); editor?.dispatch({ selection: { anchor: 0, head: editor.state.doc.length } }); error('无法访问剪贴板。源码已全选，请按 Ctrl/Cmd+C 复制。'); }
  });
  deps.bindAction('review-reload', () => guard(() => { void load(taskId, taskName, current); }));
  $('review-task-select').addEventListener('change', event => {
    const id = (event.target as HTMLSelectElement).value, task = tasks.find(item => item.id === id);
    $<HTMLSelectElement>('review-task-select').value = taskId;
    if (task && id !== taskId) guard(() => { void load(id, task.name); });
  });
  deps.bindAction('review-filter-pending', () => { pendingOnly = true; visibleCount = 50; const first = filtered()[0]; if (first && !filtered().some(item => item.id === current)) select(first.id); else renderIssues(); });
  deps.bindAction('review-filter-all', () => { pendingOnly = false; visibleCount = 50; renderIssues(); });
  deps.bindAction('review-more', () => { visibleCount += 50; renderIssues(); });
  deps.bindAction('review-previous-issue', () => { const items = filtered(), index = items.findIndex(item => item.id === current); if (index > 0) select(items[index - 1].id, true); });
  deps.bindAction('review-next-issue', () => { const items = filtered(), index = items.findIndex(item => item.id === current); if (items[index + 1]) select(items[index + 1].id, true); });
  deps.bindAction('review-prev-page', () => { void showPage(page - 1); });
  deps.bindAction('review-next-page', () => { void showPage(page + 1); });
  $('review-page-number').addEventListener('change', () => {
    const field = $<HTMLInputElement>('review-page-number'), value = Number(field.value);
    if (!/^\d+$/.test(field.value) || value < 1 || value > (data?.source.pages || 1)) { field.value = String(page); deps.notify('请输入有效的 PDF 页码'); return; }
    void showPage(value);
  });
  $('review-page-number').addEventListener('keydown', event => { if (event.key === 'Enter' && !event.isComposing) $<HTMLInputElement>('review-page-number').blur(); });
  deps.bindAction('review-zoom-in', () => { fit = false; zoom = Math.min(3, zoom + .25); sizePaper(); });
  deps.bindAction('review-zoom-out', () => { fit = false; zoom = Math.max(.25, zoom - .25); sizePaper(); });
  deps.bindAction('review-fit', () => { fit = true; sizePaper(); });
  deps.bindAction('review-continuation', () => { const item = warning(); if (item) void showPage(item.previous_page || item.page - 1); });
  deps.bindAction('review-relink', async () => {
    if (busy || loading) return;
    try {
      const reply = await deps.call<ReviewDocument>('locate_review_source', taskId);
      if (!reply.ok) throw new Error(reply.error || '无法关联原 PDF。');
      if (reply.cancelled) return;
      const fresh = await deps.call<ReviewDocument>('review_document', taskId);
      if (!fresh.ok) throw new Error(fresh.error || '无法读取原 PDF。');
      if (data) { data.source = fresh.source; data.notice = fresh.notice; $('review-notice').hidden = !fresh.notice; $('review-notice').textContent = fresh.notice ?? ''; }
      void showPage(page);
    } catch (failure) { error(failure instanceof Error ? failure.message : String(failure)); }
  });
  deps.bindAction('review-leave-continue', () => { leaveDialog.close(); leaveNext = null; });
  deps.bindAction('review-leave-discard', () => { const next = leaveNext; leaveNext = null; dirty = false; leaveDialog.close(); next?.(); });
  deps.bindAction('review-leave-save', async () => {
    button('review-leave-save').disabled = true; button('review-leave-discard').disabled = true;
    try { if (await save()) { const next = leaveNext; leaveNext = null; leaveDialog.close(); next?.(); } else { leaveDialog.close(); leaveNext = null; } }
    finally { button('review-leave-save').disabled = false; button('review-leave-discard').disabled = false; }
  });
  leaveDialog.addEventListener('cancel', () => { leaveNext = null; });
  leaveDialog.addEventListener('keydown', event => {
    if (event.key !== 'Tab') return;
    const controls = [...leaveDialog.querySelectorAll<HTMLButtonElement>('button:not(:disabled)')], first = controls[0], last = controls.at(-1);
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
  });
  window.addEventListener('beforeunload', event => { if (dirty) event.preventDefault(); });
  return { open: (id: string, name: string, warningId = '') => guard(() => { pendingOnly = true; visibleCount = 50; fit = true; zoom = 1; void load(id, name, warningId); }),
    guard, isOpen: () => !workspace.hidden, setTasks: (items: ReviewTask[]) => { tasks = items; if (!workspace.hidden) updateTaskSelect(); } };
}
