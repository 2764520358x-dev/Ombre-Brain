#!/usr/bin/env bash
# cyberboss + Ombre-Brain 一键安装脚本
set -e

OMBRE_URL="https://ombre-brain-1.onrender.com"

echo "==> [1/6] 安装 Node.js 22"
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt-get install -y nodejs
node --version

echo "==> [2/6] 安装 Claude Code"
sudo npm install -g @anthropic-ai/claude-code
claude --version

echo "==> [3/6] 克隆 cyberboss 并安装依赖"
git clone https://github.com/WenXiaoWendy/cyberboss.git ~/cyberboss 2>/dev/null || (cd ~/cyberboss && git pull)
cd ~/cyberboss && npm install

echo "==> [4/6] 写入 cyberboss 配置"
cat > ~/cyberboss/.env << 'ENVEOF'
CYBERBOSS_USER_NAME=慢
CYBERBOSS_USER_GENDER=female
CYBERBOSS_WORKSPACE_ROOT=/home/ubuntu/cyberboss
CYBERBOSS_RUNTIME=claudecode
# CYBERBOSS_ALLOWED_USER_IDS=填入微信ID
CYBERBOSS_ENABLE_CHECKIN=true
CYBERBOSS_STATE_DIR=/home/ubuntu/.cyberboss
CYBERBOSS_CLAUDE_COMMAND=claude
CYBERBOSS_CLAUDE_PERMISSION_MODE=default
ENVEOF
echo "    .env 已写入"

echo "==> [5/6] 写入 system prompt（记忆指南 + 微信人格）"
mkdir -p ~/.cyberboss
curl -fsSL "https://raw.githubusercontent.com/2764520358x-dev/Ombre-Brain/claude/cyberboss-wechat-setup-8qxr3i/docs/cyberboss-setup/weixin-instructions.md" \
  | sed 's|{{USER_NAME}}|慢|g' > ~/.cyberboss/weixin-instructions.md
echo "    system prompt 已写入 ~/.cyberboss/weixin-instructions.md"

echo "==> [6/6] 配置 Claude Code MCP：连接 Ombre-Brain 记忆"
claude mcp add ombre-brain --transport http "${OMBRE_URL}/mcp" 2>/dev/null || true
claude mcp add ombre-brain-extra --transport http "${OMBRE_URL}/mcp-extra" 2>/dev/null || true
echo "    MCP 已添加"

echo ""
echo "========================================"
echo "  安装完成！下一步："
echo "========================================"
echo ""
echo "  【第 1 步】登录 Claude Code（用你的 Claude 账号）"
echo "     claude login"
echo ""
echo "  【第 2 步】扫码登录微信小号"
echo "     cd ~/cyberboss && npm run login"
echo ""
echo "  【第 3 步】查看微信账户 ID"
echo "     cd ~/cyberboss && npm run accounts"
echo ""
echo "  【第 4 步】把 ID 填入配置（把 # 去掉并填入）"
echo "     nano ~/cyberboss/.env"
echo ""
echo "  【第 5 步】启动！开两个终端标签页"
echo "     标签1：cd ~/cyberboss && npm run shared:start"
echo "     标签2：cd ~/cyberboss && npm run shared:open"
echo ""
echo "  【第 6 步】微信里发这两条命令"
echo "     /bind /home/ubuntu/cyberboss"
echo "     /checkin 20-60"
echo ""
