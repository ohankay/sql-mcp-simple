# sql-mcp-simple (`sql-mcp`)

VS Code (GitHub Copilot Chat ve custom agent'lar) için **Microsoft SQL Server 2017 / 2019 / 2022** MCP sunucusu.

Agent'larınız SQL işlemlerini `powershell` + `sqlcmd` çalıştırmadan, doğrudan bu MCP sunucusunun tool'ları üzerinden yapar.

| | |
|---|---|
| Bağlantı | **SQL login** (kullanıcı adı + şifre) veya **Windows oturumu** (Integrated / Trusted) |
| Sorgu | SELECT, INSERT / UPDATE / DELETE / MERGE, DDL, `GO` içeren script'ler, parametreli sorgu, çoklu result set, PRINT mesajları |
| Stored procedure | Çalıştırma (named/positional parametre, OUTPUT parametre, return value), CREATE / ALTER / DROP |
| Transaction | begin / commit / rollback / status, isolation level |
| SQL Agent | Job listeleme, detay, oluşturma (step + schedule, tek transaction), güncelleme, silme, başlatma, durdurma, step/schedule ekle-düzenle-sil, history, çalışan job'lar |
| Yönetim | Session listesi, KILL, login/user/grant, BACKUP, DBCC, ALTER DATABASE... (`execute_sql` ile her şey) |
| Keşif | Veritabanları, objeler, tablo yapısı (kolon/PK/index/FK), obje kaynak kodu |

## Neden DLP / EDR ajanlarını daha az tetikler?

`sqlcmd` + PowerShell yaklaşımında her sorguda `powershell.exe` → `sqlcmd.exe` alt süreci açılır. Komut satırında SQL metni, çoğu zaman şifre de yer alır. Script-block logging, AMSI ve EDR kuralları tam olarak bu davranışı izler.

sql-mcp ise:

* VS Code tarafından **bir kez** başlatılan tek bir `python.exe` sürecidir. SQL Server'a, SSMS'in yaptığı gibi, süreç içinden ODBC/TDS ile TCP bağlantısı açar.
* Sorgu başına **hiçbir alt süreç açmaz**: PowerShell, cmd, sqlcmd yok. Komut satırında SQL de şifre de bulunmaz.
* Bağlantı kalıcıdır (connection reuse). Her sorguda yeniden login olmaz, bu yüzden hızlıdır.
* Windows Authentication kullanılırsa şifre hiçbir yerde tutulmaz.

> Dürüst not: Hiçbir araç, kurumunuzdaki güvenlik ürünlerinin hiçbir şey raporlamayacağını garanti edemez. Gerekirse BT ekibinizden yalnızca bu `python.exe` / `sql-mcp.exe` yolu için istisna isteyebilirsiniz. Bu, "her sorguda PowerShell" istisnasından çok daha dar ve savunulabilir bir istisnadır.

## Kurulum (Windows)

Gereksinim: Python 3.10+ (python.org veya Microsoft Store; kurumsal yazılım merkezinden de olabilir).

```bat
py -m venv %USERPROFILE%\.sql-mcp
%USERPROFILE%\.sql-mcp\Scripts\pip install git+https://github.com/ohankay/sql-mcp-simple.git
```

Git yoksa repoyu ZIP olarak indirip klasörde `%USERPROFILE%\.sql-mcp\Scripts\pip install .` çalıştırın.

Sürücü: Ekstra kurulum **gerekmez**. Sırayla şunlar denenir:

1. "ODBC Driver 18 / 17 for SQL Server" (SSMS ile genelde zaten kurulu gelir) → `pyodbc`
2. `mssql-python`: Microsoft'un sürücüsü. ODBC kütüphanesi wheel'in içinde gelir, sisteme hiçbir şey kurmaz.
3. Windows'un kendi eski "SQL Server" ODBC sürücüsü (son çare)

Bağlantıyı test etmek için:

```bat
set SQL_MCP_SERVER=MYSERVER\INSTANCE
set SQL_MCP_AUTH=windows
%USERPROFILE%\.sql-mcp\Scripts\sql-mcp.exe --check
```

## VS Code'a ekleme

`Ctrl+Shift+P` → **MCP: Open User Configuration** (tüm workspace'lerde geçerli olur) ya da workspace içinde `.vscode/mcp.json` dosyasını açın:

```jsonc
{
  "inputs": [
    { "type": "promptString", "id": "sql-password", "description": "SQL Server password", "password": true }
  ],
  "servers": {
    // 1) Windows oturumu ile (şifre yok)
    "sql": {
      "type": "stdio",
      "command": "${userHome}\\.sql-mcp\\Scripts\\sql-mcp.exe",
      "env": {
        "SQL_MCP_SERVER": "MYSERVER\\INSTANCE",
        "SQL_MCP_AUTH": "windows",
        "SQL_MCP_DATABASE": "master"
      }
    },
    // 2) SQL kullanıcısı ile: şifreyi VS Code bir kez sorar ve kendi secret storage'ında saklar
    "sql-login": {
      "type": "stdio",
      "command": "${userHome}\\.sql-mcp\\Scripts\\sql-mcp.exe",
      "env": {
        "SQL_MCP_SERVER": "10.0.0.15,1433",
        "SQL_MCP_AUTH": "sql",
        "SQL_MCP_USER": "app_user",
        "SQL_MCP_PASSWORD": "${input:sql-password}",
        "SQL_MCP_DATABASE": "MyDb"
      }
    }
  }
}
```

Copilot Chat'te **Agent** modunu seçin. Tool listesinde `sql` sunucusunun tool'ları görünür.

### Custom agent ile kullanım

`.github/agents/sql-dba.agent.md` (bu repoda örneği var):

```markdown
---
description: SQL Server DBA assistant
tools: ['sql/*']
---
Work only through the `sql` MCP tools - never through terminal commands, PowerShell or sqlcmd.
...
```

`tools: ['sql/*']` ifadesi, `mcp.json` içindeki `sql` sunucusunun tüm tool'larını agent'a verir. Sadece bazılarını vermek için `['sql/execute_sql', 'sql/describe_table']` yazabilirsiniz. Eski VS Code sürümlerinde aynı içerik `.github/chatmodes/*.chatmode.md` dosyasına yazılır. Terminal tool'unu agent'a vermezseniz, agent PowerShell'e hiç düşemez.

### Birden fazla sunucu (profil dosyası)

`%USERPROFILE%\.sql-mcp\connections.json` ([örnek](examples/sql-mcp.config.json)):

```json
{
  "default": "dev",
  "connections": {
    "dev":       { "server": "DEVSQL01", "auth": "windows", "database": "AppDb" },
    "test":      { "server": "TESTSQL01\\SQL2017", "auth": "sql", "user": "app_user",
                   "password_env": "SQL_TEST_PASSWORD" },
    "prod-read": { "server": "PRODSQL01,1433", "auth": "windows", "readonly": true, "max_rows": 200 }
  }
}
```

`mcp.json` → `"env": { "SQL_MCP_CONFIG": "${userHome}\\.sql-mcp\\connections.json" }`. Her tool çağrısı opsiyonel bir `connection` parametresi alır, örneğin `connection: "test"`. Agent ayrıca `add_connection` ile çalışma anında yeni profil de ekleyebilir.

## Ayarlar

Ortam değişkenleri (tek profil) ya da JSON dosyasındaki alanlar (çok profil):

| Env | JSON | Varsayılan | Açıklama |
|---|---|---|---|
| `SQL_MCP_SERVER` | `server` | (zorunlu) | `host`, `host\INSTANCE`, `host,port` |
| `SQL_MCP_PORT` | `port` | | Ayrı port |
| `SQL_MCP_AUTH` | `auth` | user varsa `sql`, yoksa `windows` | `sql` / `windows` |
| `SQL_MCP_USER` / `SQL_MCP_PASSWORD` | `user` / `password` / `password_env` | | SQL login |
| `SQL_MCP_DATABASE` | `database` | `master` | Başlangıç veritabanı |
| `SQL_MCP_DRIVER` | `driver` | `auto` | `auto` / `pyodbc` / `mssql-python` |
| `SQL_MCP_ODBC_DRIVER` | `odbc_driver` | en yenisi | Örn. `ODBC Driver 17 for SQL Server` |
| `SQL_MCP_ENCRYPT` | `encrypt` | `yes` | `yes` / `no` / `strict` |
| `SQL_MCP_TRUST_SERVER_CERTIFICATE` | `trust_server_certificate` | `true` | Şirket içi self-signed sertifikalar için |
| `SQL_MCP_READONLY` | `readonly` | `false` | Yazma komutlarını engeller (aşağıya bakın) |
| `SQL_MCP_QUERY_TIMEOUT` | `query_timeout` | `300` sn | 0 = sınırsız; tool çağrısında `timeout_seconds` ile ezilebilir |
| `SQL_MCP_LOGIN_TIMEOUT` | `login_timeout` | `15` sn | |
| `SQL_MCP_MAX_ROWS` | `max_rows` | `1000` | Result set başına dönen satır sayısı (agent'ın context'i şişmesin diye) |
| `SQL_MCP_APP_NAME` | `app_name` | `sql-mcp` | `sys.dm_exec_sessions.program_name` alanında görünür |
| `SQL_MCP_EXTRA` | `extra` | | Ham connection string eki, örn. `MultiSubnetFailover=yes;ApplicationIntent=ReadOnly` |
| `SQL_MCP_NAME` | | `default` | Env profilinin adı |
| `SQL_MCP_CONFIG` | | | Çoklu profil JSON dosyası |

## Tool'lar

| Grup | Tool | Not |
|---|---|---|
| Bağlantı | `list_connections`, `test_connection`, `add_connection`, `remove_connection` | Şifreler hiçbir çıktıda görünmez |
| Sorgu | `execute_sql` | Her T-SQL. `GO` / `GO n`, `params` (`?`), `database`, `max_rows`, `continue_on_error`, `timeout_seconds`, `output_format: json/markdown` |
| SP | `execute_procedure` | `params: {"Id": 5}` veya `[5, "x"]`, `output_params: {"Total": "decimal(18,2)"}` → `return_value`, `output_params` |
| Transaction | `begin_transaction`, `commit_transaction`, `rollback_transaction`, `transaction_status` | Bağlantı kalıcıdır; açık transaction varsa her sonuçta uyarı döner |
| Keşif | `list_databases`, `list_objects`, `describe_table`, `get_object_definition` | |
| Agent job | `list_jobs`, `get_job`, `create_job`, `update_job`, `delete_job`, `start_job`, `stop_job`, `add_job_step`, `update_job_step`, `delete_job_step`, `add_job_schedule`, `delete_job_schedule`, `get_job_history`, `list_running_jobs` | Tüm değerler parametre olarak bağlanır |
| Yönetim | `list_sessions`, `kill_session` | |

Job oluşturma örneği (agent bunu kendisi üretir):

```json
{
  "job_name": "Gece ETL",
  "steps": [
    { "name": "Yükle", "command": "EXEC dbo.LoadStage", "database": "Dwh", "retry_attempts": 2, "retry_interval": 5 },
    { "name": "Birleştir", "command": "EXEC dbo.MergeFacts", "database": "Dwh", "on_fail": "quit_failure" }
  ],
  "schedules": [
    { "name": "Her gece 02:30", "type": "daily", "start_time": "02:30" },
    { "name": "Mesai içi 15 dk", "type": "weekly", "days": ["pzt", "sali", "carsamba", "persembe", "cuma"],
      "repeat_every_minutes": 15, "start_time": "08:00", "end_time": "18:00" }
  ]
}
```

Schedule tipleri: `once` (start_date gerekir), `daily`, `weekly` (`days`), `monthly` (`day_of_month`), `agent_start`, `idle`. Tekrar için `repeat_every_seconds`, `repeat_every_minutes` veya `repeat_every_hours`. Gün adları İngilizce ya da Türkçe yazılabilir. Burada olmayan bir `sp_add_jobschedule` / `sp_add_jobstep` parametresi için `"raw": {"freq_relative_interval": 1}` kullanılabilir.

## Güvenlik notları

* SQL değerleri her zaman parametre (`?`) olarak bağlanır. Tool'ların içindeki obje adları `[...]` ile quote edilir ve ad üzerinden injection mümkün değildir (testlerde doğrulanır).
* `readonly: true` profili INSERT / UPDATE / DELETE / DDL / EXEC (`sp_help`, `sp_who2` gibi bilinen okuma prosedürleri hariç) / BACKUP / KILL / job değişikliği gibi komutları **istemci tarafında** engeller. Bu yanlışlıkla yapılan değişikliklere karşı bir emniyet kemeridir, güvenlik sınırı değildir. Prod için asıl koruma, yalnızca okuma yetkisi olan bir login'dir.
* Şifreyi `mcp.json` içine düz metin olarak yazmayın. `${input:...}` (VS Code secret storage), `password_env` ya da Windows Authentication kullanın.
* VS Code her tool çağrısında onay sorar. Okuma tool'ları `readOnlyHint` ile işaretlidir. "Always allow" seçeneğini yalnızca okuma tool'larına vermeniz önerilir.

## Bilinmesi gerekenler / ipuçları

* Türkçe ve Unicode metinler için literal'lerde `N'...'` kullanın. Parametreler (`params`) zaten Unicode bağlanır.
* `CREATE PROCEDURE/VIEW/FUNCTION/TRIGGER` kendi batch'inde olmalıdır (öncesinde ve sonrasında `GO`). `CREATE OR ALTER` 2016 SP1+ sürümlerinde, yani 2017/2022'de çalışır.
* Bağlantı kalıcıdır: `#temp` tablolar, `SET` ayarları ve açık transaction'lar çağrılar arasında korunur. `database` parametresi sorguyu o veritabanında çalıştırır ve sonra eski veritabanına geri döner. Script içindeki `USE x` ise kalıcıdır (SSMS'teki gibi).
* Uzun süre boşta kalan bağlantı otomatik test edilir ve kopmuşsa yeniden açılır. Açık bir transaction varken bağlantı koptuysa sessizce yeniden bağlanılmaz, hata döner (transaction sunucuda geri alınmıştır).
* Büyük sonuçlar `max_rows` ile ve toplam yaklaşık 2 MB ile sınırlanır, `truncated: true` notu döner.
* `geography`, `geometry`, `hierarchyid` hex olarak döner. Okunur hâli için `.ToString()` / `.STAsText()` kullanın.
* Yalnızca `mssql-python` sürücüsünde (ODBC Driver 17/18 yoksa):
  * Aynı batch'te birden çok DML olduğunda `rows_affected` değerleri hatalı olabilir (sürücü kaynaklı). Tek ifadede doğrudur.
  * Metin parametreleriyle birlikte varbinary bir kolona `NULL` parametre geçilirse `CAST(? AS varbinary(max))` yazın.
* Windows Authentication, VS Code'u açan Windows kullanıcısıyla bağlanır (`Trusted_Connection=yes`). Başka bir domain kullanıcısıyla bağlanmak için VS Code'u o kullanıcıyla açın (`runas /netonly`). Linux/macOS'ta Kerberos (`kinit`) gerekir.

## Sorun giderme

| Belirti | Çözüm |
|---|---|
| `Login failed for user ''` / `Cannot generate SSPI context` | Windows auth: domain bağlantısı / SPN sorunu. `SQL_MCP_SERVER` alanına FQDN yazmayı deneyin. |
| `SSL Provider: The certificate chain was issued by an authority that is not trusted` | `SQL_MCP_TRUST_SERVER_CERTIFICATE=true` (varsayılan) ya da sertifikayı güvenilir hâle getirin |
| `Named Pipes Provider ... could not open a connection` | Instance adı veya port yanlış, SQL Browser kapalı, firewall. `host,port` biçimini deneyin. |
| `No usable SQL Server driver` | `pip install mssql-python` |
| Tool'lar görünmüyor | VS Code → `MCP: List Servers` → `sql` → **Show Output** (log'lar orada) |

Teşhis için: `sql-mcp --check` komutu, kurulu sürücüleri, profilleri ve her profilin bağlantı sonucunu JSON olarak verir.

## Geliştirme ve testler

```bash
pip install -e ".[dev]"
docker compose -f tests/docker-compose.yml up -d      # SQL Server 2017 + 2022 (Agent açık)
export SQL_MCP_TEST_TARGETS="sql2022=localhost,14332;sql2017=localhost,14317"
export SQL_MCP_TEST_PASSWORD='Str0ng!Passw0rd'
pytest -v
```

Test kapsamı (her test **SQL Server 2017 ve 2022** üzerinde, **pyodbc ve mssql-python** sürücüleriyle ayrı ayrı çalışır):

* **Unit** (sunucu gerekmez): GO ayrıştırıcı (string, yorum, iç içe yorum, `[...]`, `GO n`), read-only guard, identifier quoting ve injection, config doğrulama ve hata mesajları, connection string escaping (`;` `}` içeren şifreler), Windows auth connection string, sürücü seçimi, job/schedule spec doğrulama, tip dönüşümleri.
* **Entegrasyon**: CRUD ve rowcount, MERGE/OUTPUT, 30'dan fazla veri tipi (datetimeoffset, sql_variant, xml, hierarchyid, geography, money, varbinary, nvarchar(max) 200K karakter, Türkçe karakterler ve emoji), çoklu result set ve PRINT, GO ile SP oluşturma, syntax/runtime/RAISERROR/THROW hataları, kısmi sonuçlar, `continue_on_error`, `max_rows` sınırı, query timeout, veritabanı context'i, temp table kalıcılığı, eşzamanlı çağrılar, SP (OUTPUT, INPUT/OUTPUT, return value, eksik parametre, iş kuralı hatası), transaction (rollback, commit, nested, isolation, XACT_ABORT), şema keşfi, yönetim (login/user/grant, BACKUP, DBCC, ALTER DATABASE), düşük yetkili login ile permission hataları, readonly profili, session listeleme ve KILL, sunucu tarafında öldürülen bağlantının otomatik yenilenmesi, yanlış şifre, erişilemeyen sunucu, Agent job'ın tüm yaşam döngüsü (çalıştırma ve history doğrulama, hata veren job, çalışan job'ı durdurma, aynı isimde job oluşturulunca tüm işlemin geri alınması).
* **Uçtan uca**: `python -m sql_mcp` gerçek MCP stdio protokolüyle (VS Code'un yaptığı gibi) başlatılır, tool'lar çağrılır. Konfigürasyonsuz ve hatalı konfigürasyonla başlatma ile `--check` CLI'ı da test edilir.

## Lisans

MIT
