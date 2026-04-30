import sqlite3
import json
import logging

logger = logging.getLogger(__name__)

DB_NAME = "siliwangi_bot.db"

def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    # Tabel produk (TETAP SAMA 100%)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY,
            nama TEXT UNIQUE,
            kategori TEXT,
            tier INTEGER
        )
    ''')

    # [PEROMBAKAN V2] Tabel users:
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id TEXT,
            username TEXT,
            password TEXT,
            is_active INTEGER DEFAULT 0,
            UNIQUE(telegram_id, username)
        )
    ''')

    # [PEROMBAKAN V2] Tabel draft_orders:
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS draft_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id TEXT,
            username TEXT,
            total_maxi INTEGER,
            payload_json TEXT,
            status TEXT DEFAULT 'PENDING',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # [FITUR BARU V2.1] Tabel Riwayat Order:
    # Untuk mencatat pesanan yang sukses di-checkout
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS order_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id TEXT,
            username TEXT,
            total_maxi INTEGER,
            payload_json TEXT,
            tanggal TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # --- PENGISIAN DATA PRODUK (TETAP SAMA 100%) ---
    products = [
        # MAXI Tier 1
        (13463, "MAXI Belgian Chocolate", "MAXI", 1),
        (13465, "MAXI Black Forest", "MAXI", 1),
        (227187, "MAXI Cokelat Dubai Pistachio", "MAXI", 1),
        (227188, "MAXI Cokelat Tiramisu", "MAXI", 1),
        # MAXI Tier 2
        (13479, "MAXI Brownies Coklat", "MAXI", 2),
        (13476, "MAXI Susu Lembang", "MAXI", 2),
        (13471, "MAXI Alpukat Mentega", "MAXI", 2),
        (13478, "MAXI Talas Bogor", "MAXI", 2),
        # MAXI Tier 3
        (13467, "MAXI Pandan Wangi", "MAXI", 3),
        (13469, "MAXI Red Velvet", "MAXI", 3),
        (13473, "MAXI Keju Cheddar", "MAXI", 3),
        (210722, "MAXI Black Pink", "MAXI", 3),
        (177113, "MAXI Durian Montong", "MAXI", 3),
        (205949, "MAXI Durian Musang King", "MAXI", 3),
        (13475, "MAXI Mangga Indramayu", "MAXI", 3),
        (219754, "MAXI Original Lapis", "MAXI", 3),
        # Dessert Cake (DC) - Tier 1
        (65017, "DC Belgian Chocolate", "DC", 1),
        (65022, "DC Black Forest", "DC", 1),
        # Plastik
        (85918, "Plastik Bolu Klasik HD Isi 3 Box", "PLASTIK", 0),
        (85922, "Plastik Bakpia Kukus HD Isi 3 Box", "PLASTIK", 0)
    ]
    
    cursor.executemany('''
        INSERT OR IGNORE INTO products (id, nama, kategori, tier) 
        VALUES (?, ?, ?, ?)
    ''', products)

    conn.commit()
    conn.close()
    print("✅ Database V2.0 berhasil diinisialisasi (Support Multi-Account & Riwayat)")

# ==============================================================
# FUNGSI LAMA
# ==============================================================

def save_user_credentials(telegram_id, username, password):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET is_active = 0 WHERE telegram_id = ?", (telegram_id,))
    cursor.execute('''
        INSERT INTO users (telegram_id, username, password, is_active)
        VALUES (?, ?, ?, 1)
        ON CONFLICT(telegram_id, username) DO UPDATE SET password=?, is_active=1
    ''', (telegram_id, username, password, password))
    conn.commit()
    conn.close()

def get_current_user(telegram_id):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT username FROM users WHERE telegram_id = ? AND is_active = 1", (telegram_id,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else None

def simpan_draft_order(telegram_id, total_maxi, keranjang):
    active_user = get_current_user(telegram_id)
    if not active_user: return False

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO draft_orders (telegram_id, username, total_maxi, payload_json)
        VALUES (?, ?, ?, ?)
    ''', (telegram_id, active_user, total_maxi, json.dumps(keranjang)))
    conn.commit()
    conn.close()
    return True

def get_pending_order(telegram_id):
    active_user = get_current_user(telegram_id)
    if not active_user: return None

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT id, total_maxi, payload_json FROM draft_orders 
        WHERE telegram_id = ? AND username = ? AND status = 'PENDING' 
        ORDER BY id DESC LIMIT 1
    ''', (telegram_id, active_user))
    row = cursor.fetchone()
    conn.close()
    return row

def delete_pending_order(telegram_id):
    active_user = get_current_user(telegram_id)
    if not active_user: return

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        DELETE FROM draft_orders 
        WHERE telegram_id = ? AND username = ? AND status = 'PENDING'
    ''', (telegram_id, active_user))
    conn.commit()
    conn.close()

def get_all_products_dict():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT id, nama, kategori, tier FROM products")
    rows = cursor.fetchall()
    conn.close()
    
    products_db = {}
    for r in rows:
        products_db[r[1]] = {"id": r[0], "kategori": r[2], "tier": r[3]}
    return products_db

# ==============================================================
# FUNGSI BARU KHUSUS VERSI 2.0 (MULTI-ACCOUNT & RIWAYAT)
# ==============================================================

def get_all_accounts(telegram_id):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT username, is_active FROM users WHERE telegram_id = ?", (telegram_id,))
    rows = cursor.fetchall()
    conn.close()
    return rows

def set_active_account(telegram_id, target_username):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET is_active = 0 WHERE telegram_id = ?", (telegram_id,))
    cursor.execute("UPDATE users SET is_active = 1 WHERE telegram_id = ? AND username = ?", (telegram_id, target_username))
    conn.commit()
    conn.close()

def get_all_pending_orders_multi(telegram_id):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT id, username, payload_json FROM draft_orders 
        WHERE telegram_id = ? AND status = 'PENDING'
    ''', (telegram_id,))
    rows = cursor.fetchall()
    conn.close()
    return rows

def get_order_history(telegram_id, username):
    """Mengambil riwayat sukses maksimal 3 terakhir"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT datetime(tanggal, 'localtime'), total_maxi, payload_json 
        FROM order_history 
        WHERE telegram_id=? AND username=? 
        ORDER BY id DESC LIMIT 3
    ''', (telegram_id, username))
    rows = cursor.fetchall()
    conn.close()
    return rows

if __name__ == "__main__":
    init_db()