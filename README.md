# Free Hourly XAUUSD Telegram Pinger

Runs in the cloud 24/7 (GitHub Actions, free), pings your Telegram **every hour** with a
**summarized** gold report + any active alerts. **Cost: $0.** Your PC can be off.

## What it sends

One compact Telegram message per hour:

- **ALERTS** line (only when triggered): big hourly move, RSI oversold/overbought,
  24h high/low break, high-impact news keyword, high-impact USD calendar event.
- **XAUUSD verdict** — BULLISH/BEARISH/NEUTRAL + conviction + horizon
- **Why / Against / Path / Next catalyst / Flip trigger** (summarized, ~150 words)
- **Calendar** — next high-impact USD events from ForexFactory
- **Data timestamp** — so you know how current it is

## Honest limits (free tier reality)

- No premium real-time feed, no live tick chart. Uses Yahoo Finance 1h data + free news/calendar.
- AI write-up uses **Groq's free tier** (rate-limited). If the key is missing or the call fails,
  it falls back to a data-only snapshot and labels it as such — never fabricates an AI report.
- The free ForexFactory calendar feed occasionally rate-limits and returns a block page; when
  that happens the calendar line quietly falls back to "none found" — news headlines still
  cover breaking catalysts.
- GitHub **cron can be delayed** 5–15 min during busy periods, and scheduled workflows are
  **paused after 60 days with no repo commits** (make a small edit/commit every ~2 months).
- All numbers come from free public sources; treat it as analysis, not a trading signal.

## Setup — the parts only you can do (about 5 minutes)

I can't do these because each needs your identity (Telegram account / email / phone / CAPTCHA).
After step 4, paste me the three values and I'll verify and finish.

### 1. Create the Telegram bot (free)
1. In Telegram, message **@BotFather**.
2. Send `/newbot`, follow prompts, name it (e.g. `GoldDeskBot`).
3. Copy the **bot token** it gives you — that's `TELEGRAM_TOKEN`.

### 2. Get your chat ID (free)
1. Message your new bot any message (e.g. `hi`).
2. Open this URL in a browser, replacing `<TOKEN>` with your bot token:
   `https://api.telegram.org/bot<TOKEN>/getUpdates`
3. Find `"chat":{"id":123456789}` — that number is `TELEGRAM_CHAT_ID`.

### 3. Get a free AI key (optional but recommended)
1. Go to **console.groq.com**, sign up (free, email).
2. API Keys -> Create API Key -> copy it. That's `GROQ_API_KEY`.
   (Free tier is plenty for one call per hour. Skip this if you want zero keys beyond Telegram.)

### 4. Put the code on GitHub (free)
1. Create a **GitHub** account (free) if needed.
2. Create a **new repository** (public is simplest) named e.g. `gold-pinger`.
3. Upload this whole folder's files into it (you can drag-and-drop in the browser, or I can
   give you git commands if you have Git installed).
4. In the repo: **Settings -> Secrets and variables -> Actions -> New repository secret**,
   add each of these one at a time:
   - `TELEGRAM_TOKEN` = your bot token
   - `TELEGRAM_CHAT_ID` = your chat ID
   - `GROQ_API_KEY` = your Groq key (optional)
5. Make sure **Actions** are enabled for the repo (Settings -> Actions -> Allow all actions).
6. The workflow `hourly.yml` is already in `.github/workflows/` — it will start on the next hour.
7. Test instantly: **Actions tab -> Hourly Gold Ping -> Run workflow**. You should get a Telegram ping.

## What I built for you (no work needed)

- `gold_bot.py` — fetches price/technicals (Yahoo), news (Google News RSS),
  high-impact calendar (ForexFactory XML), builds alerts, calls Groq for the summarized
  report (with a data-only fallback), and sends to Telegram.
- `.github/workflows/hourly.yml` — the free hourly cloud scheduler.
- `requirements.txt`, `.env.example` — dependencies and secret template.

## Troubleshooting

- **No ping?** Check Actions tab for a red X and read the log. Most common cause: wrong secret.
- **"AI key failed" in log?** Groq key invalid/rate-limited — the data-only fallback still sends.
- **Want a different schedule?** Edit the `cron` line in `hourly.yml`
  (e.g. `0 */4 * * *` = every 4 hours; cron is UTC).

**Analysis only — not financial advice. Manage your own risk.**
