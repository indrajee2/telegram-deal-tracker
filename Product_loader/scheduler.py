import json
import logging
import time
from datetime import datetime
from pathlib import Path

from Product_loader import config
from Product_loader.database import SessionLocal, engine, Base, run_migrations
from Product_loader import models  # noqa: F401  (ensures tables are registered)
from Product_loader.product_sync import run_sync_dir
from Product_loader.notifier import process_pending_deals

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Round-robin batching for scraped URL lists (Flipkart, Amazon, ...)
# ---------------------------------------------------------------------------
# Instead of scraping every configured URL on every cycle, we walk through
# the list a fixed number of URLs at a time (e.g. config.FLIPKART_BATCH_SIZE
# / config.AMAZON_BATCH_SIZE). The starting index for the next cycle is
# persisted to a small per-source JSON file so it survives process restarts
# (the scheduler is meant to run forever, but this keeps behaviour correct
# even if it's ever restarted), and each source rotates independently.

def _load_batch_index(state_file: Path) -> int:
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
        return int(data.get("next_index", 0))
    except Exception:
        return 0


def _save_batch_index(state_file: Path, next_index: int) -> None:
    try:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps({"next_index": next_index}), encoding="utf-8")
    except Exception:
        logger.exception("Could not persist batch state to %s", state_file)


def get_next_url_batch(urls, batch_size: int, state_file: Path, label: str = "URL"):
    """Return the next `batch_size` URLs from `urls`, starting where the
    previous cycle left off, wrapping back to the start once the end of
    the list is reached. Also advances and persists the state file.
    `label` is only used for the log line (e.g. "Flipkart", "Amazon")."""

    total = len(urls)
    if total == 0 or batch_size <= 0:
        return []

    start = _load_batch_index(state_file) % total
    size = min(batch_size, total)

    batch = [urls[(start + i) % total] for i in range(size)]

    next_index = (start + size) % total
    _save_batch_index(state_file, next_index)

    logger.info(
        "%s batch: URLs %d-%d of %d (indices %s)",
        label,
        start + 1,
        start + size,
        total,
        [(start + i) % total for i in range(size)],
    )
    return batch


def scrape_step():
    """Re-hit configured URL lists and overwrite each source's fixed JSON
    file in config.DATA_DIR with fresh results.

    Both Flipkart and Amazon are processed in rotating batches
    (config.FLIPKART_BATCH_SIZE / config.AMAZON_BATCH_SIZE URLs per cycle,
    default 10 each) instead of scraping their full URL lists every cycle —
    see get_next_url_batch(). Each source's rotation position is persisted
    to its own state file, so consecutive cycles move through that source's
    full list independently and wrap back to its first URL once the last
    one has been processed.

    Each scraper is lazy-imported (only when its URL list is non-empty) so
    a missing Playwright install doesn't break sync-only usage. Errors in
    any one scraper are logged, not raised, so a broken scrape doesn't stop
    the sync of whatever's already on disk."""

    ran_any = False

    flipkart_urls = getattr(config, "FLIPKART_URLS", None)
    if flipkart_urls:
        ran_any = True
        try:
            batch_size = getattr(config, "FLIPKART_BATCH_SIZE", 10)
            state_file = Path(
                getattr(
                    config,
                    "FLIPKART_BATCH_STATE_FILE",
                    Path(config.DATA_DIR) / "flipkart_batch_state.json",
                )
            )
            batch = get_next_url_batch(flipkart_urls, batch_size, state_file, label="Flipkart")

            logger.info(
                "Scraping %d of %d Flipkart URL(s) this cycle ...",
                len(batch), len(flipkart_urls),
            )
            from Product_loader.flipkart_scraper import FlipkartScraper, export_json as fk_export

            products = FlipkartScraper().scrape_urls(batch)
            fk_export(products, Path(config.DATA_DIR) / "flipkart_products.json")
        except Exception:
            logger.exception("Flipkart scrape step failed — will still sync existing files")

    amazon_urls = getattr(config, "AMAZON_URLS", None)
    if amazon_urls:
        ran_any = True
        try:
            batch_size = getattr(config, "AMAZON_BATCH_SIZE", 10)
            state_file = Path(
                getattr(
                    config,
                    "AMAZON_BATCH_STATE_FILE",
                    Path(config.DATA_DIR) / "amazon_batch_state.json",
                )
            )
            batch = get_next_url_batch(amazon_urls, batch_size, state_file, label="Amazon")

            logger.info(
                "Scraping %d of %d Amazon URL(s) this cycle ...",
                len(batch), len(amazon_urls),
            )
            from Product_loader.amazon_scraper import AmazonScraper, export_json as az_export

            products = AmazonScraper().scrape_urls(batch)
            az_export(products, Path(config.DATA_DIR) / "amazon_products.json")
        except Exception:
            logger.exception("Amazon scrape step failed — will still sync existing files")

    url_file = Path(getattr(config, "URLS_FILE", "urls.txt"))
    if url_file.exists():
        ran_any = True
        try:
            logger.info("Scraping URLs from %s ...", url_file)
            from Product_loader.master_url_products import scrape_from_url_file

            scrape_from_url_file(
                url_file,
                Path(config.DATA_DIR) / "scraped.json",
                min_discount=config.MIN_DISCOUNT_PERCENT,
            )
        except Exception:
            logger.exception("URLS_FILE scrape step failed — will still sync existing files")

    if not ran_any:
        logger.warning(
            "No URLs configured (config.FLIPKART_URLS / config.AMAZON_URLS "
            "are empty and %s doesn't exist) — skipping scrape this cycle "
            "(only syncing existing files in %s).",
            url_file, config.DATA_DIR,
        )


def run_cycle():
    """One full pass: re-scrape configured sources -> fresh JSON, sync all
    JSON in DATA_DIR -> DB (matching by platform+external_id so price
    changes are caught), then push any queued deals (new 80%+ discount
    items and price drops) to Telegram."""

    if getattr(config, "AUTO_SCRAPE", True):
        scrape_step()

    db = SessionLocal()
    try:
        logger.info("Syncing products from %s ...", config.DATA_DIR)
        stats, deal_ids = run_sync_dir(db, config.DATA_DIR)
        logger.info("Sync stats: %s", stats)

        # Only notify for what THIS batch just inserted/updated — not the
        # whole deals_queue backlog. Pass the specific ids created above.
        sent = process_pending_deals(db, deal_ids=deal_ids)
        logger.info("Telegram: %s deal(s) sent (from %d queued this cycle)", sent, len(deal_ids))
    finally:
        db.close()


def run_forever():
    Base.metadata.create_all(bind=engine)
    run_migrations()

    delay_seconds = getattr(config, "CYCLE_DELAY_SECONDS", 0) or 0

    logger.info(
        "Starting price-tracking scheduler: continuous mode (%s), "
        "min discount %.0f%%, %d Flipkart URL(s) total (%d per cycle, round-robin), "
        "%d Amazon URL(s) total (%d per cycle, round-robin), data dir '%s'",
        f"{delay_seconds:.0f}s pause between cycles" if delay_seconds > 0 else "no pause, back-to-back",
        config.MIN_DISCOUNT_PERCENT,
        len(getattr(config, "FLIPKART_URLS", []) or []),
        getattr(config, "FLIPKART_BATCH_SIZE", 10),
        len(getattr(config, "AMAZON_URLS", []) or []),
        getattr(config, "AMAZON_BATCH_SIZE", 10),
        config.DATA_DIR,
    )

    while True:
        started = datetime.now()
        try:
            run_cycle()
        except Exception:
            logger.exception("Cycle failed, retrying immediately")

        elapsed = (datetime.now() - started).total_seconds()

        if delay_seconds > 0:
            logger.info(
                "Cycle finished in %.1fs. Pausing %.1fs before next batch...",
                elapsed, delay_seconds,
            )
            time.sleep(delay_seconds)
        else:
            logger.info(
                "Cycle finished in %.1fs. Starting next batch immediately...",
                elapsed,
            )


if __name__ == "__main__":
    run_forever()
