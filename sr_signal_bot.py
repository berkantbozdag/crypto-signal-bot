import os
import time
import html
import requests
import numpy as np
import pandas as pd
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

# =====================
# AYARLAR
# =====================

MIN_QUOTE_VOLUME_USDT = 7_000_000

INTERVAL = "4h"
KLINE_LIMIT = 500
MAX_WORKERS = 6

PIVOT_LEFT = 3
PIVOT_RIGHT = 3
LEVEL_LOOKBACK = 60

# GitHub 15 dakikada bir çalışıyor.
# Sadece yeni kapanan 4H mumu dikkate alsın.
FRESH_CANDLE_MAX_MINUTES = 25

# ALIM: 4H destekten tepki
TOUCH_TOL = 0.006
RECLAIM_PCT = 0.000
STOP_UNDER_SUPPORT_PCT = 0.015
SELL_BEFORE_RESISTANCE_PCT = 0.003
MIN_TARGET_DISTANCE_PCT = 0.015
MIN_RR = 1.30

# SATIŞ: 4H dirençten red
RES_TOUCH_TOL = 0.006
REJECT_CLOSE_BELOW_PCT = 0.0015
MIN_UPPER_WICK_PCT = 0.003
SELL_INVALIDATION_ABOVE_RES_PCT = 0.008

# Hacim onayı
USE_VOLUME_CONFIRM = True
VOL_MA_LEN = 20
VOL_MULT = 0.90

MAX_SIGNALS_TO_SEND = 15

EXCLUDE_BASES = {
    "USDC", "FDUSD", "TUSD", "BUSD", "DAI", "USDP", "USDE",
    "EUR", "EURI", "TRY", "BRL", "GBP", "AUD", "PAXG"
}

EXCLUDE_SUFFIXES = ("UP", "DOWN", "BULL", "BEAR")

BASE_URLS = [
    "https://data-api.binance.vision",
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
    "https://api4.binance.com",
]

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

session = requests.Session()


# =====================
# TELEGRAM
# =====================

def get_chat_id_automatically():
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    data = r.json()

    results = data.get("result", [])
    if not results:
        raise RuntimeError("Chat ID bulunamadı. Önce Telegram’da Nuri23Bot’a /start yaz.")

    for upd in reversed(results):
        msg = upd.get("message") or upd.get("edited_message")
        if msg and "chat" in msg:
            return str(msg["chat"]["id"])

    raise RuntimeError("Chat ID bulunamadı. Bota normal mesaj olarak /start veya test yaz.")


def send_telegram(text):
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN missing")

    chat_id = get_chat_id_automatically()

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    payload = {
        "chat_id": chat_id,
        "text": text[:3900],
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }

    r = requests.post(url, json=payload, timeout=20)
    r.raise_for_status()
    return r.json()


# =====================
# BINANCE API
# =====================

def robust_get(path, params=None, retries=3):
    last_err = None

    for base in BASE_URLS:
        url = base + path

        for i in range(retries):
            try:
                r = session.get(url, params=params, timeout=20)

                if r.status_code in [418, 429]:
                    time.sleep(2 + i)
                    continue

                r.raise_for_status()
                return r.json()

            except Exception as e:
                last_err = e
                time.sleep(0.5 * (i + 1))

    raise RuntimeError(f"GET failed: {path} | {last_err}")


def is_bad_symbol(symbol, base):
    if base in EXCLUDE_BASES:
        return True

    if any(base.endswith(suf) for suf in EXCLUDE_SUFFIXES):
        return True

    if any(x in symbol for x in ["UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT"]):
        return True

    return False


def get_symbols():
    info = robust_get("/api/v3/exchangeInfo")
    rows = []

    for s in info["symbols"]:
        symbol = s.get("symbol")
        base = s.get("baseAsset")
        quote = s.get("quoteAsset")
        status = s.get("status")
        spot = s.get("isSpotTradingAllowed", False)

        if quote != "USDT":
            continue
        if status != "TRADING":
            continue
        if not spot:
            continue
        if is_bad_symbol(symbol, base):
            continue

        rows.append({"symbol": symbol, "base": base})

    return pd.DataFrame(rows)


def get_24h_volumes():
    data = robust_get("/api/v3/ticker/24hr")
    rows = []

    for x in data:
        try:
            rows.append({
                "symbol": x["symbol"],
                "quoteVolume": float(x.get("quoteVolume", 0))
            })
        except Exception:
            pass

    return pd.DataFrame(rows)


def get_universe():
    syms = get_symbols()
    vols = get_24h_volumes()

    df = syms.merge(vols, on="symbol", how="left")
    df["quoteVolume"] = df["quoteVolume"].fillna(0)

    df = df[df["quoteVolume"] >= MIN_QUOTE_VOLUME_USDT].copy()
    df = df.sort_values("quoteVolume", ascending=False).reset_index(drop=True)

    return df


def fetch_klines(symbol):
    params = {
        "symbol": symbol,
        "interval": INTERVAL,
        "limit": KLINE_LIMIT
    }

    data = robust_get("/api/v3/klines", params=params)

    cols = [
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades", "taker_base",
        "taker_quote", "ignore"
    ]

    df = pd.DataFrame(data, columns=cols)

    for c in ["open", "high", "low", "close", "volume", "quote_volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)

    df = df.dropna(subset=["open", "high", "low", "close"]).reset_index(drop=True)

    now = pd.Timestamp.now(tz="UTC")
    df = df[df["close_time"] < now].reset_index(drop=True)

    return df


# =====================
# STRATEJİ
# =====================

def confirmed_pivots(df):
    n = len(df)
    lows = df["low"].values
    highs = df["high"].values

    pl = np.full(n, np.nan)
    ph = np.full(n, np.nan)

    for i in range(PIVOT_LEFT, n - PIVOT_RIGHT):
        low_window = lows[i-PIVOT_LEFT:i+PIVOT_RIGHT+1]
        high_window = highs[i-PIVOT_LEFT:i+PIVOT_RIGHT+1]

        if lows[i] == np.min(low_window):
            pl[i + PIVOT_RIGHT] = lows[i]

        if highs[i] == np.max(high_window):
            ph[i + PIVOT_RIGHT] = highs[i]

    return pl, ph


def get_levels(df):
    pl, ph = confirmed_pivots(df)

    supports = []
    resistances = []

    for i in range(len(df)):
        if not np.isnan(pl[i]):
            supports.append(float(pl[i]))
            supports = supports[-LEVEL_LOOKBACK:]

        if not np.isnan(ph[i]):
            resistances.append(float(ph[i]))
            resistances = resistances[-LEVEL_LOOKBACK:]

    return supports, resistances


def nearest_support(supports, close):
    candidates = [s for s in supports if s <= close * (1 + TOUCH_TOL * 3)]
    if not candidates:
        return None
    return max(candidates)


def nearest_resistance_for_buy(resistances, close):
    candidates = [r for r in resistances if r >= close * (1 + MIN_TARGET_DISTANCE_PCT)]
    if not candidates:
        return None
    return min(candidates)


def nearest_rejected_resistance(resistances, high, close):
    candidates = []

    for r in resistances:
        touched = high >= r * (1 - RES_TOUCH_TOL)
        closed_below = close <= r * (1 - REJECT_CLOSE_BELOW_PCT)

        if touched and closed_below:
            candidates.append(r)

    if not candidates:
        return None

    return min(candidates, key=lambda x: abs(high / x - 1))


def is_fresh_4h_candle(last_close_time):
    now = pd.Timestamp.now(tz="UTC")
    diff_min = (now - last_close_time).total_seconds() / 60
    return 0 <= diff_min <= FRESH_CANDLE_MAX_MINUTES, diff_min


def is_buy_4h_candle(row, support, vol_ma):
    close = float(row["close"])
    open_ = float(row["open"])
    low = float(row["low"])
    qv = float(row["quote_volume"])

    touched = low <= support * (1 + TOUCH_TOL)
    reclaimed = close >= support * (1 + RECLAIM_PCT)
    green_or_reclaim = close >= open_ or close >= support * (1 + TOUCH_TOL / 2)

    volume_ok = True
    if USE_VOLUME_CONFIRM:
        volume_ok = vol_ma > 0 and qv >= vol_ma * VOL_MULT

    return touched and reclaimed and green_or_reclaim and volume_ok


def is_sell_rejection_4h_candle(row, resistance, vol_ma):
    open_ = float(row["open"])
    high = float(row["high"])
    close = float(row["close"])
    qv = float(row["quote_volume"])

    touched_resistance = high >= resistance * (1 - RES_TOUCH_TOL)
    closed_below_resistance = close <= resistance * (1 - REJECT_CLOSE_BELOW_PCT)

    red_candle = close < open_

    upper_wick = high - max(open_, close)
    upper_wick_pct = upper_wick / close if close > 0 else 0
    wick_ok = upper_wick_pct >= MIN_UPPER_WICK_PCT

    volume_ok = True
    if USE_VOLUME_CONFIRM:
        volume_ok = vol_ma > 0 and qv >= vol_ma * VOL_MULT

    return touched_resistance and closed_below_resistance and (red_candle or wick_ok) and volume_ok


def analyze_symbol(symbol, quote_volume):
    df = fetch_klines(symbol)

    if len(df) < 120:
        return None

    df = df.copy()
    df["vol_ma"] = df["quote_volume"].rolling(VOL_MA_LEN).mean()

    last = df.iloc[-1]
    last_close_time = last["close_time"]

    fresh, minutes_since_close = is_fresh_4h_candle(last_close_time)
    if not fresh:
        return None

    supports, resistances = get_levels(df)

    if len(supports) < 3 or len(resistances) < 3:
        return None

    close = float(last["close"])
    high = float(last["high"])
    last_vol_ma = float(last["vol_ma"]) if not pd.isna(last["vol_ma"]) else 0
    vol_ratio = float(last["quote_volume"]) / last_vol_ma if last_vol_ma > 0 else 0

    # =====================
    # ALIM: son kapanan 4H mum destekten tepki
    # =====================

    support = nearest_support(supports, close)

    if support is not None:
        resistance_for_buy = nearest_resistance_for_buy(resistances, close)

        if resistance_for_buy is not None:
            target = resistance_for_buy * (1 - SELL_BEFORE_RESISTANCE_PCT)
            stop = support * (1 - STOP_UNDER_SUPPORT_PCT)

            risk = close - stop
            reward = target - close

            if risk > 0 and reward > 0:
                rr = reward / risk

                if rr >= MIN_RR and is_buy_4h_candle(last, support, last_vol_ma):
                    near_support_pct = (close / support - 1) * 100
                    target_pct = (target / close - 1) * 100
                    stop_pct = (stop / close - 1) * 100

                    return {
                        "signal_type": "BUY_4H_SUPPORT_RECLAIM",
                        "symbol": symbol,
                        "quote_volume_24h": quote_volume,
                        "close": close,
                        "support": support,
                        "resistance": resistance_for_buy,
                        "target": target,
                        "stop": stop,
                        "rr": rr,
                        "near_support_pct": near_support_pct,
                        "target_pct": target_pct,
                        "stop_pct": stop_pct,
                        "vol_ratio": vol_ratio,
                        "trigger_time": str(last_close_time),
                        "minutes_since_close": minutes_since_close,
                    }

    # =====================
    # SATIŞ: son kapanan 4H mum dirençten red
    # =====================

    rejected_resistance = nearest_rejected_resistance(resistances, high, close)

    if rejected_resistance is not None and is_sell_rejection_4h_candle(last, rejected_resistance, last_vol_ma):
        lower_supports = [s for s in supports if s < close]
        nearest_down_support = max(lower_supports) if lower_supports else None

        reject_distance_pct = (close / rejected_resistance - 1) * 100

        if nearest_down_support is not None:
            downside_target_pct = (nearest_down_support / close - 1) * 100
        else:
            downside_target_pct = None

        invalidation = rejected_resistance * (1 + SELL_INVALIDATION_ABOVE_RES_PCT)
        invalidation_pct = (invalidation / close - 1) * 100

        return {
            "signal_type": "SELL_4H_RESISTANCE_REJECT",
            "symbol": symbol,
            "quote_volume_24h": quote_volume,
            "close": close,
            "support": nearest_down_support,
            "resistance": rejected_resistance,
            "target": nearest_down_support,
            "stop": invalidation,
            "rr": 0,
            "near_support_pct": 0,
            "target_pct": downside_target_pct if downside_target_pct is not None else 0,
            "stop_pct": invalidation_pct,
            "vol_ratio": vol_ratio,
            "reject_distance_pct": reject_distance_pct,
            "trigger_time": str(last_close_time),
            "minutes_since_close": minutes_since_close,
        }

    return None


def process_one(row):
    symbol = row["symbol"]
    quote_volume = float(row["quoteVolume"])

    try:
        sig = analyze_symbol(symbol, quote_volume)
        return sig, None

    except Exception as e:
        return None, f"{symbol}: {e}"


# =====================
# FORMAT
# =====================

def fmt_price(x):
    if x is None or pd.isna(x):
        return "Yok"

    x = float(x)

    if x >= 100:
        return f"{x:.2f}"
    if x >= 1:
        return f"{x:.4f}"
    if x >= 0.01:
        return f"{x:.5f}"

    return f"{x:.8f}"


def fmt_vol(x):
    x = float(x)

    if x >= 1_000_000_000:
        return f"{x/1_000_000_000:.2f}B"
    if x >= 1_000_000:
        return f"{x/1_000_000:.2f}M"

    return f"{x:.0f}"


def signal_line(row):
    signal_type = row.get("signal_type")

    if signal_type == "SELL_4H_RESISTANCE_REJECT":
        support = row.get("support")
        target = row.get("target")

        if support is None or pd.isna(support):
            support_text = "Yok"
            target_text = "Yok"
        else:
            support_text = fmt_price(support)
            target_text = f"{fmt_price(target)} ({row['target_pct']:.2f}%)"

        return (
            f"🔴 <b>{html.escape(row['symbol'])} | 4H DİRENÇTEN RED / SATIŞ UYARISI</b>\n"
            f"Fiyat: <code>{fmt_price(row['close'])}</code>\n"
            f"4H Direnç: <code>{fmt_price(row['resistance'])}</code>\n"
            f"Direnç altı kapanış: <b>{row['reject_distance_pct']:.2f}%</b>\n\n"
            f"📤 <b>UYARI</b>: Son kapanan 4H mum dirençten red verdi. Pozisyon varsa kâr alma/satış düşünülebilir.\n"
            f"Alt destek: <code>{support_text}</code>\n"
            f"Olası geri çekilme hedefi: <code>{target_text}</code>\n"
            f"Red bozulma seviyesi: <code>{fmt_price(row['stop'])}</code> (+{row['stop_pct']:.2f}%)\n\n"
            f"24h Vol: {fmt_vol(row['quote_volume_24h'])} | Vol ratio: {row['vol_ratio']:.2f}\n"
            f"4H Mum kapanışı: <code>{row['trigger_time']}</code>\n"
            f"Kapanıştan geçen süre: {row['minutes_since_close']:.1f} dk\n"
        )

    return (
        f"🟢 <b>{html.escape(row['symbol'])} | 4H DESTEK TEPKİ / ALIM UYARISI</b>\n"
        f"Fiyat: <code>{fmt_price(row['close'])}</code>\n"
        f"4H Destek: <code>{fmt_price(row['support'])}</code>\n"
        f"4H Direnç: <code>{fmt_price(row['resistance'])}</code>\n"
        f"TP: <code>{fmt_price(row['target'])}</code> ({row['target_pct']:.2f}%) | "
        f"SL: <code>{fmt_price(row['stop'])}</code> ({row['stop_pct']:.2f}%)\n"
        f"RR: <b>{row['rr']:.2f}</b> | Desteğe uzaklık: {row['near_support_pct']:.2f}%\n"
        f"24h Vol: {fmt_vol(row['quote_volume_24h'])} | Vol ratio: {row['vol_ratio']:.2f}\n"
        f"4H Mum kapanışı: <code>{row['trigger_time']}</code>\n"
        f"Kapanıştan geçen süre: {row['minutes_since_close']:.1f} dk\n"
    )


# =====================
# MAIN
# =====================

def main():
    started = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    universe = get_universe()
    print(f"Universe count: {len(universe)}")

    results = []
    errors = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = [ex.submit(process_one, row) for _, row in universe.iterrows()]

        for fut in as_completed(futures):
            sig, err = fut.result()

            if sig:
                results.append(sig)

            if err:
                errors.append(err)

    print(f"Signal count: {len(results)}")
    print(f"Error count: {len(errors)}")

    if errors:
        print("First errors:")
        for e in errors[:10]:
            print(e)

    if not results:
        print("Yeni 4H alım/satış sinyali yok. Telegram mesajı gönderilmedi.")
        return

    df = pd.DataFrame(results)

    df["signal_rank"] = df["signal_type"].map({
        "SELL_4H_RESISTANCE_REJECT": 0,
        "BUY_4H_SUPPORT_RECLAIM": 1
    }).fillna(9)

    df = df.sort_values(
        ["signal_rank", "rr", "quote_volume_24h"],
        ascending=[True, False, False]
    ).reset_index(drop=True)

    buy_count = int((df["signal_type"] == "BUY_4H_SUPPORT_RECLAIM").sum())
    sell_count = int((df["signal_type"] == "SELL_4H_RESISTANCE_REJECT").sum())

    parts = []
    parts.append("📊 <b>Yeni 4H Destek/Direnç Sinyali</b>")
    parts.append(f"Saat: {started}")
    parts.append(f"Filtre: Binance spot 24h vol ≥ {fmt_vol(MIN_QUOTE_VOLUME_USDT)} USDT")
    parts.append(f"🟢 4H Alım: {buy_count} | 🔴 4H Satış/Red: {sell_count}")
    parts.append("")

    for _, row in df.head(MAX_SIGNALS_TO_SEND).iterrows():
        parts.append(signal_line(row))

    msg = "\n".join(parts)
    send_telegram(msg)

    print(f"{len(df)} sinyal gönderildi.")


if __name__ == "__main__":
    main()
