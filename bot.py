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

from database import (
    save_user_credentials, get_all_products_dict, simpan_draft_order,
    get_current_user, get_pending_order, delete_pending_order
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

# Pastikan timezone aman menggunakan Asia/Jakarta
zona_waktu = pytz.timezone('Asia/Jakarta')
scheduler = AsyncIOScheduler(timezone=zona_waktu)

router.message.filter(F.from_user.id == ADMIN_ID)
router.callback_query.filter(F.from_user.id == ADMIN_ID)

class AkunState(StatesGroup):
    waiting_for_username = State()
    waiting_for_password = State()

class OrderState(StatesGroup):
    waiting_for_template = State()

# ==========================================
# SCHEDULER: PEMANASAN (07:55) & EKSEKUSI (08:00)
# ==========================================
mesin_siaga = {} # Menyimpan sesi login di RAM

async def job_pemanasan():
    """Berjalan & Melakukan Login, tetap Standby."""
    logger.info("Warm Up...")
    await bot.send_message(ADMIN_ID, "⚙️ **[AUTO-SYSTEM] 07:55 WIB**\nMemulai pemanasan & Mengamankan cookies login...")
    
    engine = SiliwangiEngine(telegram_id=str(ADMIN_ID))
    if await engine.login():
        mesin_siaga[ADMIN_ID] = engine
        await bot.send_message(ADMIN_ID, "✅ **Login Standby Aman!** Bot bersiap menunggu...")
    else:
        await bot.send_message(ADMIN_ID, "❌ **[GAGAL]** Login gagal! Cek akunmu.")
        await engine.close()

async def job_eksekusi():
    """Langsung Checkout."""
    logger.info("Memulai Eksekusi (08:00)...")
    
    # Ambil mesin yang sudah dipanaskan tadi
    engine = mesin_siaga.get(ADMIN_ID)
    
    # Fallback: Jika pemanasan gagal/terlewat, buat instansi baru dan login dadakan
    if not engine:
        engine = SiliwangiEngine(telegram_id=str(ADMIN_ID))
        await engine.login()

    try:
        if await engine.execute_order():
            await bot.send_message(ADMIN_ID, "🎉 **BERHASIL (Direct Hit)!**\nSilakan cek web Siliwangi!", parse_mode="Markdown")
        else:
            await bot.send_message(ADMIN_ID, "⚠️ **GAGAL.**\nCek file log. Server sibuk atau stok habis.", parse_mode="Markdown")
    finally:
        await engine.close()
        mesin_siaga.pop(ADMIN_ID, None) # Hapus dari memori setelah selesai

# Jadwalkan alarmnya!
scheduler.add_job(job_pemanasan, 'cron', hour=7, minute=55, second=0)
scheduler.add_job(job_eksekusi, 'cron', hour=8, minute=00, second=0)

# ==========================================
# MENU UTAMA (UI BERSIH)
# ==========================================
def get_main_menu_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📦 Input Pesanan", callback_data="menu_order")],
        [InlineKeyboardButton(text="📝 Kelola Order", callback_data="menu_kelola")],
        [InlineKeyboardButton(text="📊 Status", callback_data="menu_status")]
    ])

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    current_user = get_current_user(str(message.from_user.id))
    status_akun = f"🟢 {current_user}" if current_user else "🔴 Belum Setting"
    teks = (f"🤖 **Siliwangi War Bot**\n\n👤 Akun: {status_akun}\n\nPilih aksi di bawah ini:")
    await message.answer(teks, reply_markup=get_main_menu_keyboard(), parse_mode="Markdown")

@router.message(Command("akun"))
async def cmd_akun(message: Message, state: FSMContext):
    await message.answer("**Menu Rahasia: Ganti Akun**\nMasukan **Username/Email**:", parse_mode="Markdown")
    await state.set_state(AkunState.waiting_for_username)

@router.message(AkunState.waiting_for_username)
async def process_username(message: Message, state: FSMContext):
    await state.update_data(username=message.text)
    await message.answer("Masukan **Password**:")
    await state.set_state(AkunState.waiting_for_password)

@router.message(AkunState.waiting_for_password)
async def process_password(message: Message, state: FSMContext):
    data = await state.get_data()
    save_user_credentials(str(message.from_user.id), data['username'], message.text)
    await state.clear()
    await message.answer("✅ **Data Akun Diperbarui!**", parse_mode="Markdown")

@router.callback_query(F.data == "menu_status")
async def cb_menu_status(callback: CallbackQuery):
    # Tampilkan waktu menggunakan timezone Jakarta agar akurat
    now = datetime.now(zona_waktu).strftime("%d %B %Y, %H:%M:%S WIB")
    pending = get_pending_order(str(callback.from_user.id))
    status_order = "1 PENDING ⏳" if pending else "KOSONG ⭕"
    
    teks = (f"📊 **STATUS**\n\n🕒 **Waktu:** {now}\n⚙️ **Scheduler:** Standby 24 Jam ✅\n🛒 **Draft:** {status_order}\n\n*(Sesi Login: 07:55 | Eksekusi: 08:00)*")
    btn = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Kembali", callback_data="kembali_ke_menu")]])
    await callback.message.edit_text(teks, reply_markup=btn, parse_mode="Markdown")

@router.callback_query(F.data == "menu_kelola")
async def cb_menu_kelola(callback: CallbackQuery):
    pending = get_pending_order(str(callback.from_user.id))
    if not pending:
        await callback.answer("Tidak ada draft PENDING.", show_alert=True)
        return
    order_id, total_maxi, payload_json = pending
    keranjang = json.loads(payload_json)
    teks_keranjang = "\n".join([f"- {item['qty']}x {item['nama']}" for item in keranjang])
    
    teks = (f"📝 **KELOLA ORDER**\n\n{teks_keranjang}\n\n📦 **Total MAXI:** {total_maxi} pcs")
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Edit Order", callback_data="edit_order")],
        [InlineKeyboardButton(text="🗑️ Hapus Order", callback_data="hapus_order")],
        [InlineKeyboardButton(text="🔙 Kembali", callback_data="kembali_ke_menu")]
    ])
    await callback.message.edit_text(teks, reply_markup=keyboard, parse_mode="Markdown")

@router.callback_query(F.data == "hapus_order")
async def cb_hapus_order(callback: CallbackQuery):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Ya, Hapus", callback_data="confirm_hapus"), InlineKeyboardButton(text="❌ Batal", callback_data="menu_kelola")]
    ])
    await callback.message.edit_text("⚠️ **Yakin menghapus order ini?**", reply_markup=keyboard, parse_mode="Markdown")

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
    template = (
        "Salin dan edit nama & kuantitas semau kamu:\n\n"
        "- 50x MAXI Belgian Chocolate\n"
        "- 50x MAXI Black Forest\n"
        "- 12x MAXI Cokelat Dubai Pistachio\n"
        "- 12x MAXI Cokelat Tiramisu\n"
        "- 8x MAXI Brownies Coklat\n"
        "- 6x MAXI Susu Lembang\n"
        "- 3x MAXI Alpukat Mentega\n"
        "- 2x MAXI Talas Bogor\n"
        "- 8x MAXI Pandan Wangi\n"
        "- 8x MAXI Red Velvet\n"
        "- 1x MAXI Keju Cheddar\n"
        "- 1x MAXI Black Pink\n"
        "- 1x MAXI Durian Montong\n"
        "- 3x MAXI Durian Musang King\n"
        "- 1x MAXI Mangga Indramayu\n"
        "- 3x MAXI Original Lapis\n"
        "- 1x DC Belgian Chocolate\n"
        "- 1x DC Black Forest\n"
        "- 50x Plastik Bolu Klasik HD Isi 3 Box\n\n"
        "*(Catatan: Hapus yang tidak perlu, atau cukup jadikan 0x)*"
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

    # Validasi 1: Minimal 50 Box (MAXI + DC)
    total_kue = sum(item['qty'] for item in keranjang if item['kategori'] in ['MAXI', 'DC'])
    if total_kue < 50:
        await message.answer(
            f"⚠️ **PERINGATAN ORDER** ⚠️\n"
            f"Total (MAXI + DC): **{total_kue} box**.\n"
            f"Minimal **50 box**.\n"
            f"*(Silakan copy & edit template sebelumnya agar mencapai 50+ box)*",
            parse_mode="Markdown"
        )
        return

    # Validasi 2: Kelipatan 12 khusus MAXI
    sisa = total_maxi % 12
    if sisa != 0:
        kurang = sisa
        tambah = 12 - sisa
        bawah = total_maxi - kurang
        atas = total_maxi + tambah
        
        await message.answer(
            f"⚠️ **PERINGATAN KELIPATAN 12** ⚠️\n"
            f"Total MAXI: **{total_maxi} pcs** (Ditolak).\n\n"
            f"⬇️ Kurangi **{kurang}** pcs agar menjadi **{bawah}**.\n"
            f"⬆️ Atau Tambah **{tambah}** pcs agar menjadi **{atas}**.\n\n"
            f"*(Silakan copy & edit template sebelumnya agar pas kelipatan 12)*",
            parse_mode="Markdown"
        )
        return 

    # Lolos Semua Validasi: Simpan ke Database
    delete_pending_order(str(message.from_user.id))
    simpan_draft_order(str(message.from_user.id), total_maxi, keranjang)
    
    btn = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Kembali", callback_data="kembali_ke_menu")]])
    await message.answer(f"🎉 **Draf Disimpan! (Total MAXI: {total_maxi} pcs)**", reply_markup=btn, parse_mode="Markdown")
    await state.clear()

@router.callback_query(F.data == "kembali_ke_menu")
async def cb_kembali(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    current_user = get_current_user(str(callback.from_user.id))
    status_akun = f"🟢 {current_user}" if current_user else "🔴 Belum Setting"
    teks = (f"🤖 **Siliwangi War Bot**\n\n👤 Akun: {status_akun}\n\nPilih aksi di bawah ini:")
    await callback.message.edit_text(teks, reply_markup=get_main_menu_keyboard(), parse_mode="Markdown")
    await callback.answer()

async def main():
    dp.include_router(router)
    scheduler.start()
    print("🚀 Bot JAGO Readyy...!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())