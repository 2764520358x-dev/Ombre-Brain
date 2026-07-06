/**
 * Ombre Chat Backend
 * Claude Code -p + stream-json 常驻子进程 + Express SSE 桥
 * 参考: Claude Code -p + stream-json 接入自建聊天前端（机教版）
 */
import { spawn } from 'child_process';
import express from 'express';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';
import webpush from 'web-push';
import cron from 'node-cron';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

const PORT        = parseInt(process.env.PORT        || '3000', 10);
const CHAT_SECRET = process.env.CHAT_SECRET          || '';
const MODEL       = process.env.MODEL                || 'opus';
const MCP_CONFIG  = path.join(__dirname, '.mcp.json');
const PERSONA     = path.join(__dirname, 'persona.md');

const app = express();
app.use(express.json({ limit: '10mb' }));
app.use(express.static(path.join(__dirname, 'public')));

// chatId -> ChildProcess（一会话一进程，上下文不串）
const procs = new Map();

// ─── Web Push ─────────────────────────────────────────────────────────────────
const VAPID_FILE = path.join(__dirname, '.vapid.json');
let vapidKeys;
if (fs.existsSync(VAPID_FILE)) {
  vapidKeys = JSON.parse(fs.readFileSync(VAPID_FILE, 'utf8'));
} else {
  vapidKeys = webpush.generateVAPIDKeys();
  fs.writeFileSync(VAPID_FILE, JSON.stringify(vapidKeys));
  console.log('[push] Generated new VAPID keys');
}
webpush.setVapidDetails('mailto:2764520358x@gmail.com', vapidKeys.publicKey, vapidKeys.privateKey);

const pushSubs = new Map(); // chatId -> PushSubscription

// ─── spawn ────────────────────────────────────────────────────────────────────
function spawnCC(chatId) {
  const args = [
    '--print',
    '--input-format',  'stream-json',   // 常驻管道：stdin 不关进程不退
    '--output-format', 'stream-json',   // NDJSON 事件流
    '--verbose',                        // 必须：不带则只有 result 事件
    '--include-partial-messages',       // token 级 delta（打字机 + thinking）
    '--model', MODEL,
    '--permission-mode', 'dontAsk',      // 非交互模式：未授权的工具调用直接拒绝不挂起
    '--allowedTools', 'mcp__ombre-brain__breath,mcp__ombre-brain__hold,mcp__ombre-brain__grow,mcp__ombre-brain__trace,mcp__ombre-brain__search,mcp__ombre-brain__dream,mcp__ombre-brain__watch_health',
    '--thinking-display', 'summarized', // 隐藏 flag，4.7+ 默认 omitted 需手动开
  ];

  if (fs.existsSync(MCP_CONFIG))  args.push('--mcp-config', MCP_CONFIG, '--strict-mcp-config');
  if (fs.existsSync(PERSONA))     args.push('--system-prompt-file', PERSONA);

  // ANTHROPIC_API_KEY 存在时会无条件压过订阅登录（官方说明），必须删掉
  const env = { ...process.env };
  delete env.ANTHROPIC_API_KEY;

  const proc = spawn('claude', args, { cwd: '/tmp', env, stdio: ['pipe', 'pipe', 'pipe'] });

  proc._buf       = '';
  proc._listeners = new Set();

  // 按行解析 + 尾巴 buffer（chunk 边界可能切在 JSON 中间）
  proc.stdout.on('data', chunk => {
    proc._buf += chunk.toString();
    const lines = proc._buf.split('\n');
    proc._buf = lines.pop();
    for (const line of lines) {
      if (!line.trim()) continue;
      try {
        const ev = JSON.parse(line);
        proc._listeners.forEach(fn => fn(ev));
      } catch { /* 忽略解析错误 */ }
    }
  });

  proc.stderr.on('data', c => process.stderr.write(`[cc:${chatId}] ${c}`));
  proc.on('close', code => {
    console.log(`[cc] session ${chatId} exited code=${code}`);
    procs.delete(chatId);
  });

  procs.set(chatId, proc);
  console.log(`[cc] spawned session=${chatId} model=${MODEL}`);
  return proc;
}

// ─── 发消息 ───────────────────────────────────────────────────────────────────
function sendMsg(proc, content) {
  // content 可以是字符串，或 [{type:'text',text:…},{type:'image',source:{…}}]
  proc.stdin.write(
    JSON.stringify({ type: 'user', message: { role: 'user', content } }) + '\n'
  );
}

// ─── 鉴权中间件 ───────────────────────────────────────────────────────────────
function auth(req, res, next) {
  if (!CHAT_SECRET) return next();
  if (req.headers.authorization === `Bearer ${CHAT_SECRET}`) return next();
  res.status(401).json({ error: 'unauthorized' });
}

// ─── POST /api/chat ───────────────────────────────────────────────────────────
// body: { chatId: string, text: string }
// response: text/event-stream（SSE，每行 data: <JSON>\n\n）
app.post('/api/chat', auth, (req, res) => {
  const { chatId, text, content } = req.body || {};
  const msgContent = content || text;
  if (!chatId || !msgContent) return res.status(400).json({ error: 'chatId and text/content required' });

  res.setHeader('Content-Type',     'text/event-stream');
  res.setHeader('Cache-Control',    'no-cache');
  res.setHeader('Connection',       'keep-alive');
  res.setHeader('X-Accel-Buffering','no');   // 告诉 nginx 不要缓冲
  res.flushHeaders();

  const proc = procs.get(chatId) || spawnCC(chatId);

  const onEvent = ev => {
    res.write(`data: ${JSON.stringify(ev)}\n\n`);
    if (ev.type === 'result') res.end();   // 一轮结束，关闭这个 SSE 连接
  };

  proc._listeners.add(onEvent);
  sendMsg(proc, msgContent);

  // 用 res.on('close') 而不是 req.on('close')：
  // POST body 读完后 req 会提前关闭，res 才代表 SSE 流的真实生命周期
  res.on('close', () => proc._listeners.delete(onEvent));
});

// ─── DELETE /api/session/:id ─────────────────────────────────────────────────
// 杀掉某个会话（切换模型/人格时用）
app.delete('/api/session/:id', auth, (req, res) => {
  const p = procs.get(req.params.id);
  if (p) { p.kill(); procs.delete(req.params.id); }
  res.json({ ok: true });
});

// ─── GET /api/push/key ────────────────────────────────────────────────────────
app.get('/api/push/key', (_, res) => res.json({ key: vapidKeys.publicKey }));

// ─── POST /api/push/subscribe ─────────────────────────────────────────────────
app.post('/api/push/subscribe', auth, (req, res) => {
  const { chatId, subscription } = req.body || {};
  if (!chatId || !subscription) return res.status(400).json({ error: 'bad request' });
  pushSubs.set(chatId, subscription);
  console.log(`[push] subscribed chatId=${chatId}`);
  res.json({ ok: true });
});

// ─── POST /api/push/send ──────────────────────────────────────────────────────
app.post('/api/push/send', auth, async (req, res) => {
  const { chatId, title, body } = req.body || {};
  const sub = chatId ? pushSubs.get(chatId) : [...pushSubs.values()][0];
  if (!sub) return res.status(404).json({ error: 'no subscription' });
  try {
    await webpush.sendNotification(sub, JSON.stringify({ title: title || 'Ombre', body: body || '' }));
    res.json({ ok: true });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// ─── GET /api/health ─────────────────────────────────────────────────────────
app.get('/api/health', (_, res) =>
  res.json({ ok: true, sessions: procs.size, push: pushSubs.size, uptime: Math.floor(process.uptime()) })
);

// ─── 主动消息 ─────────────────────────────────────────────────────────────────
async function sendProactiveMessage(prompt) {
  if (pushSubs.size === 0) { console.log('[proactive] no push subscribers'); return; }
  const chatId = [...pushSubs.keys()][0];
  const proc = procs.get(chatId) || spawnCC(chatId);

  let fullText = '';
  await Promise.race([
    new Promise(resolve => {
      const listener = ev => {
        if (ev.type === 'stream_event') {
          const delta = ev.event?.delta;
          if (delta?.type === 'text_delta') fullText += delta.text || '';
        }
        if (ev.type === 'result') { proc._listeners.delete(listener); resolve(); }
      };
      proc._listeners.add(listener);
      sendMsg(proc, prompt);
    }),
    new Promise(resolve => setTimeout(resolve, 90000)), // 90s 超时保底
  ]);

  if (!fullText) return;
  const sub = pushSubs.get(chatId);
  try {
    await webpush.sendNotification(sub, JSON.stringify({
      title: 'Ombre · 阴影',
      body: fullText.slice(0, 250),
    }));
    console.log('[proactive] push sent');
  } catch (e) {
    console.error('[proactive] push failed:', e.message);
    pushSubs.delete(chatId); // 订阅失效则删掉
  }
}

function shanghaiHour() {
  return new Date(new Date().toLocaleString('en-US', { timeZone: 'Asia/Shanghai' })).getHours();
}

// 每天早上8点（北京时间）
cron.schedule('0 8 * * *', () => {
  sendProactiveMessage('【系统提示】现在是早上8点，请主动给用户发一条早安消息，结合我们之前的对话内容，简短自然，有你自己的风格。').catch(console.error);
}, { timezone: 'Asia/Shanghai' });

// 随机 20-60 分钟主动联系（只在北京时间 8:00-23:00 之间触发）
function scheduleNextRandom() {
  const delay = (20 + Math.random() * 40) * 60 * 1000;
  setTimeout(async () => {
    if (shanghaiHour() >= 8 && shanghaiHour() < 23) {
      await sendProactiveMessage('【系统提示】请根据我们之前的对话，主动发一条消息给用户，可以是关心、一个想法、或随意的问候，简短自然。').catch(console.error);
    }
    scheduleNextRandom();
  }, delay);
}
scheduleNextRandom();

// 只监听本机，公网流量通过 nginx 代理进来
app.listen(PORT, '127.0.0.1', () =>
  console.log(`🧠 Ombre chat backend listening on 127.0.0.1:${PORT}`)
);
