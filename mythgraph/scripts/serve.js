// Local dev server: static files from public/, /api/graph from either a JSON
// file (GRAPH_JSON=path) or the database in .env.local.
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { extname, join, normalize } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = fileURLToPath(new URL('../public/', import.meta.url));
const port = Number(process.env.PORT || 4173);
const types = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript', '.css': 'text/css', '.json': 'application/json', '.woff2': 'font/woff2', '.svg': 'image/svg+xml', '.png': 'image/png' };

let cached;
async function graph() {
  if (cached) return cached;
  if (process.env.GRAPH_JSON) return (cached = await readFile(process.env.GRAPH_JSON, 'utf8'));
  const [{ neon }, { loadGraph }, { databaseUrl }] = await Promise.all([
    import('@neondatabase/serverless'), import('../lib/graph.js'), import('../lib/env.js'),
  ]);
  return (cached = JSON.stringify(await loadGraph(neon(databaseUrl()))));
}

createServer(async (req, res) => {
  const path = decodeURIComponent(new URL(req.url, 'http://x').pathname);
  try {
    if (path === '/api/graph') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      return res.end(await graph());
    }
    const file = normalize(join(root, path.endsWith('/') ? `${path}index.html` : path));
    if (!file.startsWith(root)) throw Object.assign(new Error('forbidden'), { code: 'ENOENT' });
    const body = await readFile(file);
    res.writeHead(200, { 'Content-Type': types[extname(file)] || 'application/octet-stream' });
    res.end(body);
  } catch (err) {
    res.writeHead(err.code === 'ENOENT' ? 404 : 500);
    res.end(err.code === 'ENOENT' ? 'not found' : 'server error');
    if (err.code !== 'ENOENT') console.error(err.message);
  }
}).listen(port, () => console.log(`http://localhost:${port}`));
