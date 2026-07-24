#!/usr/bin/env node
/**
 * Patches /root/minecraft-bot-local/server.js to add persistent player chat history.
 * Adds get_player_chat MCP tool so 小克 can read what players said in-game.
 *
 * Run on Shanghai: node mc-chat-patch.js
 */
const fs = require('fs');
const FILE = '/root/minecraft-bot-local/server.js';

let code = fs.readFileSync(FILE, 'utf8');
const original = code;

// ─── 1. Add global playerChatHistory array after ITEM_DICT_PATH ───────────────
if (!code.includes('playerChatHistory')) {
  code = code.replace(
    'const ITEM_DICT_PATH = path.join(__dirname, "item_dict.json");',
    'const ITEM_DICT_PATH = path.join(__dirname, "item_dict.json");\nconst playerChatHistory = []; // persistent player chat buffer'
  );
  console.log('✓ [1] Added playerChatHistory global');
} else {
  console.log('~ [1] playerChatHistory already exists, skipped');
}

// ─── 2. Push to buffer inside permanent bot.on("chat") handler ────────────────
// The permanent handler has the [BI] check nearby, use that as anchor
if (!code.includes('playerChatHistory.push(')) {
  const logLine = 'log(`[chat] <${username}> ${message}`);';
  const idx = code.indexOf(logLine);
  if (idx === -1) {
    console.error('✗ [2] Could not find log line in chat handler');
  } else {
    const nearby = code.slice(idx, idx + 400);
    if (!nearby.includes('startsWith("[BI]")')) {
      console.error('✗ [2] Found log line but [BI] check not nearby — wrong handler');
    } else {
      const push = '\n    playerChatHistory.push({ time: new Date().toISOString(), username, message });\n    if (playerChatHistory.length > 100) playerChatHistory.shift();';
      code = code.slice(0, idx + logLine.length) + push + code.slice(idx + logLine.length);
      console.log('✓ [2] Added push to permanent chat handler');
    }
  }
} else {
  console.log('~ [2] push already exists, skipped');
}

// ─── 3. Add get_player_chat to tool list (after debug_chat entry) ────────────
if (!code.includes('"get_player_chat"')) {
  // Match the debug_chat tool block: from { to the closing },
  const debugChatRe = /(\{\s*\n\s*name:\s*"debug_chat"[\s\S]*?inputSchema:\s*\{[\s\S]*?\}\s*\},\n)/;
  const newToolDef = `    {
      name: "get_player_chat",
      description: "获取玩家在游戏里说的聊天消息（慢/ANN275说的话）。每次调用后清空缓冲区。用这个来看慢在游戏里说了什么，不要用debug_chat。",
      inputSchema: {
        type: "object",
        properties: {},
      },
    },\n`;
  if (debugChatRe.test(code)) {
    code = code.replace(debugChatRe, (m) => m + newToolDef);
    console.log('✓ [3] Added get_player_chat tool definition');
  } else {
    console.error('✗ [3] Could not find debug_chat tool block to insert after');
  }
} else {
  console.log('~ [3] get_player_chat tool already defined, skipped');
}

// ─── 4. Add get_player_chat handler in CallToolRequestSchema ─────────────────
const handlerStr = 'mcp.setRequestHandler(CallToolRequestSchema, async (req) => {';
const handlerCase = `
  const _name = req.params.name;
  if (_name === "get_player_chat") {
    if (playerChatHistory.length === 0) {
      return { content: [{ type: "text", text: "没有新的玩家聊天消息" }] };
    }
    const msgs = playerChatHistory.splice(0);
    const text = msgs.map(m => \`[\${m.time.slice(11, 16)}] <\${m.username}> \${m.message}\`).join("\\n");
    return { content: [{ type: "text", text: text }] };
  }
`;

if (!code.includes('_name === "get_player_chat"')) {
  if (code.includes(handlerStr)) {
    code = code.replace(handlerStr, handlerStr + handlerCase);
    console.log('✓ [4] Added get_player_chat handler case');
  } else {
    console.error('✗ [4] Could not find CallToolRequestSchema handler');
  }
} else {
  console.log('~ [4] handler already exists, skipped');
}

// ─── Write & verify ───────────────────────────────────────────────────────────
if (code === original) {
  console.error('\n✗ No changes were made — all patterns already patched or failed to match');
  process.exit(0);
}

// Backup original
fs.writeFileSync(FILE + '.bak', original);
console.log('\n~ Backup saved to server.js.bak');

fs.writeFileSync(FILE, code);
console.log('✓ server.js patched\n');

// Verify
console.log('Verification:');
const final = fs.readFileSync(FILE, 'utf8');
console.log('  playerChatHistory global:', final.includes('const playerChatHistory = []') ? 'OK' : 'MISSING');
console.log('  playerChatHistory.push  :', final.includes('playerChatHistory.push(') ? 'OK' : 'MISSING');
console.log('  get_player_chat tool    :', final.includes('"get_player_chat"') ? 'OK' : 'MISSING');
console.log('  get_player_chat handler :', final.includes('_name === "get_player_chat"') ? 'OK' : 'MISSING');
