#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Triangle Break V15 GitHub Scanner
- Kaynak mantık: kullanıcının gönderdiği Triangle Break V14 Pine kodu
- Ayarlar: telefondaki Triangle Break V15 ekran ayarları
- Binance Spot USDT 3G tarama + Telegram mesajı

GitHub Secrets:
  TELEGRAM_BOT_TOKEN
  TELEGRAM_CHAT_ID

Opsiyonel env:
  INTERVAL=3d
  KLINE_LIMIT=500
  LIVE_CANDLE=true
  MIN_QUOTE_VOLUME=7000000
  SCAN_LOW_VOLUME=true
  MAX_SYMBOLS=0
  ALERT_RECENT_BARS=2
"""

from __future__ import annotations

import os
import sys
import time
import math
import json
import traceback
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple, Any

import requests
import pandas as pd
import numpy as np


# ═══════════════════════════════════════════════
# TELEFONDAKİ V15 AYARLARI
# ═══════════════════════════════════════════════

@dataclass
class Settings:
    # Ortak
    lb1: int = 30
    lb2: int = 80
    lb3: int = 100
    volMulti: float = 1.0
    volLen: int = 20
    coolBars: int = 40
    emaLen: int = 100
    retestBars: int = 10
    pivotScan: int = 10
    minTouches: int = 2
    maxNarrow: float = 70.0
    minNarrow: float = 0.0
    barThresh: int = 150

    # Eski coin
    minTriH_old: float = 0.5
    touchTol_old: float = 0.05
    pvtValLen: int = 8
    useSMAf_old: bool = False
    smaFlen_old: int = 30

    # Yeni coin
    minTriH_new: float = 0.5
    touchTol_new: float = 3.0       # foto: 3. Kod aşağıda 3 => %3 diye normalize eder.
    useSMAf_new: bool = False
    smaFlen_new: int = 30

    # V15 filtreleri
    useUpperTrendlineFilter: bool = True
    useMidLongEmaFilter: bool = True
    midLongEmaLen: int = 50
    minUpperSlopePct: float = 3.0
    minRealNarrowPct: float = 20.0
    minBreakZonePct: float = 0.0    # 0 = kapalı


CFG = Settings()


# ═══════════════════════════════════════════════
# ENV / GENEL
# ═══════════════════════════════════════════════

BASE_URL = "https://data-api.binance.vision"
INTERVAL = os.getenv("INTERVAL", "3d")
KLINE_LIMIT = int(os.getenv("KLINE_LIMIT", "500"))
LIVE_CANDLE = os.getenv("LIVE_CANDLE", "true").lower() == "true"
MIN_QUOTE_VOLUME = float(os.getenv("MIN_QUOTE_VOLUME", "7000000"))
SCAN_LOW_VOLUME = os.getenv("SCAN_LOW_VOLUME", "true").lower() == "true"
MAX_SYMBOLS = int(os.getenv("MAX_SYMBOLS", "0"))  # 0 = sınırsız
ALERT_RECENT_BARS = int(os.getenv("ALERT_RECENT_BARS", "2"))
REQUEST_SLEEP = float(os.getenv("REQUEST_SLEEP", "0.06"))

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

STABLE_OR_FIAT_BASES = {
    "USDC", "FDUSD", "TUSD", "BUSD", "DAI", "USDP", "PAX", "PYUSD",
    "USDE", "SUSDE", "USDS", "USD1", "AEUR", "EURI",
    "EUR", "TRY", "BRL", "AUD", "GBP", "RUB", "UAH"
}


# ═══════════════════════════════════════════════
# YARDIMCI FONKSİYONLAR
# ═══════════════════════════════════════════════

def log(msg: str) -> None:
    print(msg, flush=True)


def http_get(path: str, params: Optional[dict] = None, timeout: int = 20) -> Any:
    url = BASE_URL + path
    last_err = None
    for attempt in range(3):
        try:
            r = requests.get(url, params=params, timeout=timeout)
            if r.status_code == 429:
                time.sleep(1.5 + attempt)
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last_err = e
            time.sleep(0.5 + attempt)
    raise RuntimeError(f"GET failed: {url} {params} err={last_err}")


def telegram_send(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log("Telegram secrets yok; mesaj konsola yazıldı.")
        log(text)
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    chunks = split_message(text, 3900)
    for chunk in chunks:
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": chunk,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        r = requests.post(url, json=payload, timeout=20)
        if not r.ok:
            log(f"Telegram hata: {r.status_code} {r.text[:500]}")
        time.sleep(0.4)


def split_message(text: str, max_len: int) -> List[str]:
    lines = text.splitlines()
    out, cur = [], ""
    for line in lines:
        if len(cur) + len(line) + 1 > max_len:
            if cur:
                out.append(cur)
            cur = line
        else:
            cur += ("\n" if cur else "") + line
    if cur:
        out.append(cur)
    return out


def fmt_num(x: Optional[float]) -> str:
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return "-"
    x = float(x)
    if x >= 100:
        return f"{x:.2f}"
    if x >= 1:
        return f"{x:.4f}"
    if x >= 0.01:
        return f"{x:.5f}"
    return f"{x:.8f}"


def fmt_money(x: float) -> str:
    if x >= 1_000_000_000:
        return f"{x/1_000_000_000:.2f}B"
    if x >= 1_000_000:
        return f"{x/1_000_000:.1f}M"
    if x >= 1_000:
        return f"{x/1_000:.1f}K"
    return f"{x:.0f}"


def normalize_tolerance(v: float) -> float:
    """
    Pine V14'te 0.05 = %5 gibi kullanılıyordu.
    V15 telefonda [YENİ] Touch Tolerance '%' değeri 3 görünüyor.
    Bu yüzden:
      0.05 -> 0.05 (%5)
      3    -> 0.03 (%3)
    """
    return v / 100.0 if v >= 1 else v


# ═══════════════════════════════════════════════
# BINANCE DATA
# ═══════════════════════════════════════════════

def get_usdt_symbols() -> Tuple[List[str], Dict[str, float]]:
    info = http_get("/api/v3/exchangeInfo")
    tickers = http_get("/api/v3/ticker/24hr")
    quote_vol: Dict[str, float] = {}
    for t in tickers:
        try:
            quote_vol[t["symbol"]] = float(t.get("quoteVolume", 0.0))
        except Exception:
            pass

    symbols = []
    for s in info.get("symbols", []):
        symbol = s.get("symbol", "")
        base = s.get("baseAsset", "")
        quote = s.get("quoteAsset", "")
        status = s.get("status", "")
        is_spot = bool(s.get("isSpotTradingAllowed", False))
        if status != "TRADING" or not is_spot:
            continue
        if quote != "USDT":
            continue
        if base in STABLE_OR_FIAT_BASES:
            continue
        qv = quote_vol.get(symbol, 0.0)
        if not SCAN_LOW_VOLUME and qv < MIN_QUOTE_VOLUME:
            continue
        symbols.append(symbol)

    symbols.sort(key=lambda x: quote_vol.get(x, 0.0), reverse=True)
    if MAX_SYMBOLS > 0:
        symbols = symbols[:MAX_SYMBOLS]
    return symbols, quote_vol


def fetch_klines(symbol: str, interval: str = INTERVAL, limit: int = KLINE_LIMIT) -> Optional[pd.DataFrame]:
    raw = http_get("/api/v3/klines", {"symbol": symbol, "interval": interval, "limit": limit})
    if not raw or len(raw) < 120:
        # yeni coinleri kaçırmamak için 15 bar altını at, ama 120 şartını kaldırıyoruz
        if not raw or len(raw) < 15:
            return None

    cols = [
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades", "taker_base",
        "taker_quote", "ignore"
    ]
    df = pd.DataFrame(raw, columns=cols)
    num_cols = ["open", "high", "low", "close", "volume", "quote_volume"]
    for c in num_cols:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
    df = df.dropna(subset=["open", "high", "low", "close", "volume"]).reset_index(drop=True)

    if not LIVE_CANDLE:
        now = pd.Timestamp.utcnow()
        df = df[df["close_time"] <= now].reset_index(drop=True)

    if len(df) < 15:
        return None
    return df


# ═══════════════════════════════════════════════
# INDIKATÖR HESAPLARI
# ═══════════════════════════════════════════════

def add_indicators(df: pd.DataFrame, cfg: Settings) -> pd.DataFrame:
    df = df.copy()
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)

    # TradingView ta.atr ~ Wilder RMA
    df["atr"] = tr.ewm(alpha=1/14, adjust=False, min_periods=14).mean()
    df["volMA"] = df["volume"].rolling(cfg.volLen, min_periods=1).mean()
    df["ema"] = df["close"].ewm(span=cfg.emaLen, adjust=False).mean()
    df["midLongEMA"] = df["close"].ewm(span=cfg.midLongEmaLen, adjust=False).mean()
    df["sma_old"] = df["close"].rolling(cfg.smaFlen_old, min_periods=1).mean()
    df["sma_new"] = df["close"].rolling(cfg.smaFlen_new, min_periods=1).mean()
    return df


def pivots(df: pd.DataFrame, left_right: int) -> Tuple[List[Tuple[int, int, float]], List[Tuple[int, int, float]]]:
    """
    Pine ta.pivothigh(high, L, L):
      pivot bar j, L bar sonra yani event_index=j+L'de bilinir.
    return: [(event_index, pivot_bar_index, price), ...]
    """
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    n = len(df)
    ph, pl = [], []
    L = left_right

    for j in range(L, n - L):
        win_h = highs[j-L:j+L+1]
        win_l = lows[j-L:j+L+1]
        if np.isfinite(highs[j]) and highs[j] == np.max(win_h):
            # tek pivot olsun diye eşit yüksek varsa merkezi tercih etme
            if list(win_h).count(highs[j]) == 1:
                ph.append((j + L, j, float(highs[j])))
        if np.isfinite(lows[j]) and lows[j] == np.min(win_l):
            if list(win_l).count(lows[j]) == 1:
                pl.append((j + L, j, float(lows[j])))
    return ph, pl


def get_recent_pivot_arrays(
    pivot_events: List[Tuple[int, int, float]],
    i: int,
    max_items: int = 15
) -> Tuple[List[int], List[float]]:
    arr = [(bar_i, price) for event_i, bar_i, price in pivot_events if event_i <= i]
    arr = arr[-max_items:]
    return [x[0] for x in arr], [x[1] for x in arr]


def has_real_triangle(
    i: int,
    max_bars: int,
    hi_events: List[Tuple[int, int, float]],
    lo_events: List[Tuple[int, int, float]],
    atr: float,
) -> bool:
    hiB, hiP = get_recent_pivot_arrays(hi_events, i, 15)
    loB, loP = get_recent_pivot_arrays(lo_events, i, 15)
    szH, szL = len(hiP), len(loP)

    valid = False
    if szH >= 3 and szL >= 2 and np.isfinite(atr):
        lhCnt = 0
        prv = None
        start_h = max(szH - 5, 0)
        for k in range(start_h, szH):
            bi = hiB[k]
            if i - bi <= max_bars:
                pi = hiP[k]
                if prv is not None and pi < prv:
                    lhCnt += 1
                prv = pi

        hlCnt = 0
        prvL = None
        flatOK = True
        start_l = max(szL - 5, 0)
        for k in range(start_l, szL):
            bi = loB[k]
            if i - bi <= max_bars:
                pi = loP[k]
                if prvL is not None:
                    if pi > prvL:
                        hlCnt += 1
                    if abs(pi - prvL) > atr * 4:
                        flatOK = False
                prvL = pi

        isFlat = flatOK
        if szL >= 2:
            fL = loP[max(szL - 5, 0)]
            lL = loP[szL - 1]
            if abs(lL - fL) > atr * 3:
                isFlat = False

        converging = False
        if szH >= 2 and szL >= 2:
            fH = hiP[max(szH - 5, 0)]
            lH = hiP[szH - 1]
            fLo = loP[max(szL - 5, 0)]
            lLo = loP[szL - 1]
            oldGap = fH - fLo
            newGap = lH - lLo
            if oldGap > 0 and newGap > 0 and newGap < oldGap:
                converging = True

        isSym = lhCnt >= 2 and hlCnt >= 1 and converging
        isDesc = lhCnt >= 2 and isFlat and converging
        valid = isSym or isDesc

    return bool(valid)


def highest(arr: np.ndarray, start: int, end: int) -> float:
    if start < 0 or end < start:
        return np.nan
    return float(np.nanmax(arr[start:end+1]))


def lowest(arr: np.ndarray, start: int, end: int) -> float:
    if start < 0 or end < start:
        return np.nan
    return float(np.nanmin(arr[start:end+1]))


def check_tri_at(df: pd.DataFrame, i: int, lb: int, cfg: Settings) -> Tuple[bool, float, Dict[str, float]]:
    """
    Pine checkTri(lb) karşılığı.
    """
    t = max(int(math.floor(lb / 3)), 3)
    if i - 3 * t + 1 < 0 or i - lb + 1 < 0:
        return False, np.nan, {}

    high_arr = df["high"].to_numpy()
    low_arr = df["low"].to_numpy()
    close_arr = df["close"].to_numpy()
    open_arr = df["open"].to_numpy()

    atr = float(df.at[i, "atr"])
    if not np.isfinite(atr) or atr <= 0:
        return False, np.nan, {}

    # Pine:
    # h1 = highest(high, t)
    # h2 = highest(high[t], t)
    # h3 = highest(high[t*2], t)
    h1 = highest(high_arr, i - t + 1, i)
    h2 = highest(high_arr, i - 2*t + 1, i - t)
    h3 = highest(high_arr, i - 3*t + 1, i - 2*t)
    l1 = lowest(low_arr, i - t + 1, i)
    l2 = lowest(low_arr, i - 2*t + 1, i - t)
    l3 = lowest(low_arr, i - 3*t + 1, i - 2*t)

    if any(not np.isfinite(x) for x in [h1, h2, h3, l1, l2, l3]):
        return False, np.nan, {}

    isNewCoin = i < cfg.barThresh

    dH = (h1 < h2) or (h1 < h3)
    dH2 = h1 < h3
    aL = (l1 > l2) or (l1 > l3)

    flatMult = 1.5 if isNewCoin else (1.5 if lb <= 60 else 4.0)
    fL = abs(l1 - l3) < atr * flatMult

    rNow = h1 - l1
    rOld = h3 - l3
    nPct = ((rOld - rNow) / rOld * 100.0) if rOld != 0 else 0.0

    if isNewCoin:
        conv = cfg.minNarrow <= nPct <= cfg.maxNarrow
    else:
        conv = (cfg.minNarrow <= nPct <= cfg.maxNarrow) if lb <= 60 else True

    sym = dH and aL and conv
    desc = dH2 and fL and conv
    ok = sym or desc

    # V15 foto: Üst Trendline Filtresi açık
    # Gerçek üçgen için h3 > h2 > h1, minimum üst eğim ve minimum daralma.
    strict_desc = h3 > h2 > h1
    slope_pct = ((h3 - h1) / h3 * 100.0) if h3 > 0 else 0.0
    real_narrow_ok = nPct >= cfg.minRealNarrowPct

    if cfg.useUpperTrendlineFilter:
        ok = ok and strict_desc and slope_pct >= cfg.minUpperSlopePct and real_narrow_ok

    top = max(h1, h2, h3)
    bot = min(l1, l2, l3)
    tH = top - bot
    minH = cfg.minTriH_new if isNewCoin else cfg.minTriH_old
    big = tH > atr * minH

    sup = min(l1, l2, l3)
    tol_base = normalize_tolerance(cfg.touchTol_new if isNewCoin else cfg.touchTol_old)
    tol = sup * tol_base if isNewCoin else sup * tol_base * max(lb / 50.0, 1.0)

    tch = 0
    for j in range(i - lb + 1, i + 1):
        if abs(float(low_arr[j]) - sup) <= tol:
            tch += 1

    if isNewCoin:
        aboveAvg = (close_arr[i] > df.at[i, "sma_new"]) if cfg.useSMAf_new else True
    else:
        aboveAvg = (close_arr[i] > df.at[i, "sma_old"]) if cfg.useSMAf_old else True

    # Min kırılım zonu: 0 ise kapalı. Açılırsa, mum üçgenin fazla üstünden kopmuşsa engeller.
    break_zone_ok = True
    if cfg.minBreakZonePct > 0 and h2 > 0:
        break_zone_ok = ((close_arr[i] - h2) / h2 * 100.0) >= cfg.minBreakZonePct

    valid = ok and big and tch >= cfg.minTouches and aboveAvg and break_zone_ok

    # brk için önceki bar h1/h2 değerleri
    prev_h1 = prev_h2 = np.nan
    if i - 1 - 3 * t + 1 >= 0:
        prev_h1 = highest(high_arr, (i-1) - t + 1, i-1)
        prev_h2 = highest(high_arr, (i-1) - 2*t + 1, (i-1) - t)

    brk1 = bool(valid and close_arr[i] > h1 and close_arr[i-1] <= prev_h1) if i > 0 and np.isfinite(prev_h1) else False
    brk2 = bool(valid and close_arr[i] > h2 and close_arr[i-1] <= prev_h2 and not brk1) if i > 0 and np.isfinite(prev_h2) else False

    meta = {
        "h1": h1, "h2": h2, "h3": h3,
        "l1": l1, "l2": l2, "l3": l3,
        "nPct": nPct,
        "slopePct": slope_pct,
        "touches": float(tch),
        "support": sup,
        "triHeightATR": tH / atr if atr > 0 else np.nan,
    }
    return (brk1 or brk2), h1, meta


def scan_symbol(df: pd.DataFrame, symbol: str, quote_vol_24h: float, cfg: Settings) -> List[Dict[str, Any]]:
    df = add_indicators(df, cfg)
    hi_events, lo_events = pivots(df, cfg.pvtValLen)

    signals: List[Dict[str, Any]] = []

    cnt = 999
    pending = False
    pendingBar = 0
    pendingLow = np.nan
    pendingHH = np.nan

    retestPending = False
    retestStart = 0
    retestLevel = np.nan
    retestDipped = False

    for i in range(len(df)):
        if i < max(cfg.lb3, cfg.volLen, cfg.emaLen // 2, 15):
            cnt += 1
            continue

        cnt += 1
        isNewCoin = i < cfg.barThresh

        shortBreak, shortHH, sMeta = check_tri_at(df, i, cfg.lb1, cfg)
        midBreak, midHH, mMeta = check_tri_at(df, i, cfg.lb2, cfg)
        longBreak, longHH, lMeta = check_tri_at(df, i, cfg.lb3, cfg)

        atr_i = float(df.at[i, "atr"])

        sPivotOK = True if isNewCoin else has_real_triangle(i, cfg.lb1 * 3, hi_events, lo_events, atr_i)
        mPivotOK = True if isNewCoin else has_real_triangle(i, cfg.lb2 * 2, hi_events, lo_events, atr_i)
        lPivotOK = True if isNewCoin else has_real_triangle(i, cfg.lb3 * 2, hi_events, lo_events, atr_i)

        volOK = (df.at[i, "volume"] > df.at[i, "volMA"] * cfg.volMulti) or cfg.volMulti <= 1.0

        # V15 foto: Mid/Long EMA filtresi açık, periyot 50
        mid_long_ema_ok = True
        if cfg.useMidLongEmaFilter:
            mid_long_ema_ok = df.at[i, "close"] > df.at[i, "midLongEMA"]

        sBreak = shortBreak and volOK and sPivotOK
        mBreak = midBreak and volOK and mPivotOK and (not sBreak) and mid_long_ema_ok
        lBreak = longBreak and volOK and lPivotOK and (not sBreak) and (not mBreak) and mid_long_ema_ok

        midDirect = mBreak and cnt >= cfg.coolBars
        longDirect = lBreak and cnt >= cfg.coolBars and not midDirect

        if midDirect or longDirect:
            cnt = 0

        doRetest = True if isNewCoin else False

        if sBreak and not pending:
            pending = True
            pendingBar = i
            pendingLow = float(df.at[i, "low"])
            pendingHH = float(shortHH)

        normalConfirm = False
        if pending and (not doRetest) and (i - pendingBar) >= 2:
            stayedUp = df.at[i-1, "low"] >= pendingLow and df.at[i, "low"] >= pendingLow
            anyGreen = (df.at[i-1, "close"] > df.at[i-1, "open"]) or (df.at[i, "close"] > df.at[i, "open"])
            if stayedUp and anyGreen:
                normalConfirm = True
            pending = False

        if pending and doRetest and (i - pendingBar) >= 2:
            retestPending = True
            retestStart = i
            retestLevel = pendingHH
            retestDipped = False
            pending = False

        retestConfirm = False
        if retestPending:
            if df.at[i, "low"] <= retestLevel * 1.02:
                retestDipped = True

            if retestDipped and df.at[i, "close"] > retestLevel and df.at[i, "close"] > df.at[i, "open"]:
                retestConfirm = True
                retestPending = False

            if (i - retestStart) >= cfg.retestBars:
                if (not retestDipped) and df.at[i, "close"] > retestLevel:
                    retestConfirm = True
                retestPending = False

            if df.at[i, "close"] < retestLevel * 0.90:
                retestPending = False

        shortConfirmed = (normalConfirm or retestConfirm) and cnt >= cfg.coolBars
        if shortConfirmed:
            cnt = 0

        finalBreak = shortConfirmed or longDirect or midDirect

        if finalBreak:
            if shortConfirmed:
                sig_type = "AL30"
                meta = sMeta
                level = pendingHH if np.isfinite(pendingHH) else shortHH
            elif midDirect:
                sig_type = "AL80"
                meta = mMeta
                level = midHH
            else:
                sig_type = "AL100"
                meta = lMeta
                level = longHH

            # Hedefler: pivot peak + ATH/fib kabaca
            targets = compute_targets(df, i, cfg)
            signals.append({
                "symbol": symbol,
                "time": df.at[i, "open_time"],
                "bar_index": i,
                "bars_from_last": len(df) - 1 - i,
                "type": sig_type,
                "new_coin": isNewCoin,
                "close": float(df.at[i, "close"]),
                "level": float(level) if np.isfinite(level) else None,
                "ema": float(df.at[i, "ema"]),
                "midLongEMA": float(df.at[i, "midLongEMA"]),
                "vol": float(df.at[i, "quote_volume"]),
                "vol24h": float(quote_vol_24h),
                "vol_ratio": float(df.at[i, "volume"] / df.at[i, "volMA"]) if df.at[i, "volMA"] else 0.0,
                "nPct": float(meta.get("nPct", np.nan)),
                "slopePct": float(meta.get("slopePct", np.nan)),
                "touches": int(meta.get("touches", 0)),
                "targets": targets,
            })

    return signals


def compute_targets(df: pd.DataFrame, i: int, cfg: Settings) -> Dict[str, Optional[float]]:
    """
    Pine hedeflerinin pratik scanner karşılığı:
    son pivot high'lardan close üstündekileri + ATH/Fib.
    """
    left = right = cfg.pivotScan
    highs = df["high"].to_numpy()
    close = float(df.at[i, "close"])
    atr = float(df.at[i, "atr"]) if np.isfinite(df.at[i, "atr"]) else 0.0

    peaks = []
    for j in range(left, i - right + 1):
        win = highs[j-left:j+right+1]
        if highs[j] == np.max(win) and list(win).count(highs[j]) == 1:
            p = float(highs[j])
            if p > close * 1.05:
                if not peaks or abs(p - peaks[-1]) > atr:
                    peaks.append(p)
    peaks = peaks[-20:]

    uniq = []
    for p in peaks:
        if p > close * 1.05 and all(abs(p - q) >= atr * 2 for q in uniq):
            uniq.append(p)
    uniq = sorted(uniq)[:5]

    ath = float(np.nanmax(highs[:i+1])) if i >= 0 else None
    out = {
        "t1": uniq[0] if len(uniq) > 0 else None,
        "t2": uniq[1] if len(uniq) > 1 else None,
        "t3": uniq[2] if len(uniq) > 2 else None,
        "ath": ath,
        "fib_1_272": ath * 1.272 if ath else None,
        "fib_1_618": ath * 1.618 if ath else None,
    }
    return out


# ═══════════════════════════════════════════════
# MESAJ
# ═══════════════════════════════════════════════

def build_message(all_signals: List[Dict[str, Any]], scanned: int, errors: int) -> str:
    now = pd.Timestamp.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    recent = [s for s in all_signals if s["bars_from_last"] <= ALERT_RECENT_BARS]
    recent.sort(key=lambda x: (x["vol24h"], x["vol"]), reverse=True)

    if not recent:
        return (
            f"🔎 <b>Triangle Break V15 Scanner</b>\n"
            f"{now}\n"
            f"3G tarama bitti. Yeni sinyal yok.\n"
            f"Taranan: {scanned} | Hata: {errors}\n"
            f"Ayar: AL30/AL80/AL100 | EMA100 | Mid/Long EMA50 | Cooldown 40"
        )

    lines = [
        "🚨 <b>Triangle Break V15 Sinyal</b>",
        f"{now}",
        f"Taranan: {scanned} | Hata: {errors}",
        "",
    ]

    for s in recent[:40]:
        vol_tag = "✅" if s["vol24h"] >= MIN_QUOTE_VOLUME else "⚠️ düşük hacim"
        mode = "NEW" if s["new_coin"] else "OLD"
        t = s["targets"]
        tv = f"https://www.tradingview.com/chart/?symbol=BINANCE:{s['symbol']}"

        lines.append(
            f"<b>{s['symbol']}</b> | {s['type']} | {mode} | {vol_tag}\n"
            f"Close: {fmt_num(s['close'])} | 24h Vol: {fmt_money(s['vol24h'])} USDT | VolRatio: {s['vol_ratio']:.2f}x\n"
            f"Daralma: {s['nPct']:.1f}% | Üst eğim: {s['slopePct']:.1f}% | Touch: {s['touches']}\n"
            f"Hedef: {fmt_num(t.get('t1'))} / {fmt_num(t.get('t2'))} | ATH: {fmt_num(t.get('ath'))}\n"
            f"{tv}\n"
        )

    if len(recent) > 40:
        lines.append(f"... +{len(recent)-40} sinyal daha")

    return "\n".join(lines)


# ═══════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════

def main() -> int:
    log("Triangle Break V15 scanner başlıyor...")
    log(f"Ayar: interval={INTERVAL} limit={KLINE_LIMIT} live={LIVE_CANDLE} scan_low_vol={SCAN_LOW_VOLUME}")

    symbols, quote_vol = get_usdt_symbols()
    log(f"Sembol sayısı: {len(symbols)}")

    all_signals: List[Dict[str, Any]] = []
    scanned = 0
    errors = 0

    for idx, sym in enumerate(symbols, 1):
        try:
            df = fetch_klines(sym)
            if df is None:
                continue
            sigs = scan_symbol(df, sym, quote_vol.get(sym, 0.0), CFG)
            if sigs:
                all_signals.extend(sigs)
                last = sigs[-1]
                if last["bars_from_last"] <= ALERT_RECENT_BARS:
                    log(f"[{idx}/{len(symbols)}] SIGNAL {sym} {last['type']} barsAgo={last['bars_from_last']}")
            scanned += 1
            time.sleep(REQUEST_SLEEP)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            errors += 1
            log(f"Hata {sym}: {e}")
            if os.getenv("DEBUG", "false").lower() == "true":
                traceback.print_exc()
            time.sleep(0.2)

    msg = build_message(all_signals, scanned, errors)
    telegram_send(msg)

    # GitHub log için json özet
    recent = [s for s in all_signals if s["bars_from_last"] <= ALERT_RECENT_BARS]
    log(json.dumps({
        "scanned": scanned,
        "errors": errors,
        "recent_signals": len(recent),
        "symbols": [s["symbol"] for s in recent[:30]]
    }, ensure_ascii=False, default=str))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
