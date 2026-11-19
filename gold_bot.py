#!/usr/bin/env python3
"""Free hourly + watchdog XAUUSD Telegram bot. No paid APIs.

MODES (run as: python gold_bot.py <mode>):
  report        - full summarized report (hourly workflow)
  watch         - poll Telegram commands + detect NEW red news / gold drops (5-min workflow)
  setcommands   - register the Telegram command menu (one-off)

Secrets (GitHub Actions -> Settings -> Secrets -> Actions):
  TELEGRAM_TOKEN   required
  TELEGRAM_CHAT_ID required
  GROQ_API_KEY     optional (enables AI-written report; data-only fallback otherwise)
"""

import os
import re
import sys
import html
import json
import datetime as dt
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
CHAT = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
GROQ = os.environ.get("GROQ_API_KEY", "").strip()
STATE_FILE = Path(__file__).with_name("state.json")

RED_PATTERNS = [
    r"\bcpi\b", r"\bppi\b", r"\bpce\b", r"\bnfp\b", r"nonfarm", r"payroll",
    r"fomc", r"\bfed\b", r"rate decision", r"\bhike\b", r"\bwar\b", r"\biran\b",
    r"\bisrael\b", r"\bukraine\b", r"\brussia\b", r"\btariff\b", r"\bsanction",
    r"recession", r"geopolit", r"\battack\b", r"\bstrike\b", r"missile",
    r"invasion", r"oil spike", r"bank crisis", r"\binflation\b", r"yield",
]
RED_RE = re.compile("|".join(RED_PATTERNS), re.IGNORECASE)

DEFAULT_STATE = {
    "last_update_id": 0,
    "seen_headlines": [],
    "last_price": None,
    "alerts_enabled": True,
    "commands_set": False,
    "last_watch": None,
}

BOT_COMMANDS = [
    {"command": "gold", "description": "Full XAUUSD report"},
    {"command": "path", "description": "Short-term path call only"},
    {"command": "alerts", "description": "Show alert settings"},
    {"command": "on", "description": "Turn RED alerts ON"},
    {"command": "off", "description": "Turn RED alerts OFF"},
    {"command": "status", "description": "Bot status"},
    {"command": "help", "description": "All commands"},
]


def log(*args):
    print("[gold-bot]", *args, flush=True)


def load_state():
    try:
        if STATE_FILE.exists():
            return {**DEFAULT_STATE, **json.loads(STATE_FILE.read_text("utf-8"))}
    except Exception as exc:
        log("state load failed", exc)
    return dict(DEFAULT_STATE)


def save_state(state):
    try:
        STATE_FILE.write_text(json.dumps(state, indent=2), "utf-8")
    except Exception as exc:
        log("state save failed", exc)


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
        "timestamp": dt.datetime.now(dt.timezone.utc).strftime("%d %b %Y · %H:%M UTC"),
    }
    try:
        import yfinance as yf
        for sym, key in [("GC=F", "gold"), ("DX-Y.NYB", "dxy"), ("^TNX", "us10y")]:
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
                    m["source"] = "Yahoo Finance · GC=F 1h"
                elif key == "dxy":
                    m["dxy"] = round(closes[-1], 2)
                else:
                    m["us10y"] = round(closes[-1], 2)
            except Exception as exc:
                log("symbol failed", sym, exc)
    except Exception as exc:
        log("yfinance unavailable", exc)

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
            timeout=20, headers={"User-Agent": "Mozilla/5.0"},
        )
        if b"weeklyevents" not in r.content:
            log("calendar feed unavailable - skipping")
            return []
        root = ET.fromstring(r.content)
        today = dt.date.today()
        for ev in root.findall(".//event"):
            if (ev.findtext("country") or "").upper() != "USD":
                continue
            if (ev.findtext("impact") or "").lower() != "high":
                continue
            date_txt = (ev.findtext("date") or "").strip()
            time_txt = (ev.findtext("time") or "").strip()
            title = (ev.findtext("title") or "").strip()
            try:
                if dt.datetime.strptime(date_txt, "%m-%d-%Y").date() < today:
                    continue
            except Exception:
                pass
            events.append(f"{date_txt} {time_txt} — {title}")
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
        if RED_RE.search(h):
            flags.append("High-impact news keyword")
            break
    if events:
        flags.append("High-impact USD event this week")
    return list(dict.fromkeys(flags))


def data_block(m, headlines, events, alerts):
    return "\n".join([
        f"Time: {m['timestamp']}",
        f"XAUUSD price: {m['price']} ({m['source']})",
        f"Prior hour: {m['prev']} | 24h high: {m['high24']} | 24h low: {m['low24']}",
        f"RSI(14): {m['rsi']} | SMA20: {m['sma20']} | SMA50: {m['sma50']}",
        f"DXY: {m['dxy']} | US10Y yield: {m['us10y']}",
        "Headlines: " + (" | ".join(headlines) if headlines else "none"),
        "High-impact USD events: " + (" | ".join(events) if events else "none found"),
        "Pre-flagged alerts: " + (", ".join(alerts) if alerts else "none"),
    ])


def ai_struct(block):
    if not GROQ:
        return None
    prompt = f"""You are a professional XAUUSD analyst. Using ONLY the data below —
never invent numbers or levels, never use outside memory — output EXACTLY 7 lines,
plain text, no markdown, no blank lines, with these exact prefixes:

VERDICT: <BULLISH or BEARISH or NEUTRAL>
CONVICTION: <integer 1-10>
WHY: <1-2 sentences on the dominant MACRO drivers only — Fed/rates, DXY/dollar, yields, geopolitics, central-bank demand. NEVER mention RSI, SMA, moving averages, or any technical indicator>
AGAINST: <1 sentence on the strongest counter-force>
PATH: <Up first / Down first / One-way, using ONLY supplied levels, plus confidence>
CATALYST: <event + date from supplied calendar/headlines, or "none in current data">
FLIP: <1-2 concrete conditions that would change the view>

DATA:
{block}
"""
    try:
        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ}", "Content-Type": "application/json"},
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
        text = r.json()["choices"][0]["message"]["content"].strip()
        result = {}
        for key in ["VERDICT", "CONVICTION", "WHY", "AGAINST", "PATH", "CATALYST", "FLIP"]:
            match = re.search(rf"^{key}:\s*(.+)$", text, re.MULTILINE | re.IGNORECASE)
            result[key] = match.group(1).strip() if match else ""
        return result if result.get("VERDICT") else None
    except Exception as exc:
        log("Groq call failed", exc)
        return None


def rules_struct(m):
    price, rsi_val, sma20 = m.get("price"), m.get("rsi"), m.get("sma20")
    if price is None:
        return {"VERDICT": "NEUTRAL", "CONVICTION": "0", "WHY": "Data unavailable — verdict withheld.",
                "AGAINST": "No live data.", "PATH": "Path withheld.",
                "CATALYST": "none in current data", "FLIP": "Requires live data."}
    trend = "below" if (sma20 and price < sma20) else "above"
    verdict = "BEARISH" if trend == "below" else "BULLISH"
    if rsi_val is not None and rsi_val <= 30 and trend == "below":
        path = "Down first, then possible short bounce (low confidence)"
    elif rsi_val is not None and rsi_val >= 70:
        path = "Up first, then pullback (low confidence)"
    else:
        path = f"One-way while gold holds {trend} its recent range (low confidence)"
    return {
        "VERDICT": verdict, "CONVICTION": "5",
        "WHY": f"Gold at {price} with DXY {m.get('dxy')} and 10Y yield {m.get('us10y')}% keeping pressure on the metal. Data-only mode — no AI key.",
        "AGAINST": "Oversold readings can trigger sharp counter-trend bounces.",
        "PATH": path,
        "CATALYST": "none in current data",
        "FLIP": "Gold reclaiming recent highs with a softer dollar.",
    }


def conviction_bar(value):
    try:
        n = max(0, min(10, int(float(value))))
    except Exception:
        n = 5
    return "🔴" * n + "⚪" * (10 - n)


def format_report(r, alerts, calendar, m, title="XAUUSD · FUNDAMENTAL DIRECTION"):
    def e(x):
        return html.escape(str(x or ""))

    verdict = r.get("VERDICT", "NEUTRAL").upper()
    v_icon = {"BULLISH": "🟢", "BEARISH": "🔴", "NEUTRAL": "🟡"}.get(verdict, "⚪")
    lines = [
        f"🟡 <b>{e(title)}</b> 🟡",
        "",
        f"{v_icon} <b>{e(verdict)}</b>  ·  Conviction {e(r.get('CONVICTION'))}/10",
        conviction_bar(r.get("CONVICTION")),
        f"⏱ {e(m['timestamp'])}  ·  {e(m['source'])}",
        "",
        f"📌 <b>WHY</b>\n{e(r.get('WHY'))}",
        "",
        f"⚖️ <b>AGAINST</b>\n{e(r.get('AGAINST'))}",
        "",
        f"📈 <b>PATH</b>\n{e(r.get('PATH'))}",
        "",
        f"📅 <b>NEXT CATALYST</b>\n{e(r.get('CATALYST'))}",
        "",
        f"🔁 <b>FLIP TRIGGER</b>\n{e(r.get('FLIP'))}",
    ]
    if alerts:
        lines += ["", "⚠️ <b>ACTIVE ALERTS</b>", "\n".join("• " + e(a) for a in alerts)]
    if calendar:
        lines += ["", "🗓 <b>HIGH-IMPACT USD EVENTS</b>", "\n".join("• " + e(c) for c in calendar)]
    lines += [
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        "💬 <b>/gold</b> full report · <b>/path</b> path only · <b>/status</b> · <b>/help</b>",
        "<i>Analysis only — not financial advice. Manage risk.</i>",
    ]
    return "\n".join(lines)


def send_telegram(text, parse_mode="HTML"):
    if not TOKEN or not CHAT:
        log("TELEGRAM_TOKEN / TELEGRAM_CHAT_ID not set")
        return False
    try:
        payload = {"chat_id": CHAT, "text": text[:4000], "disable_web_page_preview": True}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        resp = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json=payload, timeout=30,
        )
        if not resp.ok:
            log("Telegram error", resp.status_code, resp.text[:300])
        return resp.ok
    except Exception as exc:
        log("Telegram send failed", exc)
        return False


def build_report():
    m = fetch_market()
    n = fetch_news()
    c = fetch_calendar()
    a = build_alerts(m, n, c)
    r = ai_struct(data_block(m, n, c, a)) or rules_struct(m)
    return format_report(r, a, c, m), m


def set_bot_commands():
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/setMyCommands",
            json={"commands": BOT_COMMANDS}, timeout=30,
        )
        log("setMyCommands", resp.status_code)
        return resp.ok
    except Exception as exc:
        log("setMyCommands failed", exc)
        return False


def get_updates(offset):
    updates = []
    try:
        resp = requests.get(
            f"https://api.telegram.org/bot{TOKEN}/getUpdates",
            params={"offset": offset, "timeout": 5, "allowed_updates": ["message"]},
            timeout=20,
        )
        for u in resp.json().get("result", []):
            msg = u.get("message", {})
            updates.append((u["update_id"], msg.get("chat", {}).get("id"), msg.get("text", "")))
    except Exception as exc:
        log("getUpdates failed", exc)
    return updates


def handle_command(text, chat_id, state, m):
    cmd = (text or "").strip().split()[0].lower().split("@")[0]
    if cmd not in ["/gold", "/path", "/alerts", "/on", "/off", "/status", "/help", "/start"]:
        return

    if cmd in ["/gold", "/start"]:
        report, _ = build_report()
        send_telegram(report)
        log("responded", cmd)

    elif cmd == "/path":
        r = ai_struct(data_block(m, fetch_news(), fetch_calendar(), [])) or rules_struct(m)
        msg = (
            "📈 <b>XAUUSD · SHORT-TERM PATH</b>\n\n"
            f"🔴 <b>VERDICT</b>: {html.escape(r.get('VERDICT', 'NEUTRAL'))} · "
            f"Conviction {html.escape(r.get('CONVICTION', '5'))}/10\n\n"
            f"📈 <b>PATH</b>: {html.escape(r.get('PATH'))}\n\n"
            f"🔁 <b>FLIP</b>: {html.escape(r.get('FLIP'))}\n\n"
            "<i>Analysis only — not financial advice. Manage risk.</i>"
        )
        send_telegram(msg)
        log("responded", cmd)

    elif cmd == "/alerts":
        status = "🟢 ON" if state.get("alerts_enabled") else "🔴 OFF"
        send_telegram(
            "⚙️ <b>ALERT SETTINGS</b>\n\n"
            f"RED news + gold moves: <b>{status}</b>\n"
            "Checks every 5 minutes.\n\n"
            "Use /on or /off to toggle."
        )

    elif cmd == "/on":
        state["alerts_enabled"] = True
        send_telegram("🟢 <b>ALERTS ON</b> — I'll ping you on new RED news or sharp gold moves.")

    elif cmd == "/off":
        state["alerts_enabled"] = False
        send_telegram("🔴 <b>ALERTS OFF</b> — only hourly reports and manual /gold will send.")

    elif cmd == "/status":
        last = state.get("last_watch") or "never"
        price = m.get("price")
        send_telegram(
            "🤖 <b>BOT STATUS</b>\n\n"
            f"✅ Online · hourly report at :07\n"
            f"👀 Last watch: {html.escape(last)}\n"
            f"💰 Gold: {price}\n"
            f"🔔 Alerts: {'ON' if state.get('alerts_enabled') else 'OFF'}"
        )

    elif cmd == "/help":
        send_telegram(
            "📋 <b>COMMANDS</b>\n\n"
            "/gold — full summarized report\n"
            "/path — short-term path call\n"
            "/alerts — alert settings\n"
            "/on — turn RED alerts on\n"
            "/off — turn alerts off\n"
            "/status — bot status\n"
            "/help — this list\n\n"
            "I also ping hourly and on new RED news / gold drops."
        )


def detect_new_red(m, headlines, state):
    alerts = []
    seen = set(state.get("seen_headlines", []))

    if not seen:
        state["seen_headlines"] = list(headlines)[:30]
    else:
        for h in headlines:
            if h in seen:
                continue
            seen.add(h)
            if RED_RE.search(h):
                alerts.append(
                    "🚨 <b>NEW RED NEWS</b> 🚨\n\n" + html.escape(h)
                    + f"\n\n💰 Gold: {m.get('price')}"
                )
        state["seen_headlines"] = list(seen)[-30:]

    price, last = m.get("price"), state.get("last_price")
    if price and last:
        change = (price - last) / last * 100
        if change <= -0.6:
            alerts.append(
                "📉 <b>GOLD DROP</b> 📉\n\n"
                f"Down <b>{change:.2f}%</b> since last check → <b>{price}</b>\n"
                f"DXY: {m.get('dxy')} · 10Y: {m.get('us10y')}"
            )
        elif change >= 0.6:
            alerts.append(
                "🚀 <b>GOLD PUMP</b> 🚀\n\n"
                f"Up <b>+{change:.2f}%</b> since last check → <b>{price}</b>"
            )
    if price:
        state["last_price"] = price
    return alerts


def run_report():
    report, _ = build_report()
    ok = send_telegram(report)
    log("report done. telegram_ok=", ok)
    if not ok:
        raise SystemExit(1)


def run_watch():
    state = load_state()
    if not state.get("commands_set"):
        set_bot_commands()
        state["commands_set"] = True

    m = fetch_market()
    headlines = fetch_news()
    c = fetch_calendar()

    updates = get_updates(state.get("last_update_id", 0))
    for update_id, chat_id, text in updates:
        handle_command(text, chat_id, state, m)
        state["last_update_id"] = max(state.get("last_update_id", 0), update_id + 1)

    if state.get("alerts_enabled", True):
        for alert in detect_new_red(m, headlines, state):
            send_telegram(alert)
            log("sent RED alert")

    state["last_watch"] = dt.datetime.now(dt.timezone.utc).strftime("%H:%M UTC")
    save_state(state)
    log("watch done. last_update_id=", state["last_update_id"])


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "report"
    log("mode", mode)
    if mode == "report":
        run_report()
    elif mode == "watch":
        run_watch()
    elif mode == "setcommands":
        set_bot_commands()
    else:
        raise SystemExit(f"Unknown mode: {mode}")


if __name__ == "__main__":
    main()
