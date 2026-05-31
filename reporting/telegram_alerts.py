import requests
import logging
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

log = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org/bot{token}/sendMessage"


def send_message(text, chat_id=None):
    if not TELEGRAM_BOT_TOKEN:
        log.error("LEAP_BOT_TOKEN not set")
        return
    target = chat_id or TELEGRAM_CHAT_ID
    try:
        resp = requests.post(
            API_BASE.format(token=TELEGRAM_BOT_TOKEN),
            json={"chat_id": target, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
        resp.raise_for_status()
    except Exception as e:
        log.error("Telegram send failed: %s", e)
