
### Render Webhook Telegram Bot

Files:
- main.py: webhook-based bot using python-telegram-bot 21.x
- requirements.txt
- render.yaml: Web Service blueprint

Environment Variables to add in Render:
- TOKEN: Bot token from BotFather
- CHAT_ID: Your Telegram numeric chat id (e.g., 1733067489)
- (optional) WEBHOOK_BASE: public base URL of your service (Render sets RENDER_EXTERNAL_URL automatically)
- (auto) WEBHOOK_SECRET: random secret for Telegram webhook validation

Deploy:
1) Push these files to a public GitHub repo.
2) On render.com -> New + -> Blueprint -> pick your repo.
3) After service is created, open it -> Environment -> add TOKEN, CHAT_ID.
4) Deploy. On startup, the bot sets the webhook and sends a startup message.
5) In Telegram, send /ping to check.
