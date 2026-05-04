import sqlite3
import json

def setup_database_lokal():
    # Ini akan membuka (atau membuat baru) file database di folder lokalmu
    conn = sqlite3.connect("siliwangi_bot.db")
    cursor = conn.cursor()

    # 1. Membuat tabel 'users' jika belum ada
    cursor.execute('''CREATE TABLE IF NOT EXISTS users (
        telegram_id INTEGER,
        username TEXT,
        password TEXT
    )''')

    # 2. Membuat tabel 'draft_orders' jika belum ada
    cursor.execute('''CREATE TABLE IF NOT EXISTS draft_orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_id INTEGER,
        username TEXT,
        status TEXT,
        payload_json TEXT
    )''')

    # 3. Membuat tabel 'products' untuk fitur fallback jika belum ada
    cursor.execute('''CREATE TABLE IF NOT EXISTS products (
        id TEXT,
        nama TEXT,
        kategori TEXT,
        tier INTEGER
    )''')

    # Membersihkan data lama agar tidak ganda jika script ini dijalankan berkali-kali
    cursor.execute("DELETE FROM users")
    cursor.execute("DELETE FROM draft_orders")

    # --- PENTING: UBAH BAGIAN INI ---
    # Masukkan ID Telegram dan Password asli dari akun Siliwangi milikmu
    TELEGRAM_ID = 6059959817
    EMAIL_AKUN = "phapsari096@gmail.com"
    PASSWORD_ASLI = "Scopy123!" 

    cursor.execute("INSERT INTO users (telegram_id, username, password) VALUES (?, ?, ?)",
                   (TELEGRAM_ID, EMAIL_AKUN, PASSWORD_ASLI))

    # Membuat satu draf pesanan tiruan (contoh: 12 pcs Belgian Chocolate agar lolos aturan kelipatan 12)
    keranjang_dummy = [
        {"id": "3dd1b51150ea997eda1b144b1d472eb2", "nama": "MAXI Belgian Chocolate", "qty": 60, "tier": 1, "kategori": "Dessert Cake"}
    ]
    
    cursor.execute("INSERT INTO draft_orders (telegram_id, username, status, payload_json) VALUES (?, ?, ?, ?)",
                   (TELEGRAM_ID, EMAIL_AKUN, "PENDING", json.dumps(keranjang_dummy)))
    # --------------------------------

    conn.commit()
    conn.close()
    print("✅ Database lokal berhasil dibangun dan diisi data!")

if __name__ == "__main__":
    setup_database_lokal()