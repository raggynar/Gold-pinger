#!/usr/bin/env python3
"""Free hourly XAUUSD Telegram pinger. No paid APIs.

Data sources (all free, no keys):
  - Yahoo Finance (via yfinance): gold GC=F, DXY, US10Y, RSI/SMA/support/resistance
  - Google News RSS: breaking gold/Fed/CPI/PPI headlines
  - ForexFactory public XML: this week's high-impact USD calendar events
  - Groq free tier (optional): AI-written summarized report

Environment variables (set in GitHub Actions -> Settings -> Secrets):
  TELEGRAM_TOKEN   required  bot token from @BotFather
  TELEGRAM_CHAT_ID required  your chat ID
  GROQ_API_KEY     optional  free key from console.groq.com (enables AI write-up)
"""

import os
import datetime as dt
import xml.etree.ElementTree as ET

import requests

TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
CHAT = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
GROQ = os.environ.get("GROQ_API_KEY", "").strip()

RED_WORDS = (
    "cpi", "ppi", "pce", "nfp", "payroll", "nonfarm", "fed", "fomc", "powell",
    "warsh", "rate decision", "hike", "inflation", "gdp", "war", "iran",
    "israel", "ukraine", "russia", "tariff", "sanction", "recession", "bank",
)


def log(*args):
    print("[gold-bot]", *args, flush=True)


def rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0.0) for d in deltas[-period:]]
    losses = [max(-d, 0.0) for d in deltas[-period:]]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    return round(100 - 100 / (1 + avg_gain / avg_loss), 1)


def fetch_market():
    m = {
        "price": None, "prev": None, "rsi": None, "sma20": None, "sma50": None,
        "low24": None, "high24": None, "dxy": None, "us10y": None, "source": "",
        "timestamp": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }
    try:
        import yfinance as yf

        symbols = [("GC=F", "gold"), ("DX-Y.NYB", "dxy"), ("^TNX", "us10y")]
        for sym, key in symbols:
            try:
                df = yf.Ticker(sym).history(period="10d", interval="1h")
                closes = [float(x) for x in df["Close"].dropna().tolist()]
                if not closes:
                    continue
                if key == "gold":
                    m["price"] = round(closes[-1], 2)
                    m["prev"] = round(closes[-2], 2) if len(closes) > 1 else None
                    m["rsi"] = rsi(closes)
                    m["sma20"] = round(sum(closes[-20:]) / min(20, len(closes)), 2)
                    if len(closes) >= 50:
                        m["sma50"] = round(sum(closes[-50:]) / 50, 2)
                    m["low24"] = round(min(closes[-24:]), 2)
                    m["high24"] = round(max(closes[-24:]), 2)
                    m["source"] = "Yahoo Finance GC=F 1h"
                elif key == "dxy":
                    m["dxy"] = round(closes[-1], 2)
                else:
                    m["us10y"] = round(closes[-1], 2)
            except Exception as exc:
                log("symbol failed", sym, exc)
    except Exception as exc:
        log("yfinance unavailable", exc)

    # Fallback Gold price if yfinance failed (free, no key).
    if m["price"] is None:
        try:
            r = requests.get("https://api.gold-api.com/price/XAU", timeout=15)
            if r.ok and "price" in r.json():
                m["price"] = round(float(r.json()["price"]), 2)
                m["source"] = "gold-api.com"
        except Exception as exc:
            log("fallback price failed", exc)
    return m


def fetch_news():
    query = (
        "gold price OR Federal Reserve OR CPI OR PPI OR inflation OR dollar "
        "OR Treasury yields OR central bank OR war when:1d"
    )
    url = (
        "https://news.google.com/rss/search?q="
        + requests.utils.quote(query)
        + "&hl=en-US&gl=US&ceid=US:en"
    )
    headlines = []
    try:
        r = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        root = ET.fromstring(r.content)
        for item in root.findall(".//item")[:10]:
            title = (item.findtext("title") or "").strip()
            if title:
                headlines.append(title)
    except Exception as exc:
        log("news fetch failed", exc)
    return headlines[:8]


def fetch_calendar():
    events = []
    try:
        r = requests.get(
            "https://nfs.faireconomy.media/ff_calendar_thisweek.xml",
            timeout=20,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        # The feed sometimes returns an HTML block page instead of XML (rate limiting).
        if b"weeklyevents" not in r.content:
            log("calendar feed unavailable (HTML/blocked) - skipping calendar")
            return []
        root = ET.fromstring(r.content)
        today = dt.date.today()
        for ev in root.findall(".//event"):
            country = (ev.findtext("country") or "").upper()
            impact = (ev.findtext("impact") or "").lower()
            if country != "USD" or impact != "high":
                continue
            date_txt = (ev.findtext("date") or "").strip()
            time_txt = (ev.findtext("time") or "").strip()
            title = (ev.findtext("title") or "").strip()
            try:
                day = dt.datetime.strptime(date_txt, "%m-%d-%Y").date()
                if day < today:
                    continue
            except Exception:
                pass
            events.append(f"{date_txt} {time_txt} {title}")
        events = list(dict.fromkeys(events))[:5]
    except Exception as exc:
        log("calendar fetch failed", exc)
    return events


def build_alerts(m, headlines, events):
    flags = []
    if m.get("price") is not None:
        if m.get("prev"):
            move = (m["price"] - m["prev"]) / m["prev"] * 100
            if abs(move) >= 0.8:
                flags.append(f"Price {move:+.2f}% vs prior hour")
        if m.get("rsi") is not None:
            if m["rsi"] <= 30:
                flags.append(f"RSI oversold {m['rsi']}")
            elif m["rsi"] >= 70:
                flags.append(f"RSI overbought {m['rsi']}")
        if m.get("high24") and m["price"] >= m["high24"]:
            flags.append("At 24h high")
        if m.get("low24") and m["price"] <= m["low24"]:
            flags.append("At 24h low")
    for h in headlines:
        if any(word in h.lower() for word in RED_WORDS):
            flags.append("High-impact news keyword")
            break
    if events:
        flags.append("High-impact USD event this week")
    return list(dict.fromkeys(flags))


def data_block(m, headlines, events, alerts):
    return "\n".join(
        [
            f"Time: {m['timestamp']}",
            f"XAUUSD price: {m['price']} ({m['source']})",
            f"Prior hour: {m['prev']} | 24h high: {m['high24']} | 24h low: {m['low24']}",
            f"RSI(14): {m['rsi']} | SMA20: {m['sma20']} | SMA50: {m['sma50']}",
            f"DXY: {m['dxy']} | US10Y yield: {m['us10y']}",
            "Headlines: " + (" | ".join(headlines) if headlines else "none"),
            "High-impact USD events: " + (" | ".join(events) if events else "none found"),
            "Pre-flagged alerts: " + (", ".join(alerts) if alerts else "none"),
        ]
    )


def ai_report(block):
    if not GROQ:
        return None
    prompt = f"""You are a professional XAUUSD analyst writing a SHORT Telegram message.

Using ONLY the data below (do not invent numbers or levels, do not use outside memory),
write MAX 150 words, plain text, this exact compact structure:

XAUUSD: BULLISH/BEARISH/NEUTRAL - Conviction X/10 | Swing
Why: 1-2 sentences with the supplied numbers.
Against: 1 sentence on the strongest counter-force.
Path: Up first / Down first / One-way, using ONLY supplied levels, with a confidence tag.
Next catalyst: from the supplied calendar/headlines only; if none, say "none in current data".
Flip trigger: 1-2 concrete conditions.

End with: Analysis only - not financial advice. Manage risk.

DATA:
{block}
"""
    try:
        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {GROQ}",
                "Content-Type": "application/json",
            },
            json={
                "model": "openai/gpt-oss-120b",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.2,
                "max_tokens": 3000,
                "reasoning_effort": "low",
            },
            timeout=60,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()
    except Exception as exc:
        log("Groq call failed", exc)
        return None


def rules_report(m):
    """Data-only fallback when no AI key is configured."""
    price = m.get("price")
    rsi_val = m.get("rsi")
    sma20 = m.get("sma20")
    if price is None:
        return "XAUUSD: data unavailable - verdict withheld. Analysis only - not financial advice."
    trend = "below" if (sma20 and price < sma20) else "above"
    verdict = "BEARISH" if trend == "below" else "BULLISH"
    if rsi_val is not None and rsi_val <= 30 and trend == "below":
        path = "Down first, then a possible oversold bounce (low confidence)"
    elif rsi_val is not None and rsi_val >= 70:
        path = "Up first, then a pullback (low confidence)"
    else:
        path = "One-way while price holds " + trend + " SMA20 (low confidence)"
    return (
        f"XAUUSD: {verdict} - Conviction 5/10 | Swing (data-only, no AI key)\n"
        f"Why: Price {price} is {trend} SMA20 {sma20}; RSI(14) {rsi_val}; "
        f"DXY {m.get('dxy')}; US10Y {m.get('us10y')}.\n"
        f"Against: Oversold readings can trigger sharp counter-trend bounces.\n"
        f"Path: {path}.\n"
        f"Next catalyst: see calendar line below.\n"
        f"Flip trigger: close back above SMA20 with RSI recovering.\n"
        "Analysis only - not financial advice. Manage risk."
    )


def send_telegram(text):
    if not TOKEN or not CHAT:
        log("TELEGRAM_TOKEN / TELEGRAM_CHAT_ID not set")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={"chat_id": CHAT, "text": text[:4000], "disable_web_page_preview": True},
            timeout=30,
        )
        if not r.ok:
            log("Telegram error", r.status_code, r.text[:300])
        return r.ok
    except Exception as exc:
        log("Telegram send failed", exc)
        return False


def main():
    log("Starting hourly gold ping", dt.datetime.now(dt.timezone.utc).isoformat())
    m = fetch_market()
    headlines = fetch_news()
    events = fetch_calendar()
    alerts = build_alerts(m, headlines, events)
    block = data_block(m, headlines, events, alerts)

    report = ai_report(block) or rules_report(m)

    parts = []
    if alerts:
        parts.append("ALERTS: " + " | ".join(alerts))
    parts.append(report)
    parts.append("Calendar: " + (" | ".join(events) if events else "none found"))
    parts.append(f"Data: {m['timestamp']} | {m['source'] or 'source unavailable'}")
    message = "\n\n".join(parts)

    ok = send_telegram(message)
    log("Done. telegram_ok=", ok)
    if not ok:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
