import { readFileSync } from 'node:fs';
import pg from 'pg';
import { databaseUrl } from '../lib/env.js';

const client = new pg.Client({ connectionString: databaseUrl(), ssl: { rejectUnauthorized: true } });
await client.connect();
await client.query(readFileSync(new URL('./schema.sql', import.meta.url), 'utf8'));
await client.end();
console.log('schema applied');
