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
  const { chatId, text } = req.body || {};
  if (!chatId || !text) return res.status(400).json({ error: 'chatId and text required' });

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
  sendMsg(proc, text);

  // 客户端断开不 kill 进程：生成继续跑，下次重连从存储补发
  req.on('close', () => proc._listeners.delete(onEvent));
});

// ─── DELETE /api/session/:id ─────────────────────────────────────────────────
// 杀掉某个会话（切换模型/人格时用）
app.delete('/api/session/:id', auth, (req, res) => {
  const p = procs.get(req.params.id);
  if (p) { p.kill(); procs.delete(req.params.id); }
  res.json({ ok: true });
});

// ─── GET /api/health ─────────────────────────────────────────────────────────
app.get('/api/health', (_, res) =>
  res.json({ ok: true, sessions: procs.size, uptime: Math.floor(process.uptime()) })
);

// 只监听本机，公网流量通过 nginx 代理进来
app.listen(PORT, '127.0.0.1', () =>
  console.log(`🧠 Ombre chat backend listening on 127.0.0.1:${PORT}`)
);
