import { test, expect, Page } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';
import { mkdirSync, readFileSync } from 'node:fs';

const original = '# 示例文档\n\n首段文字需要核对。\n\n公式：$x^2+y^2=z^2$\n\n末段内容。\n';
const span = (part: string) => [original.indexOf(part), original.indexOf(part) + part.length];

async function mockReview(page: Page, count = 2, options: { open?: boolean; startup?: boolean; restoredOutput?: string } = {}) {
  let document = original, revision = 1, saved = 0, conflict = false, source = true, changed = false, saveCalls = 0, pageCalls = 0;
  let startup = options.startup ? 'pending' : 'ready', warmup = options.startup ? 'pending' : 'ready';
  let initializeCalls = 0, warmupRetries = 0, closeCalls = 0;
  let queueWaiting = false;
  const warnings = Array.from({ length: count }, (_, index) => ({ id: `w${index}`, code: index % 2 ? 'invalid_formula_syntax' : 'low_ocr_confidence',
    page: index % 2 + 1, message: index % 2 ? '公式语法不完整，请对照原文核对。' : '该区域文字识别置信度偏低，请对照原文核对。',
    block_ids: [index % 2 ? 'b2' : 'b1'], markdown_ranges: [span(index % 2 ? '公式：$x^2+y^2=z^2$' : '首段文字需要核对。')], reviewed: false,
    source_regions: [{ page: index % 2 + 1, polygon: [[.1,.2],[.9,.2],[.9,.35],[.1,.35]] }] }));
  const doc = () => ({ ok: true, markdown: document, sha256: `sha-${revision}`, review_revision: String(revision), warnings,
    blocks: [{ id: 'b1', markdown_ranges: [span('首段文字需要核对。')] }, { id: 'b2', markdown_ranges: [span('公式：$x^2+y^2=z^2$')] }],
    pages: [{ page: 1 }, { page: 2 }], source: { available: source, changed, pages: 2 }, reviewed_count: warnings.filter(item => item.reviewed).length,
    pending_count: warnings.filter(item => !item.reviewed).length });
  await page.route('**/resource/mock-page.svg*', route => route.fulfill({ contentType: 'image/svg+xml', body:
    '<svg xmlns="http://www.w3.org/2000/svg" width="600" height="800"><rect width="600" height="800" fill="white"/><text x="60" y="190" font-size="24">Original PDF text</text></svg>' }));
  await page.route('**/api/**', async route => {
    const name = route.request().url().split('/').pop()!;
    const args = JSON.parse(route.request().postData() ?? '{}').args ?? [];
    let reply: unknown = { ok: true };
    if (name === 'snapshot') reply = { revision: revision + saved + 1, tasks: startup !== 'ready' ? [] : [{ id: 'review-test', name: '核对示例.pdf', source: 'example.pdf', size: 1000, pages: 2,
      status: queueWaiting ? 'queued' : warnings.every(item => item.reviewed) ? 'succeeded_reviewed' : 'succeeded_with_warnings', phase: queueWaiting ? '等待识别资源' : '', error: '', completed_pages: 2,
      selected_pages: 2, output_dir: 'output', markdown_file: 'output/example.md', elapsed: 3, warnings }],
      settings: { output_dir: startup === 'ready' ? options.restoredOutput || '' : '', page_range: '', mode: 'auto', table_format: 'auto', extract_images: true, remove_headers: true, formula: true },
      startup: { state: startup, phase: startup === 'ready' ? '工作台已就绪' : '正在恢复本地任务…', error: startup === 'failed' ? '本地任务读取失败，请重试。' : '' },
      warmup: { state: warmup, phase: '正在准备识别模型…', profile: 'default', error: warmup === 'failed' ? '测试预热失败，可重新准备。' : '' },
      busy: false, validating: false, current: '', models: { ready: startup === 'ready', error: '', count: 12 } };
    else if (name === 'initialize') { initializeCalls++; if (startup === 'pending' || startup === 'failed') { startup = 'loading'; revision++; } }
    else if (name === 'retry_warmup') { warmupRetries++; warmup = 'loading'; revision++; }
    else if (name === 'finish_close') closeCalls++;
    else if (name === 'review_document') reply = doc();
    else if (name === 'review_page') { pageCalls++; reply = { ok: true, url: `/resource/mock-page.svg?page=${args[1]}`, width: 900, height: 1200, page: args[1] }; }
    else if (name === 'preview') reply = { ok: true, markdown: document, has_more: false, next_offset: document.length };
    else if (name === 'open_review_result') reply = { ok: true, task_id: 'review-test' };
    else if (name === 'locate_review_source') { source = true; reply = { ok: true }; }
    else if (name === 'save_review') {
      saveCalls++;
      const payload = args[1];
      if (conflict || payload.expected_sha256 !== `sha-${revision}`) reply = { ok: false, conflict: true, error: '文件已被外部修改，当前草稿已保留。' };
      else {
        for (const change of payload.changes) for (const warning of warnings) if (warning.markdown_ranges.some(([from, to]) => change.from <= to && change.to >= from)) warning.reviewed = false;
        Object.entries(payload.review_updates).forEach(([id, checked]) => { warnings.find(item => item.id === id)!.reviewed = Boolean(checked); });
        document = payload.markdown; revision++; saved++; reply = doc();
      }
    }
    await route.fulfill({ json: reply });
  });
  await page.goto('/');
  if (options.open !== false) {
    await expect(page.locator('#document-title')).toHaveText('核对示例.pdf');
    await page.getByRole('button', { name: '对照核对', exact: true }).click();
    await expect(page.locator('#review-body')).toBeVisible();
  }
  return { conflict: () => { conflict = true; }, missingSource: () => { source = false; }, changeSource: () => { changed = true; },
    savedText: () => document, saveCalls: () => saveCalls, pageCalls: () => pageCalls,
    ready: () => { startup = 'ready'; warmup = 'loading'; revision++; }, failWarmup: () => { warmup = 'failed'; revision++; },
    failStartup: () => { startup = 'failed'; revision++; },
    warmupReady: () => { warmup = 'ready'; revision++; }, queueWaiting: () => { queueWaiting = true; revision++; },
    initializeCalls: () => initializeCalls, warmupRetries: () => warmupRetries, closeCalls: () => closeCalls };
}

test('review click location, edit, save, mark and revoke workflow', async ({ page }) => {
  const runtimeErrors: string[] = []; page.on('pageerror', error => runtimeErrors.push(error.message));
  const mock = await mockReview(page);
  await expect(page.locator('#review-pdf-image')).toBeVisible();
  await expect(page.locator('#review-pdf-overlay polygon')).toHaveCount(1);
  await expect(page.frameLocator('#review-rendered').locator('.review-preview-highlight')).not.toHaveCount(0);
  await expect(page.locator('#review-progress')).toHaveText('已核对 0 / 2 项');
  await page.locator('#review-mark').click();
  await expect(page.locator('#review-progress')).toHaveText('已核对 1 / 2 项');
  const editor = page.getByRole('textbox', { name: '可编辑的 Markdown 源码' });
  await editor.focus(); await page.keyboard.press('End'); await page.keyboard.insertText(' 已修正');
  await expect(page.locator('#review-save-status')).toHaveText('有未保存修改');
  await expect(page.locator('#review-mark')).toHaveText('保存并标记已核对');
  await page.locator('#review-mark').click();
  await expect(page.locator('#review-progress')).toHaveText('已核对 2 / 2 项');
  expect(mock.savedText()).toContain('已修正');
  await page.locator('#review-filter-all').click();
  await page.locator('[data-warning-id="w0"]').click();
  await page.locator('#review-unmark').click();
  await expect(page.locator('#review-progress')).toHaveText('已核对 1 / 2 项');
  await page.locator('#review-back').click();
  await expect(page.locator('.task-state')).toHaveText('完成 · 需核对');
  expect(runtimeErrors).toEqual([]);
});

test('review dirty navigation, conflict retains draft and keyboard save', async ({ page }) => {
  const mock = await mockReview(page);
  const editor = page.getByRole('textbox', { name: '可编辑的 Markdown 源码' });
  await editor.focus(); await page.keyboard.press('End'); await page.keyboard.insertText(' 草稿😀');
  await page.locator('#review-back').click();
  await expect(page.locator('#review-leave-dialog')).toBeVisible();
  await expect(page.locator('#review-leave-continue')).toBeFocused();
  await page.keyboard.press('Escape');
  await expect(page.locator('#review-workspace')).toBeVisible();
  mock.conflict(); await editor.focus(); await page.keyboard.press('Control+s');
  await expect(page.locator('#review-error')).toContainText('外部修改');
  await expect(editor).toContainText('草稿😀');
  await expect(page.locator('#review-save-status')).toHaveText('有未保存修改');
  await page.locator('#review-reload').click();
  await page.locator('#review-leave-discard').click();
  await expect(editor).not.toContainText('草稿😀');
});

test('review all issues reachable, narrow layout and accessibility', async ({ page }) => {
  await mockReview(page, 64);
  await expect(page.locator('#review-issue-list li')).toHaveCount(50);
  await page.locator('#review-more').click();
  await expect(page.locator('#review-issue-list li')).toHaveCount(64);
  await page.locator('[data-warning-id="w63"]').click();
  await expect(page.locator('#review-page-number')).toHaveValue('2');
  expect((await page.locator('#review-body').boundingBox())!.height).toBeLessThan(800);
  expect(await page.locator('.review-issue-scroll').evaluate(element => element.scrollHeight > element.clientHeight)).toBeTruthy();
  const result = await new AxeBuilder({ page }).exclude('#review-rendered').options({ iframes: false }).withTags(['wcag2a','wcag2aa','wcag21aa']).analyze();
  expect(result.violations).toEqual([]);
  mkdirSync('output/qa', { recursive: true });
  await page.screenshot({ path: 'output/qa/review-desktop.png', fullPage: true });
  await page.setViewportSize({ width: 760, height: 680 });
  await expect(page.locator('#review-issue-content')).toBeHidden();
  await page.locator('#review-issues-toggle').click();
  await page.locator('#review-editor').scrollIntoViewIfNeeded();
  await expect(page.locator('#review-editor')).toBeInViewport();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy();
  await page.screenshot({ path: 'output/qa/review-narrow.png', fullPage: true });
});

test('review browser IME composition delays save and updates Chinese preview after commit', async ({ page, context }) => {
  const mock = await mockReview(page);
  const editor = page.getByRole('textbox', { name: '可编辑的 Markdown 源码' });
  await editor.evaluate(element => {
    const events: { type: string; trusted: boolean }[] = [];
    (window as unknown as { __reviewCompositionEvents: typeof events }).__reviewCompositionEvents = events;
    for (const type of ['compositionstart', 'compositionend']) element.addEventListener(type, event => events.push({ type, trusted: event.isTrusted }));
  });
  await editor.focus(); await page.keyboard.press('End');
  // Chromium's IME input protocol produces composition and DOM edits. We do not
  // dispatch composition events ourselves or emulate a physical OS candidate panel.
  // Edge reports the protocol's commit compositionend as untrusted, so this remains
  // browser protocol coverage rather than a claim of Windows/macOS IME acceptance.
  const cdp = await context.newCDPSession(page);
  await cdp.send('Input.imeSetComposition', { text: '中文核对', selectionStart: 4, selectionEnd: 4 });
  await expect(editor).toContainText('中文核对');
  await expect(page.locator('#review-save-status')).toHaveText('有未保存修改');
  await page.keyboard.press('Control+s');
  await page.waitForTimeout(350); // Exceeds the preview debounce to verify the negative assertion.
  expect(mock.saveCalls()).toBe(0);
  expect(mock.savedText()).not.toContain('中文核对');
  await expect(page.frameLocator('#review-rendered').locator('body')).not.toContainText('中文核对');
  await cdp.send('Input.insertText', { text: '中文核对' });
  await expect(page.frameLocator('#review-rendered').locator('body')).toContainText('中文核对');
  await page.keyboard.press('Control+s');
  await expect(page.locator('#review-save-status')).toHaveText('已保存');
  expect(mock.savedText()).toContain('中文核对');
  const events = await page.evaluate(() => (window as unknown as { __reviewCompositionEvents: {type:string;trusted:boolean}[] }).__reviewCompositionEvents);
  expect(events).toContainEqual({ type: 'compositionstart', trusted: true });
  expect(events.some(event => event.type === 'compositionend')).toBeTruthy();
  await cdp.detach();
});

test('review changed source hides old PDF before displaying coordinates', async ({ page }) => {
  const mock = await mockReview(page);
  await expect(page.locator('#review-pdf-image')).toBeVisible();
  await expect(page.locator('#review-pdf-overlay polygon')).toHaveCount(1);
  const renderedPages = mock.pageCalls();
  mock.changeSource();
  await page.locator('#review-back').click();
  await page.getByRole('button', { name: '对照核对', exact: true }).click();
  await expect(page.locator('#review-pdf-status')).toContainText('原 PDF 内容已变化');
  await expect(page.locator('#review-pdf-paper')).toBeHidden();
  await expect(page.locator('#review-pdf-overlay')).toBeHidden();
  await expect(page.getByRole('textbox', { name: '可编辑的 Markdown 源码' })).toBeVisible();
  expect(mock.pageCalls()).toBe(renderedPages);
});

test('startup first paint initializes once, restores settings and retries background warmup', async ({ page }) => {
  const builtHtml = readFileSync('src/pdf2md/web/index.html', 'utf8');
  expect(builtHtml).toContain('<style>');
  expect(builtHtml).not.toContain('<link rel="stylesheet" href="/app.css">');
  expect(readFileSync('frontend/index.html', 'utf8')).toContain('<link rel="stylesheet" href="/app.css">');
  await page.addInitScript(() => {
    const original = window.requestAnimationFrame;
    (window as unknown as { startupFrames: number }).startupFrames = 0;
    window.requestAnimationFrame = callback => original.call(window, time => {
      (window as unknown as { startupFrames: number }).startupFrames++; callback(time);
    });
    const schedule = window.setTimeout.bind(window);
    const pollDelays: { delay: number; addEnabled: boolean }[] = [];
    (window as unknown as { startupPollDelays: typeof pollDelays }).startupPollDelays = pollDelays;
    window.setTimeout = ((handler: TimerHandler, delay?: number, ...args: unknown[]) => {
      if (delay === 100 || delay === 650) pollDelays.push({ delay, addEnabled: !(document.getElementById('add-button') as HTMLButtonElement | null)?.disabled });
      return schedule(handler, delay, ...args);
    }) as typeof window.setTimeout;
  });
  const mock = await mockReview(page, 2, { open: false, startup: true, restoredOutput: 'D:\\restored-output' });
  await expect(page.locator('#activity-label')).toContainText('恢复本地任务');
  await expect(page.locator('#add-button')).toBeDisabled();
  await expect(page.locator('#output-dir')).toBeDisabled();
  await expect.poll(mock.initializeCalls).toBe(1);
  expect(await page.evaluate(() => (window as unknown as {startupFrames:number}).startupFrames)).toBeGreaterThanOrEqual(2);
  const startingDelays = await page.evaluate(() => (window as unknown as { startupPollDelays: { delay: number; addEnabled: boolean }[] }).startupPollDelays);
  expect(startingDelays.some(item => item.delay === 100)).toBeTruthy();
  expect(startingDelays.some(item => item.delay === 650)).toBeFalsy();
  expect(await page.locator('#output-dir').inputValue()).toBe('');
  const box = await page.locator('#startup-activity').boundingBox();
  mock.failStartup();
  await expect(page.locator('#activity-label')).toContainText('本地任务读取失败');
  await expect(page.locator('#activity-spinner')).toBeHidden();
  await expect(page.locator('#add-button')).toBeDisabled();
  await page.locator('#activity-retry').click();
  await expect.poll(mock.initializeCalls).toBe(2);
  mock.ready();
  await expect(page.locator('#add-button')).toBeEnabled();
  await expect(page.locator('#output-dir')).toHaveValue('D:\\restored-output');
  expect(await page.evaluate(() => (window as unknown as { startupPollDelays: {delay:number;addEnabled:boolean}[] }).startupPollDelays.some(item => item.delay === 650 && item.addEnabled))).toBeTruthy();
  await expect(page.locator('#model-status')).toHaveText('正在准备离线识别');
  await expect(page.locator('#activity-label')).toContainText('准备识别模型');
  mkdirSync('output/qa', { recursive: true });
  await page.screenshot({ path: 'output/qa/startup-background-warmup.png', fullPage: true });
  mock.failWarmup();
  await expect(page.locator('#activity-retry')).toBeVisible();
  await expect(page.locator('#activity-background')).toContainText('测试预热失败');
  await expect(page.locator('#model-status')).toHaveText('离线识别准备失败');
  await page.locator('#activity-retry').click();
  await expect.poll(mock.warmupRetries).toBe(1);
  await expect(page.locator('#activity-retry')).toBeHidden();
  const after = await page.locator('#startup-activity').boundingBox();
  expect([after!.x, after!.width, after!.height]).toEqual([box!.x, box!.width, box!.height]);
  mock.queueWaiting();
  await expect(page.locator('.task-state')).toHaveText('等待识别资源');
  mock.warmupReady();
  await expect(page.locator('#model-status')).toHaveText('离线识别已就绪');
  await expect(page.locator('#activity-label')).toHaveText('识别预热已完成');
  await expect(page.locator('#activity-label')).toBeEmpty({ timeout: 5000 });
});

test('lazy review shares prefetch promise and native close does not await the editor chunk', async ({ page }) => {
  let release!: () => void, requests = 0;
  const held = new Promise<void>(resolve => { release = resolve; });
  await page.route('**/chunks/review-*.js', async route => { requests++; await held; await route.continue(); });
  const mock = await mockReview(page, 2, { open: false });
  await expect.poll(() => requests).toBe(1);
  await page.locator('#review-button').click(); await page.locator('#review-button').click();
  await expect(page.locator('#activity-label')).toContainText('准备核对编辑器');
  await page.evaluate(() => window.pdf2mdRequestClose());
  await expect.poll(mock.closeCalls).toBe(1);
  release();
  await expect(page.locator('#activity-label')).not.toContainText('准备核对编辑器');
  await page.locator('#review-button').click();
  await expect(page.locator('#review-body')).toBeVisible();
  expect(requests).toBe(1);
});

test('foreground document and latest PDF page override but retain background warmup', async ({ page }) => {
  const mock = await mockReview(page, 2, { open: false, startup: true }); mock.ready();
  let releaseDoc!: () => void, releasePage!: () => void;
  const docHeld = new Promise<void>(resolve => { releaseDoc = resolve; });
  const pageHeld = new Promise<void>(resolve => { releasePage = resolve; });
  await page.route('**/api/review_document', async route => { await docHeld; await route.fallback(); });
  await page.route('**/api/review_page', async route => {
    const pageNo = route.request().postDataJSON().args[1];
    if (pageNo === 1) await pageHeld;
    await route.fallback();
  });
  await expect(page.locator('#review-button')).toBeEnabled(); await page.locator('#review-button').click();
  await expect(page.locator('#activity-label')).toContainText('读取核对文档');
  await expect(page.locator('#activity-background')).toContainText('后台：正在准备识别模型');
  await page.screenshot({ path: 'output/qa/review-foreground-loading.png', fullPage: true });
  releaseDoc();
  await expect(page.locator('#review-body')).toBeVisible();
  await expect(page.locator('#activity-label')).toContainText('原 PDF 第 1 页');
  await page.locator('[data-warning-id="w1"]').click();
  await expect(page.locator('#review-pdf-image')).toBeVisible();
  await expect(page.locator('#review-pdf-image')).toHaveAttribute('src', /page=2$/);
  await expect(page.locator('#activity-label')).toContainText('准备识别模型');
  releasePage();
  await expect.poll(mock.pageCalls).toBe(2);
  await expect(page.locator('#review-pdf-image')).toHaveAttribute('src', /page=2$/);
  await expect(page.locator('#review-page-number')).toHaveValue('2');
});

test('failed lazy module reload preserves unsaved settings and requested review entry', async ({ page }) => {
  let requests = 0;
  await page.route('**/chunks/review-*.js', async route => { if (++requests === 1) await route.abort('failed'); else await route.continue(); });
  await mockReview(page, 2, { open: false });
  await expect(page.locator('#document-title')).toHaveText('核对示例.pdf');
  await page.locator('#output-dir').fill('D:\\unsaved-session-output');
  await page.locator('#page-range').fill('2');
  await page.locator('#conversion-mode').selectOption('ocr');
  await page.locator('#table-format').selectOption('html');
  await page.locator('#task-warning button').nth(2).click();
  await expect(page.locator('#activity-retry')).toHaveText('重新加载界面');
  await expect(page.locator('#activity-label')).toContainText('编辑器加载失败');
  await expect(page.locator('#activity-spinner')).toBeHidden();
  await page.locator('#activity-retry').click();
  await expect(page.locator('#review-body')).toBeVisible();
  await expect(page.locator('[data-warning-id="w1"]')).toHaveAttribute('aria-pressed', 'true');
  await expect(page.locator('#output-dir')).toHaveValue('D:\\unsaved-session-output');
  await expect(page.locator('#page-range')).toHaveValue('2');
  await expect(page.locator('#conversion-mode')).toHaveValue('ocr');
  await expect(page.locator('#table-format')).toHaveValue('html');
  expect(requests).toBe(2);
});

test('leaving a pending review document clears loading and ignores its late result', async ({ page }) => {
  const mock = await mockReview(page, 2, { open: false, startup: true }); mock.ready();
  let requests = 0, release!: () => void;
  const held = new Promise<void>(resolve => { release = resolve; });
  await page.route('**/api/review_document', async route => {
    if (++requests === 1) {
      await held;
      await route.fulfill({ json: { ok: true, markdown: '# 过期的核对正文', sha256: 'stale', review_revision: 'stale',
        warnings: [], blocks: [], pages: [], source: { available: false, changed: false, pages: 1 }, reviewed_count: 0, pending_count: 0 } });
    } else await route.fallback();
  });
  await expect(page.locator('#review-button')).toBeEnabled(); await page.locator('#review-button').click();
  await expect(page.locator('#activity-label')).toContainText('读取核对文档');
  await expect(page.locator('#review-back')).toBeEnabled();
  await page.locator('#review-back').click();
  await expect(page.locator('#review-workspace')).toBeHidden();
  await expect(page.locator('#activity-label')).toContainText('准备识别模型');
  await page.locator('#review-button').click();
  await expect(page.locator('#review-body')).toBeVisible();
  await expect(page.locator('#review-pdf-image')).toBeVisible();
  await expect(page.locator('#activity-label')).toContainText('准备识别模型');
  mock.failWarmup();
  await expect(page.locator('#activity-label')).toContainText('预热未完成');
  await expect(page.locator('#activity-retry')).toBeVisible();
  const lateResponse = page.waitForResponse('**/api/review_document');
  release();
  await (await lateResponse).finished();
  await page.evaluate(() => new Promise<void>(resolve => requestAnimationFrame(() => resolve())));
  await expect(page.getByRole('textbox', { name: '可编辑的 Markdown 源码' })).not.toContainText('过期的核对正文');
  await expect(page.locator('#activity-label')).toContainText('预热未完成');
});

test('PDF read errors persist in header, retry reads page and a new page clears the old failure', async ({ page }) => {
  await mockReview(page, 2, { open: false });
  let attempts = 0, failFirstPage = true;
  await page.route('**/api/review_page', async route => {
    const number = route.request().postDataJSON().args[1];
    if (number === 1) {
      attempts++;
      if (failFirstPage) { await route.fulfill({ json: { ok: false, error: '测试页面读取失败' } }); return; }
    }
    await route.fallback();
  });
  await page.locator('#review-button').click();
  await expect(page.locator('#activity-label')).toHaveText('原 PDF 页面读取失败，请重试');
  await expect(page.locator('#review-pdf-status')).toHaveText('测试页面读取失败');
  await expect(page.locator('#activity-spinner')).toBeHidden();
  await expect(page.locator('#activity-retry')).toHaveText('重试');
  failFirstPage = false;
  await page.locator('#activity-retry').click();
  await expect(page.locator('#review-pdf-image')).toBeVisible();
  expect(attempts).toBe(2);
  await expect(page.locator('#activity-retry')).toBeHidden();
  failFirstPage = true;
  await page.locator('[data-warning-id="w0"]').click();
  await expect(page.locator('#activity-label')).toContainText('页面读取失败');
  await page.locator('[data-warning-id="w1"]').click();
  await expect(page.locator('#review-pdf-image')).toHaveAttribute('src', /page=2$/);
  await expect(page.locator('#activity-retry')).toBeHidden();
  await expect(page.locator('#activity-label')).not.toContainText('失败');
});

test('review document read error header retry loads the same document without reloading the app', async ({ page }) => {
  await mockReview(page, 2, { open: false });
  let attempts = 0, navigations = 0;
  page.on('framenavigated', frame => { if (frame === page.mainFrame()) navigations++; });
  await page.route('**/api/review_document', async route => {
    if (++attempts === 1) await route.fulfill({ json: { ok: false, error: '测试文档读取失败' } });
    else await route.fallback();
  });
  await page.locator('#review-button').click();
  await expect(page.locator('#activity-label')).toHaveText('核对文档读取失败，请重试');
  await expect(page.locator('#review-error')).toContainText('测试文档读取失败');
  await page.locator('#activity-retry').click();
  await expect(page.locator('#review-body')).toBeVisible();
  await expect(page.locator('#review-pdf-image')).toBeVisible();
  await expect(page.locator('#activity-retry')).toBeHidden();
  expect(attempts).toBe(2); expect(navigations).toBe(0);
});
