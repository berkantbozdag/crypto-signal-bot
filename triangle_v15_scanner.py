# triangle_scanner.py
# Binance 3D Triangle Breakout Scanner
# LIVE + CLOSED + PREV candle detection
# Telegram alert supported via GitHub Secrets:
# TELEGRAM_BOT_TOKEN
# TELEGRAM_CHAT_ID

import os
import time
import math
import html
import requests
from typing import Dict, List, Optional, Tuple


# =========================
# AYARLAR
# =========================

BINANCE_BASE = os.getenv("BINANCE_BASE", "https://data-api.binance.vision")

INTERVAL = "3d"
KLINE_LIMIT = 220

# Mevcut kapanmamış mum kontrol edilsin mi?
INCLUDE_LIVE_CANDLE = True

# Son kapanmış 3D mum kontrol edilsin mi?
INCLUDE_LAST_CLOSED_CANDLE = True

# 1 önceki kapanmış 3D mum kontrol edilsin mi?
INCLUDE_PREV_CLOSED_CANDLE = True

# Binance spot 24h quote volume referans eşiği
MIN_SERIOUS_24H_QUOTE_VOLUME = 7_000_000

# Düşük hacimli sinyalleri tamamen elemek istersen False yap.
# Şimdilik kullanıcı tercihi: eleme, sadece LOWVOL işaretle.
SHOW_LOW_VOLUME_SIGNALS = True

# Pivot ayarları
PIVOT_LEFT = 3
PIVOT_RIGHT = 3
PIVOT_LOOKBACK = 130

# Triangle minimum bar sayısı
MIN_BARS_REQUIRED = 35

# Direnç kırılım tamponu
# 0.003 = %0.3 üstü kırılım ister
BREAK_BUFFER_PCT = 0.003

# LIVE mumda fitil direnci geçerse sinyal kabul edilsin mi?
# Mum içi sinyali yakalamak için True.
LIVE_WICK_TRIGGER = True

# CLOSED/PREV mumda kapanışla onay iste
CLOSED_NEEDS_CLOSE_ABOVE = True

# Triangle geçerlilik toleransları
# Direnç çizgisi hafif yukarı eğimli olabilir mi?
MAX_RESISTANCE_RISING_PCT = 0.08

# Destek çizgisi hafif aşağı eğimli olabilir mi?
MAX_SUPPORT_FALLING_PCT = 0.08

# Üçgen daralması minimum oranı
# 0.05 = en az %5 daralma
MIN_NARROWING_RATIO = 0.05

# Çok uçuk fake kırılımları elemek için
# Sinyal mumu dirençten max %40 yukarıda olsun
MAX_EXTENSION_ABOVE_RESISTANCE = 0.40

# Telegram mesajında maksimum coin sayısı
MAX_ALERT_ROWS = 60

# Stabil / fiat / wrapped stable dışlama
EXCLUDED_BASES = {
    "USDC", "FDUSD", "TUSD", "BUSD", "DAI", "USDP", "PAX", "PYUSD",
    "USDE", "SUSDE", "USDS", "USD1", "AEUR", "EURI",
    "EUR", "TRY", "BRL", "AUD", "GBP", "RUB", "UAH",
    "WBTC", "WETH"
}


# =========================
# HTTP
# =========================

def get_json(url: str, params: Optional[dict] = None, retries: int = 3, sleep_sec: float = 0.8):
    last_err = None

    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, timeout=20)
            if r.status_code == 429:
                time.sleep(2 + attempt)
                continue

            r.raise_for_status()
            return r.json()

        except Exception as e:
            last_err = e
            time.sleep(sleep_sec * (attempt + 1))

    raise RuntimeError(f"GET failed: {url} params={params} err={last_err}")


# =========================
# BINANCE DATA
# =========================

def get_spot_usdt_symbols() -> List[str]:
    url = f"{BINANCE_BASE}/api/v3/exchangeInfo"
    data = get_json(url)

    symbols = []

    for item in data.get("symbols", []):
        symbol = item.get("symbol", "")
        base = item.get("baseAsset", "")
        quote = item.get("quoteAsset", "")
        status = item.get("status", "")

        if status != "TRADING":
            continue

        if quote != "USDT":
            continue

        if base in EXCLUDED_BASES:
            continue

        # Leveraged token benzeri şeyleri dışla
        if any(x in base for x in ["UP", "DOWN", "BULL", "BEAR"]):
            continue

        symbols.append(symbol)

    return sorted(set(symbols))


def get_24h_quote_volumes() -> Dict[str, float]:
    url = f"{BINANCE_BASE}/api/v3/ticker/24hr"
    data = get_json(url)

    volumes = {}

    for item in data:
        symbol = item.get("symbol")
        if not symbol or not symbol.endswith("USDT"):
            continue

        try:
            volumes[symbol] = float(item.get("quoteVolume", 0))
        except Exception:
            volumes[symbol] = 0.0

    return volumes


def get_klines(symbol: str) -> List[dict]:
    url = f"{BINANCE_BASE}/api/v3/klines"
    raw = get_json(
        url,
        params={
            "symbol": symbol,
            "interval": INTERVAL,
            "limit": KLINE_LIMIT,
        }
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


# =========================
# FORMAT
# =========================

def fmt_money(x: float) -> str:
    if x >= 1_000_000_000:
        return f"{x / 1_000_000_000:.2f}B"
    if x >= 1_000_000:
        return f"{x / 1_000_000:.2f}M"
    if x >= 1_000:
        return f"{x / 1_000:.1f}K"
    return f"{x:.0f}"


def pct(x: float) -> str:
    return f"{x * 100:.2f}%"


def safe_div(a: float, b: float) -> float:
    if b == 0:
        return 0.0
    return a / b


# =========================
# PIVOT / LINE
# =========================

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


def collect_pivots_before_index(rows: List[dict], signal_idx: int) -> Tuple[List[Tuple[int, float]], List[Tuple[int, float]]]:
    """
    Sinyal mumundan önceki pivotları toplar.
    LIVE mum repaint riskini azaltmak için sinyal mumunun kendisini pivot hesabına katmaz.
    """
    start = max(PIVOT_LEFT, signal_idx - PIVOT_LOOKBACK)
    end = signal_idx - 1

    pivot_highs = []
    pivot_lows = []

    for i in range(start, end + 1):
        # Sağ taraf pivot onayı için signal_idx'e taşmamalı
        if i + PIVOT_RIGHT >= signal_idx:
            continue

        if is_pivot_high(rows, i, PIVOT_LEFT, PIVOT_RIGHT):
            pivot_highs.append((i, rows[i]["high"]))

        if is_pivot_low(rows, i, PIVOT_LEFT, PIVOT_RIGHT):
            pivot_lows.append((i, rows[i]["low"]))

    return pivot_highs, pivot_lows


def line_value(p1: Tuple[int, float], p2: Tuple[int, float], x: int) -> float:
    x1, y1 = p1
    x2, y2 = p2

    if x2 == x1:
        return y2

    slope = (y2 - y1) / (x2 - x1)
    return y1 + slope * (x - x1)


def choose_triangle_points(
    pivot_highs: List[Tuple[int, float]],
    pivot_lows: List[Tuple[int, float]]
) -> Optional[Tuple[Tuple[int, float], Tuple[int, float], Tuple[int, float], Tuple[int, float]]]:
    """
    Son iki pivot high ve son iki pivot low ile triangle çizgisi kurar.
    """
    if len(pivot_highs) < 2 or len(pivot_lows) < 2:
        return None

    h1, h2 = pivot_highs[-2], pivot_highs[-1]
    l1, l2 = pivot_lows[-2], pivot_lows[-1]

    if h2[0] <= h1[0] or l2[0] <= l1[0]:
        return None

    if h2[0] - h1[0] < 5:
        return None

    if l2[0] - l1[0] < 5:
        return None

    return h1, h2, l1, l2


# =========================
# TRIANGLE SIGNAL
# =========================

def detect_triangle_breakout(
    rows: List[dict],
    signal_idx: int,
    signal_type: str
) -> Optional[dict]:

    if len(rows) < MIN_BARS_REQUIRED:
        return None

    if signal_idx < MIN_BARS_REQUIRED:
        return None

    if signal_idx <= PIVOT_LEFT + PIVOT_RIGHT + 5:
        return None

    pivot_highs, pivot_lows = collect_pivots_before_index(rows, signal_idx)
    points = choose_triangle_points(pivot_highs, pivot_lows)

    if points is None:
        return None

    h1, h2, l1, l2 = points

    resistance_now = line_value(h1, h2, signal_idx)
    support_now = line_value(l1, l2, signal_idx)

    if resistance_now <= 0 or support_now <= 0:
        return None

    if support_now >= resistance_now:
        return None

    candle = rows[signal_idx]
    prev = rows[signal_idx - 1]

    close_now = candle["close"]
    high_now = candle["high"]
    low_now = candle["low"]

    prev_resistance = line_value(h1, h2, signal_idx - 1)
    prev_close = prev["close"]

    # Direnç çizgisi çok yükseliyorsa triangle değil, yükselen kanal gibi davranabilir.
    resistance_change = safe_div(h2[1] - h1[1], h1[1])
    if resistance_change > MAX_RESISTANCE_RISING_PCT:
        return None

    # Destek çok düşüyorsa descending broadening gibi olabilir.
    support_change = safe_div(l2[1] - l1[1], l1[1])
    if support_change < -MAX_SUPPORT_FALLING_PCT:
        return None

    # Daralma kontrolü
    start_x = min(h1[0], l1[0])
    start_res = line_value(h1, h2, start_x)
    start_sup = line_value(l1, l2, start_x)

    width_start = start_res - start_sup
    width_now = resistance_now - support_now

    if width_start <= 0 or width_now <= 0:
        return None

    narrowing = 1 - safe_div(width_now, width_start)

    if narrowing < MIN_NARROWING_RATIO:
        return None

    # Önceki mum direnç üstünde kapanmışsa eski kırılım olabilir.
    # LIVE için biraz tolerans bırakıyoruz.
    was_below_before = prev_close <= prev_resistance * (1 + BREAK_BUFFER_PCT)

    if not was_below_before:
        return None

    breakout_level = resistance_now * (1 + BREAK_BUFFER_PCT)

    close_break = close_now > breakout_level
    wick_break = high_now > breakout_level

    if signal_type == "LIVE":
        if LIVE_WICK_TRIGGER:
            is_breakout = close_break or wick_break
            trigger_mode = "WICK" if wick_break and not close_break else "CLOSE"
        else:
            is_breakout = close_break
            trigger_mode = "CLOSE"
    else:
        if CLOSED_NEEDS_CLOSE_ABOVE:
            is_breakout = close_break
            trigger_mode = "CLOSE"
        else:
            is_breakout = close_break or wick_break
            trigger_mode = "WICK" if wick_break and not close_break else "CLOSE"

    if not is_breakout:
        return None

    extension = safe_div(close_now - resistance_now, resistance_now)

    # Çok fazla uzamış coinleri kırılım diye geç alma
    if extension > MAX_EXTENSION_ABOVE_RESISTANCE:
        return None

    # Mum triangle altına da sert iğne atmışsa kalite düşer ama direkt eleme yapmıyoruz.
    support_distance = safe_div(close_now - support_now, support_now)

    return {
        "signal_type": signal_type,
        "trigger_mode": trigger_mode,
        "signal_idx": signal_idx,
        "close": close_now,
        "high": high_now,
        "low": low_now,
        "resistance": resistance_now,
        "support": support_now,
        "extension": extension,
        "support_distance": support_distance,
        "narrowing": narrowing,
        "h1_idx": h1[0],
        "h1_price": h1[1],
        "h2_idx": h2[0],
        "h2_price": h2[1],
        "l1_idx": l1[0],
        "l1_price": l1[1],
        "l2_idx": l2[0],
        "l2_price": l2[1],
    }


def get_candidate_indices(rows: List[dict]) -> List[Tuple[str, int]]:
    """
    Binance 3D kline son mumu genellikle kapanmamış canlı mumdur.
    close_time > now ise LIVE kabul edilir.
    """
    now_ms = int(time.time() * 1000)

    if not rows:
        return []

    last_idx = len(rows) - 1
    last_is_live = rows[last_idx]["close_time"] > now_ms

    candidates = []

    if last_is_live:
        live_idx = last_idx
        last_closed_idx = last_idx - 1
    else:
        live_idx = None
        last_closed_idx = last_idx

    if INCLUDE_LIVE_CANDLE and live_idx is not None:
        candidates.append(("LIVE", live_idx))

    if INCLUDE_LAST_CLOSED_CANDLE and last_closed_idx is not None and last_closed_idx >= 0:
        candidates.append(("CLOSED", last_closed_idx))

    if INCLUDE_PREV_CLOSED_CANDLE and last_closed_idx is not None and last_closed_idx - 1 >= 0:
        candidates.append(("PREV", last_closed_idx - 1))

    return candidates


def scan_symbol(symbol: str, quote_volume_24h: float) -> Optional[dict]:
    try:
        rows = get_klines(symbol)
    except Exception as e:
        print(f"[WARN] {symbol} kline error: {e}")
        return None

    if len(rows) < MIN_BARS_REQUIRED:
        return None

    hits = []

    for signal_type, idx in get_candidate_indices(rows):
        hit = detect_triangle_breakout(rows, idx, signal_type)
        if hit:
            hits.append(hit)

    if not hits:
        return None

    # Aynı coin birden fazla etikette sinyal verirse tek satırda göster.
    # Öncelik: CLOSED daha güvenilir, LIVE daha güncel, PREV kaçanı gösterir.
    priority = {
        "CLOSED": 1,
        "LIVE": 2,
        "PREV": 3,
    }

    hits_sorted = sorted(hits, key=lambda x: priority.get(x["signal_type"], 99))
    best = hits_sorted[0]

    status_tags = ",".join([h["signal_type"] for h in hits_sorted])

    volume_status = "OK" if quote_volume_24h >= MIN_SERIOUS_24H_QUOTE_VOLUME else "LOWVOL"

    if volume_status == "LOWVOL" and not SHOW_LOW_VOLUME_SIGNALS:
        return None

    return {
        "symbol": symbol,
        "main_signal": best["signal_type"],
        "status_tags": status_tags,
        "trigger_mode": best["trigger_mode"],
        "quote_volume_24h": quote_volume_24h,
        "volume_status": volume_status,
        "price": best["close"],
        "resistance": best["resistance"],
        "support": best["support"],
        "extension": best["extension"],
        "narrowing": best["narrowing"],
        "support_distance": best["support_distance"],
        "details": best,
    }


# =========================
# TELEGRAM
# =========================

def build_telegram_message(results: List[dict]) -> str:
    title = "🔺 <b>3D Triangle Scanner</b>\n"
    title += "LIVE + CLOSED + PREV kontrol edildi.\n\n"

    if not results:
        return title + "Sinyal yok."

    lines = [title]

    for i, r in enumerate(results[:MAX_ALERT_ROWS], 1):
        symbol = html.escape(r["symbol"])
        main_signal = html.escape(r["main_signal"])
        status_tags = html.escape(r["status_tags"])
        trigger_mode = html.escape(r["trigger_mode"])
        volume_status = html.escape(r["volume_status"])

        warning = ""
        if "LIVE" in r["status_tags"]:
            warning = "\n⚠️ LIVE mum kapanmadı, repaint riski var."

        line = (
            f"{i}) <b>{symbol}</b> | <b>{main_signal}</b> | {status_tags} | {trigger_mode}\n"
            f"Vol: <b>{fmt_money(r['quote_volume_24h'])}</b> USDT | {volume_status}\n"
            f"Price: {r['price']:.8g}\n"
            f"Res: {r['resistance']:.8g} | Sup: {r['support']:.8g}\n"
            f"Break ext: {pct(r['extension'])} | Narrow: {pct(r['narrowing'])}"
            f"{warning}\n"
        )

        lines.append(line)

    if len(results) > MAX_ALERT_ROWS:
        lines.append(f"\n+{len(results) - MAX_ALERT_ROWS} sinyal daha var, mesaj limiti nedeniyle kesildi.")

    return "\n".join(lines)


def send_telegram(message: str):
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    if not token or not chat_id:
        print("[INFO] TELEGRAM_BOT_TOKEN veya TELEGRAM_CHAT_ID yok. Sadece konsola yazdırıyorum.")
        print(message)
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"

    chunks = split_message(message, max_len=3800)

    for chunk in chunks:
        payload = {
            "chat_id": chat_id,
            "text": chunk,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }

        try:
            r = requests.post(url, json=payload, timeout=20)
            if not r.ok:
                print(f"[WARN] Telegram error {r.status_code}: {r.text}")
            time.sleep(0.4)
        except Exception as e:
            print(f"[WARN] Telegram send failed: {e}")


def split_message(text: str, max_len: int = 3800) -> List[str]:
    if len(text) <= max_len:
        return [text]

    parts = []
    current = ""

    for block in text.split("\n\n"):
        if len(current) + len(block) + 2 <= max_len:
            current += block + "\n\n"
        else:
            if current:
                parts.append(current.strip())
            current = block + "\n\n"

    if current:
        parts.append(current.strip())

    return parts


# =========================
# MAIN
# =========================

def main():
    print("Starting 3D triangle scanner...")
    print(f"Interval: {INTERVAL}")
    print(f"Include LIVE: {INCLUDE_LIVE_CANDLE}")
    print(f"Include CLOSED: {INCLUDE_LAST_CLOSED_CANDLE}")
    print(f"Include PREV: {INCLUDE_PREV_CLOSED_CANDLE}")

    symbols = get_spot_usdt_symbols()
    volumes = get_24h_quote_volumes()

    print(f"Symbols: {len(symbols)}")

    results = []

    for n, symbol in enumerate(symbols, 1):
        qv = volumes.get(symbol, 0.0)

        try:
            result = scan_symbol(symbol, qv)
            if result:
                results.append(result)
                print(
                    f"[HIT] {symbol} "
                    f"{result['status_tags']} "
                    f"vol={fmt_money(result['quote_volume_24h'])}"
                )
        except Exception as e:
            print(f"[WARN] {symbol} failed: {e}")

        # Binance rate limit rahat kalsın
        if n % 20 == 0:
            time.sleep(0.25)

    # Hacme göre sırala
    results.sort(key=lambda x: x["quote_volume_24h"], reverse=True)

    print(f"Total signals: {len(results)}")

    msg = build_telegram_message(results)
    send_telegram(msg)


if __name__ == "__main__":
    main()
