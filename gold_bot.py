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
    r"jobless", r"claims", r"unemployment", r"retail sales", r"\bgdp\b",
    r"\bism\b", r"\bpmi\b", r"confidence", r"personal income", r"durable goods",
]
RED_RE = re.compile("|".join(RED_PATTERNS), re.IGNORECASE)

DEFAULT_STATE = {
    "last_update_id": 0,
    "seen_headlines": [],
    "last_price": None,
    "alerts_enabled": True,
    "commands_set": False,
    "last_watch": None,
    "last_report": None,
    "alerted_events": [],
    "sent_checkins": [],
    "checkins_intro_sent": False,
}

BOT_COMMANDS = [
    {"command": "gold", "description": "Full hourly direction + signal"},
    {"command": "path", "description": "Direction + where price goes first"},
    {"command": "signal", "description": "Experimental signal only"},
    {"command": "alerts", "description": "Show alert settings"},
    {"command": "on", "description": "Turn RED alerts ON"},
    {"command": "off", "description": "Turn RED alerts OFF"},
    {"command": "status", "description": "Bot status"},
    {"command": "help", "description": "All commands"},
]


def log(*args):
    print("[gold-bot]", *args, flush=True)


MA_ZONE = None
try:
    from zoneinfo import ZoneInfo as _ZoneInfo
    MA_ZONE = _ZoneInfo("Africa/Casablanca")
except Exception:
    pass


def ma_time(fmt="%d %b %Y · %H:%M"):
    """Current time in Morocco (Africa/Casablanca)."""
    if MA_ZONE:
        return dt.datetime.now(MA_ZONE).strftime(fmt)
    return dt.datetime.utcnow().strftime(fmt) + " (MA unavailable)"


def event_time_ma(date_txt, time_txt):
    """Convert a ForexFactory GMT event time to Morocco time. Returns 'MM-DD HH:MM'."""
    if not MA_ZONE:
        return None
    try:
        day = dt.datetime.strptime(date_txt.strip(), "%m-%d-%Y").date()
        clock = dt.datetime.strptime(time_txt.strip().upper(), "%I:%M%p").time()
        when_gmt = dt.datetime.combine(day, clock, tzinfo=dt.timezone.utc)
        return when_gmt.astimezone(MA_ZONE).strftime("%m-%d %H:%M")
    except Exception:
        return None


def window_line():
    """Concrete forward time window, computed locally (not by the AI)."""
    now = dt.datetime.now(dt.timezone.utc)
    h = now.hour
    if h < 7:
        session = "Asia / early"
    elif h < 12:
        session = "London"
    elif h < 16:
        session = "New York open"
    elif h < 21:
        session = "New York afternoon"
    else:
        session = "Late / Asia overlap"
    end = now + dt.timedelta(hours=2)
    end_ma = end.astimezone(MA_ZONE).strftime("%H:%M") if MA_ZONE else end.strftime("%H:%M")
    now_ma = now.astimezone(MA_ZONE).strftime("%H:%M") if MA_ZONE else now.strftime("%H:%M")
    return (f"Next 1–2 hours · 🇲🇦 {now_ma} → {end_ma} MA "
            f"({session} session)")


def red_headlines(headlines):
    """Only the headlines that actually match a high-impact keyword."""
    return [h for h in (headlines or []) if RED_RE.search(h)]


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


def yahoo_closes(symbol):
    """Lightweight Yahoo chart API — no yfinance/numpy/pandas needed."""
    from urllib.parse import quote
    url = "https://query1.finance.yahoo.com/v8/finance/chart/" + quote(symbol, safe="")
    resp = requests.get(
        url, params={"range": "10d", "interval": "1h"},
        headers={"User-Agent": "Mozilla/5.0"}, timeout=20,
    )
    resp.raise_for_status()
    result = resp.json()["chart"]["result"][0]
    closes = result["indicators"]["quote"][0].get("close", [])
    return [float(c) for c in closes if c is not None]


def fetch_market():
    m = {
        "price": None, "prev": None, "rsi": None, "sma20": None, "sma50": None,
        "low24": None, "high24": None, "range_mid": None, "dist_high": None,
        "dist_low": None, "atr": None, "trend": None,
        "dxy": None, "us10y": None, "source": "",
        "timestamp": dt.datetime.now(dt.timezone.utc).strftime("%d %b %Y · %H:%M UTC"),
    }
    for sym, key in [("GC=F", "gold"), ("DX-Y.NYB", "dxy"), ("^TNX", "us10y")]:
        try:
            closes = yahoo_closes(sym)
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
                m["range_mid"] = round((m["high24"] + m["low24"]) / 2, 2)
                m["dist_high"] = round(m["high24"] - m["price"], 2)
                m["dist_low"] = round(m["price"] - m["low24"], 2)
                moves = [abs(closes[i] - closes[i - 1])
                         for i in range(max(1, len(closes) - 24), len(closes))]
                m["atr"] = round(sum(moves) / len(moves), 2) if moves else None
                m["trend"] = ("above SMA20" if m["sma20"] and m["price"] > m["sma20"]
                              else "below SMA20")
                m["source"] = "Yahoo Finance · GC=F 1h"
            elif key == "dxy":
                m["dxy"] = round(closes[-1], 2)
            else:
                m["us10y"] = round(closes[-1], 2)
        except Exception as exc:
            log("symbol failed", sym, exc)

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
            if (ev.findtext("impact") or "").lower() not in ("high", "medium"):
                continue
            date_txt = (ev.findtext("date") or "").strip()
            time_txt = (ev.findtext("time") or "").strip()
            title = (ev.findtext("title") or "").strip()
            try:
                if dt.datetime.strptime(date_txt, "%m-%d-%Y").date() < today:
                    continue
            except Exception:
                pass
            impact_label = "🔴 HIGH" if (ev.findtext("impact") or "").lower() == "high" else "🟠 MED"
            ma = event_time_ma(date_txt, time_txt)
            when = f"{date_txt} {time_txt} GMT" + (f"  |  🇲🇦 {ma} MA" if ma else "")
            events.append(f"{impact_label} · {when} — {title}")
        events = list(dict.fromkeys(events))[:8]
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
    reds = red_headlines(headlines)
    return "\n".join([
        f"Time: {m['timestamp']} | Morocco: {ma_time()} MA",
        f"Trading window: {window_line()}",
        f"XAUUSD price: {m['price']} ({m['source']})",
        f"Prior hour: {m['prev']} | 24h high: {m['high24']} | 24h low: {m['low24']}",
        f"Range mid: {m['range_mid']} | +to high: {m['dist_high']} | -to low: {m['dist_low']}",
        f"Hourly ATR: {m['atr']} | Trend: {m['trend']}",
        f"RSI(14): {m['rsi']} | SMA20: {m['sma20']} | SMA50: {m['sma50']}",
        f"DXY: {m['dxy']} | US10Y yield: {m['us10y']}",
        "RED headlines: " + (" | ".join(reds) if reds else "none in current data"),
        "All headlines: " + (" | ".join(headlines[:6]) if headlines else "none"),
        "High-impact USD events: " + (" | ".join(events) if events else "none found"),
        "Pre-flagged alerts: " + (", ".join(alerts) if alerts else "none"),
    ])


def ai_struct(block):
    if not GROQ:
        return None
    prompt = f"""You are a professional XAUUSD analyst. Using ONLY the data below —
never invent numbers or levels, never use outside memory — output EXACTLY 9 lines,
plain text, no markdown, no blank lines, with these exact prefixes:

VERDICT: <BULLISH or BEARISH or NEUTRAL — the FUNDAMENTAL direction>
CONVICTION: <integer 1-10>
FIRST: <where price goes FIRST in the given trading window — pick ONE concrete level from the supplied price / 24h high / 24h low / range mid. Format: "Up first toward <level>" or "Down first toward <level>" or "Sideways around <level>">
THEN: <what typically happens after that first move, using ONLY supplied levels and 1 short sentence>
WHY: <1-2 sentences, dominant MACRO drivers only — Fed/rates, DXY/dollar, yields, geopolitics, central-bank demand. NEVER mention RSI, SMA, ATR, or any technical indicator>
AGAINST: <1 sentence on the strongest counter-force>
CATALYST: <the nearest RED event from supplied headlines/calendar with its date/time, or "none in current data">
SIGNAL: <EXPERIMENTAL data-derived watch: "Watch <entry zone from supplied levels> · invalidate below/above <supplied level> · target <supplied level>" OR "No clean signal — stand aside". Never claim certainty>
FLIP: <1-2 concrete conditions from the supplied data that would change the direction>

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
        for key in ["VERDICT", "CONVICTION", "FIRST", "THEN", "WHY", "AGAINST",
                    "CATALYST", "SIGNAL", "FLIP"]:
            match = re.search(rf"^{key}:\s*(.+)$", text, re.MULTILINE | re.IGNORECASE)
            result[key] = match.group(1).strip() if match else ""
        return result if result.get("VERDICT") else None
    except Exception as exc:
        log("Groq call failed", exc)
        return None


def rules_struct(m, events=None):
    price, rsi_val, sma20 = m.get("price"), m.get("rsi"), m.get("sma20")
    hi, lo, mid = m.get("high24"), m.get("low24"), m.get("range_mid")
    if price is None:
        return {"VERDICT": "NEUTRAL", "CONVICTION": "0", "FIRST": "Path withheld.",
                "THEN": "Path withheld.", "WHY": "Data unavailable — verdict withheld.",
                "AGAINST": "No live data.", "CATALYST": "none in current data",
                "SIGNAL": "No clean signal — stand aside.", "FLIP": "Requires live data."}
    below = bool(sma20 and price < sma20)
    verdict = "BEARISH" if below else "BULLISH"
    first = (f"Down first toward {lo}" if below else f"Up first toward {hi}")
    if rsi_val is not None and rsi_val <= 30 and below:
        then = f"Possible bounce off {lo} toward {mid}"
    elif rsi_val is not None and rsi_val >= 70 and not below:
        then = f"Pullback from {hi} toward {mid}"
    else:
        then = f"Reassess at {mid} if the level is reached"
    cat = (events[0] if events else "none in current data")
    return {
        "VERDICT": verdict, "CONVICTION": "5",
        "FIRST": first,
        "THEN": then,
        "WHY": (f"Gold at {price} with DXY {m.get('dxy')} and 10Y yield {m.get('us10y')}% "
                "framing the macro backdrop. Data-only mode — no AI key."),
        "AGAINST": "A sharp counter-trend move can invalidate this quickly.",
        "CATALYST": cat,
        "SIGNAL": (f"EXPERIMENTAL · Watch {(mid if below else lo)}–{(hi if below else mid)} "
                   f"· invalidate {('above' if below else 'below')} {(hi if below else lo)} "
                   f"· target {(lo if below else hi)}"),
        "FLIP": (f"Price closing {('above' if below else 'below')} {sma20} "
                 f"with a {('softer' if below else 'harder')} dollar."),
    }


def ensure_fields(r, m, events=None):
    """Fill any line the AI omitted with a data-derived fallback."""
    base = rules_struct(m, events)
    for key in base:
        if not r.get(key):
            r[key] = base[key]
    return r


def conviction_bar(value):
    try:
        n = max(0, min(10, int(float(value))))
    except Exception:
        n = 5
    return "🔴" * n + "⚪" * (10 - n)


def format_report(r, alerts, calendar, m, headlines=None,
                  title="XAUUSD · HOURLY DIRECTION"):
    def e(x):
        return html.escape(str(x or ""))

    verdict = r.get("VERDICT", "NEUTRAL").upper()
    v_icon = {"BULLISH": "🟢", "BEARISH": "🔴", "NEUTRAL": "🟡"}.get(verdict, "⚪")
    lines = [
        f"🟡 <b>{e(title)}</b> 🟡",
        "",
        f"{v_icon} <b>FUNDAMENTAL DIRECTION: {e(verdict)}</b> · Conviction {e(r.get('CONVICTION'))}/10",
        conviction_bar(r.get("CONVICTION")),
        f"⏱ {e(m['timestamp'])}  ·  🇲🇦 {e(ma_time())} MA",
        f"🕒 <b>WINDOW</b>: {e(window_line())}",
        f"💰 Gold {e(m.get('price'))} · 24h {e(m.get('low24'))}–{e(m.get('high24'))} · {e(m['source'])}",
        "",
        f"🎯 <b>WHERE PRICE GOES FIRST</b>\n{e(r.get('FIRST'))}",
        f"↳ <b>THEN</b>: {e(r.get('THEN'))}",
        "",
        f"📌 <b>WHY (FUNDAMENTAL)</b>\n{e(r.get('WHY'))}",
        "",
        f"⚖️ <b>AGAINST</b>\n{e(r.get('AGAINST'))}",
        "",
        f"📅 <b>NEXT CATALYST</b>\n{e(r.get('CATALYST'))}",
        "",
        f"⚗️ <b>SIGNAL — EXPERIMENTAL</b>\n{e(r.get('SIGNAL'))}",
        "",
        f"🔁 <b>FLIP TRIGGER</b>\n{e(r.get('FLIP'))}",
    ]
    reds = red_headlines(headlines)
    if reds:
        lines += ["", "🚨 <b>RED NEWS WATCH</b>", "\n".join("• " + e(x) for x in reds[:4])]
    else:
        lines += ["", "🚨 <b>RED NEWS WATCH</b>", "• No high-impact headlines in current data"]
    if alerts:
        lines += ["", "⚠️ <b>ACTIVE ALERTS</b>", "\n".join("• " + e(a) for a in alerts)]
    if calendar:
        lines += ["", "🗓 <b>HIGH-IMPACT USD EVENTS</b>", "\n".join("• " + e(c) for c in calendar[:5])]
    lines += [
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        "💬 <b>/gold</b> report · <b>/path</b> call · <b>/signal</b> · <b>/status</b> · <b>/help</b>",
        "<i>Signal is data-derived, not verified — forward-test only. Not financial advice.</i>",
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
    r = ensure_fields(ai_struct(data_block(m, n, c, a)) or {}, m, c)
    return format_report(r, a, c, m, n), m


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


def handle_command(text, chat_id, state, m=None):
    cmd = (text or "").strip().split()[0].lower().split("@")[0]
    if cmd not in ["/gold", "/path", "/signal", "/alerts", "/on", "/off",
                   "/status", "/help", "/start"]:
        return

    if cmd in ["/gold", "/start"]:
        report, _ = build_report()
        send_telegram(report)
        log("responded", cmd)

    elif cmd in ["/path", "/signal"]:
        m = m or fetch_market()
        n = fetch_news()
        c = fetch_calendar()
        r = ensure_fields(ai_struct(data_block(m, n, c, [])) or {}, m, c)
        if cmd == "/path":
            msg = (
                "🎯 <b>XAUUSD · DIRECTION + FIRST MOVE</b>\n\n"
                f"{html.escape(r.get('VERDICT', 'NEUTRAL'))} · "
                f"Conviction {html.escape(r.get('CONVICTION', '5'))}/10\n"
                f"🕒 <b>WINDOW</b>: {html.escape(window_line())}\n\n"
                f"🎯 <b>FIRST</b>: {html.escape(r.get('FIRST'))}\n"
                f"↳ <b>THEN</b>: {html.escape(r.get('THEN'))}\n\n"
                f"📅 <b>CATALYST</b>: {html.escape(r.get('CATALYST'))}\n"
                f"🔁 <b>FLIP</b>: {html.escape(r.get('FLIP'))}\n\n"
                "<i>Analysis only — not financial advice.</i>"
            )
        else:
            msg = (
                "⚗️ <b>XAUUSD · EXPERIMENTAL SIGNAL</b>\n\n"
                f"{html.escape(r.get('SIGNAL'))}\n\n"
                f"🕒 <b>WINDOW</b>: {html.escape(window_line())}\n"
                f"🎯 <b>FIRST</b>: {html.escape(r.get('FIRST'))}\n\n"
                "<i>Data-derived, not verified — forward-test only. "
                "Not financial advice.</i>"
            )
        send_telegram(msg)
        log("responded", cmd)

    elif cmd == "/alerts":
        status = "🟢 ON" if state.get("alerts_enabled") else "🔴 OFF"
        send_telegram(
            "⚙️ <b>ALERT SETTINGS</b>\n\n"
            f"RED news + gold moves: <b>{status}</b>\n"
            "Checks every ~20 seconds.\n\n"
            "Use /on or /off to toggle."
        )

    elif cmd == "/on":
        state["alerts_enabled"] = True
        send_telegram("🟢 <b>ALERTS ON</b> — I'll ping you on new RED news or sharp gold moves.")

    elif cmd == "/off":
        state["alerts_enabled"] = False
        send_telegram("🔴 <b>ALERTS OFF</b> — only hourly reports and manual /gold will send.")

    elif cmd == "/status":
        m = m or fetch_market()
        last = state.get("last_watch") or "never"
        price = m.get("price")
        if MA_ZONE:
            sched_ma = dt.datetime.now(dt.timezone.utc).replace(
                minute=7, second=0, microsecond=0
            ).astimezone(MA_ZONE).strftime("%H:%M")
        else:
            sched_ma = "08:07"
        send_telegram(
            "🤖 <b>BOT STATUS</b>\n\n"
            f"✅ Online · hourly report at :07 UTC / {sched_ma} MA\n"
            f"🕒 Now: 🇲🇦 {ma_time()} MA\n"
            f"👀 Last watch: {html.escape(last)}\n"
            f"💰 Gold: {price}\n"
            f"🔔 Alerts: {'ON' if state.get('alerts_enabled') else 'OFF'}"
        )

    elif cmd == "/help":
        send_telegram(
            "📋 <b>COMMANDS</b>\n\n"
            "/gold — hourly direction + first move + signal\n"
            "/path — direction + where price goes first\n"
            "/signal — experimental signal only\n"
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


def detect_upcoming_events(events, state):
    """Ping ~15 min before a HIGH/MED USD event. ForexFactory feed times are GMT.
    Events look like: '🔴 HIGH · 11-19-2026 1:30pm GMT | 🇲🇦 11-19 13:30 MA — Claims'"""
    now = dt.datetime.now(dt.timezone.utc)
    alerted = set(state.get("alerted_events", []))
    alerts = []
    for ev in events:
        if ev in alerted:
            continue
        match = re.search(r"(\d{2}-\d{2}-\d{4})\s+(\d{1,2}:\d{2}[ap]m)\s+GMT", ev)
        if not match:
            continue
        try:
            day = dt.datetime.strptime(match.group(1), "%m-%d-%Y").date()
            clock = dt.datetime.strptime(match.group(2).upper(), "%I:%M%p").time()
            when = dt.datetime.combine(day, clock, tzinfo=dt.timezone.utc)
        except Exception:
            continue
        minutes = (when - now).total_seconds() / 60
        if 0 <= minutes <= 15:
            clean = html.escape(ev.replace(" GMT", ""))
            alerts.append(
                "⏰ <b>EVENT IN ~15 MIN</b> ⏰\n\n"
                f"{clean}\n\n"
                "Stand by — expect volatility."
            )
            alerted.add(ev)
    state["alerted_events"] = list(alerted)[-40:]
    return alerts


CHECKIN_TIMES = [
    ("morning", 7, 30),   # 🇲🇦 07:30
    ("nyopen", 14, 30),   # 🇲🇦 14:30 (US 9:30am ET)
    ("evening", 21, 0),   # 🇲🇦 21:00
]


def send_scheduled_checkins(state, m, headlines, events):
    """Proactive 'speaks first' messages, once per day each, in Morocco time."""
    now = dt.datetime.now(MA_ZONE) if MA_ZONE else dt.datetime.utcnow()
    sent = set(state.get("sent_checkins", []))
    price = m.get("price")

    if not state.get("checkins_intro_sent"):
        intro = (
            "🤖 <b>I'M HERE — I'LL SPEAK FIRST FROM NOW ON</b>\n\n"
            "🌅 Morning brief · 🇲🇦 <b>07:30</b>\n"
            "🗽 New York open nudge · 🇲🇦 <b>14:30</b>\n"
            "🌙 Evening wrap · 🇲🇦 <b>21:00</b>\n"
            "⏱ Hourly: direction + time window + first target + signal\n"
            "🚨 RED news / gold drops · instantly\n\n"
            "You don't have to ask — I'll come to you. 🟡"
        )
        if send_telegram(intro):
            state["checkins_intro_sent"] = True
            log("sent check-in intro")

    for name, hour, minute in CHECKIN_TIMES:
        key = f"{now:%Y-%m-%d}-{name}"
        if key in sent:
            continue
        sched = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if not (0 <= (now - sched).total_seconds() <= 1800):
            continue

        if name == "morning":
            top = "\n".join("• " + html.escape(e) for e in events[:5])
            text = (
                "🌅 <b>GOOD MORNING</b> 🇲🇦\n\n"
                f"Gold is at <b>{price}</b> · DXY {m.get('dxy')} · 10Y {m.get('us10y')}%\n\n"
                + (f"🗓 <b>ON YOUR RADAR TODAY (MA time)</b>\n{top}\n\n" if top else "")
                + "I'll nudge you at the New York open. Stay sharp. 🟡"
            )
        elif name == "nyopen":
            prev, change = m.get("prev"), None
            if price and prev:
                change = (price - prev) / prev * 100
            text = (
                "🗽 <b>NEW YORK OPEN IS LIVE</b>\n\n"
                f"💰 Gold: <b>{price}</b>"
                + (f" ({change:+.2f}% vs prior hour)" if change is not None else "")
                + f"\n📊 24h range: {m.get('low24')} – {m.get('high24')}\n\n"
                + ("📰 " + html.escape(headlines[0]) + "\n\n" if headlines else "")
                + "I'm watching for red news and sudden drops. 🟡"
            )
        else:
            span = ""
            if m.get("low24") and m.get("high24"):
                span = f"\n📊 Today's range: {m['low24']} – {m['high24']}"
            text = (
                "🌙 <b>EVENING WRAP</b> 🇲🇦\n\n"
                f"Gold closed the session around <b>{price}</b>{span}\n\n"
                + ("📰 " + html.escape(headlines[0]) + "\n\n" if headlines else "")
                + "I'm still on watch overnight — I'll ping you if anything breaks. Sleep easy. 🟡"
            )

        if send_telegram(text):
            sent.add(key)
            log("sent check-in", name)
    state["sent_checkins"] = list(sent)[-30:]


def send_hourly_report(state, force=False):
    """Send the hourly report, deduped so watch + cron can't double-post."""
    now = dt.datetime.now(dt.timezone.utc)
    last = state.get("last_report")
    if not force and last:
        try:
            ago = (now - dt.datetime.fromisoformat(last)).total_seconds()
            if ago < 3000:  # 50 minutes
                log("hourly report skipped - sent", int(ago), "seconds ago")
                return False
        except Exception:
            pass
    report, _ = build_report()
    ok = send_telegram(report)
    state["last_report"] = now.isoformat()
    log("hourly report done. telegram_ok=", ok)
    return ok


def run_report():
    state = load_state()
    send_hourly_report(state)
    save_state(state)


def run_watch():
    state = load_state()
    if not state.get("commands_set"):
        set_bot_commands()
        state["commands_set"] = True

    # Poll commands FIRST so replies are not delayed by market/news fetches.
    updates = get_updates(state.get("last_update_id", 0))
    for update_id, chat_id, text in updates:
        handle_command(text, chat_id, state)
        state["last_update_id"] = max(state.get("last_update_id", 0), update_id + 1)

    m = fetch_market()
    headlines = fetch_news()
    c = fetch_calendar()

    if state.get("alerts_enabled", True):
        for alert in detect_new_red(m, headlines, state):
            send_telegram(alert)
            log("sent RED alert")
        for alert in detect_upcoming_events(c, state):
            send_telegram(alert)
            log("sent EVENT alert")

    # The fast watcher drives the hourly report so no long-running job blocks GitHub.
    if dt.datetime.now(dt.timezone.utc).minute >= 7:
        send_hourly_report(state)

    # Proactive 'speaks first' check-ins (Morocco time).
    send_scheduled_checkins(state, m, headlines, c)

    state["last_watch"] = dt.datetime.now(dt.timezone.utc).strftime("%H:%M UTC") + f" / {ma_time('%H:%M')} MA"
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
