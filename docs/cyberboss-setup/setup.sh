#!/usr/bin/env bash
# cyberboss + Ombre-Brain 一键安装脚本
# 用法：bash setup.sh [/path/to/Ombre-Brain]
set -e

WORKSPACE_ROOT="${1:-$(pwd)}"
OMBRE_URL="https://ombre-brain-1.onrender.com"

echo "==> 检查 Node.js 版本 (需要 >= 22)"
node --version

echo "==> 检查 Claude Code"
claude --version || { echo "ERROR: 未找到 claude 命令，请先安装 Claude Code CLI"; exit 1; }

echo "==> 克隆 cyberboss"
git clone https://github.com/WenXiaoWendy/cyberboss.git ~/cyberboss 2>/dev/null || (cd ~/cyberboss && git pull)

echo "==> 安装依赖"
cd ~/cyberboss && npm install

echo "==> 写入 cyberboss 配置"
sed "s|/absolute/path/to/Ombre-Brain|${WORKSPACE_ROOT}|g" \
    "$(dirname "$0")/.env.configured" > ~/cyberboss/.env

echo "==> 配置 Claude Code MCP：连接 Ombre-Brain 记忆系统"
claude mcp add ombre-brain --transport http "${OMBRE_URL}/mcp" 2>/dev/null || true
claude mcp add ombre-brain-extra --transport http "${OMBRE_URL}/mcp-extra" 2>/dev/null || true
echo "    MCP 已添加：${OMBRE_URL}/mcp"
echo "    MCP 已添加：${OMBRE_URL}/mcp-extra"

echo ""
echo "========================================"
echo "  配置完成！按顺序执行以下步骤："
echo "========================================"
echo ""
echo "  【第 1 步】扫码登录微信（会显示二维码，用手机扫）"
echo "     cd ~/cyberboss && npm run login"
echo ""
echo "  【第 2 步】查看你的微信账户 ID"
echo "     npm run accounts"
echo ""
echo "  【第 3 步】把账户 ID 填入配置"
echo "     编辑 ~/cyberboss/.env，把这一行的注释去掉并填入 ID："
echo "     CYBERBOSS_ALLOWED_USER_IDS=你的微信ID"
echo ""
echo "  【第 4 步】完成 Ombre-Brain OAuth 授权（只需一次）"
echo "     claude mcp list   # 确认两个 ombre-brain 服务器都在"
echo "     # 首次使用时 Claude Code 会自动弹出授权，用你的 Dashboard 密码登录"
echo "     # Dashboard 地址：${OMBRE_URL}"
echo ""
echo "  【第 5 步】启动（双端监控，开两个终端窗口）"
echo "     终端 1：cd ~/cyberboss && npm run shared:start"
echo "     终端 2：cd ~/cyberboss && npm run shared:open"
echo ""
echo "  【第 6 步】在微信里发送命令完成设置"
echo "     /bind ${WORKSPACE_ROOT}"
echo "     /checkin 20-60"
echo ""
echo "  日常检查：cd ~/cyberboss && npm run doctor"
echo ""
