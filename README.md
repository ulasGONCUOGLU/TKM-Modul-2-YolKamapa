# Yol Kapama — Backend (Python + Flask + Supabase)

Belediyelerin yol kapama duyurularını çeken, Gemini ile analiz eden,
Nominatim/OSRM ile konumlandıran ve **Supabase**'e yazan pipeline.

## Bu sürümde neler değişti

Önceki sürüme göre (bkz. eski `README.md`, SQLite tabanlıydı):

- **SQLite kaldırıldı**, yerine **Supabase** (Postgres) geldi. Kalıcılık
  katmanı artık `db_supabase.py` içinde.
- **Harita üretimi kaldırıldı** (`folium`, `build_map()`). Harita artık
  Nuxt tarafında Leaflet ile, doğrudan Supabase'ten okunan `closures`
  verisiyle çiziliyor. Python'un harita dosyası üretmesine gerek kalmadı.
- **Flask eklendi** (`app.py`): pipeline'ı bir HTTP isteğiyle tetiklemek
  için ince bir sarmalayıcı. Nuxt'taki "Verileri Çek" butonu bunu çağırır.
- **Sistem Talimatı artık Supabase'ten okunuyor**: `AI_düzenleme.py`,
  Gemini prompt'unun giriş cümlesini `settings` tablosundan (`system_prompt`
  anahtarı) okuyor. Nuxt'taki Ayarlar sayfasından değiştirilebilir.
- `main.py`'nin giriş fonksiyonu `main()` yerine `run_pipeline()` oldu
  (Flask'ın import edip çağırabilmesi için). Script tek başına da
  çalıştırılabilir: `python main.py`.

## Kurulum

```powershell
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

`.env.example` dosyasını `.env` olarak kopyalayıp doldurun:

```powershell
copy .env.example .env
```

Doldurman gereken alanlar: `URL`, `GEMINI_API_KEY`, `SUPABASE_URL`,
`SUPABASE_SERVICE_ROLE_KEY`, `INTERNAL_API_TOKEN`. Önerilen: `GEOCODER_CONTACT`
(OpenStreetMap Nominatim politikası, uygulamayı tanıtan bir `User-Agent` ister;
iletişim e-postanızı yazın). Diğer isteğe bağlı ayarlar `.env.example` içinde
açıklanmıştır.

> `.env` dosyası `.gitignore` içindedir; anahtarlarınızı asla depoya yüklemeyin.

### Gemini modelini değiştirmek

Model adı yalnızca bir metindir; **API anahtarı modelden bağımsızdır**, anahtarı
aldığınız siteye gitmenize gerek yok. `.env` içine yazın ve yeniden başlatın:

```
GEMINI_MODEL=gemini-3.8-flash
```

Boş bırakırsanız `gemini-3.6-flash` kullanılır. Geçerli model ID'leri ve kapatma
tarihleri: <https://ai.google.dev/gemini-api/docs/models> ve
<https://ai.google.dev/gemini-api/docs/deprecations>. Modeli değiştirince birkaç
gerçek kayıtla sonucu kontrol edin (konum tipi, mahalle/ilçe doğruluğu).

## Supabase şeması

`supabase_schema_update.sql` dosyasını Supabase Dashboard → SQL Editor'e yapıştırıp çalıştırın. Dosya **tekrar çalıştırılabilir**; hiçbir tabloyu silmez, mevcut veriyi korur. Şunları kurar: `closures`, `sync_runs` ve `settings` tabloları, `refresh_active_status()` fonksiyonu ve güvenli RLS politikaları (`system_prompt` satırı dahil).

> **Güvenlik notu:** Eski sürümdeki şema dosyası bu üç tabloya `TO PUBLIC` INSERT/UPDATE/DELETE izni veriyordu. Tarayıcıdaki anon anahtar herkese göründüğü için bu, erişimi olan herkesin tabloları silebilmesi/değiştirebilmesi ve `settings` içindeki AI talimatını okuyup yazabilmesi demekti. Yeni dosya bu politikaları siler: tarayıcı yalnızca `closures` ve `sync_runs`'ı **okuyabilir**; yazma işlemlerini Flask ve Nuxt sunucusu `service_role` anahtarıyla yapar (RLS'i zaten atlar). Mevcut veritabanınızda bu dosyayı **bir kez** çalıştırın.

## Çalıştırma

**Doğrudan (Flask'sız, hızlı test için):**

```powershell
python main.py
```

**Flask üzerinden (Nuxt'un kullanacağı şekil):**

```powershell
python app.py
```

Bu, `http://localhost:5000` üzerinde ayağa kalkar. Test etmek için:

```powershell
curl -X POST http://localhost:5000/run -H "X-Internal-Token: .env'deki INTERNAL_API_TOKEN"
```

Hemen `{"status": "started", "run_id": ...}` döner, pipeline arka planda
çalışmaya devam eder. İlerlemeyi Supabase Dashboard'da `sync_runs`
tablosundan izleyebilirsiniz.

## Üretim / Docker notu

İleride Docker'a taşınırsa:

- `gunicorn` zaten `requirements.txt`'te var. Container **içinde**
  Docker'ın kendi ağ izolasyonuyla çalıştığı için o zaman `0.0.0.0`
  container içi ağ arayüzünü ifade eder (host makineye/dış ağa
  otomatik açılmaz, hangi portun dışarı açılacağını `docker run -p`
  veya `docker-compose`'daki `ports:` ayarı belirler):
  ```
  gunicorn --workers 1 --bind 0.0.0.0:5000 app:app
  ```
  **Şu an, yerel makinende doğrudan `python app.py` ile çalıştırdığın
  sürece bu geçerli değil** — `app.py` içinde `host="127.0.0.1"`
  olduğu için servis sadece kendi bilgisayarından erişilebilir, ağdaki
  hiçbir cihaza (ve dolayısıyla ağ üzerindeki port çakışması riskine)
  açılmaz.
- **`--workers 1` şart** — `/run` endpoint'indeki "aynı anda ikinci
  çalıştırmayı engelle" kilidi process-içi bir Python kilidi
  (`threading.Lock`), birden fazla worker arasında paylaşılmaz. Birden
  fazla worker gerekiyorsa bu kilidi Postgres advisory lock'a taşımak
  gerekir — şimdilik gerek yok.
- `URL`, `GEMINI_API_KEY`, `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`,
  `INTERNAL_API_TOKEN` container'a ortam değişkeni olarak geçilmeli
  (`.env` dosyasını image'a gömmeyin).

## ADM (Aydem) planlı elektrik çalışmaları

"Verileri Çek" akışının sonunda (`main.py` → `_adm_adimi()`) `adm.py` çalışır:

1. ADM'nin `test-outages` adresinden Merkezefendi ve Pamukkale çalışmalarını çeker (Honaz yok).
2. Yanıtı sıkı doğrular (zarf, zorunlu alanlar, ilçe eşleşmesi); geçici sorunlarda 3 kez dener.
3. Alan çizimini (poligon/çizgi/nokta) koordinata çevirir; `kavsaklar` tablosundaki sinyalize kavşaklarla kesişimi
   ("içinde" ya da en çok 150 m "yakın") hesaplar. Şehir dışındaki kayıtlar yalnızca tabloda kalır.
4. `adm_calismalari` tablosuna yazar; ADM listesinden kalkanlar `kaynaktan_kalkti=true` olur, saat değişiklikleri `onceki_plan`da tutulur.
5. Sonucu `adm_sync_durumu` (tek satır) tablosuna yazar. ADM tarafında beklenmedik bir sorun olursa (engel, bozuk/başka biçimde yanıt)
   **mevcut veriye dokunulmaz**, Türkçe açıklayıcı hata mesajı kaydedilir ve sitede gösterilir.
   ADM adımı hata verse bile yol kapama akışı kesilmez; tersine yol kapama hatası ADM'den sonra yine bildirilir.

Kurulum: `supabase_adm_calismalari.sql` dosyasını SQL Editor'de çalıştırın (web deposundaki `supabase/adm_calismalari.sql` ile aynıdır; önce web deposundaki `kavsaklar.sql` çalışmış olmalı),
sonra `pip install -r requirements.txt` (shapely ve numpy eklendi). Ayarlar `.env.example` içindeki `ADM_*` değişkenleridir.
Riskler: ADM bu adresi değiştirebilir; ADM'nin bot koruması (F5) Python isteğini engelleyebilir (hata mesajı bunu söyler, `ADM_USER_AGENT` denenebilir).

## Nominatim / OSM kullanım kuralları

Genel `nominatim.openstreetmap.org` sunucusu ücretsizdir ama kuralları vardır ([politika](https://operations.osmfoundation.org/policies/nominatim/)):

- Saniyede en fazla 1 istek; düzenli/zamanlanmış çalışan betikler için sınır daha da düşüktür.
- Tanıtıcı `User-Agent` zorunludur (`GEOCODER_CONTACT`).
- Sonuçlar önbelleğe alınmalıdır. `konum.py` bunu otomatik yapar (`output/geocode_cache.sqlite`): aynı sorgu tekrar sunucuya gitmez, bulunamayan sorgular da 3 gün hatırlanır (`GEOCODE_CACHE_*` ayarları).

Sık çalıştırıyorsanız veya çok kayıt işliyorsanız kendi Nominatim (ve OSRM) sunucunuza geçin; adreslerini `NOMINATIM_URL` ve `OSRM_URL` ile verin. Genel OSRM adresi (`router.project-osrm.org`) bir demo sunucudur; üretim için önerilmez.

## Dosyalar

```
main.py                      Ana pipeline (scrape -> Supabase -> AI -> konum)
AI_düzenleme.py               Gemini ile güzergah analizi (system_prompt Supabase'ten)
konum.py                      Nominatim/OSRM geocoding (önbellekli, Türkçe harf duyarlı)
db_supabase.py                Supabase kalıcılık katmanı (SQLite'ın yerini alıyor)
app.py                        Flask sarmalayıcısı (POST /run, GET /health)
adm.py                        ADM planlı elektrik çalışmaları (çek, doğrula, kavşakla kesiştir, kaydet)
supabase_adm_calismalari.sql  adm_calismalari + adm_sync_durumu tabloları
supabase_schema_update.sql    Tüm tabloların güvenli, tekrar çalıştırılabilir şeması + RPC
requirements.txt
.env.example
```
