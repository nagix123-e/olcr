#!/usr/bin/env node
'use strict';
// OLCR-owned offline stdio MCP. It only reads its adjacent immutable corpus.
const fs = require('fs');
const path = require('path');
const corpus = JSON.parse(fs.readFileSync(path.join(__dirname, 'animejs-v4-reviewed.json'), 'utf8'));
const tools = [
  ['search_animejs_docs', 'Search reviewed Anime.js v4 reference records.', { query: { type: 'string' }, category: { type: 'string' } }],
  ['get_animejs_api', 'Get a reviewed Anime.js v4 API record.', { api: { type: 'string' } }],
  ['get_animejs_example', 'Get a reviewed v4 animation pattern example.', { pattern: { type: 'string' } }],
  ['get_animejs_pattern', 'Get recommended Anime.js v4 implementation guidance.', { intent: { type: 'string' } }],
];
function reply(id, result) { process.stdout.write(JSON.stringify({ jsonrpc: '2.0', id, result }) + '\n'); }
function content(value) { return { content: [{ type: 'text', text: JSON.stringify(value) }] }; }
function records(query, category) { const q = String(query || '').toLowerCase(); return corpus.records.filter(r => (!category || r.category === category) && JSON.stringify(r).toLowerCase().includes(q)).slice(0, 5); }
const readline = require('readline').createInterface({ input: process.stdin });
readline.on('line', line => { let msg; try { msg = JSON.parse(line); } catch { return; } if (!msg.id) return;
  if (msg.method === 'initialize') return reply(msg.id, { protocolVersion: '2024-11-05', serverInfo: { name: 'olcr-animejs-reference', version: '1.0.0' }, capabilities: { tools: {} } });
  if (msg.method === 'tools/list') return reply(msg.id, { tools: tools.map(([name, description, properties]) => ({ name, description, inputSchema: { type: 'object', properties, required: [Object.keys(properties)[0]] } })) });
  if (msg.method !== 'tools/call') return reply(msg.id, {});
  const a = msg.params?.arguments || {}, n = msg.params?.name; let result;
  if (n === 'search_animejs_docs') result = records(a.query, a.category);
  else if (n === 'get_animejs_api') result = corpus.records.filter(r => r.api.toLowerCase() === String(a.api || '').toLowerCase()).slice(0, 1);
  else if (n === 'get_animejs_example' || n === 'get_animejs_pattern') { const key = String(a.pattern || a.intent || '').toLowerCase(); result = { intent: key, animejs_version: corpus.animejs_version, source: corpus.source, guidance: corpus.patterns[key] || corpus.patterns.react }; }
  else return reply(msg.id, { isError: true, content: [{ type: 'text', text: 'REFERENCE_NOT_FOUND' }] });
  return reply(msg.id, content({ animejs_version: corpus.animejs_version, records: result }));
});
readline.on('close', () => process.exit(0));
