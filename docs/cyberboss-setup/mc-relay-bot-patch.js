#!/usr/bin/env node
/**
 * Patches /root/minecraft-bot-local/server.js to forward in-game chat to mc-relay on Singapore.
 * When ANN275 types in game, an HTTP POST is fired to Singapore:8932 which injects the message
 * into 小克's WeChat/cc-connect session, waking her up to respond in-game.
 *
 * Run on Shanghai AFTER mc-chat-patch.js: node mc-relay-bot-patch.js
 * Singapore IP: 43.156.80.36  Port: 8932
 */
const fs = require('fs');
const FILE = '/root/minecraft-bot-local/server.js';
const RELAY_HOST = '43.156.80.36';
const RELAY_PORT = 8932;
const BOT_USERNAME = 'xiaoke';

let code = fs.readFileSync(FILE, 'utf8');
const original = code;

// ─── Add relay HTTP call after playerChatHistory.shift() ─────────────────────
// mc-chat-patch.js must have already inserted the playerChatHistory lines.
const anchor = 'if (playerChatHistory.length > 100) playerChatHistory.shift();';
const relayBlock = `
    if (username !== '${BOT_USERNAME}') {
      try {
        const _http = require('http');
        const _rd = JSON.stringify({ username, message });
        const _rq = _http.request({
          hostname: '${RELAY_HOST}', port: ${RELAY_PORT},
          path: '/chat', method: 'POST',
          headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(_rd) },
        });
        _rq.on('error', () => {});
        _rq.end(_rd);
      } catch (_e) {}
    }`;

if (!code.includes('hostname: \'' + RELAY_HOST + '\'')) {
  if (!code.includes(anchor)) {
    console.error('✗ Could not find anchor "' + anchor + '"');
    console.error('  Did you run mc-chat-patch.js first?');
    process.exit(1);
  }
  code = code.replace(anchor, anchor + relayBlock);
  console.log('✓ Added relay HTTP call after playerChatHistory.shift()');
} else {
  console.log('~ Relay call already exists, skipped');
}

// ─── Write & verify ───────────────────────────────────────────────────────────
if (code === original) {
  console.log('\n~ No changes made');
  process.exit(0);
}

fs.writeFileSync(FILE + '.relay.bak', original);
console.log('~ Backup saved to server.js.relay.bak');

fs.writeFileSync(FILE, code);
console.log('✓ server.js patched\n');

console.log('Verification:');
const final = fs.readFileSync(FILE, 'utf8');
console.log('  relay call :', final.includes('hostname: \'' + RELAY_HOST + '\'') ? 'OK' : 'MISSING');
console.log('  bot filter :', final.includes("username !== '" + BOT_USERNAME + "'") ? 'OK' : 'MISSING');
