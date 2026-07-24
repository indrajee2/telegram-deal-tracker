import logging

from Product_loader.models import DealsQueue, Product
from Product_loader.telegram_notifier import send_telegram_message, format_deal_message

logger = logging.getLogger(__name__)


def process_pending_deals(db, deal_ids=None):
    """Notify Telegram for pending deals, then mark them sent.

    If `deal_ids` is given, ONLY those deals_queue rows are sent — this is
    how the scheduler scopes each 10-URL batch to "notify just what this
    batch inserted/updated", instead of flushing every unsent row that
    ever piled up in the table. If `deal_ids` is None, falls back to the
    old behaviour of sending every unsent row (useful for manual/backlog
    flushes).

    Returns the number of deals successfully sent."""

    query = db.query(DealsQueue).filter(DealsQueue.sent.is_(False))

    if deal_ids is not None:
        if not deal_ids:
            return 0
        query = query.filter(DealsQueue.id.in_(deal_ids))

    pending = query.order_by(DealsQueue.created_at.asc()).all()

    if not pending:
        return 0

    sent_count = 0

    for deal in pending:
        product = db.query(Product).filter(Product.id == deal.product_id).first()

        if product is None:
            # Product was removed; drop the orphaned deal row.
            deal.sent = True
            continue

        message = format_deal_message(product, deal.reason, old_price=deal.old_price)

        if send_telegram_message(message,product.image_url):
            deal.sent = True
            sent_count += 1
        else:
            logger.warning(
                "Could not send Telegram alert for product_id=%s (will retry next cycle)",
                product.id,
            )

    db.commit()

    logger.info("Notifier: sent %s/%s pending deals", sent_count, len(pending))

    return sent_count
