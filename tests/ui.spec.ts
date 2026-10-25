import { test, expect, Page } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';
import { readdirSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import path from 'node:path';

const root = path.resolve('.');
const sample = path.join(root, readdirSync(root).find(name => name.startsWith('Reddy') && name.endsWith('.pdf'))!);
async function api(page: Page, method: string, ...args: unknown[]) {
  return page.evaluate(async ({method,args}) => {
    const response = await fetch(`/api/${method}`, {method:'POST',headers:{'Content-Type':'application/json',
      'X-PDF2MD-Token':(window as unknown as {__PDF2MD_TOKEN__:string}).__PDF2MD_TOKEN__},body:JSON.stringify({args})});
    return response.json();
  }, {method,args});
}

test.beforeEach(async ({page}) => {
  await page.goto('/');
  await expect(page.locator('#add-button')).toBeEnabled({timeout:30_000});
  await api(page, 'remove');
  await expect(page.locator('#queue-count')).toHaveText('0');
});

test('empty workspace accessibility, native selects and narrow layout', async ({page}) => {
  await expect(page.getByRole('button', {name:'开始转换'})).toBeDisabled();
  await expect(page.getByText('从一份 PDF 开始')).toBeVisible();
  await page.getByLabel('识别模式').focus();
  await page.keyboard.press('ArrowDown'); await page.keyboard.press('Enter');
  await expect(page.getByLabel('识别模式')).toHaveValue('ocr');
  await page.getByLabel('识别模式').selectOption('auto');
  const results = await new AxeBuilder({page}).withTags(['wcag2a','wcag2aa','wcag21aa']).analyze();
  expect(results.violations).toEqual([]);
  mkdirSync('output/qa', {recursive:true});
  await page.screenshot({path:'output/qa/gui-empty.png',fullPage:true});
  await page.setViewportSize({width:760,height:680});
  await page.locator('#start-button').scrollIntoViewIfNeeded();
  await expect(page.locator('#start-button')).toBeInViewport();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy();
  await page.screenshot({path:'output/qa/gui-narrow.png',fullPage:true});
});

test('import validation, failed state, keyboard retry and clear', async ({page}) => {
  const reply = await api(page,'import_files',[sample,sample,path.join(root,'absent.pdf')]);
  expect([reply.added,reply.duplicates,reply.errors.length]).toEqual([1,1,1]);
  await expect(page.locator('#task-list li')).toHaveCount(1);
  await page.getByLabel('输出目录',{exact:true}).fill(path.join(root,'output','ui-tests'));
  await page.getByLabel('页码范围').fill('0');
  await page.getByRole('button',{name:'开始转换'}).click();
  await expect(page.locator('.task-state')).toHaveText('转换失败');
  await expect(page.locator('#task-detail')).toContainText('页码超出范围');
  await page.getByRole('button',{name:'重试',exact:true}).focus();
  await page.keyboard.press('Enter');
  await expect(page.locator('.task-state')).toHaveText('待开始');
  await page.getByRole('button',{name:'清空',exact:true}).click();
  await expect(page.locator('#queue-count')).toHaveText('0');
});

test('exit modal keyboard focus and Escape restoration', async ({page}) => {
  await page.route('**/api/snapshot', async route => {
    const reply = await route.fetch();
    await route.fulfill({ json: { ...await reply.json(), revision: 999999, busy: true } });
  });
  await expect(page.locator('#cancel-current')).toBeVisible();
  const trigger = page.getByRole('button',{name:'任务报告',exact:true});
  await trigger.focus();
  await page.evaluate(() => (window as unknown as {pdf2mdRequestClose:()=>void}).pdf2mdRequestClose());
  await expect(page.getByRole('dialog')).toBeVisible();
  await expect(page.getByRole('button',{name:'继续转换',exact:true})).toBeFocused();
  await page.keyboard.press('Tab');
  await expect(page.getByRole('button',{name:'取消并退出',exact:true})).toBeFocused();
  await page.keyboard.press('Tab');
  await expect(page.getByRole('button',{name:'继续转换',exact:true})).toBeFocused();
  await page.keyboard.press('Escape');
  await expect(page.getByRole('dialog')).toBeHidden();
  await expect(trigger).toBeFocused();
});

test('real OCR conversion, formula preview, copy and success accessibility', async ({page,context}) => {
  test.setTimeout(300_000);
  await api(page,'import_files',[path.join(root,'tests','fixtures','scanned-math.pdf')]);
  await expect(page.locator('#task-list li')).toHaveCount(1);
  await page.getByLabel('输出目录',{exact:true}).fill(path.join(root,'output','ui-tests'));
  await page.getByLabel('页码范围').fill('1');
  await page.getByRole('button',{name:'开始转换'}).click();
  await expect(page.locator('#cancel-current')).toBeVisible();
  await expect(page.locator('#add-button')).toBeDisabled();
  await expect(page.locator('.task-state')).toHaveText('转换成功', {timeout:240_000});
  const frame = page.frameLocator('#rendered-preview');
  await expect(frame.getByRole('heading',{name:'第二十六章 反比例函数'})).toBeVisible();
  await expect(frame.locator('.katex')).toHaveCount(1);
  await expect(frame.locator('img')).toHaveJSProperty('complete',true);
  await page.getByRole('button',{name:'源码',exact:true}).click();
  expect(await page.locator('#source-preview').inputValue()).toContain('\\frac{s}{t}');
  await context.grantPermissions(['clipboard-read','clipboard-write']);
  await page.getByRole('button',{name:'复制 Markdown'}).click();
  await expect(page.locator('#notifications')).toContainText('已复制');
  expect(await page.evaluate(() => navigator.clipboard.readText())).toContain('反比例函数');
  await page.getByRole('button',{name:'渲染',exact:true}).click();
  await expect(page.locator('#source-preview')).toBeHidden();
  // The preview deliberately forbids scripts. Audit the app shell without trying to inject
  // axe into that sandbox; rendered text, formulas and image loading are checked above.
  expect((await new AxeBuilder({page}).exclude('#rendered-preview').options({iframes:false}).withTags(['wcag2a','wcag2aa','wcag21aa']).analyze()).violations).toEqual([]);
  await page.screenshot({path:'output/qa/gui-converted.png',fullPage:true});
  // Add a review prompt to this fresh test result without another model execution.
  // The source/range values come from the real converter, then every review API is real.
  const snapshot = await api(page, 'snapshot'), task = snapshot.tasks[0];
  const contentPath = path.join(task.output_dir, 'content_list.json');
  const content = JSON.parse(readFileSync(contentPath, 'utf-8'));
  const block = content.blocks.find((item: {markdown_ranges?: unknown[];source_regions?: unknown[];type:string}) =>
    item.type !== 'image' && item.markdown_ranges?.length && item.source_regions?.length);
  expect(block).toBeTruthy();
  content.warnings = [{ id:'ui-proof-check',code:'low_ocr_confidence',page:block.page,message:'请对照原文核对本段。',
    block_ids:[block.id],markdown_ranges:block.markdown_ranges,source_regions:block.source_regions }];
  writeFileSync(contentPath, JSON.stringify(content), 'utf-8');
  await page.getByRole('button',{name:'对照核对',exact:true}).click();
  await expect(page.locator('#review-body')).toBeVisible();
  await expect(page.locator('#review-pdf-image')).toBeVisible();
  await expect(page.locator('#review-pdf-overlay polygon')).not.toHaveCount(0);
  await expect(page.locator('.review-source-highlight')).not.toHaveCount(0);
  await expect(page.frameLocator('#review-rendered').locator('.review-preview-highlight')).not.toHaveCount(0);
  const editor = page.getByRole('textbox',{name:'可编辑的 Markdown 源码'});
  await editor.focus(); await page.keyboard.press('End'); await page.keyboard.insertText('（已对照原文）');
  await page.locator('#review-mark').click();
  await expect(page.locator('#review-progress')).toHaveText('已核对 1 / 1 项');
  const saved = await api(page,'review_document',task.id);
  expect(saved.markdown).toContain('（已对照原文）'); expect(saved.pending_count).toBe(0);
  await page.screenshot({path:'output/qa/review-real-conversion.png',fullPage:true});
  await page.locator('#review-back').click();
  await expect(page.locator('.task-state')).toHaveText('完成 · 已核对');
});
