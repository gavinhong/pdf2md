import MarkdownIt from 'markdown-it';
import texmath from 'markdown-it-texmath';
import katex from 'katex';
import renderMathInElement from 'katex/contrib/auto-render';
import DOMPurify from 'dompurify';

const md = new MarkdownIt({ html: true, linkify: false, typographer: false }).use(texmath, {
  engine: katex, delimiters: 'dollars', katexOptions: { trust: false, throwOnError: false, maxExpand: 200 },
});
const attrs = ['target', 'loading', 'data-source-start', 'data-source-end'];
const forbidden = ['style', 'script', 'iframe', 'object', 'embed'];

/** One renderer owns the same resource and HTML policy in reading and review modes. */
export function markdownDocument(text: string, taskId: string, sourceRanges = false) {
  const tokens = md.parse(text, {});
  if (sourceRanges) {
    const starts = [0];
    for (let i = 0; i < text.length; i++) if (text[i] === '\n') starts.push(i + 1);
    starts.push(text.length);
    for (const token of tokens) {
      if (!token.map || !token.block || token.nesting < 0) continue;
      token.attrSet('data-source-start', String(starts[token.map[0]] ?? text.length));
      token.attrSet('data-source-end', String(starts[token.map[1]] ?? text.length));
      // Raw HTML tables and math blocks have custom renderers that ignore token attributes.
      if (token.nesting === 0 && md.renderer.rules[token.type]) {
        const original = md.renderer.rules[token.type]!;
        const rendered = original([token], 0, md.options, {}, md.renderer);
        token.type = 'html_block';
        token.content = `<div data-source-start="${token.attrGet('data-source-start')}" data-source-end="${token.attrGet('data-source-end')}">${rendered}</div>`;
      }
    }
  }
  const fragment = document.createElement('div');
  fragment.innerHTML = DOMPurify.sanitize(md.renderer.render(tokens, md.options, {}), { ADD_ATTR: attrs, FORBID_TAGS: forbidden });
  for (const table of fragment.querySelectorAll('table')) renderMathInElement(table, {
    delimiters: [{ left: '$$', right: '$$', display: true }, { left: '$', right: '$', display: false }],
    trust: false, throwOnError: false, maxExpand: 200,
  });
  const resourceBase = `${location.origin}/resource/${encodeURIComponent(window.__PDF2MD_TOKEN__)}/${encodeURIComponent(taskId)}/`;
  for (const image of fragment.querySelectorAll('img')) {
    const source = image.getAttribute('src') || '';
    if (!source.startsWith('images/') || source.includes('..')) image.remove();
    else { image.src = resourceBase + source.split('/').map(encodeURIComponent).join('/'); image.loading = 'lazy'; }
  }
  for (const link of fragment.querySelectorAll('a')) {
    link.setAttribute('target', '_blank'); link.setAttribute('rel', 'noopener noreferrer');
    const href = link.getAttribute('href') || '';
    if (!/^https?:\/\//i.test(href) && !href.startsWith('#')) link.removeAttribute('href');
  }
  const safe = DOMPurify.sanitize(fragment.innerHTML, { ADD_ATTR: attrs, FORBID_TAGS: forbidden });
  return `<!doctype html><html lang="zh-CN"><head><meta charset="UTF-8"><meta name="referrer" content="no-referrer"><link rel="stylesheet" href="${location.origin}/app.css"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline' ${location.origin}; font-src ${location.origin}; img-src ${location.origin}; script-src 'none'"></head><body class="document-preview">${safe}</body></html>`;
}

export function externalPreviewLinks(frame: HTMLIFrameElement, open: (url: string) => void) {
  frame.contentDocument?.addEventListener('click', event => {
    const href = (event.target as Element).closest('a[href]')?.getAttribute('href') || '';
    if (/^https?:\/\//i.test(href)) { event.preventDefault(); open(href); }
  });
}
