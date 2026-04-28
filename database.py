import sqlite3
import json

DB_NAME = "siliwangi_bot.db"

def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    # 1. Tabel Users (Menyimpan kredensial login & cookies)
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_id TEXT UNIQUE,
        username TEXT,
        password TEXT,
        cookies TEXT
    )
    ''')

    # 2. Tabel Produk (Kamus ID dan Tier)
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS products (
        id INTEGER PRIMARY KEY,
        nama TEXT,
        kategori TEXT,
        tier INTEGER
    )
    ''')

    # 3. Tabel Draft Orders (Menyimpan settingan WAR)
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS draft_orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_id TEXT,
        status TEXT DEFAULT 'PENDING',
        total_maxi_qty INTEGER,
        payload_json TEXT
    )
    ''')
    conn.commit()
    conn.close()
    print("✅ Database berhasil diinisialisasi.")

def seed_products():
    """Memasukkan data produk, ID, dan Tier ke database jika belum ada."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    # Cek apakah data sudah ada
    cursor.execute("SELECT COUNT(*) FROM products")
    if cursor.fetchone()[0] > 0:
        conn.close()
        return

    # Kategori MAXI - Tier 1
    tier1 = [
        (251993, "MAXI Belgian Chocolate", "MAXI", 1),
        (36124, "MAXI Black Forest", "MAXI", 1),
        (168132, "MAXI Cokelat Tiramisu", "MAXI", 1),
        (281180, "MAXI Cokelat Dubai Pistachio", "MAXI", 1),
        (312, "MAXI Brownies Coklat", "MAXI", 1)
    ]
    # Kategori MAXI - Tier 2
    tier2 = [
        (19077, "MAXI Pandan Wangi", "MAXI", 2),
        (24883, "MAXI Red Velvet", "MAXI", 2),
        (306, "MAXI Susu Lembang", "MAXI", 2),
        (168131, "MAXI Durian Musang King", "MAXI", 2),
        (311, "MAXI Alpukat Mentega", "MAXI", 2)
    ]
    # Kategori MAXI - Tier 3
    tier3 = [
        (313, "MAXI Talas Bogor", "MAXI", 3),
        (315, "MAXI Mangga Indramayu", "MAXI", 3),
        (58972, "MAXI Durian Montong", "MAXI", 3),
        (132503, "MAXI Black Pink", "MAXI", 3),
        (74878, "MAXI Keju Cheddar", "MAXI", 3),
        (219722, "MAXI Original Lapis", "MAXI", 3)
    ]
    # Kategori DC (Dessert Cake) - Anggap saja Tier 0 (Tidak ikut aturan MAXI)
    dc_items = [
        (206125, "DC Belgian Chocolate", "DC", 0),
        (54383, "DC Black Forest", "DC", 0),
        (54386, "DC Red Velvet", "DC", 0)
    ]
    # Kategori Kemasan (Fallback system, Tier 0)
    kemasan = [
        (70867, "Plastik Bolu Klasik HD Isi 3 Box", "KEMASAN", 0),
        (137748, "Plastik Bakpia Kukus HD Isi 3 Box", "KEMASAN", 0)
    ]

    all_products = tier1 + tier2 + tier3 + dc_items + kemasan
    
    cursor.executemany("INSERT INTO products (id, nama, kategori, tier) VALUES (?, ?, ?, ?)", all_products)
    conn.commit()
    conn.close()
    print("✅ Data produk berhasil di-seed ke database.")

# --- Fungsi Helper Database ---
def save_user_credentials(telegram_id, username, password):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO users (telegram_id, username, password) 
        VALUES (?, ?, ?) 
        ON CONFLICT(telegram_id) DO UPDATE SET username=excluded.username, password=excluded.password
    ''', (telegram_id, username, password))
    conn.commit()
    conn.close()
    
def get_kategori():
    """Mengambil daftar kategori unik untuk dijadikan tombol awal."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT DISTINCT kategori FROM products")
    kategori_list = [row[0] for row in cursor.fetchall()]
    conn.close()
    return kategori_list

def get_produk_by_kategori(kategori):
    """Mengambil daftar produk berdasarkan kategori yang diklik user."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT id, nama FROM products WHERE kategori = ?", (kategori,))
    produk_list = cursor.fetchall()
    conn.close()
    return produk_list

def simpan_draft_order(telegram_id, total_maxi_qty, payload_list):
    """Menyimpan keranjang belanja akhir ke dalam database sebagai status PENDING."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    # Ubah list keranjang Python menjadi string JSON agar mudah dibaca sistem nanti
    payload_json = json.dumps(payload_list)
    
    cursor.execute('''
        INSERT INTO draft_orders (telegram_id, status, total_maxi_qty, payload_json) 
        VALUES (?, 'PENDING', ?, ?)
    ''', (telegram_id, total_maxi_qty, payload_json))
    
    conn.commit()
    conn.close()
    
def get_all_products_dict():
    """Mengambil semua produk dalam bentuk Dictionary agar cepat dicocokkan oleh bot."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT id, nama, kategori, tier FROM products")
    products = cursor.fetchall()
    conn.close()
    
    # Menghasilkan output: {"MAXI Belgian Chocolate": {"id": 251993, "kategori": "MAXI", "tier": 1}, ...}
    return {row[1]: {"id": row[0], "kategori": row[2], "tier": row[3]} for row in products}

def get_current_user(telegram_id):
    """Mengambil username/email yang sedang aktif berdasarkan telegram_id."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT username FROM users WHERE telegram_id = ?", (telegram_id,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else None

def get_pending_order(telegram_id):
    """Mengambil draf order yang berstatus PENDING."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT id, total_maxi_qty, payload_json FROM draft_orders WHERE telegram_id=? AND status='PENDING' ORDER BY id DESC LIMIT 1", (telegram_id,))
    row = cursor.fetchone()
    conn.close()
    return row

def delete_pending_order(telegram_id):
    """Menghapus draf order yang PENDING."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM draft_orders WHERE telegram_id=? AND status='PENDING'", (telegram_id,))
    conn.commit()
    conn.close()

if __name__ == "__main__":
    init_db()
    seed_products()