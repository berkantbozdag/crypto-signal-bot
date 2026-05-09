import requests
import pandas as pd
import time
from pathlib import Path
from datetime import datetime, timezone

BASE = "https://data-api.binance.vision"

# =========================
# KRİTERLER
# =========================

MIN_3D_BARS = 15
MAX_3D_BARS = 183
ATH_BODY_THRESHOLD = 0.50

# Volume artışı filtresi
MIN_24H_VOLUME_USDT = 0          # 7M altı da listede kalsın diye 0
VOLUME_SPIKE_RATIO = 2.0         # Önceki saate göre en az 2x hacim
MIN_1H_VOLUME_USDT = 50_000      # Çok küçük hacimli saçma spike'ları elemek için

STABLE_OR_FIAT_BASES = {
    "USDC", "FDUSD", "TUSD", "BUSD", "DAI", "USDP", "PAX", "PYUSD",
    "USDE", "SUSDE", "USDS", "USD1", "AEUR", "EURI",
    "EUR", "TRY", "BRL", "AUD", "GBP", "RUB", "UAH"
}

RESULTS_DIR = Path("results")
STATE_DIR = Path("state")

RESULTS_DIR.mkdir(exist_ok=True)
STATE_DIR.mkdir(exist_ok=True)

LATEST_CSV = RESULTS_DIR / "scan_latest.csv"
LATEST_MD = RESULTS_DIR / "scan_latest.md"

SPIKES_CSV = RESULTS_DIR / "volume_spikes_latest.csv"
SPIKES_MD = RESULTS_DIR / "volume_spikes_latest.md"

STATE_CSV = STATE_DIR / "last_hour_snapshot.csv"


# =========================
# FONKSİYONLAR
# =========================

def get_json(endpoint, params=None, timeout=20, retries=3):
    url = BASE + endpoint

    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, timeout=timeout)

            if r.status_code == 200:
                return r.json()

            print(f"HTTP hata: {r.status_code} | {r.text[:200]}")
            time.sleep(1.5)

        except Exception as e:
            print("İstek hatası:", e)
            time.sleep(1.5)

    return None


def fmt_volume(x):
    if pd.isna(x) or x is None:
        return "N/A"

    x = float(x)

    if x >= 1_000_000_000:
        return f"{x / 1_000_000_000:.2f}B"
    elif x >= 1_000_000:
        return f"{x / 1_000_000:.2f}M"
    elif x >= 1_000:
        return f"{x / 1_000:.2f}K"
    else:
        return f"{x:.0f}"


def ms_to_utc(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def get_last_closed_1h_volume(symbol):
    """
    Binance 1H klines:
    - limit=3 alıyoruz.
    - Son mum genelde açık mum olabilir.
    - Bu yüzden sondan ikinci mumu kapanmış mum kabul ediyoruz.
    """
    klines = get_json("/api/v3/klines", {
        "symbol": symbol,
        "interval": "1h",
        "limit": 3
    })

    if not klines or len(klines) < 2:
        return None

    closed = klines[-2]

    return {
        "last_1h_open_time": int(closed[0]),
        "last_1h_close_time": int(closed[6]),
        "last_1h_quote_volume": float(closed[7])
    }


def load_previous_state():
    if not STATE_CSV.exists():
        return pd.DataFrame(columns=[
            "symbol",
            "last_1h_quote_volume",
            "last_1h_close_time"
        ])

    try:
        return pd.read_csv(STATE_CSV)
    except Exception:
        return pd.DataFrame(columns=[
            "symbol",
            "last_1h_quote_volume",
            "last_1h_close_time"
        ])


def write_markdown(df, path, title, note):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    if df.empty:
        md = f"""# {title}

Son tarama: **{now}**

Sonuç yok.

{note}
"""
    else:
        md = f"""# {title}

Son tarama: **{now}**

{note}

{df.to_markdown(index=False)}
"""

    path.write_text(md, encoding="utf-8")


# =========================
# ANA TARAMA
# =========================

def main():
    print("Binance saatlik ATH body + volume spike taraması başladı...")

    previous_state = load_previous_state()

    exchange_info = get_json("/api/v3/exchangeInfo")

    if exchange_info is None:
        raise Exception("Binance exchangeInfo alınamadı.")

    symbols = []

    for s in exchange_info["symbols"]:
        symbol = s["symbol"]
        base = s["baseAsset"]

        if s.get("status") != "TRADING":
            continue

        if s.get("quoteAsset") != "USDT":
            continue

        if s.get("isSpotTradingAllowed") is not True:
            continue

        if base in STABLE_OR_FIAT_BASES:
            continue

        if symbol.endswith(("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT")):
            continue

        symbols.append(symbol)

    print("Taranacak Binance spot USDT coin sayısı:", len(symbols))

    prices_data = get_json("/api/v3/ticker/price")

    if prices_data is None:
        raise Exception("Binance fiyat verisi alınamadı.")

    price_map = {
        x["symbol"]: float(x["price"])
        for x in prices_data
        if x["symbol"] in symbols
    }

    ticker_24h = get_json("/api/v3/ticker/24hr")

    if ticker_24h is None:
        raise Exception("Binance 24h ticker verisi alınamadı.")

    volume_24h_map = {
        x["symbol"]: float(x.get("quoteVolume", 0))
        for x in ticker_24h
        if x["symbol"] in symbols
    }

    previous_map = {}

    if not previous_state.empty:
        for _, row in previous_state.iterrows():
            previous_map[row["symbol"]] = {
                "prev_1h_quote_volume": float(row.get("last_1h_quote_volume", 0)),
                "prev_1h_close_time": int(row.get("last_1h_close_time", 0))
            }

    all_results = []
    state_rows = []

    for i, symbol in enumerate(symbols, 1):

        if i % 25 == 0:
            print(f"{i}/{len(symbols)} tarandı...")

        # 3D listing / ATH body filtresi
        klines_3d = get_json("/api/v3/klines", {
            "symbol": symbol,
            "interval": "3d",
            "startTime": 0,
            "limit": 1000
        })

        time.sleep(0.06)

        if not klines_3d:
            continue

        bar_count = len(klines_3d)

        if bar_count < MIN_3D_BARS:
            continue

        if bar_count > MAX_3D_BARS:
            continue

        first_3d_candle = klines_3d[0]

        first_3d_open = float(first_3d_candle[1])
        first_3d_close = float(first_3d_candle[4])

        ath_body_ref = max(first_3d_open, first_3d_close)

        current_price = price_map.get(symbol)
        volume_24h = volume_24h_map.get(symbol)

        if current_price is None or volume_24h is None or ath_body_ref <= 0:
            continue

        if volume_24h < MIN_24H_VOLUME_USDT:
            continue

        ath_body_ratio = current_price / ath_body_ref

        if ath_body_ratio < ATH_BODY_THRESHOLD:
            continue

        # Son kapanmış 1H hacim
        one_hour_data = get_last_closed_1h_volume(symbol)

        time.sleep(0.06)

        if one_hour_data is None:
            continue

        last_1h_volume = one_hour_data["last_1h_quote_volume"]
        last_1h_close_time = one_hour_data["last_1h_close_time"]

        prev = previous_map.get(symbol, {})
        prev_1h_volume = float(prev.get("prev_1h_quote_volume", 0))
        prev_1h_close_time = int(prev.get("prev_1h_close_time", 0))

        if prev_1h_volume > 0:
            volume_ratio = last_1h_volume / prev_1h_volume
        else:
            volume_ratio = None

        is_new_hour = last_1h_close_time != prev_1h_close_time

        is_volume_spike = (
            is_new_hour
            and volume_ratio is not None
            and volume_ratio >= VOLUME_SPIKE_RATIO
            and last_1h_volume >= MIN_1H_VOLUME_USDT
        )

        row = {
            "symbol": symbol,
            "volume_24h_raw": volume_24h,
            "volume_24h": fmt_volume(volume_24h),
            "last_1h_volume_raw": last_1h_volume,
            "last_1h_volume": fmt_volume(last_1h_volume),
            "prev_1h_volume_raw": prev_1h_volume,
            "prev_1h_volume": fmt_volume(prev_1h_volume) if prev_1h_volume > 0 else "N/A",
            "volume_ratio": round(volume_ratio, 2) if volume_ratio is not None else None,
            "price": current_price,
            "ath_body_ref": ath_body_ref,
            "ath_body_ratio": round(ath_body_ratio, 4),
            "bar_count_3d": bar_count,
            "last_1h_close": ms_to_utc(last_1h_close_time),
            "is_volume_spike": is_volume_spike
        }

        all_results.append(row)

        state_rows.append({
            "symbol": symbol,
            "last_1h_quote_volume": last_1h_volume,
            "last_1h_close_time": last_1h_close_time
        })

    if not all_results:
        empty = pd.DataFrame(columns=["symbol", "volume_24h", "last_1h_volume", "volume_ratio"])
        empty.to_csv(LATEST_CSV, index=False)
        empty.to_csv(SPIKES_CSV, index=False)

        write_markdown(
            empty,
            LATEST_MD,
            "Binance ATH Body Scan",
            "Kriterlere uyan coin bulunamadı."
        )

        write_markdown(
            empty,
            SPIKES_MD,
            "Volume Spike Scan",
            "Volume spike bulunamadı."
        )

        pd.DataFrame(state_rows).to_csv(STATE_CSV, index=False)
        return

    df = pd.DataFrame(all_results)

    df = df.sort_values(
        by="volume_24h_raw",
        ascending=False
    ).reset_index(drop=True)

    latest_simple = df[[
        "symbol",
        "volume_24h",
        "last_1h_volume",
        "prev_1h_volume",
        "volume_ratio",
        "ath_body_ratio",
        "last_1h_close"
    ]]

    latest_simple.to_csv(LATEST_CSV, index=False)

    spikes_df = df[df["is_volume_spike"] == True].copy()

    if not spikes_df.empty:
        spikes_df = spikes_df.sort_values(
            by=["volume_ratio", "last_1h_volume_raw"],
            ascending=[False, False]
        ).reset_index(drop=True)

    spikes_simple = spikes_df[[
        "symbol",
        "volume_24h",
        "last_1h_volume",
        "prev_1h_volume",
        "volume_ratio",
        "ath_body_ratio",
        "last_1h_close"
    ]]

    spikes_simple.to_csv(SPIKES_CSV, index=False)

    note_latest = f"""
Notlar:

- 7M USDT altı listede kalır ama düşük öncelik kabul edilir.
- ATH referansı: İlk 3D mumun gövde üstü.
- Wick / fitil kullanılmaz.
- ATH şartı: Güncel fiyat, ATH body referansının en az %{ATH_BODY_THRESHOLD * 100:.0f} seviyesinde olmalı.
- Volume ratio: Son kapanmış 1H hacim / önceki taramadaki son kapanmış 1H hacim.
"""

    note_spikes = f"""
Volume spike kriteri:

- Son kapanmış 1H hacim, önceki kaydedilen 1H hacmin en az `{VOLUME_SPIKE_RATIO}x` üzerinde olmalı.
- Son 1H hacim en az `{fmt_volume(MIN_1H_VOLUME_USDT)}` USDT olmalı.
- ATH-body filtresinden geçen coinler içinde aranır.
"""

    write_markdown(
        latest_simple,
        LATEST_MD,
        "Binance ATH Body Scan",
        note_latest
    )

    write_markdown(
        spikes_simple,
        SPIKES_MD,
        "Volume Spike Scan",
        note_spikes
    )

    pd.DataFrame(state_rows).to_csv(STATE_CSV, index=False)

    print("\nGenel liste:")
    print(latest_simple)

    print("\nVolume spike listesi:")
    print(spikes_simple)

    print(f"\nYazıldı: {LATEST_CSV}")
    print(f"Yazıldı: {SPIKES_CSV}")
    print(f"State yazıldı: {STATE_CSV}")


if __name__ == "__main__":
    main()
