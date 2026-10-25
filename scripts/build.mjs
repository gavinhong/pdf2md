import { build } from 'esbuild';
import { mkdir, readFile, readdir, unlink, writeFile } from 'node:fs/promises';
import path from 'node:path';

const target = 'src/pdf2md/web';
await mkdir(target, { recursive: true });
const chunks = path.resolve(target, 'chunks');
await mkdir(chunks, { recursive: true });
for (const entry of await readdir(chunks, { withFileTypes: true })) {
  if (entry.isFile() && /\.(?:js|css)(?:\.map)?$/.test(entry.name)) await unlink(path.join(chunks, entry.name));
}
const result = await build({
  entryPoints: ['frontend/app.ts'],
  bundle: true,
  minify: true,
  target: ['es2020'],
  outdir: target,
  entryNames: '[name]',
  chunkNames: 'chunks/[name]-[hash]',
  format: 'esm',
  splitting: true,
  metafile: true,
  loader: { '.woff2': 'file', '.woff': 'file', '.ttf': 'file' },
  assetNames: 'fonts/[name]-[hash]',
});
await mkdir('output/qa', { recursive: true });
await writeFile('output/qa/ui-build-metafile.json', JSON.stringify(result.metafile, null, 2));
const template = await readFile('frontend/index.html', 'utf8');
const stylesheet = await readFile(`${target}/app.css`, 'utf8');
// The app document paints without a blocking stylesheet fetch. Keep app.css for
// sandboxed Markdown documents, which still use their explicit local CSS link.
const html = template.replace('<link rel="stylesheet" href="/app.css">', `<style>${stylesheet.replace(/<\/style/gi, '<\\/style')}</style>`);
await writeFile(`${target}/index.html`, html);
console.log('Built offline desktop UI.');
