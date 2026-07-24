#!/usr/bin/env node
/**
 * Minecraft → 小克 relay server
 *
 * Run on Singapore: node mc-relay.js
 * Listens on port 8932. Receives game chat from Shanghai bot,
 * injects it into 小克's WeChat session via cc-connect API socket.
 */
const http = require('http');
const net = require('net');

const SOCKET = '/root/.cc-connect/run/api.sock';
const PORT = 8932;
const PROJECT = 'xiaoke';

// ─── cc-connect socket helper ─────────────────────────────────────────────────
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
      const responseBody = split >= 0 ? raw.slice(split + 4) : raw;
      resolve(responseBody.trim());
    });
    sock.on('error', reject);
    setTimeout(() => { sock.destroy(); reject(new Error('socket timeout')); }, 5000);
  });
}

// ─── Find active weixin DM session key ────────────────────────────────────────
async function getWeixinSessionKey() {
  const raw = await socketRequest('GET', '/sessions', null);
  let sessions;
  try { sessions = JSON.parse(raw); } catch { throw new Error('sessions parse error: ' + raw.slice(0, 100)); }

  // sessions is typically an object keyed by session_key
  if (sessions && typeof sessions === 'object') {
    for (const [key, s] of Object.entries(sessions)) {
      if (key.includes('weixin:dm') || key.includes('weixin:')) {
        // If the session belongs to our project (or no project field)
        if (!s.project || s.project === PROJECT) return key;
      }
    }
    // fallback: any weixin entry
    for (const key of Object.keys(sessions)) {
      if (key.startsWith('weixin:')) return key;
    }
  }
  throw new Error('No weixin session found. Sessions: ' + JSON.stringify(sessions).slice(0, 200));
}

// ─── Send message to 小克 via cc-connect ─────────────────────────────────────
async function notifyXiaoke(username, message) {
  const sessionKey = await getWeixinSessionKey();
  const content = `[游戏消息] <${username}>: ${message}`;
  const result = await socketRequest('POST', '/send', {
    project: PROJECT,
    session_key: sessionKey,
    message: content,
  });
  return { sessionKey, result };
}

// ─── HTTP server ──────────────────────────────────────────────────────────────
const server = http.createServer((req, res) => {
  if (req.method === 'GET' && req.url === '/health') {
    res.writeHead(200);
    res.end('OK');
    return;
  }

  if (req.method !== 'POST' || req.url !== '/chat') {
    res.writeHead(404);
    res.end('Not found');
    return;
  }

  let body = '';
  req.on('data', chunk => { body += chunk; });
  req.on('end', async () => {
    let username, message;
    try {
      const parsed = JSON.parse(body);
      username = parsed.username;
      message = parsed.message;
      if (!username || !message) throw new Error('missing username or message');
    } catch (err) {
      res.writeHead(400);
      res.end('Bad request: ' + err.message);
      return;
    }

    const ts = new Date().toISOString().slice(11, 19);
    console.log(`[${ts}] game chat: <${username}> ${message}`);

    try {
      const { sessionKey, result } = await notifyXiaoke(username, message);
      console.log(`[${ts}] → sent to session ${sessionKey.slice(0, 40)}... result: ${result.slice(0, 80)}`);
      res.writeHead(200);
      res.end('OK');
    } catch (err) {
      console.error(`[${ts}] relay error:`, err.message);
      res.writeHead(500);
      res.end(err.message);
    }
  });
});

server.listen(PORT, '0.0.0.0', () => {
  console.log(`mc-relay listening on port ${PORT}`);
  console.log(`cc-connect socket: ${SOCKET}`);
  console.log(`project: ${PROJECT}`);
});

server.on('error', err => {
  console.error('Server error:', err);
  process.exit(1);
});
