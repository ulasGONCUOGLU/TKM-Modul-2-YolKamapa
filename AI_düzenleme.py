# -*- coding: utf-8 -*-
"""
AI_düzenleme.py
================
Güzergah metnini (ör. "75 Sokak") yapay zeka (Gemini) ile analiz edip,
konum.py'nin (geocoder) kullanabileceği sorgu bileşenlerine ayıran modül.

DEĞİŞİKLİK (Supabase entegrasyonu):
Prompt'un giriş cümlesi ("Sen Denizli'deki belediyelerin...") artık
kod içinde sabit değil, Supabase'teki settings tablosundan okunuyor.
Nuxt'taki Ayarlar sayfasında bu metin değiştirilip kaydedilince, bir
sonraki pipeline çalıştırmasında AI otomatik olarak yeni talimatı
kullanır. Aşağıdaki numaralı kurallar ise pipeline'ın doğru çalışması
için kritik olduğundan kasıtlı olarak sabit tutulmuştur (kullanıcının
yanlışlıkla bozmaması için) — sadece giriş cümlesi/bağlamı değişebilir.

main.py bu modülden SADECE `analyze_route(guzergah_raw)` fonksiyonunu
çağırır ve karşılığında aşağıdaki gibi bir sözlük (dict) alır — ya da
hata durumunda None:

    {
        # --- Parça listesi (YENİ): duyuruda birden fazla yol/sokak
        #     parçası varsa her biri ayrı bir öğedir.
        "segments": [
            {
                "location_type": "road" | "road_segment" | "intersection" |
                                  "address" | "landmark" | "unknown",
                "road_name": str | None,
                "query": str | None,          # yol/sokak adı
                "start_query": str | None,    # parçanın başlangıç sınırı
                "end_query": str | None,      # parçanın bitiş sınırı
                "includes_connecting_streets": bool,
            },
            ...
        ],
        "neighborhood": str | None,
        "district": str | None,
        "confidence": float,

        # --- Eski alanlar (geriye uyumluluk): ilk parçadan türetilir.
        #     Eski kayıtlar ve web arayüzü bunları kullanmaya devam eder.
        "location_type": ..., "road_name": ..., "query": ...,
        "start_query": ..., "end_query": ...,
    }
"""

from __future__ import annotations

import os
from typing import Literal, Optional

from dotenv import load_dotenv
from google import genai
from pydantic import BaseModel

import db_supabase as db

# ============================================================
# AYARLAR
# ============================================================

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not GEMINI_API_KEY:
    raise ValueError("GEMINI_API_KEY .env dosyasında bulunamadı.")

# Model .env'den değiştirilebilir (GEMINI_MODEL); API anahtarı aynı kalır.
# Güncel model ID'leri ve kapatma (shutdown) tarihleri:
#   https://ai.google.dev/gemini-api/docs/models
#   https://ai.google.dev/gemini-api/docs/deprecations
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "").strip() or "gemini-3.6-flash"

gemini_client = genai.Client(api_key=GEMINI_API_KEY)

# Supabase'teki settings tablosunda "system_prompt" hiç ayarlanmamışsa
# (örn. ilk kurulumda) kullanılacak yedek metin.
DEFAULT_SYSTEM_PROMPT = (
    "Sen Denizli'deki belediyelerin yol kapatma duyurularını analiz eden "
    "bir konum analiz sistemisin."
)


# ============================================================
# AI VERİ MODELİ
# ============================================================

LocationType = Literal[
    "road", "road_segment", "intersection", "address", "landmark", "unknown"
]

# Bir duyurudan en fazla bu kadar parça işlenir (Nominatim hız sınırı ve
# maliyet için üst sınır; fazlası sessizce kesilir).
MAX_SEGMENTS = 12


class RouteSegment(BaseModel):
    """Duyurudaki TEK bir yol/sokak parçası."""

    location_type: LocationType

    road_name: str | None

    road_query: str | None
    start_query: str | None
    end_query: str | None

    # Metinde "... bağlanan sokaklar" gibi bir ifade bu parçayı kapsıyorsa True.
    includes_connecting_streets: bool


class RoadAnalysis(BaseModel):
    neighborhood: str | None
    district: str | None

    segments: list[RouteSegment]

    confidence: float


# ============================================================
# AI PROMPT
# ============================================================

def build_ai_prompt(guzergah_raw: str) -> str:

    system_prompt = db.get_setting("system_prompt", DEFAULT_SYSTEM_PROMPT)

    return f"""
{system_prompt}

Görevin, verilen güzergah metnini analiz ederek geocoding
işlemi için kullanılabilecek konum bilgilerini çıkarmaktır.

Kurallar:

1. Asla koordinat üretme.
2. Asla koordinat tahmin etme.
3. Metinde bulunmayan bir yol veya adresi gerçekmiş gibi
   uydurma.
4. Güzergah metni BİR VEYA BİRDEN FAZLA yol/sokak parçası
   içerebilir. Metindeki her ayrı parça için segments listesine
   AYRI bir öğe ekle. Hiçbir parçayı atlama, birbirine karıştırma
   ya da tek öğede birleştirme. En fazla {MAX_SEGMENTS} öğe.
5. Her öğenin location_type değeri:
   - Yolun tamamı belirtiliyorsa "road".
   - Bir yolun iki nokta (kesişen sokak/cadde) arasındaki bölümü
     belirtiliyorsa "road_segment".
   - İki yolun kesiştiği nokta belirtiliyorsa "intersection".
   - Açık bir adres ise "address".
   - Bir bina, tesis veya bilinen nokta ise "landmark".
   - Emin değilsen "unknown".
6. road_query: parçanın yol/cadde/sokak adı. start_query ve
   end_query: parçanın başlangıç ve bitişini belirleyen kesişen
   sokak/cadde adları.
   Örnek: "A Caddesi'nin B Sokak ile C Sokak arasında kalan kısmı"
   -> location_type="road_segment", road_query="A Caddesi",
   start_query="B Sokak", end_query="C Sokak".
7. Yol adlarını şu biçimde yaz:
   - Rakamlı sokaklarda "3131 Sokak" biçimini kullan; nokta
     ("3131. Sokak"), kısaltma ("Sk.", "Sok.") ve çekim eki
     ("Sokağın", "Sokağa", "Sokağı") KULLANMA.
   - Tamlama ve çekim eklerini kaldır:
     "Hüseyin Çokal Caddesi’ nin" -> "Hüseyin Çokal Caddesi".
   - Adı metinde yazıldığı gibi koru; düzeltme, çeviri veya
     tahmin yapma.
8. Metinde "... bağlanan sokaklar" gibi bir ifade geçiyorsa, bu
   ifadeyle anılan her parçanın (hem ana yol hem de metinde adı
   sayılan bağlanan sokaklar) includes_connecting_streets değerini
   true yap. Bağlanan sokaklar için kendin yeni sokak adı UYDURMA;
   sadece metinde adı geçenleri parça yap. İfade yoksa false.
9. confidence değerini 0 ile 1 arasında ver (tüm analiz için tek değer).
10. Metinde olmayan bilgileri uydurmak yerine ilgili alanı null bırak.
11. Metinde bir mahalle adı geçiyorsa (örn. "Yenişehir Mahallesi",
    "Şirinköy Mahallesi") bunu neighborhood alanına yaz.
    "Mahallesi" / "Mah." gibi ekleri kaldırıp sadece mahalle
    adını yaz (örn. "Yenişehir").
12. Metinde bir ilçe adı geçiyorsa (örn. "Merkezefendi",
    "Pamukkale", "Honaz") bunu district alanına yaz.
13. road_query, start_query ve end_query alanlarına SADECE
    yol/cadde/sokak adını yaz. Mahalle ve ilçe bilgisini bu
    alanlara ekleme; onlar ayrı ayrı neighborhood ve district
    alanlarına yazılacak ve geocoder sorgusu program tarafından
    birleştirilecek.
14. Mahalle veya ilçe metinde hiç geçmiyorsa neighborhood veya
    district alanını null bırak, tahmin etme.

Güzergah metni:

{guzergah_raw}
"""


# ============================================================
# AI ÇAĞRISI (dahili)
# ============================================================

def _analyze_route_with_ai(guzergah_raw: str) -> Optional[RoadAnalysis]:
    """
    Gemini'ye güzergah metnini gönderir, RoadAnalysis olarak
    parse edilmiş sonucu döndürür. Hata durumunda None döner.
    """

    prompt = build_ai_prompt(guzergah_raw)

    try:
        response = gemini_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config={
                "response_mime_type": "application/json",
                "response_schema": RoadAnalysis,
            },
        )

        return response.parsed

    except Exception as e:
        print("AI çağrısı hatası:")
        print(type(e).__name__)
        print(e)

        return None


# ============================================================
# DIŞARIYA AÇILAN TEK FONKSİYON
# ============================================================

def analyze_route(guzergah_raw: str) -> Optional[dict]:
    """
    main.py'nin çağırdığı tek giriş noktası.

    Güzergah metnini AI ile analiz eder, sonucu sade bir sözlük
    (dict) olarak döndürür. AI çağrısı başarısız olursa None
    döner; main.py bu durumu 'error' olarak kaydedip devam eder.
    """

    analysis = _analyze_route_with_ai(guzergah_raw)

    if analysis is None:
        return None

    segments = [
        {
            "location_type": seg.location_type,
            "road_name": seg.road_name,
            "query": seg.road_query,
            "start_query": seg.start_query,
            "end_query": seg.end_query,
            "includes_connecting_streets": bool(seg.includes_connecting_streets),
        }
        for seg in (analysis.segments or [])[:MAX_SEGMENTS]
    ]

    # Eski alanlar ilk parçadan türetilir (geriye uyumluluk: eski kayıtlar,
    # tablo ekranı ve db_supabase.save_ai_result bunları okur).
    ilk = segments[0] if segments else {}

    return {
        "segments": segments,
        "neighborhood": analysis.neighborhood,
        "district": analysis.district,
        "confidence": analysis.confidence,
        "location_type": ilk.get("location_type", "unknown"),
        "road_name": ilk.get("road_name"),
        "query": ilk.get("query"),
        "start_query": ilk.get("start_query"),
        "end_query": ilk.get("end_query"),
    }
