from pathlib import Path
import json
from Product_loader.models import Product, PriceHistory, DealsQueue
from Product_loader import config
from Product_loader.update_links import get_affiliate_url


import re


def extract_amazon_asin(url: str) -> str | None:
    """Extract ASIN from Amazon URL."""
    if not url:
        return None

    match = re.search(r"/dp/([A-Z0-9]{10})", url)
    if match:
        return match.group(1)

    match = re.search(r"/gp/product/([A-Z0-9]{10})", url)
    if match:
        return match.group(1)

    return None


def safe_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def compute_discount_percent(mrp, selling_price):
    mrp = safe_float(mrp)
    selling_price = safe_float(selling_price)

    if not mrp or mrp <= 0 or selling_price is None:
        return 0

    return round(((mrp - selling_price) / mrp) * 100, 2)


def normalize_entry(raw: dict) -> dict | None:
    """
    Normalize Amazon / Flipkart / Myntra product JSON
    into one common format.
    """

    platform = (raw.get("platform") or "").strip().lower()

    # ---------- Product ID ----------
    if platform == "amazon":
        external_id = (
            raw.get("product_id")
            or extract_amazon_asin(raw.get("product_url") or raw.get("url"))
        )
    else:
        external_id = raw.get("product_id")

    if not external_id:
        return None

    # ---------- Prices ----------
    selling_price = safe_float(raw.get("selling_price"))
    mrp = safe_float(raw.get("mrp"))

    if selling_price is None:
        return None

    discount_percent = compute_discount_percent(mrp, selling_price)

    # Only ingest deals that clear the configured discount threshold
    # (default 80%). Everything below is skipped, not just unflagged.
    if discount_percent < config.MIN_DISCOUNT_PERCENT:
        return None

    # ---------- URLs ----------
    # NOTE: we deliberately do NOT call get_affiliate_url() here anymore.
    # normalize_entry() runs for every single product on every sync cycle
    # (including ones that turn out "unchanged"), so converting here meant
    # burning an EKaro API call for products whose affiliate link never
    # needed to change. The conversion now happens in sync_product(),
    # only for products that are actually new or whose price changed.
    product_url = raw.get("product_url") or raw.get("url")

    return {
        "platform": platform,
        "external_id": str(external_id),
        "brand": raw.get("brand", ""),
        "product_name": (raw.get("product_name") or "").strip(),
        "current_price": selling_price,
        "lowest_price": selling_price,
        "mrp": mrp,
        "discount_percent": discount_percent,
        "rating": safe_float(raw.get("rating")),
        "bank_offer": raw.get("bank_offer"),
        "coupon_discount": safe_float(
            raw.get("coupon_discount")
        ) or 0,

        "product_url": product_url,

        "image_url": raw.get("image_url"),

        "available": bool(
            raw.get(
                "available",
                raw.get("stock", True),
            )
        ),
    }


def sync_product(db, item):

    product = (
        db.query(Product)
        .filter(
            Product.platform == item["platform"],
            Product.external_id == item["external_id"]
        )
        .first()
    )

    if product is None:

        # New product: convert to affiliate URL now, once, and store the
        # result — this is the only time we pay for the EKaro API call.
        if item["platform"] == "flipkart":
            item["product_url"] = get_affiliate_url(item["product_url"])

        product = Product(**item)

        db.add(product)
        db.flush()

        save_price_history(db, product)

        deal_id = None
        if config.NOTIFY_ON_NEW_HIGH_DISCOUNT:
            deal_id = queue_deal(db, product, reason="high_discount")

        return "inserted", deal_id


    if product.current_price != item["current_price"]:

        old_price = product.current_price

        product.current_price = item["current_price"]

        if item["current_price"] < product.lowest_price:
            product.lowest_price = item["current_price"]

        product.mrp = item["mrp"]
        product.discount_percent = item["discount_percent"]
        product.rating = item["rating"]
        product.available = item["available"]

        # Price actually changed, so refresh the affiliate link too (in
        # case the source URL shifted) — still only one EKaro call for
        # this product this cycle, not one per product regardless of
        # whether anything changed.
        if item["platform"] == "flipkart":
            product.product_url = get_affiliate_url(item["product_url"])
        else:
            product.product_url = item["product_url"]

        save_price_history(db, product)

        deal_id = None
        if item["current_price"] < old_price:
            deal_id = queue_deal(db, product, reason="price_drop", old_price=old_price)

        return "updated", deal_id

    # Unchanged: reuse product.product_url exactly as already stored in
    # the DB — no EKaro call, no write.
    return "unchanged", None


def queue_deal(db, product, reason: str, old_price: float | None = None):
    """Add a product to the deals_queue so the notifier can pick it up
    (sent=False by default). Flushes so the row gets an id immediately,
    which the caller uses to scope notifications to just this batch."""

    deal = DealsQueue(
        product_id=product.id,
        reason=reason,
        price=product.current_price,
        old_price=old_price,
        discount_percent=product.discount_percent,
    )
    db.add(deal)
    db.flush()
    return deal.id

def run_sync(db, json_path):

    data = json.loads(Path(json_path).read_text(encoding="utf-8"))

    # Support both:
    # 1. [ {...}, {...} ]
    # 2. { "products": [ {...}, {...} ] }
    if isinstance(data, list):
        products = data
    elif isinstance(data, dict):
        products = data.get("products", [])
    else:
        raise ValueError("Invalid JSON format.")

    stats = {
        "inserted": 0,
        "updated": 0,
        "unchanged": 0,
        "skipped": 0,
    }
    deal_ids = []

    for raw in products:
        try:
            item = normalize_entry(raw)

            if item is None:
                stats["skipped"] += 1
                continue

            action, deal_id = sync_product(db, item)
            stats[action] += 1
            if deal_id is not None:
                deal_ids.append(deal_id)

        except Exception as e:
            print(f"Error processing product: {e}")
            stats["skipped"] += 1

    db.commit()

    print("Sync Complete")
    print(stats)

    return stats, deal_ids




def run_sync_dir(db, dir_path):
    """Sync every *.json file found in dir_path. Returns (combined_stats,
    deal_ids) where deal_ids are the deals_queue rows created by *this*
    call only — pass them straight to the notifier so it only sends what
    this batch actually inserted/updated, not the whole backlog."""

    dir_path = Path(dir_path)
    totals = {"inserted": 0, "updated": 0, "unchanged": 0, "skipped": 0}
    all_deal_ids = []

    if not dir_path.exists():
        print(f"Data dir not found: {dir_path}")
        return totals, all_deal_ids

    json_files = sorted(dir_path.glob("*.json"))

    if not json_files:
        print(f"No JSON files found in {dir_path}")
        return totals, all_deal_ids

    for f in json_files:
        print(f"Syncing {f.name} ...")
        stats, deal_ids = run_sync(db, f)
        for k in totals:
            totals[k] += stats.get(k, 0)
        all_deal_ids.extend(deal_ids)

    return totals, all_deal_ids


def save_price_history(db, product):
    history = PriceHistory(
        product_id=product.id,
        price=product.current_price,
        mrp=product.mrp,
        discount_percent=product.discount_percent,
    )

    db.add(history)


def remove_products_by_discount(db, min_discount: float = 90, dry_run: bool = False) -> int:
    """Delete every product with discount_percent >= min_discount, along
    with its price_history and deals_queue rows.

    SQLite doesn't enforce ON DELETE CASCADE by default and there's no
    ORM relationship() wired up between these tables, so child rows are
    deleted explicitly here rather than relying on the FK definition.

    Args:
        db: an open SQLAlchemy session.
        min_discount: discount_percent threshold (inclusive). Products at
            or above this get removed — default 90 (i.e. "90%+ off").
        dry_run: if True, only counts/logs what would be deleted without
            actually deleting anything. Useful to sanity-check first.

    Returns:
        Number of products deleted (or that would be deleted, if dry_run).
    """
    products = (
        db.query(Product)
        .filter(Product.discount_percent >= min_discount)
        .all()
    )

    if not products:
        print(f"No products found with discount >= {min_discount}%")
        return 0

    product_ids = [p.id for p in products]

    if dry_run:
        for p in products:
            print(f"[dry run] Would remove: {p.platform}/{p.external_id} "
                  f"'{p.product_name}' ({p.discount_percent}% off)")
        return len(products)

    (
        db.query(DealsQueue)
        .filter(DealsQueue.product_id.in_(product_ids))
        .delete(synchronize_session=False)
    )
    (
        db.query(PriceHistory)
        .filter(PriceHistory.product_id.in_(product_ids))
        .delete(synchronize_session=False)
    )
    (
        db.query(Product)
        .filter(Product.id.in_(product_ids))
        .delete(synchronize_session=False)
    )

    db.commit()

    print(f"Removed {len(products)} product(s) with discount >= {min_discount}%")
    return len(products)