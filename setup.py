#!/usr/bin/env python3
"""
One-time setup helper: installs Python deps and the Playwright Chromium
browser (a separate download from `pip install playwright`).

Usage:
    python setup.py
"""
import subprocess
import sys


def run(cmd):
    print(f"$ {' '.join(cmd)}")
    subprocess.run(cmd, check=True)


if __name__ == "__main__":
    run([sys.executable, "-m", "pip", "install", "-r", "requirements.txt"])
    run([sys.executable, "-m", "playwright", "install", "chromium"])
    print("\nSetup complete. Next: copy .env.example to .env and fill in your")
    print("Telegram bot token/chat id, then edit Product_loader/config.py to")
    print("add your FLIPKART_URLS / AMAZON_URLS lists.")
