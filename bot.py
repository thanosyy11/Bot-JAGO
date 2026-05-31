import asyncio
import os
import json
import logging
import random
import pytz
from datetime import datetime, timedelta
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from database import (
    save_user_credentials, get_all_products_dict, simpan_draft_order,
    get_current_user, get_pending_order, delete_pending_order,
    get_all_accounts, set_active_account, get_all_pending_orders_multi,
    get_order_history, get_order_history_dates, get_order_history_by_date,
    cleanup_all_pending_orders, init_db,
    get_all_accounts_with_status, count_accounts, clear_session_cookies,
    get_all_drafts_overview, get_session_status,
    set_engine_ready_status, get_engine_ready_status,
    get_account_nickname, update_account_nickname, delete_account,
    get_setting, set_setting
)
from engine import SiliwangiEngine, CloudflareBlockException

logging.basicConfig(filename='siliwangi_error.log', level=logging.INFO, format='%(asctime)s - %(levelname)s - [BOT] %(message)s')
logger = logging.getLogger(__name__)

# Load .env explicitly from the same directory as this script
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, '.env'))
BOT_TOKEN  = os.getenv("BOT_TOKEN")
ADMIN_ID   = int(os.getenv("ADMIN_ID"))
# Production mode only — dry run dihapus

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
router = Router()
zona_waktu = pytz.timezone('Asia/Jakarta')
scheduler = AsyncIOScheduler(timezone=zona_waktu)

router.message.filter(F.from_user.id == ADMIN_ID)
router.callback_query.filter(F.from_user.id == ADMIN_ID)

class AkunState(StatesGroup):
    waiting_for_username = State()
    waiting_for_password = State()

class OrderState(StatesGroup):
    waiting_for_template = State()
    editing_existing     = State()
    confirming_order     = State()

class EditAkunState(StatesGroup):
    waiting_for_nickname = State()

class KodeAksesState(StatesGroup):
    waiting_for_kode = State()


def _dn(username: str, nickname: str) -> str:
    """Display Name: tampilkan nickname jika ada, fallback ke username."""
    return nickname.strip() if nickname and nickname.strip() else username


def get_war_panel_button() -> InlineKeyboardMarkup:
    """Tombol CTA standar yang ditempel di setiap notifikasi otomatis."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Dashboard", callback_data="war_panel")]
    ])

mesin_siaga = {} 

async def eksekusi_dengan_jeda(engine, delay, username):
    if delay > 0:
        await asyncio.sleep(delay)

    logger.info(f"[WAR] akun: {username} (Delay: {delay:.1f}s)")
    hasil = await engine.execute_order()
    return (
        username,
        hasil,
        engine.step_log,
        getattr(engine, 'order_id_woo', 'UNKNOWN'),
        getattr(engine, 'substitusi_log', []),
    )

async def job_pemanasan():
    logger.info("Warm Up...")
    orders = await get_all_pending_orders_multi(str(ADMIN_ID))

    if not orders:
        await bot.send_message(
            ADMIN_ID,
            "Tidak ada draf pesanan hari ini. Bot Nonaktif.",
            reply_markup=get_war_panel_button()
        )
        logger.info("[Nonaktif] Tidak ada draf pesanan.")
        return

    # Notifikasi warm-up dimulai — lengkap
    draf_info = "\n".join([f"  · `{o[1]}`" for o in orders])
    await bot.send_message(
        ADMIN_ID,
        f"⚙️ **WARM-UP DIMULAI**\n\n"
        f"Ditemukan **{len(orders)} draf** pesanan:\n{draf_info}\n\n"
        f"_Memulai login..._",
        parse_mode="Markdown",
        reply_markup=get_war_panel_button()
    )

    mesin_siaga[ADMIN_ID] = {}
    berhasil_login = 0
    gagal_login = []

    async def _login_task(username):
        nonlocal berhasil_login
        try:
            engine = SiliwangiEngine(telegram_id=str(ADMIN_ID), username=username)
            if await engine.login():
                mesin_siaga[ADMIN_ID][username] = engine
                berhasil_login += 1
                await set_engine_ready_status(str(ADMIN_ID), username, True)
                return True
            else:
                gagal_login.append(username)
                await set_engine_ready_status(str(ADMIN_ID), username, False)
                await engine.close()
                return False
        except Exception as e:
            logger.error(f"Error fatal warm-up {username}: {e}", exc_info=True)
            gagal_login.append(f"{username} (CRASH)")
            return False

    # Eksekusi login secara PARALEL untuk kecepatan
    tasks = [_login_task(order[1]) for order in orders]
    await asyncio.gather(*tasks, return_exceptions=True)

    if berhasil_login > 0:
        status_txt = f"✅ Login berhasil: **{berhasil_login}/{len(orders)} akun**"
        if gagal_login:
            status_txt += f"\n❌ Gagal login: " + ", ".join([f"`{u}`" for u in gagal_login])
        status_txt += "\n\n⏳ _Siap eksekusi!!!_"
        await bot.send_message(ADMIN_ID, status_txt, parse_mode="Markdown", reply_markup=get_war_panel_button())
    else:
        await bot.send_message(
            ADMIN_ID,
            "🚨 **[GAGAL WARM-UP]**\nTidak ada akun yang berhasil login!\n"
            "Periksa koneksi server dan kredensial akun.",
            reply_markup=get_war_panel_button()
        )


async def job_eksekusi():
    logger.info("Mengecek jadwal...")
    pasukan = mesin_siaga.get(ADMIN_ID, {})

    if not pasukan:
        logger.warning("⚠️ Memori kosong, memuat fallback dari database...")
        orders = await get_all_pending_orders_multi(str(ADMIN_ID))
        if orders:
            mesin_siaga[ADMIN_ID] = {}
            for order in orders:
                username = order[1]
                # Fallback: TETAP siapkan engine meskipun status_ready False
                # Karena execute_order akan mencoba login() jika sesi mati
                engine = SiliwangiEngine(telegram_id=str(ADMIN_ID), username=username)
                mesin_siaga[ADMIN_ID][username] = engine
            pasukan = mesin_siaga.get(ADMIN_ID, {})
            
        if not pasukan:
            logger.error("🚨 DATABASE JUGA KOSONG: Bot akan membatalkan WAR.")
            await bot.send_message(ADMIN_ID, "🚨 **[DARURAT]** Memori kosong & pemulihan DB gagal. WAR dibatalkan!")
            return

    try:
        import httpx
        # Cek apakah butuh Gedor Pintu
        butuh_gedor = True
        if await get_setting("kode_akses", ""):
            butuh_gedor = False
        else:
            # Cek apakah ada engine yang ready (berarti tembus via cookie 06:00)
            for u in pasukan.keys():
                if await get_engine_ready_status(str(ADMIN_ID), u):
                    butuh_gedor = False
                    break

        if butuh_gedor:
            logger.info("Tidak ada kode & tidak ada cookie tembus. Memulai mode GEDOR PINTU...")
            timeout_end = datetime.now() + timedelta(minutes=2)
            async with httpx.AsyncClient(timeout=3.0) as c:
                while datetime.now() < timeout_end:
                    try:
                        r = await c.get("https://siliwangibolukukus.com/")
                        if "password-protected=login" not in str(r.url) and "password-protected-login" not in r.text:
                            logger.info("TERBUKA! SERANGG!!!")
                            break
                    except Exception:
                        pass
                    await asyncio.sleep(0.5)
            if datetime.now() >= timeout_end:
                logger.warning("Timeout Gedor Pintu. Mencoba eksekusi paksa...")
        else:
            # Punya kode atau cookie tembus, tunggu tepat jam 08:00:00 jika dijalankan jam 07:59:50
            now = datetime.now()
            target = now.replace(hour=8, minute=0, second=0, microsecond=0)
            if now < target:
                wait_sec = (target - now).total_seconds()
                logger.info(f"⏳ Menunggu {wait_sec:.1f} detik menuju pukul 08:00:00")
                await asyncio.sleep(wait_sec)

        logger.info(f"MEMULAI WAR {len(pasukan)} AKUN!")

        tasks = []
        delay_total = 0.0
        for username, engine in pasukan.items():
            tasks.append(eksekusi_dengan_jeda(engine, delay_total, username))
            delay_total += random.uniform(0.01, 0.05)

        hasil_perang = await asyncio.gather(*tasks)

        # ─── Susun Laporan WAR ────────────────────────────────────────
        zona_wib = pytz.timezone('Asia/Jakarta')
        jam_war  = datetime.now(zona_wib).strftime("%H:%M")
        laporan  = f" -**HASIL WAR {jam_war} WIB:**\n\n"

        for target_username, is_success, step_log, order_id_woo, substitusi_log in hasil_perang:
            if is_success:
                laporan += f"👤 `{target_username}`: ✅ **BERHASIL**"
                if order_id_woo and order_id_woo != "UNKNOWN":
                    laporan += f" (Order ID: `#{order_id_woo}`)"
                laporan += "\n"
                # Tampilkan catatan substitusi/habis jika ada
                if substitusi_log:
                    laporan += "   📝 _Catatan stok:_\n"
                    for catatan in substitusi_log:
                        laporan += f"   └─ {catatan}\n"
            else:
                laporan += f"👤 `{target_username}`: ❌ **GAGAL**\n"
                # Ambil penyebab kegagalan dari step_log
                if step_log:
                    err_lines = [
                        line for line in step_log
                        if any(icon in line for icon in ["❌", "⚠️"])
                    ]
                    if err_lines:
                        laporan += f"   └─ Penyebab: {err_lines[-1][:100]}\n"
                # Juga tampilkan substitusi_log jika ada (sebelum gagal)
                if substitusi_log:
                    for catatan in substitusi_log:
                        laporan += f"   └─ {catatan}\n"
            laporan += "\n"

        for target_username, is_success, step_log, order_id_woo, substitusi_log in hasil_perang:
            engine = pasukan.get(target_username)
            if engine:
                await engine.close()

        mesin_siaga.pop(ADMIN_ID, None)
        war_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📊 Dashboard", callback_data="war_panel"),
             InlineKeyboardButton(text="📜 Riwayat", callback_data="lihat_riwayat")]
        ])
        try:
            await bot.send_message(ADMIN_ID, laporan, parse_mode="Markdown", reply_markup=war_kb)
        except Exception as e:
            logger.error(f"Gagal kirim laporan WAR ke Telegram: {e}")
            try:
                await bot.send_message(
                    ADMIN_ID,
                    laporan.replace('`', '').replace('*', '').replace('_', ''),
                    reply_markup=war_kb
                )
            except Exception:
                pass
        logger.info("War Selesai.")

    except CloudflareBlockException as e:
        pesan_error = "Bot terdeteksi oleh Cloudflare dan tidak dapat memproses request! Mohon periksa IP atau jaringan."
        logger.error(f"CLOUDFLARE ERROR saat job_eksekusi: {e}")
        try:
            await bot.send_message(ADMIN_ID, pesan_error, parse_mode="Markdown")
        except Exception:
            pass
        for engine in pasukan.values():
            try:
                await engine.close()
            except Exception:
                pass
        mesin_siaga.pop(ADMIN_ID, None)
    except Exception as e:
        pesan_error = (
            f"🚨 **[FATAL ERROR] WAR CRASH!**\n\n"
            f"Bot mengalami error kritis saat eksekusi jam 08:00:\n"
            f"`{type(e).__name__}: {str(e)[:300]}`\n\n"
            f"Cek file `siliwangi_error.log` untuk detail lengkap."
        )
        logger.error(f"FATAL ERROR saat job_eksekusi: {e}", exc_info=True)
        try:
            await bot.send_message(ADMIN_ID, pesan_error, parse_mode="Markdown")
        except Exception:
            pass
        # Pastikan semua engine ditutup meski terjadi error
        for engine in pasukan.values():
            try:
                await engine.close()
            except Exception:
                pass
        mesin_siaga.pop(ADMIN_ID, None)


async def job_bersihkan_draft():
    """
    Job berjalan setiap jam 09:00 WIB.
    Membersihkan semua draft PENDING yang tersisa setelah war selesai,
    agar tidak terbawa ke war berikutnya.
    """
    logger.info("Memulai cleanup draft otomatis...")
    deleted = await cleanup_all_pending_orders(str(ADMIN_ID))
    mesin_siaga.pop(ADMIN_ID, None)  # Bersihkan juga cache engine

    if deleted > 0:
        pesan = (
            f"🧹 CLEANUP Selesai!\n"
            f"Dihapus **{deleted}** draft PENDING yang tersisa."
        )
        logger.info(f"🧹 Cleanup: {deleted} draft dihapus.")
    else:
        pesan = "🧹 CLEANUP Bersih! ✨"
        logger.info("🧹 Cleanup: Tidak ada draft tersisa.")

    try:
        await bot.send_message(ADMIN_ID, pesan, parse_mode="Markdown", reply_markup=get_war_panel_button())
    except Exception:
        pass

# ============================================================
# HEALTH CHECK — Cek website & session setiap hari jam 07:00
# ============================================================
async def job_health_check():
    """
    Cek harian jam 07:00 WIB:
    - Apakah website target bisa dijangkau
    - Apakah session setiap akun masih valid
    Laporkan hasilnya ke admin.
    """
    import httpx as _httpx
    logger.info("Health check dimulai...")

    # 1. Cek website
    website_ok = False
    try:
        async with _httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            resp = await client.get("https://siliwangibolukukus.com/")
            website_ok = resp.status_code < 500
    except Exception as e:
        logger.warning(f"Health check Gagal: {e}")

    # 2. Cek session setiap akun yang punya draf
    orders = await get_all_pending_orders_multi(str(ADMIN_ID))
    session_lines = []
    for order in orders:
        username = order[1]
        engine = SiliwangiEngine(telegram_id=str(ADMIN_ID), username=username)
        await engine.init_session()
        try:
            import httpx as _h
            resp2 = await engine.client.get("https://siliwangibolukukus.com/my-account/")
            if "Keluar" in resp2.text or "Logout" in resp2.text:
                session_lines.append(f"  ✅ `{username}` — session aktif")
            else:
                session_lines.append(f"  ⚠️ `{username}` — session EXPIRED")
        except Exception:
            session_lines.append(f"  ❌ `{username}` — tidak bisa dicek")
        finally:
            await engine.close()

    web_status = "✅ Website OK" if website_ok else "❌ Website TIDAK DAPAT DIJANGKAU!"
    sesi_status = "\n".join(session_lines) if session_lines else "  _(tidak ada draf aktif)_"

    pesan = (
        f"HEALTH CHECK\n\n"
        f"**Website:** {web_status}\n\n"
        f"**Session Akun:**\n{sesi_status}\n\n"
    )
    try:
        await bot.send_message(ADMIN_ID, pesan, parse_mode="Markdown", reply_markup=get_war_panel_button())
    except Exception:
        pass

# ============================================================
# JADWAL UTAMA
# ============================================================
scheduler.add_job(job_health_check,    'cron', hour=7,  minute=0,  second=0)
scheduler.add_job(job_pemanasan,       'cron', hour=6,  minute=0,  second=0)
scheduler.add_job(job_pemanasan,       'cron', hour=7,  minute=55, second=0)
scheduler.add_job(job_eksekusi,        'cron', hour=7,  minute=59, second=50)
scheduler.add_job(job_bersihkan_draft, 'cron', hour=9,  minute=0,  second=0)

def get_main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚙️ Pengaturan", callback_data="menu_pengaturan"),
         InlineKeyboardButton(text="📦 Input Pesanan", callback_data="menu_order")],
        [InlineKeyboardButton(text="🚀 Dashboard", callback_data="war_panel")],
    ])

@router.message(Command("help"))
async def cmd_help(message: Message):
    teks = (
        "**Bot JAGO — Panduan Cepat**\n\n"
        "1. **Kelola Akun:** Tambah & login akun Siliwangi.\n"
        "2. **Susun Pesanan:** Input draf item yang akan di-war.\n"
        "3. **Siapkan War:** Cek sesi login & kesiapan draf.\n\n"
        "💡 _Gunakan menu di bawah untuk navigasi._"
    )
    await message.answer(teks, reply_markup=get_main_menu_keyboard(), parse_mode="Markdown")

@router.message(Command("batal", "cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("✅ Dibatalkan.", reply_markup=get_main_menu_keyboard())

@router.message(CommandStart())
@router.callback_query(F.data == "kembali_ke_menu")
async def cmd_start(event, state: FSMContext = None):
    if state: await state.clear()
    
    user_id = str(event.from_user.id if isinstance(event, Message) else event.from_user.id)
    drafts = await get_all_drafts_overview(user_id)
    current_user = await get_current_user(user_id)

    status_text = "*BOT JAGO — Dashboard*\n━━━━━━━━━━━━━━\n"

    if not drafts:
        status_text += "❌ Belum ada akun terdaftar.\nKlik ⚙️ *Pengaturan* → *Akun Siliwangi* untuk mulai."
    else:
        for i, (username, is_active, has_draft, total_maxi, _, nickname) in enumerate(drafts, 1):
            session_ok = await get_session_status(user_id, username)
            dn = _dn(username, nickname)
            s_icon = "🔑" if session_ok else "🚫"
            d_icon = "✅" if has_draft else "📝"
            active_mark = " 🟢" if username == current_user else ""
            status_text += f"{i}. *{dn}*{active_mark}\n"
            status_text += f"   {s_icon}{d_icon}  {f'**{total_maxi} Box**' if has_draft else '_Belum ada draf_'}\n"
    if isinstance(event, Message):
        await event.answer(status_text, reply_markup=get_main_menu_keyboard(), parse_mode="Markdown")
    else:
        await event.message.edit_text(status_text, reply_markup=get_main_menu_keyboard(), parse_mode="Markdown")

# ============================================================
# TUTORIAL MULTI-HALAMAN
# ============================================================

TUTORIAL_PAGES = [
    # Halaman 1 — Overview
    (
        " **TUTORIAL** — Hal. 1/7\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        " **Apa itu Bot JAGO?**\n\n"
        "Bot JAGO adalah bot Telegram untuk otomasi order WooCommerce.\n\n"
        "**Fitur utama:**\n"
        "• ⚡ Checkout otomatis tepat jam 08:00 WIB\n"
        "• 👥 Multi-akun (hingga 10 akun sekaligus)\n"
        "• 🧠 Smart tier: amankan stok parsial & fallback otomatis\n"
        "• 🔑 Session login tersimpan — tidak perlu login ulang\n"
        "• 📊 Laporan hasil langsung ke Telegram\n\n"
        "**Jadwal otomatis:**\n"
        "• `07:55` → Warm-up (login semua akun)\n"
        "• `08:00` → War eksekusi order\n"
        "• `09:00` → Cleanup draft tersisa"
    ),
    # Halaman 2 — Setup awal
    (
        " **TUTORIAL BOT JAGO** — Hal. 2/7\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        " **Setup Awal**\n\n"
        "**Langkah 1 — Tambah Akun:**\n"
        "1. Buka 👥 **Kelola Multi-Akun**\n"
        "2. Klik ➕ **Tambah Akun Baru**\n"
        "3. Masukkan **username/email** akun WooCommerce\n"
        "4. Masukkan **password** akun\n"
        "5. Ulangi untuk akun ke-2 (maks. 2 akun)\n\n"
        "**Langkah 2 — Login Awal:**\n"
        "• Setelah tambah akun, klik 🔑 **Login Semua Sekarang**\n"
        "• Bot akan login & menyimpan cookies ke database\n"
        "• Setelah ini, bot tidak perlu login ulang kecuali sesi expired\n\n"
        "💡 _Password disimpan terenkripsi di database._"
    ),
    # Halaman 3 — Input Pesanan
    (
        "**TUTORIAL BOT JAGO** — Hal. 3/7\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "**Cara Input Pesanan**\n\n"
        "**Langkah 1 — Pilih Akun Aktif:**\n"
        "• Buka 👥 **Kelola Multi-Akun** → klik nama akun\n"
        "• Akun aktif ditandai `🟢🔑` di menu\n\n"
        "**Langkah 2 — Input Template:**\n"
        "• Buka 📦 **Input Pesanan**\n"
        "• Salin template yang muncul, edit kuantitas, kirim\n"
        "• Format wajib: `- 50x MAXI Belgian Chocolate`\n\n"
        "**Langkah 3 — Konfirmasi Preview:**\n"
        "• Bot tampilkan preview + total MAXI\n"
        "• Klik ✅ **Simpan** jika sudah benar\n\n"
        "**Ulangi untuk setiap akun** (ganti akun aktif dulu)\n\n"
        "⚠️ _Pesanan masuk ke akun yang sedang aktif saat input!_"
    ),
    # Halaman 4 — Aturan Produk
    (
        "**TUTORIAL BOT JAGO** — Hal. 4/7\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "**Aturan Produk & Kelipatan**\n\n"
        "**MAXI** (wajib kelipatan **12**):\n"
        "**DC / Dessert Cake** (wajib kelipatan **4**):\n"
        "• Jika habis → dilewati otomatis, order tetap lanjut\n\n"
        "**Minimal order: 50 box** (gabungan MAXI + DC)"
    ),
    # Halaman 5 — Smart Tier System
    (
        "**TUTORIAL BOT JAGO** — Hal. 5/7\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "**Smart Tier System**\n\n"
        "Bot otomatis menangani stok yang habis/kurang:\n\n"
        "**Contoh: Kamu order 120 MAXI**\n"
        "```\n"
        "Skenario A (normal):\n"
        "  ✅ 120x dari Tier 1 → selesai\n\n"
        "Skenario B (stok parsial):\n"
        "  ⚡ Belgian Chocolate: hanya 13x tersisa\n"
        "  → Amankan 13x, sisa 107x ke produk berikutnya\n\n"
        "Skenario C (Tier 1+2 habis):\n"
        "  ✂️ Otomatis potong 40%: 120 → 72x\n"
        "  → Order 72x dari Tier 3 (tetap kelipatan 12)\n"
        "```\n"
        "💡 _Kamu tidak perlu melakukan apa-apa. Semua otomatis._"
    ),
    # Halaman 6 — Manajemen Draf
    (
        "**TUTORIAL BOT JAGO** — Hal. 6/7\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "**Manajemen Pesanan**\n\n"
        "Buka 📝 **Pesanan & Kelola** untuk:\n\n"
        "• **Lihat status semua akun** — siap/belum ada draf\n"
        "• **Detail & Edit Draf** — lihat/edit isi pesanan akun aktif\n"
        "• **Riwayat** — rekap order sukses semua akun\n\n"
        "**Aturan penting:**\n"
        "• 1 akun = 1 draf aktif (input baru otomatis gantikan lama)\n"
        "• Edit aman: draf lama tidak dihapus sampai input baru dikonfirmasi\n"
        "• Cleanup `09:00` hanya hapus draf war pagi itu — draf baru aman\n\n"
        "**Cek kesiapan sebelum war:**\n"
        "Buka Pesanan & Kelola → semua akun harus tampil ✅"
    ),
    # Halaman 7 — Alur War & Tips
    (
        "**TUTORIAL BOT JAGO** — Hal. 7/7\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "**Alur War & Tips Sukses**\n\n"
        "**H-1 malam (misal 21:00):**\n"
        "1. Input pesanan untuk semua akun\n"
        "2. Login Semua Sekarang → pastikan semua 🟢🔑\n"
        "3. Cek Pesanan & Kelola → semua akun ✅\n\n"
        "**Pagi hari (07:55-08:00):**\n"
        "• Bot otomatis warm-up & eksekusi — tidak perlu buka Telegram\n"
        "• Laporan hasil dikirim ke kamu setelah selesai\n\n"
        "**Tips sukses:**\n"
        "• ✅ Pastikan semua akun punya 🔑 session\n"
        "• ✅ Nama produk harus persis sama dengan template\n"
        "• ✅ Total MAXI kelipatan 12, DC kelipatan 4\n"
        "🎯 _Selamat War! Gibran & Jokowi for 2029_ 🇮🇩"
    ),
]

def _tutorial_keyboard(page: int) -> InlineKeyboardMarkup:
    """Buat keyboard navigasi halaman tutorial."""
    total = len(TUTORIAL_PAGES)
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton(text="⬅️ Sebelumnya", callback_data=f"tutorial:{page-1}"))
    if page < total:
        nav.append(InlineKeyboardButton(text="Berikutnya ➡️", callback_data=f"tutorial:{page+1}"))
    rows = []
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="🏠 Menu Utama", callback_data="kembali_ke_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

@router.callback_query(F.data.startswith("tutorial:"))
async def cb_tutorial(callback: CallbackQuery):
    page = int(callback.data.split(":")[1])
    total = len(TUTORIAL_PAGES)
    page = max(1, min(page, total))  # clamp
    teks = TUTORIAL_PAGES[page - 1]
    await callback.message.edit_text(
        teks,
        reply_markup=_tutorial_keyboard(page),
        parse_mode="Markdown"
    )


@router.callback_query(F.data == "menu_pengaturan")
async def cb_menu_pengaturan(callback: CallbackQuery):
    await callback.answer()
    teks = (
        "⚙️ *PENGATURAN*\n"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Akun Siliwangi", callback_data="menu_akun")],
        [InlineKeyboardButton(text="🔑 Set Kode Akses Web", callback_data="menu_kode_akses")],
        [InlineKeyboardButton(text="🏠 Kembali ke Menu Utama", callback_data="kembali_ke_menu")],
    ])
    await callback.message.edit_text(teks, reply_markup=keyboard, parse_mode="Markdown")

@router.callback_query(F.data == "menu_kode_akses")
async def cb_menu_kode_akses(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    current_code = await get_setting("kode_akses", "")
    txt_code = f"`{current_code}`" if current_code else "_(belum diatur)_"
    teks = (
        "🔑 *KODE AKSES WAR*\n"
        "━━━━━━━━━━━━━━\n"
        f"Kode saat ini: {txt_code}\n\n"
        "Ketik kode akses baru sekarang:\n"
        "_(Ketik /batal untuk membatalkan)_"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Kembali", callback_data="menu_pengaturan")]
    ])
    await callback.message.edit_text(teks, reply_markup=keyboard, parse_mode="Markdown")
    await state.set_state(KodeAksesState.waiting_for_kode)

@router.message(KodeAksesState.waiting_for_kode)
async def process_kode_akses(message: Message, state: FSMContext):
    if message.text.startswith('/'):
        await message.answer("Silakan ketik kode akses (tanpa awalan `/`):")
        return
    kode = message.text.strip()
    await set_setting("kode_akses", kode)
    await state.clear()
    
    teks = (
        f"✅ *Kode Akses Berhasil Disimpan!*\n\n"
        f"Kode baru: `{kode}`\n\n"
        f"Jika jam 07:55 gagal login karena password, Anda bisa mengklik tombol di bawah ini untuk Login Ulang secara manual."
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Coba Login Ulang Sekarang", callback_data="force_warmup")],
        [InlineKeyboardButton(text="🔙 Pengaturan", callback_data="menu_pengaturan")]
    ])
    await message.answer(teks, reply_markup=keyboard, parse_mode="Markdown")

@router.callback_query(F.data == "force_warmup")
async def cb_force_warmup(callback: CallbackQuery):
    await callback.answer("Memulai ulang proses Warm-Up...")
    await job_pemanasan()



@router.callback_query(F.data == "war_panel")
async def cb_war_panel(callback: CallbackQuery):
    await callback.answer()
    tid = str(callback.from_user.id)
    drafts = await get_all_drafts_overview(tid)

    status_lines = []
    for username, is_active, has_draft, total_maxi, _, nickname in drafts:
        dn = _dn(username, nickname)
        session_ok = await get_session_status(tid, username)
        if has_draft and session_ok:
            icon, ket = "✅", f"{total_maxi} box · Siap"
        elif has_draft and not session_ok:
            icon, ket = "⚠️", f"{total_maxi} box · Perlu siapkan"
        else:
            icon, ket = "❌", "Belum ada draf"
        status_lines.append(f"{icon} *{dn}*\n   └─ {ket}")

    status_text = "\n".join(status_lines) if status_lines else "❌ Belum ada akun terdaftar."
    teks = (
        "🚀 *PANEL WAR*\n"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚡ Siapkan Semua Akun", callback_data="siapkan_semua")],
        [InlineKeyboardButton(text="🌐 Cek Jaringan & Session", callback_data="net_diag"),
         InlineKeyboardButton(text="📜 Riwayat", callback_data="lihat_riwayat")],
        [InlineKeyboardButton(text="📖 Panduan", callback_data="tutorial:1"),
         InlineKeyboardButton(text="🏠 Menu", callback_data="kembali_ke_menu")],
    ])
    await callback.message.edit_text(teks, reply_markup=keyboard, parse_mode="Markdown")


@router.callback_query(F.data == "menu_akun")
async def cb_menu_akun(callback: CallbackQuery):
    tid = str(callback.from_user.id)
    accounts = await get_all_accounts_with_status(tid)
    current = await get_current_user(tid)
    keyboard = []

    if not accounts:
        teks = (
            "👥 *AKUN SILIWANGI*\n"
            "━━━━━━━━━━━━━━\n"
            "❌ Belum ada akun terdaftar.\n\n"
            "Klik *➕ Tambah Akun* untuk mulai."
        )
    else:
        teks = (
            "👥 *AKUN SILIWANGI*\n"
            "━━━━━━━━━━━━━━\n"
            "🟢 Aktif · 🔑 Session · ✅ Ada draf\n\n"
        )
        drafts_overview = await get_all_drafts_overview(tid)
        draft_map = {d[0]: d for d in drafts_overview}
        for acc, is_active, has_session, nickname in accounts:
            dn = _dn(acc, nickname)
            aktif_icon = "🟢" if acc == current else "⚫"
            sesi_icon  = "🔑" if has_session else "🚫"
            draft_info = draft_map.get(acc)
            draf_icon  = "✅" if draft_info and draft_info[2] else "📝"
            total_maxi = draft_info[3] if draft_info and draft_info[2] else 0
            label = f"{aktif_icon}{sesi_icon}{draf_icon} {dn}"
            if total_maxi > 0:
                label += f" [{total_maxi}box]"
            keyboard.append([InlineKeyboardButton(text=label, callback_data=f"acc_detail:{acc}")])

    keyboard.append([InlineKeyboardButton(text="➕ Tambah Akun Baru", callback_data="add_new_acc")])
    keyboard.append([InlineKeyboardButton(text="🔙 Pengaturan", callback_data="menu_pengaturan")])
    await callback.message.edit_text(
        teks, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode="Markdown"
    )

@router.callback_query(F.data.startswith("acc_detail:"))
async def cb_acc_detail(callback: CallbackQuery, state: FSMContext):
    """Halaman detail akun — tampilkan nickname, set aktif, edit alias, reset login, hapus."""
    tid = str(callback.from_user.id)
    target = callback.data.split(":", 1)[1]
    current = await get_current_user(tid)
    has_session = await get_session_status(tid, target)
    nickname = await get_account_nickname(tid, target)
    dn = _dn(target, nickname)

    status_aktif = "🟢 Dipilih untuk input pesanan" if target == current else "⚫ Tidak dipilih"
    status_siap  = "✅ Siap war" if has_session else "⚠️ Perlu disiapkan"
    alias_txt    = f"`{nickname}`" if nickname else "_(belum diset)_"

    teks = (
        f"👤 *{dn}*\n"
        f"━━━━━━━━━━━━━━\n"
        f"Akun   : `{target}`\n"
        f"Alias  : {alias_txt}\n"
        f"Status : {status_aktif}\n"
        f"War    : {status_siap}"
    )

    keyboard = []
    if target != current:
        keyboard.append([InlineKeyboardButton(
            text="🎯 Jadikan Akun Aktif Input",
            callback_data=f"setacc:{target}"
        )])
    keyboard.append([InlineKeyboardButton(
        text="✏️ Ubah Nama Alias",
        callback_data=f"edit_nickname:{target}"
    )])
    keyboard.append([InlineKeyboardButton(
        text="🔄 Reset Login (hapus session)",
        callback_data=f"force_relogin:{target}"
    )])
    keyboard.append([InlineKeyboardButton(
        text="🗑️ Hapus Akun Ini",
        callback_data=f"hapus_akun:{target}"
    )])
    keyboard.append([InlineKeyboardButton(text="🔙 Daftar Akun", callback_data="menu_akun")])

    await callback.message.edit_text(
        teks, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode="Markdown"
    )

@router.callback_query(F.data.startswith("setacc:"))
async def cb_setacc(callback: CallbackQuery, state: FSMContext):
    target_acc = callback.data.split(":", 1)[1]
    await set_active_account(str(callback.from_user.id), target_acc)
    await callback.answer(f"✅ Akun aktif: {target_acc[:30]}", show_alert=True)
    await cb_menu_akun(callback)


@router.callback_query(F.data.startswith("force_relogin:"))
async def cb_force_relogin(callback: CallbackQuery):
    tid = str(callback.from_user.id)
    target = callback.data.split(":", 1)[1]
    await clear_session_cookies(tid, target)
    if ADMIN_ID in mesin_siaga and target in mesin_siaga[ADMIN_ID]:
        engine = mesin_siaga[ADMIN_ID].pop(target)
        await engine.close()
    await callback.answer(
        f"✅ Session {target[:30]} direset.\nBot login ulang otomatis...",
        show_alert=True
    )
    await cb_acc_detail(callback)


@router.callback_query(F.data.startswith("edit_nickname:"))
async def cb_edit_nickname(callback: CallbackQuery, state: FSMContext):
    target = callback.data.split(":", 1)[1]
    await state.update_data(edit_target=target)
    await callback.message.edit_text(
        f"✏️ *Ubah Nama Alias*\n\n"
        f"Akun: `{target}`\n\n"
        f"Ketik nama alias.\n"
        f"_Ketik /batal untuk membatalkan._",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Batal", callback_data=f"acc_detail:{target}")]
        ]),
        parse_mode="Markdown"
    )
    await state.set_state(EditAkunState.waiting_for_nickname)
    await callback.answer()


@router.message(EditAkunState.waiting_for_nickname)
async def process_nickname(message: Message, state: FSMContext):
    if message.text.startswith('/'):
        await message.answer("Ketik nama alias (tanpa awalan `/`):")
        return
    nickname = message.text.strip()
    if len(nickname) > 30:
        await message.answer("⚠️ Maksimal 30 karakter. Coba lagi:")
        return
    data = await state.get_data()
    target = data.get("edit_target", "")
    await update_account_nickname(str(message.from_user.id), target, nickname)
    await state.clear()
    btn = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Detail Akun", callback_data=f"acc_detail:{target}")]
    ])
    await message.answer(
        f"✅ *Alias berhasil disimpan!*\n\nAkun: `{target}`\nAlias baru: *{nickname}*",
        reply_markup=btn, parse_mode="Markdown"
    )


@router.callback_query(F.data.startswith("hapus_akun:"))
async def cb_hapus_akun(callback: CallbackQuery):
    tid = str(callback.from_user.id)
    target = callback.data.split(":", 1)[1]
    nickname = await get_account_nickname(tid, target)
    dn = _dn(target, nickname)
    teks = (
        f"⚠️ *KONFIRMASI HAPUS AKUN*\n\n"
        f"Akun: *{dn}*\n"
        f"`{target}`\n\n"
        f"Ini akan menghapus:\n"
        f"• Data login & session\n"
        f"• Draf pesanan akun ini\n\n"
        f"_Tidak bisa dibatalkan!_"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Ya, Hapus", callback_data=f"confirm_hapus_akun:{target}"),
         InlineKeyboardButton(text="❌ Batal", callback_data=f"acc_detail:{target}")]
    ])
    await callback.message.edit_text(teks, reply_markup=keyboard, parse_mode="Markdown")


@router.callback_query(F.data.startswith("confirm_hapus_akun:"))
async def cb_confirm_hapus_akun(callback: CallbackQuery):
    tid = str(callback.from_user.id)
    target = callback.data.split(":", 1)[1]
    if ADMIN_ID in mesin_siaga and target in mesin_siaga[ADMIN_ID]:
        engine = mesin_siaga[ADMIN_ID].pop(target)
        await engine.close()
    await delete_account(tid, target)
    await callback.answer(f"✅ Akun {target[:30]} dihapus.", show_alert=True)
    await cb_menu_akun(callback)

@router.callback_query(F.data == "add_new_acc")
async def cb_add_new_acc(callback: CallbackQuery, state: FSMContext):
    total = await count_accounts(str(callback.from_user.id))
    if total >= 2:
        await callback.answer(
            "⛔ Maksimal 2 akun. Hubungi admin untuk menambah slot.",
            show_alert=True
        )
        return
    await callback.message.edit_text(
        f"➕ **Tambah Akun** ({total}/2)\n\n"
        f"Ketik **Username atau Email** akun Siliwangi:\n\n"
        f"_Ketik /batal untuk membatalkan._",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Batal", callback_data="menu_akun")]
        ]),
        parse_mode="Markdown"
    )
    await state.set_state(AkunState.waiting_for_username)

@router.callback_query(F.data == "siapkan_semua")
async def cb_siapkan_semua(callback: CallbackQuery):
    
    await callback.answer()
    tid = str(callback.from_user.id)
    orders = await get_all_pending_orders_multi(tid)

    if not orders:
        await callback.message.edit_text(
            "⚠️ **Belum ada pesanan yang tersimpan.**\n\n"
            "Masukkan pesanan melalui menu 📦 **Input Pesanan**,\n"
            "lalu kembali ke sini untuk menyiapkan akun.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔙 Kembali", callback_data="menu_akun")]
            ]),
            parse_mode="Markdown"
        )
        return

    akun_list = ", ".join([f"`{o[1]}`" for o in orders])
    await callback.message.edit_text(
        f"⏳ **Menyiapkan {len(orders)} akun...**\n\nAkun: {akun_list}\n\n"
        f"_Proses ini memastikan semua akun sudah bisa masuk ke website._",
        parse_mode="Markdown"
    )

    mesin_siaga[ADMIN_ID] = {}
    
    async def _login_task(username):
        try:
            engine = SiliwangiEngine(telegram_id=tid, username=username)
            if await engine.login():
                mesin_siaga[ADMIN_ID][username] = engine
                return True, username
            else:
                await engine.close()
                return False, username
        except Exception:
            return False, username

    # Paralel login biar cepat
    tasks = [_login_task(o[1]) for o in orders]
    results = await asyncio.gather(*tasks)
    
    berhasil = sum(1 for r in results if r[0])
    gagal = [r[1] for r in results if not r[0]]

    baris_hasil = f"✅ *{berhasil}/{len(orders)} akun berhasil disiapkan!*"
    if gagal:
        baris_hasil += "\n❌ Gagal: " + ", ".join([f"`{u}`" for u in gagal])
        baris_hasil += "\n\n_Cek username & password akun yang gagal._"
    else:
        baris_hasil += "\n\n_Semua siap!_"

    await callback.message.edit_text(
        baris_hasil,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📊 Dashboard", callback_data="war_panel")]
        ]),
        parse_mode="Markdown"
    )

@router.message(AkunState.waiting_for_username)
async def process_username(message: Message, state: FSMContext):
    if message.text.startswith('/'):
        await message.answer("⚠️ **Format Salah!**\nJangan gunakan awalan garis miring (`/`).\n\nSilakan ketik ulang **Username/Email** dengan benar:", parse_mode="Markdown")
        return
        
    await state.update_data(username=message.text)
    await message.answer("Masukan **Password**:")
    await state.set_state(AkunState.waiting_for_password)

@router.message(AkunState.waiting_for_password)
async def process_password(message: Message, state: FSMContext):
    if message.text.startswith('/'):
        await message.answer("⚠️ **Format Salah!**\nJangan gunakan awalan garis miring (`/`).\n\nSilakan ketik ulang **Password** dengan benar:", parse_mode="Markdown")
        return
        
    data = await state.get_data()
    try:
        await save_user_credentials(str(message.from_user.id), data['username'], message.text)
    except RuntimeError as e:
        await message.answer(f"❌ **Gagal menyimpan akun:**\n{e}", parse_mode="Markdown")
        await state.clear()
        return
    except Exception as e:
        logger.error(f"Error save_user_credentials: {e}")
        await message.answer("❌ **Terjadi kesalahan sistem saat menyimpan akun.**", parse_mode="Markdown")
        await state.clear()
        return
        
    await state.clear()
    
    btn = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Kembali ke Dasbor", callback_data="kembali_ke_menu")]])
    await message.answer(f"✅ **Akun Berhasil Ditambahkan!**\n(`{data['username']}`)", reply_markup=btn, parse_mode="Markdown")


@router.callback_query(F.data == "lihat_riwayat")
async def cb_lihat_riwayat(callback: CallbackQuery):
    """Menu Utama Riwayat."""
    await callback.answer()
    teks = "📜 **PILIH JENIS RIWAYAT**\n━━━━━━━━━━━━━━\nSilakan pilih riwayat yang ingin Anda lihat:"
    keyboard = [
        [InlineKeyboardButton(text="✅ Riwayat Checkout", callback_data="riwayat_checkout_dates")],
        [InlineKeyboardButton(text="📝 Riwayat Input (Draf)", callback_data="riwayat_draft_dates")],
        [InlineKeyboardButton(text="🔙 Kembali", callback_data="kembali_ke_menu")]
    ]
    await callback.message.edit_text(teks, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode="Markdown")

@router.callback_query(F.data == "riwayat_checkout_dates")
async def cb_riwayat_checkout_dates(callback: CallbackQuery):
    await callback.answer()
    tid = str(callback.from_user.id)
    dates = await get_order_history_dates(tid)
    if not dates:
        await callback.message.edit_text(
            "📜 **RIWAYAT CHECKOUT**\n━━━━━━━━━━━━━━\n_(Belum ada riwayat tersimpan.)_",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Kembali", callback_data="lihat_riwayat")]]),
            parse_mode="Markdown"
        )
        return
    keyboard = []
    hari_id = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu"]
    bulan_id = ["", "Jan", "Feb", "Mar", "Apr", "Mei", "Jun", "Jul", "Agu", "Sep", "Okt", "Nov", "Des"]
    for d in dates:
        try:
            dt = datetime.strptime(d, "%Y-%m-%d")
            label = f"📅 {dt.day} {bulan_id[dt.month]} {dt.year} ({hari_id[dt.weekday()]})"
        except Exception:
            label = d
        keyboard.append([InlineKeyboardButton(text=label, callback_data=f"riwayat_checkout:{d}")])
    keyboard.append([InlineKeyboardButton(text="🔙 Kembali", callback_data="lihat_riwayat")])
    await callback.message.edit_text("📜 **PILIH TANGGAL CHECKOUT**\n━━━━━━━━━━━━━━", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode="Markdown")

@router.callback_query(F.data.startswith("riwayat_checkout:"))
async def cb_riwayat_checkout(callback: CallbackQuery):
    await callback.answer()
    tid  = str(callback.from_user.id)
    date_str = callback.data.split(":", 1)[1]
    rows = await get_order_history_by_date(tid, date_str)
    
    bulan_id = ["", "Januari", "Februari", "Maret", "April", "Mei", "Juni", "Juli", "Agustus", "September", "Oktober", "November", "Desember"]
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        judul = f"{dt.day} {bulan_id[dt.month]} {dt.year}"
    except Exception:
        judul = date_str

    teks = f"<b>📋 RIWAYAT CHECKOUT {judul.upper()}</b>\n━━━━━━━━━━━━━━━━━━\n\n"
    if not rows:
        teks += "<i>(Tidak ada data untuk tanggal ini.)</i>"
    else:
        for jam, username, total_maxi, payload_json, order_id, status, total_nominal in rows:
            status_icon = "✅" if status == "SUKSES" else "❌"
            teks += f"{status_icon} <b>{jam[:5]} WIB</b> — <code>{username[:28]}</code>\n"
            if order_id and order_id not in ('N/A', 'UNKNOWN'):
                teks += f"   🔖 Order ID: <code>#{order_id}</code>\n"
            if total_nominal:
                teks += f"   💰 Total: <b>{total_nominal}</b>\n"
            teks += f"   📦 MAXI: <b>{total_maxi} box</b>\n"
            try:
                keranjang = json.loads(payload_json)
                for i, item in enumerate(keranjang, 1):
                    teks += f"   {i}. {item['qty']}x {item['nama']}\n"
            except Exception:
                teks += "   🛒 <i>(data tidak terbaca)</i>\n"
            if status == "GAGAL" and total_nominal:
                teks += f"   ⚠️ Alasan: <i>{total_nominal}</i>\n"
            teks += "\n"

    btn = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Kembali ke Tanggal", callback_data="lihat_riwayat")],
        [InlineKeyboardButton(text="🏠 Menu Utama", callback_data="kembali_ke_menu")]
    ])

    # Potong pesan jika > 4096 karakter (limit Telegram)
    if len(teks) > 4000:
        teks = teks[:3997] + "..."

    await callback.message.edit_text(teks, reply_markup=btn, parse_mode="Markdown")

@router.callback_query(F.data == "hapus_order")
async def cb_hapus_order(callback: CallbackQuery):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Ya, Hapus", callback_data="confirm_hapus"), InlineKeyboardButton(text="❌ Batal", callback_data="menu_order")]
    ])
    await callback.message.edit_text("⚠️ **Yakin menghapus draf akun ini?**", reply_markup=keyboard, parse_mode="Markdown")

@router.callback_query(F.data == "confirm_hapus")
async def cb_confirm_hapus(callback: CallbackQuery):
    await delete_pending_order(str(callback.from_user.id))
    btn = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Kembali", callback_data="menu_order")]])
    await callback.message.edit_text("🗑️ **Draft dihapus!**", reply_markup=btn, parse_mode="Markdown")

@router.callback_query(F.data == "menu_order")
async def cb_menu_order(callback: CallbackQuery):
    tid = str(callback.from_user.id)
    overview = await get_all_drafts_overview(tid)
    current_user = await get_current_user(tid)

    teks = (
        "📦 *INPUT PESANAN*\n"
        "━━━━━━━━━━━━━━\n"
        "Pilih akun:\n"
    )

    keyboard = []
    for username, is_active, has_draft, total_maxi, _, nickname in overview:
        dn = _dn(username, nickname)
        d_icon = "✅" if has_draft else "📝"
        active_mark = " 🟢" if username == current_user else ""
        box_info = f" · {total_maxi}box" if has_draft else ""
        label = f"{d_icon} {dn}{active_mark}{box_info}"
        keyboard.append([InlineKeyboardButton(text=label, callback_data=f"order_acc:{username}")])

    keyboard.append([InlineKeyboardButton(text="🔙 Menu Utama", callback_data="kembali_ke_menu")])
    await callback.message.edit_text(teks, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode="Markdown")

@router.callback_query(F.data.startswith("order_acc:"))
async def cb_order_acc(callback: CallbackQuery):
    target = callback.data.split(":", 1)[1]
    tid = str(callback.from_user.id)
    current = await get_current_user(tid)
    
    # Set as active first if not active
    if target != current:
        await set_active_account(tid, target)
    
    pending = await get_pending_order(tid)
    
    teks = f"📝 **Kelola Draf: `{target}`**\n━━━━━━━━━━━━━━\n"
    if pending:
        _, total_maxi, payload_json, tgl_buat = pending
        keranjang = json.loads(payload_json)
        items = "\n".join([f"· {i['qty']}x {i['nama']}" for i in keranjang[:5]])
        if len(keranjang) > 5: items += "\n... (lebih banyak)"
        teks += f"📦 Total: **{total_maxi} Box MAXI**\n🕒 Dibuat: {tgl_buat[:16]}\n\n📋 **Isi:**\n{items}"
    else:
        teks += "⚠️ **Draf Kosong!**\nSilakan input pesanan baru."

    keyboard = [
        [InlineKeyboardButton(text="📥 Input/Ganti Pesanan", callback_data="start_input_order")],
        [InlineKeyboardButton(text="🗑️ Hapus Draf", callback_data="hapus_order")],
        [InlineKeyboardButton(text="🔙 Kembali", callback_data="menu_order")]
    ]
    await callback.message.edit_text(teks, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode="Markdown")

@router.callback_query(F.data == "start_input_order")
async def cb_start_input_order(callback: CallbackQuery, state: FSMContext):
    current_user = await get_current_user(str(callback.from_user.id))

    template = (
        f"**Order untuk Akun: `{current_user}`**\n\n"
        "- 50x MAXI Belgian Chocolate\n"
        "- 50x MAXI Black Forest\n"
        "- 15x MAXI Cokelat Dubai Pistachio\n"
        "- 10x MAXI Cokelat Tiramisu\n"
        "- 10x MAXI Brownies Coklat\n"
        "- 6x MAXI Susu Lembang\n"
        "- 2x MAXI Alpukat Mentega\n"
        "- 2x MAXI Talas Bogor\n"
        "- 2x MAXI Black Pink\n"
        "- 8x MAXI Pandan Wangi\n"
        "- 8x MAXI Red Velvet\n"
        "- 1x MAXI Keju Cheddar\n"
        "- 3x MAXI Durian Musang King\n"
        "- 3x MAXI Durian Montong\n"
        "- 1x MAXI Mangga Indramayu\n"
        "- 2x MAXI Original Lapis\n"
        "- 2x DC Belgian Chocolate\n"
        "- 2x DC Black Forest\n"
        "- 50x Plastik Bolu Klasik HD Isi 3 Box\n"
        "- 50x Plastik Bakpia Kukus HD Isi 3 Box\n\n"
        "_Salin teks di atas, edit angka, lalu kirim._\n"
        "_Ketik /batal untuk membatalkan._"
    )
    await callback.message.edit_text(
        template,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Batal", callback_data="menu_order")]
        ]),
        parse_mode="Markdown"
    )
    await state.set_state(OrderState.waiting_for_template)
    await callback.answer()

@router.message(OrderState.editing_existing)
async def process_edit_existing(message: Message, state: FSMContext):
    """Proses input edit — validasi dulu, baru hapus draf lama dan simpan baru."""
    # Gunakan logika yang sama dengan process_template
    await _proses_input_order(message, state, is_edit=True)

@router.message(OrderState.waiting_for_template)
async def process_template(message: Message, state: FSMContext):
    await _proses_input_order(message, state, is_edit=False)

async def _proses_input_order(message: Message, state: FSMContext, is_edit: bool = False):
    """Core logic parse + validasi + preview konfirmasi input pesanan."""
    products_db = await get_all_products_dict()
    # Buat lookup case-insensitive
    products_db_lower = {k.lower(): (k, v) for k, v in products_db.items()}
    
    lines = message.text.strip().split('\n')
    keranjang = []
    total_maxi = 0
    baris_tidak_dikenal = []

    for line in lines:
        line = line.strip()
        if not line or not line.startswith('-'):
            continue
        try:
            parts = line.split('x ', 1)
            qty = int(parts[0].replace('-', '').strip())
            nama_produk_raw = parts[1].strip()
            nama_produk_lower = nama_produk_raw.lower()
            if qty <= 0:
                continue
            if nama_produk_lower in products_db_lower:
                nama_produk_asli, prod_info = products_db_lower[nama_produk_lower]
                keranjang.append({
                    "id": prod_info["id"], "nama": nama_produk_asli,
                    "qty": qty, "kategori": prod_info["kategori"], "tier": prod_info["tier"]
                })
                if prod_info["kategori"] == "MAXI":
                    total_maxi += qty
            else:
                baris_tidak_dikenal.append(f"`{nama_produk_raw}`")
        except Exception:
            pass

    # Tampilkan peringatan baris tidak dikenal
    if baris_tidak_dikenal:
        await message.answer(
            f"⚠️ **Produk tidak dikenal (dilewati):**\n" +
            "\n".join(baris_tidak_dikenal) +
            "\n\n_Pastikan nama persis sama dengan template._",
            parse_mode="Markdown"
        )

    if not keranjang:
        await message.answer(
            "❌ **Tidak ada produk yang dikenali.**\n"
            "Pastikan format: `- 50x MAXI Belgian Chocolate`\n"
            "_(Nama harus sama persis dengan template)_",
            parse_mode="Markdown"
        )
        return

    # Validasi minimal 50 box kue
    total_kue = sum(i['qty'] for i in keranjang if i['kategori'] in ['MAXI', 'DC'])
    if total_kue < 50:
        await message.answer(
            f"⚠️ **Minimal Order Belum Terpenuhi**\n"
            f"Total kue (MAXI+DC): **{total_kue} box** — wajib min. **50 box**.",
            parse_mode="Markdown"
        )
        return

    # Validasi kelipatan MAXI
    if total_maxi > 0 and total_maxi % 12 != 0:
        sisa = total_maxi % 12
        await message.answer(
            f"⚠️ **Kelipatan MAXI Salah**\n"
            f"Total MAXI: **{total_maxi} pcs** — wajib kelipatan 12.\n"
            f"⬇️ Kurangi **{sisa}** atau tambah **{12-sisa}**.",
            parse_mode="Markdown"
        )
        return

    # Validasi kelipatan DC
    total_dc = sum(i['qty'] for i in keranjang if i['kategori'] == 'DC')
    if total_dc > 0 and total_dc % 4 != 0:
        sisa = total_dc % 4
        await message.answer(
            f"⚠️ **Kelipatan DC Salah**\n"
            f"Total DC: **{total_dc} pcs** — wajib kelipatan 4.\n"
            f"⬇️ Kurangi **{sisa}** atau tambah **{4-sisa}**.",
            parse_mode="Markdown"
        )
        return

    # Simpan ke FSM state untuk konfirmasi
    await state.update_data(keranjang=keranjang, total_maxi=total_maxi, is_edit=is_edit)

    # Preview konfirmasi
    current_user = await get_current_user(str(message.from_user.id))
    preview_items = "\n".join(
        [f"  · {i['qty']}x {i['nama']} (T{i['tier']})" for i in keranjang]
    )
    teks_konfirm = (
        f"📋 **PREVIEW PESANAN**\n"
        f"👤 Akun: `{current_user}`\n\n"
        f"{preview_items}\n\n"
        f"📦 Total MAXI: **{total_maxi} pcs**\n"
        f"🧁 Total Kue: **{total_kue} pcs**\n\n"
        f"_Sudah benar? Klik Simpan._"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Simpan", callback_data="confirm_simpan_order"),
         InlineKeyboardButton(text="✏️ Input Ulang", callback_data="start_input_order")],
        [InlineKeyboardButton(text="🔙 Batal", callback_data="menu_order")]
    ])
    await message.answer(teks_konfirm, reply_markup=keyboard, parse_mode="Markdown")
    await state.set_state(OrderState.confirming_order)

@router.callback_query(F.data == "confirm_simpan_order")
async def cb_confirm_simpan_order(callback: CallbackQuery, state: FSMContext):
    """Simpan draf setelah user konfirmasi preview."""
    data = await state.get_data()
    keranjang  = data.get('keranjang', [])
    total_maxi = data.get('total_maxi', 0)

    if not keranjang:
        await callback.answer("Data tidak ditemukan. Input ulang.", show_alert=True)
        await state.clear()
        return

    # simpan_draft_order sudah atomik (delete lama + insert baru)
    current_user = await get_current_user(str(callback.from_user.id))
    await simpan_draft_order(str(callback.from_user.id), total_maxi, keranjang)
    await state.clear()

    btn = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Lihat Semua Draf", callback_data="menu_order")],
        [InlineKeyboardButton(text="🏠 Menu Utama", callback_data="kembali_ke_menu")]
    ])
    await callback.message.edit_text(
        f"✅ **Draf tersimpan untuk `{current_user}`!**\n"
        f"📦 {total_maxi} MAXI · {len(keranjang)} produk",
        reply_markup=btn, parse_mode="Markdown"
    )


@router.callback_query(F.data == "net_diag")
async def cb_net_diag(callback: CallbackQuery):
    """Cek koneksi internet, website target, Cloudflare, dan session akun."""
    import httpx as _httpx
    import time as _time

    await callback.answer()
    await callback.message.edit_text(
        "🌐 *CEK JARINGAN & SESSION*\n━━━━━━━━━━━━━━\n⏳ Sedang mengecek...",
        parse_mode="Markdown"
    )

    tid = str(callback.from_user.id)

    # 1. Cek koneksi bot → internet
    internet_ok, latency_ms = False, 0
    try:
        t0 = _time.monotonic()
        async with _httpx.AsyncClient(timeout=5.0) as c:
            await c.get("https://1.1.1.1")
        latency_ms = int((_time.monotonic() - t0) * 1000)
        internet_ok = True
    except Exception:
        pass

    # 2. Cek website target
    website_ok, cf_blocked = False, False
    try:
        async with _httpx.AsyncClient(timeout=10.0, follow_redirects=True) as c:
            resp = await c.get("https://siliwangibolukukus.com/")
            website_ok = resp.status_code < 500
            cf_blocked = resp.status_code in [403, 503] and "cloudflare" in resp.text.lower()
    except Exception:
        pass

    # 3. Cek session per akun
    accounts = await get_all_accounts_with_status(tid)
    session_lines = []
    for acc, _, has_session, nickname in accounts:
        dn = _dn(acc, nickname)
        icon = "✅" if has_session else "⚠️"
        keterangan = "Aktif" if has_session else "Expired / belum login"
        session_lines.append(f"  {icon} *{dn}* — {keterangan}")

    inet_txt = f"✅ OK ({latency_ms}ms)" if internet_ok else "❌ Tidak terhubung"
    if not website_ok:
        web_txt = "❌ Tidak dapat dijangkau"
    elif cf_blocked:
        web_txt = "⚠️ Cloudflare memblokir"
    else:
        web_txt = "✅ Online"
    sesi_txt = "\n".join(session_lines) if session_lines else "  _(tidak ada akun)_"

    teks = (
        "🌐 *CEK JARINGAN & SESSION*\n"
        "━━━━━━━━━━━━━━\n"
        f"🔌 Koneksi Bot     : {inet_txt}\n"
        f"🌍 Website Target  : {web_txt}\n\n"
        f"🔑 *Session Akun:*\n{sesi_txt}"
    )

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Cek Ulang", callback_data="net_diag"),
         InlineKeyboardButton(text="🧹 Reset Semua Session", callback_data="net_diag_reset_confirm")],
        [InlineKeyboardButton(text="🔙 Panel War", callback_data="war_panel")]
    ])
    await callback.message.edit_text(teks, reply_markup=keyboard, parse_mode="Markdown")


@router.callback_query(F.data == "net_diag_reset_confirm")
async def cb_net_diag_reset_confirm(callback: CallbackQuery):
    await callback.message.edit_text(
        "⚠️ *KONFIRMASI RESET SESSION*\n\n"
        "Semua cookie login akan dihapus.\n"
        "Bot perlu login ulang saat warm-up 07:55.\n\n"
        "_Yakin ingin reset?_",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Ya, Reset Semua", callback_data="net_diag_reset_execute"),
             InlineKeyboardButton(text="❌ Batal", callback_data="net_diag")]
        ]),
        parse_mode="Markdown"
    )


@router.callback_query(F.data == "net_diag_reset_execute")
async def cb_net_diag_reset_execute(callback: CallbackQuery):
    tid = str(callback.from_user.id)
    accounts = await get_all_accounts(tid)
    for username, _ in accounts:
        await clear_session_cookies(tid, username)
    mesin_siaga.pop(ADMIN_ID, None)
    await callback.answer("✅ Semua session berhasil direset!", show_alert=True)
    await cb_net_diag(callback)

async def main():

    init_db() 
    dp.include_router(router)
    scheduler.start()
    
    await bot.delete_webhook(drop_pending_updates=True)
    
    print("🚀 Bot JAGO Ready...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())