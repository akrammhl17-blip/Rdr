#!/usr/bin/env python3
"""Fetch daily prices (Yahoo Finance chart endpoint) and compute rule-based
signals. Writes data/signals.json. Standard library only.

These are mechanical rules, not predictions and not financial advice.
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


# ---------------------------------------------------------------- data
def fetch(symbol):
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/%s"
           "?range=1y&interval=1d" % urllib.parse.quote(symbol))
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.load(r)
    res = data["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    rows = [(c, h, l) for c, h, l in zip(q["close"], q["high"], q["low"])
            if c is not None and h is not None and l is not None]
    closes = [r[0] for r in rows]
    highs = [r[1] for r in rows]
    lows = [r[2] for r in rows]
    currency = res.get("meta", {}).get("currency", "")
    return closes, highs, lows, currency


# ---------------------------------------------------------- indicators
def sma(vals, n):
    return sum(vals[-n:]) / n if len(vals) >= n else None


def rsi(closes, n=14):
    if len(closes) < n + 1:
        return None
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    avg_g, avg_l = gains / n, losses / n
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        avg_g = (avg_g * (n - 1) + max(d, 0)) / n
        avg_l = (avg_l * (n - 1) + max(-d, 0)) / n
    if avg_l == 0:
        return 100.0
    return 100 - 100 / (1 + avg_g / avg_l)


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


# ------------------------------------------------------------- scoring
def analyze(closes, highs, lows):
    """Return a dict with setup, score, reasons and an informational plan."""
    if len(closes) < 200:
        return None
    price = closes[-1]
    s20, s50, s200 = sma(closes, 20), sma(closes, 50), sma(closes, 200)
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


def _r(x):
    return None if x is None else round(x, 2)


# ---------------------------------------------------------------- main
def main():
    watch = json.loads((ROOT / "watchlist.json").read_text(encoding="utf-8"))
    items, errors = [], []
    for w in watch:
        try:
            closes, highs, lows, cur = fetch(w["symbol"])
            res = analyze(closes, highs, lows)
            if res is None:
                errors.append("%s: بيانات غير كافية" % w["symbol"])
            else:
                res.update(symbol=w["symbol"], name=w.get("name", w["symbol"]),
                           group=w.get("group", ""), currency=cur)
                items.append(res)
        except Exception as e:  # keep going; report at the end
            errors.append("%s: %s" % (w["symbol"], e))
        time.sleep(0.6)

    if not items:
        print("no data fetched:", errors, file=sys.stderr)
        sys.exit(1)

    items.sort(key=lambda i: i["score"], reverse=True)
    out = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%d %H:%M UTC"),
        "items": items,
        "errors": errors,
    }
    (ROOT / "data").mkdir(exist_ok=True)
    (ROOT / "data" / "signals.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print("ok: %d items, %d errors" % (len(items), len(errors)))


if __name__ == "__main__":
    main()
