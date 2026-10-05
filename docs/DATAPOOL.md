# Veri Havuzu (Data Pool) — OneDrive proje arşivi için yerel indeks

Veri havuzu, OneDrive'daki (veya herhangi bir klasördeki) proje arşivini **tamamen kendi
bilgisayarınızda** tarar. İçerikleri tek bir SQLite veritabanında toplar. Bulut tarafına
dosya gönderilmez. İndekslemeyi yerel işçi süreçler yapar. Dosyaları inceleyip özetleyen yerel
AI ajanları da (Claude Desktop, Claude Code vb.) MCP araçlarıyla bu havuzu okur ve yorumlarını
yine havuza yazar.

```
OneDrive klasörü ──► win32-mcp-datapool index (paralel işçiler)
                          │  DWG/DXF: katmanlar, bloklar, antet (ADA, PAFTA, ÖLÇEK…), yazılar
                          │  PDF / DOCX / PPTX / XLSX / CSV / TXT / NCN / KML: metin
                          │  Otomatik sınıf: proje | etut_plani | teklif_sunumu | veri | diger
                          ▼
                 %LOCALAPPDATA%\win32-mcp\datapool.sqlite  (FTS5 tam metin indeksi)
                          ▲
        Yerel ajanlar ────┘  datapool_search / datapool_get / datapool_annotate …
```

## 1. Kurulum (Windows)

### Hızlı kurulum (tek komut)

`scripts/datapool-kurulum.ps1` betiği aşağıdaki adımların hepsini sırayla yapar:

1. Paketi kurar.
2. OneDrive klasörünüzü bulur ve `WIN32_MCP_DATAPOOL_ROOTS` değişkenine yazar.
3. ODA File Converter'ın kurulu olup olmadığını kontrol eder.
4. Claude Desktop ayarına `win32` sunucusunu ekler. Mevcut ayarı önce yedekler ve diğer sunuculara dokunmaz.
5. İlk taramayı çalıştırır.
6. Her gece 02:00'de çalışacak bir "VeriHavuzu" görevi oluşturur.

Yönetici yetkisi gerekmez.

```powershell
powershell -ExecutionPolicy Bypass -File .\datapool-kurulum.ps1
# Belirli klasörler, 6 işçi, yalnızca bulutta duran dosyaları da indir:
powershell -ExecutionPolicy Bypass -File .\datapool-kurulum.ps1 -Roots "C:\Users\ben\OneDrive - Firma\Projeler" -Workers 6 -Hydrate
```

Betiği tekrar çalıştırmak güvenlidir, çünkü tarama artımlıdır. ODA'yı sonradan kurarsanız betiği
`-SkipInstall` ile tekrar çalıştırın. Aşağıdaki bölümler aynı adımların elle nasıl yapılacağını anlatır.

### Elle kurulum

```powershell
pip install "win32-mcp-server[datapool] @ git+https://github.com/mustafadiscii-lang/win32-mcp-server.git"
```

**DWG içeriği için (önerilir):** Ücretsiz [ODA File Converter](https://www.opendesign.com/guestfiles/oda_file_converter)
programını kurun. Varsayılan kurulum klasörü (`C:\Program Files\ODA\ODAFileConverter*`) otomatik
bulunur. Program başka bir yerdeyse yolunu verin:

```powershell
setx WIN32_MCP_ODA_CONVERTER "D:\Araclar\ODAFileConverter\ODAFileConverter.exe"
```

ODA yoksa DWG dosyaları yine indekslenir, ancak yalnızca sürüm (AutoCAD 2018 vb.), boyut,
tarih ve klasör/dosya adı bilgisiyle. Bu dosyaların durumu `partial` olur. ODA'yı sonradan
kurarsanız sonraki taramada bu dosyalar kendiliğinden yeniden işlenir. Başka bir nedenle yarım
kalan dosyalar (kilitli dosya, ODA zaman aşımı, indirilemeyen OneDrive dosyası) 24 saatte bir
yeniden denenir; arada yapılan taramalar onları atlar.

DWG'ler kopyalanarak dönüştürülür. OneDrive'daki asıl dosyaya dokunulmaz ve dosya kilitlenmez.

## 2. İlk indeksleme

```powershell
# WIN32_MCP_DATAPOOL_ROOTS'taki klasörler; değişken boşsa OneDrive / OneDriveCommercial otomatik bulunur
win32-mcp-datapool index

# Veya belirli klasörler, 6 paralel işçiyle
win32-mcp-datapool index "C:\Users\ben\OneDrive - Firma\Projeler" "C:\Users\ben\OneDrive - Firma\Teklifler" --workers 6
```

Önemli seçenekler:

| Seçenek | Açıklama |
|---|---|
| `--workers N` | Paralel işçi sayısı (varsayılan: çekirdek sayısı − 1, en fazla 4) |
| `--hydrate` | *Yalnızca çevrimiçi* (bulut simgeli) OneDrive dosyalarını indirip okur. Bu seçenek verilmezse bu dosyalar `cloud_only` olarak yalnızca adıyla kaydedilir |
| `--project-depth 2` | Proje adını ilk 2 klasör seviyesinden üretir (ör. `2024/Deniz Konutları`) |

Proje adı, kökün altındaki klasörlerden üretilir. Komut satırında klasör verilirse kök o
klasördür; klasör verilmezse `WIN32_MCP_DATAPOOL_ROOTS` içindeki kökler kullanılır. MCP aracı her
zaman izin verilen kökleri esas alır, alt klasör tarasanız da proje adı değişmez; kök tüm OneDrive
ise `project_depth: 2` verin ya da `WIN32_MCP_DATAPOOL_ROOTS` değişkenini proje klasörünüze
(ör. `...\OneDrive\Projeler`) ayarlayın.
| `--ext dwg --ext pdf` | Yalnızca bu uzantıları tarar; silinen dosya temizliği de yalnızca bu uzantılara uygulanır |
| `--force` | Değişmemiş dosyaları da yeniden işler |
| `--max-files N` | Bir çalışmada en fazla N dosya işler (büyük arşivi parça parça taramak için) |

Tarama artımlıdır: sonraki çalıştırmalar yalnızca yeni veya değişmiş dosyaları işler. Silinen
dosyaların kayıtları da havuzdan temizlenir. Bir klasör okunamazsa (izin, ağ veya OneDrive hatası)
o taramada hiçbir kayıt silinmez. Bağlantı noktası (junction) ve sembolik bağlantılar izlenmez. Görev Zamanlayıcı'ya gece çalışacak bir görev
eklemek yeterlidir:

```powershell
schtasks /Create /SC DAILY /ST 02:00 /TN "VeriHavuzu" /TR "win32-mcp-datapool index -q"
```

## 3. Komut satırından sorgu

```powershell
win32-mcp-datapool stats
win32-mcp-datapool projects
win32-mcp-datapool search "zemin etüdü sondaj" --category etut_plani
win32-mcp-datapool search "123 ada" --ext dwg
win32-mcp-datapool show 42
win32-mcp-datapool export havuz.csv      # Excel'de açılabilir (UTF-8 BOM)
win32-mcp-datapool export havuz.jsonl    # Diğer araçlar / RAG için
```

Arama Türkçe karakterlere duyarlı değildir: `etut` yazmak `Etüt` kaydını da bulur. Son kelime
önek olarak aranır: `sond` yazmak `sondaj` kaydını bulur.

## 4. Yerel ajanlara bağlama (MCP)

Havuz araçları `win32-mcp-server` içindedir. Claude Desktop için
`%APPDATA%\Claude\claude_desktop_config.json` dosyasına şunu ekleyin:

```json
{
  "mcpServers": {
    "win32": {
      "command": "win32-mcp-server",
      "env": {
        "WIN32_MCP_DATAPOOL_ROOTS": "C:\\Users\\ben\\OneDrive - Firma\\Projeler;C:\\Users\\ben\\OneDrive - Firma\\Teklifler",
        "WIN32_MCP_SECURITY_PROFILE": "read_only"
      }
    }
  }
}
```

- `WIN32_MCP_DATAPOOL_ROOTS`: Ajanların tarayabileceği klasörleri noktalı virgülle ayırarak
  yazın. Bu değişken boş bırakılırsa kullanıcının OneDrive klasörleri kullanılır. Bu klasörlerin
  dışındaki yollar reddedilir.
- `WIN32_MCP_DATAPOOL_DB`: Veritabanının yolu. Birden fazla ajan aynı havuzu paylaşacaksa
  hepsine aynı yolu verin (SQLite WAL modu eşzamanlı okumayı destekler).
- `read_only` profili ekran/fare/klavye araçlarını kapatır. Bu profilde yalnızca havuzu okuyan
  araçlar kalır. İnceleme ajanının `datapool_annotate` ile havuza yazabilmesi gerekiyorsa
  varsayılan `interactive` profilini kullanın ve istemediğiniz araçları
  `WIN32_MCP_BLOCKED_TOOLS` ile kapatın.

### Araçlar

| Araç | İşlev |
|---|---|
| `datapool_index` | Tarama yapar. Her çağrı en fazla 300 dosyayı ve 120 saniyeyi işler. `stopped_early: true` döndükçe tekrar çağrılır |
| `datapool_stats` | Toplam dosya sayısı, incelenmiş dosya sayısı ve kategori/uzantı/durum dağılımı |
| `datapool_projects` | Proje başına çizim, etüt, teklif ve veri sayıları |
| `datapool_search` | Tam metin arama (dosya adı, yol, çizim yazıları, antet, belge metni, ajan özetleri) |
| `datapool_get` | Tek kayıt: DWG katmanları/blokları/antet, PDF sayfa sayısı, metin, ajan yorumu |
| `datapool_pending_reviews` | Henüz hiçbir ajanın incelemediği dosyalar (ajanların iş kuyruğu) |
| `datapool_annotate` | Ajanın özetini, etiketlerini, düzeltilmiş kategorisini ve yapısal alanlarını kaydeder |

### İnceleme ajanı için örnek talimat

> Sen bir proje arşivi inceleme ajanısın. Adımlar:
> 1. `datapool_pending_reviews` ile incelenmemiş en fazla 10 dosyayı al (istersen `category` ver).
> 2. Her dosya için `datapool_get` çağır. DWG'lerde `meta.title_block`, `meta.layers` ve
>    metne; tekliflerde tutar, tarih ve müşteriye; etüt planlarında sondaj, zemin sınıfı ve
>    ada/parsele bak.
> 3. `datapool_annotate` ile şunları kaydet: 1–3 cümlelik Türkçe `summary`; `tags`
>    (ör. `mimari`, `zemin kat`, `otel`); otomatik kategori yanlışsa doğru `category`;
>    `fields` içine bulduğun somut bilgileri yaz (`ada_parsel`, `olcek`, `musteri`,
>    `teklif_tutari`, `tarih`, `pafta`, `revizyon`); `reviewed_by` alanına kendi adını yaz.
> 4. Kuyruk boşalana kadar tekrarla. Emin olmadığın bilgiyi uydurma, boş bırak.

Birden fazla ajan aynı anda çalışacaksa işi bölün. Örneğin ajanlardan biri
`category: "proje"` dosyalarını, diğeri `teklif_sunumu` ve `etut_plani` dosyalarını incelesin.
Böylece aynı dosya iki kez incelenmez.

## 5. Sınırlar

- DWG içeriği ODA File Converter gerektirir. Taranmış (görüntü) PDF'lerde metin çıkmaz; bu
  kayıtlar `meta.needs_ocr: true` ile işaretlenir.
- Eski `.doc`, `.xls` ve `.ppt` biçimleri şimdilik yalnızca adlarıyla sınıflanır; bu dosyalar
  varsayılan uzantı listesinde yoktur.
- Otomatik kategori, klasör/dosya adı ve metindeki anahtar kelimelere dayanan kurallarla
  belirlenir. Yanlış kategoriyi ajanın `datapool_annotate` ile verdiği `category` geçersiz kılar.
- 500 MB'tan büyük dosyalar atlanır.
