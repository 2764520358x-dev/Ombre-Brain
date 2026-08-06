#!/usr/bin/env node
/**
 * send_to_man.js <message>
 *
 * Deploy to: /root/send_to_man.js on Singapore server
 *
 * Sends a message to 慢's WeChat DM via cc-connect API socket.
 * Called by 小克 during 随机脉冲 (isolated session) to proactively contact 慢.
 *
 * Usage:
 *   node /root/send_to_man.js "你好，想你了"
 */

const net = require('net');

const SOCKET = '/root/.cc-connect/run/api.sock';
const PROJECT = 'xiaoke';
const message = process.argv.slice(2).join(' ');

if (!message) {
  console.error('Usage: send_to_man.js <message>');
  process.exit(1);
}

function socketRequest(method, path, body) {
  return new Promise((resolve, reject) => {
    const sock = net.createConnection(SOCKET, () => {
      const bodyStr = body ? JSON.stringify(body) : '';
      const req = [
        `${method} ${path} HTTP/1.0`,
        'Host: localhost',
        'Content-Type: application/json',
        `Content-Length: ${Buffer.byteLength(bodyStr)}`,
        '',
        bodyStr,
      ].join('\r\n');
      sock.write(req);
    });
    let raw = '';
    sock.on('data', d => { raw += d; });
    sock.on('end', () => {
      const split = raw.indexOf('\r\n\r\n');
      resolve((split >= 0 ? raw.slice(split + 4) : raw).trim());
    });
    sock.on('error', reject);
    setTimeout(() => { sock.destroy(); reject(new Error('socket timeout')); }, 5000);
  });
}

async function findWeixinDM() {
  const raw = await socketRequest('GET', '/sessions', null);
  let sessions;
  try { sessions = JSON.parse(raw); } catch { throw new Error('sessions parse error: ' + raw.slice(0, 100)); }

  for (const [key, s] of Object.entries(sessions || {})) {
    if (key.startsWith('weixin:dm:') && (!s.project || s.project === PROJECT)) return key;
  }
  for (const key of Object.keys(sessions || {})) {
    if (key.startsWith('weixin:dm:')) return key;
  }
  throw new Error('No active WeChat DM session. Sessions: ' + JSON.stringify(sessions).slice(0, 200));
}

async function main() {
  const sessionKey = await findWeixinDM();
  const result = await socketRequest('POST', '/send', {
    project: PROJECT,
    session_key: sessionKey,
    message,
  });
  const ts = new Date().toISOString().slice(11, 19);
  console.log(`[${ts}] sent to ${sessionKey.slice(0, 50)}... result: ${result.slice(0, 80)}`);
}

main().catch(err => {
  console.error('send_to_man error:', err.message);
  process.exit(1);
});
