#!/usr/bin/env node
/**
 * 小克 Telegram Bot
 * No npm dependencies — uses built-in https + child_process.
 *
 * Start:
 *   TG_TOKEN=xxx TG_USER_ID=8972876200 node tg-bot.js
 *
 * Or via pm2 ecosystem (see tg-pm2.config.js).
 */
const https = require('https');
const { spawnSync } = require('child_process');
const fs = require('fs');
const path = require('path');

const TOKEN     = process.env.TG_TOKEN;
const ALLOWED   = process.env.TG_USER_ID ? parseInt(process.env.TG_USER_ID) : null;
const WORK_DIR  = process.env.TG_WORK_DIR || path.join(process.env.HOME || '/home/ubuntu', 'xiaoke-tg');
const HIST_FILE = path.join(WORK_DIR, 'history.json');
const MAX_PAIRS = 8;

if (!TOKEN) { console.error('TG_TOKEN is required'); process.exit(1); }

// ── Telegram API ──────────────────────────────────────────────────────────────
function tgApi(method, body) {
  return new Promise((resolve, reject) => {
    const data = JSON.stringify(body);
    const req = https.request({
      hostname: 'api.telegram.org',
      path: `/bot${TOKEN}/${method}`,
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(data) },
    }, res => {
      let raw = '';
      res.on('data', d => raw += d);
      res.on('end', () => { try { resolve(JSON.parse(raw)); } catch { resolve({}); } });
    });
    req.on('error', reject);
    req.write(data);
    req.end();
  });
}

// ── Conversation history ──────────────────────────────────────────────────────
function loadHistory() {
  try { return JSON.parse(fs.readFileSync(HIST_FILE, 'utf8')); }
  catch { return []; }
}
function saveHistory(h) {
  fs.writeFileSync(HIST_FILE, JSON.stringify(h.slice(-(MAX_PAIRS * 2))));
}

// ── Strip English-heavy lines (prevents leaked reasoning from showing up) ─────
function filterOutput(text) {
  return text.split('\n').filter(line => {
    const en = (line.match(/[a-zA-Z]/g) || []).length;
    const total = line.replace(/\s/g, '').length;
    if (total < 4) return true;
    return en / total < 0.4;
  }).join('\n').trim();
}

// ── Call Claude Code CLI ──────────────────────────────────────────────────────
function callClaude(text, history) {
  const pairs = history.slice(-(MAX_PAIRS * 2));
  let prompt;
  if (pairs.length === 0) {
    prompt = text;
  } else {
    const ctx = pairs.map(m => `${m.role === 'user' ? '慢' : '小克'}: ${m.content}`).join('\n');
    prompt = `历史对话：\n${ctx}\n\n慢说：${text}`;
  }

  const r = spawnSync('claude', ['-p', prompt], {
    timeout: 90000,
    maxBuffer: 2 * 1024 * 1024,
    cwd: WORK_DIR,
    env: { ...process.env },
    encoding: 'utf8',
  });

  if (r.error) throw r.error;
  if (r.status !== 0) throw new Error(r.stderr?.slice(0, 300) || `claude exit ${r.status}`);
  return r.stdout.trim();
}

// ── Polling loop ──────────────────────────────────────────────────────────────
let offset = 0;
let busy   = false;

async function poll() {
  try {
    const res = await tgApi('getUpdates', { offset, timeout: 30, allowed_updates: ['message'] });
    if (res.ok && res.result) {
      for (const upd of res.result) {
        offset = upd.update_id + 1;
        const msg = upd.message;
        if (!msg?.text) continue;
        if (ALLOWED && msg.from.id !== ALLOWED) continue;
        if (msg.text.startsWith('/')) continue;

        const chatId = msg.chat.id;
        const ts = new Date().toISOString().slice(11, 16);

        if (busy) {
          await tgApi('sendMessage', { chat_id: chatId, text: '等一下，还在想…' });
          continue;
        }

        busy = true;
        console.log(`[${ts}] <${msg.from.first_name}> ${msg.text}`);
        await tgApi('sendChatAction', { chat_id: chatId, action: 'typing' });

        const history = loadHistory();
        try {
          let response = callClaude(msg.text, history);
          response = filterOutput(response);
          if (!response) response = '嗯';

          history.push({ role: 'user', content: msg.text });
          history.push({ role: 'assistant', content: response });
          saveHistory(history);

          console.log(`[${ts}] → ${response.slice(0, 80)}`);
          await tgApi('sendMessage', { chat_id: chatId, text: response });
        } catch (err) {
          console.error(`[${ts}] err:`, err.message);
          await tgApi('sendMessage', { chat_id: chatId, text: '出了点问题，再说一次？' });
        } finally {
          busy = false;
        }
      }
    }
  } catch (err) {
    if (err.code !== 'ECONNRESET' && err.code !== 'ETIMEDOUT')
      console.error('poll err:', err.message);
  }
  setTimeout(poll, 1000);
}

// ── Start ─────────────────────────────────────────────────────────────────────
fs.mkdirSync(WORK_DIR, { recursive: true });
console.log(`小克 Telegram bot | work dir: ${WORK_DIR}`);
if (ALLOWED) console.log(`Only accepting messages from user ID: ${ALLOWED}`);
poll();
