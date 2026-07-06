#!/bin/bash
# 在腾讯云服务器上运行此脚本完成所有安装
# 用法: bash setup.sh

set -e
echo "=== Ombre Chat 服务器安装脚本 ==="

# 1. 安装 Node.js 20
echo ">> 安装 Node.js 20..."
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt-get install -y nodejs

# 2. 安装 Claude Code
echo ">> 安装 Claude Code..."
sudo npm install -g @anthropic-ai/claude-code

# 3. 安装 PM2（进程守护）
echo ">> 安装 PM2..."
sudo npm install -g pm2

# 4. 安装 Nginx
echo ">> 安装 Nginx..."
sudo apt-get install -y nginx certbot python3-certbot-nginx

# 5. 安装项目依赖
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
echo ">> 安装 Node 依赖..."
cd "$SCRIPT_DIR"
npm install

# 6. 创建 .env 文件（如果不存在）
if [ ! -f "$SCRIPT_DIR/.env" ]; then
  echo ">> 创建 .env 文件..."
  cat > "$SCRIPT_DIR/.env" << 'EOF'
PORT=3000
MODEL=opus
# CHAT_SECRET=改成你的密钥（空=不需要密钥）
CHAT_SECRET=
# 禁止 API key 覆盖订阅登录（已在代码里处理，这里留空）
ANTHROPIC_API_KEY=
EOF
  echo "   ⚠  请编辑 .env 文件设置 CHAT_SECRET"
fi

echo ""
echo "=== 安装完成！接下来的步骤 ==="
echo ""
echo "1. 登录 Claude（用你的订阅账号）:"
echo "   claude login"
echo ""
echo "2. 测试 Claude 连通:"
echo "   claude -p 'ping' --output-format json"
echo ""
echo "3. 配置并启动后端:"
echo "   nano $SCRIPT_DIR/.env   # 设置 CHAT_SECRET"
echo "   pm2 start $SCRIPT_DIR/server.js --name ombre-chat --env-file $SCRIPT_DIR/.env"
echo "   pm2 save && pm2 startup"
echo ""
echo "4. 配置 Nginx:"
echo "   sudo cp $SCRIPT_DIR/nginx.conf /etc/nginx/sites-available/ombre"
echo "   sudo nano /etc/nginx/sites-available/ombre  # 改 server_name"
echo "   sudo ln -sf /etc/nginx/sites-available/ombre /etc/nginx/sites-enabled/"
echo "   sudo nginx -t && sudo systemctl reload nginx"
echo ""
echo "5. 申请 HTTPS 证书（需要域名）:"
echo "   sudo certbot --nginx -d your-domain.com"
echo ""
echo "   没有域名？先用 IP 测试（http://43.156.80.36:3000），之后再加域名+HTTPS"
echo ""
echo "6. Render 上关闭 Ombre-Brain MCP 认证（让 Claude 直连）:"
echo "   在 Render 环境变量里加: OMBRE_MCP_REQUIRE_AUTH=false"
echo ""
echo "完成后用手机浏览器访问你的地址，点「添加到主屏幕」安装 PWA"
