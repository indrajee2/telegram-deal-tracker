"""
flipkart_scraper.py

Playwright-based Flipkart scraper. Give it a list of category/search page
URLs; it scrolls + collects every product card (title, price, MRP,
discount, rating, image, url) straight from the listing page — no
product detail-page visits — and writes results straight to a JSON file
already in the shape Product_loader.product_sync expects — no conversion
step needed.

Setup:
    pip install playwright
    playwright install chromium

Usage:
    python -m Product_loader.flipkart_scraper urls.txt
    # or
    from Product_loader.flipkart_scraper import FlipkartScraper, export_json
    products = FlipkartScraper().scrape_urls(urls)
    export_json(products, "output/flipkart_products.json")
"""

import json
import os
import re
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

from Product_loader import config


class FlipkartScraper:

    # Flipkart uses different card markup on different category pages.
    # We try each of these, in order, and use whichever one actually
    # finds elements on the current page.
    CARD_SELECTORS = [
        "div[data-id]",
        "div._1AtVbE",
        "div._2kHMtA",
        "div._4ddWXP",
        "div._75nlfW",
    ]

    # Possible places the product title/name shows up
    TITLE_SELECTORS = [
        "a[title]",
        "div.KzDlHZ",
        "a.IRpwTa",
        "div._4rR01T",
        "a.wjcEIp",
        "a.s1Q9rs",
    ]

    def __init__(self, discount_threshold: float = None):
        self.products = []
        self.seen_urls = set()  # avoid duplicate products across multiple category links
        # Kept for compatibility with export_json's default threshold
        # lookup; no longer used to pick detail-page targets since all
        # data now comes from the listing/card view.
        self.discount_threshold = (
            discount_threshold
            if discount_threshold is not None
            else getattr(config, "MIN_DISCOUNT_PERCENT", 80)
        )

    # -------------------------
    # Low level helpers
    # -------------------------
    def clean_price(self, value):
        """Convert '1,299' or '₹1,299' or '2,499.00' -> 1299 / 2499 (int).
        Strips currency symbols and thousands separators but keeps the
        decimal point — a plain [^\\d] strip would turn '2,499.00' into
        249900 (100x too large)."""
        if not value:
            return 0
        cleaned = re.sub(r"[^\d.]", "", str(value))
        if not cleaned:
            return 0
        try:
            return int(float(cleaned))
        except ValueError:
            return 0

    def get_text(self, card, selectors):
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
    def extract_title(self, card):
        try:
            title_attr_loc = card.locator("a[title]")
            if title_attr_loc.count() > 0:
                t = title_attr_loc.first.get_attribute("title")
                if t and t.strip():
                    return t.strip()
        except Exception:
            pass

        text = self.get_text(card, self.TITLE_SELECTORS[1:])
        if text:
            return text

        try:
            img_loc = card.locator("img[alt]")
            if img_loc.count() > 0:
                alt = img_loc.first.get_attribute("alt")
                if alt and alt.strip():
                    return alt.strip()
        except Exception:
            pass

        try:
            full_text = card.inner_text().strip()
            if full_text:
                first_line = full_text.split("\n")[0].strip()
                if first_line:
                    return first_line
        except Exception:
            pass

        return ""

    def clean_product_url(self, href):
        """
        Turn a full, tracking-param-heavy Flipkart URL into a short,
        stable link plus a short product-id code (like an ASIN),
        e.g. https://www.flipkart.com/apple-iphone-17-pro.../p/itm106f475c264c7
        -> ("https://www.flipkart.com/.../p/itm106f475c264c7", "itm106f475c264c7")
        """
        if not href:
            return "", ""

        full = href if href.startswith("http") else "https://www.flipkart.com" + href
        clean_url = full.split("?")[0]

        m = re.search(r"/p/(itm[a-zA-Z0-9]+)", clean_url)
        if m:
            product_id = m.group(1)
        else:
            m2 = re.search(r"[?&]pid=([A-Z0-9]+)", full)
            product_id = m2.group(1) if m2 else ""

        return clean_url, product_id

    def extract_url(self, card):
        """
        Prefer an anchor whose href points to an actual product page
        (Flipkart product URLs contain '/p/'). Fall back to the first
        anchor found if no '/p/' link exists. Returns (clean_url, product_id).
        """
        try:
            anchors = card.locator("a")
            count = anchors.count()
            first_href = None
            for i in range(count):
                href = anchors.nth(i).get_attribute("href")
                if not href:
                    continue
                if first_href is None:
                    first_href = href
                if "/p/" in href:
                    return self.clean_product_url(href)
            if first_href:
                return self.clean_product_url(first_href)
        except Exception:
            pass
        return "", ""

    def extract_price_mrp_discount(self, card):
        selling_price = 0
        mrp_price = 0
        discount_percent = 0

        try:
            selling = card.locator("div.hZ3P6w")
            if selling.count() > 0:
                selling_price = self.clean_price(selling.first.inner_text())
        except Exception:
            pass

        try:
            mrp = card.locator("div.kRYCnD")
            if mrp.count() > 0:
                mrp_price = self.clean_price(mrp.first.inner_text())
        except Exception:
            pass

        try:
            discount = card.locator("div.HQe8jr")
            if discount.count() > 0:
                m = re.search(r"(\d+)", discount.first.inner_text())
                if m:
                    discount_percent = int(m.group(1))
        except Exception:
            pass

        if discount_percent == 0 and selling_price and mrp_price:
            discount_percent = round(((mrp_price - selling_price) / mrp_price) * 100)

        return selling_price, mrp_price, discount_percent

    def is_out_of_stock_card(self, card):
        try:
            text = card.inner_text()
            if re.search(r"sold\s*out|out\s*of\s*stock|currently\s*unavailable|coming\s*soon", text, re.I):
                return True
        except Exception:
            pass
        return False

    def extract_image(self, card):
        try:
            img = card.locator("img").first
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

    def extract_rating(self, card):
        """
        Extract rating from Flipkart product cards.
        Supports formats: 4.8(6,508) | 4(2,295) | 4.8 ★ | 4.8 Ratings
        """
        selectors = [
            "div.XQDdHH",
            "div._3LWZlK",
            "span._3LWZlK",
            "div.gUuXy-",
            "div.ipqd2A",
            "span.ipqd2A",
            "[class*='rating']",
        ]

        for selector in selectors:
            try:
                loc = card.locator(selector)
                if loc.count() > 0:
                    txt = loc.first.inner_text().strip()
                    m = re.search(r"([1-5](?:\.\d)?)", txt)
                    if m:
                        return m.group(1)
            except Exception:
                pass

        try:
            text = card.inner_text().replace("\n", " ")
        except Exception:
            return ""

        patterns = [
            r"\b([1-5](?:\.\d)?)\s*\(\s*[\d,]+\s*\)",
            r"\b([1-5](?:\.\d)?)\s*★",
            r"\b([1-5](?:\.\d)?)\s*(?=\d[\d,]*\s*(?:Ratings?|Reviews?))",
            r"Rated\s*([1-5](?:\.\d)?)",
        ]

        for pattern in patterns:
            m = re.search(pattern, text, re.I)
            if m:
                return m.group(1)

        matches = re.findall(r"\b([1-5](?:\.\d)?)\b", text)
        for value in matches:
            try:
                val = float(value)
                if 1 <= val <= 5:
                    return value
            except Exception:
                pass

        matches = re.findall(r"\b([1-5])\s*\(\s*[\d,]+\s*\)", text)
        if matches:
            return matches[0]

        return ""

    def parse_card(self, card):
        title = self.extract_title(card)
        url, product_id = self.extract_url(card)
        selling_price, mrp_price, discount_percent = self.extract_price_mrp_discount(card)
        rating = self.extract_rating(card)
        image_url = self.extract_image(card)

        return {
            "platform": "flipkart",
            "brand": title.split()[0] if title else "",
            "product_name": title,
            "product_id": product_id,
            "selling_price": selling_price,
            "mrp": mrp_price,
            "discount": discount_percent,
            "wow_price": selling_price,
            "rating": float(rating) if rating else 0,
            "bank_offer": "",
            "coupon_offer": "",
            "coupon_discount": 0,
            "cashback_offer": "",
            "supercoin_offer": "",
            "image_url": image_url,
            "url": url,
            "affiliate_url": "",
            "available": True,
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
    # Single page scrape (listing page only - no detail visits here)
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

        previous_height = 0
        max_scrolls = 15
        scrolls = 0
        while scrolls < max_scrolls:
            try:
                page.mouse.wheel(0, 5000)
                page.wait_for_timeout(1000)
                height = page.evaluate("() => document.body.scrollHeight")
                if height == previous_height:
                    break
                previous_height = height
                scrolls += 1
            except Exception:
                break

        print("Scrolling Completed")

        cards, used_selector = self.find_cards(page)
        total = cards.count()
        print(f"Using card selector '{used_selector}' -> Products Found : {total}")

        if total == 0:
            print("[scrape_page] No products found.")
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

                if not product.get("product_name") and not product.get("url") and product.get("selling_price", 0) == 0:
                    skipped_blank += 1
                    continue

                product_url = product.get("url") or ""
                if product_url:
                    if product_url in self.seen_urls:
                        skipped_duplicate += 1
                        continue
                    self.seen_urls.add(product_url)

                self.products.append(product)
                new_count += 1

                print(
                    f"[{i+1}/{total}] "
                    f"{product.get('brand')} | "
                    f"₹{product.get('selling_price')} | "
                    f"MRP ₹{product.get('mrp')} | "
                    f"{product.get('discount')}% | "
                    f"Rating {product.get('rating')}"
                )

            except Exception as e:
                errors += 1
                print(f"[scrape_page] Error on card {i+1}: {e}")

        print(
            f"[scrape_page] "
            f"Added {new_count} | "
            f"Blank {skipped_blank} | "
            f"Duplicate {skipped_duplicate} | "
            f"Out of stock {skipped_out_of_stock} | "
            f"Errors {errors}"
        )

    # -------------------------
    # Public entry points
    # -------------------------
    def scrape(self, url):
        return self.scrape_urls([url])

    def scrape_urls(self, urls):
        """Scrape a list of category/search page URLs and return every
        product card found — no product detail-page visits, all data
        (title, price, MRP, discount, rating, image, url) comes straight
        from the listing/card view."""

        with sync_playwright() as p:
            print("=" * 70)
            print("Launching Browser...")
            print("=" * 70)

            browser = p.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1600, "height": 900})

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


# -------------------------
# Analysis / grouping
# -------------------------
def lowest_price_per_brand(products):
    priced = [p for p in products if p["selling_price"] > 0]
    best_by_brand = {}
    for p in priced:
        brand = p["brand"] or "Unknown"
        if brand not in best_by_brand or p["selling_price"] < best_by_brand[brand]["selling_price"]:
            best_by_brand[brand] = p
    return dict(sorted(best_by_brand.items(), key=lambda kv: kv[1]["selling_price"]))


def top_n_lowest_price(products, n=10):
    priced = [p for p in products if p["selling_price"] > 0]
    return sorted(priced, key=lambda x: x["selling_price"])[:n]


def export_json(products, out_path="output/flipkart_products.json", discount_threshold: float = None):
    """Write products to out_path, keeping only items at/above
    discount_threshold (defaults to config.MIN_DISCOUNT_PERCENT, i.e. 80).
    Written as {"products": [...]} — Product_loader.product_sync accepts
    both a plain list and this wrapped shape, so no conversion is needed."""

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if len(products) == 0:
        print("No Products Found")
        return out_path

    threshold = (
        discount_threshold
        if discount_threshold is not None
        else getattr(config, "MIN_DISCOUNT_PERCENT", 80)
    )

    priced_products = [p for p in products if p["selling_price"] > 0]
    lowest_price = min(priced_products, key=lambda x: x["selling_price"]) if priced_products else None
    highest_discount = max(products, key=lambda x: x["discount"])
    filtered = [p for p in products if p["discount"] >= threshold]

    brand_lowest = lowest_price_per_brand(products)
    top_10_lowest = top_n_lowest_price(products, 10)

    result = {
        "status": True,
        "total_products": len(products),
        "above_threshold_count": len(filtered),
        "discount_threshold": threshold,
        "lowest_price_product": lowest_price,
        "highest_discount_product": highest_discount,
        "products": filtered,
    }

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=4, ensure_ascii=False)

    print("=" * 70)
    print("JSON Saved Successfully ->", out_path)
    print(f"Total scraped: {len(products)} | >= {threshold}% off: {len(filtered)}")
    print(f"Brands found: {len(brand_lowest)}")
    print("Top 10 lowest priced products:")
    for i, p in enumerate(top_10_lowest, 1):
        print(f"  {i}. {p['brand']} | ₹{p['selling_price']} | {p['product_name'][:60]}")
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
    #   python -m Product_loader.flipkart_scraper urls.txt
    #   python -m Product_loader.flipkart_scraper urls.txt output/flipkart_products.json
    url_file = sys.argv[1] if len(sys.argv) > 1 else "flipkart_urls.txt"
    out_path = sys.argv[2] if len(sys.argv) > 2 else "output/flipkart_products.json"

    urls = load_urls_from_file(url_file)
    scraper = FlipkartScraper()
    products = scraper.scrape_urls(urls)
    export_json(products, out_path)
