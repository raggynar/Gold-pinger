# Free XAUUSD Telegram Bot — hourly direction, first target, signal + RED news

Runs in the cloud 24/7 on GitHub Actions (free). **Your PC can be off. Cost: $0.**

Every hour the bot sends: **fundamental direction (BULLISH/BEARISH/NEUTRAL) + conviction**,
the **exact 1–2 hour window in 🇲🇦 Morocco time**, **where price is expected to go FIRST**
(real level from the data), the follow-through, and a **🧭 EVENT SCENARIO MAP** for the next
USD event: the feed's real **forecast/previous**, then **"if below forecast → gold UP/DOWN"**
and **"if above forecast → gold UP/DOWN"** with the reason. Plus the experimental signal,
flip trigger and **RED NEWS WATCH** list. The same scenario map is included in the
**15-minute pre-event alert** and the morning brief.

Repo: `https://github.com/raggynar/Gold-pinger`

## What you get

1. **Proactive check-ins** — the bot speaks first, in 🇲🇦 Morocco time:
   - 🌅 `07:30` morning brief — gold, dollar, yields + today's events
   - 🗽 `14:30` New York open nudge — price move + top headline
   - 🌙 `21:00` evening wrap — session range + what I'm watching overnight
2. **Hourly report** (at :07 past each hour) — pretty, emoji-formatted, actionable:
   - 🟢/🔴/🟡 Fundamental direction + conviction bar
   - 🕒 **Forward window** (next 1–2h, 🇲🇦 Morocco time + current session)
   - 🎯 **Where price goes FIRST** — one concrete level taken from real 24h high/low/range data
   - ↳ THEN follow-through · 📌 Why (macro only — Fed, dollar, yields, geopolitics)
   - ⚖️ Against · 📅 Nearest RED catalyst
   - 📊 **SMART CONTEXT** — hourly move vs ATR (momentum burst / range edge / no edge) + position in range
   - 🧭 **EVENT SCENARIO MAP** — forecast/previous + "if below / if above / if in line" for gold
   - ⚗️ Experimental signal (zone / invalidation / target) · 🔁 Flip trigger
   - 🚨 RED NEWS WATCH · ⚠️ Active alerts · 🗓 High + **Medium** impact USD events with GMT **and 🇲🇦 Morocco** time
3. **Two separate RED channels** — the watcher checks every ~90 seconds and pings you when:
   - 🗓 **CALENDAR** — a new 🔴 HIGH-impact ForexFactory event appears (CPI, NFP, Fed, etc.)
   - 📰 **BREAKING NEWS** — a new headline drops (war, attacks, **Trump / Fed / dollar statements**, tariffs) — Google News, 24/7, **each with a gold + dollar impact line** (BULLISH/BEARISH/NEUTRAL, confidence, why)
   - ❌ **No gold pump/dump price alerts** — removed as spam; no price-move alerts at all
   - An event is **~15 minutes away** — ⏰ pre-event heads-up **with the full above/below scenario map**
4. **Commands** — message your bot (menu also shows in Telegram):

| Command | What it does |
|---|---|
| `/gold` | Full hourly direction + first move + signal |
| `/path` | Direction + where price goes first |
| `/signal` | Experimental signal only |
| `/event` | Next USD event with above/below-forecast scenarios |
| `/smart` | Range/ATR context + local break levels |
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

## Market hours — no weekend spam

Hourly direction reports are **suppressed when XAUUSD is closed** (Friday 17:00 New York →
Sunday 18:00 New York, DST handled automatically). You get **one** "market closed" notice and
**one** "reopened" notice, then silence until trading resumes. RED news alerts still fire on
weekends because geopolitical headlines can move the Sunday open. Manual commands still work
but are labeled "levels are last close".

## Smart watch (new)

- 📊 **SMART CONTEXT** in every hourly report: hourly move measured in ATRs, whether it's a
  momentum burst (don't fade blindly), at a range edge (break or rejection), or no edge (wait).
- 🚀 **Instant LOCAL BREAK alerts**: when gold clears the tracked local high/low by ~0.5 ATR,
  you get the break size in ATRs, the 24h reference level, an extension target (price ± 1 ATR),
  and the retest level that would confirm. Max one alert per hour to avoid spam.

## Honest limits (free tier)

- The ⚗️ **signal is experimental** — derived only from the supplied data and **not verified**
  on your funded account. Earlier M5 backtests found no statistically proven edge, so treat it
  as a watch/forward-test aid, **not** a "buy here" command.
- "Where price goes first" is a directional call from real 24h high/low/range data, not a guarantee.
- 🧭 Event scenarios are **conditional "if/when" maps** from the standard rates/dollar reaction function,
  using the feed's real forecast. They describe the typical move, not a certain outcome — gold can
  react opposite to the headline if positioning is stretched.
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
