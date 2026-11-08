#!/usr/bin/env python3
"""Fetch prices (Yahoo Finance chart endpoint) and compute rule-based signals.

Writes data/signals.json with:
  items    - daily setups for every symbol in watchlist.json
  intraday - 30-minute and 1-hour signals that are "confirmed", meaning the
             daily trend, the 1-hour trend and the candle trigger all agree.

These are mechanical rules, not predictions and not financial advice.
Standard library only.
"""
import datetime
import json
import pathlib
import sys
import time
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
UA = {"User-Agent": "Mozilla/5.0 (compatible; trading-radar/1.0)"}

TF = {
    "60m": {"sec": 3600, "range": "3mo", "label": "ساعة"},
    "30m": {"sec": 1800, "range": "1mo", "label": "30 دقيقة"},
}


# ---------------------------------------------------------------- data
def fetch(symbol, interval="1d", rng="1y"):
    """Return (timestamps, closes, highs, lows, currency)."""
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/%s"
           "?range=%s&interval=%s" % (urllib.parse.quote(symbol), rng, interval))
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.load(r)
    res = data["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    ts = res.get("timestamp") or []
    rows = [(t, c, h, l) for t, c, h, l in zip(ts, q["close"], q["high"], q["low"])
            if None not in (c, h, l)]
    return ([r[0] for r in rows], [r[1] for r in rows],
            [r[2] for r in rows], [r[3] for r in rows],
            res.get("meta", {}).get("currency", ""))


def drop_forming(ts, closes, highs, lows, sec, now):
    """Remove the last candle(s) that have not closed yet."""
    while ts and ts[-1] + sec > now:
        for x in (ts, closes, highs, lows):
            x.pop()


# ---------------------------------------------------------- indicators
def sma(vals, n):
    return sum(vals[-n:]) / n if len(vals) >= n else None


def ema(vals, n):
    if len(vals) < n:
        return None
    k = 2.0 / (n + 1)
    e = sum(vals[:n]) / n
    for v in vals[n:]:
        e = v * k + e * (1 - k)
    return e


def _rsi_val(avg_g, avg_l):
    if avg_l == 0:
        return 100.0
    return 100 - 100 / (1 + avg_g / avg_l)


def rsi_series(closes, n=14):
    out = [None] * len(closes)
    if len(closes) < n + 1:
        return out
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    avg_g, avg_l = gains / n, losses / n
    out[n] = _rsi_val(avg_g, avg_l)
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        avg_g = (avg_g * (n - 1) + max(d, 0)) / n
        avg_l = (avg_l * (n - 1) + max(-d, 0)) / n
        out[i] = _rsi_val(avg_g, avg_l)
    return out


def rsi(closes, n=14):
    s = rsi_series(closes, n)
    return s[-1] if s else None


def atr(highs, lows, closes, n=14):
    if len(closes) < n + 1:
        return None
    trs = []
    for i in range(1, len(closes)):
        trs.append(max(highs[i] - lows[i],
                       abs(highs[i] - closes[i - 1]),
                       abs(lows[i] - closes[i - 1])))
    a = sum(trs[:n]) / n
    for t in trs[n:]:
        a = (a * (n - 1) + t) / n
    return a


def pct(closes, days):
    if len(closes) <= days:
        return None
    return (closes[-1] / closes[-1 - days] - 1) * 100


def _r(x):
    return None if x is None else round(x, 2)


# --------------------------------------------------------- daily model
def analyze(closes, highs, lows):
    """Daily setup: score, label, reasons and an informational plan."""
    if len(closes) < 200:
        return None
    price = closes[-1]
    s50, s200 = sma(closes, 50), sma(closes, 200)
    r = rsi(closes)
    a = atr(highs, lows, closes)
    hi20 = max(highs[-21:-1])
    score, reasons = 0, []

    if price > s200:
        score += 2
        reasons.append("السعر فوق متوسط 200 يوم (اتجاه طويل صاعد)")
    else:
        score -= 2
        reasons.append("السعر تحت متوسط 200 يوم (اتجاه طويل هابط)")
    if price > s50:
        score += 1
        reasons.append("السعر فوق متوسط 50 يوم")
    else:
        score -= 1
        reasons.append("السعر تحت متوسط 50 يوم")
    if s50 > s200:
        score += 1
        reasons.append("متوسط 50 فوق متوسط 200")
    else:
        score -= 1
        reasons.append("متوسط 50 تحت متوسط 200")

    breakout = price > hi20
    if breakout:
        score += 2
        reasons.append("اختراق أعلى قمة في آخر 20 يوماً")

    if r > 75:
        score -= 2
        reasons.append("RSI مرتفع جداً (%.0f): تشبّع شرائي" % r)
    elif 45 <= r <= 65:
        score += 1
        reasons.append("RSI صحي (%.0f)" % r)
    elif r < 40 and price > s200:
        score += 2
        reasons.append("RSI منخفض (%.0f) داخل اتجاه صاعد: تصحيح محتمل" % r)
    else:
        reasons.append("RSI = %.0f" % r)

    if price < s200 and price < s50:
        setup, label = "avoid", "تجنّب (اتجاه هابط)"
    elif r > 75:
        setup, label = "overbought", "تشبّع شرائي: لا تطارد السعر"
    elif breakout and price > s200:
        setup, label = "breakout", "اختراق قمة 20 يوماً"
    elif r < 40 and price > s200:
        setup, label = "pullback", "تصحيح داخل اتجاه صاعد"
    elif price > s200 and price > s50:
        setup, label = "uptrend", "اتجاه صاعد"
    else:
        setup, label = "neutral", "محايد / غير واضح"

    plan = None
    if setup in ("breakout", "pullback", "uptrend") and a:
        plan = {
            "entry": round(price, 2),
            "stop": round(price - 2 * a, 2),
            "target": round(price + 3 * a, 2),
            "risk_pct": round(2 * a / price * 100, 2),
            "rr": 1.5,
        }

    return {
        "price": round(price, 2),
        "chg_1d": _r(pct(closes, 1)),
        "chg_1m": _r(pct(closes, 21)),
        "chg_3m": _r(pct(closes, 63)),
        "rsi": round(r, 1),
        "sma50": round(s50, 2),
        "sma200": round(s200, 2),
        "setup": setup,
        "label": label,
        "score": score,
        "reasons": reasons,
        "plan": plan,
    }


def daily_direction(res):
    """+1 if price is above both daily SMAs, -1 if below both, else 0."""
    p, s50, s200 = res["price"], res["sma50"], res["sma200"]
    if p > s50 and p > s200:
        return 1
    if p < s50 and p < s200:
        return -1
    return 0


# ------------------------------------------------------ intraday model
def analyze_intraday(closes, highs, lows):
    """Trend and trigger on the last CLOSED candle of a 30m / 1h series."""
    if len(closes) < 60:
        return None
    price = closes[-1]
    e20, e50 = ema(closes, 20), ema(closes, 50)
    rs = rsi_series(closes)
    r, rp = rs[-1], rs[-2]
    a = atr(highs, lows, closes)
    hi20, lo20 = max(highs[-21:-1]), min(lows[-21:-1])

    if price > e50 and e20 > e50:
        trend = 1
    elif price < e50 and e20 < e50:
        trend = -1
    else:
        trend = 0

    recent = [x for x in rs[-6:-1] if x is not None]
    trig = None
    if recent and a and a > 0:
        if trend == 1:
            if price > hi20 and r < 75:
                trig = ("buy", "breakout", "اختراق أعلى قمة في 20 شمعة")
            elif min(recent) < 40 and r > rp and price > closes[-2] and price > e20:
                trig = ("buy", "pullback", "ارتداد بعد تصحيح داخل اتجاه صاعد")
        elif trend == -1:
            if price < lo20 and r > 25:
                trig = ("sell", "breakout", "كسر أدنى قاع في 20 شمعة")
            elif max(recent) > 60 and r < rp and price < closes[-2] and price < e20:
                trig = ("sell", "pullback", "ارتداد هابط داخل اتجاه هابط")
    return {"trend": trend, "trigger": trig, "price": price, "rsi": r, "atr": a}


def build_signal(w, tf, cur, intr, daily_dir, h1_trend, bar_end, now):
    """Return a confirmed signal dict, or None if anything disagrees."""
    trig = intr["trigger"]
    if not trig:
        return None
    direction, kind, label = trig
    d = 1 if direction == "buy" else -1
    if daily_dir != d:                      # daily trend must agree
        return None
    if tf == "30m" and h1_trend != d:       # 1h trend must agree for 30m
        return None
    sec = TF[tf]["sec"]
    if bar_end > now or now - bar_end > 2 * sec:   # only fresh candles
        return None

    p, a = intr["price"], intr["atr"]
    word = "صاعد" if d == 1 else "هابط"
    reasons = ["الاتجاه اليومي %s (السعر %s متوسطي 50 و200 يوم)"
               % (word, "فوق" if d == 1 else "تحت")]
    if tf == "30m":
        reasons.append("اتجاه الساعة %s (EMA20 %s EMA50)"
                       % (word, "فوق" if d == 1 else "تحت"))
    reasons.append("اتجاه إطار %s %s" % (TF[tf]["label"], word))
    reasons.append("الإشارة: " + label)
    reasons.append("RSI = %.0f" % intr["rsi"])

    return {
        "symbol": w["symbol"],
        "name": w.get("name", w["symbol"]),
        "group": w.get("group", ""),
        "currency": cur,
        "tf": tf,
        "tf_label": TF[tf]["label"],
        "direction": direction,
        "kind": kind,
        "label": label,
        "price": round(p, 2),
        "rsi": round(intr["rsi"], 1),
        "plan": {
            "entry": round(p, 2),
            "stop": round(p - d * 1.5 * a, 2),
            "target": round(p + d * 2.25 * a, 2),
            "risk_pct": round(1.5 * a / p * 100, 2),
            "rr": 1.5,
        },
        "bar_end_iso": datetime.datetime.fromtimestamp(
            bar_end, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "age_min": int((now - bar_end) / 60),
        "reasons": reasons,
        "confirmed": True,
    }


# ---------------------------------------------------------------- main
def main():
    now = time.time()
    watch = json.loads((ROOT / "watchlist.json").read_text(encoding="utf-8"))
    items, signals, errors = [], [], []

    for w in watch:
        sym = w["symbol"]
        try:
            _, closes, highs, lows, cur = fetch(sym)
            res = analyze(closes, highs, lows)
        except Exception as e:  # keep going; report at the end
            errors.append("%s: %s" % (sym, e))
            time.sleep(0.4)
            continue
        if res is None:
            errors.append("%s: بيانات غير كافية" % sym)
            continue
        res.update(symbol=sym, name=w.get("name", sym),
                   group=w.get("group", ""), currency=cur)
        items.append(res)

        d_dir = daily_direction(res)
        h1_trend = None
        for tf in ("60m", "30m"):          # 1h first: the 30m check needs it
            try:
                time.sleep(0.4)
                ts, c, h, l, _ = fetch(sym, tf, TF[tf]["range"])
                drop_forming(ts, c, h, l, TF[tf]["sec"], now)
                intr = analyze_intraday(c, h, l)
                if intr is None:
                    continue
                if tf == "60m":
                    h1_trend = intr["trend"]
                bar_end = ts[-1] + TF[tf]["sec"]
                sig = build_signal(w, tf, cur, intr, d_dir, h1_trend, bar_end, now)
                if sig:
                    signals.append(sig)
            except Exception as e:
                errors.append("%s %s: %s" % (sym, tf, e))
        time.sleep(0.4)

    if not items:
        print("no data fetched:", errors, file=sys.stderr)
        sys.exit(1)

    items.sort(key=lambda i: i["score"], reverse=True)
    signals.sort(key=lambda s: (s["bar_end_iso"], s["tf"]), reverse=True)
    out = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%d %H:%M UTC"),
        "items": items,
        "intraday": signals,
        "errors": errors,
    }
    (ROOT / "data").mkdir(exist_ok=True)
    (ROOT / "data" / "signals.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print("ok: %d items, %d intraday signals, %d errors"
          % (len(items), len(signals), len(errors)))


if __name__ == "__main__":
    main()
