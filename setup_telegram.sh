#!/usr/bin/env bash
# One-time Telegram setup. Prompts for the bot token (hidden), finds your chat
# id from the /start message you sent the bot, and stores both as encrypted
# GitHub Actions secrets. Nothing is written to disk or printed.
set -euo pipefail
REPO="${1:-$(gh repo view --json nameWithOwner -q .nameWithOwner)}"
echo "Repository: $REPO"
read -r -s -p "Paste your Telegram bot token from @BotFather (input hidden): " TOKEN; echo
[[ "$TOKEN" =~ ^[0-9]+:[A-Za-z0-9_-]{30,}$ ]] || { echo "That doesn't look like a bot token."; exit 1; }

api() { printf 'url = "https://api.telegram.org/bot%s/%s"\n' "$TOKEN" "$1" | curl -s --max-time 20 -K -; }

ME=$(api getMe)
echo "$ME" | grep -q '"ok":true' || { echo "Telegram rejected the token."; exit 1; }
BOT=$(echo "$ME" | python3 -c 'import json,sys;print(json.load(sys.stdin)["result"]["username"])')
echo "Bot: @$BOT"

CHAT=$(api getUpdates | python3 -c '
import json,sys
d=json.load(sys.stdin); ids=[u["message"]["chat"]["id"] for u in d.get("result",[]) if "message" in u and u["message"]["chat"]["type"]=="private"]
print(ids[-1] if ids else "")')
if [ -z "$CHAT" ]; then
  echo "No message found. Open https://t.me/$BOT in Telegram, tap Start (or send /start), then re-run this script."
  exit 1
fi

printf '%s' "$TOKEN" | gh secret set TELEGRAM_BOT_TOKEN -R "$REPO"
printf '%s' "$CHAT"  | gh secret set TELEGRAM_CHAT_ID  -R "$REPO"
echo "Saved TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID as GitHub Actions secrets on $REPO."
