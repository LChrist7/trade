# trade

Минимальный сервис для отслеживания пар к USDT на Binance и отправки уведомлений в Telegram при сильных изменениях цены.

## Установка
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Конфигурация
Переменные окружения:
- `PERCENT_THRESHOLD` — порог изменения цены в процентах (по умолчанию `5`).
- `INTERVAL_SECONDS` — интервал опроса в секундах (по умолчанию `300`).
- `TELEGRAM_TOKEN` — токен Telegram-бота.
- `TELEGRAM_CHAT_ID` — ID чата или канала для уведомлений.

Если токен или chat_id не заданы, сервис просто логирует события. Храните токен и chat_id
в переменных окружения или файле `.env`, который не попадает в репозиторий (см. `.env.example`).

Пример файла `.env`:

```env
PERCENT_THRESHOLD=5
INTERVAL_SECONDS=300
TELEGRAM_TOKEN=ваш-токен
TELEGRAM_CHAT_ID=ваш-chat-id
```

## Запуск
```bash
python monitor.py
```

Сервис каждые `INTERVAL_SECONDS` секунд запрашивает 24h-текущие данные по всем парам `*USDT` на Binance, фильтрует пары с абсолютным изменением цены ≥ `PERCENT_THRESHOLD` и отправляет уведомление (или пишет в лог). Повторное уведомление по паре происходит не чаще, чем раз в час.
