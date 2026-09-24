import { neon } from '@neondatabase/serverless';
import { loadGraph } from '../lib/graph.js';

export default async function handler(req, res) {
  const url = process.env.DATABASE_URL || process.env.POSTGRES_URL || process.env.NEON_DATABASE_URL;
  if (!url) {
    res.status(500).json({ error: 'Database is not configured' });
    return;
  }
  try {
    const graph = await loadGraph(neon(url));
    res.setHeader('Cache-Control', 'public, s-maxage=3600, stale-while-revalidate=86400');
    res.status(200).json(graph);
  } catch (err) {
    console.error('graph query failed', err.message);
    res.status(500).json({ error: 'Graph query failed' });
  }
}
