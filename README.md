# 🤖 Bot JAGO - Auto CheckOut

Bot Telegram yang dirancang untuk memenangkan "War Stok" di website e-commerce berbasis WooCommerce. Bot ini berjalan 24 jam dengan menggunakan penjadwalan presisi tinggi.

## 🌟 Fitur Utama
- **Auto-Login & Cookie Session**: Mengamankan sesi login secara diam-diam (pemanasan) sebelum jam eksekusi.
- **Smart Substitution (Lintas Tier)**: Jika produk incaran (Tier 1) habis, bot akan langsung melompat mencari alternatif di Tier 2 atau Tier 3 secara instan tanpa menyerah.
- **Auto-Clear Cart**: Membersihkan "keranjang" dari orderan sebelumnya.
- **Smart Form Scraper & Dynamic Date**: Mengisi alamat pengiriman secara otomatis dari akun dan menghitung tanggal pengiriman H+1.
- **Anti 502 Bad Gateway**: Dilengkapi sistem *retry* (pengulangan) jika server website tujuan mengalami down akibat lonjakan trafik.
- **Error Logging**: Semua aktivitas dan *crash* dicatat rapi ke dalam file `siliwangi_error.log`.

## 🛠️ Teknologi yang Digunakan
- Python 3.10+
- **Aiogram 3.x** (Telegram Bot Framework)
- **HTTPX** (Asynchronous HTTP Client)
- **BeautifulSoup4** (HTML Scraper)
- **APScheduler** (Sistem Penjadwalan/Cron Job)
- **SQLite3** (Database Lokal)

## 🚀 Cara Instalasi di Server (Debian/Ubuntu)
1. Clone repositori ini: `git clone <link-repo-ini>`
2. Masuk ke folder: `cd Bot-JAGO`
3. Buat virtual environment: `python3 -m venv .emyu`
4. Aktifkan venv: `source .emyu/bin/activate`
5. Install dependensi: `pip install aiogram httpx beautifulsoup4 apscheduler python-dotenv pytz`
6. Buat file rahasia `.env` dan isi dengan `BOT_TOKEN` serta `ADMIN_ID` Telegram Anda.
7. Jalankan setup database: `python database.py`
8. Jalankan bot: `python bot.py` (Atau gunakan `systemd` untuk deployment 24 jam).

---
*Gibran Presiden 2029.* 🎯