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
    # 1. telegram_id tidak lagi UNIQUE sendirian, melainkan kombinasi (telegram_id + username)
    # 2. Tambah kolom is_active untuk penanda "🟢 Akun Aktif Saat Ini"
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
    # Ditambahkan kolom "username" agar draf menempel pada akun Siliwanginya, bukan Telegram ID
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
    print("✅ Database V2.0 berhasil diinisialisasi (Support Multi-Account)")

# ==============================================================
# FUNGSI LAMA (Tetap ada agar bot.py/engine.py yang lama tidak rusak)
# Internalnya disesuaikan untuk membaca kolom V2.0 secara otomatis
# ==============================================================

def save_user_credentials(telegram_id, username, password):
    """Menyimpan akun. Jika baru ditambahkan, langsung jadikan Akun Aktif."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    # Matikan semua akun lain yang sedang aktif
    cursor.execute("UPDATE users SET is_active = 0 WHERE telegram_id = ?", (telegram_id,))
    # Masukkan akun baru (atau update password jika sudah ada), dan jadikan aktif
    cursor.execute('''
        INSERT INTO users (telegram_id, username, password, is_active)
        VALUES (?, ?, ?, 1)
        ON CONFLICT(telegram_id, username) DO UPDATE SET password=?, is_active=1
    ''', (telegram_id, username, password, password))
    conn.commit()
    conn.close()

def get_current_user(telegram_id):
    """Membaca akun mana yang sedang aktif saat ini"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT username FROM users WHERE telegram_id = ? AND is_active = 1", (telegram_id,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else None

def simpan_draft_order(telegram_id, total_maxi, keranjang):
    """Menyimpan draf pesanan langsung ke akun yang sedang aktif"""
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
    """Mengambil draf pesanan milik akun yang sedang aktif"""
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
    """Menghapus draf pesanan milik akun yang sedang aktif"""
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
# FUNGSI BARU KHUSUS VERSI 2.0 (MULTI-ACCOUNT)
# (Akan dipanggil oleh bot.py dan engine.py yang baru di Fase selanjutnya)
# ==============================================================

def get_all_accounts(telegram_id):
    """Mengambil daftar semua akun yang tersimpan untuk UI Telegram"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT username, is_active FROM users WHERE telegram_id = ?", (telegram_id,))
    rows = cursor.fetchall()
    conn.close()
    return rows # Format: [('putri@email.com', 1), ('ayu@email.com', 0)]

def set_active_account(telegram_id, target_username):
    """Memindah '🟢 Akun Aktif' ke akun pilihan"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET is_active = 0 WHERE telegram_id = ?", (telegram_id,))
    cursor.execute("UPDATE users SET is_active = 1 WHERE telegram_id = ? AND username = ?", (telegram_id, target_username))
    conn.commit()
    conn.close()

def get_all_pending_orders_multi(telegram_id):
    """(Untuk Fase 4) Mengambil semua draf PENDING dari SEMUA AKUN sekaligus"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT id, username, payload_json FROM draft_orders 
        WHERE telegram_id = ? AND status = 'PENDING'
    ''', (telegram_id,))
    rows = cursor.fetchall()
    conn.close()
    return rows

if __name__ == "__main__":
    init_db()