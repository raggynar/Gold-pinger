# Free XAUUSD Telegram Bot — hourly reports + RED news alerts

Runs in the cloud 24/7 on GitHub Actions (free). **Your PC can be off. Cost: $0.**

Repo: `https://github.com/raggynar/Gold-pinger`

## What you get

1. **Hourly report** (at :07 past each hour) — pretty, emoji-formatted, summarized:
   - Verdict + conviction bar + horizon
   - 🕒 Times shown in **UTC and Morocco (🇲🇦)** — event times in **GMT (feed native) + Morocco**
   - 📌 Why (macro only — Fed, dollar, yields, geopolitics, central banks — no RSI/SMA jargon)
   - ⚖️ Against · 📈 Path (up-first/down-first with cited levels) · 📅 Next catalyst · 🔁 Flip trigger
   - ⚠️ Active alerts · 🗓 High + **Medium** impact USD events (jobless claims, Philly Fed, PMI, GDP…)
2. **RED news + event alerts** — the watcher checks every ~90 seconds and pings you when:
   - A **new high-impact headline** appears (CPI, PPI, PCE, NFP, Fed, war, tariffs, etc.)
   - **Gold drops ≥0.6%** between checks (or pumps up)
   - An event is **~15 minutes away** (⏰ pre-event heads-up, e.g. Unemployment Claims)
3. **Commands** — message your bot (menu also shows in Telegram):

| Command | What it does |
|---|---|
| `/gold` | Full summarized report right now |
| `/path` | Short-term path call only |
| `/alerts` | Show alert settings |
| `/on` / `/off` | Turn RED alerts on/off |
| `/status` | Bot health + last check + gold price |
| `/help` | Command list |

## Architecture

- `gold_bot.py` — all logic. Modes: `report` (hourly), `watch` (~20s), `setcommands` (one-off).
  Commands are polled before heavy data fetches, and market data uses Yahoo's lightweight
  chart API directly — no yfinance/pandas, so cold starts are ~1s.
- `.github/workflows/watch.yml` — self-chaining watcher; handles commands, alerts **and sends the hourly report**.
- `.github/workflows/hourly.yml` — cron `7 * * * *` as backup only; reports are deduped via `state.json`.
- `state.json` — remembers commands, headlines, price, alert toggle, last report time, alerted events.
- Free data: Yahoo Finance, Google News RSS, ForexFactory XML. Free AI: Groq (`openai/gpt-oss-120b`).
  If Groq fails, a clearly-labeled data-only report is sent instead — never a fake one.

## Honest limits (free tier)

- No premium real-time feed or live tick chart — uses free public data (1h candles).
- GitHub cron can be delayed 5–15 min; scheduled workflows pause after 60 days with no commits
  (make a small commit every ~2 months).
- ForexFactory calendar occasionally rate-limits → calendar line falls back to "none found".
- News headlines guide the RED detection; there is no paid news wire.

## Secrets (already configured in GitHub repo settings)

| Secret | Status |
|---|---|
| `TELEGRAM_TOKEN` | ✅ set |
| `TELEGRAM_CHAT_ID` | ✅ set |
| `GROQ_API_KEY` | ✅ set |

## Security note

Your Telegram token and Groq key were shared in chat. After confirming everything works, rotate them:
- Telegram: message @BotFather → `/mybots` → your bot → API Token → **Revoke** and generate new.
- Groq console: delete the key and create a new one.
Then send me the new values and I'll update the secrets via the API in seconds.

## Troubleshooting

- **No hourly ping?** Actions tab → check the "Gold Watch" log. It also sends the report.
- **`/gold` no reply?** The watch workflow runs every ~20s; allow 40s, then check its log.
- **Too many alerts?** Send `/off`.
- **Change frequency?** Edit the `cron` in the workflow files (GitHub cron is UTC).

**Analysis only — not financial advice. Manage your own risk.**
