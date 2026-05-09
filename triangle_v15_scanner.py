import requests
import pandas as pd
import numpy as np
import time
import os

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

BASE_URL = "https://data-api.binance.vision/api/v3"

LB1 = 30
LB2 = 80
LB3 = 100

VOL_MULTI = 1.0
VOL_LEN = 20

EMA_FLT = 50
COOL_BARS = 40
BAR_THRESH = 150

MIN_TRIH_OLD = 0.5
MIN_TRIH_NEW = 0.5

TOUCH_TOL_OLD = 0.05
TOUCH_TOL_NEW = 3.0

MAX_NARROW = 70
MIN_NARROW = 0

MIN_SLOPE = 3.0
MIN_NARROW_REAL = 20

USE_EMA_FILTER = True
USE_TREND_FILTER = True


def send_telegram(msg):
    if not BOT_TOKEN or not CHAT_ID:
        print("TELEGRAM SECRET MISSING")
        return

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": msg,
        "parse_mode": "HTML"
    }

    try:
        r = requests.post(url, json=payload, timeout=20)
        print("TELEGRAM:", r.status_code, r.text[:120])
    except Exception as e:
        print("TELEGRAM ERROR:", e)


def get_symbols():
    url = f"{BASE_URL}/exchangeInfo"

    try:
        r = requests.get(url, timeout=30)
        data = r.json()

        if "symbols" not in data:
            print("BINANCE VISION API ERROR")
            print(data)
            return []

        symbols = []

        blacklist_exact = {
            "USDCUSDT",
            "FDUSDUSDT",
            "TUSDUSDT",
            "BUSDUSDT",
            "DAIUSDT",
            "EURUSDT",
            "TRYUSDT",
            "USDPUSDT",
            "PAXUSDT"
        }

        blacklist_contains = [
            "UPUSDT",
            "DOWNUSDT",
            "BULLUSDT",
            "BEARUSDT"
        ]

        for s in data["symbols"]:
            name = s.get("symbol", "")

            if s.get("quoteAsset") != "USDT":
                continue

            if s.get("status") != "TRADING":
                continue

            if name in blacklist_exact:
                continue

            if any(x in name for x in blacklist_contains):
                continue

            symbols.append(name)

        return symbols

    except Exception as e:
        print("GET SYMBOL ERROR:", e)
        return []


def get_klines(symbol, limit=250):
    url = f"{BASE_URL}/klines?symbol={symbol}&interval=3d&limit={limit}"

    try:
        r = requests.get(url, timeout=30)
        data = r.json()

        if not isinstance(data, list):
            print(symbol, "KLINE ERROR:", data)
            return None

        if len(data) < 60:
            return None

        df = pd.DataFrame(data)
        df = df.iloc[:, :6]
        df.columns = ["time", "open", "high", "low", "close", "volume"]

        for c in ["open", "high", "low", "close", "volume"]:
            df[c] = df[c].astype(float)

        return df

    except Exception as e:
        print(symbol, "GET KLINE ERROR:", e)
        return None


def calc_atr(df, period=14):
    high = df["high"]
    low = df["low"]
    close = df["close"]

    tr1 = high - low
    tr2 = abs(high - close.shift())
    tr3 = abs(low - close.shift())

    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    return tr.rolling(period).mean()


def check_triangle(df, lb):
    if len(df) < lb + 20:
        return False

    close = df["close"]
    high = df["high"]
    low = df["low"]
    volume = df["volume"]

    atr = calc_atr(df)

    if np.isnan(atr.iloc[-1]):
        return False

    ema_filter = close.ewm(span=EMA_FLT, adjust=False).mean()
    vol_ma = volume.rolling(VOL_LEN).mean()

    is_new = len(df) < BAR_THRESH

    t = max(lb // 3, 3)

    h1 = high.iloc[-t:].max()
    h2 = high.iloc[-(t * 2):-t].max()
    h3 = high.iloc[-(t * 3):-(t * 2)].max()

    l1 = low.iloc[-t:].min()
    l2 = low.iloc[-(t * 2):-t].min()
    l3 = low.iloc[-(t * 3):-(t * 2)].min()

    two_step_h = h1 < h2 and h1 < h3
    seq_asc_l = l1 > l2 and l2 > l3
    two_step_l = l1 > l2 and l1 > l3

    flat_mult = 1.5 if is_new else 2.5

    flat_l = abs(l1 - l3) < atr.iloc[-1] * flat_mult
    flat_all = (
        abs(l1 - l2) < atr.iloc[-1] * flat_mult and
        abs(l2 - l3) < atr.iloc[-1] * flat_mult
    )

    r_now = h1 - l1
    r_old = h3 - l3

    n_pct = ((r_old - r_now) / r_old) * 100 if r_old != 0 else 0

    conv = n_pct >= MIN_NARROW and n_pct <= MAX_NARROW

    sym = two_step_h and (seq_asc_l or two_step_l) and conv
    desc = two_step_h and flat_l and flat_all and conv

    ok = sym or desc

    top = max(h1, h2, h3)
    bot = min(l1, l2, l3)

    tri_h = top - bot
    min_h = MIN_TRIH_NEW if is_new else MIN_TRIH_OLD
    big = tri_h > atr.iloc[-1] * min_h

    if USE_TREND_FILTER:
        strict_desc = h1 < h2 and h2 < h3
        slope = ((h3 - h1) / h3) * 100 if h3 != 0 else 0
        slope_ok = slope >= MIN_SLOPE
        narrow_ok = n_pct >= MIN_NARROW_REAL
        trend_ok = strict_desc and slope_ok and narrow_ok
    else:
        trend_ok = True

    sup = min(l1, l2, l3)

    tol_base = TOUCH_TOL_NEW if is_new else TOUCH_TOL_OLD
    tol = sup * tol_base if is_new else sup * tol_base * max(lb / 50.0, 1.0)

    touches = 0
    for x in low.iloc[-lb:]:
        if abs(x - sup) <= tol:
            touches += 1

    valid = ok and big and touches >= 2 and trend_ok

    last_close = close.iloc[-1]
    prev_close = close.iloc[-2]

    brk1 = valid and last_close > h1 and prev_close <= h1
    brk2 = valid and last_close > h2 and prev_close <= h2 and not brk1

    if np.isnan(vol_ma.iloc[-1]):
        vol_ok = True
    else:
        vol_ok = volume.iloc[-1] > vol_ma.iloc[-1] * VOL_MULTI or VOL_MULTI <= 1.0

    ema_ok = last_close > ema_filter.iloc[-1] if USE_EMA_FILTER else True

    return (brk1 or brk2) and vol_ok and ema_ok


def main():
    symbols = get_symbols()

    print("SYMBOL COUNT:", len(symbols))

    if not symbols:
        send_telegram("⚠️ Triangle V15 Scanner: Binance Vision sembol listesi alınamadı.")
        print("DONE")
        return

    found = []

    for symbol in symbols:
        try:
            df = get_klines(symbol)

            if df is None:
                continue

            sig30 = check_triangle(df, LB1)
            sig80 = check_triangle(df, LB2)
            sig100 = check_triangle(df, LB3)

            if sig30 or sig80 or sig100:
                price = df["close"].iloc[-1]
                vol_usdt = df["close"].iloc[-1] * df["volume"].iloc[-1]

                sig_text = []

                if sig30:
                    sig_text.append("AL 30")

                if sig80:
                    sig_text.append("AL 90")

                if sig100:
                    sig_text.append("AL 100")

                found.append({
                    "symbol": symbol,
                    "signals": " | ".join(sig_text),
                    "price": price,
                    "volume": vol_usdt
                })

            time.sleep(0.08)

        except Exception as e:
            print(symbol, "ERROR:", e)

    if not found:
        print("NO SIGNAL")
        print("DONE")
        return

    found = sorted(found, key=lambda x: x["volume"], reverse=True)

    lines = []
    lines.append("🚨 <b>Triangle V15 3D Signals</b>")
    lines.append("")

    for x in found[:30]:
        lines.append(
            f"<b>{x['symbol']}</b> — {x['signals']} — "
            f"Vol: ${x['volume']:,.0f} — Price: {x['price']:.8f}"
        )

    msg = "\n".join(lines)

    print(msg)
    send_telegram(msg)

    print("DONE")


if __name__ == "__main__":
    main()
