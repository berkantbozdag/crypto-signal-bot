import os
import time
import requests
from datetime import datetime, timezone

# =========================================================
# RETEST RADAR SCANNER V2
# 3D structure + 4H retest/reclaim + BTC relative strength + orderbook
# Binance Spot USDT pairs -> Telegram alert
# =========================================================

BASE_URLS = [
    "https://data-api.binance.vision",
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
]

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# Ana filtreler
MIN_QUOTE_VOLUME_USDT = float(os.getenv("MIN_QUOTE_VOLUME_USDT", "7000000"))

# 4H ayarları
INTERVAL_4H = "4h"
KLINE_LIMIT_4H = int(os.getenv("KLINE_LIMIT_4H", "140"))
RESISTANCE_LOOKBACK_4H = int(os.getenv("RESISTANCE_LOOKBACK_4H", "36"))
VOL_RATIO_MIN_4H = float(os.getenv("VOL_RATIO_MIN_4H", "1.5"))

# ÖNEMLİ FİLTRE:
# Fiyat retest bölgesinin üstünden çok kaçtıysa sinyal atmasın.
# LAYER gibi %50 yukarı kaçmış coinleri eler.
MAX_EXTENSION_FROM_RETEST_PCT = float(os.getenv("MAX_EXTENSION_FROM_RETEST_PCT", "12.0"))

# 3D ayarları
INTERVAL_3D = "3d"
KLINE_LIMIT_3D = int(os.getenv("KLINE_LIMIT_3D", "90"))
MIN_3D_BARS = int(os.getenv("MIN_3D_BARS", "15"))

# BTC'ye göre güç
RS_MIN_ADVANTAGE = float(os.getenv("RS_MIN_ADVANTAGE", "3.0"))

# Orderbook ayarları
DEPTH_LIMIT = int(os.getenv("DEPTH_LIMIT", "500"))
BOOK_BAND_PCT = float(os.getenv("BOOK_BAND_PCT", "0.02"))
MAX_SPREAD_PCT = float(os.getenv("MAX_SPREAD_PCT", "0.25"))
BID_ASK_RATIO_MIN = float(os.getenv("BID_ASK_RATIO_MIN", "1.35"))
MIN_WALL_USDT = float(os.getenv("MIN_WALL_USDT", "20000"))
WALL_MAX_DISTANCE_PCT = float(os.getenv("WALL_MAX_DISTANCE_PCT", "0.025"))

MAX_ALERTS = int(os.getenv("MAX_ALERTS", "12"))
REQUEST_SLEEP = float(os.getenv("REQUEST_SLEEP", "0.05"))

STABLE_BASES = {
    "USDC", "FDUSD", "TUSD", "BUSD", "DAI", "USDP", "USDS",
    "EUR", "TRY", "BRL", "GBP", "AUD", "RUB", "UAH"
}

BAD_SUFFIXES = (
    "UP", "DOWN", "BULL", "BEAR", "3L", "3S", "5L", "5S"
)


def now_utc_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def fnum(x, default=0.0):
    try:
        return float(x)
    except Exception:
        return default


def get_json(path, params=None, timeout=12):
    last_err = None

    for base in BASE_URLS:
        try:
            r = requests.get(base + path, params=params, timeout=timeout)

            if r.status_code == 200:
                return r.json()

            last_err = f"{r.status_code}: {r.text[:160]}"

        except Exception as e:
            last_err = str(e)

    raise RuntimeError(f"GET failed {path}: {last_err}")


def send_telegram(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram secrets missing. Message below:")
        print(text)
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    try:
        r = requests.post(url, data=payload, timeout=15)

        if r.status_code != 200:
            print("Telegram error:", r.status_code, r.text[:300])
        else:
            print("Telegram sent.")

    except Exception as e:
        print("Telegram exception:", e)


def is_bad_symbol(symbol):
    if not symbol.endswith("USDT"):
        return True

    base = symbol[:-4]

    if base in STABLE_BASES:
        return True

    for suffix in BAD_SUFFIXES:
        if base.endswith(suffix):
            return True

    return False


def get_trading_spot_symbols():
    data = get_json("/api/v3/exchangeInfo")
    symbols = set()

    for s in data.get("symbols", []):
        symbol = s.get("symbol", "")

        if s.get("status") != "TRADING":
            continue

        if s.get("quoteAsset") != "USDT":
            continue

        if s.get("isSpotTradingAllowed") is False:
            continue

        if is_bad_symbol(symbol):
            continue

        symbols.add(symbol)

    return symbols


def get_24h_candidates(trading_symbols):
    data = get_json("/api/v3/ticker/24hr")
    out = []

    for t in data:
        symbol = t.get("symbol", "")

        if symbol not in trading_symbols:
            continue

        quote_volume = fnum(t.get("quoteVolume"))

        if quote_volume < MIN_QUOTE_VOLUME_USDT:
            continue

        out.append({
            "symbol": symbol,
            "last_price": fnum(t.get("lastPrice")),
            "quote_volume": quote_volume,
            "price_change_pct": fnum(t.get("priceChangePercent")),
            "high_24h": fnum(t.get("highPrice")),
            "low_24h": fnum(t.get("lowPrice")),
        })

    out.sort(key=lambda x: x["quote_volume"], reverse=True)
    return out


def get_klines(symbol, interval, limit):
    raw = get_json("/api/v3/klines", {
        "symbol": symbol,
        "interval": interval,
        "limit": limit,
    })

    now_ms = int(time.time() * 1000)
    bars = []

    for k in raw:
        close_time = int(k[6])

        # Açık mumu kullanma
        if close_time > now_ms:
            continue

        bars.append({
            "open_time": int(k[0]),
            "open": fnum(k[1]),
            "high": fnum(k[2]),
            "low": fnum(k[3]),
            "close": fnum(k[4]),
            "volume": fnum(k[5]),
            "close_time": close_time,
            "quote_volume": fnum(k[7]),
        })

    return bars


def sma(values):
    clean = [x for x in values if x is not None]

    if not clean:
        return 0.0

    return sum(clean) / len(clean)


def pct_change(start, end):
    if start <= 0:
        return 0.0

    return (end - start) / start * 100


def calc_atr(bars, length=14):
    if len(bars) < length + 2:
        return 0.0

    trs = []

    for i in range(-length, 0):
        high = bars[i]["high"]
        low = bars[i]["low"]
        prev_close = bars[i - 1]["close"]

        tr = max(
            high - low,
            abs(high - prev_close),
            abs(low - prev_close)
        )

        trs.append(tr)

    return sma(trs)


def analyze_3d_structure(bars3d):
    if len(bars3d) < MIN_3D_BARS:
        return None

    last = bars3d[-1]
    close = last["close"]

    if close <= 0:
        return None

    recent = bars3d[-12:] if len(bars3d) >= 12 else bars3d
    wider = bars3d[-30:] if len(bars3d) >= 30 else bars3d

    recent_high = max(b["high"] for b in recent)
    recent_low = min(b["low"] for b in recent)

    wider_high = max(b["high"] for b in wider)
    wider_low = min(b["low"] for b in wider)

    recent_range_pct = (recent_high - recent_low) / close * 100
    wider_range_pct = (wider_high - wider_low) / close * 100

    qvs = [b["quote_volume"] for b in bars3d[-12:-2]]
    avg_qv = sma(qvs)
    last_qv = last["quote_volume"]

    vol_ratio_3d = last_qv / avg_qv if avg_qv > 0 else 0

    # 3D major direnç:
    # Son 2 mumu hariç tutuyoruz, güncel pump direnci bozmasın.
    if len(bars3d) >= 34:
        resistance_pool = bars3d[-32:-2]
    else:
        resistance_pool = bars3d[:-2]

    if not resistance_pool:
        return None

    major_resistance = max(b["high"] for b in resistance_pool)

    if major_resistance <= 0:
        return None

    dist_to_major_res_pct = (close - major_resistance) / major_resistance * 100

    score = 0
    reasons = []

    score += 1
    reasons.append(f"{len(bars3d)} adet 3D mum")

    if recent_range_pct < wider_range_pct * 0.75:
        score += 1
        reasons.append("3D sıkışma var")

    if abs(dist_to_major_res_pct) <= 10:
        score += 2
        reasons.append("3D major direnç yakın")

    if close >= major_resistance * 0.98:
        score += 1
        reasons.append("3D direnç bölgesinde")

    if vol_ratio_3d >= 1.2:
        score += 1
        reasons.append(f"3D hacim {vol_ratio_3d:.1f}x")

    ok = score >= 3

    return {
        "ok": ok,
        "score": score,
        "reasons": reasons,
        "major_resistance_3d": major_resistance,
        "dist_to_major_res_pct": dist_to_major_res_pct,
        "vol_ratio_3d": vol_ratio_3d,
        "recent_range_pct": recent_range_pct,
    }


def analyze_4h_retest(bars4h):
    if len(bars4h) < max(50, RESISTANCE_LOOKBACK_4H + 8):
        return None

    last = bars4h[-1]
    prev = bars4h[-2]

    close = last["close"]
    low = last["low"]

    if close <= 0:
        return None

    # Son 5 mumu dışarıda bırakıyoruz.
    # Amaç: yeni pump mumlarını direnç hesabına sokmamak.
    resistance_pool = bars4h[-(RESISTANCE_LOOKBACK_4H + 5):-5]

    if not resistance_pool:
        return None

    resistance = max(b["high"] for b in resistance_pool)
    support = min(b["low"] for b in resistance_pool)

    if resistance <= 0:
        return None

    atr = calc_atr(bars4h, 14)

    if atr <= 0:
        atr = close * 0.015

    # Retest bölgesi:
    # Eski direnç etrafında ATR bazlı dinamik bant.
    min_buffer = resistance * 0.006
    max_buffer = resistance * 0.035
    buffer = max(min_buffer, min(atr * 0.65, max_buffer))

    retest_low = resistance - buffer
    retest_high = resistance + buffer * 0.45
    reclaim_level = retest_high
    invalid_level = retest_low - buffer * 0.45

    dist_to_res_pct = (close - resistance) / resistance * 100

    # Yeni filtre:
    # Fiyat retest üst bandından fazla uzaklaştıysa sinyal atma.
    extension_from_retest_pct = (
        (close - retest_high) / retest_high * 100
        if retest_high > 0
        else 999
    )

    too_extended = extension_from_retest_pct > MAX_EXTENSION_FROM_RETEST_PCT
    below_invalid = close < invalid_level

    # 4H hacim oranı
    prev_qvs = [b["quote_volume"] for b in bars4h[-24:-2]]
    avg_qv = sma(prev_qvs)

    vol_ratio = last["quote_volume"] / avg_qv if avg_qv > 0 else 0

    # Son 8 mumda kırılım denemesi var mı?
    last8 = bars4h[-8:]
    broke_recently = any(b["close"] > resistance * 1.002 for b in last8)

    # Retest bölgesine temas var mı?
    touched_retest = low <= retest_high and close >= retest_low

    # Reclaim var mı?
    reclaim = close >= reclaim_level

    # Retest tuttu mu?
    retest_hold = touched_retest and close >= resistance * 0.995

    green = close > last["open"]
    close_up = close >= prev["close"] * 0.995

    score = 0
    reasons = []

    if vol_ratio >= VOL_RATIO_MIN_4H:
        score += 2
        reasons.append(f"4H hacim {vol_ratio:.1f}x")

    if abs(dist_to_res_pct) <= 5:
        score += 1
        reasons.append("4H direnç çevresi")

    if broke_recently:
        score += 1
        reasons.append("son mumlarda kırılım denemesi")

    if reclaim:
        score += 2
        reasons.append("reclaim üstünde")
    elif retest_hold:
        score += 2
        reasons.append("retest tutuyor")

    if green or close_up:
        score += 1
        reasons.append("mum toparlıyor")

    if close > support:
        score += 1
        reasons.append("destek üstünde")

    if too_extended:
        reasons.append(f"fazla kaçmış +{extension_from_retest_pct:.1f}%")

    if below_invalid:
        reasons.append("iptal altında")

    ok = (
        vol_ratio >= VOL_RATIO_MIN_4H
        and (reclaim or retest_hold or broke_recently)
        and not too_extended
        and not below_invalid
        and score >= 5
    )

    return {
        "ok": ok,
        "score": score,
        "reasons": reasons,
        "close": close,
        "resistance": resistance,
        "support": support,
        "atr": atr,
        "retest_low": retest_low,
        "retest_high": retest_high,
        "reclaim_level": reclaim_level,
        "invalid_level": invalid_level,
        "dist_to_res_pct": dist_to_res_pct,
        "extension_from_retest_pct": extension_from_retest_pct,
        "too_extended": too_extended,
        "below_invalid": below_invalid,
        "vol_ratio": vol_ratio,
        "last_qv": last["quote_volume"],
    }


def analyze_relative_strength(symbol_bars, btc_bars):
    if len(symbol_bars) < 43 or len(btc_bars) < 43:
        return None

    coin_start = symbol_bars[-43]["close"]
    coin_end = symbol_bars[-1]["close"]

    btc_start = btc_bars[-43]["close"]
    btc_end = btc_bars[-1]["close"]

    coin_7d = pct_change(coin_start, coin_end)
    btc_7d = pct_change(btc_start, btc_end)

    advantage = coin_7d - btc_7d

    score = 0
    reasons = []

    if advantage >= RS_MIN_ADVANTAGE:
        score += 2
        reasons.append(f"BTC'ye göre güçlü +{advantage:.1f}%")

    if coin_7d > 0 and btc_7d <= coin_7d:
        score += 1
        reasons.append("7g relatif güç pozitif")

    if coin_7d > 10:
        score += 1
        reasons.append(f"coin 7g %{coin_7d:.1f}")

    ok = advantage >= RS_MIN_ADVANTAGE

    return {
        "ok": ok,
        "score": score,
        "reasons": reasons,
        "coin_7d": coin_7d,
        "btc_7d": btc_7d,
        "advantage": advantage,
    }


def get_orderbook(symbol):
    data = get_json("/api/v3/depth", {
        "symbol": symbol,
        "limit": DEPTH_LIMIT,
    })

    bids = [
        (fnum(price), fnum(qty))
        for price, qty in data.get("bids", [])
        if fnum(price) > 0 and fnum(qty) > 0
    ]

    asks = [
        (fnum(price), fnum(qty))
        for price, qty in data.get("asks", [])
        if fnum(price) > 0 and fnum(qty) > 0
    ]

    if not bids or not asks:
        return None

    best_bid = bids[0][0]
    best_ask = asks[0][0]
    mid = (best_bid + best_ask) / 2

    if mid <= 0:
        return None

    spread_pct = (best_ask - best_bid) / mid * 100

    bid_floor = mid * (1 - BOOK_BAND_PCT)
    ask_ceiling = mid * (1 + BOOK_BAND_PCT)

    bid_zone = [
        (p, q, p * q)
        for p, q in bids
        if bid_floor <= p <= mid
    ]

    ask_zone = [
        (p, q, p * q)
        for p, q in asks
        if mid <= p <= ask_ceiling
    ]

    bid_depth = sum(x[2] for x in bid_zone)
    ask_depth = sum(x[2] for x in ask_zone)

    ratio = bid_depth / ask_depth if ask_depth > 0 else 99.0

    max_bid_wall = max(bid_zone, key=lambda x: x[2]) if bid_zone else (0, 0, 0)
    max_ask_wall = max(ask_zone, key=lambda x: x[2]) if ask_zone else (0, 0, 0)

    bid_wall_price, _, bid_wall_usdt = max_bid_wall
    ask_wall_price, _, ask_wall_usdt = max_ask_wall

    bid_wall_dist_pct = (
        (mid - bid_wall_price) / mid * 100
        if bid_wall_price > 0
        else 999
    )

    ask_wall_dist_pct = (
        (ask_wall_price - mid) / mid * 100
        if ask_wall_price > 0
        else 999
    )

    return {
        "best_bid": best_bid,
        "best_ask": best_ask,
        "mid": mid,
        "spread_pct": spread_pct,
        "bid_depth": bid_depth,
        "ask_depth": ask_depth,
        "ratio": ratio,
        "bid_wall_price": bid_wall_price,
        "bid_wall_usdt": bid_wall_usdt,
        "bid_wall_dist_pct": bid_wall_dist_pct,
        "ask_wall_price": ask_wall_price,
        "ask_wall_usdt": ask_wall_usdt,
        "ask_wall_dist_pct": ask_wall_dist_pct,
    }


def analyze_orderbook(book, quote_volume):
    if not book:
        return None

    dynamic_min_wall = max(MIN_WALL_USDT, quote_volume * 0.00020)

    score = 0
    reasons = []

    if book["spread_pct"] <= MAX_SPREAD_PCT:
        score += 2
        reasons.append(f"spread düşük %{book['spread_pct']:.3f}")

    if book["ratio"] >= BID_ASK_RATIO_MIN:
        score += 2
        reasons.append(f"bid/ask {book['ratio']:.2f}x")

    if book["bid_wall_usdt"] >= dynamic_min_wall:
        score += 2
        reasons.append(f"alış duvarı ${book['bid_wall_usdt']:,.0f}")

    if book["bid_wall_dist_pct"] <= WALL_MAX_DISTANCE_PCT * 100:
        score += 1
        reasons.append(f"alış duvarı yakın %{book['bid_wall_dist_pct']:.2f}")

    if book["bid_wall_usdt"] > book["ask_wall_usdt"] * 1.15:
        score += 1
        reasons.append("alış duvarı satıştan büyük")

    ok = (
        book["spread_pct"] <= MAX_SPREAD_PCT
        and book["ratio"] >= BID_ASK_RATIO_MIN
        and score >= 5
    )

    return {
        "ok": ok,
        "score": score,
        "reasons": reasons,
        "dynamic_min_wall": dynamic_min_wall,
    }


def fmt_usd(x):
    if x >= 1_000_000:
        return f"{x / 1_000_000:.1f}M"

    if x >= 1_000:
        return f"{x / 1_000:.1f}K"

    return f"{x:.0f}"


def fmt_price(x):
    if x >= 100:
        return f"{x:.2f}"

    if x >= 1:
        return f"{x:.4f}"

    if x >= 0.01:
        return f"{x:.5f}"

    return f"{x:.8f}"


def build_alert(item):
    symbol = item["symbol"]

    tech3d = item["tech3d"]
    tech4h = item["tech4h"]
    rs = item["rs"]
    book = item["book"]
    ob = item["ob"]

    total_score = (
        tech3d["score"]
        + tech4h["score"]
        + rs["score"]
        + ob["score"]
    )

    text = (
        f"<b>{symbol} takip uyarısı</b>\n"
        f"Skor: <b>{total_score}</b> | 24h vol: <b>{fmt_usd(item['quote_volume'])} USDT</b>\n"
        f"Fiyat: <b>{fmt_price(tech4h['close'])}</b> | 24h: <b>{item['price_change_pct']:.2f}%</b>\n\n"

        f"<b>Retest planı</b>\n"
        f"4H direnç: <b>{fmt_price(tech4h['resistance'])}</b>\n"
        f"Retest bölgesi: <b>{fmt_price(tech4h['retest_low'])} - {fmt_price(tech4h['retest_high'])}</b>\n"
        f"Reclaim seviyesi: <b>{fmt_price(tech4h['reclaim_level'])} üstü 4H kapanış</b>\n"
        f"İptal: <b>{fmt_price(tech4h['invalid_level'])} altı 4H kapanış</b>\n"
        f"Retest üstünden uzaklık: <b>{tech4h['extension_from_retest_pct']:.1f}%</b>\n\n"

        f"<b>3D yapı:</b> {', '.join(tech3d['reasons'])}\n"
        f"3D major direnç: <b>{fmt_price(tech3d['major_resistance_3d'])}</b> "
        f"({tech3d['dist_to_major_res_pct']:.1f}%)\n\n"

        f"<b>4H trigger:</b> {', '.join(tech4h['reasons'])}\n"
        f"4H hacim oranı: <b>{tech4h['vol_ratio']:.2f}x</b>\n\n"

        f"<b>Relative strength:</b> {', '.join(rs['reasons'])}\n"
        f"Coin 7g: <b>{rs['coin_7d']:.1f}%</b> | BTC 7g: <b>{rs['btc_7d']:.1f}%</b>\n\n"

        f"<b>Orderbook:</b> {', '.join(ob['reasons'])}\n"
        f"Bid depth ±%2: <b>{fmt_usd(book['bid_depth'])}</b> | Ask depth ±%2: <b>{fmt_usd(book['ask_depth'])}</b>\n"
        f"Yakın alış duvarı: <b>{fmt_price(book['bid_wall_price'])}</b> / <b>{fmt_usd(book['bid_wall_usdt'])}</b>\n"
        f"Yakın satış duvarı: <b>{fmt_price(book['ask_wall_price'])}</b> / <b>{fmt_usd(book['ask_wall_usdt'])}</b>\n\n"

        f"Yorum: <b>Takip edilebilir setup.</b> Retest bölgesine yakın değilse piyasa emriyle atlama."
    )

    return text


def main():
    print(f"Started: {now_utc_str()}")

    btc4h = get_klines("BTCUSDT", INTERVAL_4H, KLINE_LIMIT_4H)
    print(f"BTC 4H bars: {len(btc4h)}")

    trading_symbols = get_trading_spot_symbols()
    print(f"Trading USDT spot symbols: {len(trading_symbols)}")

    candidates = get_24h_candidates(trading_symbols)
    print(f"Volume candidates >= {MIN_QUOTE_VOLUME_USDT:,.0f}: {len(candidates)}")

    stage_hits = []

    for c in candidates:
        symbol = c["symbol"]

        try:
            bars3d = get_klines(symbol, INTERVAL_3D, KLINE_LIMIT_3D)
            tech3d = analyze_3d_structure(bars3d)

            if not tech3d or not tech3d["ok"]:
                time.sleep(REQUEST_SLEEP)
                continue

            bars4h = get_klines(symbol, INTERVAL_4H, KLINE_LIMIT_4H)
            tech4h = analyze_4h_retest(bars4h)

            if not tech4h or not tech4h["ok"]:
                time.sleep(REQUEST_SLEEP)
                continue

            rs = analyze_relative_strength(bars4h, btc4h)

            if not rs or not rs["ok"]:
                time.sleep(REQUEST_SLEEP)
                continue

            c["tech3d"] = tech3d
            c["tech4h"] = tech4h
            c["rs"] = rs

            stage_hits.append(c)

            print(
                f"TECH HIT {symbol}: "
                f"3D={tech3d['score']} "
                f"4H={tech4h['score']} "
                f"RS={rs['advantage']:.1f}% "
                f"EXT={tech4h['extension_from_retest_pct']:.1f}%"
            )

            time.sleep(REQUEST_SLEEP)

        except Exception as e:
            print(f"Tech error {symbol}: {e}")
            time.sleep(REQUEST_SLEEP)

    print(f"Tech + RS hits: {len(stage_hits)}")

    final_hits = []

    for c in stage_hits:
        symbol = c["symbol"]

        try:
            book = get_orderbook(symbol)
            ob = analyze_orderbook(book, c["quote_volume"])

            if ob and ob["ok"]:
                c["book"] = book
                c["ob"] = ob

                c["total_score"] = (
                    c["tech3d"]["score"]
                    + c["tech4h"]["score"]
                    + c["rs"]["score"]
                    + ob["score"]
                )

                final_hits.append(c)

                print(
                    f"FINAL HIT {symbol}: "
                    f"score={c['total_score']} "
                    f"bidask={book['ratio']:.2f}x "
                    f"ext={c['tech4h']['extension_from_retest_pct']:.1f}%"
                )

            time.sleep(REQUEST_SLEEP)

        except Exception as e:
            print(f"Orderbook error {symbol}: {e}")
            time.sleep(REQUEST_SLEEP)

    final_hits.sort(
        key=lambda x: (
            x.get("total_score", 0),
            -x["tech4h"]["extension_from_retest_pct"],
            x["tech4h"]["vol_ratio"],
            x["rs"]["advantage"],
            x["quote_volume"],
        ),
        reverse=True,
    )

    if not final_hits:
        msg = (
            f"Retest Radar çalıştı.\n"
            f"Saat: {now_utc_str()}\n"
            f"Volume aday: {len(candidates)}\n"
            f"Teknik + RS aday: {len(stage_hits)}\n"
            f"Net sinyal: 0"
        )

        print(msg)
        return

    final_hits = final_hits[:MAX_ALERTS]

    header = (
        f"<b>3D + 4H Retest Radar Scanner V2</b>\n"
        f"Saat: {now_utc_str()}\n"
        f"Net sinyal: <b>{len(final_hits)}</b>\n\n"
    )

    messages = [build_alert(x) for x in final_hits]
    full_msg = header + "\n\n----------------------\n\n".join(messages)

    if len(full_msg) <= 3900:
        send_telegram(full_msg)
    else:
        send_telegram(header)

        for m in messages:
            send_telegram(m)
            time.sleep(0.8)

    print("Done.")


if __name__ == "__main__":
    main()
