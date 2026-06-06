import os
import json
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Any, Optional

import requests


# =========================
# AYARLAR
# =========================

BINANCE_BASE_URL = os.getenv("BINANCE_BASE_URL", "https://api.binance.com")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

MIN_24H_QUOTE_VOLUME = float(os.getenv("MIN_24H_QUOTE_VOLUME", "10000000"))  # 10M USDT
INTERVAL = os.getenv("INTERVAL", "1h")

AVG_VOLUME_BARS = int(os.getenv("AVG_VOLUME_BARS", "20"))
VOLUME_SPIKE_MULTIPLIER = float(os.getenv("VOLUME_SPIKE_MULTIPLIER", "2.0"))  # 2x ani hacim

COOLDOWN_HOURS = int(os.getenv("COOLDOWN_HOURS", "12"))
MAX_ALERTS = int(os.getenv("MAX_ALERTS", "25"))

STATE_FILE = os.getenv("STATE_FILE", "alert_state.json")
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "15"))
SLEEP_BETWEEN_REQUESTS = float(os.getenv("SLEEP_BETWEEN_REQUESTS", "0.08"))


# Stable / fiat / gereksiz bazları çıkarıyoruz
EXCLUDED_BASE_ASSETS = {
    "USDC", "FDUSD", "TUSD", "BUSD", "DAI", "USDP", "PAX",
    "PYUSD", "USDE", "SUSDE", "USDS", "USD1",
    "EUR", "TRY", "BRL", "AUD", "GBP", "RUB", "UAH",
    "AEUR", "EURI",
}

# Eski leveraged token kalıntıları / spam önleme
EXCLUDED_SUFFIXES = ("UP", "DOWN", "BULL", "BEAR")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def fmt_usdt(n: float) -> str:
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.2f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return f"{n:.0f}"


def get_json(path: str, params: Optional[dict] = None) -> Any:
    url = f"{BINANCE_BASE_URL}{path}"
    r = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r.json()


def load_state() -> Dict[str, str]:
    if not os.path.exists(STATE_FILE):
        return {}

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state: Dict[str, str]) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


def is_in_cooldown(symbol: str, state: Dict[str, str], now: datetime) -> bool:
    ts = state.get(symbol)

    if not ts:
        return False

    try:
        last = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return False

    return now - last < timedelta(hours=COOLDOWN_HOURS)


def cleanup_state(state: Dict[str, str], now: datetime) -> Dict[str, str]:
    cleaned = {}
    keep_for = timedelta(hours=max(COOLDOWN_HOURS * 4, 48))

    for symbol, ts in state.items():
        try:
            last = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except Exception:
            continue

        if now - last <= keep_for:
            cleaned[symbol] = ts

    return cleaned


def fetch_spot_usdt_symbols() -> Dict[str, str]:
    """
    Binance spotta işlem gören USDT paritelerini döndürür.
    Çıktı: {symbol: baseAsset}
    """

    info = get_json("/api/v3/exchangeInfo")

    symbols: Dict[str, str] = {}

    for item in info.get("symbols", []):
        symbol = item.get("symbol", "")
        base = item.get("baseAsset", "")
        quote = item.get("quoteAsset", "")
        status = item.get("status", "")

        if status != "TRADING":
            continue

        if quote != "USDT":
            continue

        if base in EXCLUDED_BASE_ASSETS:
            continue

        if base.endswith(EXCLUDED_SUFFIXES):
            continue

        if not item.get("isSpotTradingAllowed", True):
            continue

        symbols[symbol] = base

    return symbols


def fetch_24h_quote_volumes() -> Dict[str, float]:
    """
    Binance 24h ticker verisinden quoteVolume alır.
    USDT paritelerinde quoteVolume = yaklaşık USDT hacmi.
    """

    tickers = get_json("/api/v3/ticker/24hr")

    out: Dict[str, float] = {}

    for t in tickers:
        symbol = t.get("symbol")

        try:
            out[symbol] = float(t.get("quoteVolume", 0.0))
        except Exception:
            out[symbol] = 0.0

    return out


def analyze_symbol(symbol: str, quote_volume_24h: float) -> Optional[dict]:
    """
    Ani volüm şartı:
    Son kapanmış 1 saatlik mumun quote volume değeri,
    önceki 20 kapanmış mum ortalamasının en az 2 katı olmalı.
    """

    limit = max(AVG_VOLUME_BARS + 3, 30)

    klines = get_json(
        "/api/v3/klines",
        {
            "symbol": symbol,
            "interval": INTERVAL,
            "limit": limit,
        },
    )

    if len(klines) < AVG_VOLUME_BARS + 2:
        return None

    # Son mum genelde açık mumdur.
    # Bu yüzden son kapanmış mum = -2
    last_closed = klines[-2]

    # Önceki 20 kapanmış mum
    previous = klines[-(AVG_VOLUME_BARS + 2):-2]

    last_quote_vol = float(last_closed[7])
    prev_quote_vols = [float(k[7]) for k in previous]

    avg_quote_vol = sum(prev_quote_vols) / len(prev_quote_vols) if prev_quote_vols else 0.0

    if avg_quote_vol <= 0:
        return None

    spike_ratio = last_quote_vol / avg_quote_vol

    if spike_ratio < VOLUME_SPIKE_MULTIPLIER:
        return None

    open_price = float(last_closed[1])
    close_price = float(last_closed[4])

    price_change_pct = ((close_price - open_price) / open_price * 100.0) if open_price else 0.0

    close_time = datetime.fromtimestamp(
        int(last_closed[6]) / 1000,
        tz=timezone.utc,
    )

    return {
        "symbol": symbol,
        "quote_volume_24h": quote_volume_24h,
        "last_quote_vol": last_quote_vol,
        "avg_quote_vol": avg_quote_vol,
        "spike_ratio": spike_ratio,
        "price_change_pct": price_change_pct,
        "close_price": close_price,
        "close_time_utc": close_time.strftime("%Y-%m-%d %H:%M UTC"),
    }


def build_message(alerts: List[dict]) -> str:
    now_str = utc_now().strftime("%Y-%m-%d %H:%M UTC")

    lines = [
        "🚨 Binance Spot Ani Volüm Scanner",
        f"Zaman: {now_str}",
        f"Filtre: 24h hacim ≥ {fmt_usdt(MIN_24H_QUOTE_VOLUME)} USDT | Son {INTERVAL} hacim ≥ {VOLUME_SPIKE_MULTIPLIER:g}x / {AVG_VOLUME_BARS} mum ort.",
        "",
    ]

    for i, a in enumerate(alerts[:MAX_ALERTS], 1):
        tv = f"https://www.tradingview.com/chart/?symbol=BINANCE:{a['symbol']}"

        lines.extend(
            [
                f"{i}) #{a['symbol']}",
                f"24h Hacim: {fmt_usdt(a['quote_volume_24h'])} USDT",
                f"Son {INTERVAL} Hacim: {fmt_usdt(a['last_quote_vol'])} USDT",
                f"Ortalama Hacim: {fmt_usdt(a['avg_quote_vol'])} USDT",
                f"Volüm Artışı: {a['spike_ratio']:.2f}x",
                f"Mum Değişim: {a['price_change_pct']:+.2f}%",
                f"Kapanış: {a['close_price']:.8g}",
                f"Mum: {a['close_time_utc']}",
                tv,
                "",
            ]
        )

    return "\n".join(lines).strip()


def send_telegram(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("TELEGRAM_BOT_TOKEN veya TELEGRAM_CHAT_ID eksik.")
        print("Telegram mesajı gönderilmedi.")
        print(text)
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    # Telegram tek mesaj limiti yaklaşık 4096 karakter.
    # Uzun listeyi parçalara bölüyoruz.
    chunks = []
    current = ""

    for block in text.split("\n\n"):
        candidate = f"{current}\n\n{block}".strip() if current else block

        if len(candidate) > 3900:
            if current:
                chunks.append(current)
            current = block
        else:
            current = candidate

    if current:
        chunks.append(current)

    for chunk in chunks:
        r = requests.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": chunk,
                "disable_web_page_preview": True,
            },
            timeout=REQUEST_TIMEOUT,
        )
        r.raise_for_status()
        time.sleep(0.5)


def main() -> None:
    now = utc_now()

    state = cleanup_state(load_state(), now)

    symbols = fetch_spot_usdt_symbols()
    volumes_24h = fetch_24h_quote_volumes()

    candidates = [
        (symbol, volumes_24h.get(symbol, 0.0))
        for symbol in symbols.keys()
        if volumes_24h.get(symbol, 0.0) >= MIN_24H_QUOTE_VOLUME
    ]

    print(f"Binance spot USDT parite sayısı: {len(symbols)}")
    print(f"24h hacim >= {MIN_24H_QUOTE_VOLUME:.0f} USDT olanlar: {len(candidates)}")

    alerts: List[dict] = []

    for symbol, qv24 in candidates:
        if is_in_cooldown(symbol, state, now):
            continue

        try:
            result = analyze_symbol(symbol, qv24)

            if result:
                alerts.append(result)

        except requests.HTTPError as e:
            print(f"{symbol} HTTP error: {e}")

        except Exception as e:
            print(f"{symbol} error: {e}")

        time.sleep(SLEEP_BETWEEN_REQUESTS)

    alerts.sort(
        key=lambda x: (x["spike_ratio"], x["last_quote_vol"]),
        reverse=True,
    )

    alerts = alerts[:MAX_ALERTS]

    if alerts:
        message = build_message(alerts)
        send_telegram(message)

        for a in alerts:
            state[a["symbol"]] = now.isoformat().replace("+00:00", "Z")

        print(f"{len(alerts)} alarm gönderildi.")

    else:
        print("Alarm yok.")

    save_state(state)


if __name__ == "__main__":
    main()
