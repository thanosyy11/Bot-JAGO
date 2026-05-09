# Bot JAGO 🤖
Bot Telegram untuk memenangkan **war stok** di toko berbasis **WooCommerce** secara otomatis.

---

## Fitur Utama

| Fitur | Keterangan |
|-------|-----------|
| ⚡ Auto War | Checkout tepat jam 08:00 WIB tanpa intervensi manual |
| 🔐 Multi-Akun | Kelola banyak akun dari satu bot |
| 🔄 Smart Fallback | Produk habis? Bot otomatis cari varian tier terdekat |
| 🔒 Password Aman | Enkripsi Fernet — tidak ada password tersimpan plaintext |
| 📅 Jadwal Lengkap | Warm-up 07:55, War 08:00, Cleanup 09:00 — fully automated |
| 🧪 Dry Run Mode | Simulasi penuh tanpa eksekusi checkout — laporan tiap langkah via Telegram |
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

> ⚠️ **Penting:** `ENCRYPTION_KEY` harus **sama persis** antara mesin lokal dan LXC jika kamu memindahkan database. Beda key = password tidak bisa terbaca.

### 3. Dry Run (Pengujian Pertama)

Sebelum war sesungguhnya, aktifkan dry run untuk memvalidasi seluruh alur tanpa checkout:
```env
DRY_RUN=true
```
Bot akan berjalan penuh jam 08:00 dan mengirim laporan detail tiap langkah ke Telegram. Jika semua ✅, ganti ke `DRY_RUN=false` untuk war berikutnya.

### 4. Inisialisasi Database
```bash
python database.py
```

### 5. Jalankan Bot
```bash
python bot.py
```

---

## Deployment Permanen dengan systemd

```bash
sudo cp bot-jago.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable bot-jago
sudo systemctl start bot-jago

# Cek status
sudo systemctl status bot-jago
```

> Sesuaikan path di `bot-jago.service` jika direktori instalasi berbeda dari `/opt/Bot-JAGO`.

---

## Update dari GitHub

```bash
cd /opt/Bot-JAGO
git pull
source .emyu/bin/activate
pip install -r requirements.txt
python database.py
sudo systemctl restart bot-jago
```

---

## Alur Kerja Harian Admin

```
21:00  Buka bot → Input pesanan (per akun, via template)
07:55  [Otomatis] Warm-up & login semua akun
08:00  [Otomatis] War — add to cart & checkout
08:00+ Terima laporan hasil di Telegram
09:00  [Otomatis] Cleanup draft sisa
```

---

## Kompatibilitas

Bot ini dirancang untuk bekerja dengan toko berbasis **WooCommerce** yang menggunakan tema **Flatsome** dan metode pembayaran **COD/Cheque**. Endpoint yang digunakan mengikuti standar WooCommerce AJAX (`?wc-ajax=`).

---

## File Penting

| File | Keterangan |
|------|-----------|
| `.env` | Konfigurasi rahasia — **jangan di-commit ke Git** |
| `siliwangi_bot.db` | Database SQLite — **jangan di-commit ke Git** |
| `siliwangi_error.log` | Log error runtime |
| `bot-jago.service` | Systemd service untuk deployment LXC |

---

*Gibran & Jokowi for 2029* 🎯