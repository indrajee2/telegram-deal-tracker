"""
product_search.py

Takes a list of URLs (search/category/listing pages), scrapes each one for
products, then filters the combined results by keyword and optional
price/discount criteria.

Design notes:
- `extract_products_from_page` is the ONLY function you need to customize
  per-site (Flipkart vs Myntra selectors). Everything else is generic.
- `search_and_filter_urls` is the main entry point you asked for.
- Uses requests + BeautifulSoup. Swap in your existing scraper's fetch
  logic (e.g. if you're already using playwright/httpx with headers,
  retries, proxies) inside `fetch_html`.
"""

import json
import re
import sys
import time
import logging
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass, field
from typing import List, Optional, Callable

import requests
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    )
}


@dataclass
class Product:
    """Unified product schema — align this with your existing
    Flipkart/Myntra output schema if it differs."""
    title: str
    price: Optional[float]
    original_price: Optional[float]
    discount_percent: Optional[float]
    url: str
    image_url: Optional[str]
    source_url: str  # which search/listing page this came from
    site: Optional[str] = None
    extra: dict = field(default_factory=dict)


def fetch_html(url: str, timeout: int = 15, retries: int = 2) -> Optional[str]:
    """Fetch raw HTML for a URL with basic retry handling.
    Replace with your existing scraper's session/proxy/retry logic if you
    already have one (e.g. the one used in your Flipkart/Myntra pipeline)."""
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, headers=DEFAULT_HEADERS, timeout=timeout)
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as e:
            logger.warning("Fetch failed (%s/%s) for %s: %s", attempt, retries, url, e)
            time.sleep(1.5 * attempt)
    logger.error("Giving up on %s after %s attempts", url, retries)
    return None


def _parse_price(text: str) -> Optional[float]:
    """Extract a numeric price from messy text like '₹1,299' or 'Rs. 999.00'."""
    if not text:
        return None
    match = re.search(r"[\d,]+(?:\.\d+)?", text.replace(",", ""))
    return float(match.group()) if match else None


def extract_products_from_page(html: str, source_url: str) -> List[Product]:
    """
    Amazon.in search-results-page extractor. Targets the standard
    '[data-component-type="s-search-result"]' result cards.

    NOTE: Amazon actively blocks datacenter-IP / non-browser requests with
    captchas or empty responses. If `fetch_html` returns HTML that doesn't
    contain these cards, you're likely getting a captcha page — you'll
    need real browser headers, a residential proxy, or a headless
    browser (playwright/selenium) instead of plain `requests`.
    """
    from urllib.parse import urljoin

    soup = BeautifulSoup(html, "html.parser")
    products: List[Product] = []

    cards = soup.select('div[data-component-type="s-search-result"]')

    for card in cards:
        asin = card.get("data-asin") or ""

        title_el = card.select_one("h2 a span") or card.select_one("h2 span")
        link_el = card.select_one("h2 a") or card.select_one("a.a-link-normal.s-no-outline")

        # The MRP span also carries the generic "a-price" class
        # (class="a-price a-text-price"), so a plain "span.a-price"
        # selector can accidentally match the MRP instead of the actual
        # selling price depending on DOM order. Amazon marks each span's
        # real role with data-a-color / data-a-strike — use those instead.
        price_el = (
            card.select_one('span.a-price[data-a-color="base"] > span.a-offscreen')
            or card.select_one("span.a-price:not(.a-text-price) > span.a-offscreen")
        )
        orig_price_el = (
            card.select_one('span.a-price[data-a-strike="true"] > span.a-offscreen')
            or card.select_one("span.a-price.a-text-price > span.a-offscreen")
        )

        img_el = card.select_one("img.s-image")
        rating_el = card.select_one("span.a-icon-alt")
        reviews_el = card.select_one("span[aria-label][class*='a-size-base']")

        if not title_el or not link_el:
            continue

        title = title_el.get_text(strip=True)
        href = link_el.get("href", "")
        if href.startswith("/"):
            href = urljoin(source_url, href)

        price = _parse_price(price_el.get_text()) if price_el else None
        orig_price = _parse_price(orig_price_el.get_text()) if orig_price_el else None
        discount = None
        if price and orig_price and orig_price > 0:
            discount = round((1 - price / orig_price) * 100, 1)

        rating = None
        if rating_el:
            m = re.search(r"[\d.]+", rating_el.get_text())
            rating = float(m.group()) if m else None

        products.append(
            Product(
                title=title,
                price=price,
                original_price=orig_price,
                discount_percent=discount,
                url=href,
                image_url=img_el.get("src") if img_el else None,
                source_url=source_url,
                site="amazon.in",
                extra={
                    "asin": asin,
                    "rating": rating,
                    "reviews_text": reviews_el.get_text(strip=True) if reviews_el else None,
                },
            )
        )

    return products


def dedupe_by_asin(products: List[Product]) -> List[Product]:
    """Amazon pagination can repeat items across pages — dedupe by ASIN."""
    seen = set()
    unique = []
    for p in products:
        asin = p.extra.get("asin")
        key = asin or p.url
        if key in seen:
            continue
        seen.add(key)
        unique.append(p)
    return unique


def scrape_urls(urls: List[str]) -> List[Product]:
    """Fetch + extract products from every URL in the list."""
    all_products: List[Product] = []
    for url in urls:
        logger.info("Scraping %s", url)
        html = fetch_html(url)
        if not html:
            continue
        page_products = extract_products_from_page(html, url)
        logger.info("  -> found %s products", len(page_products))
        all_products.extend(page_products)
    return all_products


def filter_products(
    products: List[Product],
    query: Optional[str] = None,
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
    min_discount: Optional[float] = None,
    predicate: Optional[Callable[[Product], bool]] = None,
) -> List[Product]:
    """
    Filter products by keyword (matched against title, case-insensitive),
    price range, minimum discount, and/or a custom predicate function for
    anything else (e.g. brand match, in-stock check).
    """
    results = products

    if query:
        q = query.lower()
        results = [p for p in results if q in p.title.lower()]

    if min_price is not None:
        results = [p for p in results if p.price is not None and p.price >= min_price]

    if max_price is not None:
        results = [p for p in results if p.price is not None and p.price <= max_price]

    if min_discount is not None:
        results = [
            p for p in results
            if p.discount_percent is not None and p.discount_percent >= min_discount
        ]

    if predicate:
        results = [p for p in results if predicate(p)]

    return results


def search_and_filter_urls(
    urls: List[str],
    query: Optional[str] = None,
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
    min_discount: Optional[float] = None,
    predicate: Optional[Callable[[Product], bool]] = None,
) -> List[Product]:
    """
    Main entry point.

    Args:
        urls: list of search/category/listing page URLs to scrape.
        query: keyword to filter product titles by (case-insensitive substring).
        min_price / max_price: price range filter.
        min_discount: minimum discount percent required.
        predicate: optional custom filter function(Product) -> bool.

    Returns:
        List[Product] matching all provided filters.
    """
    scraped = dedupe_by_asin(scrape_urls(urls))
    filtered = filter_products(
        scraped,
        query=query,
        min_price=min_price,
        max_price=max_price,
        min_discount=min_discount,
        predicate=predicate,
    )
    logger.info("Scraped %s products, %s after filtering", len(scraped), len(filtered))
    return filtered


def load_urls_from_file(path) -> List[str]:
    """Read one URL per line from a text file.
    - Blank lines are skipped.
    - Lines starting with '#' are treated as comments and skipped.
    - Leading/trailing whitespace is stripped.
    """
    path = Path(path)

    if not path.exists():
        raise FileNotFoundError(f"URL file not found: {path.resolve()}")

    urls = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        urls.append(line)

    logger.info("Loaded %s URL(s) from %s", len(urls), path)
    return urls


def product_to_loader_dict(p: "Product") -> dict:
    """Convert this module's scraped Product into the flat dict shape
    Product_loader.product_sync.normalize_entry expects (same shape as
    the sample JSON: platform, brand, product_name, selling_price, mrp,
    discount, rating, image_url, url, coupon, bank_offer)."""

    site = (p.site or "amazon.in").split(".")[0]
    asin = p.extra.get("asin")

    # Amazon search-result links carry long tracking query strings
    # (?dib=...&qid=...&ref=...). Since we already have the ASIN, build
    # the short canonical product URL instead of passing that through.
    if asin:
        domain = p.site or "amazon.in"
        clean_url = f"https://www.{domain}/dp/{asin}"
    else:
        clean_url = p.url

    return {
        "platform": site,
        "brand": None,
        "product_name": p.title,
        "selling_price": p.price,
        "mrp": p.original_price,
        "discount": p.discount_percent,
        "rating": p.extra.get("rating"),
        "image_url": p.image_url,
        "url": clean_url,
        # ASIN is already parsed out during extraction — pass it straight
        # through so normalize_entry doesn't have to re-derive it from url.
        "product_id": asin,
        "coupon": None,
        "bank_offer": None,
    }


def export_products_json(products: List["Product"], out_path) -> Path:
    """Write scraped products to out_path in the loader's JSON schema."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    data = [product_to_loader_dict(p) for p in products]
    out_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    logger.info("Wrote %s product(s) to %s", len(data), out_path)
    return out_path


def scrape_from_url_file(
    url_file,
    out_path,
    min_discount: Optional[float] = 80,
    query: Optional[str] = None,
) -> Path:
    """One-call pipeline: read URLs from url_file, scrape + filter, and
    write the result to out_path in the loader's JSON schema."""

    urls = load_urls_from_file(url_file)

    if not urls:
        logger.warning("No URLs found in %s — nothing to scrape.", url_file)
        return export_products_json([], out_path)

    results = search_and_filter_urls(urls, query=query, min_discount=min_discount)
    return export_products_json(results, out_path)


if __name__ == "__main__":
    # Usage:
    #   python -m Product_loader.master_url_products
    #   python -m Product_loader.master_url_products path/to/urls.txt
    #   python -m Product_loader.master_url_products path/to/urls.txt path/to/output/scraped.json
    #
    # urls.txt: one URL per line, blank lines / lines starting with '#' ignored.
    from Product_loader import config

    url_file = sys.argv[1] if len(sys.argv) > 1 else "urls.txt"

    if len(sys.argv) > 2:
        out_path = sys.argv[2]
    else:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = Path(config.DATA_DIR) / f"scraped_{stamp}.json"

    scrape_from_url_file(
        url_file,
        out_path,
        min_discount=config.MIN_DISCOUNT_PERCENT,
    )


