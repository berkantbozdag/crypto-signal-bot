# ============================================================
# 3D EMA + RISING SUPPORT COMBINED SCANNER
# Signals:
# 1) EMA_SUPPORT_HOLD
# 2) DIP_SWEEP_RECLAIM
#
# Binance Spot USDT pairs
# Timeframe: 3D
# ============================================================

import os
import time
import json
import math
import csv
import urllib.parse
import urllib.request
from datetime import datetime, timezone


# =========================
# SETTINGS
# =========================

INTERVAL = "3d"
KLINE_LIMIT = 260

USE_LIVE_CANDLE = True
# True  = devam eden 3D mumu da tarar, erken sinyal verir
# False = sadece kapanmış 3D mumlarla tarar

EMA_LEN = 100

PIVOT_LEFT = 3
PIVOT_RIGHT = 3
PIVOT_LOOKBACK = 180
MAX_PIVOTS_FOR_LINE = 10
MIN_PIVOT_GAP = 10
MIN_PIVOT_RISE_PCT = 0.005

# Coin daha önce pump yapmış mı?
PUMP_LOOKBACK = 180
MIN_PUMP_X = 1.80
MIN_DROP_FROM_PEAK = 0.10

# EMA / trendline yakınlıkları
EMA_TOUCH_TOL = 0.06          # Low EMA'nın %6 yakınına geldiyse temas say
EMA_RECLAIM_TOL = 0.03       # Close EMA'nın %3 altına kadar tolerans
LINE_TOUCH_TOL = 0.06        # Low trendline'ın %6 yakınına geldiyse temas say
LINE_CLOSE_RECLAIM_TOL = 0.006

# Hold ve sweep ayrımı
HOLD_ALLOWED_LINE_BREAK = 0.008
SWEEP_WINDOW = 2
SWEEP_MIN_BREAK = 0.008
SWEEP_CLOSE_BREAK = 0.004

# EMA ile yükselen dip hattı çok uzak olmasın
MAX_EMA_LINE_DIST = 0.25

# Geçmişte hattı çok fazla bozduysa trendline kalitesi düşer
HIST_LINE_BREAK_TOL = 0.10
MAX_HIST_VIOLATIONS = 2

# Hacim tercihin
VOLUME_SOFT_MIN_USDT = 7_000_000
HIDE_LOW_VOLUME = False

MAX_TELEGRAM_RESULTS = 60
RATE_LIMIT_SLEEP = 0.04

CSV_FILE = "combined_ema_support_results.csv"


# =========================
# TELEGRAM SECRETS
# =========================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()


# =========================
# BINANCE ENDPOINTS
# =========================

BASE_URLS = [
    "https://data-api.binance.vision",
    "https://api.binance.com",
]


STABLE_OR_FIAT_BASES = {
    "USDC", "FDUSD", "TUSD", "BUSD", "DAI", "USDP", "PAX", "PYUSD",
    "USDE", "SUSDE", "USDS", "USD1", "AEUR", "EURI",
    "EUR", "TRY", "BRL", "AUD", "GBP", "RUB", "UAH"
}


def http_get_json(path, params=None, timeout=15):
    if params is None:
        params = {}

    query = urllib.parse.urlencode(params)
    last_err = None

    for base in BASE_URLS:
        url = base + path
        if query:
            url += "?" + query

        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 EMA-Rising-Support-Scanner"
                }
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw)
        except Exception as e:
            last_err = e
            time.sleep(0.25)

    raise RuntimeError(f"GET failed: {path} | {last_err}")


def http_post_json(url, data, timeout=15):
    encoded = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=encoded,
        headers={"User-Agent": "Mozilla/5.0 EMA-Rising-Support-Scanner"},
        method="POST"
    )

    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw)


# =========================
# HELPERS
# =========================

def now_utc_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def safe_float(x, default=0.0):
    try:
        return float(x)
    except Exception:
        return default


def fmt_price(x):
    if x is None:
        return "-"
    if x >= 100:
        return f"{x:.2f}"
    if x >= 10:
        return f"{x:.3f}"
    if x >= 1:
        return f"{x:.4f}"
    if x >= 0.1:
        return f"{x:.5f}"
    if x >= 0.01:
        return f"{x:.6f}"
    return f"{x:.8f}"


def fmt_pct(x):
    if x is None:
        return "-"
    return f"{x:+.2f}%"


def fmt_vol(x):
    if x >= 1_000_000_000:
        return f"{x / 1_000_000_000:.2f}B"
    if x >= 1_000_000:
        return f"{x / 1_000_000:.2f}M"
    if x >= 1_000:
        return f"{x / 1_000:.1f}K"
    return f"{x:.0f}"


def symbol_base(symbol):
    if symbol.endswith("USDT"):
        return symbol[:-4]
    return symbol


def is_bad_symbol(symbol):
    base = symbol_base(symbol)

    if base in STABLE_OR_FIAT_BASES:
        return True

    bad_suffixes = ("UP", "DOWN", "BULL", "BEAR")
    if base.endswith(bad_suffixes):
        return True

    return False


def calc_ema(values, length):
    if not values:
        return []

    k = 2.0 / (length + 1.0)
    out = []
    ema = values[0]

    for v in values:
        ema = v * k + ema * (1.0 - k)
        out.append(ema)

    return out


def parse_klines(raw_klines):
    now_ms = int(time.time() * 1000)
    candles = []

    for k in raw_klines:
        close_time = int(k[6])
        candles.append({
            "open_time": int(k[0]),
            "open": safe_float(k[1]),
            "high": safe_float(k[2]),
            "low": safe_float(k[3]),
            "close": safe_float(k[4]),
            "volume": safe_float(k[5]),
            "close_time": close_time,
            "is_live": close_time > now_ms
        })

    if not USE_LIVE_CANDLE and candles and candles[-1]["is_live"]:
        candles = candles[:-1]

    return candles


def get_symbols():
    data = http_get_json("/api/v3/exchangeInfo")

    symbols = []
    for s in data.get("symbols", []):
        symbol = s.get("symbol", "")
        status = s.get("status", "")
        quote = s.get("quoteAsset", "")
        is_spot = s.get("isSpotTradingAllowed", False)

        if status != "TRADING":
            continue
        if quote != "USDT":
            continue
        if not is_spot:
            continue
        if is_bad_symbol(symbol):
            continue

        symbols.append(symbol)

    return sorted(set(symbols))


def get_24h_quote_volumes():
    data = http_get_json("/api/v3/ticker/24hr")
    volumes = {}

    for item in data:
        symbol = item.get("symbol", "")
        if symbol.endswith("USDT"):
            volumes[symbol] = safe_float(item.get("quoteVolume", 0.0))

    return volumes


def get_klines(symbol):
    raw = http_get_json(
        "/api/v3/klines",
        {
            "symbol": symbol,
            "interval": INTERVAL,
            "limit": KLINE_LIMIT
        }
    )
    return parse_klines(raw)


# =========================
# PIVOT / TRENDLINE
# =========================

def find_pivot_lows(candles, left=PIVOT_LEFT, right=PIVOT_RIGHT, lookback=PIVOT_LOOKBACK):
    n = len(candles)
    pivots = []

    start = max(left, n - lookback)
    end = n - right

    for i in range(start, end):
        low_i = candles[i]["low"]

        ok = True
        for j in range(i - left, i + right + 1):
            if j == i:
                continue
            if candles[j]["low"] < low_i:
                ok = False
                break

        if ok:
            pivots.append((i, low_i))

    return pivots


def find_pivot_highs(candles, left=3, right=3, lookback=120):
    n = len(candles)
    pivots = []

    start = max(left, n - lookback)
    end = n - right

    for i in range(start, end):
        high_i = candles[i]["high"]

        ok = True
        for j in range(i - left, i + right + 1):
            if j == i:
                continue
            if candles[j]["high"] > high_i:
                ok = False
                break

        if ok:
            pivots.append((i, high_i))

    return pivots


def line_value(p1, p2, x):
    x1, y1 = p1
    x2, y2 = p2

    if x2 == x1:
        return None

    slope = (y2 - y1) / (x2 - x1)
    return y1 + slope * (x - x1)


def choose_rising_support_line(candles, pivots, ema_now, close_now, current_index):
    usable = [p for p in pivots if p[0] <= current_index - SWEEP_WINDOW - 1]

    if len(usable) < 2:
        return None

    tail = usable[-MAX_PIVOTS_FOR_LINE:]
    best = None

    for a in range(len(tail) - 1):
        p1 = tail[a]

        for b in range(a + 1, len(tail)):
            p2 = tail[b]

            if p2[0] - p1[0] < MIN_PIVOT_GAP:
                continue

            if p2[1] <= p1[1] * (1.0 + MIN_PIVOT_RISE_PCT):
                continue

            lv_now = line_value(p1, p2, current_index)
            if lv_now is None or lv_now <= 0:
                continue

            # Şu anki fiyat çizgiden aşırı uzakta olmasın
            if close_now / lv_now > 1.50:
                continue
            if close_now / lv_now < 0.65:
                continue

            ema_line_dist = abs(lv_now - ema_now) / ema_now
            if ema_line_dist > MAX_EMA_LINE_DIST:
                continue

            violations = 0
            touches = 0

            for p in usable:
                if p[0] < p1[0]:
                    continue

                lv = line_value(p1, p2, p[0])
                if lv is None or lv <= 0:
                    continue

                if p[1] < lv * (1.0 - HIST_LINE_BREAK_TOL):
                    violations += 1

                if abs(p[1] - lv) / lv <= LINE_TOUCH_TOL:
                    touches += 1

            if violations > MAX_HIST_VIOLATIONS:
                continue

            price_dist = abs(close_now - lv_now) / lv_now

            score = (
                touches * 12.0
                + p2[0] * 0.05
                - ema_line_dist * 80.0
                - price_dist * 50.0
                - violations * 20.0
            )

            candidate = {
                "p1": p1,
                "p2": p2,
                "line_now": lv_now,
                "touches": touches,
                "violations": violations,
                "ema_line_dist_pct": ema_line_dist * 100.0,
                "score": score
            }

            if best is None or candidate["score"] > best["score"]:
                best = candidate

    return best


# =========================
# SETUP FILTERS
# =========================

def prior_pump_stats(candles, current_index):
    start = max(0, current_index - PUMP_LOOKBACK)
    end = current_index

    if end - start < 20:
        return None

    # Son mumu peak sayma; önceki pump'ı arıyoruz
    peak_i = None
    peak_high = -1.0

    for i in range(start, end):
        h = candles[i]["high"]
        if h > peak_high:
            peak_high = h
            peak_i = i

    if peak_i is None:
        return None

    min_low_before_peak = min(c["low"] for c in candles[start:peak_i + 1])
    if min_low_before_peak <= 0:
        return None

    close_now = candles[current_index]["close"]
    pump_x = peak_high / min_low_before_peak
    drop_from_peak = (peak_high - close_now) / peak_high

    return {
        "peak_i": peak_i,
        "peak_high": peak_high,
        "min_low_before_peak": min_low_before_peak,
        "pump_x": pump_x,
        "drop_from_peak_pct": drop_from_peak * 100.0
    }


def nearest_resistance(candles, close_now):
    pivots = find_pivot_highs(candles)

    above = []
    for _, h in pivots:
        if h > close_now * 1.02:
            above.append(h)

    if above:
        return min(above)

    recent_high = max(c["high"] for c in candles[-120:])
    if recent_high > close_now * 1.02:
        return recent_high

    return None


# =========================
# SIGNAL ENGINE
# =========================

def evaluate_symbol(symbol, candles, quote_volume):
    if len(candles) < max(EMA_LEN + 20, 120):
        return None

    closes = [c["close"] for c in candles]
    emas = calc_ema(closes, EMA_LEN)

    current_index = len(candles) - 1
    last = candles[current_index]

    close_now = last["close"]
    low_now = last["low"]
    ema_now = emas[current_index]

    if ema_now <= 0 or close_now <= 0:
        return None

    pump = prior_pump_stats(candles, current_index)
    if pump is None:
        return None

    if pump["pump_x"] < MIN_PUMP_X:
        return None

    if pump["drop_from_peak_pct"] < MIN_DROP_FROM_PEAK * 100.0:
        return None

    pivots = find_pivot_lows(candles)
    line = choose_rising_support_line(
        candles=candles,
        pivots=pivots,
        ema_now=ema_now,
        close_now=close_now,
        current_index=current_index
    )

    if line is None:
        return None

    line_now = line["line_now"]

    # Recent EMA / line touch kontrolü
    recent_touched_ema = False
    recent_touched_line = False
    sweep_event = False
    sweep_depth_pct = 0.0

    start_k = max(0, current_index - SWEEP_WINDOW + 1)

    for k in range(start_k, current_index + 1):
        c = candles[k]
        ema_k = emas[k]
        line_k = line_value(line["p1"], line["p2"], k)

        if line_k is None or line_k <= 0:
            continue

        if c["low"] <= ema_k * (1.0 + EMA_TOUCH_TOL):
            recent_touched_ema = True

        if c["low"] <= line_k * (1.0 + LINE_TOUCH_TOL):
            recent_touched_line = True

        low_break = (line_k - c["low"]) / line_k
        close_break = (line_k - c["close"]) / line_k

        if low_break >= SWEEP_MIN_BREAK or close_break >= SWEEP_CLOSE_BREAK:
            sweep_event = True
            sweep_depth_pct = max(sweep_depth_pct, low_break * 100.0, close_break * 100.0)

    close_reclaimed_line = close_now >= line_now * (1.0 - LINE_CLOSE_RECLAIM_TOL)
    close_reclaimed_ema = close_now >= ema_now * (1.0 - EMA_RECLAIM_TOL)

    current_line_touch = low_now <= line_now * (1.0 + LINE_TOUCH_TOL)
    current_ema_touch = low_now <= ema_now * (1.0 + EMA_TOUCH_TOL)

    # 1) Temiz tutunma
    hold_signal = (
        current_line_touch
        and current_ema_touch
        and close_reclaimed_line
        and close_reclaimed_ema
        and low_now >= line_now * (1.0 - HOLD_ALLOWED_LINE_BREAK)
    )

    # 2) Fake kırılım / sweep + reclaim
    sweep_signal = (
        sweep_event
        and recent_touched_line
        and recent_touched_ema
        and close_reclaimed_line
        and close_reclaimed_ema
    )

    if not hold_signal and not sweep_signal:
        return None

    if sweep_signal:
        signal = "DIP_SWEEP_RECLAIM"
    else:
        signal = "EMA_SUPPORT_HOLD"

    dist_ema_pct = (close_now - ema_now) / ema_now * 100.0
    dist_line_pct = (close_now - line_now) / line_now * 100.0

    resistance = nearest_resistance(candles, close_now)
    target_pct = None
    if resistance is not None:
        target_pct = (resistance - close_now) / close_now * 100.0

    volume_ok = quote_volume >= VOLUME_SOFT_MIN_USDT

    score = 0.0

    if signal == "DIP_SWEEP_RECLAIM":
        score += 35.0
    else:
        score += 25.0

    if close_now > ema_now:
        score += 10.0

    if close_now > line_now:
        score += 10.0

    if quote_volume >= VOLUME_SOFT_MIN_USDT:
        score += 8.0

    if last["close"] > last["open"]:
        score += 5.0

    score += min(pump["pump_x"], 6.0) * 2.0
    score -= abs(dist_ema_pct) * 0.25
    score -= abs(dist_line_pct) * 0.25
    score -= line["ema_line_dist_pct"] * 0.15

    return {
        "symbol": symbol,
        "signal": signal,
        "status": "LIVE" if last["is_live"] else "CLOSED",
        "close": close_now,
        "ema": ema_now,
        "line": line_now,
        "dist_ema_pct": dist_ema_pct,
        "dist_line_pct": dist_line_pct,
        "ema_line_dist_pct": line["ema_line_dist_pct"],
        "pump_x": pump["pump_x"],
        "drop_from_peak_pct": pump["drop_from_peak_pct"],
        "sweep_depth_pct": sweep_depth_pct if sweep_signal else 0.0,
        "resistance": resistance,
        "target_pct": target_pct,
        "quote_volume": quote_volume,
        "volume_ok": volume_ok,
        "score": score,
        "touches": line["touches"],
        "violations": line["violations"]
    }


# =========================
# OUTPUT
# =========================

def sort_results(results):
    priority = {
        "DIP_SWEEP_RECLAIM": 0,
        "EMA_SUPPORT_HOLD": 1
    }

    return sorted(
        results,
        key=lambda r: (
            priority.get(r["signal"], 9),
            -r["score"],
            -r["quote_volume"]
        )
    )


def save_csv(results):
    fields = [
        "symbol", "signal", "status", "close", "ema", "line",
        "dist_ema_pct", "dist_line_pct", "ema_line_dist_pct",
        "pump_x", "drop_from_peak_pct", "sweep_depth_pct",
        "resistance", "target_pct", "quote_volume", "volume_ok",
        "score", "touches", "violations"
    ]

    with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for r in results:
            writer.writerow(r)


def build_console_table(results):
    if not results:
        return "No signals found."

    lines = []
    header = (
        f"{'SYMBOL':<14} {'SIGNAL':<20} {'PX':>12} {'EMA':>12} "
        f"{'LINE':>12} {'D_EMA':>8} {'D_LINE':>8} {'VOL':>10} {'SCORE':>7}"
    )
    lines.append(header)
    lines.append("-" * len(header))

    for r in results:
        lines.append(
            f"{r['symbol']:<14} "
            f"{r['signal']:<20} "
            f"{fmt_price(r['close']):>12} "
            f"{fmt_price(r['ema']):>12} "
            f"{fmt_price(r['line']):>12} "
            f"{fmt_pct(r['dist_ema_pct']):>8} "
            f"{fmt_pct(r['dist_line_pct']):>8} "
            f"{fmt_vol(r['quote_volume']):>10} "
            f"{r['score']:>7.1f}"
        )

    return "\n".join(lines)


def build_telegram_message(results):
    ts = now_utc_str()

    if not results:
        return (
            "3D EMA + Rising Support Scanner\n"
            f"Time: {ts}\n\n"
            "No signals found."
        )

    lines = []
    lines.append("3D EMA + Rising Support Scanner")
    lines.append(f"Time: {ts}")
    lines.append(f"Signals: {len(results)}")
    lines.append("")

    top = results[:MAX_TELEGRAM_RESULTS]

    for r in top:
        vol_tag = "OK" if r["volume_ok"] else "LOW_VOL"
        res_txt = "-"
        if r["resistance"] is not None:
            res_txt = f"{fmt_price(r['resistance'])} / {fmt_pct(r['target_pct'])}"

        extra = ""
        if r["signal"] == "DIP_SWEEP_RECLAIM":
            extra = f" | Sweep: {r['sweep_depth_pct']:.2f}%"

        lines.append(
            f"{r['symbol']} | {r['signal']} | {r['status']}\n"
            f"Px: {fmt_price(r['close'])} | EMA: {fmt_price(r['ema'])} | Line: {fmt_price(r['line'])}\n"
            f"D_EMA: {fmt_pct(r['dist_ema_pct'])} | D_LINE: {fmt_pct(r['dist_line_pct'])}{extra}\n"
            f"Pump: {r['pump_x']:.2f}x | Drop: {r['drop_from_peak_pct']:.1f}% | Vol: {fmt_vol(r['quote_volume'])} {vol_tag}\n"
            f"Next R: {res_txt} | Score: {r['score']:.1f}\n"
        )

    if len(results) > MAX_TELEGRAM_RESULTS:
        lines.append(f"+{len(results) - MAX_TELEGRAM_RESULTS} more results in CSV.")

    return "\n".join(lines)


def send_telegram(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram secrets not found. Skipping Telegram message.")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    # Telegram 4096 karakter sınırı var, parça parça gönderiyoruz.
    chunks = []
    current = ""

    for line in text.splitlines():
        if len(current) + len(line) + 1 > 3800:
            chunks.append(current)
            current = line
        else:
            current += ("\n" if current else "") + line

    if current:
        chunks.append(current)

    for chunk in chunks:
        try:
            http_post_json(
                url,
                {
                    "chat_id": TELEGRAM_CHAT_ID,
                    "text": chunk,
                    "disable_web_page_preview": "true"
                }
            )
            time.sleep(0.7)
        except Exception as e:
            print(f"Telegram send error: {e}")


# =========================
# MAIN
# =========================

def main():
    print("Starting 3D EMA + Rising Support Combined Scanner")
    print(f"Time: {now_utc_str()}")
    print(f"Interval: {INTERVAL}")
    print(f"USE_LIVE_CANDLE: {USE_LIVE_CANDLE}")
    print("")

    symbols = get_symbols()
    volumes = get_24h_quote_volumes()

    max_symbols_env = os.getenv("MAX_SYMBOLS", "").strip()
    if max_symbols_env:
        try:
            max_symbols = int(max_symbols_env)
            if max_symbols > 0:
                symbols = symbols[:max_symbols]
        except Exception:
            pass

    print(f"Symbols: {len(symbols)}")

    results = []
    errors = 0

    for idx, symbol in enumerate(symbols, start=1):
        try:
            quote_volume = volumes.get(symbol, 0.0)

            if HIDE_LOW_VOLUME and quote_volume < VOLUME_SOFT_MIN_USDT:
                continue

            candles = get_klines(symbol)
            result = evaluate_symbol(symbol, candles, quote_volume)

            if result is not None:
                results.append(result)
                print(
                    f"[SIGNAL] {result['symbol']} | {result['signal']} | "
                    f"close={fmt_price(result['close'])} | vol={fmt_vol(result['quote_volume'])}"
                )

            if idx % 25 == 0:
                print(f"Scanned {idx}/{len(symbols)}")

            time.sleep(RATE_LIMIT_SLEEP)

        except Exception as e:
            errors += 1
            print(f"[ERROR] {symbol}: {e}")
            time.sleep(0.2)

    results = sort_results(results)
    save_csv(results)

    print("")
    print("=" * 80)
    print(build_console_table(results))
    print("=" * 80)
    print(f"Saved CSV: {CSV_FILE}")
    print(f"Errors: {errors}")

    msg = build_telegram_message(results)
    send_telegram(msg)


if __name__ == "__main__":
    main()
