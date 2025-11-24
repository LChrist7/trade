import importlib.util
import logging
import os
import time
from typing import Dict, List

import requests

dotenv_spec = importlib.util.find_spec("dotenv")
if dotenv_spec:
    from dotenv import load_dotenv  # type: ignore
else:
    load_dotenv = None  # type: ignore

BINANCE_TICKER_URL = "https://api.binance.com/api/v3/ticker/24hr"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def load_env_file() -> None:
    if load_dotenv is None:
        logging.debug("python-dotenv not installed; skipping .env loading")
        return
    loaded = load_dotenv()
    if loaded:
        logging.debug("Loaded environment from .env")


def get_env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        logging.warning("Invalid value for %s, using default %s", name, default)
        return float(default)


def fetch_usdt_pairs() -> List[Dict[str, str]]:
    response = requests.get(BINANCE_TICKER_URL, timeout=10)
    response.raise_for_status()
    return [item for item in response.json() if item.get("symbol", "").endswith("USDT")]


def detect_movers(tickers: List[Dict[str, str]], threshold: float) -> List[Dict[str, str]]:
    movers = []
    for ticker in tickers:
        try:
            change_percent = float(ticker.get("priceChangePercent", 0))
        except (TypeError, ValueError):
            continue
        if abs(change_percent) >= threshold:
            movers.append({
                "symbol": ticker.get("symbol"),
                "priceChangePercent": change_percent,
                "lastPrice": ticker.get("lastPrice"),
            })
    return movers


def send_telegram_message(token: str, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    response = requests.post(url, json=payload, timeout=10)
    if not response.ok:
        logging.warning("Telegram returned %s: %s", response.status_code, response.text)


def main() -> None:
    load_env_file()
    threshold = get_env_float("PERCENT_THRESHOLD", 5.0)
    interval = int(get_env_float("INTERVAL_SECONDS", 300))
    telegram_token = os.getenv("TELEGRAM_TOKEN")
    telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID")
    notified: Dict[str, float] = {}

    if telegram_token and not telegram_chat_id:
        logging.warning("TELEGRAM_CHAT_ID is not set; alerts will not be sent")
    elif telegram_chat_id and not telegram_token:
        logging.warning("TELEGRAM_TOKEN is not set; alerts will not be sent")
    elif telegram_token and telegram_chat_id:
        logging.info("Telegram alerts enabled for chat_id %s", telegram_chat_id)
    else:
        logging.info("Telegram alerts disabled (no token or chat_id provided)")

    logging.info(
        "Monitoring USDT pairs with threshold %.2f%% every %s seconds", threshold, interval
    )

    while True:
        try:
            tickers = fetch_usdt_pairs()
            movers = detect_movers(tickers, threshold)
        except requests.RequestException as exc:
            logging.warning("Network error: %s", exc)
            time.sleep(interval)
            continue

        for mover in movers:
            symbol = mover["symbol"]
            change = mover["priceChangePercent"]
            last_price = mover["lastPrice"]
            # avoid spamming: notify only if last alert older than 1 hour
            now = time.time()
            if notified.get(symbol, 0) > now - 3600:
                continue
            notified[symbol] = now

            message = f"{symbol}: change {change:.2f}% (last price {last_price})"
            logging.info(message)
            if telegram_token and telegram_chat_id:
                send_telegram_message(telegram_token, telegram_chat_id, message)

        time.sleep(interval)


if __name__ == "__main__":
    main()
