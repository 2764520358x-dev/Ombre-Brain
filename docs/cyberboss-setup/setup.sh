#!/usr/bin/env bash
# cyberboss 本地安装脚本
# 用法：bash setup.sh [/path/to/Ombre-Brain]
set -e

WORKSPACE_ROOT="${1:-$(pwd)}"

echo "==> 检查 Node.js 版本 (需要 >= 22)"
node --version

echo "==> 克隆 cyberboss"
git clone https://github.com/WenXiaoWendy/cyberboss.git ~/cyberboss 2>/dev/null || (cd ~/cyberboss && git pull)

echo "==> 安装依赖"
cd ~/cyberboss && npm install

echo "==> 写入配置"
sed "s|/absolute/path/to/Ombre-Brain|${WORKSPACE_ROOT}|g" \
    "$(dirname "$0")/.env.configured" > ~/cyberboss/.env

echo ""
echo "==> 配置完成！下一步："
echo ""
echo "  1. 扫码登录微信："
echo "     cd ~/cyberboss && npm run login"
echo ""
echo "  2. 登录成功后，查看账户 ID："
echo "     npm run accounts"
echo ""
echo "  3. 把账户 ID 填入 ~/cyberboss/.env 的 CYBERBOSS_ALLOWED_USER_IDS"
echo ""
echo "  4. 启动（双端监控）——开两个终端窗口："
echo "     终端 1：cd ~/cyberboss && npm run shared:start"
echo "     终端 2：cd ~/cyberboss && npm run shared:open"
echo ""
echo "  5. 在微信里发送以下命令绑定项目并开启随机轮询："
echo "     /bind ${WORKSPACE_ROOT}"
echo "     /checkin 20-60"
echo ""
echo "  运行 npm run doctor 随时检查配置状态。"
