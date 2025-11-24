import asyncio
import importlib.util
import logging
import os
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Set, Tuple

import requests
import pytz
import apscheduler.util as aps_util
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    JobQueue,
    MessageHandler,
    filters,
)

dotenv_spec = importlib.util.find_spec("dotenv")
if dotenv_spec:
    from dotenv import load_dotenv  # type: ignore
else:
    load_dotenv = None  # type: ignore

BINANCE_TICKER_URL = "https://api.binance.com/api/v3/ticker/24hr"
WINDOW_SECONDS = 15 * 60
FETCH_INTERVAL = 5 * 60
DEFAULT_TELEGRAM_TOKEN = "8269111976:AAF6OsENWrVsTXJEftgu_NoTXCPXWTdX5gQ"
DEFAULT_TELEGRAM_CHAT_ID = "1108359014"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

SET_VALUE = 1

SPECIAL_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "SUIUSDT"]


@dataclass
class Thresholds:
    default_threshold: float
    custom_thresholds: Dict[str, float] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    def format_summary(self) -> str:
        lines = [f"По умолчанию для остальных пар: {self.default_threshold:.2f}%"]
        for symbol in SPECIAL_SYMBOLS:
            value = self.custom_thresholds.get(symbol, self.default_threshold)
            lines.append(f"{symbol[:-4]}_USDT: {value:.2f}%")
        return "\n".join(lines)

    async def set_threshold(self, key: str, value: float) -> None:
        async with self._lock:
            if key == "DEFAULT":
                self.default_threshold = value
            else:
                self.custom_thresholds[key] = value

    async def get_threshold(self, symbol: str) -> float:
        async with self._lock:
            return self.custom_thresholds.get(symbol, self.default_threshold)


@dataclass
class AlertDispatcher:
    chat_ids: Set[int] = field(default_factory=set)
    bot: Optional[Application] = None
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    def set_application(self, application: Application) -> None:
        self.bot = application

    async def register_chat(self, chat_id: int) -> None:
        async with self._lock:
            self.chat_ids.add(chat_id)

    async def notify(self, text: str) -> None:
        if not self.bot:
            logging.warning("Bot is not initialized; cannot send notifications")
            return
        async with self._lock:
            targets = list(self.chat_ids)
        if not targets:
            logging.info("No chat IDs registered; skipping Telegram notification")
            return
        for chat_id in targets:
            try:
                await self.bot.bot.send_message(chat_id=chat_id, text=text, disable_web_page_preview=True)
            except Exception as exc:  # pragma: no cover - log only
                logging.warning("Failed to send alert to %s: %s", chat_id, exc)


async def fetch_usdt_pairs() -> List[Dict[str, str]]:
    def _fetch() -> List[Dict[str, str]]:
        response = requests.get(BINANCE_TICKER_URL, timeout=10)
        response.raise_for_status()
        return [item for item in response.json() if item.get("symbol", "").endswith("USDT")]

    return await asyncio.to_thread(_fetch)


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


def enforce_pytz_timezone() -> None:
    """Ensure APScheduler uses a pytz timezone to avoid zoneinfo TypeError."""

    try:
        aps_util.get_localzone = lambda: pytz.UTC  # type: ignore[assignment]
    except Exception as exc:  # pragma: no cover - defensive guard
        logging.warning("Could not enforce pytz timezone: %s", exc)


async def monitor_prices(
    thresholds: Thresholds,
    dispatcher: AlertDispatcher,
    interval: int,
    window: int,
) -> None:
    price_history: Dict[str, Deque[Tuple[float, float]]] = defaultdict(deque)
    last_notified: Dict[str, float] = {}

    while True:
        try:
            tickers = await fetch_usdt_pairs()
        except requests.RequestException as exc:
            logging.warning("Network error: %s", exc)
            await asyncio.sleep(interval)
            continue

        now = time.time()
        for ticker in tickers:
            symbol = ticker.get("symbol")
            if not symbol:
                continue
            try:
                price = float(ticker.get("lastPrice", 0))
            except (TypeError, ValueError):
                continue

            history = price_history[symbol]
            history.append((now, price))
            while history and history[0][0] < now - window:
                history.popleft()

            if not history or history[0][0] >= now:
                continue
            _, baseline_price = history[0]
            if baseline_price <= 0:
                continue
            change_percent = (price - baseline_price) / baseline_price * 100

            threshold = await thresholds.get_threshold(symbol)
            if abs(change_percent) < threshold:
                continue

            previous = last_notified.get(symbol, 0)
            if previous and previous > now - window:
                continue
            last_notified[symbol] = now

            readable_symbol = f"{symbol[:-4]}_USDT"
            message = (
                f"{readable_symbol}: изменение за 15 минут {change_percent:.2f}%\n"
                f"Цена была {baseline_price:.6g}, стала {price:.6g}"
            )
            logging.info(message)
            await dispatcher.notify(message)

        await asyncio.sleep(interval)


def settings_keyboard(current: Thresholds) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(
            f"По умолчанию: {current.default_threshold:.2f}%", callback_data="set:DEFAULT"
        )]
    ]
    for symbol in SPECIAL_SYMBOLS:
        value = current.custom_thresholds.get(symbol, current.default_threshold)
        label = f"{symbol[:-4]}_USDT: {value:.2f}%"
        buttons.append([InlineKeyboardButton(label, callback_data=f"set:{symbol}")])
    return InlineKeyboardMarkup(buttons)


async def start(update: Update, context) -> int:
    assert context.application is not None
    thresholds: Thresholds = context.application.bot_data["thresholds"]
    dispatcher: AlertDispatcher = context.application.bot_data["dispatcher"]

    chat_id = update.effective_chat.id
    await dispatcher.register_chat(chat_id)

    summary = (
        "Бот отслеживает изменение цены за 15 минут.\n"
        "Интервал замера 5 минут.\n"
        "Выберите процент для уведомлений:"
    )
    await update.message.reply_text(
        f"{summary}\n\nТекущие настройки:\n{thresholds.format_summary()}",
        reply_markup=settings_keyboard(thresholds),
    )
    return ConversationHandler.END


async def show_settings(update: Update, context) -> int:
    thresholds: Thresholds = context.application.bot_data["thresholds"]
    dispatcher: AlertDispatcher = context.application.bot_data["dispatcher"]
    if update.effective_chat:
        await dispatcher.register_chat(update.effective_chat.id)
    await update.message.reply_text(
        "Выберите, что изменить:", reply_markup=settings_keyboard(thresholds)
    )
    return ConversationHandler.END


async def handle_selection(update: Update, context) -> int:
    query = update.callback_query
    await query.answer()
    target = query.data.split(":", 1)[1]
    context.user_data["target"] = target
    label = "по умолчанию" if target == "DEFAULT" else f"{target[:-4]}_USDT"
    await query.edit_message_text(
        f"Введите новый порог изменения (%) для {label}. Например: 4.5"
    )
    return SET_VALUE


async def set_value(update: Update, context) -> int:
    thresholds: Thresholds = context.application.bot_data["thresholds"]
    text = update.message.text.strip().replace(",", ".")
    try:
        value = float(text)
    except ValueError:
        await update.message.reply_text("Не могу разобрать число, попробуйте ещё раз.")
        return SET_VALUE

    target = context.user_data.get("target")
    if not target:
        await update.message.reply_text("Не выбрана настройка. Используйте /settings")
        return ConversationHandler.END

    await thresholds.set_threshold(target, value)
    await update.message.reply_text(
        f"Обновлено для {'по умолчанию' if target == 'DEFAULT' else target[:-4] + '_USDT'}: {value:.2f}%",
        reply_markup=settings_keyboard(thresholds),
    )
    return ConversationHandler.END


async def show_thresholds(update: Update, context) -> int:
    thresholds: Thresholds = context.application.bot_data["thresholds"]
    await update.message.reply_text(f"Текущие пороги:\n{thresholds.format_summary()}")
    return ConversationHandler.END


async def main() -> None:
    load_env_file()
    enforce_pytz_timezone()

    telegram_token = os.getenv("TELEGRAM_TOKEN", DEFAULT_TELEGRAM_TOKEN)
    if not telegram_token:
        logging.error("TELEGRAM_TOKEN is not set; bot cannot start")
        return

    default_threshold = get_env_float("DEFAULT_THRESHOLD", 5.0)
    custom_thresholds = {}
    for symbol in SPECIAL_SYMBOLS:
        env_key = f"THRESHOLD_{symbol[:-4]}"
        value = os.getenv(env_key)
        if value:
            try:
                custom_thresholds[symbol] = float(value)
            except ValueError:
                logging.warning("Invalid %s value %s; skipping", env_key, value)

    thresholds = Thresholds(default_threshold=default_threshold, custom_thresholds=custom_thresholds)
    dispatcher = AlertDispatcher()

    job_queue = JobQueue()
    job_queue.scheduler.configure(timezone=pytz.UTC)
    application: Application = ApplicationBuilder().token(telegram_token).job_queue(job_queue).build()
    dispatcher.set_application(application)

    chat_id_env = os.getenv("TELEGRAM_CHAT_ID", DEFAULT_TELEGRAM_CHAT_ID)
    if chat_id_env:
        try:
            dispatcher.chat_ids.add(int(chat_id_env))
        except ValueError:
            logging.warning("Invalid TELEGRAM_CHAT_ID %s", chat_id_env)

    conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(handle_selection, pattern=r"^set:")],
        states={SET_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_value)]},
        fallbacks=[],
        per_chat=True,
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("settings", show_settings))
    application.add_handler(CommandHandler("thresholds", show_thresholds))
    application.add_handler(conv_handler)

    monitor_task = asyncio.create_task(
        monitor_prices(thresholds=thresholds, dispatcher=dispatcher, interval=FETCH_INTERVAL, window=WINDOW_SECONDS)
    )

    await application.initialize()
    await application.start()
    await application.updater.start_polling()

    logging.info("Bot is running. Use /start in Telegram to register your chat.")

    try:
        await monitor_task
    finally:
        await application.updater.stop()
        await application.stop()


if __name__ == "__main__":
    asyncio.run(main())
