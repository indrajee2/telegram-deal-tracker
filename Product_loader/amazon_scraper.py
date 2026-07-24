"""
amazon_scraper.py

Playwright-based Amazon.in scraper, structured the same way as the
existing FlipkartScraper: give it a list of search/category page URLs,
it scrolls + collects every product card on each page, and returns a
list of dicts already in the shape Product_loader.product_sync expects
(platform, brand, product_name, selling_price, mrp, discount, rating,
image_url, url, product_id, coupon, bank_offer) — so the output can be
written straight to a JSON file and synced with no extra conversion step.

Why Playwright instead of requests+BeautifulSoup (like the older
master_url_products.py):
Amazon actively blocks plain `requests` calls from datacenter IPs with
503s / captchas. A real browser (what Playwright drives) gets much
further, same as your Flipkart scraper already does.

Setup:
    pip install playwright
    playwright install chromium

Usage:
    from Product_loader.amazon_scraper import AmazonScraper, export_json

    urls = [...]
    scraper = AmazonScraper()
    products = scraper.scrape_urls(urls)
    export_json(products, "output/amazon_products.json")

Or from the command line:
    python -m Product_loader.amazon_scraper urls.txt
"""

import json
import os
import re
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright


class AmazonScraper:

    # Amazon.in search-result cards. This is the standard one; if Amazon
    # changes markup, add fallbacks here the same way Flipkart's
    # CARD_SELECTORS list tries several in order.
    CARD_SELECTORS = [
        'div[data-component-type="s-search-result"]',
    ]

    def __init__(self, site: str = "amazon.in"):
        self.site = site
        self.products = []
        self.seen_asins = set()  # avoid duplicate products across pages

    # -------------------------
    # Low level helpers
    # -------------------------
    def clean_price(self, value) -> float:
        """Convert '₹1,299.00' or '1,299' -> 1299.0."""
        if not value:
            return 0.0
        cleaned = re.sub(r"[^\d.]", "", str(value))
        try:
            return float(cleaned) if cleaned else 0.0
        except ValueError:
            return 0.0

    def get_text(self, card, selectors) -> str:
        """Try a list of CSS selectors, return the first non-empty text found."""
        for selector in selectors:
            try:
                locator = card.locator(selector)
                if locator.count() > 0:
                    text = locator.first.inner_text().strip()
                    if text:
                        return text
            except Exception:
                pass
        return ""

    # -------------------------
    # Field extractors
    # -------------------------
    def extract_title(self, card) -> str:
        text = self.get_text(card, ["h2 a span", "h2 span"])
        if text:
            return text

        try:
            img_loc = card.locator("img.s-image")
            if img_loc.count() > 0:
                alt = img_loc.first.get_attribute("alt")
                if alt and alt.strip():
                    return alt.strip()
        except Exception:
            pass

        return ""

    def extract_asin(self, card) -> str:
        try:
            return card.get_attribute("data-asin") or ""
        except Exception:
            return ""

    def extract_url(self, card, asin: str) -> str:
        """Build the short canonical product URL directly from the ASIN
        rather than keeping the long tracking-param-heavy search-result
        href (same fix as master_url_products.product_to_loader_dict)."""
        if asin:
            return f"https://www.{self.site}/dp/{asin}"

        try:
            link = card.locator("h2 a").first
            href = link.get_attribute("href") or ""
            if href.startswith("/"):
                href = f"https://www.{self.site}{href}"
            return href.split("?")[0]
        except Exception:
            return ""

    def extract_price_mrp_discount(self, card):
        """Selling price and MRP each carry a specific Amazon attribute
        (data-a-color="base" for the real price, data-a-strike="true" for
        the crossed-out MRP) rather than relying on DOM order — a plain
        `.a-price` selector can match either one since the MRP span also
        carries the generic "a-price" class."""

        selling_price = 0.0
        mrp_price = 0.0

        try:
            loc = card.locator('span.a-price[data-a-color="base"] > span.a-offscreen')
            if loc.count() == 0:
                loc = card.locator("span.a-price:not(.a-text-price) > span.a-offscreen")
            if loc.count() > 0:
                selling_price = self.clean_price(loc.first.inner_text())
        except Exception:
            pass

        try:
            loc = card.locator('span.a-price[data-a-strike="true"] > span.a-offscreen')
            if loc.count() == 0:
                loc = card.locator("span.a-price.a-text-price > span.a-offscreen")
            if loc.count() > 0:
                mrp_price = self.clean_price(loc.first.inner_text())
        except Exception:
            pass

        discount_percent = 0.0
        if selling_price and mrp_price and mrp_price > 0:
            discount_percent = round((1 - selling_price / mrp_price) * 100, 1)

        return selling_price, mrp_price, discount_percent

    def extract_rating(self, card) -> float:
        try:
            loc = card.locator("span.a-icon-alt")
            if loc.count() > 0:
                m = re.search(r"[\d.]+", loc.first.inner_text())
                if m:
                    return float(m.group())
        except Exception:
            pass
        return 0.0

    def extract_image(self, card) -> str:
        try:
            img = card.locator("img.s-image").first
            for attr in ["src", "data-src", "srcset"]:
                url = img.get_attribute(attr)
                if url:
                    if attr == "srcset":
                        url = url.split(",")[-1].strip().split(" ")[0]
                    if url.startswith("//"):
                        url = "https:" + url
                    return url
        except Exception:
            pass
        return ""

    def is_out_of_stock_card(self, card) -> bool:
        try:
            text = card.inner_text()
            if re.search(r"currently unavailable|out of stock", text, re.I):
                return True
        except Exception:
            pass
        return False

    def parse_card(self, card) -> dict:
        asin = self.extract_asin(card)
        title = self.extract_title(card)
        url = self.extract_url(card, asin)
        selling_price, mrp_price, discount_percent = self.extract_price_mrp_discount(card)
        rating = self.extract_rating(card)
        image_url = self.extract_image(card)

        # Output already matches Product_loader.product_sync.normalize_entry's
        # expected keys directly — no separate conversion step needed.
        return {
            "platform": self.site.split(".")[0],
            "brand": None,
            "product_name": title,
            "product_id": asin,
            "selling_price": selling_price,
            "mrp": mrp_price,
            "discount": discount_percent,
            "rating": rating,
            "image_url": image_url,
            "url": url,
            "coupon": None,
            "bank_offer": None,
        }

    # -------------------------
    # Card discovery
    # -------------------------
    def find_cards(self, page):
        for selector in self.CARD_SELECTORS:
            loc = page.locator(selector)
            count = loc.count()
            print(f"[find_cards] Selector '{selector}' -> {count} matches")
            if count > 0:
                return loc, selector
        return page.locator(self.CARD_SELECTORS[0]), self.CARD_SELECTORS[0]

    # -------------------------
    # Single page scrape
    # -------------------------
    def scrape_page(self, page, url):
        print("=" * 70)
        print(f"Opening: {url}")
        print("=" * 70)

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(2000)
        except Exception as e:
            print(f"[scrape_page] Failed to open page: {e}")
            return

        # Scroll to load lazy content (capped, mirrors the Flipkart scraper).
        previous_height = 0
        max_scrolls = 10
        scrolls = 0
        while scrolls < max_scrolls:
            try:
                page.mouse.wheel(0, 5000)
                page.wait_for_timeout(800)
                height = page.evaluate("() => document.body.scrollHeight")
                if height == previous_height:
                    break
                previous_height = height
                scrolls += 1
            except Exception:
                break

        cards, used_selector = self.find_cards(page)
        total = cards.count()
        print(f"Using card selector '{used_selector}' -> Products Found: {total}")

        if total == 0:
            print("[scrape_page] No products found (possible captcha/block page).")
            return

        new_count = 0
        skipped_blank = 0
        skipped_duplicate = 0
        skipped_out_of_stock = 0
        errors = 0

        for i in range(total):
            try:
                card = cards.nth(i)

                if self.is_out_of_stock_card(card):
                    skipped_out_of_stock += 1
                    continue

                product = self.parse_card(card)

                if not product["product_name"] and not product["product_id"]:
                    skipped_blank += 1
                    continue

                asin = product["product_id"]
                if asin:
                    if asin in self.seen_asins:
                        skipped_duplicate += 1
                        continue
                    self.seen_asins.add(asin)

                self.products.append(product)
                new_count += 1

                print(
                    f"[{i+1}/{total}] "
                    f"₹{product['selling_price']} | "
                    f"MRP ₹{product['mrp']} | "
                    f"{product['discount']}% | "
                    f"Rating {product['rating']} | "
                    f"{product['product_name'][:60]}"
                )

            except Exception as e:
                errors += 1
                print(f"[scrape_page] Error on card {i+1}: {e}")

        print(
            f"[scrape_page] "
            f"Added {new_count} | Blank {skipped_blank} | "
            f"Duplicate {skipped_duplicate} | Out of stock {skipped_out_of_stock} | "
            f"Errors {errors}"
        )

    # -------------------------
    # Public entry points
    # -------------------------
    def scrape(self, url):
        return self.scrape_urls([url])

    def scrape_urls(self, urls):
        """Scrape every URL in the list, return all collected products."""

        with sync_playwright() as p:
            print("=" * 70)
            print("Launching Browser...")
            print("=" * 70)

            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                viewport={"width": 1600, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/125.0.0.0 Safari/537.36"
                ),
            )
            page = context.new_page()

            try:
                for idx, url in enumerate(urls, start=1):
                    url = url.strip()
                    if not url:
                        continue

                    print(f"\n########## URL {idx}/{len(urls)} ##########")
                    try:
                        self.scrape_page(page, url)
                    except Exception as e:
                        print(f"Failed to scrape {url}: {e}")
            finally:
                page.close()
                context.close()
                browser.close()

        print(f"\nTotal products collected across all URLs: {len(self.products)}")
        return self.products


def export_json(products, out_path="output/amazon_products.json"):
    """Write products straight to disk in the loader's expected schema —
    same shape product_sync.normalize_entry already reads, no conversion
    step needed."""

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if not products:
        print("No products found — nothing written.")
        return out_path

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(products, f, indent=4, ensure_ascii=False)

    print("=" * 70)
    print("JSON Saved Successfully ->", out_path)
    print(f"Total products: {len(products)}")
    print("=" * 70)
    return out_path


def load_urls_from_file(path) -> list:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"URL file not found: {path.resolve()}")

    urls = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        urls.append(line)
    return urls


if __name__ == "__main__":
    # Usage:
    #   python -m Product_loader.amazon_scraper urls.txt
    #   python -m Product_loader.amazon_scraper urls.txt output/amazon_products.json
    url_file = sys.argv[1] if len(sys.argv) > 1 else "urls.txt"
    out_path = sys.argv[2] if len(sys.argv) > 2 else "output/amazon_products.json"

    urls = load_urls_from_file(url_file)
    scraper = AmazonScraper()
    products = scraper.scrape_urls(urls)
    export_json(products, out_path)
