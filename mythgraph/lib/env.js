// Loads .env.local / .env into process.env without echoing anything.
// Values are never logged; callers only learn whether a key is present.
import { readFileSync, existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');

export function loadEnv() {
  for (const dir of [root, join(root, '..')]) {
    for (const name of ['.env.local', '.env']) {
      const p = join(dir, name);
      if (!existsSync(p)) continue;
      for (const line of readFileSync(p, 'utf8').split('\n')) {
        const m = line.match(/^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$/);
        if (!m || process.env[m[1]] !== undefined) continue;
        process.env[m[1]] = m[2].replace(/^(['"])(.*)\1$/, '$2');
      }
    }
  }
}

export function databaseUrl() {
  loadEnv();
  const url = process.env.DATABASE_URL || process.env.POSTGRES_URL || process.env.NEON_DATABASE_URL;
  if (!url) {
    throw new Error('No DATABASE_URL (or POSTGRES_URL / NEON_DATABASE_URL) in env or .env.local');
  }
  return url;
}
