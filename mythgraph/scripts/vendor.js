// Build step: copy d3 and the self-hosted fonts into public/vendor.
import { mkdirSync, copyFileSync } from 'node:fs';

const out = new URL('../public/vendor/', import.meta.url);
mkdirSync(new URL('fonts/', out), { recursive: true });
const nm = new URL('../node_modules/', import.meta.url);
copyFileSync(new URL('d3/dist/d3.min.js', nm), new URL('d3.min.js', out));
const fonts = [
  ...['400-normal', '500-normal', '600-normal', '400-italic', '500-italic'].map((v) => `@fontsource/cormorant-garamond/files/cormorant-garamond-latin-${v}.woff2`),
  ...['400', '500', '600'].map((w) => `@fontsource/inter/files/inter-latin-${w}-normal.woff2`),
];
for (const f of fonts) copyFileSync(new URL(f, nm), new URL(`fonts/${f.split('/').pop()}`, out));
console.log(`vendored d3 + ${fonts.length} font files`);
