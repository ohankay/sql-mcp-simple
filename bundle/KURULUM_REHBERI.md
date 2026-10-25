# sql-mcp-simple: Kurulum Rehberi (GitHub Copilot ajanı için)

## Kullanıcı için (30 saniye)

1. Zip'i bir klasöre açın, örneğin `C:\Araclar\sql-mcp-simple`.
2. VS Code'da **File → Open Folder** ile bu klasörü açın.
3. Copilot Chat'i açın, modu **Agent** yapın ve şunu yazın:

   > `KURULUM_REHBERI.md dosyasını oku ve "Ajan için kurulum adımları" bölümünü sırayla uygula.`

4. Ajan size SQL Server adresini ve bağlantı tipini soracak, kurulumu kendisi yapacak.
   **Şifrenizi sohbete asla yazmayın.** Şifreyi VS Code ilk başlatmada güvenli bir kutuda soracak.

Gereksinimler: Windows 10/11 x64, Python 3.10–3.14 (`py --version` ile kontrol edin). İnternet gerekmez, tüm paketler `wheelhouse` klasöründe.

Not: Kurulum sırasında terminalde bir kez `install.cmd` çalışır. Kurulumdan sonra SQL işlemleri için PowerShell veya sqlcmd hiç kullanılmaz.

---

## Ajan için kurulum adımları

> Sen bu kurulumu yapan GitHub Copilot ajanısın. Adımları **sırayla** uygula. Her adımın sonucunu kullanıcıya kısa bir cümleyle bildir. Bir adım başarısız olursa sonraki adıma geçme; "Sorun giderme" bölümüne bak, çözemiyorsan kullanıcıya hatayı olduğu gibi göster.
>
> **Kesin kurallar:**
> - Kullanıcıdan şifre İSTEME, şifreyi hiçbir dosyaya, komuta veya ortam değişkenine YAZMA. SQL şifresi yalnızca `${input:sql-password}` ile VS Code tarafından sorulur.
> - Kullanıcının mevcut `mcp.json` dosyasındaki diğer sunucuları SİLME, sadece `sql` girdisini ekle veya güncelle.
> - Bu klasördeki dosyaları değiştirme (`templates` sadece kopyalanır).
> - Komutları VS Code'un varsayılan terminalinde (PowerShell) çalıştır.

### Adım 1: Paketleri kur

Bu rehberin bulunduğu klasörde (zip'in açıldığı kök klasör) çalıştır:

```powershell
cmd /c install.cmd; "EXIT=$LASTEXITCODE"
```

- Çıktıda `KURULUM TAMAM` ve `EXIT=0` görmelisin.
- Kurulum yeri: `%USERPROFILE%\.sql-mcp`. Komut yolu: `%USERPROFILE%\.sql-mcp\Scripts\sql-mcp.exe`.
- Hata kodları: `10` = uygun Python yok, `11` = venv oluşturulamadı, `12` = paket kurulamadı, `13` = komut çalışmadı. Ayrıntı için "Sorun giderme" bölümüne bak.

### Adım 2: Bağlantı bilgilerini kullanıcıdan al

Kullanıcıya şu soruları tek mesajda sor (şifre SORMA):

1. **SQL Server adresi:** `SUNUCU`, `SUNUCU\INSTANCE` veya `SUNUCU,PORT` (örn. `DBSRV01\SQL2017`, `10.1.2.3,1433`)
2. **Bağlantı tipi:**
   - `windows`: VS Code'u açan Windows kullanıcısıyla bağlanır, şifre yok (önerilen)
   - `sql`: SQL kullanıcı adı ve şifre
3. **Kullanıcı adı:** sadece `sql` seçildiyse
4. **Varsayılan veritabanı:** boş bırakılırsa `master`
5. **Salt okunur mu?** Prod gibi sunucular için `evet` önerilir (varsayılan: hayır)
6. **Birden fazla sunucu mu?** Evetse her biri için 1–5 tekrarlanır.

### Adım 3: Bağlantıyı test et (yalnızca `windows` tipi için)

`sql` tipinde bu adımı ATLA. Şifre ajan tarafından kullanılamaz; test Adım 6'da VS Code içinden yapılacak.

```powershell
$env:SQL_MCP_SERVER = "<ADRES>"; $env:SQL_MCP_AUTH = "windows"; $env:SQL_MCP_DATABASE = "<VERITABANI>"
& "$env:USERPROFILE\.sql-mcp\Scripts\sql-mcp.exe" --check; "EXIT=$LASTEXITCODE"
Remove-Item Env:SQL_MCP_SERVER, Env:SQL_MCP_AUTH, Env:SQL_MCP_DATABASE
```

- `EXIT=0` ve çıktıda `"success": true` varsa: kullanıcıya `server.product` (örn. "SQL Server 2017"), `login_name` ve `sql_agent` değerlerini bildir.
- `EXIT=1` ise `connections.default.errors[0].message` mesajını kullanıcıya göster ve "Sorun giderme" bölümüne bak. Adres düzeltilirse bu adımı tekrarla.

### Adım 4: VS Code MCP yapılandırmasını yaz

Hedef dosya, **kullanıcı düzeyi** (tüm workspace'lerde geçerli):

```powershell
"$env:APPDATA\Code\User\mcp.json"
```

(VS Code Insiders kullanılıyorsa `$env:APPDATA\Code - Insiders\User\mcp.json`. Kullanıcı sadece bu proje için isterse `<workspace>\.vscode\mcp.json`.)

İçerik, `templates` klasöründeki şablonlardan birinden alınır:

| Durum | Şablon |
|---|---|
| Tek sunucu, `windows` | `templates/mcp.windows-auth.json` |
| Tek sunucu, `sql` | `templates/mcp.sql-auth.json` |
| Birden fazla sunucu | `templates/mcp.multi-profile.json` + `templates/connections.json` |

Kurallar:

- Şablondaki yer tutucuları kullanıcının cevaplarıyla değiştir: `SUNUCU\\INSTANCE` / `SUNUCU,1433` → adres, `KULLANICI` → kullanıcı adı, `master` → veritabanı. JSON içinde `\` karakteri `\\` olarak yazılır, örneğin `DBSRV01\\SQL2017`.
- Salt okunur istendiyse `env` içine `"SQL_MCP_READONLY": "true"` ekle.
- **Dosya yoksa:** şablonun tamamıyla oluştur.
- **Dosya varsa:** dosyayı oku, `servers` altına yalnızca `"sql"` anahtarını ekle; varsa güncelle. `sql` tipinde `inputs` dizisine `sql-password` girdisini ekle; aynı `id` zaten varsa ekleme. Diğer sunuculara, girdilere ve yorumlara dokunma.
- Aynı adda başka amaçlı bir `sql` sunucusu varsa kullanıcıya sor; gerekirse ad olarak `sql-mcp` kullan ve Adım 5'teki `tools` satırını buna göre değiştir.
- Çoklu sunucuda: `templates/connections.json` dosyasını `%USERPROFILE%\.sql-mcp\connections.json` olarak kopyala ve `connections` içini kullanıcının sunucularıyla doldur. `sql` tipindeki profillerde `password` alanı OLMAZ; bunun yerine `"password_env": "<DEGISKEN_ADI>"` yaz ve kullanıcıya bu ortam değişkenini kendisinin tanımlaması gerektiğini söyle (Windows → "Ortam değişkenleri" → Kullanıcı değişkeni). `windows` tipinde ek bir şey gerekmez.
- `%USERPROFILE%\.sql-mcp` dışında bir yere kurulduysa (`SQL_MCP_HOME`), `command` alanına tam yolu yaz.

Dosyayı yazdıktan sonra geçerli JSON olduğunu kontrol et (VS Code `mcp.json` içinde `//` yorumlarına izin verir, ama sondaki virgüller ve kapanmamış parantezler hata verir).

### Adım 5: Copilot custom agent ekle (isteğe bağlı)

Kullanıcıya "SQL işleri için hazır bir custom agent ister misin?" diye sor. Evet derse `templates/sql-dba.agent.md` dosyasını kullanıcının çalıştığı proje klasörüne `.github/agents/sql-dba.agent.md` olarak kopyala. Bu agent'ın `tools: ['sql/*']` satırı, MCP sunucusunun adıyla aynı olmalıdır.

### Adım 6: Kullanıcıya son adımları söyle

Kullanıcıya aşağıdakileri aynen ilet:

1. `Ctrl+Shift+P` → **MCP: List Servers** → **sql** → **Start Server**. İlk açılışta güven onayı gelirse **Trust** deyin.
2. `sql` tipi seçildiyse VS Code şifreyi bir kez soracak ve güvenli depoda saklayacak.
3. Copilot Chat → **Agent** modu → araç (🔧) simgesinde `sql` altında 30 tool görünmeli.
4. Test için şunu yazın: **"sql test_connection çalıştır ve sonucu özetle"**.
5. Sorun olursa: **MCP: List Servers** → **sql** → **Show Output** (log'lar orada).

Kurulum burada biter.

---

## Sorun giderme

| Belirti | Neden / Çözüm |
|---|---|
| `install.cmd` → EXIT 10 | Python 3.10–3.14 yok veya PATH'te değil. Python 3.12'yi kurumsal yazılım merkezinden ya da python.org'dan kurun ("Add to PATH" ve "py launcher" seçili olsun), VS Code'u kapatıp açın. |
| EXIT 11 | `%USERPROFILE%` yazılamıyor ya da antivirüs engelliyor. `set SQL_MCP_HOME=D:\Araclar\sql-mcp` ile başka bir klasöre kurmayı deneyin. |
| EXIT 12 | `wheelhouse` klasörü eksik veya Python sürümü desteklenmiyor (3.15+). Zip'i yeniden açın ya da Python 3.12 kullanın. |
| `Login failed for user 'DOMAIN\kullanici'` | Windows kullanıcınızın SQL Server'da login'i yok. DBA'dan yetki isteyin. |
| `Cannot generate SSPI context` | Kerberos/SPN sorunu. Adresi FQDN olarak deneyin (`dbsrv01.sirket.local\SQL2017`) veya `sql` tipini kullanın. |
| `Login timeout expired` / `Named Pipes Provider` | Adres, instance adı veya port yanlış; ya da firewall/SQL Browser kapalı. `SUNUCU,PORT` biçimini deneyin. |
| `certificate chain was issued by an authority that is not trusted` | Varsayılan olarak `TrustServerCertificate=yes`. `SQL_MCP_TRUST_SERVER_CERTIFICATE` değeri `false` yapılmışsa geri alın. |
| VS Code'da `sql` sunucusu görünmüyor | `mcp.json` geçersiz JSON veya yanlış dosya. **MCP: Open User Configuration** ile açıp kontrol edin. |
| Sunucu başlıyor ama Copilot tool'ları görünmüyor | Chat modu **Agent** olmalı. Copilot Business/Enterprise'da kurum yöneticisinin "MCP servers in Copilot" politikasını açması gerekir. |
| `spawn ... ENOENT` | `command` yolu yanlış. `%USERPROFILE%\.sql-mcp\Scripts\sql-mcp.exe` dosyasının var olduğunu kontrol edin. |

## Güncelleme ve kaldırma

- **Güncelleme:** Yeni zip'i açın ve `install.cmd`'yi tekrar çalıştırın; mevcut kurulum yerinde güncellenir, `mcp.json` değişmez.
- **Kaldırma:** `%USERPROFILE%\.sql-mcp` klasörünü silin ve `mcp.json` içinden `sql` girdisini kaldırın.
