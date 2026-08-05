/**
 * pm2 ecosystem config for 小克 Telegram bot
 *
 * Deploy on Singapore:
 *   TG_TOKEN=xxx TG_USER_ID=8972876200 pm2 start tg-pm2.config.js
 *   pm2 save
 *
 * Or export env vars first:
 *   export TG_TOKEN=xxx TG_USER_ID=8972876200
 *   pm2 start tg-pm2.config.js && pm2 save
 */
module.exports = {
  apps: [
    {
      name: 'xiaoke-tg',
      script: '/root/xiaoke-tg/tg-bot.js',
      cwd: '/root/xiaoke-tg',
      watch: false,
      autorestart: true,
      restart_delay: 3000,
      max_restarts: 10,
      env: {
        TG_WORK_DIR: '/root/xiaoke-tg',
      },
    },
  ],
};
