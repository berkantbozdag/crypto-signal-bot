import os
import time
import math
import html
import requests
from typing import Dict, List, Tuple, Optional

# ============================================================
# TRIANGLE BREAK V15 - BINANCE 3D SCANNER - MID LOOKBACK 80
# Fotoğraflardaki TradingView V15 ayarlarına göre tek parça.
#
# Dosya adı:
# triangle_v15_scanner.py
#
# GitHub Secrets:
# TELEGRAM_BOT_TOKEN
# TELEGRAM_CHAT_ID
# ============================================================

BINANCE_BASE = os.getenv("BINANCE_BASE", "https://data-api.binance.vision")
INTERVAL = "3d"
KLINE_LIMIT = 1000

# ============================================================
# FOTOĞRAFLARDAKİ V15 AYARLARI
# ============================================================

LB1 = 30
LB2 = 80
LB3 = 100

VOL_MULTI = 1.0
VOL_LEN = 20

COOL_BARS = 40
EMA_PERIOD = 100
RETEST_WINDOW_BARS = 10
PEAK_SCAN_PERIOD = 10
MIN_SUPPORT_TOUCHES = 2
MAX_RANGE_NARROW_PCT = 70.0
MIN_RANGE_NARROW_PCT = 0.0
NEW_OLD_BAR_THRESHOLD = 150

# [ESKI]
OLD_MIN_TRI_HEIGHT_ATR = 0.5
OLD_TOUCH_TOLERANCE_PCT = 0.05
OLD_PIVOT_VALIDATE_LEN = 8
OLD_SMA_FILTER_ON = False
OLD_SMA_FILTER_PERIOD = 30

# [YENI]
NEW_MIN_TRI_HEIGHT_ATR = 0.5
NEW_TOUCH_TOLERANCE_PCT = 3.0
NEW_SMA_FILTER_ON = False
NEW_SMA_FILTER_PERIOD = 30

# FILTRELER
USE_REAL_TRIANGLE_FILTER = True
USE_MID_LONG_EMA_FILTER = True
MID_LONG_EMA_PERIOD = 50
MIN_UPPER_SLOPE_PCT = 3.0
MIN_REAL_TRIANGLE_NARROW_PCT = 20.0
MIN_BREAK_ZONE = 0.0

# Tarama penceresi
INCLUDE_LIVE_CANDLE = True
INCLUDE_LAST_CLOSED_CANDLE = True
INCLUDE_PREV_CLOSED_CANDLE = True

# Hacim etiketi
MIN_SERIOUS_24H_QUOTE_VOLUME = 7_000_000
SHOW_LOW_VOLUME_SIGNALS = True
MAX_ALERT_ROWS = 80

EXCLUDED_BASES = {
    "USDC", "FDUSD", "TUSD", "BUSD", "DAI", "USDP", "PAX", "PYUSD",
    "USDE", "SUSDE", "USDS", "USD1", "AEUR", "EURI",
    "EUR", "TRY", "BRL", "AUD", "GBP", "RUB", "UAH",
    "WBTC", "WETH"
}


# ============================================================
# HTTP
# ============================================================

def get_json(url: str, params: Optional[dict] = None, retries: int = 3):
    last_err = None

    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, timeout=25)

            if r.status_code in (418, 429):
                time.sleep(2 + attempt)
                continue

            r.raise_for_status()
            return r.json()

        except Exception as e:
            last_err = e
            time.sleep(0.8 * (attempt + 1))

    raise RuntimeError(f"GET failed url={url} params={params} err={last_err}")


# ============================================================
# BINANCE
# ============================================================

def get_spot_usdt_symbols() -> List[str]:
    data = get_json(f"{BINANCE_BASE}/api/v3/exchangeInfo")
    out = []

    for s in data.get("symbols", []):
        symbol = s.get("symbol", "")
        base = s.get("baseAsset", "")
        quote = s.get("quoteAsset", "")
        status = s.get("status", "")

        if status != "TRADING":
            continue
        if quote != "USDT":
            continue
        if base in EXCLUDED_BASES:
            continue
        if any(x in base for x in ["UP", "DOWN", "BULL", "BEAR"]):
            continue

        out.append(symbol)

    return sorted(set(out))


def get_24h_quote_volumes() -> Dict[str, float]:
    data = get_json(f"{BINANCE_BASE}/api/v3/ticker/24hr")
    out = {}

    for item in data:
        sym = item.get("symbol", "")
        if not sym.endswith("USDT"):
            continue

        try:
            out[sym] = float(item.get("quoteVolume", 0.0))
        except Exception:
            out[sym] = 0.0

    return out


def get_klines(symbol: str) -> List[dict]:
    raw = get_json(
        f"{BINANCE_BASE}/api/v3/klines",
        params={
            "symbol": symbol,
            "interval": INTERVAL,
            "limit": KLINE_LIMIT,
        },
    )

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


# ============================================================
# INDICATORS
# ============================================================

def sma(values: List[float], length: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(values)

    if length <= 0:
        return out

    total = 0.0

    for i, v in enumerate(values):
        total += v

        if i >= length:
            total -= values[i - length]

        if i >= length - 1:
            out[i] = total / length

    return out


def ema(values: List[float], length: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(values)

    if not values or length <= 0:
        return out

    alpha = 2.0 / (length + 1.0)
    e = values[0]
    out[0] = e

    for i in range(1, len(values)):
        e = values[i] * alpha + e * (1.0 - alpha)
        out[i] = e

    return out


def rma(values: List[float], length: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(values)

    if not values or length <= 0:
        return out

    acc = None

    for i, v in enumerate(values):
        if i == length - 1:
            acc = sum(values[:length]) / length
            out[i] = acc
        elif i >= length:
            acc = ((acc or 0.0) * (length - 1) + v) / length
            out[i] = acc

    return out


def atr(rows: List[dict], length: int = 14) -> List[Optional[float]]:
    tr = []

    for i, r in enumerate(rows):
        if i == 0:
            tr.append(r["high"] - r["low"])
        else:
            prev_close = rows[i - 1]["close"]
            tr.append(max(
                r["high"] - r["low"],
                abs(r["high"] - prev_close),
                abs(r["low"] - prev_close),
            ))

    return rma(tr, length)


def highest(rows: List[dict], field: str, idx: int, offset: int, length: int) -> Optional[float]:
    end = idx - offset
    start = end - length + 1

    if start < 0 or end < 0 or end >= len(rows):
        return None

    return max(rows[i][field] for i in range(start, end + 1))


def lowest(rows: List[dict], field: str, idx: int, offset: int, length: int) -> Optional[float]:
    end = idx - offset
    start = end - length + 1

    if start < 0 or end < 0 or end >= len(rows):
        return None

    return min(rows[i][field] for i in range(start, end + 1))


# ============================================================
# PIVOT / GERÇEK ÜÇGEN FİLTRESİ
# ============================================================

def is_pivot_high(rows: List[dict], i: int, left: int, right: int) -> bool:
    h = rows[i]["high"]

    for j in range(i - left, i + right + 1):
        if j == i:
            continue
        if j < 0 or j >= len(rows):
            return False
        if rows[j]["high"] >= h:
            return False

    return True


def is_pivot_low(rows: List[dict], i: int, left: int, right: int) -> bool:
    l = rows[i]["low"]

    for j in range(i - left, i + right + 1):
        if j == i:
            continue
        if j < 0 or j >= len(rows):
            return False
        if rows[j]["low"] <= l:
            return False

    return True


def build_pivots(rows: List[dict], pvt_len: int):
    highs = []
    lows = []

    for i in range(pvt_len, len(rows) - pvt_len):
        confirm_idx = i + pvt_len

        if is_pivot_high(rows, i, pvt_len, pvt_len):
            highs.append((i, rows[i]["high"], confirm_idx))

        if is_pivot_low(rows, i, pvt_len, pvt_len):
            lows.append((i, rows[i]["low"], confirm_idx))

    return highs, lows


def pivots_available_until(pivots, idx: int):
    # TradingView array davranışına yaklaşmak için son 20 pivot tutuluyor.
    return [(bar_i, price) for bar_i, price, confirm_i in pivots if confirm_i <= idx][-20:]


def has_real_triangle(
    rows: List[dict],
    atr_values: List[Optional[float]],
    piv_hi_all,
    piv_lo_all,
    idx: int,
    max_bars: int,
) -> bool:
    atr_now = atr_values[idx]

    if atr_now is None or atr_now <= 0:
        return False

    val_hi = pivots_available_until(piv_hi_all, idx)
    val_lo = pivots_available_until(piv_lo_all, idx)

    sz_h = len(val_hi)
    sz_l = len(val_lo)

    if sz_h < 3 or sz_l < 2:
        return False

    lh_cnt = 0
    prv_h = None

    for bar_i, price in val_hi[max(sz_h - 5, 0):]:
        if idx - bar_i <= max_bars:
            if prv_h is not None and price < prv_h:
                lh_cnt += 1
            prv_h = price

    hl_cnt = 0
    prv_l = None
    flat_ok = True

    for bar_i, price in val_lo[max(sz_l - 5, 0):]:
        if idx - bar_i <= max_bars:
            if prv_l is not None:
                if price > prv_l:
                    hl_cnt += 1
                if abs(price - prv_l) > atr_now * 4:
                    flat_ok = False
            prv_l = price

    is_flat = flat_ok

    first_l = val_lo[max(sz_l - 5, 0)][1]
    last_l = val_lo[-1][1]

    if abs(last_l - first_l) > atr_now * 3:
        is_flat = False

    first_h = val_hi[max(sz_h - 5, 0)][1]
    last_h = val_hi[-1][1]
    first_lo = val_lo[max(sz_l - 5, 0)][1]
    last_lo = val_lo[-1][1]

    old_gap = first_h - first_lo
    new_gap = last_h - last_lo

    converging = old_gap > 0 and new_gap > 0 and new_gap < old_gap
    real_narrow_pct = ((old_gap - new_gap) / old_gap * 100.0) if old_gap > 0 else 0.0
    enough_narrow = real_narrow_pct >= MIN_REAL_TRIANGLE_NARROW_PCT

    is_sym = lh_cnt >= 2 and hl_cnt >= 1 and converging and enough_narrow
    is_desc = lh_cnt >= 2 and is_flat and converging and enough_narrow

    return is_sym or is_desc


# ============================================================
# TRIANGLE CHECK
# ============================================================

def params_for_bar(idx: int) -> dict:
    is_new = idx < NEW_OLD_BAR_THRESHOLD

    if is_new:
        return {
            "market_age": "YENI",
            "min_tri_height_atr": NEW_MIN_TRI_HEIGHT_ATR,
            "touch_tolerance_pct": NEW_TOUCH_TOLERANCE_PCT,
            "sma_filter_on": NEW_SMA_FILTER_ON,
            "sma_period": NEW_SMA_FILTER_PERIOD,
        }

    return {
        "market_age": "ESKI",
        "min_tri_height_atr": OLD_MIN_TRI_HEIGHT_ATR,
        "touch_tolerance_pct": OLD_TOUCH_TOLERANCE_PCT,
        "sma_filter_on": OLD_SMA_FILTER_ON,
        "sma_period": OLD_SMA_FILTER_PERIOD,
    }


def tri_values(rows: List[dict], idx: int, lb: int) -> Optional[dict]:
    t = max(math.floor(lb / 3), 3)

    h1 = highest(rows, "high", idx, 0, t)
    h2 = highest(rows, "high", idx, t, t)
    h3 = highest(rows, "high", idx, t * 2, t)

    l1 = lowest(rows, "low", idx, 0, t)
    l2 = lowest(rows, "low", idx, t, t)
    l3 = lowest(rows, "low", idx, t * 2, t)

    if None in (h1, h2, h3, l1, l2, l3):
        return None

    return {
        "t": t,
        "h1": h1,
        "h2": h2,
        "h3": h3,
        "l1": l1,
        "l2": l2,
        "l3": l3,
    }


def check_tri(
    rows: List[dict],
    atr_values: List[Optional[float]],
    ema_ml: List[Optional[float]],
    sma_old: List[Optional[float]],
    sma_new: List[Optional[float]],
    idx: int,
    lb: int,
    require_mid_long_ema: bool,
):
    if idx <= 1:
        return False, None, None

    atr_now = atr_values[idx]

    if atr_now is None or atr_now <= 0:
        return False, None, None

    p = params_for_bar(idx)
    v = tri_values(rows, idx, lb)
    v_prev = tri_values(rows, idx - 1, lb)

    if v is None or v_prev is None:
        return False, None, None

    h1 = v["h1"]
    h2 = v["h2"]
    h3 = v["h3"]
    l1 = v["l1"]
    l2 = v["l2"]
    l3 = v["l3"]

    d_h = h1 < h2 or h1 < h3
    d_h2 = h1 < h3
    a_l = l1 > l2 or l1 > l3
    f_l = abs(l1 - l3) < atr_now * (1.5 if lb <= 60 else 4.0)

    r_now = h1 - l1
    r_old = h3 - l3
    n_pct = ((r_old - r_now) / r_old * 100.0) if r_old != 0 else 0.0
    conv = n_pct >= MIN_RANGE_NARROW_PCT and n_pct <= MAX_RANGE_NARROW_PCT

    sym = d_h and a_l and conv
    desc = d_h2 and f_l and conv
    ok = sym or desc

    top = max(h1, h2, h3)
    bot = min(l1, l2, l3)
    tri_h = top - bot
    big = tri_h > atr_now * p["min_tri_height_atr"]

    sup = min(l1, l2, l3)

    # Fotoğraftaki input "Touch Tolerance %" olduğu için yüzde olarak kullanılıyor.
    # YENI 3 => %3, ESKI 0.05 => %0.05
    touch_tol_decimal = p["touch_tolerance_pct"] / 100.0
    tol = sup * touch_tol_decimal * max(lb / 50.0, 1.0)

    touches = 0
    for j in range(lb):
        k = idx - j
        if k < 0:
            break
        if abs(rows[k]["low"] - sup) <= tol:
            touches += 1

    upper_slope_pct = ((h3 - h1) / h3 * 100.0) if h3 != 0 else 0.0
    upper_strict_ok = h3 > h2 and h2 > h1 and upper_slope_pct >= MIN_UPPER_SLOPE_PCT
    upper_ok = (not USE_REAL_TRIANGLE_FILTER) or upper_strict_ok

    zone_denom = h1 - sup
    zone_pos = ((rows[idx]["close"] - sup) / zone_denom) if zone_denom > 0 else 1.0
    zone_ok = True if MIN_BREAK_ZONE <= 0 else zone_pos >= MIN_BREAK_ZONE

    if p["sma_filter_on"]:
        sma_list = sma_new if p["market_age"] == "YENI" else sma_old
        sma_now = sma_list[idx]
        above_sma = sma_now is not None and rows[idx]["close"] > sma_now
    else:
        above_sma = True

    ema_ml_now = ema_ml[idx]
    mid_long_ok = (
        (not require_mid_long_ema)
        or (not USE_MID_LONG_EMA_FILTER)
        or (ema_ml_now is not None and rows[idx]["close"] > ema_ml_now)
    )

    valid = (
        ok
        and big
        and touches >= MIN_SUPPORT_TOUCHES
        and above_sma
        and upper_ok
        and zone_ok
        and mid_long_ok
    )

    meta = {
        "market_age": p["market_age"],
        "narrow_pct": n_pct,
        "touches": touches,
        "upper_slope_pct": upper_slope_pct,
        "sup": sup,
        "h1": h1,
        "h2": h2,
        "h3": h3,
        "valid": valid,
    }

    if not valid:
        return False, h1, meta

    close_now = rows[idx]["close"]
    close_prev = rows[idx - 1]["close"]

    brk1 = close_now > h1 and close_prev <= v_prev["h1"]
    brk2 = close_now > h2 and close_prev <= v_prev["h2"] and not brk1

    if brk1:
        meta["break_type"] = "H1"
    elif brk2:
        meta["break_type"] = "H2"
    else:
        meta["break_type"] = "NONE"

    return brk1 or brk2, h1, meta


# ============================================================
# FULL SIMULATION
# ============================================================

def simulate_v15(rows: List[dict]) -> Dict[int, dict]:
    n = len(rows)

    if n < 80:
        return {}

    closes = [r["close"] for r in rows]
    volumes = [r["volume"] for r in rows]

    atr_values = atr(rows, 14)
    ema100 = ema(closes, EMA_PERIOD)
    ema_ml = ema(closes, MID_LONG_EMA_PERIOD)
    vol_ma = sma(volumes, VOL_LEN)
    sma_old = sma(closes, OLD_SMA_FILTER_PERIOD)
    sma_new = sma(closes, NEW_SMA_FILTER_PERIOD)

    piv_hi_all, piv_lo_all = build_pivots(rows, OLD_PIVOT_VALIDATE_LEN)

    signals: Dict[int, dict] = {}

    cnt = 999

    pending = False
    pending_bar = 0
    pending_low = None
    pending_hh = None
    pending_meta = None

    for idx in range(n):
        cnt += 1

        short_break, short_hh, short_meta = check_tri(
            rows, atr_values, ema_ml, sma_old, sma_new, idx, LB1, False
        )

        mid_break, mid_hh, mid_meta = check_tri(
            rows, atr_values, ema_ml, sma_old, sma_new, idx, LB2, True
        )

        long_break, long_hh, long_meta = check_tri(
            rows, atr_values, ema_ml, sma_old, sma_new, idx, LB3, True
        )

        if USE_REAL_TRIANGLE_FILTER:
            s_pivot_ok = has_real_triangle(rows, atr_values, piv_hi_all, piv_lo_all, idx, LB1 * 3)
            m_pivot_ok = has_real_triangle(rows, atr_values, piv_hi_all, piv_lo_all, idx, LB2 * 2)
            l_pivot_ok = has_real_triangle(rows, atr_values, piv_hi_all, piv_lo_all, idx, LB3 * 2)
        else:
            s_pivot_ok = True
            m_pivot_ok = True
            l_pivot_ok = True

        vm = vol_ma[idx]
        vol_ok = True if VOL_MULTI <= 1.0 else (
            vm is not None and rows[idx]["volume"] > vm * VOL_MULTI
        )

        s_break = short_break and vol_ok and s_pivot_ok
        m_break = mid_break and vol_ok and m_pivot_ok and not s_break
        l_break = long_break and vol_ok and l_pivot_ok and not s_break and not m_break

        mid_direct = m_break and cnt >= COOL_BARS
        long_direct = l_break and cnt >= COOL_BARS and not mid_direct

        if mid_direct or long_direct:
            cnt = 0

        # AL30: kısa üçgen için 2 bar onay
        if s_break and not pending:
            pending = True
            pending_bar = idx
            pending_low = rows[idx]["low"]
            pending_hh = short_hh
            pending_meta = short_meta

        normal_confirm = False
        normal_meta = None

        if pending and (idx - pending_bar) >= 2:
            stayed_up = (
                pending_low is not None
                and rows[idx - 1]["low"] >= pending_low
                and rows[idx]["low"] >= pending_low
            )
            any_green = (
                rows[idx - 1]["close"] > rows[idx - 1]["open"]
                or rows[idx]["close"] > rows[idx]["open"]
            )

            if stayed_up and any_green:
                normal_confirm = True
                normal_meta = pending_meta

            pending = False

        short_confirmed = normal_confirm and cnt >= COOL_BARS

        if short_confirmed:
            cnt = 0

        if short_confirmed or mid_direct or long_direct:
            if short_confirmed:
                label = "AL30"
                meta = normal_meta or pending_meta or {}
                level = pending_hh if pending_hh is not None else short_hh
            elif mid_direct:
                label = "AL80"
                meta = mid_meta or {}
                level = mid_hh
            else:
                label = "AL100"
                meta = long_meta or {}
                level = long_hh

            signals[idx] = {
                "label": label,
                "level": level,
                "close": rows[idx]["close"],
                "meta": meta,
                "ema100": ema100[idx],
                "ema_ml": ema_ml[idx],
            }

    return signals


# ============================================================
# SCAN HELPERS
# ============================================================

def candidate_indices(rows: List[dict]) -> List[Tuple[str, int]]:
    if not rows:
        return []

    now_ms = int(time.time() * 1000)
    last_idx = len(rows) - 1
    last_is_live = rows[last_idx]["close_time"] > now_ms

    if last_is_live:
        live_idx = last_idx
        last_closed_idx = last_idx - 1
    else:
        live_idx = None
        last_closed_idx = last_idx

    out = []

    if INCLUDE_LIVE_CANDLE and live_idx is not None:
        out.append(("LIVE", live_idx))

    if INCLUDE_LAST_CLOSED_CANDLE and last_closed_idx is not None and last_closed_idx >= 0:
        out.append(("CLOSED", last_closed_idx))

    if INCLUDE_PREV_CLOSED_CANDLE and last_closed_idx is not None and last_closed_idx - 1 >= 0:
        out.append(("PREV", last_closed_idx - 1))

    return out


def scan_symbol(symbol: str, quote_volume_24h: float) -> Optional[dict]:
    try:
        rows = get_klines(symbol)
    except Exception as e:
        print(f"[WARN] {symbol} kline error: {e}")
        return None

    if len(rows) < 80:
        return None

    signals = simulate_v15(rows)
    hits = []

    for status, idx in candidate_indices(rows):
        if idx in signals:
            h = dict(signals[idx])
            h["status"] = status
            h["idx"] = idx
            hits.append(h)

    if not hits:
        return None

    priority = {
        "CLOSED": 1,
        "LIVE": 2,
        "PREV": 3,
    }

    hits.sort(key=lambda x: priority.get(x["status"], 99))
    best = hits[0]

    volume_status = "OK" if quote_volume_24h >= MIN_SERIOUS_24H_QUOTE_VOLUME else "LOWVOL"

    if volume_status == "LOWVOL" and not SHOW_LOW_VOLUME_SIGNALS:
        return None

    status_tags = ",".join(h["status"] + ":" + h["label"] for h in hits)

    return {
        "symbol": symbol,
        "status": best["status"],
        "label": best["label"],
        "status_tags": status_tags,
        "quote_volume_24h": quote_volume_24h,
        "volume_status": volume_status,
        "price": best["close"],
        "level": best.get("level"),
        "meta": best.get("meta", {}),
    }


# ============================================================
# TELEGRAM
# ============================================================

def fmt_money(x: float) -> str:
    if x >= 1_000_000_000:
        return f"{x / 1_000_000_000:.2f}B"
    if x >= 1_000_000:
        return f"{x / 1_000_000:.2f}M"
    if x >= 1_000:
        return f"{x / 1_000:.1f}K"
    return f"{x:.0f}"


def fmt_num(x: Optional[float]) -> str:
    if x is None:
        return "na"
    if x == 0:
        return "0"
    if abs(x) >= 100:
        return f"{x:.3f}"
    if abs(x) >= 1:
        return f"{x:.5f}"
    return f"{x:.8f}"


def fmt1(x: Optional[float]) -> str:
    if x is None:
        return "na"
    return f"{x:.1f}"


def build_message(results: List[dict]) -> str:
    header = (
        "🔺 <b>Triangle Break V15 Scanner</b>\n"
        "3D | LIVE + CLOSED + PREV kontrol edildi\n"
        "Ayar: Fotoğraflardaki V15 ayarları\n\n"
    )

    if not results:
        return header + "Sinyal yok."

    lines = [header]

    for i, r in enumerate(results[:MAX_ALERT_ROWS], 1):
        meta = r.get("meta", {}) or {}

        warning = ""
        if r["status"] == "LIVE":
            warning = "\n⚠️ LIVE: 3D mum kapanmadı, TradingView sinyali mum içinde kaybolabilir."

        line = (
            f"{i}) <b>{html.escape(r['symbol'])}</b> | <b>{html.escape(r['label'])}</b> | {html.escape(r['status'])}\n"
            f"Vol: <b>{fmt_money(r['quote_volume_24h'])}</b> USDT | {html.escape(r['volume_status'])}\n"
            f"Price: {fmt_num(r['price'])} | Level: {fmt_num(r.get('level'))}\n"
            f"Age: {html.escape(str(meta.get('market_age', 'na')))} | "
            f"Narrow: {fmt1(meta.get('narrow_pct'))}% | "
            f"Touches: {html.escape(str(meta.get('touches', 'na')))} | "
            f"Slope: {fmt1(meta.get('upper_slope_pct'))}% | "
            f"Break: {html.escape(str(meta.get('break_type', 'na')))}\n"
            f"Tags: {html.escape(r['status_tags'])}"
            f"{warning}\n"
        )

        lines.append(line)

    if len(results) > MAX_ALERT_ROWS:
        lines.append(f"\n+{len(results) - MAX_ALERT_ROWS} sinyal daha var, mesaj limiti nedeniyle kesildi.")

    return "\n".join(lines)


def split_message(text: str, max_len: int = 3800) -> List[str]:
    if len(text) <= max_len:
        return [text]

    parts = []
    cur = ""

    for block in text.split("\n\n"):
        if len(cur) + len(block) + 2 <= max_len:
            cur += block + "\n\n"
        else:
            if cur.strip():
                parts.append(cur.strip())
            cur = block + "\n\n"

    if cur.strip():
        parts.append(cur.strip())

    return parts


def send_telegram(message: str):
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    if not token or not chat_id:
        print("[INFO] Telegram secret yok. Mesaj konsola yazılıyor.")
        print(message)
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"

    for part in split_message(message):
        payload = {
            "chat_id": chat_id,
            "text": part,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }

        try:
            r = requests.post(url, json=payload, timeout=25)
            if not r.ok:
                print(f"[WARN] Telegram error {r.status_code}: {r.text}")
        except Exception as e:
            print(f"[WARN] Telegram send failed: {e}")

        time.sleep(0.4)


# ============================================================
# MAIN
# ============================================================

def main():
    print("Starting Triangle Break V15 scanner...")
    print(f"Interval={INTERVAL}, limit={KLINE_LIMIT}")
    print(f"Lookbacks={LB1}/{LB2}/{LB3}, cooldown={COOL_BARS}, barThreshold={NEW_OLD_BAR_THRESHOLD}")
    print(f"LIVE={INCLUDE_LIVE_CANDLE}, CLOSED={INCLUDE_LAST_CLOSED_CANDLE}, PREV={INCLUDE_PREV_CLOSED_CANDLE}")

    symbols = get_spot_usdt_symbols()
    volumes = get_24h_quote_volumes()

    print(f"Symbols: {len(symbols)}")

    results = []

    for i, symbol in enumerate(symbols, 1):
        qv = volumes.get(symbol, 0.0)

        try:
            hit = scan_symbol(symbol, qv)
            if hit:
                results.append(hit)
                print(f"[HIT] {symbol} {hit['label']} {hit['status']} vol={fmt_money(qv)}")

        except Exception as e:
            print(f"[WARN] {symbol} failed: {e}")

        if i % 25 == 0:
            time.sleep(0.25)

    results.sort(key=lambda x: x["quote_volume_24h"], reverse=True)

    print(f"Total signals: {len(results)}")

    msg = build_message(results)
    send_telegram(msg)


if __name__ == "__main__":
    main()
