import asyncio
import os
import json
import logging
import pytz
from datetime import datetime
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# [UPDATE V2] Mengimpor fungsi database baru
from database import (
    save_user_credentials, get_all_products_dict, simpan_draft_order,
    get_current_user, get_pending_order, delete_pending_order,
    get_all_accounts, set_active_account, get_all_pending_orders_multi,
    get_order_history # [FITUR BARU] Mengambil riwayat order
)
from engine import SiliwangiEngine

logging.basicConfig(filename='siliwangi_error.log', level=logging.INFO, format='%(asctime)s - %(levelname)s - [BOT] %(message)s')
logger = logging.getLogger(__name__)

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))

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

mesin_siaga = {} 

# ==========================================
# FASE 3 & 4: MULTI-ACCOUNT GATEKEEPER & PARALLEL EXECUTION
# ==========================================

async def eksekusi_dengan_jeda(engine, delay, username):
    """Menunggu sebelum War"""
    if delay > 0:
        await asyncio.sleep(delay)
    
    logger.info(f"🔫 akun: {username} (Delay: {delay}s)")
    hasil = await engine.execute_order()
    return username, hasil

async def job_pemanasan():
    logger.info("Warm Up (07:55)...")
    orders = get_all_pending_orders_multi(str(ADMIN_ID))
    
    if not orders:
        logger.info("[Libur] Tidak ada draf pesanan.")
        return

    await bot.send_message(ADMIN_ID, f"⚙️ Terdeteksi {len(orders)} draf pesanan! Memulai pemanasan massal...")
    
    # Menyiapkan pasukan (mesin) sebanyak jumlah draf akun
    mesin_siaga[ADMIN_ID] = {}
    berhasil_login = 0
    
    for order in orders:
        username = order[1]
        engine = SiliwangiEngine(telegram_id=str(ADMIN_ID), username=username)
        if await engine.login():
            mesin_siaga[ADMIN_ID][username] = engine
            berhasil_login += 1
        else:
            await engine.close()

    if berhasil_login > 0:
        await bot.send_message(ADMIN_ID, f"✅ *{berhasil_login} Akun Standby!*")
    else:
        await bot.send_message(ADMIN_ID, "❌ **[GAGAL]** Tidak ada akun yang berhasil login.")

async def job_eksekusi():
    logger.info("Mengecek jadwal (08:00)...")
    pasukan = mesin_siaga.get(ADMIN_ID, {})
    
    if not pasukan:
        logger.info("🏖️ [MODE CUTI/GAGAL] Eksekusi dibatalkan.")
        return

    logger.info(f"🚀 MEMULAI WAR {len(pasukan)} AKUN!")
    
    # Mengumpulkan tugas tembakan dengan strategi Micro-Delay (0.3 detik antar akun)
    tasks = []
    jeda = 0.0
    for username, engine in pasukan.items():
        tasks.append(eksekusi_dengan_jeda(engine, jeda, username))
        jeda += 0.3 # Akun berikutnya akan telat 300 milidetik
        
    # EKSEKUSI SEMUANYA SECARA BERSAMAAN (CONCURRENCY)
    hasil_perang = await asyncio.gather(*tasks)
    
    # Membersihkan memori mesin & Rekap Laporan
    laporan = "📊 **HASIL WAR 08:00 WIB:**\n\n"
    for target_username, is_success in hasil_perang:
        status = "✅ BERHASIL" if is_success else "❌ GAGAL/HABIS"
        laporan += f"👤 `{target_username}`: {status}\n"
        
        # Tutup browser virtual untuk akun ini
        engine = pasukan.get(target_username)
        if engine: await engine.close()
        
    mesin_siaga.pop(ADMIN_ID, None)
    
    await bot.send_message(ADMIN_ID, laporan, parse_mode="Markdown")
    logger.info("War Selesai.")

scheduler.add_job(job_pemanasan, 'cron', hour=7, minute=55, second=0)
scheduler.add_job(job_eksekusi, 'cron', hour=8, minute=0, second=0)

# ==========================================
# FASE 2: DASBOR MULTI-AKUN (UI/UX)
# ==========================================
def get_main_menu_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📦 Input Pesanan", callback_data="menu_order")],
        [InlineKeyboardButton(text="👥 Kelola Multi-Akun", callback_data="menu_akun")],
        [InlineKeyboardButton(text="📝 Cek Draf & Kelola", callback_data="menu_kelola")],
        [InlineKeyboardButton(text="📊 Status", callback_data="menu_status")]
    ])

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    current_user = get_current_user(str(message.from_user.id))
    status_akun = f"{current_user}" if current_user else "Belum Ada Akun"
    
    teks = (
        f"🤖 **Bot JAGO**\n\n"
        f"🟢 **Akun Aktif:** `{status_akun}`\n\n"
        f"*(Input pesanan akan otomatis masuk ke Akun Aktif)*"
    )
    await message.answer(teks, reply_markup=get_main_menu_keyboard(), parse_mode="Markdown")

# [FITUR BARU] Menu Manajemen Multi-Akun
@router.callback_query(F.data == "menu_akun")
async def cb_menu_akun(callback: CallbackQuery):
    accounts = get_all_accounts(str(callback.from_user.id))
    keyboard = []
    
    for acc, is_active in accounts:
        status = "🟢" if is_active else "⚪"
        # Memotong username jika terlalu panjang untuk tombol
        label = f"{status} {acc[:25]}..." if len(acc) > 25 else f"{status} {acc}"
        keyboard.append([InlineKeyboardButton(text=label, callback_data=f"setacc:{acc}")])
        
    keyboard.append([InlineKeyboardButton(text="➕ Tambah Akun Baru", callback_data="add_new_acc")])
    keyboard.append([InlineKeyboardButton(text="🔙 Kembali", callback_data="kembali_ke_menu")])
    
    teks = (
        "👥 **Manajemen Multi-Akun**\n\n"
        "Klik nama akun di bawah ini untuk **menjadikannya Akun Aktif**, "
        "atau klik Tambah Akun Baru."
    )
    await callback.message.edit_text(teks, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode="Markdown")

@router.callback_query(F.data.startswith("setacc:"))
async def cb_setacc(callback: CallbackQuery):
    target_acc = callback.data.split(":", 1)[1]
    set_active_account(str(callback.from_user.id), target_acc)
    await callback.answer(f"✅ Kendali pindah ke: {target_acc}", show_alert=True)
    # Refresh ke menu utama
    await cb_kembali(callback, FSMContext)

@router.callback_query(F.data == "add_new_acc")
async def cb_add_new_acc(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("➕ **Tambah Akun**\nMasukan **Username/Email**:", parse_mode="Markdown")
    await state.set_state(AkunState.waiting_for_username)

# [PERBAIKAN BUG] Pelindung Anti-Salah Ketik (Bug /daftar)
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
    save_user_credentials(str(message.from_user.id), data['username'], message.text)
    await state.clear()
    
    btn = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Kembali ke Dasbor", callback_data="kembali_ke_menu")]])
    await message.answer(f"✅ **Akun Berhasil Ditambahkan!**\n(`{data['username']}`)", reply_markup=btn, parse_mode="Markdown")

@router.callback_query(F.data == "menu_status")
async def cb_menu_status(callback: CallbackQuery):
    now = datetime.now(zona_waktu).strftime("%d %B %Y, %H:%M:%S WIB")
    
    # Menghitung total orderan dari semua akun
    orders = get_all_pending_orders_multi(str(callback.from_user.id))
    total_draf = len(orders)
    status_order = f"{total_draf} PENDING ⏳" if total_draf > 0 else "KOSONG (Liburr 🏖️)"
    
    teks = (f"📊 **STATUS**\n\n🕒 **Waktu:** {now}\n\n🛒 **Total Draf (Semua Akun):** {status_order}\n")
    btn = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Kembali", callback_data="kembali_ke_menu")]])
    await callback.message.edit_text(teks, reply_markup=btn, parse_mode="Markdown")

# [FITUR BARU] Modifikasi Menu Kelola & Tambah Tombol Riwayat
@router.callback_query(F.data == "menu_kelola")
async def cb_menu_kelola(callback: CallbackQuery):
    current_user = get_current_user(str(callback.from_user.id))
    pending = get_pending_order(str(callback.from_user.id))
    
    keyboard = []
    if pending:
        order_id, total_maxi, payload_json = pending
        keranjang = json.loads(payload_json)
        teks_keranjang = "\n".join([f"- {item['qty']}x {item['nama']}" for item in keranjang])
        
        teks = f"📝 **DRAF AKUN: {current_user}**\n\n{teks_keranjang}\n\n📦 **Total MAXI:** {total_maxi} pcs"
        keyboard.append([InlineKeyboardButton(text="✏️ Edit Order", callback_data="edit_order")])
        keyboard.append([InlineKeyboardButton(text="🗑️ Hapus Order", callback_data="hapus_order")])
    else:
        teks = f"⭕ Tidak ada draf PENDING untuk akun **{current_user}**.\n\n*(Jika ingin melihat draf akun lain, ganti Akun Aktif di menu Kelola Multi-Akun)*"

    # Menyisipkan Tombol Riwayat
    keyboard.append([InlineKeyboardButton(text="📜 Lihat Riwayat Order", callback_data="lihat_riwayat")])
    keyboard.append([InlineKeyboardButton(text="🔙 Kembali", callback_data="kembali_ke_menu")])
    
    await callback.message.edit_text(teks, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode="Markdown")

# [FITUR BARU] Fungsi Membaca Riwayat dari Database
@router.callback_query(F.data == "lihat_riwayat")
async def cb_lihat_riwayat(callback: CallbackQuery):
    current_user = get_current_user(str(callback.from_user.id))
    rows = get_order_history(str(callback.from_user.id), current_user)
    
    if not rows:
        await callback.answer("Belum ada riwayat sukses untuk akun ini.", show_alert=True)
        return
        
    teks = f"📜 **RIWAYAT ORDER: {current_user}** (3 Terakhir)\n\n"
    for tgl, total, payload in rows:
        keranjang = json.loads(payload)
        preview = ", ".join([f"{i['qty']}x {i['nama']}" for i in keranjang[:3]])
        if len(keranjang) > 3:
            preview += "..."
        teks += f"🗓️ **{tgl}**\n📦 Total: {total} pcs\n🛒 Isi: {preview}\n\n"
        
    btn = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Kembali", callback_data="menu_kelola")]])
    await callback.message.edit_text(teks, reply_markup=btn, parse_mode="Markdown")

@router.callback_query(F.data == "hapus_order")
async def cb_hapus_order(callback: CallbackQuery):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Ya, Hapus", callback_data="confirm_hapus"), InlineKeyboardButton(text="❌ Batal", callback_data="menu_kelola")]
    ])
    await callback.message.edit_text("⚠️ **Yakin menghapus draf akun ini?**", reply_markup=keyboard, parse_mode="Markdown")

@router.callback_query(F.data == "confirm_hapus")
async def cb_confirm_hapus(callback: CallbackQuery):
    delete_pending_order(str(callback.from_user.id))
    btn = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Kembali", callback_data="kembali_ke_menu")]])
    await callback.message.edit_text("🗑️ **Draft dihapus!**", reply_markup=btn, parse_mode="Markdown")

@router.callback_query(F.data == "edit_order")
async def cb_edit_order(callback: CallbackQuery, state: FSMContext):
    pending = get_pending_order(str(callback.from_user.id))
    if not pending: return
    _, _, payload_json = pending
    keranjang = json.loads(payload_json)
    delete_pending_order(str(callback.from_user.id))
    
    teks_template = "Salin dan edit:\n\n"
    for item in keranjang:
        teks_template += f"- {item['qty']}x {item['nama']}\n"
    
    await callback.message.edit_text(teks_template, parse_mode="Markdown")
    await state.set_state(OrderState.waiting_for_template)

@router.callback_query(F.data == "menu_order")
async def cb_menu_order(callback: CallbackQuery, state: FSMContext):
    current_user = get_current_user(str(callback.from_user.id))
    if not current_user:
        await callback.answer("Tambahkan akun Siliwangi terlebih dahulu!", show_alert=True)
        return

    template = (
        f" **Order untuk Akun: {current_user}**\n\n"
        "Salin dan edit kuantitas & nama semau kamu:\n\n"
        "- 50x MAXI Belgian Chocolate\n"
        "- 50x MAXI Black Forest\n"
        "- 15x MAXI Cokelat Dubai Pistachio\n"
        "- 14x MAXI Cokelat Tiramisu\n"
        "- 8x MAXI Brownies Coklat\n"
        "- 6x MAXI Susu Lembang\n"
        "- 3x MAXI Alpukat Mentega\n"
        "- 2x MAXI Talas Bogor\n"
        "- 8x MAXI Pandan Wangi\n"
        "- 8x MAXI Red Velvet\n"
        "- 1x MAXI Keju Cheddar\n"
        "- 3x MAXI Durian Musang King\n"
        "- 1x MAXI Mangga Indramayu\n"
        "- 3x MAXI Original Lapis\n"
        "- 0x DC Belgian Chocolate\n"
        "- 0x DC Black Forest\n"
        "- 50x Plastik Bolu Klasik HD Isi 3 Box\n\n"
"*(Catatan: Hapus baris yang tidak perlu, atau cukup jadikan 0x)*"
    )
    await callback.message.edit_text(template, parse_mode="Markdown")
    await state.set_state(OrderState.waiting_for_template)
    await callback.answer()

@router.message(OrderState.waiting_for_template)
async def process_template(message: Message, state: FSMContext):
    products_db = get_all_products_dict()
    lines = message.text.strip().split('\n')
    keranjang = []
    total_maxi = 0

    for line in lines:
        line = line.strip()
        if not line or not line.startswith('-'): continue
        try:
            parts = line.split('x ', 1)
            qty = int(parts[0].replace('-', '').strip())
            nama_produk = parts[1].strip()

            if qty <= 0: continue
            if nama_produk in products_db:
                prod_info = products_db[nama_produk]
                keranjang.append({
                    "id": prod_info["id"], "nama": nama_produk, 
                    "qty": qty, "kategori": prod_info["kategori"], "tier": prod_info["tier"]
                })
                if prod_info["kategori"] == "MAXI": total_maxi += qty
        except Exception:
            pass

    if not keranjang:
        await message.answer("⚠️ Keranjang kosong. Pastikan format teks sudah benar.")
        return

    total_kue = sum(item['qty'] for item in keranjang if item['kategori'] in ['MAXI', 'DC'])
    if total_kue < 50:
        await message.answer(
            f"⚠️ **PERINGATAN MINIMAL ORDER**\nTotal kue (MAXI + DC): **{total_kue} box**.\nWeb Siliwangi mewajibkan minimal **50 box**.",
            parse_mode="Markdown"
        )
        return

    sisa = total_maxi % 12
    if sisa != 0:
        kurang, tambah = sisa, 12 - sisa
        await message.answer(
            f"⚠️ **PERINGATAN KELIPATAN 12**\nTotal MAXI kamu: **{total_maxi} pcs**.\n\n⬇️ Kurangi **{kurang}** agar menjadi **{total_maxi - kurang}**.\n⬆️ Atau Tambah **{tambah}** agar menjadi **{total_maxi + tambah}**.",
            parse_mode="Markdown"
        )
        return 

    delete_pending_order(str(message.from_user.id))
    simpan_draft_order(str(message.from_user.id), total_maxi, keranjang)
    
    current_user = get_current_user(str(message.from_user.id))
    btn = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Kembali", callback_data="kembali_ke_menu")]])
    await message.answer(f"🎉 **Draf Tersimpan untuk {current_user}!**\n*(Total MAXI: {total_maxi} pcs)*", reply_markup=btn, parse_mode="Markdown")
    await state.clear()

@router.callback_query(F.data == "kembali_ke_menu")
async def cb_kembali(callback: CallbackQuery, state: FSMContext):
    # Digunakan state.clear() tapi kita buat aman dengan type check
    if hasattr(state, 'clear'):
        await state.clear()
        
    current_user = get_current_user(str(callback.from_user.id))
    status_akun = f"{current_user}" if current_user else "Belum Ada Akun"
    
    teks = (
        f"🤖 **Bot JAGO**\n\n"
        f"🟢 **Akun Aktif:** `{status_akun}`\n\n"
        f"*(Input pesanan akan otomatis masuk ke Akun Aktif)*"
    )
    # Gunakan try/except untuk menghindari error 'Message is not modified' jika UI tidak berubah
    try:
        await callback.message.edit_text(teks, reply_markup=get_main_menu_keyboard(), parse_mode="Markdown")
    except Exception:
        pass
    await callback.answer()

async def main():
    dp.include_router(router)
    scheduler.start()
    
    # TAMBAHKAN BARIS INI UNTUK MENYAPU ANTREAN TELEGRAM
    await bot.delete_webhook(drop_pending_updates=True)
    
    print("🚀 Bot JAGO Ready...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())