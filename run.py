"""
Standalone entrypoint: runs the price-tracking loop forever.

Usage:
    python run.py

Drop scraper JSON files into DATA_DIR (default: ./data) and this process
will pick them up on the next cycle, sync them into the DB, and send a
Telegram alert for any 80%+ discount product it sees for the first time
or any tracked product whose price just dropped.

If you'd rather run this behind the FastAPI app (so you also get the
/sync-now and /health endpoints), use `uvicorn Product_loader.app:app`
instead — it starts the same scheduler in a background thread.
"""

from Product_loader.scheduler import run_forever

if __name__ == "__main__":
    run_forever()
