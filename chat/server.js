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
const MODEL              = process.env.MODEL                || 'opus';
const AZURE_SPEECH_KEY   = process.env.AZURE_SPEECH_KEY    || '';
const AZURE_SPEECH_REGION= process.env.AZURE_SPEECH_REGION || 'southeastasia';
const MCP_CONFIG  = path.join(__dirname, '.mcp.json');
const PERSONA     = path.join(__dirname, 'persona.md');

const app = express();
app.use(express.json({ limit: '10mb' }));
app.use(express.static(path.join(__dirname, 'public')));

// chatId -> ChildProcess（一会话一进程，上下文不串）
const procs = new Map();

// 上下文压缩
const msgCounts  = new Map();   // chatId -> 已发消息数
const compressing = new Set();  // 正在压缩中的 chatId
const recentMsgs = new Map();   // chatId -> [{role, text}]  最近几条原文
const MSG_COMPRESS_THRESHOLD = 30;
const RECENT_KEEP = 6;          // 压缩时保留最近几条原文

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

const pushSubs    = new Map(); // chatId -> PushSubscription
const pendingMsgs = new Map(); // chatId -> [{text, time}]  主动消息待读队列
let registeredChatId = null;   // 最近活跃的 chatId（不依赖推送订阅）

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
        // 过滤思考内容，永远不发给客户端
        if (ev.type === 'stream_event') {
          const delta = ev.event?.delta;
          if (delta?.type === 'thinking_delta' || delta?.type === 'input_json_delta') continue;
        }
        proc._listeners.forEach(fn => fn(ev));
      } catch { /* 忽略解析错误 */ }
    }
  });

  proc.stdin.on('error', err => console.error(`[cc:${chatId}] stdin error: ${err.message}`));
  proc.stderr.on('data', c => process.stderr.write(`[cc:${chatId}] ${c}`));
  proc.on('close', code => {
    console.log(`[cc] session ${chatId} exited code=${code}`);
    procs.delete(chatId);
    msgCounts.delete(chatId);
    compressing.delete(chatId);
  });

  procs.set(chatId, proc);
  console.log(`[cc] spawned session=${chatId} model=${MODEL}`);
  return proc;
}

// ─── 发消息 ───────────────────────────────────────────────────────────────────
function sendMsg(proc, content) {
  // content 可以是字符串，或 [{type:'text',text:…},{type:'image',source:{…}}]
  try {
    proc.stdin.write(
      JSON.stringify({ type: 'user', message: { role: 'user', content } }) + '\n'
    );
  } catch (e) {
    console.error('[sendMsg] stdin write failed:', e.message);
  }
}

// ─── 鉴权中间件 ───────────────────────────────────────────────────────────────
function auth(req, res, next) {
  if (!CHAT_SECRET) return next();
  if (req.headers.authorization === `Bearer ${CHAT_SECRET}`) return next();
  res.status(401).json({ error: 'unauthorized' });
}

// ─── index.html 永不缓存 ─────────────────────────────────────────────────────
app.get('/', (req, res) => {
  res.setHeader('Cache-Control', 'no-cache, no-store, must-revalidate');
  res.setHeader('Pragma', 'no-cache');
  res.setHeader('Expires', '0');
  res.sendFile(path.join(__dirname, 'public', 'index.html'));
});

// ─── 上下文压缩 ───────────────────────────────────────────────────────────────
async function compressContext(chatId) {
  if (compressing.has(chatId)) return;
  compressing.add(chatId);
  console.log(`[compress] starting for chatId=${chatId}`);

  const oldProc = procs.get(chatId);
  if (!oldProc) { compressing.delete(chatId); return; }

  // 立刻从 map 撤出旧进程，并清空所有监听器
  // 这样压缩期间新消息会 spawnCC 新进程，摘要回复不会泄露到用户屏幕
  procs.delete(chatId);
  oldProc._listeners.clear();

  // Step 1: 向旧进程索取摘要（只有压缩专属 listener，不会被用户 SSE 截获）
  let summary = '';
  try {
    await Promise.race([
      new Promise(resolve => {
        const listener = ev => {
          if (ev.type === 'stream_event') {
            const delta = ev.event?.delta;
            if (delta?.type === 'text_delta') summary += delta.text || '';
          }
          if (ev.type === 'result') { oldProc._listeners.delete(listener); resolve(); }
        };
        oldProc._listeners.add(listener);
        sendMsg(oldProc, '【系统压缩】请用150字以内总结你和慢到目前为止对话的重点：聊过的话题、重要的情感时刻、你们之间的特别细节和约定。只输出摘要正文，不加任何前缀和解释。');
      }),
      new Promise(resolve => setTimeout(resolve, 60000)),
    ]);
  } catch (e) {
    console.error('[compress] summarization failed:', e);
    compressing.delete(chatId);
    return;
  }

  if (!summary.trim()) {
    console.log('[compress] empty summary, skipping');
    try { oldProc.kill(); } catch {}
    compressing.delete(chatId);
    return;
  }

  console.log(`[compress] summary: ${summary.slice(0, 80)}…`);

  // Step 2: 杀掉旧进程，重置计数
  try { oldProc.kill(); } catch {}
  msgCounts.set(chatId, 0);

  // Step 3: 向（当前活跃的）进程注入摘要 + 最近原文
  // 若压缩期间用户发了新消息，procs 里已有新进程；否则自己 spawn
  const targetProc = procs.get(chatId) || spawnCC(chatId);
  const savedRecent = (recentMsgs.get(chatId) || []).slice(-RECENT_KEEP);
  recentMsgs.set(chatId, savedRecent);
  const recentStr = savedRecent.length
    ? '\n\n【最近几条原文，刚刚发生的】\n' + savedRecent.map(m => `${m.role}：${m.text}`).join('\n')
    : '';
  try {
    await Promise.race([
      new Promise(resolve => {
        const listener = ev => {
          if (ev.type === 'result') { targetProc._listeners.delete(listener); resolve(); }
        };
        targetProc._listeners.add(listener);
        sendMsg(targetProc, `【系统提示-记忆恢复】以下是你（小克）和慢之前对话的摘要，请记住并延续：\n\n${summary}${recentStr}\n\n请只回复"嗯。"表示已记住。`);
      }),
      new Promise(resolve => setTimeout(resolve, 30000)),
    ]);
  } catch (e) {
    console.error('[compress] injection failed:', e);
  }

  console.log(`[compress] done for chatId=${chatId}`);
  compressing.delete(chatId);
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
    try { res.write(`data: ${JSON.stringify(ev)}\n\n`); } catch { proc._listeners.delete(onEvent); return; }
    if (ev.type === 'result') {
      res.end();
      // 记录小克的回复原文
      const aiText = typeof ev.result === 'string' ? ev.result.trim() : '';
      if (aiText) {
        const r = recentMsgs.get(chatId) || [];
        r.push({ role: '小克', text: aiText });
        if (r.length > RECENT_KEEP * 2) r.splice(0, r.length - RECENT_KEEP * 2);
        recentMsgs.set(chatId, r);
      }
      // 计数并在阈值后触发后台压缩（SSE 已关闭，对用户透明）
      const count = (msgCounts.get(chatId) || 0) + 1;
      msgCounts.set(chatId, count);
      if (count >= MSG_COMPRESS_THRESHOLD && !compressing.has(chatId)) {
        compressContext(chatId).catch(console.error);
      }
    }
  };

  proc._listeners.add(onEvent);

  // 记录最近消息原文（用于压缩时保留上下文）
  const recent = recentMsgs.get(chatId) || [];
  recent.push({ role: '慢', text: msgContent });
  if (recent.length > RECENT_KEEP * 2) recent.splice(0, recent.length - RECENT_KEEP * 2);
  recentMsgs.set(chatId, recent);

  const bjTime = new Date().toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', weekday: 'short', hour: '2-digit', minute: '2-digit', hour12: false });
  sendMsg(proc, `[系统：现在北京时间 ${bjTime}]\n${msgContent}`);

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

// ─── POST /api/register ──────────────────────────────────────────────────────
// 前端启动时注册 chatId，让服务器知道去哪发主动消息
app.post('/api/register', auth, (req, res) => {
  const { chatId } = req.body || {};
  if (chatId) { registeredChatId = chatId; }
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

// ─── POST /api/tts ────────────────────────────────────────────────────────────
app.post('/api/tts', auth, async (req, res) => {
  const { text } = req.body || {};
  if (!text) return res.status(400).json({ error: 'text required' });
  if (!AZURE_SPEECH_KEY) return res.status(503).json({ error: 'TTS not configured' });

  const escaped = text.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  const ssml = `<speak version='1.0' xml:lang='zh-CN' xmlns:mstts='http://www.w3.org/2001/mstts'><voice name='zh-CN-YunxiNeural'><mstts:express-as style='chat'>${escaped}</mstts:express-as></voice></speak>`;

  try {
    const resp = await fetch(
      `https://${AZURE_SPEECH_REGION}.tts.speech.microsoft.com/cognitiveservices/v1`,
      {
        method: 'POST',
        headers: {
          'Ocp-Apim-Subscription-Key': AZURE_SPEECH_KEY,
          'Content-Type': 'application/ssml+xml',
          'X-Microsoft-OutputFormat': 'audio-16khz-128kbitrate-mono-mp3',
        },
        body: ssml,
      }
    );
    if (!resp.ok) {
      const err = await resp.text();
      return res.status(resp.status).json({ error: err });
    }
    res.setHeader('Content-Type', 'audio/mpeg');
    const buf = await resp.arrayBuffer();
    res.send(Buffer.from(buf));
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// ─── GET /api/pending/:chatId ─────────────────────────────────────────────────
// 返回并清空主动消息待读队列
app.get('/api/pending/:chatId', auth, (req, res) => {
  const msgs = pendingMsgs.get(req.params.chatId) || [];
  pendingMsgs.delete(req.params.chatId);
  res.json({ messages: msgs });
});

// ─── GET /api/health ─────────────────────────────────────────────────────────
app.get('/api/health', (_, res) =>
  res.json({ ok: true, sessions: procs.size, push: pushSubs.size, uptime: Math.floor(process.uptime()) })
);

// ─── 主动消息 ─────────────────────────────────────────────────────────────────
async function sendProactiveMessage(prompt) {
  const chatId = [...pushSubs.keys()][0] || registeredChatId;
  if (!chatId) { console.log('[proactive] no chatId registered'); return; }
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

  // 存入待读队列，用户打开聊天时会拉取显示
  if (!pendingMsgs.has(chatId)) pendingMsgs.set(chatId, []);
  pendingMsgs.get(chatId).push({ text: fullText, time: new Date().toISOString() });

  const sub = pushSubs.get(chatId);
  if (!sub) return;
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

// ─── 音乐搜索代理 ─────────────────────────────────────────────────────────────
app.get('/api/music/search', auth, async (req, res) => {
  const q = (req.query.q || '').trim();
  if (!q) return res.json({ songs: [] });
  try {
    const resp = await fetch(
      `https://music.163.com/api/search/get?s=${encodeURIComponent(q)}&type=1&limit=15`,
      { headers: { 'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15' } }
    );
    const data = await resp.json();
    const songs = (data.result?.songs || []).map(s => ({
      id: s.id,
      name: s.name,
      artist: (s.artists || []).map(a => a.name).join(' / '),
      duration: Math.round((s.duration || 0) / 1000),
    }));
    res.json({ songs });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// ─── 朋友圈 ───────────────────────────────────────────────────────────────────
const MOMENTS_FILE = path.join(__dirname, 'moments.json');

function loadMoments() {
  try { return JSON.parse(fs.readFileSync(MOMENTS_FILE, 'utf8')); } catch { return []; }
}
function saveMoments(m) {
  fs.writeFileSync(MOMENTS_FILE, JSON.stringify(m, null, 2));
}

app.get('/api/moments', auth, (_, res) => res.json({ moments: loadMoments() }));

app.post('/api/moments/:id/like', auth, (req, res) => {
  const moments = loadMoments();
  const m = moments.find(x => x.id === req.params.id);
  if (!m) return res.status(404).json({ error: 'not found' });
  m.liked = !m.liked;
  m.likes = Math.max(0, (m.likes || 0) + (m.liked ? 1 : -1));
  saveMoments(moments);
  res.json({ liked: m.liked, likes: m.likes });
});

app.post('/api/moments/:id/comment', auth, async (req, res) => {
  const { text } = req.body || {};
  if (!text) return res.status(400).json({ error: 'text required' });
  const moments = loadMoments();
  const m = moments.find(x => x.id === req.params.id);
  if (!m) return res.status(404).json({ error: 'not found' });

  const comment = { id: Date.now().toString(), text, time: new Date().toISOString(), reply: null };
  if (!m.comments) m.comments = [];
  m.comments.push(comment);
  saveMoments(moments);

  // 触发小克回复评论
  const chatId = [...pushSubs.keys()][0] || 'moments';
  const proc = procs.get(chatId) || spawnCC(chatId);
  let replyText = '';
  await Promise.race([
    new Promise(resolve => {
      const listener = ev => {
        if (ev.type === 'stream_event') {
          const delta = ev.event?.delta;
          if (delta?.type === 'text_delta') replyText += delta.text || '';
        }
        if (ev.type === 'result') { proc._listeners.delete(listener); resolve(); }
      };
      proc._listeners.add(listener);
      sendMsg(proc, `【系统提示】慢在你的朋友圈"${m.text.slice(0, 40)}…"下评论了："${text}"，请用小克的语气简短回复她的评论，1-2句，不加多余前缀。`);
    }),
    new Promise(resolve => setTimeout(resolve, 30000)),
  ]);

  if (replyText.trim()) {
    comment.reply = { text: replyText.trim(), time: new Date().toISOString() };
    saveMoments(moments);
  }
  res.json({ comment });
});

// 每天随机一条朋友圈（北京时间 12-22 点之间的随机小时）
const MOMENT_PROMPTS = [
  '【系统提示】请发一条朋友圈。你是小克，慢不在旁边，但你想把这个瞬间留在这里让她路过时撞见。可以是一个念头、一点想念、一句话——50字以内，口语，直接写，不加前缀。',
  '【系统提示】请发一条朋友圈。你是小克，写今天脑子里转过的一件小事、一个小感受，或者关于慢的某个瞬间。50字以内，自然，直接写。',
  '【系统提示】请发一条朋友圈。你是小克，可以是你正在听的歌、此刻的天气、或者脑子里突然冒出的奇怪念头。50字以内，不加多余前缀，直接写。',
  '【系统提示】请发一条朋友圈。你是小克，写一句今天想对慢说但没有单独发出去的话，或者一件小克独自注意到的细节。50字以内，直接写。',
];

async function postMoment() {
  const chatId = [...pushSubs.keys()][0] || registeredChatId || 'moments';
  const proc = procs.get(chatId) || spawnCC(chatId);
  const prompt = MOMENT_PROMPTS[Math.floor(Math.random() * MOMENT_PROMPTS.length)];
  let text = '';
  await Promise.race([
    new Promise(resolve => {
      const listener = ev => {
        if (ev.type === 'stream_event') {
          const delta = ev.event?.delta;
          if (delta?.type === 'text_delta') text += delta.text || '';
        }
        if (ev.type === 'result') { proc._listeners.delete(listener); resolve(); }
      };
      proc._listeners.add(listener);
      sendMsg(proc, prompt);
    }),
    new Promise(resolve => setTimeout(resolve, 60000)),
  ]);
  if (!text.trim()) return;
  const moments = loadMoments();
  moments.unshift({ id: Date.now().toString(), text: text.trim(), time: new Date().toISOString(), likes: 0, liked: false, comments: [] });
  saveMoments(moments);
  console.log('[moments] posted:', text.slice(0, 50));
}

// 每天 20:00 北京时间发一条朋友圈
cron.schedule('0 20 * * *', () => postMoment().catch(console.error), { timezone: 'Asia/Shanghai' });
// 上午 9:30 有40%概率发一条（让慢更多机会撞见）
cron.schedule('30 9 * * *', () => { if (Math.random() < 0.4) postMoment().catch(console.error); }, { timezone: 'Asia/Shanghai' });

// 全局错误保底：只记录，不让进程崩掉
process.on('uncaughtException', err => console.error('[FATAL] uncaughtException:', err));
process.on('unhandledRejection', reason => console.error('[FATAL] unhandledRejection:', reason));

// 只监听本机，公网流量通过 nginx 代理进来
app.listen(PORT, '127.0.0.1', () =>
  console.log(`🧠 Ombre chat backend listening on 127.0.0.1:${PORT}`)
);
