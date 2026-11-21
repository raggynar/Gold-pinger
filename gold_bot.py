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

COMMAND_VERSION = 3  # bump to re-register the Telegram command menu

DEFAULT_STATE = {
    "commands_version": 0,
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
    "market_notices": [],
    "watch_high": None,
    "watch_low": None,
    "last_smart_alert": None,
}

BOT_COMMANDS = [
    {"command": "gold", "description": "Full hourly direction + signal"},
    {"command": "path", "description": "Direction + where price goes first"},
    {"command": "signal", "description": "Experimental signal only"},
    {"command": "event", "description": "Next event: above/below forecast"},
    {"command": "smart", "description": "Range/ATR context + break levels"},
    {"command": "alerts", "description": "Show alert settings"},
    {"command": "on", "description": "Turn RED alerts ON"},
    {"command": "off", "description": "Turn RED alerts OFF"},
    {"command": "status", "description": "Bot status"},
    {"command": "help", "description": "All commands"},
]


def log(*args):
    print("[gold-bot]", *args, flush=True)


MA_ZONE = None
ET_ZONE = None
try:
    from zoneinfo import ZoneInfo as _ZoneInfo
    MA_ZONE = _ZoneInfo("Africa/Casablanca")
    ET_ZONE = _ZoneInfo("America/New_York")  # handles DST for market hours
except Exception:
    pass


def ma_time(fmt="%d %b %Y · %H:%M"):
    """Current time in Morocco (Africa/Casablanca)."""
    if MA_ZONE:
        return dt.datetime.now(MA_ZONE).strftime(fmt)
    return dt.datetime.utcnow().strftime(fmt) + " (MA unavailable)"


def market_status():
    """XAUUSD market hours: Fri 17:00 ET close → Sun 18:00 ET reopen."""
    now_et = (dt.datetime.now(ET_ZONE) if ET_ZONE
              else dt.datetime.utcnow() - dt.timedelta(hours=4))
    wd, hour = now_et.weekday(), now_et.hour + now_et.minute / 60.0
    if wd == 4 and hour >= 17:
        return False, "Closed — NY close / weekend begins"
    if wd == 5:
        return False, "Closed — weekend"
    if wd == 6 and hour < 18:
        return False, "Closed — weekend (reopens Sun 18:00 ET / 🇲🇦 22:00 MA approx)"
    return True, "Open"


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


def event_gmt_datetime(date_txt, time_txt):
    try:
        day = dt.datetime.strptime(date_txt.strip(), "%m-%d-%Y").date()
        clock = dt.datetime.strptime(time_txt.strip().upper(), "%I:%M%p").time()
        return dt.datetime.combine(day, clock, tzinfo=dt.timezone.utc)
    except Exception:
        return None


def fetch_calendar_events():
    """Structured USD High/Medium events, with the feed's real forecast/previous."""
    out = []
    try:
        r = requests.get(
            "https://nfs.faireconomy.media/ff_calendar_thisweek.xml",
            timeout=20, headers={"User-Agent": "Mozilla/5.0"},
        )
        if b"weeklyevents" not in r.content:
            log("calendar feed unavailable - skipping")
            return []
        root = ET.fromstring(r.content)
        for ev in root.findall(".//event"):
            impact = (ev.findtext("impact") or "").strip().lower()
            if (ev.findtext("country") or "").upper() != "USD":
                continue
            if impact not in ("high", "medium"):
                continue
            date_txt = (ev.findtext("date") or "").strip()
            time_txt = (ev.findtext("time") or "").strip()
            title = (ev.findtext("title") or "").strip()
            if not title:
                continue
            when = event_gmt_datetime(date_txt, time_txt)
            if when and when < dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=30):
                continue
            impact_label = "🔴 HIGH" if impact == "high" else "🟠 MED"
            ma = event_time_ma(date_txt, time_txt)
            when_txt = f"{date_txt} {time_txt} GMT" + (f"  |  🇲🇦 {ma} MA" if ma else "")
            out.append({
                "title": title,
                "impact": impact,
                "date": date_txt,
                "time": time_txt,
                "when": when,
                "forecast": (ev.findtext("forecast") or "").strip() or "n/a",
                "previous": (ev.findtext("previous") or "").strip() or "n/a",
                "label": f"{impact_label} · {when_txt} — {title}",
            })
        seen, unique = set(), []
        for e in out:
            key = (e["date"], e["time"], e["title"])
            if key not in seen:
                seen.add(key)
                unique.append(e)
        unique.sort(key=lambda x: x["when"] or dt.datetime.max.replace(tzinfo=dt.timezone.utc))
        return unique[:10]
    except Exception as exc:
        log("calendar fetch failed", exc)
        return []


def fetch_calendar():
    return [e["label"] for e in fetch_calendar_events()]


def event_kind(title):
    t = (title or "").lower()
    if any(k in t for k in ["fomc", "fed chair", "chairman", "rate decision",
                            "federal funds", "fomc minutes"]):
        return "FED"
    if any(k in t for k in ["cpi", "ppi", "pce", "inflation", "rpi",
                            "price index", "cost of living"]):
        return "INFLATION"
    if any(k in t for k in ["unemployment rate", "jobless claims", "unemployment claims",
                            "claimant", "continuing claims", "initial claims"]):
        return "LABOR_LOOSE"
    if any(k in t for k in ["nonfarm", "payroll", "employment change", "adp",
                            "nfp", "jobs"]):
        return "LABOR_TIGHT"
    if any(k in t for k in ["trump", "president speaks"]):
        return "TWO_WAY"
    return "GROWTH"


def scenario_for(ev):
    """Conditional XAUUSD reaction map. Forecast/previous come from the feed only."""
    if not ev:
        return None
    title = ev.get("title", "")
    fc = ev.get("forecast", "n/a")
    prev = ev.get("previous", "n/a")
    kind = event_kind(title)

    if kind == "INFLATION":
        above = "📉 Gold DOWN — hotter prices lift Treasury yields and the dollar, raising the cost of holding gold."
        below = "📈 Gold UP — softer inflation boosts Fed rate-cut bets, pulling yields and the dollar lower."
    elif kind == "LABOR_LOOSE":
        above = "📈 Gold UP — more joblessness is dovish for the Fed; yields/dollar tend to fall."
        below = "📉 Gold DOWN — fewer claims signal a resilient labour market and fewer cuts."
    elif kind == "LABOR_TIGHT":
        above = "📉 Gold DOWN — strong hiring delays Fed cuts; yields/dollar rise."
        below = "📈 Gold UP — weak hiring brings rate cuts closer."
    elif kind == "FED":
        above = "📉 Gold DOWN — hawkish tone lifts yields and the dollar."
        below = "📈 Gold UP — dovish tone weighs on yields and the dollar."
    elif kind == "TWO_WAY":
        above = "⚠️ Two-way — headline-driven; gold can spike either way. Confirm with DXY/yields."
        below = "⚠️ Two-way — headline-driven; do not pre-position without confirmation."
    else:
        above = "📉 Gold DOWN — stronger activity reduces the urgency for Fed cuts."
        below = "📈 Gold UP — weaker activity increases rate-cut odds."

    if kind == "FED":
        hi_label, lo_label = "IF HAWKISH", "IF DOVISH"
    else:
        hi_label, lo_label = f"IF ABOVE {fc}", f"IF BELOW {fc}"

    return {
        "title": title,
        "label": ev.get("label", ""),
        "forecast": fc,
        "previous": prev,
        "kind": kind,
        "above": f"{hi_label} → {above}",
        "below": f"{lo_label} → {below}",
        "inline": "IF IN LINE → limited move; watch DXY and 10Y yield for the real direction.",
    }


def next_event(event_objs, soonest_only=True):
    """Next USD event that has not passed yet."""
    now = dt.datetime.now(dt.timezone.utc)
    future = [e for e in (event_objs or [])
              if e.get("when") is None or e["when"] >= now - dt.timedelta(minutes=5)]
    return future[0] if future else None


def scenario_block(ev):
    s = scenario_for(ev)
    if not s:
        return ""
    lines = [
        f"🧭 <b>EVENT SCENARIO · {html.escape(s['title'])}</b>",
        html.escape(s["label"]),
        f"Forecast: <b>{html.escape(s['forecast'])}</b> · Previous: {html.escape(s['previous'])}",
        s["below"],
        s["above"],
        html.escape(s["inline"]),
        "<i>Typical reaction, not a guarantee — positioning can reverse it.</i>",
    ]
    return "\n".join(lines)


def smart_context(m):
    """Hourly momentum + range position — tells you if fading the move is dangerous."""
    price, prev, hi, lo, atr = (m.get("price"), m.get("prev"), m.get("high24"),
                               m.get("low24"), m.get("atr"))
    if not all([price, prev, hi, lo, atr]) or atr <= 0 or hi <= lo:
        return None
    rng = hi - lo
    move = price - prev
    mult = abs(move) / atr
    pos = (price - lo) / rng * 100
    loc = "near 24h HIGH" if pos > 75 else "near 24h LOW" if pos < 25 else "mid-range"
    if mult >= 1.5:
        tone = f"MOMENTUM BURST {move:+.2f} ({mult:.1f}× ATR) — don't fade it blindly"
    elif pos > 75 or pos < 25:
        tone = f"At range edge ({move:+.2f}) — watch for break or rejection"
    else:
        tone = f"Normal hourly move {move:+.2f} ({mult:.1f}× ATR) — no edge, wait"
    return (f"📊 <b>SMART CONTEXT</b> · {tone}. "
            f"24h range {rng:.2f} ({rng / atr:.1f}× ATR), price {pos:.0f}% of range, {loc}.")


def detect_smart_moves(m, state):
    """Instant alert when gold breaks the recent local high/low by ~0.5 ATR."""
    price, atr = m.get("price"), m.get("atr") or 3.0
    if price is None:
        return []
    hi, lo = state.get("watch_high"), state.get("watch_low")
    if hi is None or lo is None:
        state["watch_high"] = state["watch_low"] = price
        return []
    now = dt.datetime.now(dt.timezone.utc)
    last = state.get("last_smart_alert")
    cooled = True
    if last:
        try:
            cooled = (now - dt.datetime.fromisoformat(last)).total_seconds() >= 3600
        except Exception:
            cooled = True
    thr = max(atr * 0.5, 1.5)
    alerts = []
    if price > hi + thr:
        if cooled:
            alerts.append(
                "🚀 <b>SMART WATCH · LOCAL BREAK</b>\n\n"
                f"Gold cleared local high <b>{hi}</b> → <b>{price}</b> "
                f"(+{price - hi:.2f}, ≈{(price - hi) / atr:.1f}× ATR).\n"
                f"24h high {m.get('high24')} · ATR {atr}\n"
                f"Extension watch <b>{round(price + atr, 2)}</b> · "
                f"Retest of {hi} as support would confirm."
            )
            state["last_smart_alert"] = now.isoformat()
        state["watch_high"] = price
    elif price < lo - thr:
        if cooled:
            alerts.append(
                "📉 <b>SMART WATCH · LOCAL BREAK</b>\n\n"
                f"Gold broke local low <b>{lo}</b> → <b>{price}</b> "
                f"(-{lo - price:.2f}, ≈{(lo - price) / atr:.1f}× ATR).\n"
                f"24h low {m.get('low24')} · ATR {atr}\n"
                f"Extension watch <b>{round(price - atr, 2)}</b> · "
                f"Retest of {lo} as resistance would confirm."
            )
            state["last_smart_alert"] = now.isoformat()
        state["watch_low"] = price
    else:
        state["watch_high"] = max(hi, price)
        state["watch_low"] = min(lo, price)
    return alerts


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


def format_report(r, alerts, calendar, m, headlines=None, event_objs=None,
                  title="XAUUSD · HOURLY DIRECTION"):
    def e(x):
        return html.escape(str(x or ""))

    verdict = r.get("VERDICT", "NEUTRAL").upper()
    v_icon = {"BULLISH": "🟢", "BEARISH": "🔴", "NEUTRAL": "🟡"}.get(verdict, "⚪")
    is_open, why = market_status()
    lines = [
        f"🟡 <b>{e(title)}</b> 🟡",
        "",
    ]
    if not is_open:
        lines += [f"🔒 <b>MARKET CLOSED</b> — {e(why)} · levels below are the last close", ""]
    lines += [
        f"{v_icon} <b>FUNDAMENTAL DIRECTION: {e(verdict)}</b> · Conviction {e(r.get('CONVICTION'))}/10",
        conviction_bar(r.get("CONVICTION")),
        f"⏱ {e(m['timestamp'])}  ·  🇲🇦 {e(ma_time())} MA",
        f"🕒 <b>WINDOW</b>: {e(window_line())}",
        f"💰 Gold {e(m.get('price'))} · 24h {e(m.get('low24'))}–{e(m.get('high24'))} · {e(m['source'])}",
    ]
    ctx = smart_context(m)
    if ctx:
        lines += [ctx]
    lines += [
        "",
        f"🎯 <b>WHERE PRICE GOES FIRST</b>\n{e(r.get('FIRST'))}",
        f"↳ <b>THEN</b>: {e(r.get('THEN'))}",
        "",
        f"📌 <b>WHY (FUNDAMENTAL)</b>\n{e(r.get('WHY'))}",
        "",
        f"⚖️ <b>AGAINST</b>\n{e(r.get('AGAINST'))}",
        "",
        f"📅 <b>NEXT CATALYST</b>\n{e(r.get('CATALYST'))}",
    ]
    block = scenario_block(next_event(event_objs))
    if block:
        lines += ["", block]
    lines += [
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
        "💬 <b>/gold</b> report · <b>/event</b> scenario · <b>/path</b> · <b>/signal</b> · <b>/help</b>",
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
    ev_objs = fetch_calendar_events()
    c = [e["label"] for e in ev_objs]
    a = build_alerts(m, n, c)
    r = ensure_fields(ai_struct(data_block(m, n, c, a)) or {}, m, c)
    return format_report(r, a, c, m, n, ev_objs), m


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
    if cmd not in ["/gold", "/path", "/signal", "/event", "/smart", "/alerts",
                   "/on", "/off", "/status", "/help", "/start"]:
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

    elif cmd == "/event":
        ev_objs = fetch_calendar_events()
        block = scenario_block(next_event(ev_objs))
        msg = ("🗓 <b>XAUUSD · EVENT SCENARIO</b>\n\n" + block
               if block else
               "🗓 <b>XAUUSD · EVENT SCENARIO</b>\n\nNo upcoming USD High/Medium event in the feed.")
        send_telegram(msg)
        log("responded", cmd)

    elif cmd == "/smart":
        m = m or fetch_market()
        ctx = smart_context(m) or "Not enough live data right now."
        msg = (
            "📊 <b>XAUUSD · SMART WATCH</b>\n\n" + ctx + "\n\n"
            f"Local high: <b>{state.get('watch_high') or m.get('price')}</b> · "
            f"Local low: <b>{state.get('watch_low') or m.get('price')}</b>\n"
            "I ping you automatically when gold clears either one by ~0.5 ATR "
            "(max once an hour).\n\n"
            "<i>Data-derived context — not financial advice.</i>"
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
            f"✅ Online · hourly report at :07 UTC / {sched_ma} MA (market hours only)\n"
            f"📈 Market: {'OPEN' if market_status()[0] else 'CLOSED — weekend'}\n"
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
            "/event — next event: if above/below forecast\n"
            "/smart — range/ATR context + break levels\n"
            "/alerts — alert settings\n"
            "/on — turn RED alerts on\n"
            "/off — turn alerts off\n"
            "/status — bot status\n"
            "/help — this list\n\n"
            "Hourly reports + gold alerts run only while the market is open. "
            "RED news pings any time."
        )


def detect_new_red(m, headlines, state, market_open=True):
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
                    + (f"\n\n💰 Gold: {m.get('price')}" if market_open else
                       "\n\n🔒 Market closed — headline only, no price move.")
                )
        state["seen_headlines"] = list(seen)[-30:]

    price, last = m.get("price"), state.get("last_price")
    # Never send price-move alerts while gold isn't trading. Clear the baseline
    # so the first check after reopen can't fire a fake gap alert.
    if not market_open:
        state["last_price"] = None
        return alerts
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


def detect_upcoming_events(events, state, event_objs=None):
    """Ping ~15 min before a HIGH/MED USD event, with the above/below scenario."""
    now = dt.datetime.now(dt.timezone.utc)
    alerted = set(state.get("alerted_events", []))
    alerts = []
    by_label = {o.get("label"): o for o in (event_objs or [])}
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
            sc = scenario_block(by_label.get(ev))
            alerts.append(
                "⏰ <b>EVENT IN ~15 MIN</b> ⏰\n\n"
                f"{clean}\n\n"
                + (sc + "\n\n" if sc else "")
                + "Stand by — expect volatility."
            )
            alerted.add(ev)
    state["alerted_events"] = list(alerted)[-40:]
    return alerts


CHECKIN_TIMES = [
    ("morning", 7, 30),   # 🇲🇦 07:30
    ("nyopen", 14, 30),   # 🇲🇦 14:30 (US 9:30am ET)
    ("evening", 21, 0),   # 🇲🇦 21:00
]


def send_scheduled_checkins(state, m, headlines, events, event_objs=None):
    """Proactive 'speaks first' messages, once per day each, in Morocco time."""
    if not market_status()[0]:
        log("check-ins skipped - market closed")
        return
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
                + (scenario_block(next_event(event_objs)) + "\n\n" if event_objs else "")
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


def send_market_notices(state):
    """One Friday-close notice and one Sunday-reopen notice. No weekend spam."""
    is_open, why = market_status()
    now = dt.datetime.now(dt.timezone.utc)
    day = now.strftime("%Y-%m-%d")
    notices = state.setdefault("market_notices", [])
    if not is_open:
        key = day + "-closed"
        if key not in notices:
            text = (
                "🔒 <b>MARKET CLOSED — WEEKEND</b>\n\n"
                f"{html.escape(why)}\n"
                "No hourly direction while gold isn't trading. "
                "RED news alerts stay on.\n\n"
                "I'll come back with the next report when the market reopens. 🟡"
            )
            if send_telegram(text):
                notices.append(key)
                log("sent market-closed notice")
    else:
        key = day + "-open"
        if key not in notices and now.weekday() in (5, 6):
            text = (
                "🔓 <b>MARKET REOPENED</b>\n\n"
                "Gold is trading again. Hourly direction + event scenarios resume "
                "at the next :07. 🟡"
            )
            if send_telegram(text):
                notices.append(key)
                state["last_price"] = None
                state["watch_high"] = state["watch_low"] = None
                log("sent market-open notice")
    state["market_notices"] = notices[-10:]


def send_hourly_report(state, force=False):
    """Send the hourly report, deduped so watch + cron can't double-post."""
    now = dt.datetime.now(dt.timezone.utc)
    is_open, why = market_status()
    if not is_open and not force:
        log("hourly report skipped - market closed:", why)
        return False
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
    if (not state.get("commands_set")
            or state.get("commands_version") != COMMAND_VERSION):
        set_bot_commands()
        state["commands_set"] = True
        state["commands_version"] = COMMAND_VERSION

    # Poll commands FIRST so replies are not delayed by market/news fetches.
    updates = get_updates(state.get("last_update_id", 0))
    for update_id, chat_id, text in updates:
        handle_command(text, chat_id, state)
        state["last_update_id"] = max(state.get("last_update_id", 0), update_id + 1)

    send_market_notices(state)

    m = fetch_market()
    headlines = fetch_news()
    ev_objs = fetch_calendar_events()
    c = [e["label"] for e in ev_objs]

    is_open, _ = market_status()
    if state.get("alerts_enabled", True):
        for alert in detect_new_red(m, headlines, state, is_open):
            send_telegram(alert)
            log("sent RED alert")
        if is_open:
            for alert in detect_upcoming_events(c, state, ev_objs):
                send_telegram(alert)
                log("sent EVENT alert")
            for alert in detect_smart_moves(m, state):
                send_telegram(alert)
                log("sent SMART WATCH alert")

    # The fast watcher drives the hourly report so no long-running job blocks GitHub.
    if dt.datetime.now(dt.timezone.utc).minute >= 7:
        send_hourly_report(state)

    # Proactive 'speaks first' check-ins (Morocco time).
    send_scheduled_checkins(state, m, headlines, c, ev_objs)

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
