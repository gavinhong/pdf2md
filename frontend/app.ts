import './styles.css';
import type { setupReview } from './review';
import { createIcons, Plus, FileInput, FileText, FileClock, FolderOpen, Play, Copy,
  ExternalLink, SlidersHorizontal, ChevronDown, ShieldCheck, BookOpenText,
  RotateCcw, Check, AlertTriangle, CircleX, LoaderCircle, Clock3 } from 'lucide';

type Task = { id: string; source: string; name: string; size: number; pages: number;
  status: string; phase: string; error: string; completed_pages: number; selected_pages: number;
  output_dir: string; markdown_file: string; elapsed: number; warnings: { id?: string; page: number; message: string; reviewed?: boolean }[] };
type Settings = { output_dir: string; page_range: string; mode: string; table_format: string;
  extract_images: boolean; remove_headers: boolean; formula: boolean };
type Snapshot = { revision: number; tasks: Task[]; settings: Settings; busy: boolean; validating: boolean;
  current: string; models: { ready: boolean; error: string; count: number };
  startup?: { state: 'pending'|'loading'|'ready'|'failed'; phase: string; error: string };
  warmup?: { state: 'pending'|'loading'|'ready'|'failed'; phase: string; profile: string; error: string } };
type Reply = { ok: boolean; error?: string; path?: string; added?: number; duplicates?: number;
  errors?: { name: string; message: string }[]; markdown?: string; next_offset?: number;
  has_more?: boolean; characters?: number };
declare global {
  interface Window {
    __PDF2MD_TOKEN__: string;
    pywebview?: { api: Record<string, (...args: unknown[]) => Promise<unknown>> };
    pdf2mdRequestClose: () => void;
    pdf2mdAction: (event: MouseEvent) => void;
    pdf2mdImported: (reply: Reply) => void;
  }
}

const $ = <T extends HTMLElement = HTMLElement>(id: string) => document.getElementById(id) as T;
const actionHandlers = new Map<string, () => void>();
const bindAction = (id: string, handler: () => void) => { actionHandlers.set(id, handler); };
window.pdf2mdAction = (event: MouseEvent) => {
  const button = event.currentTarget as HTMLButtonElement;
  if (!button.disabled) actionHandlers.get(button.id)?.();
};
const iconSet = { Plus, FileInput, FileText, FileClock, FolderOpen, Play, Copy, ExternalLink,
  SlidersHorizontal, ChevronDown, ShieldCheck, BookOpenText, RotateCcw, Check, AlertTriangle,
  CircleX, LoaderCircle, Clock3 };
const icons = () => createIcons({ icons: iconSet, attrs: { 'aria-hidden': 'true' } });
const done = (task?: Task) => Boolean(task?.status.startsWith('succeeded'));
const statusLabels: Record<string,string> = { draft:'待开始',queued:'等待中',running:'转换中',succeeded:'转换成功',succeeded_with_warnings:'完成 · 需核对',succeeded_reviewed:'完成 · 已核对',failed:'转换失败',cancelled:'已取消' };
const statusIcons: Record<string,string> = { draft:'clock-3',queued:'clock-3',running:'loader-circle',succeeded:'check',succeeded_with_warnings:'alert-triangle',succeeded_reviewed:'check',failed:'circle-x',cancelled:'circle-x' };
let state: Snapshot | null = null;
let selected = '';
let previewTask = '';
let markdown = '';
let nextOffset = 0;
let hasMore = false;
let sourceMode = false;
let settingsLoaded = false;
let previewRequest = 0;
let pendingSnapshot = false;
let refreshTimer = 0;
let refreshAgain = false;
let pendingStart = false;
let toastTimer = 0;
let connectionErrors = 0;
type ResumeState = { selected: string; settings: Settings; review?: { id: string; name: string; warningId: string } };
let resume: ResumeState | null = null;
try { const saved = sessionStorage.getItem('pdf2md-reload-once'); sessionStorage.removeItem('pdf2md-reload-once'); if (saved) resume = JSON.parse(saved); }
catch { /* Storage can be unavailable under an OS privacy policy. */ }
if (resume?.selected) selected = resume.selected;
let markdownModule: typeof import('./render-markdown') | null = null;
let markdownPromise: Promise<typeof import('./render-markdown')> | null = null;
let reviewModulePromise: Promise<typeof import('./review')> | null = null;
let review: ReturnType<typeof setupReview> | null = null;
let reviewOpenRequest = 0;
let documentRenderRequest = 0;
let prefetched = false;
let markdownFailed = false;
let activityId = 0;
const activities = new Map<number, string>();
const activityScopes = new Map<string, number>();
const loadingFailures = new Map<string, { message: string; retry: () => void; reload: boolean }>();
let lastWarmupState = '', completionUntil = 0, completionTimer = 0;
const initialized = () => !state?.startup || state.startup.state === 'ready';

function activityDisplay() {
  const foreground = [...activities.values()].at(-1);
  const loadFailure = [...loadingFailures.values()].at(-1);
  const startup = state?.startup;
  const warmup = state?.warmup;
  if ((warmup?.state || '') !== lastWarmupState) {
    lastWarmupState = warmup?.state || '';
    window.clearTimeout(completionTimer);
    completionUntil = warmup?.state === 'ready' ? performance.now() + 2000 : 0;
    if (completionUntil) completionTimer = window.setTimeout(activityDisplay, 2050);
  }
  const starting = startup && startup.state !== 'ready';
  const warming = (!startup || startup.state === 'ready') && (warmup?.state === 'loading' || warmup?.state === 'pending');
  let label = foreground || loadFailure?.message || (starting ? startup.phase || '正在准备工作台…' :
    warmup?.state === 'failed' ? '识别预热未完成，可重试' : warming ? warmup.phase || '正在后台准备识别…' :
    warmup?.state === 'ready' && performance.now() < completionUntil ? '识别预热已完成' : '');
  if (!foreground && startup?.state === 'failed') label = startup.error || '工作台准备失败，请重试';
  $('activity-label').textContent = label;
  $('activity-background').textContent = (foreground || loadFailure) && warming ? `后台：${warmup?.phase || '识别预热中'}` :
    !foreground && warmup?.state === 'failed' ? warmup.error || '可以重新准备识别，已完成结果仍可查看。' : '';
  $('activity-spinner').hidden = !foreground && (Boolean(loadFailure) || (!(starting && startup?.state !== 'failed') && !warming));
  $('startup-activity').classList.toggle('has-error', Boolean(!foreground && (loadFailure || startup?.state === 'failed' || warmup?.state === 'failed')));
  $('activity-retry').hidden = Boolean(foreground) || !(loadFailure || startup?.state === 'failed' || warmup?.state === 'failed');
  $('activity-retry').textContent = loadFailure?.reload ? '重新加载界面' : '重试';
  $('startup-activity').title = [label, $('activity-background').textContent].filter(Boolean).join('\n');
}

function clearActivity(scope: string) {
  const id = activityScopes.get(scope);
  loadingFailures.delete(scope);
  if (id !== undefined) { activities.delete(id); activityScopes.delete(scope); }
  activityDisplay();
}

function failLoading(message: string, retry: () => void, scope: string, reload = false) {
  loadingFailures.delete(scope); loadingFailures.set(scope, { message, retry, reload }); activityDisplay();
}

function beginActivity(label: string, scope?: string) {
  if (scope) clearActivity(scope);
  const id = ++activityId; activities.set(id, label); activityDisplay();
  if (scope) activityScopes.set(scope, id);
  return () => { activities.delete(id); if (scope && activityScopes.get(scope) === id) activityScopes.delete(scope); activityDisplay(); };
}

function getMarkdown() {
  if (!markdownPromise) markdownPromise = import('./render-markdown').then(module => { markdownModule = module; return module; })
    .catch(error => { markdownPromise = null; markdownFailed = true; throw error; });
  return markdownPromise;
}

function getReviewModule() {
  if (!reviewModulePromise) reviewModulePromise = import('./review').catch(error => { reviewModulePromise = null; throw error; });
  return reviewModulePromise;
}

function guardReview(next: () => void) { if (review) review.guard(next); else next(); }

function reloadWorkbench(target?: ResumeState['review']) {
  // A failed ESM graph is cached by browsers. A new document retries it reliably.
  // The shared review guard prevents refreshing a live unsaved editor.
  guardReview(() => {
    try { sessionStorage.setItem('pdf2md-reload-once', JSON.stringify({ selected, settings: settingsValue(), review: target })); }
    catch { notify('无法暂存当前设置，请重新打开界面后检查转换设置。'); }
    location.reload();
  });
}

async function openReview(id: string, name: string, warningId = '') {
  const request = ++reviewOpenRequest;
  ++previewRequest; ++documentRenderRequest; clearActivity('preview'); clearActivity('preview-render');
  const finish = beginActivity('正在准备核对编辑器…', 'editor-module');
  try {
    const module = await getReviewModule();
    if (request !== reviewOpenRequest) return;
    if (!review) review = module.setupReview({ call, notify, bindAction, loading: beginActivity, failLoading, clearLoading: clearActivity,
      changed: async () => { previewTask = ''; await refresh(); if (state) render(state); },
      openExternal: url => { void action(() => call('open_external', url)); } });
    review.setTasks(state?.tasks || []);
    review.open(id, name, warningId);
  } catch {
    if (request !== reviewOpenRequest) return;
    failLoading('核对编辑器加载失败，请重新加载界面', () => reloadWorkbench({ id, name, warningId }), 'editor-module', true);
    notify('核对编辑器加载失败，文档保持不变。请点击顶部「重新加载界面」。');
  } finally { finish(); }
}

async function call<T = Reply>(method: string, ...args: unknown[]): Promise<T> {
  let result: unknown;
  if (window.pywebview?.api?.[method]) {
    result = await window.pywebview.api[method](...args);
  } else {
    const response = await fetch(`/api/${method}`, { method:'POST', headers:{ 'Content-Type':'application/json',
      'X-PDF2MD-Token':window.__PDF2MD_TOKEN__ }, body:JSON.stringify({args}) });
    if (!response.ok) throw new Error('本地服务暂时无响应，请稍后重试。');
    result = await response.json();
  }
  return result as T;
}

function notify(message: string) {
  window.clearTimeout(toastTimer);
  $('notifications').textContent = message;
  toastTimer = window.setTimeout(() => { $('notifications').textContent = ''; }, 5000);
}

function showError(message: string) {
  $('settings-error').textContent = message;
  $('settings-error').hidden = false;
  const input = /页码/.test(message) ? $<HTMLInputElement>('page-range') : $<HTMLInputElement>('output-dir');
  input.setAttribute('aria-invalid', 'true');
  input.focus();
}

function importFeedback(reply: Reply) {
  const errors = reply.errors || [];
  const feedback = $('import-feedback');
  feedback.hidden = errors.length === 0 && reply.ok;
  feedback.textContent = !reply.ok ? (reply.error || '文件添加失败，请重试。') :
    errors.map(error => `${error.name}：${error.message}`).join('\n');
  if (reply.added) notify(`已添加 ${reply.added} 个 PDF${reply.duplicates ? `，跳过 ${reply.duplicates} 个重复文件` : ''}`);
  if (!reply.added && reply.duplicates) notify('这些文件已在任务列表中。');
  void refresh();
}
window.pdf2mdImported = importFeedback;

function settingsValue(): Settings {
  return { output_dir:$<HTMLInputElement>('output-dir').value.trim(), page_range:$<HTMLInputElement>('page-range').value.trim(),
    mode:$<HTMLSelectElement>('conversion-mode').value, table_format:$<HTMLSelectElement>('table-format').value,
    extract_images:$<HTMLInputElement>('extract-images').checked, remove_headers:$<HTMLInputElement>('remove-headers').checked,
    formula:$<HTMLInputElement>('formula').checked };
}

function loadSettings(settings: Settings) {
  $<HTMLInputElement>('output-dir').value = settings.output_dir;
  $<HTMLInputElement>('page-range').value = settings.page_range;
  $<HTMLSelectElement>('conversion-mode').value = settings.mode;
  $<HTMLSelectElement>('table-format').value = settings.table_format;
  $<HTMLInputElement>('extract-images').checked = settings.extract_images;
  $<HTMLInputElement>('remove-headers').checked = settings.remove_headers;
  $<HTMLInputElement>('formula').checked = settings.formula;
}

function renderQueue(snapshot: Snapshot) {
  const list = $('task-list');
  const activeIds = new Set(snapshot.tasks.map(task => task.id));
  for (const child of [...list.children]) if (!activeIds.has((child as HTMLElement).dataset.id || '')) child.remove();
  for (const task of snapshot.tasks) {
    let row = list.querySelector<HTMLElement>(`li[data-id="${task.id}"]`);
    if (!row) {
      row = document.createElement('li'); row.dataset.id = task.id;
      row.innerHTML = '<button type="button" class="task-button"><span class="task-file-icon"><i data-lucide="file-text"></i></span><span class="task-info"><span class="task-name"></span><span class="task-meta"></span><span class="task-state"></span><progress class="task-progress" hidden></progress></span></button>';
      row.querySelector('button')!.addEventListener('click', () => { guardReview(() => { selected = task.id; if (state) render(state); }); });
      list.append(row);
    }
    const button = row.querySelector('button')!;
    button.setAttribute('aria-pressed', String(selected === task.id));
    button.setAttribute('aria-label', `${task.name}，${statusLabels[task.status] || task.status}`);
    button.title = task.source;
    row.querySelector('.task-name')!.textContent = task.name;
    row.querySelector('.task-meta')!.textContent = `${task.pages} 页 · ${(task.size / 1024**2).toFixed(1)} MB${task.elapsed ? ` · ${task.elapsed} 秒` : ''}`;
    const badge = row.querySelector<HTMLElement>('.task-state')!;
    const label = ['running','queued'].includes(task.status) && task.phase ? task.phase : statusLabels[task.status];
    if (badge.dataset.state !== `${task.status}:${label}`) {
      badge.dataset.state = `${task.status}:${label}`;
      badge.className = `task-state ${task.status}`;
      badge.replaceChildren();
      const icon = document.createElement('i'); icon.setAttribute('data-lucide', statusIcons[task.status] || 'clock-3');
      badge.append(icon, document.createTextNode(label));
    }
    const progress = row.querySelector('progress')!;
    progress.hidden = task.status !== 'running';
    progress.max = task.selected_pages || 1;
    if (task.completed_pages > 0) progress.value = task.completed_pages;
    else progress.removeAttribute('value');
    progress.setAttribute('aria-label', `已处理 ${task.completed_pages} / ${task.selected_pages} 页`);
  }
  icons();
}

function updateButtons() {
  const busy = Boolean(!state || !initialized() || state.busy || state.validating);
  const task = state?.tasks.find(item => item.id === selected);
  const ready = state?.tasks.some(item => item.status === 'draft');
  $<HTMLButtonElement>('add-button').disabled = busy;
  $<HTMLButtonElement>('remove-button').disabled = busy || !task;
  $<HTMLButtonElement>('clear-button').disabled = busy || !state?.tasks.length;
  $<HTMLFieldSetElement>('settings-fields').disabled = busy;
  const start = $<HTMLButtonElement>('start-button');
  start.disabled = busy || pendingStart || !ready || !state?.models.ready || !settingsValue().output_dir;
  start.title = !ready ? '请先添加 PDF 或重试失败任务' : !settingsValue().output_dir ? '请先选择输出目录' : !state?.models.ready ? '离线识别资源尚未准备好' : '';
  $('cancel-current').hidden = !state?.busy;
  $('cancel-all').hidden = !state?.busy;
  $<HTMLButtonElement>('copy-button').disabled = !done(task);
  $<HTMLButtonElement>('open-file').disabled = !done(task);
  $<HTMLButtonElement>('open-folder').disabled = !done(task);
  $<HTMLButtonElement>('review-button').disabled = !done(task);
  $<HTMLButtonElement>('open-review-result').disabled = !state || !initialized();
  $<HTMLButtonElement>('report-button').disabled = !state || !initialized();
  $('retry-button').hidden = !(task && ['failed','cancelled'].includes(task.status));
  $<HTMLButtonElement>('retry-button').disabled = busy;
}

function render(snapshot: Snapshot) {
  state = snapshot;
  review?.setTasks(snapshot.tasks);
  if (!settingsLoaded && initialized()) { loadSettings(resume?.settings || snapshot.settings); settingsLoaded = true; }
  activityDisplay();
  if (!snapshot.tasks.some(task => task.id === selected)) selected = snapshot.tasks[0]?.id || '';
  $('queue-count').textContent = String(snapshot.tasks.length);
  $('queue-empty').hidden = snapshot.tasks.length > 0;
  $('drop-zone').classList.toggle('compact', snapshot.tasks.length > 0);
  $('queue-summary').textContent = snapshot.tasks.length ? `${snapshot.tasks.length} 份文档 · ${snapshot.tasks.reduce((sum,task) => sum + task.pages,0)} 页` : '支持文字、扫描件和复杂版面';
  const status = $('model-status');
  const recognitionReady = snapshot.models.ready && (!snapshot.warmup || snapshot.warmup.state === 'ready');
  status.classList.toggle('not-ready', !recognitionReady);
  status.replaceChildren();
  const dot = document.createElement('span'); dot.className = 'status-dot';
  const resourceLabel = !initialized() ? '正在检查离线识别' : snapshot.warmup?.state === 'failed' ? '离线识别准备失败' :
    snapshot.warmup && ['loading','pending'].includes(snapshot.warmup.state) ? '正在准备离线识别' : recognitionReady ? '离线识别已就绪' : '离线识别未就绪';
  status.append(dot, document.createTextNode(resourceLabel));
  status.title = snapshot.warmup?.error || snapshot.models.error || (recognitionReady ? `${snapshot.models.count} 项本地识别资源` : snapshot.warmup?.phase || resourceLabel);
  const terminal = snapshot.tasks.filter(task => task.status.startsWith('succeeded') || ['failed','cancelled'].includes(task.status));
  const success = terminal.filter(done).length;
  const failures = terminal.filter(task => task.status === 'failed').length;
  const cancelled = terminal.filter(task => task.status === 'cancelled').length;
  $('batch-label').textContent = snapshot.validating ? '正在读取文件信息' : snapshot.busy ? '正在转换 · 可继续查看已完成结果' : terminal.length ? `成功 ${success} · 失败 ${failures} · 已取消 ${cancelled}` : '准备就绪';
  $('batch-count').textContent = snapshot.busy ? `${terminal.length} / ${snapshot.tasks.length}` : '';
  renderQueue(snapshot); updateButtons();
  const task = snapshot.tasks.find(item => item.id === selected);
  $('preview-empty').hidden = Boolean(task);
  $('preview-task').hidden = !task;
  if (!task) { previewTask = ''; ++previewRequest; ++documentRenderRequest; clearActivity('preview'); clearActivity('preview-render'); return; }
  $('document-title').textContent = task.name;
  $('document-meta').textContent = `${task.pages} 页 · ${statusLabels[task.status]}`;
  $('task-detail').textContent = task.error || (task.output_dir ? `已保存至 ${task.output_dir}` : task.source);
  $('task-detail').classList.toggle('has-error', Boolean(task.error));
  const warning = $('task-warning');
  warning.hidden = !task.warnings.length;
  warning.replaceChildren();
  if (task.warnings.length) {
    const summary = document.createElement('button'); summary.type = 'button'; summary.className = 'warning-summary';
    summary.textContent = `${task.warnings.length} 项转换提示 · 进入对照核对`;
    summary.addEventListener('click', () => { void openReview(task.id, task.name); }); warning.append(summary);
    for (const item of task.warnings.slice(0, 3)) {
      const entry = document.createElement('button'); entry.type = 'button';
      entry.textContent = `第 ${item.page} 页：${item.message}`;
      entry.addEventListener('click', () => { void openReview(task.id, task.name, item.id); }); warning.append(entry);
    }
  }
  const complete = done(task);
  $('preview-loading').hidden = complete || ['failed','cancelled'].includes(task.status);
  $('loading-label').textContent = task.status === 'running' ? task.phase : task.status === 'queued' ? '任务已排队，稍后开始转换' : '点击「开始转换」，完成后即可查看内容';
  if (!complete) {
    clearActivity('preview'); clearActivity('preview-render'); ++documentRenderRequest;
    $('rendered-preview').hidden = true; $('source-preview').hidden = true; $('load-more').hidden = true;
    previewTask = ''; previewRequest++;
    $('preview-caption').textContent = task.error ? '请修正问题后重试' : '转换结果将自动出现在这里';
    $('document-stat').textContent = task.status === 'running' ? `${task.completed_pages} / ${task.selected_pages} 页` : '本地文档';
  } else if (previewTask !== task.id) {
    previewTask = task.id; markdown = ''; nextOffset = 0;
    void loadPreview(false);
  }
}

async function refresh(immediate = false) {
  if (pendingSnapshot) { if (immediate) refreshAgain = true; return; }
  window.clearTimeout(refreshTimer);
  pendingSnapshot = true;
  try {
    const snapshot = await call<Snapshot>('snapshot'); connectionErrors = 0;
    if (!state || state.revision !== snapshot.revision || JSON.stringify(state.startup) !== JSON.stringify(snapshot.startup) || JSON.stringify(state.warmup) !== JSON.stringify(snapshot.warmup)) render(snapshot);
    if (initialized() && !prefetched) {
      prefetched = true;
      // The first document action reuses these promises, including while a download is pending.
      window.setTimeout(() => { void Promise.allSettled([getMarkdown(), getReviewModule()]); }, 0);
    }
    if (initialized() && resume) {
      const target = resume.review; resume = null;
      if (target && snapshot.tasks.some(task => task.id === target.id)) void openReview(target.id, target.name, target.warningId);
    }
  } catch (error) {
    if (++connectionErrors === 3) notify(error instanceof Error ? error.message : '本地服务无响应，请重新打开程序。');
  } finally {
    pendingSnapshot = false;
    if (refreshAgain) { refreshAgain = false; void refresh(); }
    else {
      const startupPending = !state || ['pending','loading'].includes(state.startup?.state || 'ready');
      refreshTimer = window.setTimeout(() => { void refresh(); }, startupPending ? 100 : 650);
    }
  }
}

async function renderDocument() {
  const task = state?.tasks.find(item => item.id === selected);
  if (!task) return;
  const request = ++documentRenderRequest, taskId = task.id, text = markdown;
  $<HTMLTextAreaElement>('source-preview').value = markdown;
  if (!sourceMode) {
    const finish = beginActivity('正在准备阅读预览…', 'preview-render');
    try {
      const renderer = await getMarkdown();
      if (request !== documentRenderRequest || selected !== taskId || text !== markdown || sourceMode) return;
      $<HTMLIFrameElement>('rendered-preview').srcdoc = renderer.markdownDocument(text, taskId);
    } finally { finish(); }
  }
  $('rendered-preview').hidden = sourceMode;
  $('source-preview').hidden = !sourceMode;
  $('load-more').hidden = !hasMore;
  $('preview-caption').textContent = sourceMode ? 'UTF-8 Markdown · 只读预览' : '顺序正文 · 表格与公式';
  $('document-stat').textContent = `${markdown.length.toLocaleString('zh-CN')} 字符${hasMore ? ' · 部分内容' : ''}`;
}

$<HTMLIFrameElement>('rendered-preview').addEventListener('load', () => {
  const frame = $<HTMLIFrameElement>('rendered-preview');
  markdownModule?.externalPreviewLinks(frame, href => { void action(() => call('open_external', href)); });
});

async function loadPreview(append: boolean) {
  const taskId = selected;
  const request = ++previewRequest;
  ++documentRenderRequest; clearActivity('preview-render');
  const finish = beginActivity('正在读取文档…', 'preview');
  $('preview-retry').hidden = true;
  $('preview-loading').hidden = false; $('loading-label').textContent = '正在读取文档';
  try {
    const reply = await call('preview', taskId, append ? nextOffset : 0);
    if (request !== previewRequest || selected !== taskId) return;
    if (!reply.ok) throw new Error(reply.error || '无法读取结果');
    markdown = append ? markdown + reply.markdown : reply.markdown || '';
    nextOffset = reply.next_offset || 0; hasMore = Boolean(reply.has_more);
    await renderDocument();
  } catch (error) {
    if (request !== previewRequest || selected !== taskId) return;
    $('task-detail').textContent = error instanceof Error ? error.message : '预览加载失败，请重试。';
    if (markdownFailed) {
      failLoading('阅读预览组件加载失败，请重新加载界面', () => reloadWorkbench(), 'preview', true);
      $('task-detail').textContent = '阅读预览组件加载失败。重新加载界面会保留当前设置和文档选择。';
    } else failLoading('文档预览读取失败，请重试', () => { void loadPreview(false); }, 'preview');
    $('preview-retry').hidden = false;
  } finally { finish(); if (request === previewRequest) $('preview-loading').hidden = true; }
}

async function action(operation: () => Promise<Reply>, success = '', immediate = false) {
  try { const reply = await operation(); if (!reply.ok) notify(reply.error || '操作未完成，请重试。'); else if (success) notify(success); await refresh(immediate); }
  catch (error) { notify(error instanceof Error ? error.message : String(error)); }
}

bindAction('add-button', () => { void action(async () => { const reply = await call('choose_files'); importFeedback(reply); return reply; }); });
bindAction('output-button', () => { void action(async () => { const reply = await call('choose_output'); if (reply.path) { $<HTMLInputElement>('output-dir').value = reply.path; updateButtons(); } return reply; }); });
$('settings-form').addEventListener('submit', event => { event.preventDefault(); });
$('settings-form').addEventListener('input', () => { $('settings-error').hidden = true; $('output-dir').removeAttribute('aria-invalid'); $('page-range').removeAttribute('aria-invalid'); updateButtons(); });
bindAction('start-button', async () => {
  if (pendingStart) return;
  pendingStart = true; updateButtons(); $('settings-error').hidden = true;
  try { const reply = await call('start', settingsValue()); if (!reply.ok) showError(reply.error || '请检查转换设置。'); else await refresh(); }
  catch (error) { showError(error instanceof Error ? error.message : String(error)); }
  finally { pendingStart = false; updateButtons(); }
});
bindAction('cancel-current', () => { void action(() => call('cancel', false), '正在取消当前任务'); });
bindAction('cancel-all', () => { void action(() => call('cancel', true), '正在取消全部任务'); });
bindAction('remove-button', () => { void action(() => call('remove', selected), '已移除选中任务，源文件和结果保持不变'); });
bindAction('clear-button', () => { void action(() => call('remove', ''), '已清空任务列表'); });
bindAction('retry-button', () => { void action(() => call('retry', selected), '已准备重试，请点击开始转换'); });
bindAction('open-folder', () => { void action(() => call('open_result', selected, true)); });
bindAction('open-file', () => { void action(() => call('open_result', selected, false)); });
bindAction('report-button', () => { void action(() => call('export_report')); });
bindAction('review-button', () => { const task = state?.tasks.find(item => item.id === selected); if (task && done(task)) void openReview(task.id, task.name); });
bindAction('open-review-result', () => { guardReview(() => { void (async () => {
  try { const reply = await call<{ok: boolean; task_id?: string; error?: string}>('open_review_result');
    if (!reply.ok) notify(reply.error || '无法打开已有结果');
    else if (reply.task_id) { selected = reply.task_id; await refresh(); const task = state?.tasks.find(item => item.id === selected); void openReview(selected, task?.name || '已有转换结果'); }
  } catch (error) { notify(error instanceof Error ? error.message : String(error)); }
})(); }); });
function selectMode(source: boolean) { sourceMode = source; $('render-tab').setAttribute('aria-pressed',String(!source)); $('source-tab').setAttribute('aria-pressed',String(source)); if (markdown) void renderDocument().catch(() => { $('task-detail').textContent = '阅读预览加载失败，请重试。'; $('preview-retry').hidden = false; failLoading('阅读预览组件加载失败，请重新加载界面', () => reloadWorkbench(), 'preview', true); }); }
bindAction('source-tab', () => selectMode(true));
bindAction('render-tab', () => selectMode(false));
bindAction('load-more', () => { void loadPreview(true); });
bindAction('preview-retry', () => { if (markdownFailed) reloadWorkbench(); else void loadPreview(false); });
bindAction('activity-retry', () => {
  const failure = [...loadingFailures.entries()].at(-1);
  if (failure) { const [scope, detail] = failure; clearActivity(scope); detail.retry(); }
  else void action(() => call(state?.startup?.state === 'failed' ? 'initialize' : 'retry_warmup'), '', true);
});
bindAction('copy-button', async () => {
  const task = state?.tasks.find(item => item.id === selected);
  if (!task || !done(task)) return;
  try {
    const name = task.markdown_file.split(/[\\/]/).pop() || '';
    const result = await fetch(`/resource/${encodeURIComponent(window.__PDF2MD_TOKEN__)}/${task.id}/${encodeURIComponent(name)}`);
    if (!result.ok) throw new Error('结果文件不存在，请重新转换。');
    const text = await result.text();
    try { await navigator.clipboard.writeText(text); }
    catch { const field = document.createElement('textarea'); field.value=text; field.style.position='fixed'; field.style.opacity='0'; field.style.resize='none'; document.body.append(field); field.select(); const ok=document.execCommand('copy'); field.remove(); if (!ok) throw new Error('无法写入剪贴板，请使用源码视图复制。'); }
    notify('已复制完整 Markdown');
  } catch (error) { notify(error instanceof Error ? error.message : String(error)); }
});
for (const eventName of ['dragenter','dragover']) document.addEventListener(eventName, event => { event.preventDefault(); if (!state?.busy) $('drop-zone').classList.add('drag-over'); });
for (const eventName of ['dragleave','drop']) document.addEventListener(eventName, event => { event.preventDefault(); $('drop-zone').classList.remove('drag-over'); });
const exitDialog = $<HTMLDialogElement>('exit-dialog');
exitDialog.addEventListener('keydown', event => {
  if (event.key !== 'Tab') return;
  const controls = [...exitDialog.querySelectorAll<HTMLButtonElement>('button:not(:disabled)')];
  const first = controls[0], last = controls[controls.length - 1];
  if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
});
window.pdf2mdRequestClose = () => { ++reviewOpenRequest; ++previewRequest; ++documentRenderRequest;
  clearActivity('editor-module'); clearActivity('preview'); clearActivity('preview-render'); guardReview(() => {
  if (state?.busy) { if (!exitDialog.open) exitDialog.showModal(); }
  else void action(() => call('finish_close'));
}); };
bindAction('keep-working', () => exitDialog.close());
bindAction('confirm-exit', () => { void action(() => call('finish_close')); });
document.addEventListener('keydown', event => {
  if (event.isComposing || event.target instanceof HTMLInputElement || event.target instanceof HTMLTextAreaElement || (event.target as HTMLElement).isContentEditable || review?.isOpen()) return;
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'o') { event.preventDefault(); $<HTMLButtonElement>('add-button').click(); }
});
icons();
updateButtons();
void refresh();
requestAnimationFrame(() => requestAnimationFrame(() => { void action(() => call('initialize'), '', true); }));
