# Bot JAGO 🤖
Bot Telegram untuk memenangkan **WAR** di toko berbasis **WooCommerce** secara otomatis.

---

## Fitur Utama

| Fitur | Keterangan |
|-------|-----------|
| ⚡ Auto War | Checkout tepat jam 08:00 WIB tanpa intervensi manual |
| 🔐 Multi-Akun | Kelola banyak akun dari satu bot |
| 🔄 Smart Fallback | Produk habis? Bot otomatis cari varian tier terdekat |
| 🔒 Password Aman | Enkripsi Fernet — tidak ada password tersimpan plaintext |
| 🚨 Alert Darurat | Notifikasi Telegram langsung jika bot crash saat war |
| 📊 Riwayat Order | Histori 3 pesanan terakhir per akun |

---

## Stack

- **Python 3.10+**
- [aiogram 3.x](https://docs.aiogram.dev/) — Telegram Bot
- [httpx](https://www.python-httpx.org/) — Async HTTP
- [BeautifulSoup4](https://beautiful-soup-4.readthedocs.io/) — HTML scraper
- [APScheduler](https://apscheduler.readthedocs.io/) — Cron job
- [cryptography](https://cryptography.io/) — Enkripsi password

---

## Instalasi (Debian/Ubuntu LXC)

### 1. Clone & Setup Environment
```bash
git clone <url-repo-ini>
cd Bot-JAGO
python3 -m venv .emyu
source .emyu/bin/activate
pip install -r requirements.txt
```

### 2. Buat File `.env`
```bash
cp .env.example .env
nano .env
```

Isi dengan nilai berikut:
```env
BOT_TOKEN=isi_token_dari_BotFather
ADMIN_ID=isi_telegram_id_kamu
ENCRYPTION_KEY=isi_hasil_generate_di_bawah
DRY_RUN=false
```

Generate `ENCRYPTION_KEY`:
```bash
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

### 3. Inisialisasi Database
```bash
python database.py
```

### 4. Jalankan Bot
```bash
python bot.py
```

---

## Deployment Permanen dengan systemd

```bash
id -u botjago >/dev/null 2>&1 || sudo useradd --system --home /opt/Bot-JAGO --shell /usr/sbin/nologin botjago
sudo chown -R botjago:botjago /opt/Bot-JAGO
sudo cp bot-jago.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable bot-jago
sudo systemctl start bot-jago

# Cek status
sudo systemctl status bot-jago
```

## File Penting

| File | Keterangan |
|------|-----------|
| `.env` | Konfigurasi rahasia — **jangan di-commit ke Git** |
| `siliwangi_bot.db` | Database SQLite — **jangan di-commit ke Git** |
| `siliwangi_error.log` | Log error runtime |
| `bot-jago.service` | Systemd service untuk deployment LXC |

---

*Gibran & Jokowi for 2029* 🎯
