import html
import logging
import requests

from Product_loader import config

logger = logging.getLogger(__name__)

TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendPhoto"
TELEGRAM_MAX_MESSAGE_LENGTH = 4096


def send_telegram_message(text: str, image_url: str | None = None) -> bool:
    """Send a plain message to the configured Telegram chat.
    Returns True on success, False otherwise (never raises)."""

    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        logger.warning(
            "Telegram not configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID "
            "missing) — skipping notification."
        )
        return False

    if image_url:
        url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendPhoto"

        payload = {
            "chat_id": config.TELEGRAM_CHAT_ID,
            "photo": image_url,
            "caption": text,
            "parse_mode": "HTML",
        }
    else:
        url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"

        payload = {
            "chat_id": config.TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        }

    try:
        resp = requests.post(url, json=payload, timeout=15)
        resp.raise_for_status()
        return True
    except requests.RequestException as e:
        # Telegram puts the real reason (e.g. "can't parse entities",
        # "chat not found") in the response body, not the exception str.
        body = resp.text if "resp" in locals() else ""
        logger.error("Failed to send Telegram message: %s | response: %s", e, body)
        return False


def format_deal_message(product, reason: str, old_price: float | None = None) -> str:
    """Build a human-readable deal alert for a Product row."""

    # parse_mode="HTML" means any raw &, <, > in scraped text (product
    # names/brands routinely have these — "M&Ms", "Size < 10", etc.)
    # breaks Telegram's parser and the whole message gets rejected.
    # Escape untrusted text; leave our own <b> tags untouched.
    def esc(value) -> str:
        return html.escape(str(value), quote=False)

    lines = []

    if reason == "price_drop":
        lines.append("🔻 <b>Price Drop!</b>")
    else:
        lines.append("🔥 <b>Hot Deal</b>")

    lines.append(f"<b>{esc(product.product_name)}</b>")

    if old_price is not None and reason == "price_drop":
        lines.append(f"💰Price: ₹{old_price:.0f} → ₹{product.current_price:.0f}")
    else:
        lines.append(f"💰Price: ₹{product.current_price:.0f}")
    if product.mrp:
        lines.append(f"💸MRP: ₹{product.mrp:.0f}")
    if product.discount_percent:
        lines.append(f"🎯Discount: {product.discount_percent:.0f}% off")

    link = product.affiliate_url or product.product_url
    if link:
        # Plain URL text, not an <a> tag, so no escaping needed here —
        # but guard against stray HTML-looking junk in scraped URLs anyway.
        lines.append(esc(link))

    return "\n".join(lines)
