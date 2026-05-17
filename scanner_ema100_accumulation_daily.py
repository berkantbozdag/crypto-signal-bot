import os
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests


# ============================================================
# EMA100 ACCUMULATION + BREAKOUT DAILY SCANNER
# Binance Spot USDT - 3D
# Telegram destekli
# Diğer scannerlarla çakışmasın diye rapor klasörü: reports_ema100
# ============================================================


# =========================
# GENEL AYARLAR
# =========================

BASE_URL = "https://data-api.binance.vision"

QUOTE_ASSET = "USDT"
INTERVAL = "3d"
KLINE_LIMIT = 220

MIN_BARS = 15

# Ana hacim eşiğin
MIN_BINANCE_24H_QUOTE_VOL = 7_000_000  # 7M USDT

# ATH gövde filtresi
# İlk 3D mumun gövde high seviyesi ATH referansı kabul edilir.
# Coinin güncel fiyatı bu seviyenin en az %50'sinde olmalı.
ATH_BODY_THRESHOLD = 0.50

# Dirence yakınlık / kırılım
BOX_LOOKBACK = 80
NEAR_RESISTANCE_PCT = 5.0
BREAKOUT_BUFFER_PCT = 0.0

# Akümülasyon kutusu çok genişse ele
MAX_BOX_HEIGHT_PCT = 80.0

# EMA100 temas filtresi
EMA_LEN = 100
EMA_TOUCH_LOOKBACK = 90
EMA_TOUCH_TOLERANCE_PCT = 4.0

# Direnç hesabında wick yerine gövde kullan
USE_BODY_FOR_RESISTANCE = True

# Request arası bekleme
SLEEP_BETWEEN_SYMBOLS = 0.05

# Çıktı klasörü: eski scannerlarla çakışmasın
REPORT_DIR_NAME = "reports_ema100"


EXCLUDED_BASES = {
    "USDC", "FDUSD", "TUSD", "BUSD", "DAI", "USDP", "PAX", "PYUSD",
    "USDE", "SUSDE", "USDS", "USD1", "AEUR", "EURI", "EUR", "TRY",
    "BRL", "AUD", "GBP", "RUB", "UAH"
}


session = requests.Session()
session.headers.update({
    "User-Agent": "ema100-accumulation-daily-scanner/1.0"
})


# =========================
# HTTP
# =========================

def get_json(path, params=None, retries=3, sleep_sec=1.5):
    url = BASE_URL + path

    last_error = None

    for attempt in range(1, retries + 1):
        try:
            response = session.get(url, params=params, timeout=25)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            last_error = e
            print(f"HTTP hata attempt {attempt}/{retries}: {url} | {e}")
            if attempt < retries:
                time.sleep(sleep_sec * attempt)

    raise last_error


# =========================
# BINANCE DATA
# =========================

def get_spot_usdt_symbols():
    data = get_json("/api/v3/exchangeInfo")
    symbols = []

    for item in data.get("symbols", []):
        symbol = item.get("symbol")
        base = item.get("baseAsset")
        quote = item.get("quoteAsset")
        status = item.get("status")

        if quote != QUOTE_ASSET:
            continue

        if status != "TRADING":
            continue

        if base in EXCLUDED_BASES:
            continue

        if not item.get("isSpotTradingAllowed", True):
            continue

        symbols.append(symbol)

    return sorted(symbols)


def get_24h_volume_map():
    data = get_json("/api/v3/ticker/24hr")
    volume_map = {}

    for item in data:
        symbol = item.get("symbol")

        try:
            quote_volume = float(item.get("quoteVolume", 0))
        except Exception:
            quote_volume = 0.0

        volume_map[symbol] = quote_volume

    return volume_map


def get_klines(symbol):
    raw = get_json(
        "/api/v3/klines",
        params={
            "symbol": symbol,
            "interval": INTERVAL,
            "limit": KLINE_LIMIT,
        },
    )

    if not raw:
        return pd.DataFrame()

    df = pd.DataFrame(
        raw,
        columns=[
            "open_time",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "close_time",
            "quote_volume",
            "trades",
            "taker_buy_base",
            "taker_buy_quote",
            "ignore",
        ],
    )

    numeric_cols = [
        "open",
        "high",
        "low",
        "close",
        "volume",
        "quote_volume",
    ]

    for col in numeric_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
    df["close_time"] = pd.to_datetime(df["close_time"], unit="ms")

    df = df.dropna(subset=["open", "high", "low", "close"])
    df = df.reset_index(drop=True)

    return df


# =========================
# ANALİZ
# =========================

def analyze_symbol(symbol, quote_vol_24h):
    df = get_klines(symbol)

    if len(df) < MIN_BARS:
        return None

    close = float(df["close"].iloc[-1])

    if close <= 0:
        return None

    # ------------------------------------------------------------
    # ATH BODY REFERANSI
    # İlk 3D mumun gövde üstü referans alınır.
    # ------------------------------------------------------------

    first_open = float(df["open"].iloc[0])
    first_close = float(df["close"].iloc[0])
    first_body_high = max(first_open, first_close)

    if first_body_high <= 0:
        return None

    ath_body_pct = close / first_body_high

    if ath_body_pct < ATH_BODY_THRESHOLD:
        return None

    # ------------------------------------------------------------
    # EMA100 temas filtresi
    # Yeni coinlerde EMA100 tam oturmayabilir, bu yüzden 100 bar yoksa geçiyoruz.
    # ------------------------------------------------------------

    df["ema100"] = df["close"].ewm(span=EMA_LEN, adjust=False).mean()

    if len(df) >= EMA_LEN:
        recent = df.iloc[-EMA_TOUCH_LOOKBACK:].copy()

        ema_touch = (
            recent["high"] >= recent["ema100"] * (1 - EMA_TOUCH_TOLERANCE_PCT / 100)
        ).any()
    else:
        ema_touch = True

    if not ema_touch:
        return None

    # ------------------------------------------------------------
    # Direnç / destek kutusu
    # Son mumu hariç tutuyoruz ki kırılım mumu direnci şişirmesin.
    # ------------------------------------------------------------

    if len(df) > BOX_LOOKBACK + 1:
        box = df.iloc[-BOX_LOOKBACK - 1:-1].copy()
    else:
        box = df.iloc[:-1].copy()

    if len(box) < 5:
        return None

    if USE_BODY_FOR_RESISTANCE:
        box["body_high"] = box[["open", "close"]].max(axis=1)
        box["body_low"] = box[["open", "close"]].min(axis=1)

        resistance = float(box["body_high"].max())
        support = float(box["body_low"].min())
    else:
        resistance = float(box["high"].max())
        support = float(box["low"].min())

    if resistance <= 0 or support <= 0:
        return None

    box_height_pct = ((resistance - support) / support) * 100

    if box_height_pct > MAX_BOX_HEIGHT_PCT:
        return None

    distance_to_res_pct = ((resistance - close) / resistance) * 100

    breakout = close > resistance * (1 + BREAKOUT_BUFFER_PCT / 100)
    near_breakout = 0 <= distance_to_res_pct <= NEAR_RESISTANCE_PCT

    if not breakout and not near_breakout:
        return None

    volume_ok = quote_vol_24h >= MIN_BINANCE_24H_QUOTE_VOL

    if breakout and volume_ok:
        setup = "BREAKOUT"
    elif breakout and not volume_ok:
        setup = "LOW_VOL_BREAKOUT"
    elif near_breakout and volume_ok:
        setup = "NEAR_BREAKOUT"
    else:
        setup = "LOW_VOL_NEAR_BREAKOUT"

    return {
        "symbol": symbol,
        "setup": setup,
        "binance_24h_vol_m": round(quote_vol_24h / 1_000_000, 2),
        "close": close,
        "resistance": resistance,
        "support": support,
        "distance_to_res_pct": round(distance_to_res_pct, 2),
        "ath_body_pct": round(ath_body_pct * 100, 2),
        "box_height_pct": round(box_height_pct, 2),
        "bars_3d": len(df),
        "first_3d_body_high": first_body_high,
    }


# =========================
# TELEGRAM
# =========================

def send_telegram_message(text):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        print("Telegram secret yok. Mesaj gönderilmedi.")
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"

    payload = {
        "chat_id": chat_id,
        "text": text[:3900],
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    try:
        response = requests.post(url, data=payload, timeout=25)
        if response.status_code != 200:
            print(f"Telegram hata: {response.status_code} | {response.text}")
        else:
            print("Telegram mesajı gönderildi.")
    except Exception as e:
        print(f"Telegram gönderilemedi: {e}")


def make_telegram_text(df, today):
    if df.empty:
        return (
            f"📊 <b>EMA100 Accumulation Scanner</b>\n"
            f"🗓 {today}\n\n"
            f"Bugün aday yok."
        )

    serious = df[df["binance_24h_vol_m"] >= 7]
    low_vol = df[df["binance_24h_vol_m"] < 7]

    lines = [
        "📊 <b>EMA100 Accumulation Scanner</b>",
        f"🗓 {today}",
        "",
        f"Toplam aday: <b>{len(df)}</b>",
        f"7M üstü ciddi aday: <b>{len(serious)}</b>",
        f"7M altı düşük öncelik: <b>{len(low_vol)}</b>",
        "",
    ]

    serious_df = df[df["setup"].isin(["BREAKOUT", "NEAR_BREAKOUT"])]
    low_df = df[df["setup"].isin(["LOW_VOL_BREAKOUT", "LOW_VOL_NEAR_BREAKOUT"])]

    if not serious_df.empty:
        lines.append("🔥 <b>CİDDİ ADAYLAR</b>")

        for _, r in serious_df.head(15).iterrows():
            lines.append(
                f"{r['symbol']} | {r['setup']} | "
                f"Vol: {r['binance_24h_vol_m']}M | "
                f"Direnç uzaklık: {r['distance_to_res_pct']}% | "
                f"ATH body: {r['ath_body_pct']}%"
            )

        lines.append("")

    if not low_df.empty:
        lines.append("⚠️ <b>DÜŞÜK HACİM / İZLEME</b>")

        for _, r in low_df.head(15).iterrows():
            lines.append(
                f"{r['symbol']} | {r['setup']} | "
                f"Vol: {r['binance_24h_vol_m']}M | "
                f"Direnç uzaklık: {r['distance_to_res_pct']}%"
            )

        lines.append("")

    lines.append("Not:")
    lines.append("BREAKOUT = direnç üstü kapanış")
    lines.append("NEAR_BREAKOUT = dirence %5 yakın")
    lines.append("LOW_VOL = 7M USDT altı düşük öncelik")

    return "\n".join(lines)


# =========================
# MAIN
# =========================

def main():
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")

    print("=" * 70)
    print(f"EMA100 Accumulation Scanner başladı: {today}")
    print("=" * 70)

    print("Binance spot USDT sembolleri alınıyor...")
    symbols = get_spot_usdt_symbols()

    print("24h Binance spot quote volume alınıyor...")
    volume_map = get_24h_volume_map()

    print(f"Taranacak sembol sayısı: {len(symbols)}")

    results = []

    for i, symbol in enumerate(symbols, start=1):
        try:
            if i % 25 == 0:
                print(f"{i}/{len(symbols)} tarandı...")

            quote_vol_24h = volume_map.get(symbol, 0.0)

            row = analyze_symbol(symbol, quote_vol_24h)

            if row is not None:
                results.append(row)

            time.sleep(SLEEP_BETWEEN_SYMBOLS)

        except Exception as e:
            print(f"{symbol} hata: {e}")
            continue

    columns = [
        "symbol",
        "setup",
        "binance_24h_vol_m",
        "close",
        "resistance",
        "support",
        "distance_to_res_pct",
        "ath_body_pct",
        "box_height_pct",
        "bars_3d",
        "first_3d_body_high",
    ]

    df = pd.DataFrame(results, columns=columns)

    if not df.empty:
        setup_rank = {
            "BREAKOUT": 0,
            "NEAR_BREAKOUT": 1,
            "LOW_VOL_BREAKOUT": 2,
            "LOW_VOL_NEAR_BREAKOUT": 3,
        }

        df["setup_rank"] = df["setup"].map(setup_rank).fillna(9)

        df = df.sort_values(
            by=["setup_rank", "binance_24h_vol_m"],
            ascending=[True, False],
        )

        df = df.drop(columns=["setup_rank"])
        df = df.reset_index(drop=True)

    reports_dir = Path(REPORT_DIR_NAME)
    reports_dir.mkdir(exist_ok=True)

    latest_path = reports_dir / "latest.csv"
    dated_path = reports_dir / f"{today}.csv"

    df.to_csv(latest_path, index=False)
    df.to_csv(dated_path, index=False)

    print("\n" + "=" * 70)
    print("SONUÇ")
    print("=" * 70)

    if df.empty:
        print("Bugün aday yok.")
    else:
        print(df.to_string(index=False))

    telegram_text = make_telegram_text(df, today)
    send_telegram_message(telegram_text)

    print("\nRapor kaydedildi:")
    print(f"- {latest_path}")
    print(f"- {dated_path}")

    print("\nNot:")
    print("7M USDT altı düşük öncelik.")
    print("NEAR_BREAKOUT = dirence yakın.")
    print("BREAKOUT = direnç üstüne çıkmış.")
    print("LOW_VOL_BREAKOUT = kırmış ama hacim 7M altı.")
    print("=" * 70)


if __name__ == "__main__":
    main()
