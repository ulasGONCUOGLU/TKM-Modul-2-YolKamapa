# -*- coding: utf-8 -*-
"""
adm.py
======
ADM (Aydem) Elektrik'in planlı çalışmalarını çeker, Denizli şehir içi
sinyalize kavşaklarla (150 m) kesiştirir ve Supabase'e yazar.

Amaç: sinyalizasyonu etkileyebilecek elektrik çalışmalarından erken haberdar olmak.

Bu modülde AI ve konum arama (geocoding) YOKTUR: ADM'nin kendi verdiği
poligon (Alan_Geometri.halka, [boylam, enlem]) doğrudan kullanılır.

Kaynak (ADM sitesinin kendi harita sayfasının kullandığı adres):
    GET https://www.admelektrik.com.tr/api/test-outages?city=denizli&district=Merkezefendi&action=outages
Adres adında "test" geçiyor: ADM önceden haber vermeden değiştirebilir.
Bu yüzden her yanıt sıkı doğrulanır; beklenmedik bir şey olursa

  - o ilçenin mevcut kayıtlarına DOKUNULMAZ (yanlış veriyle ezilmez),
  - açıklayıcı bir hata metni `adm_sync_durumu` tablosuna yazılır,
  - web sitesi bu metni ekranda gösterir.

Ayarlar (.env, hepsi isteğe bağlı):
    ADM_AKTIF=0                    ADM adımını kapatır
    ADM_ILCELER=Merkezefendi,Pamukkale
    ADM_SEHIR=denizli
    ADM_KAVSAK_MESAFE_M=150
    ADM_API_URL=...                (yukarıdaki adres)
    ADM_USER_AGENT=...             (boşsa botu tanıtan bir User-Agent)
"""

from __future__ import annotations

import math
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests
from requests.adapters import HTTPAdapter
from shapely.geometry import LineString, MultiPoint, Point, Polygon
from urllib3.util.retry import Retry

# ============================================================
# SABİTLER
# ============================================================

VARSAYILAN_API_URL = "https://www.admelektrik.com.tr/api/test-outages"
VARSAYILAN_UA = "YolKapamaBot/1.0 (belediye yol kapama ve elektrik calismasi takibi)"

# Türkiye kalıcı olarak UTC+3 kullanıyor: zoneinfo/tzdata bağımlılığı gerekmez.
TR_SAAT = timezone(timedelta(hours=3))

# Denizli şehir içi (sinyalize kavşakların bulunduğu alan + pay). Bunun dışına
# düşen çalışmalar tabloda görünür ama haritada çizilmez (ADM'nin "cbs" tipi
# kayıtlarında merkez noktası bazen şehir dışında çıkıyor).
SEHIR_ICI_BBOX = {"lon_min": 28.95, "lon_max": 29.35, "lat_min": 37.68, "lat_max": 37.90}

# ADM'nin kullandığı alan adları (biri kaybolursa "ADM biçimi değişti" hatası verilir)
ZORUNLU_ALANLAR = (
    "Kesinti_ID",
    "Durum",
    "Planlanan_Baslangic_Zamani",
    "Planlanan_Sona_Erme_Zamani",
    "Alan_Geometri",
)

# Eşleme için yerel metre dönüşümünün merkezi (Denizli)
_LAT0 = 37.78
_LON0 = 29.09
_M_PER_DEG_LAT = 110_574.0
_M_PER_DEG_LON = 111_320.0 * math.cos(math.radians(_LAT0))


class AdmHata(Exception):
    """Kullanıcıya olduğu gibi gösterilen, Türkçe açıklayıcı hata."""


# ============================================================
# AYARLAR
# ============================================================


def ayarlar() -> Dict[str, Any]:
    """.env'i her çağrıda okur (testlerde değiştirilebilsin diye)."""

    ilceler = [
        i.strip()
        for i in (os.getenv("ADM_ILCELER") or "Merkezefendi,Pamukkale").split(",")
        if i.strip()
    ]
    try:
        mesafe = float(os.getenv("ADM_KAVSAK_MESAFE_M") or 150)
    except ValueError:
        mesafe = 150.0

    return {
        "aktif": (os.getenv("ADM_AKTIF", "1").strip().lower() not in ("0", "false", "hayir", "hayır")),
        "api_url": (os.getenv("ADM_API_URL") or VARSAYILAN_API_URL).strip(),
        "sehir": (os.getenv("ADM_SEHIR") or "denizli").strip().lower(),
        "ilceler": ilceler,
        "mesafe_m": mesafe,
        "user_agent": (os.getenv("ADM_USER_AGENT") or "").strip() or VARSAYILAN_UA,
    }


def build_session(user_agent: str) -> requests.Session:
    retry = Retry(
        total=2,
        backoff_factor=1.5,
        status_forcelist=(500, 502, 503, 504),
        allowed_methods=("GET",),
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": user_agent,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "tr-TR,tr;q=0.9",
            "Referer": "https://www.admelektrik.com.tr/",
        }
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


# ============================================================
# METİN / ZAMAN YARDIMCILARI
# ============================================================


_TR_DONUSUM = str.maketrans("ıçğöşüâîû", "icgosuaiu")


def _tr_kucuk(metin: str) -> str:
    """Büyük/küçük harf ve Türkçe karakter duyarsız karşılaştırma için sadeleştirir."""
    t = str(metin or "").replace("İ", "i").replace("I", "ı").lower()
    return t.translate(_TR_DONUSUM).strip()


_ZAMAN_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?"
)


def parse_zaman(deger: Any) -> Optional[datetime]:
    """
    "2026-10-30 09:00:00.324" -> 2026-10-30 09:00:00+03:00. Boş değer None döner.
    Biçim tanınmazsa ValueError (ADM biçimi değişmiş olabilir).
    """

    if deger is None:
        return None
    metin = str(deger).strip()
    if not metin:
        return None

    m = _ZAMAN_RE.match(metin)
    if not m:
        raise ValueError(f"tanınmayan zaman biçimi: {metin!r}")

    yil, ay, gun, saat, dakika, saniye = (int(x) if x else 0 for x in m.groups())
    return datetime(yil, ay, gun, saat, dakika, saniye, tzinfo=TR_SAAT)


def _dakikaya_yuvarla(dt: Optional[datetime]) -> Optional[datetime]:
    """Planlanan saatlerdeki "09:00:00.944" gibi saniye gürültüsünü atar."""
    return dt.replace(second=0, microsecond=0) if dt else None


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def durum_kodu_bul(
    durum: str, gercek_bas: Optional[datetime], gercek_bit: Optional[datetime]
) -> str:
    d = _tr_kucuk(durum)

    if "iptal" in d:
        return "iptal"
    if "arsiv" in d or "tamamland" in d:
        return "tamamlandi"
    if "devam" in d:
        return "devam_ediyor"
    if "planland" in d:
        # Başlamış ama bitmemişse "devam ediyor"
        if gercek_bas and not gercek_bit:
            return "devam_ediyor"
        return "planlandi"
    return "bilinmiyor"


# ============================================================
# GEOMETRİ
# ============================================================


def _gecerli_nokta(p: Any) -> Optional[Tuple[float, float]]:
    """[boylam, enlem] -> (lon, lat); Türkiye dışı/bozuk değerler None."""
    try:
        lon, lat = float(p[0]), float(p[1])
    except (TypeError, ValueError, IndexError, KeyError):
        return None
    if not (math.isfinite(lon) and math.isfinite(lat)):
        return None
    if not (25.0 <= lon <= 45.0 and 35.0 <= lat <= 43.0):
        return None
    return lon, lat


def sehir_ici_mi(lon: float, lat: float) -> bool:
    b = SEHIR_ICI_BBOX
    return b["lon_min"] <= lon <= b["lon_max"] and b["lat_min"] <= lat <= b["lat_max"]


def _metreye(lon: float, lat: float) -> Tuple[float, float]:
    return (lon - _LON0) * _M_PER_DEG_LON, (lat - _LAT0) * _M_PER_DEG_LAT


def _geojson(geom) -> Dict[str, Any]:
    if geom.geom_type == "Polygon":
        return {
            "type": "Polygon",
            "coordinates": [[[round(x, 6), round(y, 6)] for x, y in geom.exterior.coords]],
        }
    if geom.geom_type == "LineString":
        return {"type": "LineString", "coordinates": [[round(x, 6), round(y, 6)] for x, y in geom.coords]}
    return {"type": "Point", "coordinates": [round(geom.x, 6), round(geom.y, 6)]}


def alan_olustur(alan: Any) -> Dict[str, Any]:
    """
    ADM'nin Alan_Geometri nesnesinden geometriyi hazırlar.

    Döndürür:
        geometry      : GeoJSON (Polygon / LineString / Point) ya da None
        turu          : polygon | cizgi | nokta | yok
        kaynak        : ADM'nin "kaynak" değeri
        merkez        : (lat, lon) ya da None
        sehir_ici     : bool
        geom          : shapely geometrisi (lon/lat) ya da None (kesişim için)
    """

    if not isinstance(alan, dict):
        alan = {}

    kaynak = alan.get("kaynak")
    halka = alan.get("halka") or []

    noktalar: List[Tuple[float, float]] = []
    if isinstance(halka, list):
        for p in halka:
            nokta = _gecerli_nokta(p)
            if nokta and (not noktalar or noktalar[-1] != nokta):
                noktalar.append(nokta)
    # Halka kapalı gelirse son nokta ilkinin tekrarıdır
    if len(noktalar) > 1 and noktalar[0] == noktalar[-1]:
        noktalar.pop()

    geom = None
    turu = "yok"

    if len(noktalar) >= 3:
        poly = Polygon(noktalar)
        if not poly.is_valid or poly.area == 0:
            # Kendini kesen/çizgi gibi çöken poligon: noktaların dış kabuğunu kullan
            poly = MultiPoint(noktalar).convex_hull
        if poly.geom_type == "Polygon" and poly.area > 0:
            geom, turu = poly, "polygon"
        elif poly.geom_type == "LineString":
            geom, turu = poly, "cizgi"
        else:
            geom, turu = Point(noktalar[0]), "nokta"
    elif len(noktalar) == 2:
        geom, turu = LineString(noktalar), "cizgi"
    elif len(noktalar) == 1:
        geom, turu = Point(noktalar[0]), "nokta"

    merkez_ll = _gecerli_nokta(alan.get("merkez"))
    if geom is None and merkez_ll:
        geom, turu = Point(merkez_ll), "nokta"

    merkez = None
    if merkez_ll:
        merkez = (merkez_ll[1], merkez_ll[0])
    elif geom is not None:
        c = geom.centroid
        merkez = (c.y, c.x)

    sehir_ici = False
    if geom is not None:
        c = geom.centroid
        sehir_ici = sehir_ici_mi(c.x, c.y)

    return {
        "geometry": _geojson(geom) if geom is not None else None,
        "turu": turu,
        "kaynak": str(kaynak) if kaynak is not None else None,
        "merkez": merkez,
        "sehir_ici": sehir_ici,
        "geom": geom,
    }


def kavsak_iliskileri(
    geom, kavsaklar: List[Dict[str, Any]], mesafe_m: float
) -> List[Dict[str, Any]]:
    """
    Alanın içinde ("icinde") ya da mesafe_m metre yakınında ("yakin") kalan
    sinyalize kavşakları döndürür (yakından uzağa). Mesafe metre cinsindendir.
    """

    if geom is None:
        return []

    # Hesabı metre düzleminde yap (derece değil)
    if geom.geom_type == "Polygon":
        g = Polygon([_metreye(x, y) for x, y in geom.exterior.coords])
    elif geom.geom_type == "LineString":
        g = LineString([_metreye(x, y) for x, y in geom.coords])
    else:
        g = Point(_metreye(geom.x, geom.y))

    sonuc = []
    for k in kavsaklar:
        try:
            lat, lon = float(k["latitude"]), float(k["longitude"])
        except (KeyError, TypeError, ValueError):
            continue
        p = Point(_metreye(lon, lat))

        if g.geom_type == "Polygon" and g.contains(p):
            sonuc.append({"kod": k.get("kod"), "ad": k.get("ad"), "iliski": "icinde", "mesafe_m": 0})
            continue

        d = g.distance(p)
        if d <= mesafe_m:
            sonuc.append({"kod": k.get("kod"), "ad": k.get("ad"), "iliski": "yakin", "mesafe_m": int(round(d))})

    sonuc.sort(key=lambda x: (x["mesafe_m"], str(x["kod"])))
    return sonuc


# ============================================================
# ADM'DEN VERİ ÇEKME + DOĞRULAMA
# ============================================================


def _govde_ozeti(govde: str, n: int = 120) -> str:
    return re.sub(r"\s+", " ", govde or "")[:n]


def ilce_cek(
    session: requests.Session,
    api_url: str,
    sehir: str,
    ilce: str,
    deneme: int = 3,
    bekleme_s: float = 2.0,
) -> List[Dict[str, Any]]:
    """
    Bir ilçenin çalışma kayıtlarını getirir ve zarfı doğrular.
    Geçici sorunlarda (HTTP 5xx, JSON olmayan yanıt, "neighborhoods" gibi
    beklenmeyen seviye) birkaç kez yeniden dener; olmazsa AdmHata verir.
    """

    son_neden = "bilinmeyen sorun"

    for sira in range(1, deneme + 1):
        if sira > 1 and bekleme_s:
            time.sleep(bekleme_s * (sira - 1))

        try:
            yanit = session.get(
                api_url,
                params={"city": sehir, "district": ilce, "action": "outages"},
                timeout=30,
            )
        except requests.RequestException as e:
            son_neden = f"ADM sunucusuna ulaşılamadı ({type(e).__name__})"
            continue

        if yanit.status_code in (401, 403, 406, 429):
            son_neden = (
                f"ADM sunucusu isteği reddetti (HTTP {yanit.status_code}). "
                "Bot koruması devreye girmiş olabilir; .env içinde ADM_USER_AGENT ayarını deneyin"
            )
            continue
        if yanit.status_code == 404:
            raise AdmHata(
                "ADM veri adresi bulunamadı (HTTP 404): adres değişmiş ya da kaldırılmış olabilir. "
                f"Adres: {api_url}"
            )
        if yanit.status_code != 200:
            son_neden = f"ADM sunucusu hata döndürdü (HTTP {yanit.status_code})"
            continue

        try:
            veri = yanit.json()
        except ValueError:
            son_neden = (
                "ADM, JSON yerine başka bir içerik döndürdü "
                f"(başlangıcı: {_govde_ozeti(yanit.text)!r}). Adres ya da koruma değişmiş olabilir"
            )
            continue

        if not isinstance(veri, dict) or "data" not in veri:
            son_neden = "ADM yanıt biçimi değişmiş: beklenen 'data' alanı yok"
            continue
        if veri.get("success") is not True:
            son_neden = "ADM yanıtı başarısız olarak işaretlendi (success değeri true değil)"
            continue

        seviye = veri.get("level")
        if seviye != "district_outages":
            # Gözlem: bazen kesinti kayıtları yerine mahalle listesi ("neighborhoods") dönüyor.
            son_neden = f"ADM beklenmeyen bir yanıt türü döndürdü (level={seviye!r}, beklenen 'district_outages')"
            continue

        if _tr_kucuk(veri.get("district", "")) != _tr_kucuk(ilce):
            son_neden = (
                f"ADM başka bir ilçenin verisini döndürdü (istenen {ilce!r}, gelen {veri.get('district')!r})"
            )
            continue

        kayitlar = veri.get("data")
        if not isinstance(kayitlar, list):
            raise AdmHata("ADM yanıt biçimi değişmiş: 'data' bir liste değil")

        return kayitlar

    raise AdmHata(f"{ilce}: {son_neden} ({deneme} deneme sonrası vazgeçildi)")


def kayit_normalize(
    ham: Dict[str, Any],
    ilce: str,
    sehir: str,
    kavsaklar: List[Dict[str, Any]],
    mesafe_m: float,
    simdi: datetime,
) -> Dict[str, Any]:
    """ADM kaydını veritabanı satırına çevirir. Biçim bozuksa AdmHata verir."""

    if not isinstance(ham, dict):
        raise AdmHata(f"{ilce}: ADM kaydı beklenen biçimde değil (nesne yerine {type(ham).__name__})")

    eksik = [a for a in ZORUNLU_ALANLAR if a not in ham]
    if eksik:
        raise AdmHata(
            f"ADM yanıt biçimi değişmiş: kayıtlarda şu alanlar yok: {', '.join(eksik)} "
            f"(kayıt: {ham.get('Kesinti_ID', '?')}, ilçe: {ilce})"
        )

    kesinti_id = str(ham["Kesinti_ID"]).strip()
    if not kesinti_id:
        raise AdmHata(f"{ilce}: ADM kaydında Kesinti_ID boş")

    try:
        plan_bas = _dakikaya_yuvarla(parse_zaman(ham["Planlanan_Baslangic_Zamani"]))
        plan_bit = _dakikaya_yuvarla(parse_zaman(ham["Planlanan_Sona_Erme_Zamani"]))
        gercek_bas = parse_zaman(ham.get("Kesinti_Baslangic_Zamani"))
        gercek_bit = parse_zaman(ham.get("Kesinti_Sona_Erme_Zamani"))
    except ValueError as e:
        raise AdmHata(f"ADM zaman biçimi değişmiş (kayıt {kesinti_id}, ilçe {ilce}): {e}") from e

    if plan_bas is None:
        raise AdmHata(f"{ilce}: kayıt {kesinti_id} için planlanan başlangıç zamanı boş")

    durum = str(ham["Durum"] or "").strip()
    alan = alan_olustur(ham["Alan_Geometri"])
    iliskiler = kavsak_iliskileri(alan["geom"], kavsaklar, mesafe_m) if alan["geom"] is not None else []

    mahalleler = [str(m).strip() for m in (ham.get("Mahalle") or []) if str(m).strip()]
    sokaklar = [str(s).strip() for s in (ham.get("Sokak") or []) if str(s).strip()]
    mahalle_sokak = ham.get("MahalleSokak")

    return {
        "kesinti_id": kesinti_id,
        "durum": durum,
        "durum_kodu": durum_kodu_bul(durum, gercek_bas, gercek_bit),
        # ADM ilçeyi BÜYÜK harfle yazıyor (ham kayıtta saklı); tabloda istediğimiz yazımı kullanıyoruz
        "sehir": sehir.capitalize(),
        "ilce": ilce,
        "mahalleler": mahalleler,
        "sokaklar": sokaklar,
        "mahalle_sokak": mahalle_sokak if isinstance(mahalle_sokak, list) else None,
        "neden": str(ham.get("Kesinti_Nedeni") or "").strip() or None,
        "planlanan_baslangic": _iso(plan_bas),
        "planlanan_bitis": _iso(plan_bit),
        "gerceklesen_baslangic": _iso(gercek_bas),
        "gerceklesen_bitis": _iso(gercek_bit),
        "geometry": alan["geometry"],
        "geometry_turu": alan["turu"],
        "geometry_kaynak": alan["kaynak"],
        "merkez_lat": alan["merkez"][0] if alan["merkez"] else None,
        "merkez_lon": alan["merkez"][1] if alan["merkez"] else None,
        "sehir_ici": alan["sehir_ici"],
        "etkilenen_kavsaklar": iliskiler,
        "kavsak_sayisi": len(iliskiler),
        "ham": ham,
        "son_gorulme": simdi.isoformat(),
        "updated_at": simdi.isoformat(),
        "kaynaktan_kalkti": False,
    }


# ============================================================
# ANA AKIŞ
# ============================================================


def _saat_degisti_mi(mevcut: Dict[str, Any], yeni: Dict[str, Any]) -> bool:
    for alan in ("planlanan_baslangic", "planlanan_bitis"):
        eski = mevcut.get(alan)
        if not eski:
            continue
        try:
            e = datetime.fromisoformat(str(eski).replace("Z", "+00:00"))
            y = datetime.fromisoformat(yeni[alan])
        except (ValueError, TypeError):
            continue
        if e != y:
            return True
    return False


def run_adm_sync(db=None, session: Optional[requests.Session] = None, simdi: Optional[datetime] = None, bekleme_s: float = 2.0) -> Dict[str, Any]:
    """
    Tüm ADM adımını çalıştırır ve sonucu adm_sync_durumu tablosuna yazar.
    Asla istisna fırlatmaz: sorun varsa durumu 'hata' / 'kismen' yazar.
    Döndürülen sözlük testlerde ve log için kullanılır.
    """

    if db is None:
        import db_supabase as db  # noqa: PLC0415  (tembel import: testlerde sahte db verilebilsin)

    cfg = ayarlar()
    simdi = simdi or datetime.now(TR_SAAT)

    sonuc: Dict[str, Any] = {"ilceler": {}, "hatalar": {}, "uyarilar": [], "kayit_sayisi": 0}

    # Kavşak listesi (yoksa çalışmalar yine kaydedilir, yalnızca ilişki hesaplanmaz)
    kavsaklar: List[Dict[str, Any]] = []
    try:
        kavsaklar = db.get_kavsaklar()
        if not kavsaklar:
            sonuc["uyarilar"].append("kavsaklar tablosu boş: çalışma-kavşak ilişkisi hesaplanamadı")
    except Exception as e:  # noqa: BLE001
        sonuc["uyarilar"].append(
            f"kavsaklar tablosu okunamadı, kavşak ilişkisi hesaplanmadı ({type(e).__name__}). "
            "supabase/kavsaklar.sql çalıştırıldı mı?"
        )

    own_session = session is None
    if own_session:
        session = build_session(cfg["user_agent"])

    try:
        for ilce in cfg["ilceler"]:
            try:
                ham_kayitlar = ilce_cek(
                    session, cfg["api_url"], cfg["sehir"], ilce, bekleme_s=bekleme_s
                )

                satirlar = []
                gorulen = set()
                for ham in ham_kayitlar:
                    satir = kayit_normalize(ham, ilce, cfg["sehir"], kavsaklar, cfg["mesafe_m"], simdi)
                    if satir["kesinti_id"] in gorulen:
                        continue
                    gorulen.add(satir["kesinti_id"])
                    satirlar.append(satir)

                bilinmeyen = sorted({s["durum"] for s in satirlar if s["durum_kodu"] == "bilinmiyor"})
                if bilinmeyen:
                    sonuc["uyarilar"].append(
                        f"{ilce}: ADM tanınmayan durum değeri gönderdi: {', '.join(repr(b) for b in bilinmeyen)}"
                    )

                mevcut = db.get_adm_mevcut(ilce)

                # Güvenlik: ADM boş liste döndürdü ama elimizde canlı kayıt var -> bozuk yanıt say
                canli = [m for m in mevcut if m.get("durum_kodu") in ("planlandi", "devam_ediyor")]
                if not satirlar and canli:
                    raise AdmHata(
                        f"{ilce}: ADM boş liste döndürdü ama kayıtlı {len(canli)} planlı çalışma var; "
                        "yanıt hatalı olabilir, kayıtlar silinmedi"
                    )

                mevcut_by_id = {m["kesinti_id"]: m for m in mevcut}
                for satir in satirlar:
                    onceki = mevcut_by_id.get(satir["kesinti_id"])
                    if onceki and _saat_degisti_mi(onceki, satir):
                        satir["onceki_plan"] = {
                            "baslangic": onceki.get("planlanan_baslangic"),
                            "bitis": onceki.get("planlanan_bitis"),
                            "zaman": simdi.isoformat(),
                        }

                kalkan = [m["kesinti_id"] for m in mevcut if m["kesinti_id"] not in gorulen]
                if satirlar or kalkan:  # yapacak iş yoksa veritabanına gitme
                    db.save_adm_calismalari(satirlar, kalkan)

                sonuc["ilceler"][ilce] = {"kayit": len(satirlar), "kalkan": len(kalkan)}
                sonuc["kayit_sayisi"] += len(satirlar)

            except AdmHata as e:
                sonuc["hatalar"][ilce] = str(e)
            except Exception as e:  # noqa: BLE001  (örn. veritabanı hatası)
                sonuc["hatalar"][ilce] = (
                    f"{ilce}: kayıt sırasında beklenmeyen hata ({type(e).__name__}: {e}). "
                    "supabase_adm_calismalari.sql çalıştırıldı mı?"
                )
    finally:
        if own_session:
            session.close()

    # ---- Durum özeti ----
    hata_sayisi = len(sonuc["hatalar"])
    toplam = len(cfg["ilceler"])
    if hata_sayisi == 0:
        durum = "basarili"
    elif hata_sayisi < toplam:
        durum = "kismen"
    else:
        durum = "hata"

    mesajlar = list(sonuc["hatalar"].values())
    if durum == "basarili" and sonuc["uyarilar"]:
        mesajlar = sonuc["uyarilar"]

    payload: Dict[str, Any] = {
        "id": 1,
        "durum": durum,
        "son_deneme": simdi.isoformat(),
        "hata_mesaji": " | ".join(mesajlar) if mesajlar else None,
        "detay": {
            "ilceler": sonuc["ilceler"],
            "hatalar": sonuc["hatalar"],
            "uyarilar": sonuc["uyarilar"],
        },
    }
    if durum in ("basarili", "kismen"):
        payload["son_basarili"] = simdi.isoformat()
        payload["kayit_sayisi"] = sonuc["kayit_sayisi"]

    try:
        db.save_adm_sync_durumu(payload)
    except Exception as e:  # noqa: BLE001
        print(f"ADM durumu kaydedilemedi: {e}")

    sonuc["durum"] = durum
    return sonuc
