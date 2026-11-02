-- ============================================================
-- ADM (Aydem) ELEKTRİK PLANLI ÇALIŞMALARI
-- ============================================================
-- Supabase > SQL Editor'e olduğu gibi yapıştırıp bir kez çalıştırın.
-- Tekrar çalıştırmak güvenlidir (veri silmez).
--
-- İki tablo:
--   adm_calismalari  : ADM'nin duyurduğu çalışmalar (Python "Verileri Çek" doldurur)
--   adm_sync_durumu  : ADM verisinin son çekme sonucu (tek satır). ADM beklenmedik
--                      bir şey döndürürse hata metni buraya yazılır, sitede gösterilir.
--
-- Önkoşul: supabase/kavsaklar.sql (web projesinde) önce çalıştırılmış olmalı;
-- çalışma ile sinyalize kavşak ilişkisi kavsaklar tablosundan hesaplanır.
-- Tarayıcı (anon) iki tabloyu yalnızca OKUYABİLİR; yazma service_role ile yapılır.
-- ============================================================

create table if not exists public.adm_calismalari (
  kesinti_id            text primary key,            -- ADM'nin Kesinti_ID'si
  durum                 text not null,               -- ADM'nin yazdığı: Planlandı / Arşivlendi / Planlı Kesinti İptal Edildi
  durum_kodu            text not null,               -- planlandi | devam_ediyor | tamamlandi | iptal | bilinmiyor
  sehir                 text,
  ilce                  text not null,
  mahalleler            text[] not null default '{}',
  sokaklar              text[] not null default '{}',
  mahalle_sokak         jsonb,                       -- [{Mahalle, Sokak:[...]}]
  neden                 text,
  planlanan_baslangic   timestamptz,
  planlanan_bitis       timestamptz,
  gerceklesen_baslangic timestamptz,
  gerceklesen_bitis     timestamptz,

  -- Alan: poligon (GeoJSON Polygon), çizgi, nokta ya da yok
  geometry              jsonb,
  geometry_turu         text not null default 'yok', -- polygon | cizgi | nokta | yok
  geometry_kaynak       text,                        -- ADM'nin "kaynak" değeri (strict / cbs)
  merkez_lat            double precision,
  merkez_lon            double precision,
  sehir_ici             boolean not null default false, -- Denizli şehir içi sınırında mı (haritada çizmek için)

  -- Sinyalize kavşaklarla ilişki (150 m): [{kod, ad, iliski: icinde|yakin, mesafe_m}]
  etkilenen_kavsaklar   jsonb not null default '[]'::jsonb,
  kavsak_sayisi         integer not null default 0,

  onceki_plan           jsonb,                       -- saat değiştiyse eski {baslangic, bitis, zaman}
  kaynaktan_kalkti      boolean not null default false, -- son çekmede ADM'nin listesinde yoktu
  ham                   jsonb not null,              -- ADM'den gelen kaydın aynısı

  ilk_gorulme           timestamptz not null default now(),
  son_gorulme           timestamptz not null default now(),
  updated_at            timestamptz not null default now()
);

create index if not exists adm_calismalari_planlanan_baslangic_idx
  on public.adm_calismalari (planlanan_baslangic);
create index if not exists adm_calismalari_durum_kodu_idx
  on public.adm_calismalari (durum_kodu);

create table if not exists public.adm_sync_durumu (
  id             integer primary key default 1,
  durum          text not null default 'bilinmiyor',  -- basarili | kismen | hata | bilinmiyor
  son_deneme     timestamptz,
  son_basarili   timestamptz,
  hata_mesaji    text,                                -- kullanıcıya gösterilen açıklama
  kayit_sayisi   integer,
  detay          jsonb,                               -- ilçe bazında sonuçlar, uyarılar
  constraint adm_sync_durumu_tek_satir check (id = 1)
);

insert into public.adm_sync_durumu (id) values (1) on conflict (id) do nothing;

-- ---- Güvenlik: tarayıcı yalnızca okur ----
alter table public.adm_calismalari enable row level security;
alter table public.adm_sync_durumu enable row level security;

drop policy if exists "adm_calismalari_public_read" on public.adm_calismalari;
create policy "adm_calismalari_public_read"
  on public.adm_calismalari for select
  to anon, authenticated
  using (true);

drop policy if exists "adm_sync_durumu_public_read" on public.adm_sync_durumu;
create policy "adm_sync_durumu_public_read"
  on public.adm_sync_durumu for select
  to anon, authenticated
  using (true);

revoke all on public.adm_calismalari from anon, authenticated;
revoke all on public.adm_sync_durumu from anon, authenticated;
grant select on public.adm_calismalari to anon, authenticated;
grant select on public.adm_sync_durumu to anon, authenticated;
