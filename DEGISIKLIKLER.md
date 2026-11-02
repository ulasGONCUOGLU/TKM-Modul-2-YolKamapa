# PythonYolKapama — Neler değişti?

> **Hangi dal?** Bu ZIP, deponuzun **`NuxtBag`** dalının (commit `2cf3519`, "YolKapama Projesi ile birleşti artık. Flask kullanıyoruz")
> güncellenmiş hâlidir. Depoda iki dal var: `main` (eski SQLite sürümü) ve `NuxtBag` (Flask + Supabase, web arayüzüyle çalışan güncel sürüm).
> İlk başta yalnızca `main`'i incelemiştim, sonra `NuxtBag`'e geçip değişiklikleri oraya taşıdım.

`.git`, `.venv`, `.env`, `output/` ve `__pycache__` ZIP'te yoktur.

## Nasıl kontrol edersiniz?

1. ZIP'i **ayrı bir klasöre** çıkarın (kendi projenizin üzerine değil).
2. VS Code'da iki klasörü karşılaştırın: kendi klasörünüze sağ tık → "Select for Compare",
   ZIP'ten çıkan klasöre sağ tık → "Compare with Selected". Ya da WinMerge/Meld kullanın.
3. Beğendiğiniz dosyaları tek tek kendi projenize kopyalayın.

**Silinecek dosya yok.** Bu ZIP'te boşaltılmış dosya bulunmaz.

## Değişen dosyalar (11)

| Dosya | Değişiklik | Neden |
| --- | --- | --- |
| `supabase_schema_update.sql` | Yeniden yazıldı | `refresh_active_status()` fonksiyonuna `where true` eklendi (Supabase WHERE'siz UPDATE'i `21000` ile reddediyor). **En önemlisi.** Eski dosya `closures` tablosunu `DROP` ediyordu (tekrar çalıştırınca tüm veri silinir) ve `closures`, `settings`, `sync_runs` tablolarına **herkese** ekleme/güncelleme/silme izni veriyordu. Tarayıcıdaki anon anahtar herkese göründüğü için, internete erişimi olan herkes tabloları silebilir ya da AI talimatını okuyup değiştirebilirdi. Yeni dosya veri silmez, tekrar çalıştırılabilir; tarayıcı yalnızca `closures` ve `sync_runs`'ı okuyabilir, `settings`'e hiç erişemez. Yazma işlemlerini Flask ve Nuxt sunucusu `service_role` anahtarıyla yapar (zaten RLS'i atlar). |
| `konum.py` | Değişti | **Çok parçalı duyurular:** `konum_bul_parcalar()` her parçayı ayrı konumlandırır; birinin bulunamaması diğerlerini etkilemez. `road_segment` için yolun çizgisi iki kesişen sokak arasında **kesilir** (kesişimden kesişime gerçek yol dilimi); olmazsa eski yönteme düşer. Her parçaya kesinlik etiketi: kesin / yaklaşık / bulunamadı. Rakamlı sokaklar "3131 Sokak" ve "3131. Sokak" biçimlerinde aranır. "Bağlanan sokaklar" için etkilenen bölge dairesi (üst üste binenler birleştirilir). Ayrıca: (1) Nominatim yanıtları için yerel önbellek: bulunamayan kayıtlar artık her çalıştırmada aynı istekleri tekrarlamıyor. (2) **Hata düzeltmesi:** büyük harfli Türkçe mahalle adları (`ŞİRİNKÖY`) OSM'deki `Şirinköy` ile eşleşmiyordu, doğru sonuçlar reddediliyordu. (3) `sokağı`/`bulvarı` ek kelimeleri hiç eşleşmiyordu. (4) Yer tutucu `ornek@eposta.com` User-Agent'ı yerine `GEOCODER_CONTACT`. (5) `NOMINATIM_URL` ve `OSRM_URL` ile kendi sunucunuza geçebilirsiniz. |
| `app.py` | Değişti | (1) Supabase'e ulaşılamadığında çalışma kilidi açılmıyordu; servis yeniden başlatılana kadar her istek "zaten çalışıyor" (409) dönüyordu. (2) Token karşılaştırması sabit süreli. (3) `debug=True` artık varsayılan değil (Werkzeug hata ayıklayıcısı sunucuda kod çalıştırmaya izin verir); geliştirirken `FLASK_DEBUG=1 python app.py`. |
| `db_supabase.py` | Değişti | Parça listesi eklenmeden önce analiz edilmiş aktif kayıtlar **bir kez yeniden analiz edilir**; bazı parçaları bulunamayan kayıtların konumu her çalıştırmada yeniden denenir. |
| `main.py` | Değişti | **Tablo bulunamadı hatası:** eski `id=15420` sayfası kaldırılmış; güncel adres `https://www.denizli.bel.tr/hizli-erisim/trafige-kapali-yollar` (`.env` içindeki `URL`). Başlık eşleştirmesi Türkçe İ/I farkına karşı sağlamlaştırıldı, eski adres için açıklayıcı hata mesajı eklendi. Ayrıca kaynak siteyi geçici hatalarda (429/5xx) yeniden deneyen, botu tanıtan `User-Agent`'lı oturum. |
| `AI_düzenleme.py` | Değişti | **Çok parçalı duyurular:** AI artık tek konum yerine `segments[]` (parça listesi) döndürür; her parçada yol adı, başlangıç/bitiş kesişen sokaklar ve "bağlanan sokaklar" bayrağı vardır. Prompt yeniden yazıldı (rakamlı sokakları "3131 Sokak" biçiminde yazma, çekim eklerini atma, parçaları birleştirmeme). Eski alanlar (`location_type`, `query`...) ilk parçadan türetilir. Ayrıca model adı `.env` içindeki `GEMINI_MODEL` ile değiştirilebilir (bkz. aşağıdaki soru). |
| `.env.example` | **YENİ** | README'de anılıyordu ama depoda yoktu. |
| `README.md` | Değişti | Güncel akışa göre. |

## ADM (Aydem) planlı elektrik çalışmaları (yeni)

| Dosya | Değişiklik | Neden |
| --- | --- | --- |
| `adm.py` | Yeni | ADM'den Merkezefendi/Pamukkale çalışmalarını çeker, doğrular, alanı çözer, 150 m içindeki sinyalize kavşakları bulur, kaydeder. Hata olursa Türkçe mesaj yazar, eski veriye dokunmaz. |
| `supabase_adm_calismalari.sql` | Yeni | `adm_calismalari` ve `adm_sync_durumu` tabloları (SQL Editor'de bir kez çalıştırın). |
| `main.py` | Değişti | Yol kapama adımından sonra ADM adımı; biri hata verse diğeri durmaz. |
| `db_supabase.py` | Değişti | `get_kavsaklar`, `get_adm_mevcut`, `save_adm_calismalari`, `save_adm_sync_durumu`. |
| `requirements.txt` | Değişti | `shapely`, `numpy` eklendi (`pip install -r requirements.txt`). |
| `.env.example`, `README.md` | Değişti | `ADM_*` ayarları ve açıklama. |

Test paketine `tests/test_adm.py` (53 test; toplam 132, hepsi geçti) ve `adm_dene.py` (veritabanına yazmadan gerçek ADM'yi dener) eklendi.

## Çok parçalı duyurular nasıl çalışır?

1. AI metni parçalara ayırır (`segments[]`). Her parça: yol adı + başlangıç/bitiş kesişen sokaklar.
2. Her parça ayrı konumlandırılır. `kesin`: kesişimden kesişime yol dilimi bulundu. `yaklaşık`: yalnızca yolun tamamı ya da şüpheli eşleşme. `bulunamadı`: web arayüzü bunu mahalle düzeyinde gösterir.
3. Sonuç tek kayıtta saklanır: `geometry` (birden çok parça varsa GeometryCollection) ve `geocode_result.parcalar` (her parçanın kesinliği).
4. İlk çalıştırmada, eski biçimle analiz edilmiş aktif kayıtlar bir kez yeniden analiz edilir (Gemini çağrısı kayıt başına 1).

**Denemek için:** Testler ve deneme betiği bu projede YOKTUR; ayrı `PythonYolKapama-Test.zip` paketindedir (içindeki README'ye bakın).

**Sınırlar:** Kesişim, Nominatim'in döndürdüğü yol çizgilerinden hesaplanır; çizgi eksik ya da kesişen sokak 60 m'den uzaktaysa (`KESISIM_ESIK_M`) kayıt yaklaşık sayılır. Overpass kullanılmadı.

## Gemini modelini yükseltmek

Model adı yalnızca bir metindir. **API anahtarı modelden bağımsızdır, anahtarı aldığınız siteye gitmenize gerek yok.**
`.env` dosyanıza şunu yazıp programı yeniden başlatmanız yeter:

```
GEMINI_MODEL=gemini-3.8-flash
```

- Boş bırakırsanız mevcut model `gemini-3.6-flash` kullanılır (Google'ın listesinde kararlı, kapatma tarihi yok).
- Google'ın güncel listesinde: `gemini-4-argon`, `gemini-3.8-flash`, `gemini-3.6-flash`, `gemini-3.5-flash-lite`, `gemini-3.1-flash-lite`.
- Değiştirdikten sonra **birkaç gerçek kayıtla** sonucu kontrol edin (konum tipi, mahalle/ilçe). Modeli ben gerçek anahtarla deneyemedim.
- Not: Bu iş basit bir yapılandırılmış metin çıkarma işi; en büyük model genelde gereksizdir ve daha pahalıdır.

## Çalıştırma ve test

```powershell
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
copy .env.example .env      # ardından doldurun
python app.py
```

## Supabase şemasını uygulamadan önce

`supabase_schema_update.sql` dosyasını Supabase Dashboard → SQL Editor'de **bir kez** çalıştırın. Mevcut verinizi silmez.
Başka bir şeyin (ör. Nuxt'un anon anahtarıyla `settings`'e yazan eski bir kod) bu açık izinlere dayanıp dayanmadığını bilmiyorum; web deposundaki sürümde dayanmıyor, `settings` sunucu üzerinden okunup yazılıyor.

## Doğruladıklarım

- 132 birim testin hepsi geçti (ayrı test paketinden, temiz bir sanal ortamda).
- Şema, **gerçek Postgres 16'da** denendi: temiz kurulum, ikinci çalıştırma ve eski şemadan geçiş (veri korundu). Supabase'in varsayılan yetkileri taklit edilerek anon rolünün yalnızca okuyabildiği, sunucu rolünün (service_role) her şeyi yapabildiği ölçüldü.

## Yapmadıklarım

- Gerçek Supabase, Gemini veya Nominatim ile denemedim (anahtarlarınız bende yok).
- Git geçmişinizi değiştirmedim.
- ADM kodunu gerçek ADM sitesine karşı çalıştıramadım (sandbox internetsiz); ADM'nin bot koruması Python isteğini engelleyebilir.
