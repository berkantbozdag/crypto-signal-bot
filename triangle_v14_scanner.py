import os
import json
import time
import math
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# ============================================================
# TRIANGLE BREAK V14 SCANNER — TradingView ekran ayarlarına göre
# ============================================================

INTERVAL = "3d"
KLINE_LIMIT = 600

# TradingView aktif ayarların
LB1 = 30
LB2 = 90
LB3 = 100

VOL_MULTI = 1.0
VOL_LEN = 20
COOLDOWN_BARS = 100
EMA_LEN = 100

RETEST_BARS = 10
PIVOT_SCAN = 10
MIN_TOUCHES = 2
MAX_NARROW = 70.0
MIN_NARROW = 0.0

BAR_THRESH = 150

OLD_MIN_TRI_H = 0.5
OLD_TOUCH_TOL = 0.03
OLD_PVT_VAL_LEN = 8
OLD_USE_SMA = False
OLD_SMA_LEN = 30

NEW_MIN_TRI_H = 0.5
NEW_TOUCH_TOL = 3.0       # KRİTİK: aynen 3, 0.03 değil
NEW_USE_SMA = True
NEW_SMA_LEN = 30

# True = açık 3D mumda da bakar, TradingView canlıya daha yakın.
# False = sadece kapanmış mumda bakar, daha güvenli ama geç gelir.
SCAN_CURRENT_CANDLE = True

STATE_FILE = "sent_alerts_v14.json"

BASE_URLS = [
    "https://data-api.binance.vision",
    "https://api.binance.com",
]

EXCLUDED_BASES = {
    "USDC", "FDUSD", "TUSD", "BUSD", "DAI", "USDP", "PAX", "PYUSD",
    "USDE", "SUSDE", "USDS", "USD1", "AEUR", "EURI",
    "EUR", "TRY", "BRL", "AUD", "GBP", "RUB", "UAH",
}

EXCLUDED_SUFFIXES = ("UP", "DOWN", "BULL", "BEAR")


# ============================================================
# HTTP
# ============================================================

def http_get(path, params=None, timeout=20):
    last_err = None
    query = ""
    if params:
        query = "?" + urllib.parse.urlencode(params)

    for base in BASE_URLS:
        url = base + path + query
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "triangle-v14-scanner/1.0"}
            )
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            last_err = e
            time.sleep(0.3)

    raise RuntimeError(f"HTTP failed: {path} | {last_err}")


def telegram_send(text):
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    if not token or not chat_id:
        print("Telegram secrets yok. Mesaj yazdırılıyor:")
        print(text)
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode("utf-8")

    req = urllib.request.Request(url, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=20) as r:
        r.read()


# ============================================================
# DATA
# ============================================================

def get_symbols():
    info = http_get("/api/v3/exchangeInfo")
    symbols = []

    for s in info.get("symbols", []):
        symbol = s.get("symbol", "")
        base = s.get("baseAsset", "")
        quote = s.get("quoteAsset", "")
        status = s.get("status", "")

        if quote != "USDT":
            continue
        if status != "TRADING":
            continue
        if base in EXCLUDED_BASES:
            continue
        if base.endswith(EXCLUDED_SUFFIXES):
            continue
        if not s.get("isSpotTradingAllowed", True):
            continue

        symbols.append(symbol)

    return sorted(symbols)


def get_klines(symbol):
    raw = http_get("/api/v3/klines", {
        "symbol": symbol,
        "interval": INTERVAL,
        "limit": KLINE_LIMIT,
    })

    rows = []
    for k in raw:
        rows.append({
            "open_time": int(k[0]),
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "volume": float(k[5]),
            "close_time": int(k[6]),
            "quote_volume": float(k[7]),
        })

    return rows


def get_24h_quote_volumes():
    try:
        data = http_get("/api/v3/ticker/24hr")
        return {x["symbol"]: float(x.get("quoteVolume", 0.0)) for x in data}
    except Exception as e:
        print("24h volume alınamadı:", e)
        return {}


# ============================================================
# INDICATOR HELPERS
# ============================================================

def sma(values, i, length):
    if i + 1 < length:
        return None
    return sum(values[i - length + 1:i + 1]) / length


def rma(values, length):
    out = [None] * len(values)
    if len(values) < length:
        return out

    first = sum(values[:length]) / length
    out[length - 1] = first

    for i in range(length, len(values)):
        out[i] = (out[i - 1] * (length - 1) + values[i]) / length

    return out


def atr_series(highs, lows, closes, length=14):
    tr = []
    for i in range(len(highs)):
        if i == 0:
            tr.append(highs[i] - lows[i])
        else:
            tr.append(max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            ))
    return rma(tr, length)


def is_pivot_high(highs, i, left, right):
    if i < left + right:
        return None

    c = i - right
    start = c - left
    end = c + right

    if start < 0:
        return None

    val = highs[c]
    window = highs[start:end + 1]

    if val == max(window):
        return val

    return None


def is_pivot_low(lows, i, left, right):
    if i < left + right:
        return None

    c = i - right
    start = c - left
    end = c + right

    if start < 0:
        return None

    val = lows[c]
    window = lows[start:end + 1]

    if val == min(window):
        return val

    return None


def seg_high(highs, i, start_ago, length):
    start = i - start_ago - length + 1
    end = i - start_ago
    if start < 0 or end < 0:
        return None
    return max(highs[start:end + 1])


def seg_low(lows, i, start_ago, length):
    start = i - start_ago - length + 1
    end = i - start_ago
    if start < 0 or end < 0:
        return None
    return min(lows[start:end + 1])


# ============================================================
# PINE V14 LOGIC
# ============================================================

def has_real_triangle(max_bars, i, val_hi_p, val_hi_b, val_lo_p, val_lo_b, atr):
    sz_h = len(val_hi_p)
    sz_l = len(val_lo_p)

    if sz_h < 3 or sz_l < 2 or atr is None:
        return False

    lh_cnt = 0
    prv = None

    for idx in range(max(sz_h - 5, 0), sz_h):
        bi = val_hi_b[idx]
        if i - bi <= max_bars:
            pi = val_hi_p[idx]
            if prv is not None and pi < prv:
                lh_cnt += 1
            prv = pi

    hl_cnt = 0
    prv_l = None
    flat_ok = True

    for idx in range(max(sz_l - 5, 0), sz_l):
        bi = val_lo_b[idx]
        if i - bi <= max_bars:
            pi = val_lo_p[idx]
            if prv_l is not None:
                if pi > prv_l:
                    hl_cnt += 1
                if abs(pi - prv_l) > atr * 4:
                    flat_ok = False
            prv_l = pi

    is_flat = flat_ok

    if sz_l >= 2:
        f_l = val_lo_p[max(sz_l - 5, 0)]
        l_l = val_lo_p[sz_l - 1]
        if abs(l_l - f_l) > atr * 3:
            is_flat = False

    converging = False

    if sz_h >= 2 and sz_l >= 2:
        f_h = val_hi_p[max(sz_h - 5, 0)]
        l_h = val_hi_p[sz_h - 1]
        f_lo = val_lo_p[max(sz_l - 5, 0)]
        l_lo = val_lo_p[sz_l - 1]

        old_gap = f_h - f_lo
        new_gap = l_h - l_lo

        if old_gap > 0 and new_gap > 0 and new_gap < old_gap:
            converging = True

    is_sym = lh_cnt >= 2 and hl_cnt >= 1 and converging
    is_desc = lh_cnt >= 2 and is_flat and converging

    return is_sym or is_desc


def check_tri(lb, i, highs, lows, closes, atrs, is_new_coin, above_avg):
    atr = atrs[i]

    if atr is None:
        return False, None

    t = max(math.floor(lb / 3), 3)

    h1 = seg_high(highs, i, 0, t)
    h2 = seg_high(highs, i, t, t)
    h3 = seg_high(highs, i, t * 2, t)

    l1 = seg_low(lows, i, 0, t)
    l2 = seg_low(lows, i, t, t)
    l3 = seg_low(lows, i, t * 2, t)

    if None in (h1, h2, h3, l1, l2, l3):
        return False, None

    d_h = h1 < h2 or h1 < h3
    d_h2 = h1 < h3
    a_l = l1 > l2 or l1 > l3

    flat_mult = 1.5 if is_new_coin else (1.5 if lb <= 60 else 4.0)
    f_l = abs(l1 - l3) < atr * flat_mult

    r_now = h1 - l1
    r_old = h3 - l3

    n_pct = ((r_old - r_now) / r_old * 100) if r_old != 0 else 0

    if is_new_coin:
        conv = n_pct >= MIN_NARROW and n_pct <= MAX_NARROW
    else:
        conv = (n_pct >= MIN_NARROW and n_pct <= MAX_NARROW) if lb <= 60 else True

    sym = d_h and a_l and conv
    desc = d_h2 and f_l and conv
    ok = sym or desc

    top = max(h1, h2, h3)
    bot = min(l1, l2, l3)
    tri_h = top - bot

    min_h = NEW_MIN_TRI_H if is_new_coin else OLD_MIN_TRI_H
    big = tri_h > atr * min_h

    sup = min(l1, l2, l3)

    tol_base = NEW_TOUCH_TOL if is_new_coin else OLD_TOUCH_TOL
    if is_new_coin:
        tol = sup * tol_base
    else:
        tol = sup * tol_base * max(lb / 50.0, 1.0)

    if i - lb + 1 < 0:
        return False, h1

    touch_count = 0
    for j in range(i - lb + 1, i + 1):
        if abs(lows[j] - sup) <= tol:
            touch_count += 1

    valid = ok and big and touch_count >= MIN_TOUCHES and above_avg

    if not valid or i < 1:
        return False, h1

    # h1[1] ve h2[1] karşılığı
    h1_prev = seg_high(highs, i - 1, 0, t)
    h2_prev = seg_high(highs, i - 1, t, t)

    brk1 = False
    brk2 = False

    if h1_prev is not None:
        brk1 = closes[i] > h1 and closes[i - 1] <= h1_prev

    if h2_prev is not None:
        brk2 = closes[i] > h2 and closes[i - 1] <= h2_prev and not brk1

    return brk1 or brk2, h1


def scan_symbol(symbol, rows):
    if len(rows) < 120:
        return []

    opens = [r["open"] for r in rows]
    highs = [r["high"] for r in rows]
    lows = [r["low"] for r in rows]
    closes = [r["close"] for r in rows]
    volumes = [r["volume"] for r in rows]

    atrs = atr_series(highs, lows, closes, 14)

    val_hi_p = []
    val_hi_b = []
    val_lo_p = []
    val_lo_b = []

    cnt = 999

    pending = False
    pending_bar = 0
    pending_low = None
    pending_hh = None

    retest_pending = False
    retest_start = 0
    retest_level = None
    retest_dipped = False

    events = []

    last_index_to_scan = len(rows) - 1 if SCAN_CURRENT_CANDLE else len(rows) - 2
    if last_index_to_scan < 0:
        return []

    for i in range(0, last_index_to_scan + 1):
        atr = atrs[i]

        # Pivot update
        pv_h = is_pivot_high(highs, i, OLD_PVT_VAL_LEN, OLD_PVT_VAL_LEN)
        pv_l = is_pivot_low(lows, i, OLD_PVT_VAL_LEN, OLD_PVT_VAL_LEN)

        if pv_h is not None:
            val_hi_p.append(pv_h)
            val_hi_b.append(i - OLD_PVT_VAL_LEN)
            if len(val_hi_p) > 15:
                val_hi_p.pop(0)
                val_hi_b.pop(0)

        if pv_l is not None:
            val_lo_p.append(pv_l)
            val_lo_b.append(i - OLD_PVT_VAL_LEN)
            if len(val_lo_p) > 15:
                val_lo_p.pop(0)
                val_lo_b.pop(0)

        is_new_coin = i < BAR_THRESH

        sma_old = sma(closes, i, OLD_SMA_LEN)
        sma_new = sma(closes, i, NEW_SMA_LEN)

        if is_new_coin:
            above_avg = closes[i] > sma_new if NEW_USE_SMA and sma_new is not None else (not NEW_USE_SMA)
        else:
            above_avg = closes[i] > sma_old if OLD_USE_SMA and sma_old is not None else (not OLD_USE_SMA)

        short_break, short_hh = check_tri(LB1, i, highs, lows, closes, atrs, is_new_coin, above_avg)
        mid_break, mid_hh = check_tri(LB2, i, highs, lows, closes, atrs, is_new_coin, above_avg)
        long_break, long_hh = check_tri(LB3, i, highs, lows, closes, atrs, is_new_coin, above_avg)

        s_pivot_ok = True if is_new_coin else has_real_triangle(LB1 * 3, i, val_hi_p, val_hi_b, val_lo_p, val_lo_b, atr)
        m_pivot_ok = True if is_new_coin else has_real_triangle(LB2 * 2, i, val_hi_p, val_hi_b, val_lo_p, val_lo_b, atr)
        l_pivot_ok = True if is_new_coin else has_real_triangle(LB3 * 2, i, val_hi_p, val_hi_b, val_lo_p, val_lo_b, atr)

        vol_ma = sma(volumes, i, VOL_LEN)
        vol_ok = True
        if VOL_MULTI > 1.0:
            vol_ok = vol_ma is not None and volumes[i] > vol_ma * VOL_MULTI

        s_break = short_break and vol_ok and s_pivot_ok
        m_break = mid_break and vol_ok and m_pivot_ok and not s_break
        l_break = long_break and vol_ok and l_pivot_ok and not s_break and not m_break

        cnt += 1

        mid_direct = m_break and cnt >= COOLDOWN_BARS
        long_direct = l_break and cnt >= COOLDOWN_BARS and not mid_direct

        if mid_direct or long_direct:
            cnt = 0

        do_retest = True if is_new_coin else False

        if s_break and not pending:
            pending = True
            pending_bar = i
            pending_low = lows[i]
            pending_hh = short_hh

        normal_confirm = False

        if pending and not do_retest and (i - pending_bar) >= 2:
            stayed_up = lows[i - 1] >= pending_low and lows[i] >= pending_low
            any_green = closes[i - 1] > opens[i - 1] or closes[i] > opens[i]

            if stayed_up and any_green:
                normal_confirm = True

            pending = False

        if pending and do_retest and (i - pending_bar) >= 2:
            retest_pending = True
            retest_start = i
            retest_level = pending_hh
            retest_dipped = False
            pending = False

        retest_confirm = False

        if retest_pending and retest_level is not None:
            if lows[i] <= retest_level * 1.02:
                retest_dipped = True

            if retest_dipped and closes[i] > retest_level and closes[i] > opens[i]:
                retest_confirm = True
                retest_pending = False

            if (i - retest_start) >= RETEST_BARS:
                if not retest_dipped and closes[i] > retest_level:
                    retest_confirm = True
                retest_pending = False

            if closes[i] < retest_level * 0.90:
                retest_pending = False

        short_confirmed = (normal_confirm or retest_confirm) and cnt >= COOLDOWN_BARS

        if short_confirmed:
            cnt = 0

        final_break = short_confirmed or mid_direct or long_direct

        if final_break:
            if short_confirmed:
                sig = "AL 30"
            elif mid_direct:
                sig = "AL 90"
            else:
                sig = "AL 100"

            events.append({
                "symbol": symbol,
                "signal": sig,
                "index": i,
                "open_time": rows[i]["open_time"],
                "close_time": rows[i]["close_time"],
                "price": closes[i],
                "is_new_coin": is_new_coin,
            })

    return events


# ============================================================
# STATE
# ============================================================

def load_state():
    if not os.path.exists(STATE_FILE):
        return {}

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False, sort_keys=True)


def event_key(e):
    return f'{e["symbol"]}|{e["signal"]}|{e["open_time"]}'


# ============================================================
# MAIN
# ============================================================

def main():
    print("Triangle Break V14 scanner başladı.")

    state = load_state()
    symbols = get_symbols()
    quote_volumes = get_24h_quote_volumes()

    print(f"Toplam sembol: {len(symbols)}")

    new_alerts = []
    scanned = 0
    failed = 0

    for symbol in symbols:
        try:
            rows = get_klines(symbol)
            events = scan_symbol(symbol, rows)

            if not events:
                scanned += 1
                continue

            latest_bar_index = len(rows) - 1 if SCAN_CURRENT_CANDLE else len(rows) - 2

            for e in events:
                if e["index"] != latest_bar_index:
                    continue

                key = event_key(e)
                if state.get(key):
                    continue

                e["quote_volume_24h"] = quote_volumes.get(symbol, 0.0)
                new_alerts.append(e)
                state[key] = datetime.now(timezone.utc).isoformat()

            scanned += 1
            time.sleep(0.08)

        except Exception as ex:
            failed += 1
            print(f"{symbol} hata: {ex}")
            time.sleep(0.2)

    save_state(state)

    if not new_alerts:
        print(f"Sinyal yok. Scanned={scanned}, Failed={failed}")
        return

    new_alerts.sort(key=lambda x: x.get("quote_volume_24h", 0.0), reverse=True)

    lines = []
    lines.append("🚨 <b>Triangle Break V14 Sinyal</b>")
    lines.append(f"TF: <b>{INTERVAL}</b>")
    lines.append(f"Mode: {'Açık mum' if SCAN_CURRENT_CANDLE else 'Kapanmış mum'}")
    lines.append("")

    for e in new_alerts:
        dt = datetime.fromtimestamp(e["open_time"] / 1000, tz=timezone.utc)
        vol_m = e.get("quote_volume_24h", 0.0) / 1_000_000

        lines.append(
            f'• <b>{e["symbol"]}</b> | {e["signal"]} | '
            f'Price: <b>{e["price"]:.8g}</b> | '
            f'24h Vol: <b>{vol_m:.1f}M USDT</b> | '
            f'{"Yeni" if e["is_new_coin"] else "Eski"} | '
            f'{dt.strftime("%Y-%m-%d %H:%M")} UTC'
        )

    msg = "\n".join(lines)
    telegram_send(msg)

    print(f"{len(new_alerts)} yeni sinyal gönderildi.")


if __name__ == "__main__":
    main()
