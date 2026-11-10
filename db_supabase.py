# -*- coding: utf-8 -*-
"""
db_supabase.py
==============
SQLite tabanlı eski kalıcılık katmanının yerini alır.

main.py bu modülden SADECE aşağıdaki fonksiyonları çağırır ve Supabase
SDK detaylarını (bağlantı, tablo adları, RPC çağrıları) hiç bilmek
zorunda değildir.

Gerekli ortam değişkenleri (.env):
    SUPABASE_URL
    SUPABASE_SERVICE_ROLE_KEY   <-- DİKKAT: bu anahtar asla tarayıcıya
                                    veya herkese açık bir yere sızmamalı.
                                    Sadece bu script'in çalıştığı
                                    ortamda (kendi bilgisayarın / Docker
                                    container'ı) .env içinde durmalı.
"""

from __future__ import annotations

import os
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from supabase import Client, create_client

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

if not SUPABASE_URL:
    raise ValueError("SUPABASE_URL .env dosyasında bulunamadı.")

if not SUPABASE_SERVICE_ROLE_KEY:
    raise ValueError("SUPABASE_SERVICE_ROLE_KEY .env dosyasında bulunamadı.")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


# ============================================================
# YENİ KAYIT EKLE
# ============================================================

def save_record(
    record: Dict[str, Any],
    record_hash: str,
    start_date_value: Optional[date],
    end_date_value: Optional[date],
    active: bool,
) -> bool:
    """
    record_hash'e göre "varsa dokunma" mantığıyla ekler.

    Supabase'te native "INSERT OR IGNORE" olmadığı için
    upsert + ignore_duplicates kullanılır: kayıt zaten varsa
    response.data boş döner; biz bunu "yeni eklenmedi" sayıyoruz.

    Döndürür: yeni eklendiyse True, zaten vardıysa False.
    """

    now = datetime.now().isoformat()

    payload = {
        "record_hash": record_hash,
        "guzergah_raw": record["guzergah"],
        "tarih_raw": record["tarih"],
        "kapanma_nedeni_raw": record["kapanma_nedeni"],
        "ilce_mahalle_raw": record.get("ilce_mahalle_raw"),
        "ilce": record.get("ilce"),
        "mahalle": record.get("mahalle"),
        "start_date": start_date_value.isoformat() if start_date_value else None,
        "end_date": end_date_value.isoformat() if end_date_value else None,
        "active": active,
        "created_at": now,
        "updated_at": now,
    }

    response = (
        supabase.table("closures")
        .upsert(payload, on_conflict="record_hash", ignore_duplicates=True)
        .execute()
    )

    return len(response.data) > 0


# ============================================================
# AKTİF DURUMUNU BUGÜNE GÖRE YENİDEN HESAPLA
# ============================================================

def refresh_active_status() -> None:
    """
    Postgres tarafındaki refresh_active_status() RPC fonksiyonunu
    çağırır (bkz. sql/kurulum.sql). Tüm kayıtların active
    alanını tek seferde bugünün tarihine göre yeniden hesaplar.
    """
    supabase.rpc("refresh_active_status").execute()


# ============================================================
# AI SONUCUNU KAYDET
# ============================================================

def save_ai_result(record_id: int, ai_result: Dict[str, Any]) -> None:
    supabase.table("closures").update(
        {
            "ai_status": "success",
            "ai_location_type": ai_result.get("location_type"),
            "ai_neighborhood": ai_result.get("neighborhood"),
            "ai_district": ai_result.get("district"),
            "ai_query": ai_result.get("query"),
            "ai_start_query": ai_result.get("start_query"),
            "ai_end_query": ai_result.get("end_query"),
            "ai_result": ai_result,
            "updated_at": datetime.now().isoformat(),
        }
    ).eq("id", record_id).execute()


def save_ai_error(record_id: int, error_message: str) -> None:
    supabase.table("closures").update(
        {
            "ai_status": "error",
            "ai_result": {"error": error_message},
            "updated_at": datetime.now().isoformat(),
        }
    ).eq("id", record_id).execute()


# ============================================================
# GEOCODE SONUCUNU KAYDET
# ============================================================

def save_geocode_result(record_id: int, result: Optional[Dict[str, Any]]) -> None:
    if result is None:
        supabase.table("closures").update(
            {
                "geocode_status": "not_found",
                "updated_at": datetime.now().isoformat(),
            }
        ).eq("id", record_id).execute()
        return

    supabase.table("closures").update(
        {
            "geocode_status": "success",
            "geocode_result": result,
            "updated_at": datetime.now().isoformat(),
        }
    ).eq("id", record_id).execute()


def save_geometry(record_id: int, geometry: Optional[Dict[str, Any]]) -> None:
    # Birden çok parça bulunduysa geometry bir GeometryCollection olabilir.
    if geometry is None:
        return

    supabase.table("closures").update(
        {
            "geometry_type": geometry.get("type"),
            "geometry": geometry,
            "updated_at": datetime.now().isoformat(),
        }
    ).eq("id", record_id).execute()


# ============================================================
# BEKLEYEN/EKSİK KAYITLARI GETİR
# ============================================================
#
# Not: Supabase REST API'sinde "ai_status IS NULL OR ai_status != 'success'
# OR ..." gibi karma OR/IS NULL filtrelerini tek sorguda ifade etmek
# kırılgan olduğundan (özellikle jsonb/null karışımı), aktif kayıtları
# çekip filtrelemeyi Python tarafında yapıyoruz. Bu projenin ölçeğinde
# (belediye duyuruları — yüzlerce kayıt) performans sorunu yaratmaz.
# ============================================================

def get_pending_ai_records() -> List[Dict[str, Any]]:
    """AI analizi hiç yapılmamış veya eksik/başarısız kalmış aktif kayıtlar."""

    response = (
        supabase.table("closures")
        .select("id, guzergah_raw, ilce, mahalle, ai_status, ai_result, ai_location_type")
        .eq("active", True)
        .execute()
    )

    def eski_bicim(row: Dict[str, Any]) -> bool:
        """Parça listesi (segments) eklenmeden önce analiz edilmiş kayıt: bir kez yeniden analiz edilir."""
        sonuc = row.get("ai_result")
        return isinstance(sonuc, dict) and "segments" not in sonuc and "error" not in sonuc

    return [
        row
        for row in response.data
        if row.get("ai_status") != "success"
        or row.get("ai_result") is None
        or row.get("ai_location_type") is None
        or eski_bicim(row)
    ]


def get_pending_geocode_records() -> List[Dict[str, Any]]:
    """AI'sı başarılı ama konum/koordinatı hâlâ eksik kalmış aktif kayıtlar."""

    response = (
        supabase.table("closures")
        .select(
            "id, guzergah_raw, ilce, mahalle, ai_result, "
            "ai_status, geocode_status, geocode_result, geometry"
        )
        .eq("active", True)
        .eq("ai_status", "success")
        .execute()
    )

    def eksik_parca(row: Dict[str, Any]) -> bool:
        """Parça bazlı sonuç yok (eski kayıt) ya da bazı parçalar bulunamamış: yeniden dene."""
        sonuc = row.get("geocode_result")
        if not isinstance(sonuc, dict) or "parcalar" not in sonuc:
            return True
        return sonuc.get("bulunan", 0) < sonuc.get("toplam", 0)

    return [
        row
        for row in response.data
        if row.get("ai_result") is not None
        and (
            row.get("geocode_status") != "success"
            or row.get("geocode_result") is None
            or row.get("geometry") is None
            or eksik_parca(row)
        )
    ]


# ============================================================
# AYARLAR (settings tablosu)
# ============================================================

def get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    """
    settings tablosundan bir anahtarın değerini okur.
    Ayarlar sayfasındaki "Sistem Talimatı" gibi kullanıcı tarafından
    değiştirilebilir metinler burada tutulur.
    """

    response = (
        supabase.table("settings")
        .select("value")
        .eq("key", key)
        .limit(1)
        .execute()
    )

    if not response.data:
        return default

    return response.data[0]["value"]


# ============================================================
# ADM (AYDEM) PLANLI ÇALIŞMALARI
# ============================================================
# Tablolar: sql/kurulum.sql. Mantık: adm.py.

def get_kavsaklar() -> List[Dict[str, Any]]:
    """Sinyalize kavşakların kodu, adı ve koordinatları (tablo: kavsaklar)."""
    response = (
        supabase.table("kavsaklar")
        .select("kod, ad, latitude, longitude")
        .limit(5000)
        .execute()
    )
    return response.data or []


def get_adm_mevcut(ilce: str) -> List[Dict[str, Any]]:
    """Bir ilçe için kayıtlı ADM çalışmalarının özeti (değişiklik/silinme tespiti için)."""
    response = (
        supabase.table("adm_calismalari")
        .select("kesinti_id, durum_kodu, planlanan_baslangic, planlanan_bitis")
        .eq("ilce", ilce)
        .limit(5000)
        .execute()
    )
    return response.data or []


def save_adm_calismalari(rows: List[Dict[str, Any]], kalkan_idler: List[str]) -> None:
    """
    Çalışmaları kesinti_id'ye göre ekler/günceller (ilk_gorulme korunur) ve
    ADM'nin listesinden kalkanları kaynaktan_kalkti=true olarak işaretler.
    """
    for i in range(0, len(rows), 100):
        supabase.table("adm_calismalari").upsert(
            rows[i : i + 100], on_conflict="kesinti_id"
        ).execute()

    for i in range(0, len(kalkan_idler), 100):
        (
            supabase.table("adm_calismalari")
            .update({"kaynaktan_kalkti": True, "updated_at": datetime.now().isoformat()})
            .in_("kesinti_id", kalkan_idler[i : i + 100])
            .execute()
        )


def save_adm_sync_durumu(payload: Dict[str, Any]) -> None:
    """adm_sync_durumu tek satırını (id=1) günceller; verilmeyen alanlar korunur."""
    supabase.table("adm_sync_durumu").upsert(payload, on_conflict="id").execute()
