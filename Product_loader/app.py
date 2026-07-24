import threading

from fastapi import FastAPI
from Product_loader.database import engine, SessionLocal, run_migrations
from Product_loader import models, config
from Product_loader.product_sync import run_sync_dir
from Product_loader.notifier import process_pending_deals
from Product_loader.scheduler import run_forever


models.Base.metadata.create_all(bind=engine)
run_migrations()

app = FastAPI(title="Deal Aggregator - Product Loader")


@app.on_event("startup")
def start_background_scheduler():
    # Runs run_cycle() every CHECK_INTERVAL_MINUTES in a background thread
    # so the API stays responsive while price tracking happens on its own.
    thread = threading.Thread(target=run_forever, daemon=True)
    thread.start()


@app.get("/health")
def health():
    return {"status": "ok", "interval_minutes": config.CHECK_INTERVAL_MINUTES}


@app.post("/sync-now")
def sync_now():
    """Manually trigger a sync + Telegram notify cycle immediately."""
    db = SessionLocal()
    try:
        stats, deal_ids = run_sync_dir(db, config.DATA_DIR)
        sent = process_pending_deals(db, deal_ids=deal_ids)
        return {"sync_stats": stats, "deals_sent": sent}
    finally:
        db.close()


