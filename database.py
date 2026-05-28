import sqlite3
import aiosqlite
import json
import logging
import os
from cryptography.fernet import Fernet

logger = logging.getLogger(__name__)

DB_NAME = "siliwangi_bot.db"

def _get_fernet():
    key = os.getenv("ENCRYPTION_KEY")
    if not key:
        # Jika belum ada di env, coba generate/load otomatis
        key = ensure_encryption_key()
        
    try:
        return Fernet(key.encode())
    except Exception as e:
        logger.error(f"❌ ENCRYPTION_KEY tidak valid: {e}")
        raise RuntimeError("ENCRYPTION_KEY di .env tidak valid. Silakan hapus baris tersebut agar digenerate ulang.")

def encrypt_password(plaintext: str) -> str:
    try:
        f = _get_fernet()
        return f.encrypt(plaintext.encode()).decode()
    except Exception as e:
        logger.error(f"Gagal mengenkripsi password: {e}")
        raise

def decrypt_password(encrypted: str) -> str:
    try:
        f = _get_fernet()
        return f.decrypt(encrypted.encode()).decode()
    except Exception:
        logger.warning("⚠️ Dekripsi gagal — password mungkin masih plaintext (belum dimigrasi).")
        return encrypted


def ensure_encryption_key():
    """Memastikan ENCRYPTION_KEY ada di .env. Jika tidak, buat otomatis."""
    key = os.getenv("ENCRYPTION_KEY")
    if key:
        return key

    env_path = ".env"
    
    # Cek isi file secara manual
    if os.path.exists(env_path):
        with open(env_path, "r") as f:
            for line in f:
                if line.strip().startswith("ENCRYPTION_KEY="):
                    val = line.split("=", 1)[1].strip().strip("'").strip('"')
                    if val:
                        os.environ["ENCRYPTION_KEY"] = val
                        return val

    # Generate baru
    new_key = Fernet.generate_key().decode()
    logger.info("🔑 ENCRYPTION_KEY tidak ditemukan. Membuat key baru...")
    
    mode = "a" if os.path.exists(env_path) else "w"
    with open(env_path, mode) as f:
        if mode == "a":
            f.write("\n")
        f.write(f"ENCRYPTION_KEY={new_key}\n")
    
    os.environ["ENCRYPTION_KEY"] = new_key
    return new_key

def init_db():
    # Pastikan key ada sebelum melakukan apa pun
    ensure_encryption_key()

    conn = sqlite3.connect(DB_NAME, timeout=10)
    cursor = conn.cursor()

    # Fungsi bantu migrasi kolom
    def add_column_if_missing(table, column, definition):
        try:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition};")
        except sqlite3.OperationalError:
            pass

    # ── Tabel produk
    cursor.execute("DROP TABLE IF EXISTS products")
    cursor.execute('''
        CREATE TABLE products (
            id INTEGER PRIMARY KEY,
            nama TEXT UNIQUE NOT NULL,
            kategori TEXT,
            tier INTEGER
        )
    ''')

    # Tabel users
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id TEXT,
            username TEXT,
            password TEXT,
            is_active INTEGER DEFAULT 0,
            nickname TEXT DEFAULT NULL,
            UNIQUE(telegram_id, username)
        )
    ''')

    # Tabel draft_orders
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

    # Tabel order_history
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS order_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id TEXT,
            username TEXT,
            total_maxi INTEGER,
            payload_json TEXT,
            order_id TEXT,
            status TEXT DEFAULT 'SUKSES',
            total_nominal TEXT DEFAULT '',
            tanggal TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # Tabel engine_ready_status
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS engine_ready_status (
            telegram_id TEXT,
            username TEXT,
            is_ready INTEGER DEFAULT 0,
            PRIMARY KEY (telegram_id, username)
        )
    ''')

    # Tabel sessions
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS sessions (
            telegram_id TEXT,
            username    TEXT,
            cookies_json TEXT,
            saved_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (telegram_id, username)
        )
    ''')

    # Tabel settings (Global)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    ''')

    # Migrasi kolom yang mungkin belum ada di DB lama
    add_column_if_missing("users",         "is_active",   "INTEGER DEFAULT 0")
    add_column_if_missing("users",         "nickname",    "TEXT DEFAULT NULL")
    add_column_if_missing("draft_orders",  "total_maxi",  "INTEGER")
    add_column_if_missing("draft_orders",  "created_at",  "TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
    add_column_if_missing("order_history", "order_id",    "TEXT")
    add_column_if_missing("order_history", "status",      "TEXT DEFAULT 'SUKSES'")
    add_column_if_missing("order_history", "total_nominal", "TEXT DEFAULT ''")

    # ── Data produk (single source of truth) ─────────────────────────────────
    products = [
        # === MAXI Tier 1 ===
        (251993, "MAXI Belgian Chocolate",       "MAXI", 1),
        (36124,  "MAXI Black Forest",            "MAXI", 1),
        (281180, "MAXI Cokelat Dubai Pistachio", "MAXI", 1),
        (168132, "MAXI Cokelat Tiramisu",        "MAXI", 1),
        (312,    "MAXI Brownies Coklat",         "MAXI", 1),
        # === MAXI Tier 2 ===
        (19077,  "MAXI Pandan Wangi",            "MAXI", 2),
        (24883,  "MAXI Red Velvet",              "MAXI", 2),
        (306,    "MAXI Susu Lembang",            "MAXI", 2),
        (313,    "MAXI Talas Bogor",             "MAXI", 2),
        (168131, "MAXI Durian Musang King",      "MAXI", 2),
        # === MAXI Tier 3 ===
        (311,    "MAXI Alpukat Mentega",         "MAXI", 3),
        (74878,  "MAXI Keju Cheddar",            "MAXI", 3),
        (132503, "MAXI Black Pink",              "MAXI", 3),
        (58972,  "MAXI Durian Montong",          "MAXI", 3),
        (315,    "MAXI Mangga Indramayu",        "MAXI", 3),
        (219722, "MAXI Original Lapis",          "MAXI", 3),
        # === Dessert Cake (DC)
        (206125, "DC Belgian Chocolate",         "DC",   1),
        (54383,  "DC Black Forest",              "DC",   1),
        (54386,  "DC Red Velvet",                "DC",   1),
        # === Kemasan
        (70867,  "Plastik Bolu Klasik HD Isi 3 Box",   "PLASTIK", 0),
        (137748, "Plastik Bakpia Kukus HD Isi 3 Box",  "PLASTIK", 0),
    ]

    cursor.executemany('''
        INSERT OR REPLACE INTO products (id, nama, kategori, tier)
        VALUES (?, ?, ?, ?)
    ''', products)

    conn.commit()
    conn.close()
    print("[OK] Database berhasil diinisialisasi & dimigrasi")


async def set_engine_ready_status(telegram_id: str, username: str, is_ready: bool):
    conn = await aiosqlite.connect(DB_NAME, timeout=10)
    cursor = await conn.cursor()
    await cursor.execute('''
        INSERT INTO engine_ready_status (telegram_id, username, is_ready)
        VALUES (?, ?, ?)
        ON CONFLICT(telegram_id, username) DO UPDATE SET is_ready=?
    ''', (telegram_id, username, int(is_ready), int(is_ready)))
    await conn.commit()
    await conn.close()


async def get_engine_ready_status(telegram_id: str, username: str) -> bool:
    conn = await aiosqlite.connect(DB_NAME, timeout=10)
    cursor = await conn.cursor()
    await cursor.execute("SELECT is_ready FROM engine_ready_status WHERE telegram_id=? AND username=?", (telegram_id, username))
    row = await cursor.fetchone()
    await conn.close()
    return bool(row[0]) if row else False


async def save_user_credentials(telegram_id, username, password):
    """Menyimpan kredensial dengan password terenkripsi."""
    encrypted = encrypt_password(password)
    conn = await aiosqlite.connect(DB_NAME, timeout=10)
    cursor = await conn.cursor()
    await cursor.execute("UPDATE users SET is_active = 0 WHERE telegram_id = ?", (telegram_id,))
    await cursor.execute('''
        INSERT INTO users (telegram_id, username, password, is_active)
        VALUES (?, ?, ?, 1)
        ON CONFLICT(telegram_id, username) DO UPDATE SET password=?, is_active=1
    ''', (telegram_id, username, encrypted, encrypted))
    await conn.commit()
    await conn.close()

async def get_current_user(telegram_id):
    conn = await aiosqlite.connect(DB_NAME, timeout=10)
    cursor = await conn.cursor()
    await cursor.execute("SELECT username FROM users WHERE telegram_id = ? AND is_active = 1", (telegram_id,))
    row = await cursor.fetchone()
    await conn.close()
    return row[0] if row else None

async def get_all_accounts(telegram_id):
    conn = await aiosqlite.connect(DB_NAME, timeout=10)
    cursor = await conn.cursor()
    await cursor.execute("SELECT username, is_active FROM users WHERE telegram_id = ?", (telegram_id,))
    rows = await cursor.fetchall()
    await conn.close()
    return rows

async def set_active_account(telegram_id, target_username):
    conn = await aiosqlite.connect(DB_NAME, timeout=10)
    cursor = await conn.cursor()
    await cursor.execute("UPDATE users SET is_active = 0 WHERE telegram_id = ?", (telegram_id,))
    await cursor.execute("UPDATE users SET is_active = 1 WHERE telegram_id = ? AND username = ?", (telegram_id, target_username))
    await conn.commit()
    await conn.close()


# ============================================================
# SESSION MANAGEMENT (cookies per akun)
# ============================================================

async def save_session_cookies(telegram_id: str, username: str, cookies: dict):
    """Simpan cookies httpx ke DB agar bot tidak perlu login ulang setelah restart."""
    conn = await aiosqlite.connect(DB_NAME, timeout=10)
    cursor = await conn.cursor()
    await cursor.execute('''
        INSERT OR REPLACE INTO sessions (telegram_id, username, cookies_json, saved_at)
        VALUES (?, ?, ?, CURRENT_TIMESTAMP)
    ''', (telegram_id, username, json.dumps(cookies)))
    await conn.commit()
    await conn.close()

async def load_session_cookies(telegram_id: str, username: str) -> dict:
    """Ambil cookies tersimpan. Return dict kosong jika belum ada."""
    conn = await aiosqlite.connect(DB_NAME, timeout=10)
    cursor = await conn.cursor()
    await cursor.execute(
        "SELECT cookies_json FROM sessions WHERE telegram_id=? AND username=?",
        (telegram_id, username)
    )
    row = await cursor.fetchone()
    await conn.close()
    if row and row[0]:
        try:
            return json.loads(row[0])
        except Exception:
            return {}
    return {}

async def clear_session_cookies(telegram_id: str, username: str):
    """Hapus session (saat logout manual atau session expired)."""
    conn = await aiosqlite.connect(DB_NAME, timeout=10)
    cursor = await conn.cursor()
    await cursor.execute(
        "DELETE FROM sessions WHERE telegram_id=? AND username=?",
        (telegram_id, username)
    )
    await conn.commit()
    await conn.close()

async def get_session_status(telegram_id: str, username: str) -> bool:
    """Cek apakah akun punya session cookies tersimpan."""
    cookies = load_session_cookies(telegram_id, username)
    return bool(cookies)

async def get_all_accounts_with_status(telegram_id: str) -> list:
    """
    Return list of (username, is_active, has_session, nickname).
    Digunakan di menu akun untuk tampilkan status login setiap akun.
    """
    conn = await aiosqlite.connect(DB_NAME, timeout=10)
    cursor = await conn.cursor()
    await cursor.execute(
        "SELECT username, is_active, COALESCE(nickname, '') FROM users WHERE telegram_id = ? ORDER BY is_active DESC, username ASC",
        (telegram_id,)
    )
    rows = await cursor.fetchall()
    # Cek session per akun
    await cursor.execute(
        "SELECT username FROM sessions WHERE telegram_id = ?",
        (telegram_id,)
    )
    session_users = {r[0] for r in await cursor.fetchall()}
    await conn.close()
    return [(u, is_active, u in session_users, nick) for u, is_active, nick in rows]

async def count_accounts(telegram_id: str) -> int:
    """Hitung jumlah akun terdaftar."""
    conn = await aiosqlite.connect(DB_NAME, timeout=10)
    cursor = await conn.cursor()
    await cursor.execute("SELECT COUNT(*) FROM users WHERE telegram_id=?", (telegram_id,))
    row = await cursor.fetchone()
    await conn.close()
    return row[0] if row else 0


async def get_account_nickname(telegram_id: str, username: str) -> str:
    """Ambil nickname akun. Return string kosong jika belum diset."""
    conn = await aiosqlite.connect(DB_NAME, timeout=10)
    cursor = await conn.cursor()
    await cursor.execute(
        "SELECT COALESCE(nickname, '') FROM users WHERE telegram_id=? AND username=?",
        (telegram_id, username)
    )
    row = await cursor.fetchone()
    await conn.close()
    return row[0] if row else ''


async def update_account_nickname(telegram_id: str, username: str, nickname: str) -> None:
    """Simpan atau perbarui nickname untuk akun."""
    conn = await aiosqlite.connect(DB_NAME, timeout=10)
    cursor = await conn.cursor()
    await cursor.execute(
        "UPDATE users SET nickname=? WHERE telegram_id=? AND username=?",
        (nickname.strip(), telegram_id, username)
    )
    await conn.commit()
    await conn.close()


async def delete_account(telegram_id: str, username: str) -> None:
    """Hapus akun beserta seluruh data terkait (session, draft, engine status)."""
    conn = await aiosqlite.connect(DB_NAME, timeout=10)
    cursor = await conn.cursor()
    await cursor.execute("DELETE FROM users WHERE telegram_id=? AND username=?", (telegram_id, username))
    await cursor.execute("DELETE FROM sessions WHERE telegram_id=? AND username=?", (telegram_id, username))
    await cursor.execute("DELETE FROM draft_orders WHERE telegram_id=? AND username=?", (telegram_id, username))
    await cursor.execute("DELETE FROM engine_ready_status WHERE telegram_id=? AND username=?", (telegram_id, username))
    await conn.commit()
    await conn.close()

# ============================================================
# CRUD DRAFT ORDERS
# ============================================================

async def simpan_draft_order(telegram_id, total_maxi, keranjang):
    """
    Simpan draf pesanan untuk akun aktif.
    Selalu REPLACE — 1 akun hanya boleh punya 1 draf PENDING.
    """
    active_user = get_current_user(telegram_id)
    if not active_user:
        return False
    conn = await aiosqlite.connect(DB_NAME, timeout=10)
    cursor = await conn.cursor()
    # Hapus semua pending lama untuk akun ini dulu (aman, atomik)
    await cursor.execute(
        "DELETE FROM draft_orders WHERE telegram_id=? AND username=? AND status='PENDING'",
        (telegram_id, active_user)
    )
    await cursor.execute('''
        INSERT INTO draft_orders (telegram_id, username, total_maxi, payload_json)
        VALUES (?, ?, ?, ?)
    ''', (telegram_id, active_user, total_maxi, json.dumps(keranjang)))
    await conn.commit()
    await conn.close()
    return True

async def get_pending_order(telegram_id):
    """Ambil draf PENDING terbaru akun aktif, beserta info tanggalnya."""
    active_user = get_current_user(telegram_id)
    if not active_user:
        return None
    conn = await aiosqlite.connect(DB_NAME, timeout=10)
    cursor = await conn.cursor()
    await cursor.execute('''
        SELECT id, total_maxi, payload_json,
               datetime(created_at, 'localtime') as tgl_buat
        FROM draft_orders
        WHERE telegram_id = ? AND username = ? AND status = 'PENDING'
        ORDER BY id DESC LIMIT 1
    ''', (telegram_id, active_user))
    row = await cursor.fetchone()
    await conn.close()
    return row  # (id, total_maxi, payload_json, tgl_buat)

async def delete_pending_order(telegram_id):
    active_user = get_current_user(telegram_id)
    if not active_user:
        return
    conn = await aiosqlite.connect(DB_NAME, timeout=10)
    cursor = await conn.cursor()
    await cursor.execute('''
        DELETE FROM draft_orders
        WHERE telegram_id = ? AND username = ? AND status = 'PENDING'
    ''', (telegram_id, active_user))
    await conn.commit()
    await conn.close()

async def get_all_pending_orders_multi(telegram_id):
    """
    Ambil 1 draf PENDING terbaru PER AKUN.
    Aman dari duplikat: jika ada 2 draf untuk 1 akun, ambil yang terbaru.
    """
    conn = await aiosqlite.connect(DB_NAME, timeout=10)
    cursor = await conn.cursor()
    await cursor.execute('''
        SELECT d.id, d.username, d.payload_json
        FROM draft_orders d
        INNER JOIN (
            SELECT username, MAX(id) as max_id
            FROM draft_orders
            WHERE telegram_id = ? AND status = 'PENDING'
            GROUP BY username
        ) latest ON d.id = latest.max_id
        WHERE d.telegram_id = ?
        ORDER BY d.id ASC
    ''', (telegram_id, telegram_id))
    rows = await cursor.fetchall()
    await conn.close()
    return rows

async def cleanup_all_pending_orders(telegram_id):
    """
    Cleanup jam 09:00: hapus draf yang dibuat SEBELUM jam 08:00 hari ini.
    Aman: draf yang diinput setelah jam 08:00 (untuk besok) TIDAK dihapus.
    """
    conn = await aiosqlite.connect(DB_NAME, timeout=10)
    cursor = await conn.cursor()
    # Hanya hapus draf yang dibuat sebelum jam 08:00 WIB hari ini
    # 'localtime' di SQLite = UTC+0, jadi 08:00 WIB = 01:00 UTC
    await cursor.execute('''
        DELETE FROM draft_orders
        WHERE telegram_id = ? AND status = 'PENDING'
        AND created_at < datetime('now', 'start of day', '+1 hours')
    ''', (telegram_id,))
    deleted = cursor.rowcount
    await conn.commit()
    await conn.close()
    return deleted

async def get_all_drafts_overview(telegram_id: str) -> list:
    """
    Untuk menu kelola: ambil status draf semua akun.
    Return: list of (username, is_active, has_draft, total_maxi, tgl_buat, nickname)
    """
    conn = await aiosqlite.connect(DB_NAME, timeout=10)
    cursor = await conn.cursor()
    # Semua akun terdaftar
    await cursor.execute(
        "SELECT username, is_active, COALESCE(nickname, '') FROM users WHERE telegram_id=? ORDER BY is_active DESC, username ASC",
        (telegram_id,)
    )
    accounts = await cursor.fetchall()

    # Draf pending terbaru per akun
    await cursor.execute('''
        SELECT d.username, d.total_maxi, datetime(d.created_at, 'localtime')
        FROM draft_orders d
        INNER JOIN (
            SELECT username, MAX(id) as max_id
            FROM draft_orders
            WHERE telegram_id = ? AND status = 'PENDING'
            GROUP BY username
        ) latest ON d.id = latest.max_id
        WHERE d.telegram_id = ?
    ''', (telegram_id, telegram_id))
    drafts = {row[0]: (row[1], row[2]) for row in await cursor.fetchall()}
    await conn.close()

    result = []
    for username, is_active, nickname in accounts:
        if username in drafts:
            total_maxi, tgl_buat = drafts[username]
            result.append((username, is_active, True, total_maxi, tgl_buat, nickname))
        else:
            result.append((username, is_active, False, 0, None, nickname))
    return result


# ============================================================
# PRODUK
# ============================================================

async def get_all_products_dict():
    conn = await aiosqlite.connect(DB_NAME, timeout=10)
    cursor = await conn.cursor()
    await cursor.execute("SELECT id, nama, kategori, tier FROM products")
    rows = await cursor.fetchall()
    await conn.close()
    products_db = {}
    for r in rows:
        products_db[r[1]] = {"id": r[0], "kategori": r[2], "tier": r[3]}
    return products_db


# ============================================================
# RIWAYAT ORDER
# ============================================================

async def get_order_history_dates(telegram_id: str) -> list:
    """
    Ambil 5 tanggal unik terbaru yang punya riwayat order (semua akun).
    Return: list of date strings 'YYYY-MM-DD', urut terbaru dulu.
    """
    conn = await aiosqlite.connect(DB_NAME, timeout=10)
    cursor = await conn.cursor()
    await cursor.execute('''
        SELECT DISTINCT date(tanggal, 'localtime') as tgl
        FROM order_history
        WHERE telegram_id = ?
        ORDER BY tgl DESC
        LIMIT 5
    ''', (telegram_id,))
    rows = await cursor.fetchall()
    await conn.close()
    return [r[0] for r in rows]


async def get_order_history_by_date(telegram_id: str, date_str: str) -> list:
    """
    Ambil semua order pada tanggal tertentu (semua akun).
    Return: list of (jam, username, total_maxi, payload_json, order_id, status, total_nominal)
    """
    conn = await aiosqlite.connect(DB_NAME, timeout=10)
    cursor = await conn.cursor()
    await cursor.execute('''
        SELECT
            time(tanggal, 'localtime')      as jam,
            username,
            total_maxi,
            payload_json,
            COALESCE(order_id, 'N/A')       as order_id,
            COALESCE(status, 'SUKSES')      as status,
            COALESCE(total_nominal, '')     as total_nominal
        FROM order_history
        WHERE telegram_id = ?
          AND date(tanggal, 'localtime') = ?
        ORDER BY tanggal ASC
    ''', (telegram_id, date_str))
    rows = await cursor.fetchall()
    await conn.close()
    return rows


async def save_failed_order(telegram_id: str, username: str, total_maxi: int,
                      payload_json: str, reason: str):
    """Simpan riwayat order GAGAL ke order_history."""
    conn = await aiosqlite.connect(DB_NAME, timeout=10)
    cursor = await conn.cursor()
    try:
        await cursor.execute('''
            INSERT INTO order_history
                (telegram_id, username, total_maxi, payload_json, order_id, status, total_nominal)
            VALUES (?, ?, ?, ?, 'N/A', 'GAGAL', ?)
        ''', (telegram_id, username, total_maxi, payload_json, reason[:120]))
        await conn.commit()
    except Exception as e:
        logger.error(f"save_failed_order error: {e}")
    finally:
        await conn.close()


async def get_order_history(telegram_id, username):
    """Legacy — ambil riwayat per akun, maks 20 entri terakhir."""
    conn = await aiosqlite.connect(DB_NAME, timeout=10)
    cursor = await conn.cursor()
    await cursor.execute('''
        SELECT datetime(tanggal, 'localtime'), total_maxi, payload_json,
               COALESCE(status, 'SUKSES') as status
        FROM order_history
        WHERE telegram_id=? AND username=?
        ORDER BY id DESC LIMIT 20
    ''', (telegram_id, username))
    rows = await cursor.fetchall()
    await conn.close()
    return rows

# ============================================================
# GLOBAL SETTINGS
# ============================================================

async def get_setting(key: str, default_value: str = "") -> str:
    conn = await aiosqlite.connect(DB_NAME, timeout=10)
    cursor = await conn.cursor()
    await cursor.execute("SELECT value FROM settings WHERE key=?", (key,))
    row = await cursor.fetchone()
    await conn.close()
    return row[0] if row else default_value

async def set_setting(key: str, value: str):
    conn = await aiosqlite.connect(DB_NAME, timeout=10)
    cursor = await conn.cursor()
    await cursor.execute('''
        INSERT INTO settings (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value=?
    ''', (key, value, value))
    await conn.commit()
    await conn.close()


if __name__ == "__main__":
    init_db()
