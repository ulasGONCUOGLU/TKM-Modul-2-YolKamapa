# -*- coding: utf-8 -*-
"""
main.py
=======
Belediye yol kapama duyurularını çeken, AI ile analiz eden, konum
bulan ve Supabase'e yazan ana pipeline.

DEĞİŞİKLİK (Supabase entegrasyonu):
- Kalıcılık katmanı artık SQLite değil, db_supabase.py üzerinden Supabase.
- Harita artık burada üretilmiyor (folium kaldırıldı) — harita Nuxt
  tarafında Leaflet ile doğrudan Supabase'ten okunan verilerle çiziliyor.
- Giriş noktası `run_pipeline()` olarak adlandırıldı; Flask (app.py) bu
  fonksiyonu import edip çağırıyor. Script tek başına da çalıştırılabilir
  (`python main.py`), böylece Flask olmadan da yerelde test edilebilir.

main.py; Gemini, Nominatim, OSRM veya Supabase SDK detaylarını bilmez —
sadece AI_düzenleme.py, konum.py ve db_supabase.py modüllerinin
döndürdüğü sonuçları birbirine bağlar.
"""

from __future__ import annotations

import hashlib
import os
import re
from datetime import date, datetime
from typing import Optional

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import db_supabase as db
from AI_düzenleme import analyze_route
from konum import konum_bul_parcalar

# ============================================================
# AYARLAR
# ============================================================

load_dotenv()

URL = os.getenv("URL")

if not URL:
    raise ValueError("URL .env dosyasında bulunamadı.")

# Kaynak siteye giden isteklerde botu tanıtan bir User-Agent kullanılır.
# İletişim bilgisi eklemek için SOURCE_USER_AGENT'ı .env'de değiştirin.
SOURCE_USER_AGENT = (
    os.getenv("SOURCE_USER_AGENT", "").strip()
    or "YolKapamaBot/1.0 (belediye yol kapama duyuru takibi)"
)


def build_http_session() -> requests.Session:
    """
    Kaynak siteyi çekmek için oturum oluşturur.

    - Geçici hatalarda (429/5xx, bağlantı kopması) 3 kez, artan bekleme ile
      yeniden dener; sunucu Retry-After gönderirse ona uyar.
    - Botu tanıtan User-Agent gönderir.
    """

    retry = Retry(
        total=3,
        backoff_factor=1.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
        respect_retry_after_header=True,
    )

    adapter = HTTPAdapter(max_retries=retry)

    session = requests.Session()
    session.headers.update({"User-Agent": SOURCE_USER_AGENT})
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    return session


# ============================================================
# METİN TEMİZLEME
# ============================================================

def clean_text(text: str) -> str:
    """
    HTML içerisindeki gereksiz boşlukları normalize eder.

    Önemli: anlamlı kelimeleri silmez, sadece çoklu whitespace
    karakterlerini tek boşluğa indirir.
    """

    text = text.replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text)

    return text.strip()


# ============================================================
# TARİH AYRIŞTIRMA
# ============================================================

def parse_dates(text: str):
    """
    Aşağıdaki gibi tarihleri destekler:

    04.08.2026 - 28.08.2026
    04.08.2026 – 28.08.2026 tarihleri arasında
    10.08.2026 tarihinde

    Dönen değer: start_date, end_date
    """

    matches = re.findall(r"\b(\d{2}\.\d{2}\.\d{4})\b", text)

    if not matches:
        return None, None

    parsed_dates = []

    for value in matches:
        try:
            parsed_dates.append(datetime.strptime(value, "%d.%m.%Y").date())
        except ValueError:
            continue

    if not parsed_dates:
        return None, None

    if len(parsed_dates) == 1:
        return parsed_dates[0], parsed_dates[0]

    return parsed_dates[0], parsed_dates[1]


# ============================================================
# AKTİF KAYIT KONTROLÜ
# ============================================================

def is_active(start_date: Optional[date], end_date: Optional[date]) -> bool:
    """Bugün, start_date ile end_date arasındaysa True döner."""

    if start_date is None or end_date is None:
        return False

    today = date.today()

    return start_date <= today <= end_date


# ============================================================
# KAYIT HASH
# ============================================================

def create_record_hash(guzergah: str, tarih: str, kapanma_nedeni: str) -> str:
    raw_value = f"{guzergah.strip()}|{tarih.strip()}|{kapanma_nedeni.strip()}"
    return hashlib.sha256(raw_value.encode("utf-8")).hexdigest()


# ============================================================
# HTML TABLOSUNU BUL
# ============================================================

def baslik_norm(text: str) -> str:
    """Başlık karşılaştırması için: Türkçe harfleri ASCII'ye indirip büyütür.

    Python'da "Tarih".upper() == "TARIH" (noktasız I) olur ve "TARİH" ile
    eşleşmez; bu yüzden iki tarafı da aynı biçime getiriyoruz.
    """
    tablo = str.maketrans("İıŞşĞğÜüÖöÇç", "IISSGGUUOOCC")
    return " ".join(clean_text(text).translate(tablo).upper().split())


def find_target_table(soup: BeautifulSoup):
    """GÜZERGAH, TARİH ve KAPANMA NEDENİ başlıklarını içeren tabloyu bulur."""

    for table in soup.find_all("table"):

        cells = table.find_all(["th", "td"])

        headers = [
            baslik_norm(cell.get_text(" ", strip=True))
            for cell in cells
        ]

        if (
            "GUZERGAH" in headers
            and "TARIH" in headers
            and "KAPANMA NEDENI" in headers
        ):
            return table

    return None


# ============================================================
# TABLO BAŞLIKLARINI BUL
# ============================================================

def find_column_indexes(table):
    """
    Sütunların kaçıncı sırada olduğunu başlık adına göre bulur.
    Böylece sütun sırası değişse bile kod çalışmaya devam eder.
    """

    rows = table.find_all("tr")

    if not rows:
        raise ValueError("Tabloda satır bulunamadı.")

    for row in rows:

        cells = row.find_all(["th", "td"])

        values = [
            baslik_norm(cell.get_text(" ", strip=True))
            for cell in cells
        ]

        if (
            "GUZERGAH" in values
            and "TARIH" in values
            and "KAPANMA NEDENI" in values
        ):

            indexes = {
                "guzergah": values.index("GUZERGAH"),
                "tarih": values.index("TARIH"),
                "kapanma_nedeni": values.index("KAPANMA NEDENI"),
                "header_row": row,
            }

            if "ILCE-MAHALLE" in values:
                indexes["ilce_mahalle"] = values.index("ILCE-MAHALLE")
            else:
                indexes["ilce_mahalle"] = None

            return indexes

    raise ValueError("GÜZERGAH, TARİH ve KAPANMA NEDENİ başlıkları bulunamadı.")


# ============================================================
# TABLO KAYITLARINI ÇIKAR
# ============================================================

def extract_records(table):
    """Target table içerisinden istediğimiz sütunları çıkarır."""

    columns = find_column_indexes(table)

    guzergah_index = columns["guzergah"]
    tarih_index = columns["tarih"]
    neden_index = columns["kapanma_nedeni"]
    ilce_mahalle_index = columns["ilce_mahalle"]

    records = []

    for row in table.find_all("tr"):

        if row is columns["header_row"]:
            continue

        cells = row.find_all("td")

        required_indexes = [guzergah_index, tarih_index, neden_index]

        if ilce_mahalle_index is not None:
            required_indexes.append(ilce_mahalle_index)

        max_index = max(required_indexes)

        if len(cells) <= max_index:
            continue

        guzergah = clean_text(cells[guzergah_index].get_text(" ", strip=True))
        tarih = clean_text(cells[tarih_index].get_text(" ", strip=True))
        kapanma_nedeni = clean_text(cells[neden_index].get_text(" ", strip=True))

        ilce_mahalle_raw = None
        ilce = None
        mahalle = None

        if ilce_mahalle_index is not None:
            ilce_mahalle_raw = clean_text(
                cells[ilce_mahalle_index].get_text(" ", strip=True)
            )
            ilce, mahalle = split_ilce_mahalle(ilce_mahalle_raw)

        if not guzergah and not tarih and not kapanma_nedeni:
            continue

        records.append(
            {
                "guzergah": guzergah,
                "tarih": tarih,
                "kapanma_nedeni": kapanma_nedeni,
                "ilce_mahalle_raw": ilce_mahalle_raw,
                "ilce": ilce,
                "mahalle": mahalle,
            }
        )

    return records


# ============================================================
# İLÇE-MAHALLE HÜCRESİNİ AYRIŞTIR
# ============================================================

def split_ilce_mahalle(ilce_mahalle_raw: str | None):
    """
    Kaynak sitedeki "İLÇE-MAHALLE" hücresini ayrıştırır.

    Örnek:
        "Pamukkale-Pınarkent"   -> ("Pamukkale", "Pınarkent")
        "Merkezefendi-Şemikler" -> ("Merkezefendi", "Şemikler")
    """

    if not ilce_mahalle_raw:
        return None, None

    parts = ilce_mahalle_raw.split("-", 1)

    if len(parts) == 2:
        ilce = parts[0].strip()
        mahalle = parts[1].strip()
        return ilce or None, mahalle or None

    return ilce_mahalle_raw.strip() or None, None


# ============================================================
# KONUM BULMA (TEK KAYIT İÇİN - ORTAK ADIM)
# ============================================================

def _process_konum_for_record(
    record_id,
    ai_result,
    web_mahalle,
    web_ilce,
):
    """
    Elde zaten bir ai_result varken konum.konum_bul_parcalar() ile her parçayı konumlandırıp
    sonucu Supabase'e kaydeden ortak adım.
    """

    effective_neighborhood = web_mahalle or ai_result.get("neighborhood")
    effective_district = web_ilce or ai_result.get("district")

    print(f"AI konum tipi : {ai_result.get('location_type')}")
    print(
        f"Mahalle       : {effective_neighborhood} "
        f"(AI: {ai_result.get('neighborhood')}, Web: {web_mahalle})"
    )
    print(
        f"İlçe          : {effective_district} "
        f"(AI: {ai_result.get('district')}, Web: {web_ilce})"
    )
    print(f"Confidence    : {ai_result.get('confidence')}")

    sonuc = konum_bul_parcalar(
        ai_result,
        web_mahalle=web_mahalle,
        web_ilce=web_ilce,
    )

    for parca in sonuc.parcalar:
        if parca.sonuc is None:
            print(f"  [bulunamadı] {parca.etiket}")
        else:
            print(f"  [{parca.kesinlik}] {parca.etiket} -> {parca.sonuc.gosterim_adi}")
            if parca.sonuc.uyari:
                print(f"    UYARI: {parca.sonuc.uyari}")

    geocode_sonucu = sonuc.geocode_sonucu()

    if geocode_sonucu is None:
        print("Konum bulunamadı.")
        db.save_geocode_result(record_id, None)
        return

    print(f"Konum bulundu: {geocode_sonucu['bulunan']}/{geocode_sonucu['toplam']} parça")

    db.save_geocode_result(record_id, geocode_sonucu)
    db.save_geometry(record_id, sonuc.geometri())


# ============================================================
# AI + KONUM (BEKLEYEN/EKSİK KAYITLARI İŞLE)
# ============================================================

def process_pending_ai(pending_records):
    """
    Her bekleyen/eksik kayıt için:
    1. AI_düzenleme.analyze_route() ile analiz ettirir, sonucu Supabase'e yazar.
    2. _process_konum_for_record() ile konum.py'yi çağırıp sonucu kaydeder.
    """

    if not pending_records:
        print("AI analizi bekleyen/eksik kayıt yok.")
        return

    print(f"\n{len(pending_records)} kayıt için AI analizi başlıyor...")

    for row in pending_records:

        record_id = row["id"]
        guzergah_raw = row["guzergah_raw"]
        web_ilce = row.get("ilce")
        web_mahalle = row.get("mahalle")

        print(f"\n[{record_id}] {guzergah_raw}")

        if web_ilce or web_mahalle:
            print(f"Kaynak ilçe/mahalle: {web_ilce} / {web_mahalle}")

        ai_result = analyze_route(guzergah_raw)

        if ai_result is None:
            print("AI analizi başarısız, kayıt atlanıyor.")
            db.save_ai_error(record_id, "AI analizi başarısız oldu.")
            continue

        db.save_ai_result(record_id, ai_result)

        _process_konum_for_record(record_id, ai_result, web_mahalle, web_ilce)


# ============================================================
# SADECE KONUM (AI ZATEN BAŞARILI AMA KOORDİNAT/GEOMETRİ EKSİK)
# ============================================================

def process_pending_geocode(pending_records):
    """
    AI analizi başarıyla tamamlanmış ama konum/koordinat hâlâ
    bulunamamış kayıtları, AI'yı TEKRAR ÇAĞIRMADAN, kayıtlı ai_result
    kullanarak sadece konum.py üzerinden yeniden dener.
    """

    if not pending_records:
        print("Konumu eksik/başarısız kayıt yok.")
        return

    print(f"\n{len(pending_records)} kayıt için konum yeniden deneniyor...")

    for row in pending_records:

        record_id = row["id"]
        guzergah_raw = row["guzergah_raw"]
        web_ilce = row.get("ilce")
        web_mahalle = row.get("mahalle")
        ai_result = row.get("ai_result")

        print(f"\n[{record_id}] {guzergah_raw}")

        if not ai_result:
            print("Kayıtlı AI sonucu bulunamadı, atlanıyor.")
            continue

        _process_konum_for_record(record_id, ai_result, web_mahalle, web_ilce)


# ============================================================
# ANA PROGRAM (Flask'ın çağırdığı giriş noktası)
# ============================================================

def _adm_adimi() -> None:
    """
    ADM (Aydem) planlı çalışmalarını çeker. Yol kapama adımından BAĞIMSIZDIR:
    ADM tarafında bir sorun olursa yol kapamalar etkilenmez; sorun
    adm_sync_durumu tablosuna yazılır ve sitede gösterilir. Bu fonksiyon istisna fırlatmaz.
    """

    if os.getenv("ADM_AKTIF", "1").strip().lower() in ("0", "false", "hayir", "hayır"):
        print("ADM adımı kapalı (ADM_AKTIF=0).")
        return

    print("ADM planlı çalışmaları çekiliyor...")

    try:
        import adm

        sonuc = adm.run_adm_sync(db)
        print(f"ADM: {sonuc['durum']} - {sonuc['kayit_sayisi']} kayıt")
        for ilce, mesaj in sonuc["hatalar"].items():
            print(f"ADM uyarı ({ilce}): {mesaj}")
    except Exception as e:  # noqa: BLE001
        # Örn. shapely kurulu değil. Sebebi siteye de yazmaya çalış.
        print(f"ADM adımı çalıştırılamadı: {e}")
        try:
            db.save_adm_sync_durumu(
                {
                    "id": 1,
                    "durum": "hata",
                    "son_deneme": datetime.now().isoformat(),
                    "hata_mesaji": (
                        f"ADM adımı çalıştırılamadı ({type(e).__name__}: {e}). "
                        "Eksik paket olabilir: pip install -r requirements.txt"
                    ),
                }
            )
        except Exception as e2:  # noqa: BLE001
            print(f"ADM durumu yazılamadı: {e2}")


def run_pipeline() -> None:
    """
    Tüm pipeline'ı bir kez baştan sona çalıştırır:
      1) yol kapama: scrape -> Supabase'e kaydet -> AI analizi -> konum bulma
      2) ADM planlı çalışmaları (adm.py) — 1. adım hata verse de çalışır.

    Flask (app.py) bu fonksiyonu arka plan thread'inde çağırır.
    Script tek başına da çalıştırılabilir: `python main.py`
    """

    yol_kapama_hatasi: Optional[BaseException] = None

    try:
        _yol_kapama_adimi()
    except Exception as e:  # noqa: BLE001
        yol_kapama_hatasi = e
        print(f"Yol kapama adımı hata verdi: {e}")

    _adm_adimi()

    if yol_kapama_hatasi is not None:
        # Site "Tekrar Dene" göstersin: hata yol kapama tarafında
        raise yol_kapama_hatasi

    print("İşlem tamamlandı.")


def _yol_kapama_adimi() -> None:
    """Belediye yol kapama duyurularını çeker, AI ile analiz eder, konumlandırır."""

    print("Yol Kapama pipeline'ı başlatılıyor...")
    print(f"URL: {URL}")

    # --------------------------------------------------------
    # Web sitesi
    # --------------------------------------------------------

    with build_http_session() as session:
        response = session.get(URL, timeout=30)

    response.raise_for_status()

    print(f"HTTP Durum Kodu: {response.status_code}")

    # --------------------------------------------------------
    # BeautifulSoup
    # --------------------------------------------------------

    soup = BeautifulSoup(response.text, "html.parser")

    target_table = find_target_table(soup)

    if target_table is None:
        ipucu = ""
        if "içerik bulunamadı" in response.text:
            ipucu = (
                " Sayfa 'Aradığınız içerik bulunamadı' diyor: .env içindeki URL "
                "artık geçerli değil. Güncel adres: "
                "https://www.denizli.bel.tr/hizli-erisim/trafige-kapali-yollar"
            )
        raise RuntimeError(
            "GÜZERGAH, TARİH ve KAPANMA NEDENİ sütunlarını içeren tablo bulunamadı."
            + ipucu
        )

    print("Hedef tablo bulundu.")

    # --------------------------------------------------------
    # 3 sütunu çıkar
    # --------------------------------------------------------

    records = extract_records(target_table)

    print(f"Toplam {len(records)} kayıt bulundu.")

    # --------------------------------------------------------
    # Supabase'e kaydet
    # --------------------------------------------------------

    new_count = 0
    existing_count = 0

    for record in records:

        record_hash = create_record_hash(
            record["guzergah"], record["tarih"], record["kapanma_nedeni"]
        )

        start_date_value, end_date_value = parse_dates(record["tarih"])
        active = is_active(start_date_value, end_date_value)

        inserted = db.save_record(
            record, record_hash, start_date_value, end_date_value, active
        )

        if inserted:
            new_count += 1
        else:
            existing_count += 1

    print(f"Yeni kayıt: {new_count}")
    print(f"Zaten kayıtlı: {existing_count}")

    # --------------------------------------------------------
    # AKTİF DURUMUNU BUGÜNE GÖRE YENİDEN HESAPLA
    # --------------------------------------------------------

    db.refresh_active_status()

    # --------------------------------------------------------
    # AI'YA GİDECEK KAYITLAR
    # --------------------------------------------------------

    pending_records = db.get_pending_ai_records()

    print(f"AI analizi bekleyen/eksik aktif kayıt: {len(pending_records)}")

    process_pending_ai(pending_records)

    # --------------------------------------------------------
    # SADECE KONUMU EKSİK KALAN KAYITLAR
    # --------------------------------------------------------

    pending_geocode_records = db.get_pending_geocode_records()

    print(f"Konumu eksik/başarısız aktif kayıt: {len(pending_geocode_records)}")

    process_pending_geocode(pending_geocode_records)


if __name__ == "__main__":
    run_pipeline()
