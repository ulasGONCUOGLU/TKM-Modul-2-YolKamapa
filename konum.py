# -*- coding: utf-8 -*-
"""
konum.py
========
İl / İlçe / Mahalle / Sokak bilgisinden Nominatim (OpenStreetMap) geocoding
servisini kullanarak koordinat (enlem, boylam - WGS84) ve mümkünse yol
geometrisi (LineString) bulan modül.

Bu dosya, önceki "konbul.py" prototipinin üzerine main.py'nin ihtiyaç
duyduğu tek bir giriş fonksiyonunu ekler: `konum_bul_ai(ai_result, ...)`.

main.py bu modülden SADECE `konum_bul_ai(...)` fonksiyonunu çağırır ve
karşılığında bir AdresSonucu nesnesi (ya da bulunamazsa None) alır.
main.py, Nominatim/OSRM detaylarını, sokak varyasyonu deneme mantığını
vb. bilmek zorunda değildir.

Kullanım kuralları (ÖNEMLİ):
- Nominatim'in kullanım politikası gereği saniyede en fazla 1 istek atılmalı
  ve isteklerde tanımlayıcı bir User-Agent gönderilmeli. Bu modül ikisini de
  otomatik yapar (istekler arası minimum bekleme + sabit User-Agent).
- Yoğun/production kullanım için kendi Nominatim sunucunuzu kurmanız ya da
  ücretli bir sağlayıcıya (Google, HERE, Mapbox vb.) geçmeniz önerilir.

Geometri stratejisi (road / road_segment):
1. Önce Nominatim'den, yolun kendi geometrisi (polygon_geojson ->
   LineString) istenir. Bulunursa (tam yol/cadde çizgisi) bu kullanılır,
   OSRM'e gidilmez.
2. road_segment için Nominatim'den tam çizgi gelmezse, start_query ve
   end_query ayrı ayrı geocode edilir ve aralarında OSRM ile sürüş
   rotası (LineString) hesaplanır (yedek/son çare).
3. Hiçbiri olmazsa, bulunabilen tek nokta (Point) döner.
"""

from __future__ import annotations

import heapq
import json
import math
import os
import re
import sqlite3
import time
import unicodedata
import warnings
from contextlib import closing
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import requests
from dotenv import load_dotenv

load_dotenv()

# --------------------------------------------------------------------------- #
# AYARLAR (hepsi .env / ortam değişkeniyle değiştirilebilir, bkz. .env.example)
# --------------------------------------------------------------------------- #

def _nominatim_search_url() -> str:
    """NOMINATIM_URL tam '/search' adresi ya da sadece taban adres olabilir."""
    # `or`: .env.example'dan kopyalanan boş değerler ("NOMINATIM_URL=") varsayılana düşsün.
    taban = (os.getenv("NOMINATIM_URL") or "https://nominatim.openstreetmap.org").strip().rstrip("/")
    return taban if taban.endswith("/search") else f"{taban}/search"


NOMINATIM_URL = _nominatim_search_url()
OSRM_URL = (os.getenv("OSRM_URL") or "https://router.project-osrm.org/route/v1/driving").strip().rstrip("/")

TIMEOUT = 10  # saniye (Nominatim)
OSRM_TIMEOUT = 30  # saniye (OSRM)

# Nominatim kuralı: tüm uygulama için saniyede en fazla 1 istek. Genel OSM
# sunucusunda, düzenli/zamanlanmış çalışan betikler için politika bundan da
# katıdır (dakikada 4 istek). Bu nedenle yoğun/sık çalışan kurulumlarda kendi
# Nominatim sunucunuza geçin (NOMINATIM_URL) ya da aşağıdaki önbelleği açık tutun.
# Politika: https://operations.osmfoundation.org/policies/nominatim/
MIN_ISTEK_ARALIGI = float(os.getenv("NOMINATIM_MIN_INTERVAL") or "1.1")  # saniye

# Nominatim, User-Agent'sız veya jenerik/yer tutucu User-Agent'lı istekleri
# engelleyebilir. GEOCODER_CONTACT'a iletişim e-postanızı/sitenizi yazın;
# ya da tamamını NOMINATIM_USER_AGENT ile verin.
_GEOCODER_CONTACT = os.getenv("GEOCODER_CONTACT", "").strip()
_USER_AGENT = os.getenv("NOMINATIM_USER_AGENT", "").strip() or (
    f"YolKapama-konum/1.0 ({_GEOCODER_CONTACT or 'iletisim-bilgisi-tanimsiz'})"
)

if not os.getenv("NOMINATIM_USER_AGENT", "").strip() and not _GEOCODER_CONTACT:
    warnings.warn(
        "GEOCODER_CONTACT (veya NOMINATIM_USER_AGENT) tanımlı değil: Nominatim "
        "politikası uygulamayı tanıtan geçerli bir User-Agent ister. "
        ".env dosyasına iletişim bilginizi ekleyin.",
        stacklevel=2,
    )

HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept-Language": "tr",
}

# Nominatim yanıt önbelleği (politika: toplu/tekrarlayan geocoding'de önbellek
# zorunludur). Aynı sorgu tekrar tekrar sunucuya gitmez; bulunamayan sorgular
# da kısa süre hatırlanır, böylece 'not_found' kayıtlar her çalıştırmada aynı
# istekleri yeniden atmaz. GEOCODE_CACHE_PATH boş bırakılırsa önbellek kapanır.
CACHE_DB_PATH = os.getenv("GEOCODE_CACHE_PATH", "output/geocode_cache.sqlite")
CACHE_TTL_SANIYE = int(float(os.getenv("GEOCODE_CACHE_TTL_DAYS") or "30") * 86400)
CACHE_NEGATIF_TTL_SANIYE = int(float(os.getenv("GEOCODE_CACHE_NEGATIVE_TTL_DAYS") or "3") * 86400)

_son_istek_zamani = 0.0


def _rate_limit_bekle() -> None:
    """Nominatim kuralına uymak için istekler arasında en az MIN_ISTEK_ARALIGI kadar bekler."""
    global _son_istek_zamani
    gecen = time.monotonic() - _son_istek_zamani
    if gecen < MIN_ISTEK_ARALIGI:
        time.sleep(MIN_ISTEK_ARALIGI - gecen)
    _son_istek_zamani = time.monotonic()


@dataclass
class AdresSonucu:
    lat: float
    lon: float
    gosterim_adi: str  # Nominatim'in döndürdüğü tam adres metni
    tip: str = ""       # örn. "residential", "highway" vb. (osm class/type)
    onem: float = 0.0    # Nominatim "importance" skoru (sıralama için)
    geojson: Optional[Dict[str, Any]] = None  # varsa çizgi/alan geometrisi (WGS84)
    arama_terimi: str = ""       # ne aradık (örn. "10157 Sokak")
    bulunan_yol_adi: str = ""    # Nominatim'in gerçekte döndürdüğü sokak/cadde adı
    tam_eslesme: bool = True     # aranan ile bulunan GERÇEKTEN aynı sokak mı?
    uyari: Optional[str] = None  # tam_eslesme False ise açıklayıcı uyarı metni
    kaynak: str = "nominatim"    # "nominatim" ya da "osrm" (rota kaynağı)
    ham_veri: Optional[Dict[str, Any]] = field(default=None)

    @property
    def koordinat(self):
        return (self.lat, self.lon)

    @property
    def cizgi_mi(self) -> bool:
        """Sonuç bir sokak/cadde gibi çizgi (LineString/MultiLineString) mi?"""
        return bool(self.geojson and "LineString" in self.geojson.get("type", ""))

    @property
    def geometry_type(self) -> str:
        """main.py'nin DB'ye yazdığı geometry_type ile uyumlu bir değer döndürür."""
        if self.geojson:
            return self.geojson.get("type", "Point")
        return "Point"

    def __str__(self) -> str:
        geo_bilgi = f" [{self.geojson.get('type')}]" if self.geojson else ""
        durum = "✓ TAM EŞLEŞME" if self.tam_eslesme else f"⚠ YAKLAŞIK ({self.uyari})"
        return f"{self.gosterim_adi} -> {self.lat:.6f}, {self.lon:.6f}{geo_bilgi} [{durum}]"


def _sorgu_metni_olustur(
    sokak: Optional[str] = None,
    mahalle: Optional[str] = None,
    ilce: Optional[str] = None,
    il: str = "Denizli",
    ulke: str = "Türkiye",
) -> str:
    """Verilen adres parçalarını Nominatim'in sevdiği serbest metin formatına birleştirir."""
    parcalar = [p.strip() for p in (sokak, mahalle, ilce, il, ulke) if p and p.strip()]
    return ", ".join(parcalar)


def _cache_anahtari(sorgu: str, limit: int) -> str:
    """Önbellek anahtarı: sunucu + normalize sorgu + limit (büyük/küçük harf ve boşluk farkı yok sayılır)."""
    temiz = " ".join(_tr_kucuk(sorgu).split())
    return json.dumps([NOMINATIM_URL, temiz, limit], ensure_ascii=False)


def _cache_baglan() -> sqlite3.Connection:
    klasor = os.path.dirname(CACHE_DB_PATH)
    if klasor:
        os.makedirs(klasor, exist_ok=True)
    conn = sqlite3.connect(CACHE_DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS nominatim_cache (
            cache_key TEXT PRIMARY KEY,
            response  TEXT NOT NULL,
            created_at REAL NOT NULL
        )
        """
    )
    return conn


def _cache_oku(anahtar: str) -> Optional[List[Dict[str, Any]]]:
    """Süresi dolmamış önbellek kaydını döndürür; yoksa/dolmuşsa/bozuksa None."""
    if not CACHE_DB_PATH:
        return None
    try:
        with closing(_cache_baglan()) as conn:
            satir = conn.execute(
                "SELECT response, created_at FROM nominatim_cache WHERE cache_key = ?",
                (anahtar,),
            ).fetchone()
        if satir is None:
            return None
        veri = json.loads(satir[0])
        ttl = CACHE_TTL_SANIYE if veri else CACHE_NEGATIF_TTL_SANIYE
        if time.time() - satir[1] > ttl:
            return None
        return veri
    except (sqlite3.Error, ValueError, OSError):
        return None  # önbellek bozuksa/kilitliyse sessizce atla, gerçek istek atılsın


def _cache_yaz(anahtar: str, veri: List[Dict[str, Any]]) -> None:
    if not CACHE_DB_PATH:
        return
    try:
        with closing(_cache_baglan()) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO nominatim_cache (cache_key, response, created_at) VALUES (?, ?, ?)",
                (anahtar, json.dumps(veri, ensure_ascii=False), time.time()),
            )
            conn.commit()
    except (sqlite3.Error, OSError):
        pass  # önbelleğe yazılamaması sonucu etkilememeli


def _nominatim_ara(sorgu: str, limit: int = 5, session: Optional[requests.Session] = None) -> List[Dict[str, Any]]:
    """
    Nominatim /search uç noktasına istek atar, ham JSON sonuç listesini döndürür.

    Önce yerel önbelleğe bakılır; isabet varsa ağa HİÇ çıkılmaz ve hız sınırı
    beklemesi de uygulanmaz. Yalnızca başarılı (HTTP 2xx) yanıtlar önbelleğe
    yazılır; ağ/HTTP hataları önbelleğe alınmaz.
    """
    anahtar = _cache_anahtari(sorgu, limit)
    onbellekten = _cache_oku(anahtar)
    if onbellekten is not None:
        return onbellekten

    _rate_limit_bekle()
    sess = session or requests
    params = {
        "q": sorgu,
        "format": "jsonv2",
        "addressdetails": 1,
        "limit": limit,
        "countrycodes": "tr",
        "polygon_geojson": 1,  # sokak/cadde için LineString, alan için Polygon geometrisi de gelsin
    }
    resp = sess.get(NOMINATIM_URL, params=params, headers=HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    veri = resp.json()
    _cache_yaz(anahtar, veri)
    return veri


_NUMARALI_SOKAK_RE = re.compile(
    r"^\s*(\d+(?:\s*/\s*\d+)?)\s*\.?\s*(?:sokağın|sokağa|sokağı|sokak|sok|sk)\s*\.?\s*$",
    re.IGNORECASE,
)


def kanonik_yol_adi(ad: Optional[str]) -> Optional[str]:
    """
    Rakamlı sokak adlarını tek biçime getirir: "3131. Sokak", "3131 Sk.",
    "3131 Sokağın" -> "3131 Sokak". Diğer adlara yalnızca boşluk düzeltmesi
    uygulanır. Yapay zeka promptu da aynı biçimi ister; burası ikinci güvencedir.
    """
    if not ad:
        return ad
    ad = " ".join(ad.split())
    m = _NUMARALI_SOKAK_RE.match(ad)
    if m:
        sayi = re.sub(r"\s*/\s*", "/", m.group(1))
        return f"{sayi} Sokak"
    return ad


def _sokak_varyasyonlari(sokak: str) -> List[str]:
    """
    Sokak adını, sonundan kelime kelime kısaltarak varyasyon listesi üretir.

    Örnek: "Fatih Sultan Mehmet /1 Caddesi"
        -> "Fatih Sultan Mehmet /1 Caddesi"
        -> "Fatih Sultan Mehmet /1"
        -> "Fatih Sultan Mehmet"
        -> "Fatih Sultan"
        -> "Fatih"

    Nominatim'de "Caddesi/Sokak/Bulvarı" gibi ek kelimeler bazen adres
    kayıtlarıyla birebir uyuşmuyor; bu yüzden en spesifikten en kısaya doğru
    dener ve ilk eşleşeni kullanır.
    """
    kelimeler = sokak.strip().split()
    if not kelimeler:
        return []

    varyasyonlar = []
    for i in range(len(kelimeler), 0, -1):
        aday = " ".join(kelimeler[:i]).strip(" -–,")
        if aday and aday not in varyasyonlar:
            varyasyonlar.append(aday)

    # OSM'de Türkiye'deki numaralı sokaklar hem "3131 Sokak" hem "3131. Sokak"
    # biçiminde kayıtlı olabilir; ikisini de dene (kanonik biçim önce).
    m = _NUMARALI_SOKAK_RE.match(sokak.strip())
    if m:
        sayi = re.sub(r"\s*/\s*", "/", m.group(1))
        return [f"{sayi} Sokak", f"{sayi}. Sokak", sayi]
    return varyasyonlar


# Karşılaştırma _normalize_basit() ile ASCII'ye çevrilmiş kelimeler üzerinden
# yapıldığı için buradaki kelimeler de ASCII olmalı ("sokağı" -> "sokagi").
_YOL_EK_KELIMELERI = {
    "sokak", "sokagi", "sok", "sk", "cadde", "caddesi", "cad", "cd",
    "bulvar", "bulvari", "bulv", "blv", "mahallesi", "mah",
}


_TR_SADELESTIR = str.maketrans({
    "ı": "i", "ş": "s", "ğ": "g", "ü": "u", "ö": "o", "ç": "c",
    "â": "a", "î": "i", "û": "u",
})


def _tr_kucuk(text: str) -> str:
    """
    Türkçe kurallarına uygun küçük harfe çevirme.

    Python'un str.lower()'ı "İ" harfini "i" + birleşik nokta (U+0307) çiftine
    çevirir ve "I"yı "i" yapar (Türkçede "ı" olmalı). Bu yüzden "İSTİKLAL"
    ile "İstiklal"/"Istiklal" karşılaştırması sessizce başarısız olurdu.
    """
    if not text:
        return ""
    return text.replace("İ", "i").replace("I", "ı").lower()


def _normalize_basit(text: str) -> str:
    """
    Türkçe karakterleri sadeleştirip küçük harfe çevirir (karşılaştırma için).

    Büyük harfli yazımlar (örn. kaynak sitedeki "ŞİRİNKÖY") ile OSM'deki
    normal yazımlar ("Şirinköy") aynı sonucu verir.
    """
    if not text:
        return ""
    text = _tr_kucuk(text.strip()).translate(_TR_SADELESTIR)
    # Kalan birleşik işaretleri (aksan/nokta) at.
    text = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def _sayi_cek(text: str) -> Optional[str]:
    """Metindeki ilk sayı dizisini çeker (örn. '10157 Sk' -> '10157')."""
    sayi = ""
    for ch in text:
        if ch.isdigit():
            sayi += ch
        elif sayi:
            break
    return sayi or None


def _cekirdek_isim(text: str) -> str:
    """Yol adından ek kelimeleri (Sokak/Cadde/Bulvar vb.) atıp çekirdek ismi döndürür."""
    kelimeler = _normalize_basit(text).replace(".", "").replace("/", " ").split()
    kalan = [k for k in kelimeler if k not in _YOL_EK_KELIMELERI]
    return " ".join(kalan)


def _eslesme_kontrolu(arama_terimi: str, bulunan_yol_adi: str) -> tuple:
    """
    Aranan sokak adı ile Nominatim'in gerçekte döndürdüğü sokak adını karşılaştırır.

    Döndürür: (tam_eslesme: bool, uyari: Optional[str])
    """
    if not bulunan_yol_adi:
        return True, None  # karşılaştıracak bir şey yok, varsayılan olarak güven

    arama_sayi = _sayi_cek(arama_terimi)
    bulunan_sayi = _sayi_cek(bulunan_yol_adi)

    # --- Numaralı sokak durumu (10157 Sokak gibi) - EN KRİTİK KONTROL ---
    if arama_sayi:
        if bulunan_sayi and bulunan_sayi != arama_sayi:
            return False, (
                f"'{arama_terimi}' arandı ama '{bulunan_yol_adi}' bulundu "
                f"(sokak numarası uyuşmuyor: {arama_sayi} != {bulunan_sayi})"
            )
        if not bulunan_sayi:
            return False, (
                f"'{arama_terimi}' arandı ama bulunan '{bulunan_yol_adi}' "
                f"numarasız/farklı bir sokak"
            )
        return True, None  # sayılar eşleşiyor

    # --- İsimli sokak durumu (Hasan Gönüllü Sokak gibi) ---
    cekirdek_arama = _cekirdek_isim(arama_terimi)
    cekirdek_bulunan = _cekirdek_isim(bulunan_yol_adi)

    if not cekirdek_arama:
        return True, None

    if cekirdek_arama == cekirdek_bulunan:
        return True, None
    if cekirdek_arama in cekirdek_bulunan or cekirdek_bulunan in cekirdek_arama:
        # Kısaltma denemesi (örn. "Fatih Sultan Mehmet" içinde "Fatih Sultan") - kabul et
        return True, None

    return False, (
        f"'{arama_terimi}' arandı ama '{bulunan_yol_adi}' bulundu - isimler örtüşmüyor"
    )


_IDARI_SINIR_TIPLERI = {
    "administrative", "suburb", "neighbourhood", "neighborhood", "quarter",
    "city_district", "borough", "town", "village", "municipality", "city",
    "county", "state_district", "region",
}


def _idari_sinir_mi(item: Dict[str, Any]) -> bool:
    """
    Nominatim sonucunun bir SOKAK/CADDE değil, bir idari sınır
    (mahalle/ilçe/il vb. poligonu) olup olmadığını tespit eder.
    """
    osm_class = (item.get("class") or "").lower()
    osm_type = (item.get("type") or "").lower()
    if osm_class == "boundary":
        return True
    if osm_type in _IDARI_SINIR_TIPLERI:
        return True
    return False


# Nominatim'in address bloğunda mahalle/semt bilgisini taşıyabilecek alanlar.
_MAHALLE_ADRES_ALANLARI = (
    "suburb", "neighbourhood", "quarter", "city_district",
    "borough", "town", "village", "hamlet",
)


def _mahalle_uyusuyor_mu(
    address: Dict[str, Any],
    hedef_mahalle: Optional[str],
) -> Optional[bool]:
    """
    Nominatim sonucunun address bloğundaki mahalle/semt alanlarını hedef
    mahalle ile karşılaştırır.

    Bu kontrol, sokak adı aynı olsa bile YANLIŞ mahalledeki (ör. "Süleyman
    Demirel Caddesi" hem Zeytinköy'de hem Güzelköy'de geçiyorsa) bir sonucun
    sessizce kabul edilmesini engellemek için var.

    Dönüş:
        True  -> en az bir adres alanı hedef mahalle ile eşleşiyor
        False -> adres alanları var ama HİÇBİRİ eşleşmiyor (kesin FARKLI
                 bir mahalle) -> bu sonuç asla kabul edilmemeli
        None  -> hedef mahalle verilmemiş, ya da sonuçta hiç mahalle/semt
                 bilgisi yok (doğrulanamıyor, temkinli kabul edilebilir)
    """
    if not hedef_mahalle:
        return None

    hedef_norm = _normalize_basit(hedef_mahalle)

    if not hedef_norm:
        return None

    bulunan_degerler = [
        address.get(alan)
        for alan in _MAHALLE_ADRES_ALANLARI
        if address.get(alan)
    ]

    if not bulunan_degerler:
        return None  # doğrulanamıyor

    for deger in bulunan_degerler:
        deger_norm = _normalize_basit(deger)
        if hedef_norm == deger_norm or hedef_norm in deger_norm or deger_norm in hedef_norm:
            return True

    return False


def _dene(
    sorgu: str,
    arama_terimi: str = "",
    mahalle: Optional[str] = None,
    debug: bool = False,
    session: Optional[requests.Session] = None,
) -> Optional[AdresSonucu]:
    """
    Tek bir sorgu metnini Nominatim'e gönderir, en iyi (ilk) sonucu
    AdresSonucu olarak döndürür.

    `mahalle` verilmişse (ve sokak aranıyorsa), sonuçlar programatik olarak
    da mahalle bilgisine göre süzülür: adres bloğunda AÇIKÇA FARKLI bir
    mahalle/semt yazan sonuçlar -- sorgu metninde mahalle geçmese bile --
    tamamen elenir. Böylece "sokak + mahalle" ile bulunamayan bir sonuç,
    mahallesiz denemede rastgele başka bir mahalledeki aynı isimli sokağa
    sessizce kaymaz.
    """
    try:
        sonuclar_ham = _nominatim_ara(sorgu, session=session)
    except requests.RequestException as e:
        if debug:
            print(f"[UYARI] '{sorgu}' sorgusunda ağ hatası: {e}")
        return None

    if debug:
        print(f"Sorgu: '{sorgu}' -> {len(sonuclar_ham)} sonuç")
        for s in sonuclar_ham:
            print("  ", s.get("display_name"), "|", s.get("lat"), s.get("lon"),
                  "| importance:", s.get("importance"))

    if not sonuclar_ham:
        return None

    if arama_terimi:
        # SOKAK arıyorsak: mahalle/ilçe/il gibi idari sınır sonuçlarını
        # TAMAMEN ele - bunlar hiçbir zaman (yaklaşık olarak bile) sokak
        # yerine geçmemeli. Sadece yol-seviyeli (highway vb.) sonuçları al.
        aday_liste = [s for s in sonuclar_ham if not _idari_sinir_mi(s)]
        if not aday_liste:
            if debug:
                print("  [!] Yalnızca idari sınır (mahalle/ilçe) sonucu geldi, "
                      "sokak seviyesinde eşleşme yok -> bu sonuç TAMAMEN reddedildi")
            return None

        # --------------------------------------------------------
        # MAHALLE DOĞRULAMASI (kritik): sorgu metninde mahalle geçmese
        # bile, dönen sonucun adres bloğu AÇIKÇA farklı bir mahalleye
        # aitse o aday tamamen elenir. Aynı isimli sokak başka bir
        # mahallede varsa oraya kaymasını engeller.
        # --------------------------------------------------------
        mahalle_belirsiz = False

        if mahalle:
            eslesenler = []
            belirsizler = []

            for s in aday_liste:
                durum = _mahalle_uyusuyor_mu(s.get("address", {}), mahalle)
                if durum is True:
                    eslesenler.append(s)
                elif durum is None:
                    belirsizler.append(s)
                # durum is False -> tamamen ele, listeye alma

            if eslesenler:
                aday_liste = eslesenler
            elif belirsizler:
                aday_liste = belirsizler
                mahalle_belirsiz = True
            else:
                if debug:
                    print(f"  [!] '{mahalle}' mahallesiyle eşleşen sonuç yok, "
                          f"tüm adaylar FARKLI bir mahalleye ait -> TAMAMEN reddedildi")
                return None
    else:
        aday_liste = sonuclar_ham
        mahalle_belirsiz = False

    # Nominatim sonuçları "importance" skoruna göre sıralı gelir; ilkini alıyoruz.
    en_iyi = aday_liste[0]
    bulunan_yol_adi = en_iyi.get("address", {}).get("road") or en_iyi.get("name") or ""

    tam_eslesme, uyari = _eslesme_kontrolu(arama_terimi, bulunan_yol_adi) if arama_terimi else (True, None)

    if mahalle_belirsiz:
        # Mahalle uyuşmazlığı YOK ama doğrulanamadı da (Nominatim'in bu
        # sonucunda hiç mahalle/semt bilgisi yok). Kesin FARKLI mahalle
        # olan adaylar zaten yukarıda tamamen elendi; bu sadece
        # "emin değiliz" uyarısıdır.
        ek_uyari = f"'{mahalle}' mahallesi doğrulanamadı (Nominatim sonucunda mahalle/semt bilgisi yok)"
        tam_eslesme = False
        uyari = f"{uyari}; {ek_uyari}" if uyari else ek_uyari

    if debug and not tam_eslesme:
        print(f"  [!] EŞLEŞME UYARISI: {uyari}")

    return AdresSonucu(
        lat=float(en_iyi["lat"]),
        lon=float(en_iyi["lon"]),
        gosterim_adi=en_iyi.get("display_name", ""),
        tip=en_iyi.get("type", ""),
        onem=float(en_iyi.get("importance", 0.0)),
        geojson=en_iyi.get("geojson"),
        arama_terimi=arama_terimi,
        bulunan_yol_adi=bulunan_yol_adi,
        tam_eslesme=tam_eslesme,
        uyari=uyari,
        kaynak="nominatim",
        ham_veri=en_iyi,
    )


def adres_konumu_bul(
    sokak: Optional[str] = None,
    mahalle: Optional[str] = None,
    ilce: Optional[str] = None,
    il: str = "Denizli",
    ulke: str = "Türkiye",
    debug: bool = False,
) -> Optional[AdresSonucu]:
    """
    İlçe / Mahalle / Sokak bilgisinden en uygun eşleşmeyi bulup koordinat döndürür.

    DAVRANIŞ (önemli):
    - Eğer `sokak` verilmişse: sokak adı sondan kelime kelime kısaltılarak
      (bkz. _sokak_varyasyonlari) her varyasyon Mahalle+İlçe ile birlikte
      denenir.
        * Bulunan sonucun GERÇEK sokak adı (Nominatim'in address.road alanı)
          aranan isimle karşılaştırılır (özellikle numaralı sokaklarda
          "10157 Sokak" arayıp "10147 Sokak" gibi FARKLI bir sokağın sessizce
          dönmesini engellemek için).
        * Bulunan sonucun adres bloğu (suburb/neighbourhood/quarter vb.)
          `mahalle` parametresiyle de karşılaştırılır (bkz.
          _mahalle_uyusuyor_mu). Sorgu metninde mahalle geçse bile
          Nominatim bunu göz ardı edip BAŞKA bir mahalledeki aynı/benzer
          isimli yolu döndürebiliyor; bu kontrol o sonucu tamamen eler.
        * TAM eşleşme (hem yol adı hem mahalle uyuyor) bulunursa hemen o
          döner (`tam_eslesme=True`).
        * Hiçbir varyasyon tam eşleşmezse, en azından bir sonuç bulunmuşsa
          (yaklaşık/belirsiz olabilir - ör. mahalle doğrulanamadı) o
          **işaretli** şekilde döner: `tam_eslesme=False`, `uyari="..."`
          açıklamasıyla.
        * Hiçbir sonuç (yaklaşık dahi) bulunamazsa **None** döner.
    - Eğer `sokak` verilmemişse: Mahalle/İlçe ile arama yapılır.

    Parametreler
    ------------
    sokak, mahalle, ilce : Adres bileşenleri.
    il, ulke             : Varsayılan olarak "Denizli" / "Türkiye".
    debug                : True ise denenen her sorgu ve sonucu ekrana yazdırılır.

    Döndürür
    --------
    AdresSonucu, ya da bulunamazsa None.
    """
    if not (sokak or mahalle or ilce):
        raise ValueError("En az sokak, mahalle veya ilçe bilgisi vermelisin.")

    # --- SOKAK VERİLMİŞSE: kısaltarak dene, önce TAM eşleşme ara ---
    if sokak:
        en_iyi_yaklasik: Optional[AdresSonucu] = None

        for varyasyon in _sokak_varyasyonlari(sokak):
            sorgu = _sorgu_metni_olustur(varyasyon, mahalle, ilce, il, ulke)
            sonuc = _dene(sorgu, arama_terimi=varyasyon, mahalle=mahalle, debug=debug)
            if sonuc:
                if sonuc.tam_eslesme:
                    return sonuc
                if en_iyi_yaklasik is None:
                    en_iyi_yaklasik = sonuc

            # Mahalle bilgisi bazı OSM kayıtlarında eksik/farklı olabilir;
            # aynı sokak varyasyonunu mahallesiz (sadece ilçe ile) de dene.
            # ÖNEMLİ: mahalle metni sorguya eklenmese bile, dönen sonucun
            # adres bloğu yine `mahalle` parametresiyle doğrulanır -
            # aksi halde sonuç sessizce BAŞKA bir mahalledeki aynı isimli
            # sokağa kayabilir (bkz. _mahalle_uyusuyor_mu).
            if mahalle and ilce:
                sorgu2 = _sorgu_metni_olustur(varyasyon, None, ilce, il, ulke)
                sonuc2 = _dene(sorgu2, arama_terimi=varyasyon, mahalle=mahalle, debug=debug)
                if sonuc2:
                    if sonuc2.tam_eslesme:
                        return sonuc2
                    if en_iyi_yaklasik is None:
                        en_iyi_yaklasik = sonuc2

        if en_iyi_yaklasik is not None:
            if debug:
                print(f"\n'{sokak}' için TAM eşleşme bulunamadı, en yakın (İŞARETLİ) sonuç dönülüyor: "
                      f"{en_iyi_yaklasik.uyari}")
            return en_iyi_yaklasik  # tam_eslesme=False olarak işaretli, ama None DEĞİL

        if debug:
            print(f"\n'{sokak}' için hiçbir sonuç (yaklaşık dahi) bulunamadı -> None dönülüyor.")
        return None  # gerçekten hiçbir şey bulunamadı

    # --- SOKAK VERİLMEMİŞSE: mahalle/ilçe seviyesinde ara ---
    for sorgu in filter(None, [
        _sorgu_metni_olustur(None, mahalle, ilce, il, ulke) if (mahalle or ilce) else None,
        _sorgu_metni_olustur(None, None, ilce, il, ulke) if (mahalle and ilce) else None,
    ]):
        sonuc = _dene(sorgu, debug=debug)
        if sonuc:
            return sonuc

    return None


# --------------------------------------------------------------------------- #
# OSRM (sadece road_segment için Nominatim'den çizgi gelmezse yedek olarak)
# --------------------------------------------------------------------------- #

def get_route_geometry(
    start_lat: float,
    start_lon: float,
    end_lat: float,
    end_lon: float,
) -> Optional[Dict[str, Any]]:
    """
    İki koordinat arasındaki gerçek sürüş rotası geometrisini OSRM'den alır.

    OSRM GeoJSON (LineString) döndürür, hata/rota bulunamama durumunda None.
    """

    coordinates = f"{start_lon},{start_lat};{end_lon},{end_lat}"
    url = f"{OSRM_URL}/{coordinates}"

    params = {
        "overview": "full",
        "geometries": "geojson",
    }

    try:
        response = requests.get(url, params=params, timeout=OSRM_TIMEOUT)
        response.raise_for_status()
        data = response.json()
    except requests.exceptions.RequestException as e:
        print("OSRM ağ hatası:")
        print(type(e).__name__)
        print(e)
        return None

    if data.get("code") != "Ok":
        return None

    routes = data.get("routes", [])

    if not routes:
        return None

    return routes[0]["geometry"]


def _osrm_sonucu_olustur(
    start_sonuc: AdresSonucu,
    end_sonuc: AdresSonucu,
    geometry: Dict[str, Any],
) -> AdresSonucu:
    """Başlangıç/bitiş nokta sonuçlarını ve OSRM geometrisini tek bir AdresSonucu'na paketler."""

    tam_eslesme = start_sonuc.tam_eslesme and end_sonuc.tam_eslesme

    uyari = None
    if not tam_eslesme:
        parcalar = [u for u in (start_sonuc.uyari, end_sonuc.uyari) if u]
        uyari = "Rota uçlarından en az biri tam eşleşmedi: " + " | ".join(parcalar) if parcalar else \
            "Rota uçlarından en az biri tam eşleşmedi."

    return AdresSonucu(
        lat=start_sonuc.lat,
        lon=start_sonuc.lon,
        gosterim_adi=f"{start_sonuc.gosterim_adi} → {end_sonuc.gosterim_adi}",
        tip="route",
        onem=0.0,
        geojson=geometry,
        arama_terimi=f"{start_sonuc.arama_terimi} - {end_sonuc.arama_terimi}",
        bulunan_yol_adi=start_sonuc.bulunan_yol_adi,
        tam_eslesme=tam_eslesme,
        uyari=uyari,
        kaynak="osrm",
        ham_veri={"start": start_sonuc.ham_veri, "end": end_sonuc.ham_veri},
    )


def _konum_bul_osrm_rota(
    start_query: str,
    end_query: str,
    mahalle: Optional[str],
    ilce: Optional[str],
    il: str,
    ulke: str,
    debug: bool,
) -> Optional[AdresSonucu]:
    """start_query ve end_query'yi ayrı ayrı geocode edip aralarında OSRM rotası ister."""

    start_sonuc = adres_konumu_bul(sokak=start_query, mahalle=mahalle, ilce=ilce, il=il, ulke=ulke, debug=debug)
    end_sonuc = adres_konumu_bul(sokak=end_query, mahalle=mahalle, ilce=ilce, il=il, ulke=ulke, debug=debug)

    if not start_sonuc and not end_sonuc:
        return None

    if not start_sonuc or not end_sonuc:
        # Sadece bir uç bulunabildiyse, en azından onu nokta olarak dön.
        return start_sonuc or end_sonuc

    if start_sonuc.lat == end_sonuc.lat and start_sonuc.lon == end_sonuc.lon:
        # İki uç da aynı noktaya düştü (örn. ikisi de mahalle merkezine
        # denk geldi); anlamsız bir rota istemek yerine noktayı dön.
        return start_sonuc

    geometry = get_route_geometry(
        start_sonuc.lat, start_sonuc.lon,
        end_sonuc.lat, end_sonuc.lon,
    )

    if not geometry:
        # OSRM rota bulamadı, en azından başlangıç noktasını dön.
        return start_sonuc

    return _osrm_sonucu_olustur(start_sonuc, end_sonuc, geometry)


def _konum_bul_yol(
    road_query: Optional[str],
    start_query: Optional[str],
    end_query: Optional[str],
    mahalle: Optional[str],
    ilce: Optional[str],
    il: str,
    ulke: str,
    debug: bool,
    segment: bool,
) -> Optional[AdresSonucu]:
    """location_type 'road' veya 'road_segment' için konum/geometri arar."""

    sokak_adi = road_query or start_query or end_query
    nokta_sonucu: Optional[AdresSonucu] = None

    # road_segment: önce "kesişimden kesişime" gerçek yol dilimini dene
    # (yolun kendi çizgisini iki kesişen sokak arasında keser).
    if segment and road_query and start_query and end_query:
        dilim = _konum_bul_dilim(road_query, start_query, end_query, mahalle, ilce, il, ulke, debug)
        if dilim:
            return dilim

    if sokak_adi:
        nokta_sonucu = adres_konumu_bul(sokak=sokak_adi, mahalle=mahalle, ilce=ilce, il=il, ulke=ulke, debug=debug)

        if nokta_sonucu and nokta_sonucu.cizgi_mi:
            # Nominatim'den yolun kendi çizgisi geldi, yeterli -> OSRM'e gerek yok.
            return nokta_sonucu

    if segment and start_query and end_query:
        osrm_sonucu = _konum_bul_osrm_rota(start_query, end_query, mahalle, ilce, il, ulke, debug)
        if osrm_sonucu and osrm_sonucu.cizgi_mi:
            return osrm_sonucu
        if osrm_sonucu and nokta_sonucu is None:
            nokta_sonucu = osrm_sonucu

    # Çizgi/rota bulunamadı; elimizdeki en iyi nokta sonucunu dön (varsa).
    return nokta_sonucu


def _konum_bul_kesisim(
    road_query: Optional[str],
    start_query: Optional[str],
    end_query: Optional[str],
    mahalle: Optional[str],
    ilce: Optional[str],
    il: str,
    ulke: str,
    debug: bool,
) -> Optional[AdresSonucu]:
    """location_type 'intersection' için iki yol adını birlikte/ayrı ayrı dener."""

    isimler = [q for q in (road_query, start_query, end_query) if q]

    if len(isimler) >= 2:
        birlesik = " ile ".join(isimler[:2])
        sonuc = adres_konumu_bul(sokak=birlesik, mahalle=mahalle, ilce=ilce, il=il, ulke=ulke, debug=debug)
        if sonuc:
            return sonuc

    for isim in isimler:
        sonuc = adres_konumu_bul(sokak=isim, mahalle=mahalle, ilce=ilce, il=il, ulke=ulke, debug=debug)
        if sonuc:
            return sonuc

    return None


# --------------------------------------------------------------------------- #
# GEOMETRİ YARDIMCILARI (kesişimden kesişime yol dilimi)
# --------------------------------------------------------------------------- #

KESISIM_ESIK_M = float(os.getenv("KESISIM_ESIK_M") or "60")        # çizgi-çizgi en yakın mesafe
KESISIM_NOKTA_ESIK_M = float(os.getenv("KESISIM_NOKTA_ESIK_M") or "120")  # kesişen sokak yalnızca nokta ise
PARCA_BIRLESTIRME_M = 10.0  # aynı yolun ayrı OSM parçalarının uçları bu mesafedeyse bağlı say


def _mesafe_m(a: tuple, b: tuple) -> float:
    """(lon, lat) iki nokta arası yaklaşık mesafe (metre); kısa mesafeler için düzlem yaklaşımı."""
    ort_lat = math.radians((a[1] + b[1]) / 2)
    dx = (a[0] - b[0]) * math.cos(ort_lat) * 111320.0
    dy = (a[1] - b[1]) * 110540.0
    return math.hypot(dx, dy)


def _cizgi_parcalari(geojson: Optional[Dict[str, Any]]) -> List[List[tuple]]:
    """LineString / MultiLineString -> [[(lon, lat), ...], ...]. Diğer tipler boş döner."""
    if not geojson:
        return []
    tip = geojson.get("type")
    koordinatlar = geojson.get("coordinates")
    if not koordinatlar:
        return []
    if tip == "LineString":
        return [[(float(x[0]), float(x[1])) for x in koordinatlar]]
    if tip == "MultiLineString":
        return [[(float(x[0]), float(x[1])) for x in parca] for parca in koordinatlar]
    return []


def _nokta_dogru_mesafesi_m(p: tuple, a: tuple, b: tuple) -> float:
    """p noktasının a-b doğru parçasına uzaklığı (metre, yerel düzlem yaklaşımı)."""
    cos_lat = math.cos(math.radians(p[1]))

    def duzlem(n: tuple) -> tuple:
        return ((n[0] - p[0]) * cos_lat * 111320.0, (n[1] - p[1]) * 110540.0)

    ax, ay = duzlem(a)
    bx, by = duzlem(b)
    dx, dy = bx - ax, by - ay
    uzunluk2 = dx * dx + dy * dy
    t = 0.0 if uzunluk2 == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / uzunluk2))
    return math.hypot(ax + t * dx, ay + t * dy)


def _yogunlastir(parcalar: List[List[tuple]], adim_m: float = 10.0) -> List[List[tuple]]:
    """Çizgi köşeleri arasına en fazla `adim_m` aralıkla ara noktalar ekler (kesişimi daha hassas bulmak için)."""
    sonuc = []
    for parca in parcalar:
        yeni: List[tuple] = []
        for i, nokta in enumerate(parca):
            yeni.append(nokta)
            if i + 1 < len(parca):
                sonraki = parca[i + 1]
                adet = int(_mesafe_m(nokta, sonraki) // adim_m)
                for k in range(1, adet + 1):
                    oran = k / (adet + 1)
                    yeni.append((
                        nokta[0] + (sonraki[0] - nokta[0]) * oran,
                        nokta[1] + (sonraki[1] - nokta[1]) * oran,
                    ))
        sonuc.append(yeni)
    return sonuc


def _en_yakin_nokta(a_parcalari: List[List[tuple]], b_parcalari: List[List[tuple]]):
    """
    `a` çizgisinin köşeleri içinde `b` çizgisine (doğru parçalarına) en yakın olanı:
    (mesafe_m, a_koşesi) ya da None. `b` tek noktaysa nokta olarak ele alınır.
    """
    en_iyi = None
    for a_parca in a_parcalari:
        for pa in a_parca:
            for b_parca in b_parcalari:
                if len(b_parca) == 1:
                    d = _mesafe_m(pa, b_parca[0])
                else:
                    d = min(
                        _nokta_dogru_mesafesi_m(pa, b_parca[i], b_parca[i + 1])
                        for i in range(len(b_parca) - 1)
                    )
                if en_iyi is None or d < en_iyi[0]:
                    en_iyi = (d, pa)
    return en_iyi


def _cizgi_dilimle(parcalar: List[List[tuple]], bas: tuple, bit: tuple) -> Optional[List[tuple]]:
    """
    Yolun çizgisini (birden fazla OSM parçası olabilir) `bas` ve `bit` noktaları
    arasında keser: köşe noktalarından oluşan bir grafikte en kısa yolu bulur.
    Bağlantı yoksa None döner.
    """
    dugumler: List[tuple] = []
    parca_uclari: List[tuple] = []  # (dugum_id) her parçanın ilk/son düğümü
    kenarlar: Dict[int, List[tuple]] = {}

    def kenar_ekle(i: int, j: int, w: float) -> None:
        kenarlar.setdefault(i, []).append((j, w))
        kenarlar.setdefault(j, []).append((i, w))

    for parca in parcalar:
        onceki = None
        for nokta in parca:
            dugumler.append(nokta)
            idx = len(dugumler) - 1
            kenarlar.setdefault(idx, [])
            if onceki is not None:
                kenar_ekle(onceki, idx, _mesafe_m(dugumler[onceki], nokta))
            onceki = idx
        if parca:
            parca_uclari.append(len(dugumler) - len(parca))
            parca_uclari.append(len(dugumler) - 1)

    # Aynı yolun ayrı OSM parçalarını uçlarından birleştir.
    for i in range(len(parca_uclari)):
        for j in range(i + 1, len(parca_uclari)):
            a, b = parca_uclari[i], parca_uclari[j]
            d = _mesafe_m(dugumler[a], dugumler[b])
            if a != b and d <= PARCA_BIRLESTIRME_M:
                kenar_ekle(a, b, d)

    if not dugumler:
        return None

    def en_yakin(nokta: tuple) -> int:
        return min(range(len(dugumler)), key=lambda k: _mesafe_m(dugumler[k], nokta))

    kaynak, hedef = en_yakin(bas), en_yakin(bit)
    if kaynak == hedef:
        return None

    mesafe = {kaynak: 0.0}
    onceki_dugum: Dict[int, int] = {}
    kuyruk = [(0.0, kaynak)]
    while kuyruk:
        d, u = heapq.heappop(kuyruk)
        if d > mesafe.get(u, math.inf):
            continue
        if u == hedef:
            break
        for v, w in kenarlar.get(u, []):
            nd = d + w
            if nd < mesafe.get(v, math.inf):
                mesafe[v] = nd
                onceki_dugum[v] = u
                heapq.heappush(kuyruk, (nd, v))

    if hedef not in mesafe:
        return None

    yol = [hedef]
    while yol[-1] != kaynak:
        yol.append(onceki_dugum[yol[-1]])
    yol.reverse()
    return [dugumler[k] for k in yol]


def _kesisim_noktasi(yol_parcalari: List[List[tuple]], diger: AdresSonucu) -> Optional[tuple]:
    """`yol_parcalari` (yoğunlaştırılmış) üzerinde, `diger` sokakla kesiştiği (en yakın olduğu) köşeyi döndürür."""
    if not yol_parcalari:
        return None

    diger_parcalari = _cizgi_parcalari(diger.geojson)
    if diger_parcalari:
        esik = KESISIM_ESIK_M
    else:
        diger_parcalari = [[(diger.lon, diger.lat)]]
        esik = KESISIM_NOKTA_ESIK_M

    cift = _en_yakin_nokta(yol_parcalari, diger_parcalari)
    if cift is None or cift[0] > esik:
        return None
    return cift[1]


def _konum_bul_dilim(
    road_query: str,
    start_query: str,
    end_query: str,
    mahalle: Optional[str],
    ilce: Optional[str],
    il: str,
    ulke: str,
    debug: bool,
) -> Optional[AdresSonucu]:
    """
    "A Caddesi'nin B Sokak ile C Sokak arası": A'nın çizgisini B ve C ile
    kesiştiği noktalar arasında keser. Yol çizgisi ya da kesişimler güvenilir
    bulunamazsa None döner (çağıran eski yönteme düşer).
    """
    yol = adres_konumu_bul(sokak=road_query, mahalle=mahalle, ilce=ilce, il=il, ulke=ulke, debug=debug)
    if not yol or not yol.cizgi_mi:
        return None

    baslangic = adres_konumu_bul(sokak=start_query, mahalle=mahalle, ilce=ilce, il=il, ulke=ulke, debug=debug)
    bitis = adres_konumu_bul(sokak=end_query, mahalle=mahalle, ilce=ilce, il=il, ulke=ulke, debug=debug)
    if not baslangic or not bitis:
        return None

    yol_parcalari = _yogunlastir(_cizgi_parcalari(yol.geojson))
    p_bas = _kesisim_noktasi(yol_parcalari, baslangic)
    p_bit = _kesisim_noktasi(yol_parcalari, bitis)
    if not p_bas or not p_bit:
        return None

    dilim = _cizgi_dilimle(yol_parcalari, p_bas, p_bit)
    if not dilim or len(dilim) < 2:
        return None

    tam_eslesme = yol.tam_eslesme and baslangic.tam_eslesme and bitis.tam_eslesme
    uyarilar = [u for u in (yol.uyari, baslangic.uyari, bitis.uyari) if u]
    orta = dilim[len(dilim) // 2]

    return AdresSonucu(
        lat=orta[1],
        lon=orta[0],
        gosterim_adi=f"{yol.gosterim_adi} ({start_query} – {end_query} arası)",
        tip="segment",
        geojson={"type": "LineString", "coordinates": [[x, y] for x, y in dilim]},
        arama_terimi=f"{road_query}: {start_query} - {end_query}",
        bulunan_yol_adi=yol.bulunan_yol_adi,
        tam_eslesme=tam_eslesme,
        uyari=" | ".join(uyarilar) if uyarilar else None,
        kaynak="kesisim",
        ham_veri={"yol": yol.ham_veri, "baslangic": baslangic.ham_veri, "bitis": bitis.ham_veri},
    )


# --------------------------------------------------------------------------- #
# main.py'nin ÇAĞIRDIĞI TEK GİRİŞ NOKTASI
# --------------------------------------------------------------------------- #

def konum_bul_ai(
    ai_result: Dict[str, Any],
    web_mahalle: Optional[str] = None,
    web_ilce: Optional[str] = None,
    il: str = "Denizli",
    ulke: str = "Türkiye",
    debug: bool = False,
) -> Optional[AdresSonucu]:
    """
    main.py'den gelen AI analiz sonucunu (ai_result dict) alıp konum/geometri
    bulur. Kaynak sitedeki (web) mahalle/ilçe bilgisi, AI'nin tahmininden
    her zaman daha güvenilir kabul edilir ve önceliklidir.

    ai_result beklenen anahtarlar:
        location_type, neighborhood, district, query, start_query, end_query

    Döndürür
    --------
    AdresSonucu (nokta / çizgi / alan geometrisiyle), ya da bulunamazsa None.
    """

    mahalle = web_mahalle or ai_result.get("neighborhood")
    ilce = web_ilce or ai_result.get("district")

    location_type = ai_result.get("location_type")
    road_query = ai_result.get("query")
    start_query = ai_result.get("start_query")
    end_query = ai_result.get("end_query")

    if location_type in ("road", "road_segment"):
        return _konum_bul_yol(
            road_query, start_query, end_query,
            mahalle, ilce, il, ulke, debug,
            segment=(location_type == "road_segment"),
        )

    if location_type == "intersection":
        return _konum_bul_kesisim(
            road_query, start_query, end_query,
            mahalle, ilce, il, ulke, debug,
        )

    if location_type in ("address", "landmark", "unknown"):
        sorgu = road_query or start_query or end_query
        if not sorgu:
            return None
        return adres_konumu_bul(sokak=sorgu, mahalle=mahalle, ilce=ilce, il=il, ulke=ulke, debug=debug)

    return None


# --------------------------------------------------------------------------- #
# ÇOK PARÇALI DUYURULAR (main.py bunu kullanır)
# --------------------------------------------------------------------------- #

ALAN_MIN_YARICAP_M = 120.0
ALAN_MAX_YARICAP_M = 400.0
ALAN_BIRLESTIRME_MAX_YARICAP_M = 500.0

KESINLIK_KESIN = "kesin"            # istenen parça (kesişimden kesişime / tam yol / adres) bulundu
KESINLIK_YAKLASIK = "yaklasik"      # yalnızca yolun tamamı ya da şüpheli eşleşme bulundu
KESINLIK_BULUNAMADI = "bulunamadi"  # hiçbir sonuç yok (web arayüzü mahalle düzeyinde gösterir)


@dataclass
class ParcaSonucu:
    """Duyurudaki tek bir yol/sokak parçasının konum sonucu."""

    etiket: str
    location_type: str
    kesinlik: str
    sonuc: Optional[AdresSonucu] = None
    baglanan_sokaklar: bool = False

    @property
    def geometri(self) -> Optional[Dict[str, Any]]:
        if not self.sonuc:
            return None
        if self.sonuc.geojson:
            return self.sonuc.geojson
        return {"type": "Point", "coordinates": [self.sonuc.lon, self.sonuc.lat]}


@dataclass
class KonumSonucu:
    """Bir duyurunun tüm parçalarının sonucu + etkilenen alanlar."""

    parcalar: List[ParcaSonucu]
    alanlar: List[Dict[str, float]] = field(default_factory=list)

    @property
    def bulunanlar(self) -> List[ParcaSonucu]:
        return [p for p in self.parcalar if p.sonuc]

    def geometri(self) -> Optional[Dict[str, Any]]:
        """Bulunan parçaların geometrisi: 1 tane ise kendisi, birden çoksa GeometryCollection."""
        geometriler = [p.geometri for p in self.bulunanlar if p.geometri]
        if not geometriler:
            return None
        if len(geometriler) == 1:
            return geometriler[0]
        return {"type": "GeometryCollection", "geometries": geometriler}

    def geocode_sonucu(self) -> Optional[Dict[str, Any]]:
        """Supabase `geocode_result` alanına yazılacak sözlük (bulunan yoksa None)."""
        bulunanlar = self.bulunanlar
        if not bulunanlar:
            return None

        ilk = bulunanlar[0].sonuc
        uyarilar = [p.sonuc.uyari for p in bulunanlar if p.sonuc.uyari]

        return {
            # Eski alanlar (ilk bulunan parçadan): eski web sürümleri ve tablo bunları okur.
            "latitude": ilk.lat,
            "longitude": ilk.lon,
            "display_name": ilk.gosterim_adi,
            "tam_eslesme": all(p.sonuc.tam_eslesme for p in bulunanlar),
            "uyari": " | ".join(uyarilar) if uyarilar else None,
            "kaynak": ilk.kaynak,
            # Yeni alanlar
            "parcalar": [
                {
                    "etiket": p.etiket,
                    "location_type": p.location_type,
                    "kesinlik": p.kesinlik,
                    "uyari": p.sonuc.uyari if p.sonuc else None,
                    "baglanan_sokaklar": p.baglanan_sokaklar,
                    "geometry": p.geometri,
                }
                for p in self.parcalar
            ],
            "bulunan": len(bulunanlar),
            "toplam": len(self.parcalar),
            "etkilenen_alanlar": self.alanlar,
        }


def _parca_etiketi(seg: Dict[str, Any]) -> str:
    yol = seg.get("query") or seg.get("road_name")
    bas, bit = seg.get("start_query"), seg.get("end_query")
    if yol and bas and bit:
        return f"{yol}: {bas} – {bit} arası"
    return " – ".join(x for x in (yol, bas, bit) if x) or "(adsız parça)"


def _parcalari_cikar(ai_result: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Yeni kayıtlarda `segments`; eski kayıtlarda tek parça (eski alanlardan)."""
    segments = ai_result.get("segments")
    if isinstance(segments, list):
        return [s for s in segments if isinstance(s, dict)]
    return [
        {
            "location_type": ai_result.get("location_type"),
            "road_name": ai_result.get("road_name"),
            "query": ai_result.get("query"),
            "start_query": ai_result.get("start_query"),
            "end_query": ai_result.get("end_query"),
            "includes_connecting_streets": False,
        }
    ]


def _kesinlik_belirle(location_type: Optional[str], sonuc: Optional[AdresSonucu]) -> str:
    if sonuc is None:
        return KESINLIK_BULUNAMADI
    if location_type == "road_segment":
        # Kesin: gerçekten kesişimden kesişime dilim ya da iki uç eşleşen rota.
        kesin = sonuc.kaynak in ("kesisim", "osrm") and sonuc.tam_eslesme and sonuc.cizgi_mi
    elif location_type == "road":
        kesin = sonuc.tam_eslesme and sonuc.cizgi_mi
    elif location_type in ("intersection", "address", "landmark"):
        kesin = sonuc.tam_eslesme
    else:
        kesin = False
    return KESINLIK_KESIN if kesin else KESINLIK_YAKLASIK


def _etkilenen_alan(sonuc: AdresSonucu) -> Dict[str, float]:
    """Bir parçanın çevresinde "bağlanan sokaklar" için yaklaşık daire (merkez + yarıçap, metre)."""
    parcalar = _cizgi_parcalari(sonuc.geojson)
    noktalar = [n for parca in parcalar for n in parca] or [(sonuc.lon, sonuc.lat)]
    lon = sum(n[0] for n in noktalar) / len(noktalar)
    lat = sum(n[1] for n in noktalar) / len(noktalar)
    uzunluk = sum(
        _mesafe_m(parca[i], parca[i + 1]) for parca in parcalar for i in range(len(parca) - 1)
    )
    yaricap = min(max(0.6 * uzunluk + 80.0, ALAN_MIN_YARICAP_M), ALAN_MAX_YARICAP_M)
    return {"lat": round(lat, 6), "lon": round(lon, 6), "radius_m": round(yaricap)}


def _alanlari_birlestir(alanlar: List[Dict[str, float]]) -> List[Dict[str, float]]:
    """
    Üst üste binen daireleri, ikisini de kapsayan tek daireye birleştirir
    (aynı bölgeyi iki kez çizmemek için). Birleşim çok büyürse ayrı bırakılır.
    """
    sonuc: List[Dict[str, float]] = []
    for yeni in alanlar:
        birlesti = False
        for i, eski in enumerate(sonuc):
            d = _mesafe_m((eski["lon"], eski["lat"]), (yeni["lon"], yeni["lat"]))
            if d > max(eski["radius_m"], yeni["radius_m"]):
                continue
            if d + yeni["radius_m"] <= eski["radius_m"]:
                birlesti = True  # yeni zaten eskinin içinde
                break
            if d + eski["radius_m"] <= yeni["radius_m"]:
                sonuc[i] = dict(yeni)
                birlesti = True
                break
            r = (d + eski["radius_m"] + yeni["radius_m"]) / 2
            if r > ALAN_BIRLESTIRME_MAX_YARICAP_M:
                continue
            oran = (r - eski["radius_m"]) / d if d else 0.0
            sonuc[i] = {
                "lat": round(eski["lat"] + (yeni["lat"] - eski["lat"]) * oran, 6),
                "lon": round(eski["lon"] + (yeni["lon"] - eski["lon"]) * oran, 6),
                "radius_m": round(r),
            }
            birlesti = True
            break
        if not birlesti:
            sonuc.append(dict(yeni))
    return sonuc


def konum_bul_parcalar(
    ai_result: Dict[str, Any],
    web_mahalle: Optional[str] = None,
    web_ilce: Optional[str] = None,
    il: str = "Denizli",
    ulke: str = "Türkiye",
    debug: bool = False,
) -> KonumSonucu:
    """
    AI sonucundaki HER parçayı ayrı ayrı konumlandırır. Bir parçanın bulunamaması
    ya da ağ hatası diğerlerini etkilemez. Bulunamayan parça kesinlik="bulunamadi"
    olarak kalır (web arayüzü onu mahalle düzeyinde listeler).
    """
    parcalar: List[ParcaSonucu] = []
    gorulen = set()

    for seg in _parcalari_cikar(ai_result):
        seg = dict(seg)
        for anahtar in ("query", "start_query", "end_query"):
            seg[anahtar] = kanonik_yol_adi(seg.get(anahtar))

        ozet = (seg.get("location_type"), seg.get("query"), seg.get("start_query"), seg.get("end_query"))
        if ozet in gorulen:
            continue  # AI aynı parçayı iki kez yazdıysa tekrar arama
        gorulen.add(ozet)

        etiket = _parca_etiketi(seg)
        tip = seg.get("location_type") or "unknown"
        baglanan = bool(seg.get("includes_connecting_streets"))

        sonuc: Optional[AdresSonucu] = None
        if seg.get("query") or seg.get("start_query") or seg.get("end_query"):
            parca_ai = {
                "location_type": tip,
                "query": seg.get("query"),
                "start_query": seg.get("start_query"),
                "end_query": seg.get("end_query"),
                "neighborhood": ai_result.get("neighborhood"),
                "district": ai_result.get("district"),
            }
            try:
                sonuc = konum_bul_ai(
                    parca_ai, web_mahalle=web_mahalle, web_ilce=web_ilce,
                    il=il, ulke=ulke, debug=debug,
                )
            except requests.RequestException as e:
                if debug:
                    print(f"[UYARI] '{etiket}' parçasında ağ hatası: {e}")

        parcalar.append(
            ParcaSonucu(
                etiket=etiket,
                location_type=tip,
                kesinlik=_kesinlik_belirle(tip, sonuc),
                sonuc=sonuc,
                baglanan_sokaklar=baglanan,
            )
        )

    alanlar = _alanlari_birlestir(
        [_etkilenen_alan(p.sonuc) for p in parcalar if p.sonuc and p.baglanan_sokaklar]
    )
    return KonumSonucu(parcalar=parcalar, alanlar=alanlar)


# --------------------------------------------------------------------------- #
# Haritada çizim (Leaflet + OpenStreetMap) - bağımsız test/debug amaçlı
# --------------------------------------------------------------------------- #

_HARITA_HTML_SABLONU = """<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="utf-8" />
<title>Konum Haritası</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
<style>
  html, body, #harita {{ height: 100%; margin: 0; padding: 0; font-family: sans-serif; }}
  .info-kutu {{
    position: absolute; top: 10px; right: 10px; z-index: 1000;
    background: white; padding: 10px 14px; border-radius: 8px;
    box-shadow: 0 2px 8px rgba(0,0,0,0.25); max-width: 280px; font-size: 13px;
  }}
  .info-kutu b {{ display: block; margin-bottom: 4px; color: #2c3e50; }}
</style>
</head>
<body>
<div id="harita"></div>
<div class="info-kutu" id="bilgi-kutu"></div>

<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
  const veriler = {veri_json};

  const harita = L.map('harita');
  L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
      maxZoom: 19,
      attribution: '&copy; OpenStreetMap katkıda bulunanlar'
  }}).addTo(harita);

  const renkler = ["#e74c3c", "#3498db", "#27ae60", "#f39c12", "#9b59b6", "#1abc9c"];
  const tumSinirlar = [];
  let bilgiHtml = "";

  veriler.forEach((adres, i) => {{
      const renk = renkler[i % renkler.length];
      const cizgiStili = adres.tam_eslesme
          ? {{ color: renk, weight: 6, opacity: 0.85 }}
          : {{ color: renk, weight: 5, opacity: 0.7, dashArray: "10, 8" }};  // yaklaşık -> KESİKLİ çizgi

      if (adres.geojson && adres.geojson.type && adres.geojson.type.includes("LineString")) {{
          const katman = L.geoJSON(adres.geojson, {{ style: cizgiStili }}).addTo(harita);
          tumSinirlar.push(katman.getBounds());
      }} else if (adres.geojson && adres.geojson.type && adres.geojson.type.includes("Polygon")) {{
          const katman = L.geoJSON(adres.geojson, {{
              style: {{ color: renk, weight: 2, fillColor: renk, fillOpacity: 0.2 }}
          }}).addTo(harita);
          tumSinirlar.push(katman.getBounds());
      }} else {{
          const marker = L.marker([adres.lat, adres.lon]).addTo(harita);
          tumSinirlar.push(L.latLngBounds([[adres.lat, adres.lon]]));
      }}

      const uyariSatiri = adres.tam_eslesme
          ? ""
          : `<br><span style="color:#e67e22; font-size:12px;">⚠ Tam eşleşme değil: ${{adres.uyari || ''}}</span>`;
      bilgiHtml += `<b style="color:${{renk}}">${{adres.tam_eslesme ? '●' : '⚠'}} ${{adres.etiket}}</b>${{adres.gosterim_adi}}${{uyariSatiri}}<br><br>`;
  }});

  document.getElementById("bilgi-kutu").innerHTML = bilgiHtml;

  if (tumSinirlar.length === 1) {{
      harita.fitBounds(tumSinirlar[0], {{ padding: [40, 40], maxZoom: 18 }});
  }} else if (tumSinirlar.length > 1) {{
      let birlesikSinir = tumSinirlar[0];
      tumSinirlar.slice(1).forEach(b => birlesikSinir.extend(b));
      harita.fitBounds(birlesikSinir, {{ padding: [40, 40] }});
  }}
</script>
</body>
</html>
"""


def haritada_goster(sonuclar, dosya_yolu: str = "harita.html") -> str:
    """
    Bir veya birden fazla AdresSonucu'nu Leaflet.js ile çizen bağımsız bir
    HTML dosyası üretir (sadece bağımsız test/debug amaçlı; main.py bunu
    kullanmaz, main.py kendi folium haritasını üretir).
    """
    if isinstance(sonuclar, AdresSonucu):
        sonuclar = [sonuclar]

    veri = []
    for i, s in enumerate(sonuclar, start=1):
        veri.append({
            "lat": s.lat,
            "lon": s.lon,
            "gosterim_adi": s.gosterim_adi,
            "geojson": s.geojson,
            "tam_eslesme": s.tam_eslesme,
            "uyari": s.uyari,
            "etiket": f"Sonuç {i}: ",
        })

    html = _HARITA_HTML_SABLONU.format(veri_json=json.dumps(veri, ensure_ascii=False))

    with open(dosya_yolu, "w", encoding="utf-8") as f:
        f.write(html)

    return dosya_yolu


# --------------------------------------------------------------------------- #
# Bağımsız çalıştırma / hızlı test
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    # Örnek: main.py'den gelecek türden bir ai_result ile deneme.
    ornek_ai_result = {
        "location_type": "road_segment",
        "road_name": "Fatih Sultan Mehmet Caddesi",
        "neighborhood": "Adalet",
        "district": "Merkezefendi",
        "query": "Fatih Sultan Mehmet /1 Caddesi",
        "start_query": "10157 Sokak",
        "end_query": "10054 Sokak",
        "confidence": 0.8,
    }

    sonuc = konum_bul_ai(ornek_ai_result, debug=True)

    if sonuc:
        print("\n[BULUNDU]")
        print(sonuc)
        haritada_goster(sonuc, dosya_yolu="harita.html")
        print("Harita oluşturuldu: harita.html")
    else:
        print("\n[BULUNAMADI]")