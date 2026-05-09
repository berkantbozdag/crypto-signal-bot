import requests
import pandas as pd
import numpy as np
import time
from datetime import datetime

# ═══════════════════════════════════════════════
# TELEGRAM
# ═══════════════════════════════════════════════
import os

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")

# ═══════════════════════════════════════════════
# AYARLAR (V15)
# ═══════════════════════════════════════════════
LB1 = 30
LB2 = 80
LB3 = 100

VOL_MULTI = 1.0
VOL_LEN   = 20

EMA_LEN   = 100
EMA_FLT   = 50

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

# ═══════════════════════════════════════════════
# TELEGRAM SEND
# ═══════════════════════════════════════════════
def send_telegram(msg):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    payload = {
        "chat_id": CHAT_ID,
        "text": msg,
        "parse_mode": "HTML"
    }

    try:
        requests.post(url, json=payload, timeout=10)
    except:
        pass

# ═══════════════════════════════════════════════
# BINANCE SYMBOLS
# ═══════════════════════════════════════════════
def get_symbols():

    url = "https://api.binance.com/api/v3/exchangeInfo"

    data = requests.get(url, timeout=20).json()

    symbols = []

    for s in data["symbols"]:

        if s["quoteAsset"] != "USDT":
            continue

        if s["status"] != "TRADING":
            continue

        if s["contractType"] if "contractType" in s else False:
            continue

        name = s["symbol"]

        bad = [
            "UPUSDT",
            "DOWNUSDT",
            "BULLUSDT",
            "BEARUSDT"
        ]

        if any(x in name for x in bad):
            continue

        symbols.append(name)

    return symbols

# ═══════════════════════════════════════════════
# KLINES
# ═══════════════════════════════════════════════
def get_klines(symbol, limit=250):

    url = (
        f"https://api.binance.com/api/v3/klines?"
        f"symbol={symbol}&interval=3d&limit={limit}"
    )

    r = requests.get(url, timeout=20).json()

    if not isinstance(r, list):
        return None

    df = pd.DataFrame(r)

    df = df.iloc[:, :6]

    df.columns = [
        "time",
        "open",
        "high",
        "low",
        "close",
        "volume"
    ]

    for c in ["open","high","low","close","volume"]:
        df[c] = df[c].astype(float)

    return df

# ═══════════════════════════════════════════════
# ATR
# ═══════════════════════════════════════════════
def atr(df, period=14):

    high = df["high"]
    low  = df["low"]
    close = df["close"]

    tr1 = high - low
    tr2 = abs(high - close.shift())
    tr3 = abs(low - close.shift())

    tr = pd.concat([tr1,tr2,tr3], axis=1).max(axis=1)

    return tr.rolling(period).mean()

# ═══════════════════════════════════════════════
# CHECK TRIANGLE
# ═══════════════════════════════════════════════
def check_triangle(df, lb):

    if len(df) < lb + 20:
        return False

    close = df["close"]
    high  = df["high"]
    low   = df["low"]
    vol   = df["volume"]

    a = atr(df)

    ema100 = close.ewm(span=EMA_LEN).mean()
    ema50  = close.ewm(span=EMA_FLT).mean()

    volma = vol.rolling(VOL_LEN).mean()

    is_new = len(df) < BAR_THRESH

    t = max(lb // 3, 3)

    h1 = high.iloc[-t:].max()
    h2 = high.iloc[-(t*2):-t].max()
    h3 = high.iloc[-(t*3):-(t*2)].max()

    l1 = low.iloc[-t:].min()
    l2 = low.iloc[-(t*2):-t].min()
    l3 = low.iloc[-(t*3):-(t*2)].min()

    twoStepH = h1 < h2 and h1 < h3

    seqAscL  = l1 > l2 and l2 > l3
    twoStepL = l1 > l2 and l1 > l3

    flatMult = 1.5 if is_new else 2.5

    fL = abs(l1 - l3) < a.iloc[-1] * flatMult

    flatAll = (
        abs(l1 - l2) < a.iloc[-1] * flatMult and
        abs(l2 - l3) < a.iloc[-1] * flatMult
    )

    rNow = h1 - l1
    rOld = h3 - l3

    nPct = (
        ((rOld - rNow) / rOld) * 100
        if rOld != 0 else 0
    )

    conv = nPct >= MIN_NARROW and nPct <= MAX_NARROW

    sym  = twoStepH and (seqAscL or twoStepL) and conv
    desc = twoStepH and fL and flatAll and conv

    ok = sym or desc

    top = max(h1,h2,h3)
    bot = min(l1,l2,l3)

    triH = top - bot

    minH = MIN_TRIH_NEW if is_new else MIN_TRIH_OLD

    big = triH > a.iloc[-1] * minH

    if USE_TREND_FILTER:

        strictDesc = h1 < h2 and h2 < h3

        slope = ((h3 - h1) / h3) * 100 if h3 != 0 else 0

        slopeOK = slope >= MIN_SLOPE
        narrowOK = nPct >= MIN_NARROW_REAL

        trendOK = strictDesc and slopeOK and narrowOK

    else:
        trendOK = True

    sup = min(l1,l2,l3)

    tol_base = TOUCH_TOL_NEW if is_new else TOUCH_TOL_OLD

    tol = (
        sup * tol_base
        if is_new
        else sup * tol_base * max(lb / 50.0, 1.0)
    )

    touches = 0

    for x in low.iloc[-lb:]:

        if abs(x - sup) <= tol:
            touches += 1

    valid = (
        ok and
        big and
        touches >= 2 and
        trendOK
    )

    last_close = close.iloc[-1]
    prev_close = close.iloc[-2]

    brk1 = valid and last_close > h1 and prev_close <= h1
    brk2 = valid and last_close > h2 and prev_close <= h2 and not brk1

    vol_ok = (
        vol.iloc[-1] > volma.iloc[-1] * VOL_MULTI
        if not np.isnan(volma.iloc[-1])
        else True
    )

    ema_ok = (
        last_close > ema50.iloc[-1]
        if USE_EMA_FILTER
        else True
    )

    signal = (brk1 or brk2) and vol_ok and ema_ok

    return signal

# ═══════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════
symbols = get_symbols()

results = []

for symbol in symbols:

    try:

        df = get_klines(symbol)

        if df is None:
            continue

        sig30  = check_triangle(df, LB1)
        sig80  = check_triangle(df, LB2)
        sig100 = check_triangle(df, LB3)

        if sig30 or sig80 or sig100:

            price = round(df["close"].iloc[-1], 6)

            vol_usdt = (
                df["close"].iloc[-1] *
                df["volume"].iloc[-1]
            )

            sig_text = []

            if sig30:
                sig_text.append("AL 30")

            if sig80:
                sig_text.append("AL 90")

            if sig100:
                sig_text.append("AL 100")

            msg = (
                f"🚨 <b>{symbol}</b>\n\n"
                f"🎯 {' | '.join(sig_text)}\n"
                f"💰 Price: {price}\n"
                f"📊 Vol: ${vol_usdt:,.0f}\n"
                f"⏰ 3D Breakout"
            )

            print(msg)

            send_telegram(msg)

            time.sleep(1)

    except Exception as e:
        print(symbol, e)

print("DONE")
