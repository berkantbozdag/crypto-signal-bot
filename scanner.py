import os
import time
import requests
import pandas as pd
from pathlib import Path
from datetime import datetime, timezone

BASE = "https://data-api.binance.vision"

# =========================
# AYARLAR
# =========================

MIN_3D_BARS = 15
MAX_3D_BARS = 183

# Güncel fiyat, ilk 3D mum gövde üstünün en az %50'sinde olsun
ATH_BODY_THRESHOLD = 0.50

# Volume spike filtresi
VOLUME_SPIKE_RATIO = 2.0
MIN_1H_VOLUME_USDT = 50_000

# 7M altı listede kalır ama düşük öncelik kabul edilir
LOW_PRIORITY_24H_VOLUME = 7_000_000

# Spike yoksa da Telegram'a mesaj atsın mı?
SEND_NO_SPIKE_MESSAGE = True

STABLE_OR_FIAT_BASES = {
    "USDC", "FDUSD", "TUSD", "BUSD", "DAI", "USDP", "PAX", "PYUSD",
    "USDE", "SUSDE", "USDS", "USD1", "AEUR", "EURI",
    "EUR", "TRY", "BRL", "AUD", "GBP", "RUB", "UAH"
}

RESULTS_DIR = Path("results")
STATE_DIR = Path("state")

RESULTS_DIR.mkdir(exist_ok=True)
STATE_DIR.mkdir(exist_ok=True)

SCAN_CSV = RESULTS_DIR / "scan_latest.csv"
SCAN_MD = RESULTS_DIR / "scan_latest.md"

SPIKES_CSV = RESULTS_DIR / "volume_spikes_latest.csv"
SPIKES_MD = RESULTS_DIR / "volume_spikes_latest.md"

STATE_CSV = STATE_DIR / "last_hour_snapshot.csv"


# =========================
# YARDIMCI FONKSİYONLAR
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
    if x is None or pd.isna(x):
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


def now_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def send_telegram_message(text):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        print("Telegram bilgileri eksik. Secrets kontrol et.")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"

    payload = {
        "chat_id": chat_id,
        "text": text[:3900],
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }

    try:
        r = requests.post(url, json=payload, timeout=20)
        print("Telegram status:", r.status_code, r.text[:300])
        return r.status_code == 200
    except Exception as e:
        print("Telegram gönderim hatası:", e)
        return False


def get_last_closed_1h(symbol):
    klines = get_json("/api/v3/klines", {
        "symbol": symbol,
        "interval": "1h",
        "limit": 3
    })

    if not klines or len(klines) < 2:
        return None

    # Son mum açık olabilir; sondan ikinci kapanmış mumdur.
    closed = klines[-2]

    return {
        "open_time": int(closed[0]),
        "close_time": int(closed[6]),
        "quote_volume": float(closed[7])
    }


def load_previous_state():
    if not STATE_CSV.exists():
        return {}

    try:
        df = pd.read_csv(STATE_CSV)
    except Exception:
        return {}

    previous = {}

    for _, row in df.iterrows():
        previous[row["symbol"]] = {
            "last_1h_quote_volume": float(row.get("last_1h_quote_volume", 0)),
            "last_1h_close_time": int(row.get("last_1h_close_time", 0))
        }

    return previous


def write_markdown(df, path, title, note):
    if df.empty:
        md = f"""# {title}

Son tarama: **{now_utc()}**

Sonuç yok.

{note}
"""
    else:
        md = f"""# {title}

Son tarama: **{now_utc()}**

{note}

{df.to_markdown(index=False)}
"""

    path.write_text(md, encoding="utf-8")


# =========================
# ANA TARAMA
# =========================

def main():
    print("Saatlik Binance ATH-body + volume spike taraması başladı.")

    previous_state = load_previous_state()
    first_run = len(previous_state) == 0

    exchange_info = get_json("/api/v3/exchangeInfo")

    if exchange_info is None:
        raise Exception("exchangeInfo alınamadı.")

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
        raise Exception("Fiyat verisi alınamadı.")

    price_map = {
        x["symbol"]: float(x["price"])
        for x in prices_data
        if x["symbol"] in symbols
    }

    ticker_24h = get_json("/api/v3/ticker/24hr")

    if ticker_24h is None:
        raise Exception("24H ticker verisi alınamadı.")

    volume_24h_map = {
        x["symbol"]: float(x.get("quoteVolume", 0))
        for x in ticker_24h
        if x["symbol"] in symbols
    }

    rows = []
    state_rows = []

    for i, symbol in enumerate(symbols, 1):

        if i % 25 == 0:
            print(f"{i}/{len(symbols)} tarandı...")

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

        first_3d = klines_3d[0]

        first_open = float(first_3d[1])
        first_close = float(first_3d[4])

        # ATH referansı: İlk 3D mumun gövde üstü. Wick yok.
        ath_body_ref = max(first_open, first_close)

        current_price = price_map.get(symbol)
        volume_24h = volume_24h_map.get(symbol)

        if current_price is None or volume_24h is None or ath_body_ref <= 0:
            continue

        ath_body_ratio = current_price / ath_body_ref

        if ath_body_ratio < ATH_BODY_THRESHOLD:
            continue

        one_h = get_last_closed_1h(symbol)

        time.sleep(0.06)

        if one_h is None:
            continue

        last_1h_volume = one_h["quote_volume"]
        last_1h_close_time = one_h["close_time"]

        prev = previous_state.get(symbol)

        if prev:
            prev_1h_volume = float(prev["last_1h_quote_volume"])
            prev_1h_close_time = int(prev["last_1h_close_time"])
        else:
            prev_1h_volume = 0
            prev_1h_close_time = 0

        is_new_hour = last_1h_close_time != prev_1h_close_time

        if prev_1h_volume > 0:
            volume_ratio = last_1h_volume / prev_1h_volume
        else:
            volume_ratio = None

        is_volume_spike = (
            is_new_hour
            and volume_ratio is not None
            and volume_ratio >= VOLUME_SPIKE_RATIO
            and last_1h_volume >= MIN_1H_VOLUME_USDT
        )

        rows.append({
            "symbol": symbol,
            "volume_24h_raw": volume_24h,
            "volume_24h": fmt_volume(volume_24h),
            "priority": "OK" if volume_24h >= LOW_PRIORITY_24H_VOLUME else "LOW",
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
        })

        state_rows.append({
            "symbol": symbol,
            "last_1h_quote_volume": last_1h_volume,
            "last_1h_close_time": last_1h_close_time
        })

    if not rows:
        empty = pd.DataFrame(columns=[
            "symbol",
            "volume_24h",
            "priority",
            "last_1h_volume",
            "prev_1h_volume",
            "volume_ratio",
            "ath_body_ratio",
            "last_1h_close"
        ])

        empty.to_csv(SCAN_CSV, index=False)
        empty.to_csv(SPIKES_CSV, index=False)

        write_markdown(
            empty,
            SCAN_MD,
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

        send_telegram_message("⚠️ Tarama bitti ama ATH-body kriterlerine uyan coin bulunamadı.")
        return

    df = pd.DataFrame(rows)

    df = df.sort_values(
        by="volume_24h_raw",
        ascending=False
    ).reset_index(drop=True)

    scan_simple = df[[
        "symbol",
        "volume_24h",
        "priority",
        "last_1h_volume",
        "prev_1h_volume",
        "volume_ratio",
        "ath_body_ratio",
        "last_1h_close"
    ]]

    scan_simple.to_csv(SCAN_CSV, index=False)

    spikes_df = df[df["is_volume_spike"] == True].copy()

    if not spikes_df.empty:
        spikes_df = spikes_df.sort_values(
            by=["volume_ratio", "last_1h_volume_raw"],
            ascending=[False, False]
        ).reset_index(drop=True)

    spikes_simple = spikes_df[[
        "symbol",
        "volume_24h",
        "priority",
        "last_1h_volume",
        "prev_1h_volume",
        "volume_ratio",
        "ath_body_ratio",
        "last_1h_close"
    ]]

    spikes_simple.to_csv(SPIKES_CSV, index=False)

    note_scan = f"""
Notlar:

- ATH referansı: İlk 3D mumun gövde üstü.
- Wick / fitil kullanılmaz.
- ATH şartı: Güncel fiyat, ATH body referansının en az %{ATH_BODY_THRESHOLD * 100:.0f} seviyesinde olmalı.
- Minimum 3D bar: {MIN_3D_BARS}
- Maksimum 3D bar: {MAX_3D_BARS}
- Priority LOW: 24H hacim 7M USDT altında.
"""

    note_spikes = f"""
Volume spike kriteri:

- Son kapanmış 1H hacim, önceki kayıtlı 1H hacmin en az {VOLUME_SPIKE_RATIO}x üstünde olmalı.
- Son 1H hacim en az {fmt_volume(MIN_1H_VOLUME_USDT)} USDT olmalı.
- Sadece ATH-body filtresinden geçen coinlerde aranır.
"""

    write_markdown(scan_simple, SCAN_MD, "Binance ATH Body Scan", note_scan)
    write_markdown(spikes_simple, SPIKES_MD, "Volume Spike Scan", note_spikes)

    pd.DataFrame(state_rows).to_csv(STATE_CSV, index=False)

    # =========================
    # TELEGRAM MESAJI
    # =========================

    if first_run:
        message = (
            "✅ <b>Bot kuruldu ve ilk tarama tamamlandı.</b>\n\n"
            "Bu ilk çalışmada kıyas yapılmaz; sadece saatlik hacim state dosyası oluşturuldu.\n"
            "Bir sonraki çalışmadan itibaren volume spike yakalanır.\n\n"
            f"ATH-body filtresinden geçen coin sayısı: <b>{len(scan_simple)}</b>\n"
            f"Son tarama: {now_utc()}"
        )
        send_telegram_message(message)

    elif not spikes_simple.empty:
        lines = []
        lines.append("🚨 <b>Volume Spike Bulundu</b>")
        lines.append("")
        lines.append(f"Spike kriteri: {VOLUME_SPIKE_RATIO}x ve üstü")
        lines.append(f"Minimum 1H hacim: {fmt_volume(MIN_1H_VOLUME_USDT)} USDT")
        lines.append(f"Son tarama: {now_utc()}")
        lines.append("")

        for _, row in spikes_simple.head(20).iterrows():
            lines.append(
                f"• <b>{row['symbol']}</b> | "
                f"1H: {row['last_1h_volume']} | "
                f"Önceki: {row['prev_1h_volume']} | "
                f"Oran: {row['volume_ratio']}x | "
                f"24H: {row['volume_24h']} | "
                f"{row['priority']}"
            )

        send_telegram_message("\n".join(lines))

    else:
        if SEND_NO_SPIKE_MESSAGE:
            message = (
                "✅ Tarama bitti. Bu saatte volume spike yok.\n\n"
                f"ATH-body filtresinden geçen coin sayısı: {len(scan_simple)}\n"
                f"Spike kriteri: {VOLUME_SPIKE_RATIO}x\n"
                f"Minimum 1H hacim: {fmt_volume(MIN_1H_VOLUME_USDT)} USDT\n"
                f"Son tarama: {now_utc()}"
            )
            send_telegram_message(message)

    print("\nGenel liste:")
    print(scan_simple)

    print("\nVolume spike listesi:")
    print(spikes_simple)

    print(f"\nYazıldı: {SCAN_CSV}")
    print(f"Yazıldı: {SPIKES_CSV}")
    print(f"Yazıldı: {STATE_CSV}")


if __name__ == "__main__":
    main()
