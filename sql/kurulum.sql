-- ############################################################
-- #  YOL KAPAMA — TEMİZ VERİTABANI KURULUMU (TEK DOSYA)      #
-- ############################################################
-- Yeni (boş) bir Supabase projesinde Dashboard → SQL Editor'e yapıştırıp BİR KEZ çalıştırın.
-- İçerik, sırayla:
--   1) settings, sync_runs, closures tabloları + RLS + refresh_active_status() fonksiyonu
--   2) kavsaklar  (sinyalize kavşaklar, 142 kayıt)
--   3) adm_calismalari + adm_sync_durumu  (ADM planlı elektrik çalışmaları; kavsaklar'a bağlıdır)
--
-- ⚠️  `closures` tablosu DROP edilip yeniden oluşturulur. Dolu bir projede çalıştırmayın.
-- Bu dosya TEK doğru kaynaktır; veritabanı şeması başka bir projede (Nuxt dahil) tutulmaz.
-- ############################################################

-- ============ BÖLÜM 1: ÇEKİRDEK ŞEMA ============
-- ============================================================
-- YOL KAPAMA — Supabase Şeması (TEK DOSYA, sıfırdan kurulum)
-- ============================================================
-- Bu dosya Supabase SQL Editor'e yapıştırılıp tek seferde çalıştırılacak
-- şekilde tasarlandı. Yeni bir Supabase projesinde projeyi baştan ayağa
-- kaldırmak için kullan.
--
-- ⚠️  UYARI — VERİ KAYBI:
-- Bu script `closures` tablosunu DROP edip yeniden oluşturuyor.
-- Eğer bunu, içinde gerçek veri olan MEVCUT bir projede çalıştırırsan
-- o veriler SİLİNİR. Sadece şu durumlarda çalıştır:
--   (a) Yepyeni bir Supabase projesinde ilk kurulum yapıyorsan, veya
--   (b) Mevcut projedeki closures verisini bilerek sıfırlamak
--       istiyorsan.
-- `settings` ve `sync_runs` tabloları `create table if not exists`
-- kullanır — zaten varsa dokunulmaz, veri kaybı olmaz.
--
-- RLS ÖZETİ (bu dosya çalıştıktan sonraki nihai durum):
--   closures   -> anon/authenticated: sadece SELECT. INSERT/UPDATE/
--                 DELETE YOK. Yazma işlemleri SADECE service_role ile
--                 (Python backend ve Nuxt'un server/api route'ları)
--                 yapılır — service_role zaten RLS'i atlar, bu yüzden
--                 closures için hiçbir yazma policy'si TANIMLANMAMALI.
--   settings   -> anon/authenticated: HİÇBİR ERİŞİM YOK (ne okuma ne
--                 yazma). Sadece service_role erişir (Nuxt'un
--                 server/api/yolkapama/ayarlar.get|post.ts route'ları
--                 ve Python'un AI_düzenleme.py'si üzerinden).
--   sync_runs  -> anon/authenticated: sadece SELECT (Nuxt'un polling
--                 için okuması gerekiyor). INSERT/UPDATE YOK — sadece
--                 service_role (Nuxt'un calistir.post.ts'i satırı
--                 oluşturur, Flask/Python durumunu günceller).
-- ============================================================


-- ============================================================
-- 1) settings
-- ============================================================

create table if not exists public.settings (
  key text primary key,
  value text,
  updated_at timestamptz not null default now()
);

comment on table public.settings is
  'Uygulama ayarları (ör. system_prompt) - sadece service_role ile (Nuxt sunucu route + Python) erişilir';

alter table public.settings enable row level security;

-- Kasıtlı olarak HİÇ policy eklenmiyor: anon/authenticated hiçbir şey
-- okuyup yazamaz. Ayarlar sayfası okuma/yazmayı Nuxt sunucu route'u
-- üzerinden (service_role ile) yapar.

insert into public.settings (key, value) values (
  'system_prompt',
  'Sen Denizli''deki belediyelerin yol kapatma duyurularını analiz eden bir konum analiz sistemisin. Gelen metinleri inceleyip kapatılan caddeleri, sokakları, başlangıç ve bitiş tarihlerini çıkar.'
)
on conflict (key) do nothing;


-- ============================================================
-- 2) sync_runs
-- ============================================================

do $$
begin
  if not exists (select 1 from pg_type where typname = 'sync_run_status') then
    create type public.sync_run_status as enum ('beklemede', 'calisiyor', 'basarili', 'hata');
  end if;
end
$$;

create table if not exists public.sync_runs (
  id bigint generated always as identity primary key,
  durum public.sync_run_status not null default 'beklemede',
  baslangic timestamptz,
  bitis timestamptz,
  hata_mesaji text,
  olusturulma timestamptz not null default now()
);

comment on table public.sync_runs is
  'Python veri çekme script''inin çalıştırma geçmişi ve anlık durumu (Nuxt bunu polling ile izler)';

alter table public.sync_runs enable row level security;

drop policy if exists "sync_runs_public_read" on public.sync_runs;

-- Nuxt tarayıcıdan durumu okuyabilsin diye herkese açık SELECT.
-- insert/update yalnızca service_role: satırı Nuxt sunucu route'u
-- oluşturur, durumunu Flask/Python günceller.
create policy "sync_runs_public_read"
  on public.sync_runs
  for select
  to anon, authenticated
  using (true);

create index if not exists idx_sync_runs_olusturulma
  on public.sync_runs (olusturulma desc);


-- ============================================================
-- 3) closures
-- ============================================================
-- DROP + CREATE: yukarıdaki uyarıyı oku. Bu, tablonun üzerindeki TÜM
-- policy'leri de (elle eklenmiş olanlar dahil) temizleyip sıfırdan
-- kurar.

drop table if exists public.closures cascade;

create table public.closures (
  id bigint generated always as identity primary key,

  -- Tekilleştirme anahtarı: guzergah+tarih+neden hash'i
  record_hash text not null unique,

  -- Kaynaktan gelen ham veriler
  guzergah_raw text not null,
  tarih_raw text not null,
  kapanma_nedeni_raw text not null,

  -- Kaynak sitedeki "İLÇE-MAHALLE" hücresi (AI tahminine gerek kalmadan)
  ilce_mahalle_raw text,
  ilce text,
  mahalle text,

  -- Python tarafından ayrıştırılan tarih aralığı
  start_date date,
  end_date date,
  active boolean not null default false,

  -- AI (Gemini) analiz sonucu
  ai_status text not null default 'pending',       -- pending | success | error
  ai_location_type text,
  ai_neighborhood text,
  ai_district text,
  ai_query text,
  ai_start_query text,
  ai_end_query text,
  ai_result jsonb,

  -- Geocoder (Nominatim/OSRM) sonucu
  geocode_status text not null default 'pending',   -- pending | success | not_found
  geocode_result jsonb,

  -- Harita geometrisi (Point veya LineString GeoJSON)
  geometry_type text,
  geometry jsonb,

  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

comment on table public.closures is
  'Belediye yol kapatma duyuruları - Python pipeline (main.py) tarafından dolduruluyor';
comment on column public.closures.geometry is
  'GeoJSON: {"type":"Point","coordinates":[lon,lat]} veya {"type":"LineString","coordinates":[[lon,lat],...]}';

alter table public.closures enable row level security;

-- SADECE SELECT. Tarayıcıdan (anon key) doğrudan okunabilsin diye.
-- INSERT/UPDATE/DELETE için KASITLI OLARAK policy YOK: bu tabloya
-- yazma SADECE service_role ile yapılır (Python backend + Nuxt'un
-- server/api/yolkapama/kapama/[id].patch.ts route'u). service_role
-- zaten RLS'i atlar, bu yüzden ona ayrıca bir policy tanımlamaya
-- gerek yoktur ve anon/authenticated'a asla UPDATE/INSERT/DELETE
-- policy'si EKLENMEMELİDİR (anon key tarayıcıda herkese açıktır).
create policy "closures_public_read"
  on public.closures
  for select
  to anon, authenticated
  using (true);

create index idx_closures_active on public.closures (active);
create index idx_closures_ai_status on public.closures (ai_status);
create index idx_closures_geocode_status on public.closures (geocode_status);


-- ============================================================
-- 4) RPC: refresh_active_status
-- ============================================================
-- main.py her çalıştığında tüm kayıtların 'active' durumunu bugünün
-- tarihine göre tek seferde yeniden hesaplar. Supabase REST API'si
-- "tüm satırları koşulsuz güncelle" işlemini native desteklemediği
-- için bunu bir Postgres fonksiyonu (RPC) olarak tanımlıyoruz.
--
-- `where true`: Postgres bazı bağlantı modlarında WHERE'siz UPDATE'i
-- güvenlik önlemi olarak reddediyor ("UPDATE requires a WHERE clause");
-- `where true` mantığı değiştirmeden bu kısıtlamayı karşılar.

create or replace function public.refresh_active_status()
returns void
language sql
security definer
set search_path = public
as $$
  update public.closures
  set
    active = case
      when start_date is not null
        and end_date is not null
        and start_date <= current_date
        and end_date >= current_date
      then true
      else false
    end,
    updated_at = now()
  where true;
$$;

revoke execute on function public.refresh_active_status() from public;
revoke execute on function public.refresh_active_status() from anon;
revoke execute on function public.refresh_active_status() from authenticated;
grant execute on function public.refresh_active_status() to service_role;


-- ============================================================
-- KURULUM SONRASI DOĞRULAMA (opsiyonel, elle çalıştırılabilir)
-- ============================================================
-- Aşağıdaki sorgu, her tabloda hangi rollere hangi izinlerin
-- tanımlı olduğunu gösterir. closures ve sync_runs'ta sadece
-- "SELECT" / "anon,authenticated" satırlarını görmelisin;
-- settings'te hiç satır görmemelisin.
--
-- select schemaname, tablename, policyname, roles, cmd
-- from pg_policies
-- where schemaname = 'public'
-- order by tablename, policyname;


-- ============ BÖLÜM 2: SİNYALİZE KAVŞAKLAR ============
-- ============================================================
-- SİNYALİZE KAVŞAKLAR (kavsaklar.kmz -> Supabase)
-- ============================================================
-- Supabase > SQL Editor'e olduğu gibi yapıştırıp bir kez çalıştırın.
-- Tekrar çalıştırmak güvenlidir: kayıtlar "kod" üzerinden güncellenir,
-- çift kayıt oluşmaz.
--
-- Koordinatlar KMZ dosyasındaki Point alanından alındı; enlem ve boylam
-- ayrı sütunlarda tutulur (Excel'deki "37.7872429.08587" gibi birleşik
-- yazım sorunu burada yoktur).
-- ============================================================

create table if not exists public.kavsaklar (
  kod              text primary key,            -- örn. DNZ-01
  ad               text not null,               -- örn. Demokrasi
  bolge            text not null,               -- Pamukkale / Merkezefendi / Honaz
  tkm_bagli        boolean not null default false,
  butonlu          boolean not null default false,
  adaptif          boolean not null default false,
  durum            text not null default 'Aktif',
  mod              text,
  model            text,
  iletisim         text,
  yapim_tarihi     date,
  elektrik_abone_no text,
  aciklama         text,
  latitude         double precision not null,
  longitude        double precision not null,
  created_at       timestamptz not null default now(),
  -- Denizli çevresi dışındaki (ters yazılmış/bozuk) koordinatları reddeder
  constraint kavsaklar_lat_chk check (latitude  between 37.0 and 38.5),
  constraint kavsaklar_lon_chk check (longitude between 28.5 and 29.7)
);

-- Tarayıcıdan (anon key) yalnızca OKUMA. Yazma yalnızca bu SQL / service_role ile.
alter table public.kavsaklar enable row level security;

drop policy if exists "kavsaklar_public_read" on public.kavsaklar;
create policy "kavsaklar_public_read"
  on public.kavsaklar for select
  to anon, authenticated
  using (true);

revoke all on public.kavsaklar from anon, authenticated;
grant select on public.kavsaklar to anon, authenticated;

insert into public.kavsaklar
  (kod, ad, bolge, tkm_bagli, butonlu, adaptif, durum, mod, model, iletisim,
   yapim_tarihi, elektrik_abone_no, aciklama, latitude, longitude)
values
  ('DNZ-01', 'Demokrasi', 'Pamukkale', true, false, true, 'Aktif', 'Adaptif', 'Maestro', 'Fiber', '1990-01-01', null, null, 37.7546588, 29.0911531),
  ('DNZ-02', 'Zübeyde Hanım', 'Pamukkale', true, false, true, 'Aktif', 'Adaptif', 'Maestro', 'Fiber', null, null, null, 37.7599688, 29.0923119),
  ('DNZ-03', 'Kıbrıs Şehitler İnönü', 'Pamukkale', true, false, true, 'Aktif', 'Adaptif-Koordine', 'Maestro', 'Fiber', null, null, null, 37.7629968, 29.0892327),
  ('DNZ-04', 'Kıbrıs Şehitler Emek', 'Pamukkale', true, false, true, 'Aktif', 'Adaptif-Koordine', 'Maestro', 'Fiber', null, null, null, 37.7658848, 29.0860462),
  ('DNZ-05', 'İncilipınar', 'Pamukkale', true, false, true, 'Aktif', 'Adaptif-Koordine', 'Maestro', 'Fiber', null, null, null, 37.7651087, 29.0985346),
  ('DNZ-06', '10 Nisan', 'Pamukkale', true, false, true, 'Aktif', 'Adaptif-Koordine', 'Maestro', 'Fiber', null, null, null, 37.7677761, 29.0949243),
  ('DNZ-07', 'Suluköprü', 'Pamukkale', true, false, true, 'Aktif', 'Adaptif-Koordine', 'Maestro', 'Fiber', null, null, null, 37.7710879, 29.0916467),
  ('DNZ-08', 'Pelitlibağ', 'Pamukkale', true, false, true, 'Aktif', 'Adaptif', 'Maestro', 'Fiber', null, null, null, 37.7721035, 29.0949699),
  ('DNZ-09', 'Atatürk Halk', 'Pamukkale', true, false, true, 'Aktif', 'Adaptif-Koordine', 'Maestro', 'Fiber', null, null, null, 37.7778680, 29.0960991),
  ('DNZ-10', 'Hacıkaplanlar', 'Pamukkale', true, false, true, 'Aktif', 'Adaptif-Koordine', 'Maestro', 'Fiber', null, null, null, 37.7750928, 29.0923521),
  ('DNZ-11', 'Çınar', 'Pamukkale', true, false, true, 'Aktif', 'Adaptif-Koordine', 'Maestro', 'Fiber', null, null, null, 37.7734964, 29.0874329),
  ('DNZ-12', 'İtfaiye', 'Pamukkale', true, false, true, 'Aktif', 'Adaptif-Koordine', 'Maestro', 'Fiber', null, null, null, 37.7813065, 29.0873230),
  ('DNZ-13', 'Turan Güneş', 'Pamukkale', true, false, true, 'Aktif', 'Adaptif', 'Maestro', 'Fiber', null, null, null, 37.7829854, 29.0903378),
  ('DNZ-14', 'Halley', 'Pamukkale', true, false, true, 'Aktif', 'Adaptif', 'Maestro', 'Fiber', '2018-03-15', null, '2. Tic. Yol kavaşağı ile birleştirildi.', 37.7850883, 29.0882778),
  ('DNZ-15', 'Ulucami', 'Pamukkale', true, false, true, 'Aktif', 'Adaptif-Koordine', 'Maestro', 'Fiber', null, null, 'Uzaktan açıp kapatma 1. port', 37.7830448, 29.0853649),
  ('DNZ-16', 'Hacılarcami', 'Merkezefendi', true, false, true, 'Aktif', 'Adaptif', 'Maestro', 'Fiber', null, null, null, 37.7872462, 29.0858746),
  ('DNZ-17', 'Doktorlar Cad', 'Merkezefendi', true, false, true, 'Aktif', 'Adaptif-Koordine', 'Maestro', 'Fiber', null, null, null, 37.7819679, 29.0829241),
  ('DNZ-18', 'Sağlık Müdürlüğü', 'Merkezefendi', true, false, true, 'Aktif', 'Adaptif-Koordine', 'Maestro', 'Fiber', null, null, null, 37.7832271, 29.0817976),
  ('DNZ-19', 'Mimar Sinan', 'Merkezefendi', true, false, true, 'Aktif', 'Adaptif', 'Maestro', 'Fiber', null, null, null, 37.7756970, 29.0820336),
  ('DNZ-20', 'Ahi Sinan', 'Merkezefendi', true, false, true, 'Aktif', 'Adaptif-Koordine', 'Maestro', 'Fiber', '2023-07-20', null, null, 37.7888869, 29.0816796),
  ('DNZ-21', 'Eski Zahire Pazarı', 'Merkezefendi', true, false, true, 'Aktif', 'Adaptif-Koordine', 'Maestro', 'Fiber', '2019-03-30', null, null, 37.7871148, 29.0810037),
  ('DNZ-22', 'Esma Hatun', 'Merkezefendi', true, false, true, 'Aktif', 'Adaptif-Koordine', 'Maestro', 'Fiber', '2021-07-30', null, 'Geometrik Düzenleme yapıldı.', 37.7810797, 29.0751565),
  ('DNZ-23', 'Devlet Hastanesi', 'Merkezefendi', true, false, true, 'Aktif', 'Adaptif-Koordine', 'Maestro', 'Fiber', null, null, null, 37.7822986, 29.0762937),
  ('DNZ-24', 'Özay Gönlüm', 'Merkezefendi', true, false, true, 'Aktif', 'Adaptif-Koordine', 'Maestro', 'Fiber', '2021-05-30', null, 'Geometrik Düzenleme yapıldı.', 37.7790594, 29.0729946),
  ('DNZ-25', 'Bim Kavşağı', 'Pamukkale', true, false, true, 'Aktif', 'Adaptif-Koordine', 'Maestro', 'Fiber', null, null, null, 37.7498321, 29.0977916),
  ('DNZ-26', 'Anadolu Lisesi', 'Pamukkale', true, false, true, 'Aktif', 'Adaptif-Koordine', 'Maestro', 'Fiber', null, null, null, 37.7469838, 29.1016728),
  ('DNZ-27', 'Üniversite Kavşağı', 'Pamukkale', true, false, true, 'Aktif', 'Adaptif', 'Maestro', 'Fiber', null, null, null, 37.7433729, 29.1048700),
  ('DNZ-28', 'Endüstri Meslek', 'Pamukkale', true, false, true, 'Aktif', 'Adaptif-Koordine', 'Maestro', 'Fiber', null, null, null, 37.7443263, 29.0972793),
  ('DNZ-29', 'Kınıklı Belediye', 'Pamukkale', true, false, true, 'Aktif', 'Adaptif-Koordine', 'Maestro', 'Fiber', null, null, null, 37.7463114, 29.0930414),
  ('DNZ-30', 'Ulus Tokat', 'Pamukkale', true, false, true, 'Aktif', 'Adaptif-Koordine', 'Maestro', 'Fiber', null, null, null, 37.7545613, 29.0993983),
  ('DNZ-31', 'Ulus Bursa', 'Pamukkale', true, false, true, 'Aktif', 'Adaptif-Koordine', 'Maestro', 'Fiber', null, null, null, 37.7532115, 29.0951832),
  ('DNZ-32', 'Havuzlu Köşk', 'Pamukkale', true, false, true, 'Aktif', 'Adaptif-Koordine', 'Maestro', 'Fiber', null, null, null, 37.7520673, 29.0918049),
  ('DNZ-33', 'Tiyatro', 'Pamukkale', true, false, true, 'Aktif', 'Adaptif-Koordine', 'Maestro', 'Fiber', null, null, 'Uzaktan açıp kapatma 1. port', 37.7509688, 29.0856814),
  ('DNZ-34', 'Dörtçeşme', 'Merkezefendi', true, false, true, 'Aktif', 'Adaptif', 'Maestro', 'Fiber', null, null, null, 37.7855250, 29.0831226),
  ('DNZ-35', 'Lise', 'Pamukkale', true, false, true, 'Aktif', 'Adaptif-Koordine', 'Maestro', 'Fiber', null, null, null, 37.7688617, 29.0839595),
  ('DNZ-36', 'Sırakapılar', 'Merkezefendi', true, false, true, 'Aktif', 'Adaptif-Koordine', 'Maestro', 'Fiber', null, null, null, 37.7782962, 29.0772700),
  ('DNZ-37', 'Lozan', 'Merkezefendi', true, false, true, 'Aktif', 'Adaptif-Koordine', 'Maestro', 'Fiber', null, null, null, 37.7759048, 29.0783912),
  ('DNZ-38', 'Ordu Evi', 'Pamukkale', true, false, true, 'Aktif', 'Adaptif-Koordine', 'Maestro', 'Fiber', '2018-05-30', null, null, 37.7681535, 29.0814328),
  ('DNZ-39', 'Kiremitçi', 'Merkezefendi', true, false, true, 'Aktif', 'Adaptif-Koordine', 'Maestro', 'Fiber', null, null, 'Uzaktan açıp kapatma 1. port', 37.7679203, 29.0789062),
  ('DNZ-40', 'Karaman', 'Merkezefendi', true, false, true, 'Aktif', 'Adaptif-Koordine', 'Maestro', 'Fiber', null, null, null, 37.7695211, 29.0751055),
  ('DNZ-41', 'Akkonak', 'Merkezefendi', true, false, true, 'Aktif', 'Adaptif-Koordine', 'Maestro', 'Fiber', null, null, null, 37.7726992, 29.0712404),
  ('DNZ-42', 'Berkay Halı Saha', 'Merkezefendi', true, false, true, 'Aktif', 'Adaptif-Koordine', 'Maestro', 'Fiber', null, null, null, 37.7742172, 29.0674210),
  ('DNZ-43', 'Kanal Kavşağı', 'Merkezefendi', true, false, true, 'Aktif', 'Adaptif-Koordine', 'Maestro', 'Fiber', null, null, null, 37.7698285, 29.0636659),
  ('DNZ-44', 'Askeri Yol', 'Merkezefendi', true, false, true, 'Aktif', 'Adaptif-Koordine', 'Maestro', 'Fiber', null, null, null, 37.7531065, 29.0557373),
  ('DNZ-45', 'Belkon', 'Merkezefendi', true, false, true, 'Aktif', 'Adaptif-Koordine', 'Maestro', 'Fiber', null, null, null, 37.7553883, 29.0494072),
  ('DNZ-46', 'Yenişehir', 'Merkezefendi', true, false, true, 'Aktif', 'Adaptif-Koordine', 'Maestro', 'Fiber', null, null, null, 37.7587431, 29.0428841),
  ('DNZ-47', 'Meclis Ormanı', 'Merkezefendi', true, false, true, 'Aktif', 'Adaptif-Koordine', 'Maestro', 'Fiber', null, null, null, 37.7651638, 29.0351111),
  ('DNZ-48', 'Şirinköy', 'Merkezefendi', true, false, true, 'Aktif', 'Adaptif-Koordine', 'Maestro', 'Fiber', null, null, null, 37.7692073, 29.0358514),
  ('DNZ-49', 'Gülistan', 'Merkezefendi', true, false, false, 'Aktif', 'Koordine', 'Maestro', 'Fiber', '2018-03-30', null, null, 37.7718681, 29.0358406),
  ('DNZ-50', 'Adliye Lojmanı', 'Merkezefendi', true, false, true, 'Aktif', 'Adaptif-Koordine', 'Maestro', 'Fiber', null, null, null, 37.7748151, 29.0357924),
  ('DNZ-51', 'Adliye', 'Merkezefendi', true, false, true, 'Aktif', 'Adaptif-Koordine', 'Maestro', 'Fiber', null, null, null, 37.7785930, 29.0357977),
  ('DNZ-52', 'Sirinköy Yol Ayrımı', 'Merkezefendi', true, false, false, 'Aktif', 'Koordine', 'Maestro', 'Fiber', null, null, null, 37.7770878, 29.0542942),
  ('DNZ-53', 'Albeni', 'Merkezefendi', true, false, true, 'Aktif', 'Adaptif-Koordine', 'Maestro', 'Fiber', null, null, null, 37.7788431, 29.0615791),
  ('DNZ-54', 'Esentepe', 'Merkezefendi', true, false, false, 'Aktif', 'Koordine', 'Maestro', 'Fiber', null, null, null, 37.7927107, 29.0694836),
  ('DNZ-55', 'Fabrikalar', 'Merkezefendi', true, false, true, 'Aktif', 'Adaptif-Koordine', 'Maestro', 'Fiber', null, null, null, 37.7959196, 29.0736705),
  ('DNZ-56', 'Bakırlı Köprü Altı', 'Merkezefendi', true, false, true, 'Aktif', 'Adaptif', 'Maestro', 'Fiber', null, null, null, 37.7980136, 29.0812826),
  ('DNZ-57', 'Levazım', 'Merkezefendi', true, false, true, 'Aktif', 'Adaptif', 'Maestro', 'Fiber', '2018-03-30', null, null, 37.7951057, 29.0873712),
  ('DNZ-58', 'Sümer Çal Caddesi', 'Merkezefendi', true, false, true, 'Aktif', 'Adaptif', 'Maestro', 'Fiber', null, null, '10.09.2026 Vtt looplar kesildi adaptif yazıldı', 37.7898958, 29.0926659),
  ('DNZ-59', 'Cinkaya Sağlık Ocağı', 'Merkezefendi', true, false, true, 'Aktif', 'Adaptif-Koordine', 'Maestro', 'Fiber', null, null, null, 37.8076903, 29.0576255),
  ('DNZ-60', 'Aydoğdu Cami', 'Pamukkale', true, false, false, 'Aktif', 'Adaptif', 'Maestro', 'Fiber', null, null, null, 37.7889420, 29.1139466),
  ('DNZ-61', 'Hal Kavşağı', 'Merkezefendi', true, false, true, 'Aktif', 'Adaptif-Koordine', 'Maestro', 'Fiber', '2018-03-30', null, null, 37.8234041, 29.0556514),
  ('DNZ-62', 'Akçeşme Kavşağı', 'Merkezefendi', true, false, true, 'Aktif', 'Adaptif-Koordine', 'Maestro', 'Fiber', null, null, null, 37.8108901, 29.0653771),
  ('DNZ-63', 'Sadıkoğlu Kavşağı', 'Merkezefendi', true, false, true, 'Aktif', 'Adaptif-Koordine', 'Maestro', 'Fiber', null, null, null, 37.8080166, 29.0694997),
  ('DNZ-64', 'Bakırlı Kavşağı', 'Merkezefendi', true, false, true, 'Aktif', 'Adaptif', 'Maestro', 'Fiber', null, null, null, 37.7966911, 29.0794694),
  ('DNZ-65', 'Sümer', 'Merkezefendi', true, false, true, 'Aktif', 'Adaptif-Koordine', 'Maestro', 'Fiber', '2020-05-30', null, 'Yapım tarihi Geometrik Düzenleme yapıldı.', 37.7881916, 29.0881383),
  ('DNZ-66', 'İstasyon', 'Pamukkale', true, false, true, 'Aktif', 'Adaptif-Koordine', 'Maestro', 'Fiber', null, null, null, 37.7867078, 29.0902841),
  ('DNZ-67', 'Üçgen', 'Pamukkale', true, false, true, 'Aktif', 'Adaptif', 'Maestro', 'Fiber', '2018-02-28', null, null, 37.7831296, 29.0954876),
  ('DNZ-68', 'Zeybek', 'Pamukkale', true, false, true, 'Aktif', 'Adaptif', 'Maestro', 'Fiber', null, null, null, 37.7907818, 29.1009539),
  ('DNZ-69', 'Akkale', 'Pamukkale', true, false, true, 'Aktif', 'Adaptif-Koordine', 'Maestro', 'Fiber', null, null, null, 37.8129667, 29.1472864),
  ('DNZ-70', 'Sevindik Köprülü Kav', 'Pamukkale', true, false, true, 'Aktif', 'Adaptif', 'Maestro', 'Fiber', null, null, null, 37.8024219, 29.1064095),
  ('DNZ-71', 'Güzelköy', 'Pamukkale', true, false, true, 'Aktif', 'Adaptif-Koordine', 'Maestro', 'Fiber', null, null, null, 37.8087276, 29.1681808),
  ('DNZ-72', 'Emniyet', 'Pamukkale', true, false, true, 'Aktif', 'Adaptif-Koordine', 'Maestro', 'Fiber', null, null, 'Uzaktan açıp kapatma 1. port', 37.7785336, 29.0979660),
  ('DNZ-73', 'Fatih', 'Pamukkale', true, false, true, 'Aktif', 'Adaptif-Koordine', 'Maestro', 'Fiber', null, null, null, 37.7640400, 29.1016674),
  ('DNZ-74', 'Ulus Kavşağı', 'Pamukkale', true, false, true, 'Aktif', 'Adaptif-Koordine', 'Maestro', 'Fiber', null, null, null, 37.7545019, 29.1056371),
  ('DNZ-75', 'Pekdemir Kavşağı', 'Pamukkale', true, false, true, 'Aktif', 'Adaptif-Koordine', 'Maestro', 'Fiber', null, null, null, 37.7483644, 29.1073000),
  ('DNZ-76', 'Kalp Merkezi', 'Pamukkale', true, false, true, 'Aktif', 'Adaptif-Koordine', 'Maestro', 'Fiber', null, null, null, 37.7424174, 29.1106260),
  ('DNZ-77', 'Yüksektepe', 'Pamukkale', true, false, true, 'Aktif', 'Adaptif-Koordine', 'Maestro', 'Fiber', null, null, null, 37.7357231, 29.1187692),
  ('DNZ-78', 'Bağbaşı', 'Pamukkale', true, false, true, 'Aktif', 'Adaptif-Koordine', 'Maestro', 'Fiber', null, null, null, 37.7324798, 29.1287443),
  ('DNZ-79', 'Kent Ormanı', 'Pamukkale', true, false, true, 'Aktif', 'Adaptif-Koordine', 'Maestro', 'Fiber', null, null, null, 37.7330186, 29.1357797),
  ('DNZ-80', 'Kelleci', 'Pamukkale', true, false, true, 'Aktif', 'Adaptif-Koordine', 'Maestro', 'Fiber', null, null, null, 37.7306618, 29.1553116),
  ('DNZ-81', 'Kasaplar', 'Pamukkale', true, false, true, 'Aktif', 'Adaptif-Koordine', 'Maestro', 'Fiber', null, null, null, 37.7526654, 29.1109103),
  ('DNZ-82', 'Emsan', 'Pamukkale', true, false, true, 'Aktif', 'Adaptif-Koordine', 'Maestro', 'Fiber', null, null, null, 37.7476094, 29.1151267),
  ('DNZ-83', 'Ticaret Borsası', 'Pamukkale', true, false, true, 'Aktif', 'Adaptif', 'Maestro', 'Fiber', null, null, null, 37.7410175, 29.1226637),
  ('DNZ-84', 'Hacıeyüplü', 'Merkezefendi', true, false, false, 'Aktif', 'Koordine', 'Maestro', 'Fiber', null, null, null, 37.8379835, 29.0417200),
  ('DNZ-85', 'Salihağa', 'Merkezefendi', true, false, false, 'Aktif', 'Koordine', 'Maestro', 'Fiber', null, null, null, 37.8340773, 29.0463817),
  ('DNZ-86', 'Anemon', 'Merkezefendi', true, false, false, 'Aktif', 'Koordine', 'Maestro', 'Fiber', null, null, null, 37.8291879, 29.0514940),
  ('DNZ-87', 'İnönü Çamlık', 'Pamukkale', true, false, true, 'Aktif', 'Adaptif-Koordine', 'Maestro', 'Fiber', null, null, null, 37.7591799, 29.0829778),
  ('DNZ-88', 'Emek Çamlık', 'Pamukkale', true, false, false, 'Aktif', 'Koordine', 'Maestro', 'Fiber', null, null, null, 37.7626745, 29.0819478),
  ('DNZ-89', 'Ertuğrul Gazi', 'Merkezefendi', true, false, false, 'Aktif', 'Koordine', 'Maestro', 'Fiber', null, null, null, 37.8060924, 29.0672010),
  ('DNZ-90', 'Hüdai Oral-5004 Sokak', 'Merkezefendi', true, false, false, 'Aktif', 'Koordine', 'Maestro', 'Fiber', null, null, null, 37.7984842, 29.0760469),
  ('DNZ-91', 'Esentepe Pazaryeri', 'Merkezefendi', true, false, false, 'Aktif', 'Koordine', 'Maestro', 'Fiber', null, null, null, 37.7901163, 29.0651894),
  ('DNZ-92', 'Esentepe Cami', 'Merkezefendi', true, false, false, 'Aktif', 'Koordine', 'Maestro', 'Fiber', null, null, null, 37.7877253, 29.0622872),
  ('DNZ-93', 'Aydem', 'Merkezefendi', true, false, true, 'Aktif', 'Adaptif-Koordine', 'Maestro', 'Fiber', null, null, null, 37.7782792, 29.0243125),
  ('DNZ-94', 'Servergazi Hastanesi', 'Merkezefendi', true, false, true, 'Aktif', 'Adaptif-Koordine', 'Maestro', 'Fiber', null, null, null, 37.7781308, 29.0211260),
  ('DNZ-95', 'Bahçeli Evler Orhan Gazi', 'Merkezefendi', true, false, false, 'Aktif', 'Off-Data', 'Maestro', 'Fiber', null, null, null, 37.7745140, 29.0251386),
  ('DNZ-96', 'Yunus Emre Cad', 'Pamukkale', true, false, false, 'Aktif', 'Koordine', 'Maestro', 'Fiber', null, null, null, 37.7475030, 29.1039910),
  ('DNZ-97', 'Çevik Kuvvet', 'Merkezefendi', true, false, false, 'Aktif', 'Off-Data', 'Maestro', 'Fiber', '2019-01-30', null, null, 37.7677422, 29.0288562),
  ('DNZ-98', 'Gerzele', 'Merkezefendi', true, false, false, 'Aktif', 'Koordine', 'Maestro', 'Fiber', '2021-03-30', null, 'Uzaktan açıp kapatma 5. port', 37.7511978, 29.0542191),
  ('DNZ-99', 'Müftü Ahmet Hulusi', 'Pamukkale', true, false, false, 'Aktif', 'Off-Data', 'Maestro', 'Fiber', null, null, null, 37.7773634, 29.0915394),
  ('DNZ-100', 'Vilayet', 'Pamukkale', true, false, false, 'Aktif', 'Off-Data', 'Maestro', 'Fiber', null, null, null, 37.7806197, 29.0844315),
  ('DNZ-101', 'Maliye', 'Pamukkale', true, false, false, 'Aktif', 'Off-Data', 'Maestro', 'Fiber', null, null, null, 37.7791569, 29.0848982),
  ('DNZ-102', 'Tanca', 'Merkezefendi', true, false, false, 'Aktif', 'Koordine', 'Maestro', 'Fiber', '2018-09-30', null, 'Uzaktan açıp kapatma 5. port', 37.7684418, 29.0626198),
  ('DNZ-103', 'Şht Öğretmen Yusuf Batur', 'Merkezefendi', true, false, false, 'Aktif', 'Off-Data', 'Maestro', 'Fiber', '2018-11-30', null, 'Uzaktan açıp kapatma 1. port', 37.7658339, 29.0452820),
  ('DNZ-104', '1.Sanayi Köprüsü', 'Merkezefendi', true, false, false, 'Aktif', 'Koordine', 'Maestro', 'Fiber', '2018-04-30', null, null, 37.7902180, 29.0823340),
  ('DNZ-105', 'Tekden', 'Merkezefendi', true, false, false, 'Aktif', 'Koordine', 'Maestro', 'Fiber', '2018-07-30', null, null, 37.7803313, 29.0678394),
  ('DNZ-106', '29 Ekim Hüdai Oral', 'Merkezefendi', true, false, false, 'Aktif', 'Koordine', 'Maestro', 'Fiber', '2018-09-25', null, null, 37.7781139, 29.0498525),
  ('DNZ-107', '29 Ekim Gülistan', 'Merkezefendi', true, false, false, 'Aktif', 'Koordine', 'Maestro', 'Fiber', '2021-07-30', null, null, 37.7776517, 29.0473580),
  ('DNZ-108', '29 Ekim Bahçeli Evler', 'Merkezefendi', true, false, false, 'Aktif', 'Koordine', 'Maestro', 'Fiber', '2021-05-30', null, null, 37.7777238, 29.0429109),
  ('DNZ-109', 'Hallaçlar', 'Merkezefendi', true, false, false, 'Aktif', 'Koordine', 'Maestro', 'Fiber', null, null, null, 37.7828116, 29.0124464),
  ('DNZ-110', 'Üçler ASM', 'Merkezefendi', true, false, false, 'Aktif', 'Koordine', 'Maestro', 'Fiber', '2020-12-30', null, null, 37.7901120, 29.0154988),
  ('DNZ-111', 'Çakmak', 'Merkezefendi', true, false, false, 'Aktif', 'Koordine', 'Maestro', 'Fiber', '2019-11-30', null, null, 37.7941138, 29.0174299),
  ('DNZ-112', 'Hilal Cami', 'Merkezefendi', true, false, false, 'Aktif', 'Koordine', 'Maestro', 'Fiber', null, null, null, 37.7989292, 29.0200853),
  ('DNZ-113', 'Üçler', 'Merkezefendi', true, false, true, 'Aktif', 'Adaptif-Koordine', 'Maestro', 'Fiber', '2018-12-30', null, null, 37.8039139, 29.0234220),
  ('DNZ-114', 'Üçler 1147 Sk', 'Merkezefendi', true, false, true, 'Aktif', 'Adaptif-Koordine', 'Maestro', 'Fiber', '2018-06-30', null, null, 37.8055160, 29.0283358),
  ('DNZ-115', 'Üçler 1186 Sk', 'Merkezefendi', true, false, true, 'Aktif', 'Adaptif-Koordine', 'Maestro', 'Fiber', '2018-08-25', null, null, 37.8121319, 29.0368170),
  ('DNZ-116', 'TOKİ', 'Merkezefendi', true, false, true, 'Aktif', 'Adaptif-Koordine', 'Maestro', 'Fiber', null, null, null, 37.8215651, 29.0507805),
  ('DNZ-117', 'Yeni Şafak 1029 (Ferhatlar)', 'Merkezefendi', true, false, false, 'Aktif', 'Koordine', 'Maestro', 'Fiber', '2018-02-28', null, null, 37.8006205, 29.0312219),
  ('DNZ-118', 'Yeni Şafak 1048 Sk', 'Merkezefendi', true, false, false, 'Aktif', 'Koordine', 'Maestro', 'Fiber', '2018-03-30', null, null, 37.7963393, 29.0329814),
  ('DNZ-119', 'Huzurbul', 'Merkezefendi', true, false, false, 'Aktif', 'Koordine', 'Maestro', 'Fiber', null, null, null, 37.7921850, 29.0359855),
  ('DNZ-120', 'Cinkaya', 'Merkezefendi', true, false, false, 'Aktif', 'Koordine', 'Maestro', 'Fiber', null, null, 'Uzaktan açıp kapatma 2. port', 37.7970514, 29.0430290),
  ('DNZ-121', 'ÜÇLER-112 İstasyon', 'Merkezefendi', true, false, false, 'Aktif', 'Koordine', 'Maestro', 'Fiber', '2022-03-30', null, null, 37.8086566, 29.0276384),
  ('DNZ-122', 'AKÇA2052sk', 'Merkezefendi', true, false, false, 'Aktif', 'Koordine', 'Maestro', 'Anten', '2022-03-25', null, null, 37.8102247, 29.0285289),
  ('DNZ-123', 'Neşet Çakır', 'Merkezefendi', true, false, false, 'Aktif', 'Koordine', 'Maestro', 'Fiber', null, null, null, 37.8098306, 29.0625715),
  ('DNZ-124', 'Hakkı Dereköylü', 'Merkezefendi', true, false, false, 'Pasif', 'Flaş', 'Maestro', 'Fiber', null, null, 'Kavşak kaldırıldı', 37.7487123, 29.0546000),
  ('DNZ-125', 'Hayme Hatun', 'Merkezefendi', true, false, false, 'Aktif', 'Flaş', 'Maestro', 'Fiber', null, null, null, 37.7800049, 29.0824038),
  ('DNZ-126', 'Ulaşım Aş', 'Merkezefendi', true, false, false, 'Aktif', 'Off-Data', 'Maestro', 'Fiber', null, null, null, 37.7929900, 29.0969210),
  ('DNZ-128', 'Kalkanlar', 'Merkezefendi', true, false, false, 'Aktif', 'Koordine', 'Maestro', 'Fiber', null, null, null, 37.7994930, 29.0964425),
  ('DNZ-129', 'Abalıoğlu Yem', 'Merkezefendi', true, false, false, 'Aktif', 'Off-Data', 'Maestro', 'Fiber', '2018-01-31', null, null, 37.8182555, 29.1149336),
  ('DNZ-130', 'MYO', 'Merkezefendi', true, false, false, 'Aktif', 'Off-Data', 'Maestro', 'Fiber', null, null, null, 37.7483220, 29.0895385),
  ('DNZ-131', 'İlbade Mezarlık', 'Merkezefendi', true, false, false, 'Aktif', 'Off-Data', 'Maestro', 'Fiber', null, null, null, 37.7884714, 29.0767604),
  ('DNZ-132', '1200 Evler Minibüs Durağı', 'Merkezefendi', true, false, false, 'Aktif', 'Off-Data', 'Maestro', 'Fiber', '2018-10-30', null, null, 37.7865721, 29.0069532),
  ('DNZ-133', 'Yeni Cadde', 'Merkezefendi', true, false, false, 'Aktif', 'Adaptif-Koordine', 'Maestro', 'Fiber', '2023-07-30', null, null, 37.7875472, 29.0796304),
  ('DNZ-134', 'Şht. J. Çvş. Eyüp Kepenek', 'Merkezefendi', true, false, false, 'Aktif', 'Off-Data', 'Maestro', 'Anten', '2023-10-30', null, null, 37.7654352, 29.1041404),
  ('DNZ-135', 'Dörtyol Camii', 'Merkezefendi', true, false, false, 'Aktif', 'Off-Data', 'Maestro', 'Anten', null, null, null, 37.7709692, 29.0698349),
  ('DNZ-150', 'Üniversite Yaya', 'Pamukkale', true, true, false, 'Aktif', 'Off-Data', 'Maestro', 'Anten', null, null, null, 37.7430197, 29.1077775),
  ('DNZ-136', 'Dimbilli', 'Pamukkale', true, false, false, 'Aktif', 'Off-Data', 'Maestro', 'Mobil Hat', null, null, null, 37.7595192, 29.1137695),
  ('DNZ-140', 'Honaz-Emlak', 'Honaz', true, false, false, 'Aktif', 'Off-Data', 'Maestro', 'Mobil Hat', null, null, null, 37.7602148, 29.2740959),
  ('DNZ-137', 'Mustafa Alper Cad', 'Merkezefendi', true, false, false, 'Aktif', 'Off-Data', 'Maestro', 'Anten', '2024-07-09', null, null, 37.7500017, 29.0889430),
  ('DNZ-138', 'Necdet Adalı', 'Pamukkale', true, false, false, 'Aktif', 'Off-Data', 'Maestro', 'Anten', '2025-09-08', null, null, 37.7616864, 29.0993232),
  ('DNZ-141', 'ÇİĞDEM', 'Merkezefendi', true, false, false, 'Aktif', 'Off-Data', 'Maestro', 'Mobil Hat', '2026-04-09', '700877073', null, 37.8006374, 29.0651464),
  ('DNZ-143', 'Cinkaya 3131 Sk', 'Merkezefendi', true, false, false, 'Aktif', 'Koordine', 'Maestro', 'Fiber', '2026-07-14', null, null, 37.8014343, 29.0476638),
  ('DNZ-144', 'Bilim Kampüs', 'Merkezefendi', true, false, false, 'Aktif', 'Koordine', 'Maestro', 'Anten', '2026-07-27', null, null, 37.7664827, 29.0401214)
on conflict (kod) do update set
  ad = excluded.ad,
  bolge = excluded.bolge,
  tkm_bagli = excluded.tkm_bagli,
  butonlu = excluded.butonlu,
  adaptif = excluded.adaptif,
  durum = excluded.durum,
  mod = excluded.mod,
  model = excluded.model,
  iletisim = excluded.iletisim,
  yapim_tarihi = excluded.yapim_tarihi,
  elektrik_abone_no = excluded.elektrik_abone_no,
  aciklama = excluded.aciklama,
  latitude = excluded.latitude,
  longitude = excluded.longitude;

-- Kontrol: 142 satır, 87 Merkezefendi + 54 Pamukkale + 1 Honaz beklenir
select bolge, count(*) from public.kavsaklar group by bolge order by 2 desc;


-- ============ BÖLÜM 3: ADM PLANLI ÇALIŞMALAR ============
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
