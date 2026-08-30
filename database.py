import sqlite3
import aiosqlite
import json
import logging
import os
from datetime import datetime, timedelta, time as dt_time
from cryptography.fernet import Fernet
import pytz

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_NAME = os.path.join(BASE_DIR, "siliwangi_bot.db")
ENV_PATH = os.path.join(BASE_DIR, ".env")
ENCRYPTED_PREFIX = "fernet:"
WIB = pytz.timezone("Asia/Jakarta")

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


def encrypt_secret_value(plaintext: str) -> str:
    """Enkripsi data sensitif non-password dengan marker agar bisa dibedakan dari data lama."""
    if plaintext.startswith(ENCRYPTED_PREFIX):
        return plaintext
    f = _get_fernet()
    return ENCRYPTED_PREFIX + f.encrypt(plaintext.encode()).decode()


def decrypt_secret_value(value: str) -> str:
    """Dekripsi data bertanda fernet:, fallback ke plaintext lama agar backward-compatible."""
    if not value or not value.startswith(ENCRYPTED_PREFIX):
        return value
    token = value[len(ENCRYPTED_PREFIX):]
    try:
        f = _get_fernet()
        return f.decrypt(token.encode()).decode()
    except Exception as e:
        logger.warning(f"⚠️ Dekripsi secret gagal: {e}")
        return ""


def ensure_encryption_key():
    """Memastikan ENCRYPTION_KEY ada di .env. Jika tidak, buat otomatis."""
    key = os.getenv("ENCRYPTION_KEY")
    if key:
        return key

    env_path = ENV_PATH
    
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
        cursor.execute(f"PRAGMA table_info({table})")
        columns = {row[1] for row in cursor.fetchall()}
        if column not in columns:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition};")

    # ── Tabel produk
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS products (
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
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            war_date TEXT,
            attempt_id TEXT,
            order_id TEXT,
            last_error TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
            tanggal TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            draft_id INTEGER,
            attempt_id TEXT
        )
    ''')

    # Tabel draft_history (Log historis semua draf pesanan)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS draft_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id TEXT,
            username TEXT,
            total_maxi INTEGER,
            payload_json TEXT,
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
    add_column_if_missing("draft_orders",  "war_date",    "TEXT")
    add_column_if_missing("draft_orders",  "attempt_id",  "TEXT")
    add_column_if_missing("draft_orders",  "order_id",    "TEXT")
    add_column_if_missing("draft_orders",  "last_error",  "TEXT")
    add_column_if_missing("draft_orders",  "updated_at",  "TIMESTAMP")
    add_column_if_missing("order_history", "order_id",    "TEXT")
    add_column_if_missing("order_history", "status",      "TEXT DEFAULT 'SUKSES'")
    add_column_if_missing("order_history", "total_nominal", "TEXT DEFAULT ''")
    add_column_if_missing("order_history", "draft_id",    "INTEGER")
    add_column_if_missing("order_history", "attempt_id",  "TEXT")

    # Legacy rows receive a deterministic date so they can be expired safely.
    cursor.execute("""
        UPDATE draft_orders
        SET war_date = date(created_at), updated_at = COALESCE(updated_at, created_at)
        WHERE war_date IS NULL
    """)

    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_draft_orders_lookup
        ON draft_orders (telegram_id, status, war_date, username)
    """)
    cursor.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_order_history_draft_success
        ON order_history (draft_id)
        WHERE draft_id IS NOT NULL AND status = 'SUKSES'
    """)

    # Session lama disimpan sebagai JSON plaintext. Enkripsi seluruh row
    # sebelum bot mulai menggunakannya. Row rusak dikarantina sebagai [] agar
    # bearer-cookie tidak tersisa plaintext di database.
    cursor.execute("SELECT telegram_id, username, cookies_json FROM sessions")
    for telegram_id, username, cookies_json in cursor.fetchall():
        if not cookies_json or cookies_json.startswith(ENCRYPTED_PREFIX):
            continue
        try:
            parsed = json.loads(cookies_json)
            if not isinstance(parsed, (dict, list)):
                raise ValueError("format cookie bukan object/list")
            encrypted = encrypt_secret_value(json.dumps(parsed))
        except Exception as exc:
            logger.error(
                "Session legacy rusak dikarantina untuk telegram_id=%s username=%s: %s",
                telegram_id, username, exc,
            )
            encrypted = encrypt_secret_value("[]")
        cursor.execute(
            """
            UPDATE sessions SET cookies_json=?
            WHERE telegram_id=? AND username=?
            """,
            (encrypted, telegram_id, username),
        )

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

    # Cleanup history older than 5 days
    cursor.execute("DELETE FROM order_history WHERE datetime(tanggal) < datetime('now', '-5 days')")
    cursor.execute("DELETE FROM draft_history WHERE datetime(tanggal) < datetime('now', '-5 days')")

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


def get_current_war_date(now=None) -> str:
    """Tanggal war lokal WIB untuk draft baru atau scheduler hari ini."""
    current = now.astimezone(WIB) if now else datetime.now(WIB)
    # Input setelah jam war diarahkan ke war berikutnya.
    if current.time() >= dt_time(8, 0):
        current += timedelta(days=1)
    return current.date().isoformat()


def get_today_wib_date(now=None) -> str:
    """Tanggal kalender hari ini di WIB, tanpa menggeser ke hari berikutnya."""
    current = now.astimezone(WIB) if now else datetime.now(WIB)
    return current.date().isoformat()


async def expire_stale_pending_orders(
    telegram_id: str,
    war_date: str,
    max_age_hours: int = 48,
) -> int:
    """Karantina draft lama agar tidak pernah dieksekusi otomatis."""
    conn = await aiosqlite.connect(DB_NAME, timeout=10)
    cursor = await conn.cursor()
    try:
        await cursor.execute(
            f"""
            UPDATE draft_orders
            SET status='EXPIRED',
                last_error='Draft melewati tanggal war atau batas usia.',
                updated_at=CURRENT_TIMESTAMP
            WHERE telegram_id=?
              AND status='PENDING'
              AND (
                    (war_date IS NOT NULL AND war_date < ?)
                    OR (war_date IS NULL AND created_at < datetime('now', '-{int(max_age_hours)} hours'))
                  )
            """,
            (telegram_id, war_date),
        )
        expired = cursor.rowcount
        await conn.commit()
        return expired
    except Exception:
        await conn.rollback()
        raise
    finally:
        await conn.close()


async def recover_stale_running_orders(
    telegram_id: str,
    stale_minutes: int = 30,
) -> int:
    """Tandai attempt RUNNING lama sebagai UNKNOWN, bukan PENDING."""
    conn = await aiosqlite.connect(DB_NAME, timeout=10)
    cursor = await conn.cursor()
    try:
        await cursor.execute(
            f"""
            UPDATE draft_orders
            SET status='UNKNOWN',
                last_error='Attempt sebelumnya terputus; status remote perlu diverifikasi.',
                updated_at=CURRENT_TIMESTAMP
            WHERE telegram_id=?
              AND status='RUNNING'
              AND updated_at < datetime('now', '-{int(stale_minutes)} minutes')
            """,
            (telegram_id,),
        )
        recovered = cursor.rowcount
        await conn.commit()
        return recovered
    except Exception:
        await conn.rollback()
        raise
    finally:
        await conn.close()


async def get_pending_order_for_account(
    telegram_id: str,
    username: str,
    war_date: str | None = None,
):
    """Ambil draft pending terbaru untuk akun tertentu."""
    conn = await aiosqlite.connect(DB_NAME, timeout=10)
    cursor = await conn.cursor()
    try:
        query = """
            SELECT id, total_maxi, payload_json,
                   datetime(created_at, 'localtime'), war_date
            FROM draft_orders
            WHERE telegram_id=? AND username=? AND status='PENDING'
        """
        params = [telegram_id, username]
        if war_date is not None:
            query += " AND war_date=?"
            params.append(war_date)
        query += " ORDER BY id DESC LIMIT 1"
        await cursor.execute(query, tuple(params))
        return await cursor.fetchone()
    finally:
        await conn.close()


async def claim_draft_order(
    draft_id: int,
    telegram_id: str,
    username: str,
    attempt_id: str,
) -> bool:
    """Claim atomik; hanya satu worker boleh memproses draft pending."""
    conn = await aiosqlite.connect(DB_NAME, timeout=10)
    cursor = await conn.cursor()
    try:
        await cursor.execute(
            """
            UPDATE draft_orders
            SET status='RUNNING', attempt_id=?, updated_at=CURRENT_TIMESTAMP,
                last_error=NULL
            WHERE id=? AND telegram_id=? AND username=? AND status='PENDING'
            """,
            (attempt_id, draft_id, telegram_id, username),
        )
        claimed = cursor.rowcount == 1
        await conn.commit()
        return claimed
    except Exception:
        await conn.rollback()
        raise
    finally:
        await conn.close()


async def mark_order_success(
    draft_id: int,
    telegram_id: str,
    username: str,
    attempt_id: str,
    order_id: str,
    total_nominal: str = "",
) -> bool:
    """Finalisasi sukses remote + lokal secara idempotent dalam satu transaksi."""
    conn = await aiosqlite.connect(DB_NAME, timeout=10)
    cursor = await conn.cursor()
    try:
        await cursor.execute(
            "SELECT status, payload_json, total_maxi FROM draft_orders WHERE id=? AND telegram_id=? AND username=?",
            (draft_id, telegram_id, username),
        )
        row = await cursor.fetchone()
        if not row:
            raise RuntimeError("Draft tidak ditemukan saat finalisasi sukses")
        if row[0] == "SUCCESS":
            await conn.commit()
            return True
        if row[0] not in {"RUNNING", "PENDING"}:
            raise RuntimeError(f"Draft berada pada status {row[0]}, bukan RUNNING")

        await cursor.execute(
            """
            INSERT INTO order_history
                (telegram_id, username, total_maxi, payload_json,
                 order_id, status, total_nominal, draft_id, attempt_id)
            VALUES (?, ?, ?, ?, ?, 'SUKSES', ?, ?, ?)
            """,
            (
                telegram_id, username, row[2], row[1], order_id,
                total_nominal, draft_id, attempt_id,
            ),
        )
        await cursor.execute(
            """
            UPDATE draft_orders
            SET status='SUCCESS', order_id=?, last_error=NULL,
                updated_at=CURRENT_TIMESTAMP
            WHERE id=? AND telegram_id=? AND username=?
              AND status IN ('RUNNING', 'PENDING')
            """,
            (order_id, draft_id, telegram_id, username),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("Draft berubah sebelum finalisasi sukses")
        await conn.commit()
        return True
    except Exception:
        await conn.rollback()
        raise
    finally:
        await conn.close()


async def mark_order_failed(
    draft_id: int,
    telegram_id: str,
    username: str,
    attempt_id: str,
    reason: str,
) -> bool:
    """Tandai kegagalan yang sudah pasti; tidak akan diambil oleh war berikutnya."""
    conn = await aiosqlite.connect(DB_NAME, timeout=10)
    cursor = await conn.cursor()
    try:
        await cursor.execute(
            "SELECT payload_json, total_maxi, status FROM draft_orders WHERE id=? AND telegram_id=? AND username=?",
            (draft_id, telegram_id, username),
        )
        row = await cursor.fetchone()
        if not row:
            raise RuntimeError("Draft tidak ditemukan saat finalisasi gagal")
        if row[2] == "FAILED":
            await conn.commit()
            return True
        if row[2] in {"SUCCESS", "UNKNOWN", "EXPIRED"}:
            return False
        reason_short = reason[:120]
        await cursor.execute(
            """
            UPDATE draft_orders
            SET status='FAILED', last_error=?, updated_at=CURRENT_TIMESTAMP
            WHERE id=? AND telegram_id=? AND username=?
              AND status IN ('RUNNING', 'PENDING')
            """,
            (reason_short, draft_id, telegram_id, username),
        )
        if cursor.rowcount != 1:
            await conn.rollback()
            return False
        await cursor.execute(
            """
            INSERT INTO order_history
                (telegram_id, username, total_maxi, payload_json,
                 order_id, status, total_nominal, draft_id, attempt_id)
            VALUES (?, ?, ?, ?, 'N/A', 'GAGAL', ?, ?, ?)
            """,
            (telegram_id, username, row[1], row[0], reason_short, draft_id, attempt_id),
        )
        await conn.commit()
        return True
    except Exception:
        await conn.rollback()
        raise
    finally:
        await conn.close()


async def mark_order_unknown(
    draft_id: int,
    telegram_id: str,
    username: str,
    attempt_id: str,
    reason: str,
) -> bool:
    """Tandai hasil ambigu agar tidak pernah di-retry otomatis."""
    conn = await aiosqlite.connect(DB_NAME, timeout=10)
    cursor = await conn.cursor()
    try:
        await cursor.execute(
            "SELECT payload_json, total_maxi, status FROM draft_orders WHERE id=? AND telegram_id=? AND username=?",
            (draft_id, telegram_id, username),
        )
        row = await cursor.fetchone()
        if not row:
            raise RuntimeError("Draft tidak ditemukan saat finalisasi UNKNOWN")
        if row[2] == "UNKNOWN":
            await conn.commit()
            return True
        if row[2] == "SUCCESS":
            await conn.commit()
            return True
        reason_short = reason[:120]
        await cursor.execute(
            """
            UPDATE draft_orders
            SET status='UNKNOWN', last_error=?, updated_at=CURRENT_TIMESTAMP
            WHERE id=? AND telegram_id=? AND username=?
              AND status IN ('RUNNING', 'PENDING')
            """,
            (reason_short, draft_id, telegram_id, username),
        )
        if cursor.rowcount != 1:
            await conn.rollback()
            return False
        await cursor.execute(
            """
            INSERT INTO order_history
                (telegram_id, username, total_maxi, payload_json,
                 order_id, status, total_nominal, draft_id, attempt_id)
            VALUES (?, ?, ?, ?, 'UNKNOWN', 'UNKNOWN', ?, ?, ?)
            """,
            (telegram_id, username, row[1], row[0], reason_short, draft_id, attempt_id),
        )
        await conn.commit()
        return True
    except Exception:
        await conn.rollback()
        raise
    finally:
        await conn.close()


async def get_draft_history_dates(telegram_id: str) -> list:
    """Ambil daftar tanggal unik dari riwayat DRAF (5 hari terakhir)."""
    conn = await aiosqlite.connect(DB_NAME, timeout=10)
    cursor = await conn.cursor()
    await cursor.execute('''
        SELECT DISTINCT date(tanggal) as tgl
        FROM draft_history
        WHERE telegram_id=?
        ORDER BY tgl DESC
        LIMIT 5
    ''', (telegram_id,))
    rows = await cursor.fetchall()
    await conn.close()
    return [row[0] for row in rows]

async def get_draft_history_by_date(telegram_id: str, date_str: str) -> list:
    """Ambil semua riwayat draf pada tanggal tertentu."""
    conn = await aiosqlite.connect(DB_NAME, timeout=10)
    cursor = await conn.cursor()
    await cursor.execute('''
        SELECT time(tanggal), username, total_maxi, payload_json
        FROM draft_history
        WHERE telegram_id=? AND date(tanggal)=?
        ORDER BY tanggal DESC
    ''', (telegram_id, date_str))
    rows = await cursor.fetchall()
    await conn.close()
    return rows


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
    cookies_json = encrypt_secret_value(json.dumps(cookies))
    conn = await aiosqlite.connect(DB_NAME, timeout=10)
    cursor = await conn.cursor()
    await cursor.execute('''
        INSERT OR REPLACE INTO sessions (telegram_id, username, cookies_json, saved_at)
        VALUES (?, ?, ?, CURRENT_TIMESTAMP)
    ''', (telegram_id, username, cookies_json))
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
            cookies_json = decrypt_secret_value(row[0])
            return json.loads(cookies_json)
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
    cookies = await load_session_cookies(telegram_id, username)
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
    await conn.close()
    result = []
    for u, is_active, nick in rows:
        result.append((u, is_active, bool(await load_session_cookies(telegram_id, u)), nick))
    return result

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

async def simpan_draft_order(telegram_id, total_maxi, keranjang, war_date=None):
    """
    Simpan draf pesanan untuk akun aktif.
    Selalu REPLACE — 1 akun hanya boleh punya 1 draf PENDING.
    """
    active_user = await get_current_user(telegram_id)
    if not active_user:
        return False
    war_date = war_date or get_current_war_date()
    conn = await aiosqlite.connect(DB_NAME, timeout=10)
    cursor = await conn.cursor()
    # Hapus semua pending lama untuk akun ini dulu (aman, atomik)
    await cursor.execute(
        "DELETE FROM draft_orders WHERE telegram_id=? AND username=? AND status='PENDING'",
        (telegram_id, active_user)
    )
    await cursor.execute('''
        INSERT INTO draft_orders
            (telegram_id, username, total_maxi, payload_json, war_date,
             status, updated_at)
        VALUES (?, ?, ?, ?, ?, 'PENDING', CURRENT_TIMESTAMP)
    ''', (telegram_id, active_user, total_maxi, json.dumps(keranjang), war_date))
    
    # Simpan juga ke log history
    await cursor.execute('''
        INSERT INTO draft_history (telegram_id, username, total_maxi, payload_json)
        VALUES (?, ?, ?, ?)
    ''', (telegram_id, active_user, total_maxi, json.dumps(keranjang)))
    
    await conn.commit()
    await conn.close()
    return True

async def get_pending_order(telegram_id):
    """Ambil draf PENDING terbaru akun aktif, beserta info tanggalnya."""
    active_user = await get_current_user(telegram_id)
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
    active_user = await get_current_user(telegram_id)
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

async def get_all_pending_orders_multi(telegram_id, war_date=None):
    """
    Ambil 1 draf PENDING terbaru PER AKUN.
    Aman dari duplikat: jika ada 2 draf untuk 1 akun, ambil yang terbaru.
    """
    conn = await aiosqlite.connect(DB_NAME, timeout=10)
    cursor = await conn.cursor()
    query = '''
        SELECT d.id, d.username, d.payload_json
        FROM draft_orders d
        INNER JOIN (
            SELECT username, MAX(id) as max_id
            FROM draft_orders
            WHERE telegram_id = ? AND status = 'PENDING'
    '''
    params = [telegram_id]
    if war_date is not None:
        query += " AND war_date = ?"
        params.append(war_date)
    query += '''
            GROUP BY username
        ) latest ON d.id = latest.max_id
        WHERE d.telegram_id = ? AND d.status = 'PENDING'
        ORDER BY d.id ASC
    '''
    params.append(telegram_id)
    await cursor.execute(query, tuple(params))
    rows = await cursor.fetchall()
    await conn.close()
    return rows

async def cleanup_all_pending_orders(telegram_id):
    """
    Cleanup jam 09:00: karantina draf PENDING untuk war yang sudah lewat.
    Aman: draf yang diinput setelah jam 08:00 (untuk besok) TIDAK dihapus.
    
    Timezone: Gunakan 'localtime' konsisten agar cleanup tepat waktu.
    """
    next_war_date = get_current_war_date()
    deleted = await expire_stale_pending_orders(telegram_id, next_war_date)

    conn = await aiosqlite.connect(DB_NAME, timeout=10)
    cursor = await conn.cursor()
    
    # BONUS: Cleanup old sessions juga (lebih dari 7 hari)
    await cursor.execute('''
        DELETE FROM sessions
        WHERE telegram_id = ? AND saved_at < datetime('now', '-7 days')
    ''', (telegram_id,))
    old_sessions_deleted = cursor.rowcount
    if old_sessions_deleted > 0:
        logger.info(f"🧹 Cleanup: {old_sessions_deleted} old session(s) dihapus")
    
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
    if not row:
        return default_value
    value = row[0]
    if key in {"kode_akses"}:
        value = decrypt_secret_value(value)
    return value if value else default_value

async def set_setting(key: str, value: str):
    stored_value = encrypt_secret_value(value) if key in {"kode_akses"} else value
    conn = await aiosqlite.connect(DB_NAME, timeout=10)
    cursor = await conn.cursor()
    await cursor.execute('''
        INSERT INTO settings (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value=?
    ''', (key, stored_value, stored_value))
    await conn.commit()
    await conn.close()


if __name__ == "__main__":
    init_db()
