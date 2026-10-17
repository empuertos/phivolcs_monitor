# phivolcs_monitor

Real-time PHIVOLCS earthquake + tsunami monitor.
Shows **only today's** (PH time) events and sends Telegram alerts for new ones.

## Features
- ✅ Today-only filter (auto-resets at midnight PH)
- ✅ 30-second polling (configurable via `CHECK_INTERVAL_SECONDS`)
- ✅ Telegram alerts for new events
- ✅ CORS-locked to trusted origins
- ✅ Atomic JSON writes — website never reads a half-written file
- ✅ Works with `python phivolcs_monitor.py` **and** gunicorn

## Environment Variables
| Name | Purpose |
|------|---------|
| `TELEGRAM_BOT_TOKEN` | Telegram bot token |
| `TELEGRAM_CHAT_ID` | Telegram chat/group ID |
| `ACCESS_KEY` | Secret key for `/earthquakes.json` and `/tsunami.json` |
| `CHECK_INTERVAL_SECONDS` | Poll interval (default `30`) |
| `PORT` | HTTP port (default `10000`) |

## Run locally
```bash
pip install -r requirements.txt
python phivolcs_monitor.py