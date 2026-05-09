import asyncio
import re
import urllib.parse
import httpx
from bs4 import BeautifulSoup
import sqlite3
import json
from datetime import datetime, timedelta
import pytz
import logging
import os

from database import decrypt_password

logging.basicConfig(
    filename='siliwangi_error.log',
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - [ENGINE] %(message)s'
)
logger = logging.getLogger(__name__)

DB_NAME = "siliwangi_bot.db"


class SiliwangiEngine:
    def __init__(self, telegram_id, username):
        self.telegram_id = telegram_id
        self.username = username
        self.password = None
        self.checkout_nonce = None
        self.security_nonce = None   # nonce untuk update_order_review
        self.order_id = None
        self.step_log = []           # Log tiap langkah untuk laporan dry run

        # ============================================================
        # User-Agent disesuaikan dengan record tracking (Chromium 145)
        # ============================================================
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9,id;q=0.8",
            "Referer": "https://siliwangibolukukus.com/"
        }

        self.client = httpx.AsyncClient(
            headers=self.headers,
            follow_redirects=True,
            timeout=20.0
        )

    # ------------------------------------------------------------------
    # STEP LOGGER (untuk laporan dry run)
    # ------------------------------------------------------------------

    def _step(self, icon: str, nama: str, detail: str = ""):
        """Catat satu langkah ke step_log dan ke file log."""
        baris = f"{icon} {nama}"
        if detail:
            baris += f" → {detail}"
        self.step_log.append(baris)
        logger.info(f"[STEP] [{self.username}] {baris}")

    # ------------------------------------------------------------------
    # UTILITAS
    # ------------------------------------------------------------------

    def _simpan_snapshot_html(self, html_text, nama_kejadian):
        try:
            waktu_sekarang = datetime.now().strftime("%H%M%S")
            username_pendek = self.username.split('@')[0] if self.username else "unknown"
            nama_file = f"snapshot_{nama_kejadian}_{username_pendek}_{waktu_sekarang}.html"
            with open(nama_file, 'w', encoding='utf-8') as file:
                file.write(html_text)
            logger.info(f"📸 SNAPSHOT TERSIMPAN: '{nama_file}'")
        except Exception as e:
            logger.error(f"Gagal menyimpan snapshot HTML: {e}")

    async def _safe_request(self, method, url, max_retries=4, **kwargs):
        for attempt in range(1, max_retries + 1):
            try:
                if method.upper() == 'GET':
                    res = await self.client.get(url, **kwargs)
                else:
                    res = await self.client.post(url, **kwargs)

                if res.status_code in [500, 502, 503, 504]:
                    logger.warning(f"Server {res.status_code}. Percobaan {attempt}/{max_retries}...")
                    if attempt < max_retries:
                        await asyncio.sleep(1.5)
                        continue
                return res
            except httpx.RequestError as e:
                logger.error(f"Koneksi terputus ({e}). Percobaan {attempt}/{max_retries}...")
                if attempt < max_retries:
                    await asyncio.sleep(1.5)
                    continue
                return None
        return None

    def _get_credentials(self):
        conn = sqlite3.connect(DB_NAME, timeout=10)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT password FROM users WHERE telegram_id = ? AND username = ?",
            (self.telegram_id, self.username)
        )
        row = cursor.fetchone()
        conn.close()
        if row:
            # Dekripsi password (dengan fallback plaintext untuk migrasi)
            self.password = decrypt_password(row[0])
            return True
        return False

    # ------------------------------------------------------------------
    # LOGIN
    # ------------------------------------------------------------------

    async def login(self):
        if not self._get_credentials():
            logger.error(f"Kredensial tidak ditemukan untuk: {self.username}")
            return False

        url_account = "https://siliwangibolukukus.com/my-account/"
        try:
            response = await self._safe_request('GET', url_account)
            if not response:
                return False

            # Cek apakah sesi masih aktif
            if "Keluar" in response.text or "Logout" in response.text or "Pesanan" in response.text:
                logger.info(f"✅ [{self.username}] Sesi masih aktif, lanjut eksekusi.")
                return True

            soup = BeautifulSoup(response.text, 'html.parser')
            nonce_field = soup.find('input', {'name': 'woocommerce-login-nonce'})
            if not nonce_field:
                logger.error(f"Gagal mendapatkan Login Nonce untuk: {self.username}")
                return False

            payload = {
                "username": self.username,
                "password": self.password,
                "woocommerce-login-nonce": nonce_field.get('value'),
                "_wp_http_referer": "/my-account/",
                "login": "Masuk"
            }
            login_res = await self._safe_request('POST', url_account, data=payload)
            if login_res and ("Keluar" in login_res.text or "Logout" in login_res.text):
                logger.info(f"✅ Login sukses: {self.username}")
                return True
            else:
                logger.warning(f"❌ Login gagal: {self.username}")
                if login_res:
                    self._simpan_snapshot_html(login_res.text, "Login_Gagal")
                return False

        except Exception as e:
            logger.error(f"Error saat login {self.username}: {e}", exc_info=True)
            return False

    # ------------------------------------------------------------------
    # VALIDASI KELIPATAN
    # ------------------------------------------------------------------

    async def _validate_kelipatan(self, keranjang):
        total_maxi = sum(item['qty'] for item in keranjang if item['kategori'] == 'MAXI')
        total_dc   = sum(item['qty'] for item in keranjang if item['kategori'] == 'DC')

        logger.info(f"🧮 [{self.username}] Cek Draf: MAXI={total_maxi}, DC={total_dc}")

        aman = True
        if total_maxi > 0 and total_maxi % 12 != 0:
            logger.error(f"❌ [{self.username}] MAXI DITOLAK! {total_maxi} bukan kelipatan 12.")
            aman = False
        if total_dc > 0 and total_dc % 4 != 0:
            logger.error(f"❌ [{self.username}] DC DITOLAK! {total_dc} bukan kelipatan 4.")
            aman = False
        if aman:
            logger.info(f"✅ [{self.username}] Aturan kelipatan OK.")
        return aman

    # ------------------------------------------------------------------
    # CLEAR CART
    # ------------------------------------------------------------------

    async def clear_cart(self):
        logger.info(f"🧹 [{self.username}] Membersihkan keranjang...")
        try:
            res = await self._safe_request('GET', "https://siliwangibolukukus.com/cart/")
            if not res:
                return
            soup = BeautifulSoup(res.text, 'html.parser')
            remove_links = soup.find_all('a', class_='remove')
            if not remove_links:
                logger.info(f"✨ [{self.username}] Keranjang sudah bersih.")
                return
            for link in remove_links:
                href = link.get('href')
                if href:
                    await self._safe_request('GET', href)
            logger.info(f"🗑️ [{self.username}] Dihapus {len(remove_links)} item sisa.")
        except Exception as e:
            logger.error(f"Gagal bersihkan keranjang {self.username}: {e}")

    # ------------------------------------------------------------------
    # ADD TO CART
    # ✅ Menggunakan ?wc-ajax=add_to_cart (standard WooCommerce AJAX)
    #    sesuai dengan mekanisme Flatsome quickview + WooCommerce handler
    # ------------------------------------------------------------------

    async def _add_to_cart(self, prod_id, qty):
        """
        Menambahkan produk ke keranjang via WooCommerce AJAX endpoint.
        Sesuai dengan record: tombol single_add_to_cart_button di quickview
        memanggil ?wc-ajax=add_to_cart dengan product_id dan quantity.
        """
        url = "https://siliwangibolukukus.com/?wc-ajax=add_to_cart"
        payload = {
            "product_id": str(prod_id),
            "quantity":   str(qty)
        }

        try:
            res = await self._safe_request('POST', url, data=payload)
            if not res:
                return False

            # Coba parse JSON response WooCommerce
            try:
                data = res.json()
                if data.get('error'):
                    logger.warning(f"🕵️ [STOK] Server menolak ID {prod_id}: {data.get('error')}")
                    return False
                if 'fragments' in data or 'cart_hash' in data:
                    return True
            except Exception:
                pass

            # Fallback: GET dengan parameter add-to-cart
            url_get = f"https://siliwangibolukukus.com/?add-to-cart={prod_id}&quantity={qty}"
            res_get = await self._safe_request('GET', url_get)
            if not res_get:
                return False

            res_text_lower = res_get.text.lower()
            if any(kw in res_text_lower for kw in ["tidak dapat menambahkan", "out of stock", "habis"]):
                logger.warning(f"🕵️ [STOK] Gagal via GET. ID: {prod_id}")
                return False

            return True

        except Exception as e:
            logger.error(f"Error _add_to_cart [{self.username}]: {e}")
            return False

    async def add_to_cart_with_fallback(self, item):
        target_id  = item['id']
        qty        = item['qty']
        nama       = item['nama']
        target_tier = item.get('tier', 0)
        kategori   = item.get('kategori', '')

        if await self._add_to_cart(target_id, qty):
            logger.info(f"✅ [{self.username}] Masuk: {qty}x {nama}")
            return True

        if target_tier == 0:
            logger.error(f"❌ [{self.username}] {nama} HABIS (Tier 0, no fallback).")
            return False

        logger.warning(f"⚠️ [{self.username}] {nama} HABIS! Mencari pengganti...")
        conn = sqlite3.connect(DB_NAME, timeout=10)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT id, nama, tier
            FROM products
            WHERE kategori=? AND tier>0 AND id!=?
            ORDER BY ABS(tier - ?) ASC
        ''', (kategori, target_id, target_tier))
        alternatives = cursor.fetchall()
        conn.close()

        for alt_id, alt_nama, alt_tier in alternatives:
            logger.info(f"   🔄 [{self.username}] Mencoba: {alt_nama} (Tier {alt_tier})...")
            if await self._add_to_cart(alt_id, qty):
                logger.info(f"   🎯 [{self.username}] Disubstitusi dengan: {alt_nama}!")
                return True

        logger.error(f"💀 [{self.username}] GAGAL TOTAL! Semua varian {kategori} habis.")
        return False

    # ------------------------------------------------------------------
    # AMBIL NONCE CHECKOUT & SECURITY
    # ------------------------------------------------------------------

    async def get_checkout_nonce(self):
        """
        Mengambil dua nonce dari halaman checkout:
        1. woocommerce-process-checkout-nonce  → untuk POST ?wc-ajax=checkout
        2. security nonce                       → untuk POST ?wc-ajax=update_order_review
        """
        try:
            res = await self._safe_request('GET', "https://siliwangibolukukus.com/checkout/")
            if not res:
                return False

            soup = BeautifulSoup(res.text, 'html.parser')

            # Nonce 1: checkout nonce
            nonce_field = soup.find('input', {'name': 'woocommerce-process-checkout-nonce'})
            if nonce_field:
                self.checkout_nonce = nonce_field.get('value')
            else:
                logger.error(f"Gagal mendapatkan checkout nonce [{self.username}]")
                return False

            # Nonce 2: security nonce untuk update_order_review
            # WooCommerce menyimpannya di inline JS: "update_order_review_nonce":"xxxxx"
            self.security_nonce = self._extract_security_nonce(res.text)
            if self.security_nonce:
                logger.info(f"🔐 [{self.username}] Security nonce OK: {self.security_nonce[:8]}...")
            else:
                logger.warning(f"⚠️ [{self.username}] Security nonce tidak ditemukan, update_order_review mungkin gagal.")

            return True

        except Exception as e:
            logger.error(f"Gagal ambil checkout nonce [{self.username}]: {e}")
            return False

    def _extract_security_nonce(self, html_text):
        """
        Mengekstrak security nonce untuk update_order_review dari halaman checkout.

        WordPress nonce: 10 karakter alphanumeric (a-z, A-Z, 0-9).
        WooCommerce menyimpannya di inline JS sebagai wc_checkout_params.update_order_review_nonce.

        Strategi (berurutan dari paling spesifik ke paling umum):
        1. Cari di blok var wc_checkout_params = {...} secara langsung
        2. Cari pola key 'update_order_review_nonce' di seluruh HTML
        3. Fallback: hidden input[name=security]
        TIDAK menggunakan pattern "nonce" generik — terlalu berisiko ambil nonce yang salah.
        """
        # ── Strategi 1: Isolasi blok wc_checkout_params ──────────────────────
        # Cari: var wc_checkout_params = { ... }
        block_match = re.search(
            r'var\s+wc_checkout_params\s*=\s*(\{.*?\})\s*;',
            html_text,
            re.DOTALL
        )
        if block_match:
            block = block_match.group(1)
            nonce_match = re.search(
                r'["\']update_order_review_nonce["\']\s*:\s*["\']([a-zA-Z0-9]{6,20})["\']',
                block
            )
            if nonce_match:
                logger.info(f"[NONCE] Ditemukan di blok wc_checkout_params.")
                return nonce_match.group(1)

        # ── Strategi 2: Cari di seluruh HTML (lebih luas) ────────────────────
        # WordPress nonces: 10 char alphanumeric
        full_match = re.search(
            r'["\']update_order_review_nonce["\']\s*:\s*["\']([a-zA-Z0-9]{6,20})["\']',
            html_text
        )
        if full_match:
            logger.info(f"[NONCE] Ditemukan via full-HTML search.")
            return full_match.group(1)

        # ── Strategi 3: Hidden input[name=security] ───────────────────────────
        soup = BeautifulSoup(html_text, 'html.parser')
        field = soup.find('input', {'name': 'security'})
        if field and field.get('value'):
            logger.info(f"[NONCE] Ditemukan via hidden input[security].")
            return field.get('value')

        logger.warning("[NONCE] Tidak ditemukan di halaman checkout. update_order_review akan dilewati.")
        return None

    # ------------------------------------------------------------------
    # UPDATE ORDER REVIEW (sebelum checkout)
    # ✅ Sesuai record: POST ke ?wc-ajax=update_order_review
    # ------------------------------------------------------------------

    async def _call_update_order_review(self, base_payload):
        """
        Memanggil update_order_review sebelum POST checkout final.
        Sesuai record tracking: user selalu memanggil endpoint ini
        setelah memilih metode pembayaran dan sebelum menekan PESAN SEKARANG.
        """
        if not self.security_nonce:
            logger.warning(f"⚠️ [{self.username}] Melewati update_order_review (nonce tidak ada).")
            return False

        url = "https://siliwangibolukukus.com/?wc-ajax=update_order_review"

        # Bangun post_data (URL-encoded dari form fields checkout)
        post_data_str = urllib.parse.urlencode(base_payload)

        review_payload = {
            "security":          self.security_nonce,
            "payment_method":    "cheque",
            "shipping_method[0]": "flat_rate:67",
            "has_full_address":  "true",
            "post_data":         post_data_str,
        }

        # Tambahkan field alamat billing/shipping jika ada di form
        field_map = {
            "state":     "billing_state",
            "postcode":  "billing_postcode",
            "city":      "billing_city",
            "address":   "billing_address_1",
            "address_2": "billing_address_2",
            "s_state":   "shipping_state",
            "s_postcode": "shipping_postcode",
            "s_city":    "shipping_city",
            "s_address": "shipping_address_1",
            "s_address_2": "shipping_address_2",
        }
        for review_key, form_key in field_map.items():
            if form_key in base_payload:
                review_payload[review_key] = base_payload[form_key]

        try:
            res = await self._safe_request('POST', url, data=review_payload)
            if res and res.status_code == 200:
                logger.info(f"✅ [{self.username}] update_order_review berhasil (200 OK).")
                return True
            else:
                status = res.status_code if res else "No Response"
                logger.warning(f"⚠️ [{self.username}] update_order_review status: {status}")
                return False
        except Exception as e:
            logger.error(f"Error update_order_review [{self.username}]: {e}")
            return False

    # ------------------------------------------------------------------
    # EKSEKUSI ORDER (main flow)
    # ------------------------------------------------------------------

    async def execute_order(self, dry_run: bool = False):
        """Eksekusi order lengkap. Jika dry_run=True, berhenti sebelum POST checkout."""
        self.step_log = []  # Reset log setiap eksekusi
        self._step("🔑", "Memulai", "dry run" if dry_run else "WAR")

        logger.info(f"🔑 [{self.username}] Mengamankan sesi login...")
        if not await self.login():
            self._step("❌", "Login", "GAGAL")
            logger.error(f"🛑 [{self.username}] Gagal login!")
            return False
        self._step("✅", "Login", "Sesi aktif")

        conn = sqlite3.connect(DB_NAME, timeout=10)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, payload_json FROM draft_orders "
            "WHERE telegram_id=? AND username=? AND status='PENDING' "
            "ORDER BY id DESC LIMIT 1",
            (self.telegram_id, self.username)
        )
        row = cursor.fetchone()
        conn.close()

        if not row:
            logger.error(f"🛑 [{self.username}] Draf KOSONG dari database saat eksekusi!")
            return False

        self.order_id, payload_json = row
        keranjang = json.loads(payload_json)

        if not await self._validate_kelipatan(keranjang):
            logger.error(f"🛑 [{self.username}] Draf ditolak sebelum masuk keranjang.")
            return False

        await self.clear_cart()
        self._step("🧹", "Clear Cart", "Selesai")


        for item in keranjang:
            ok = await self.add_to_cart_with_fallback(item)
            nama_item = item['nama']
            qty_item  = item['qty']
            if ok:
                self._step("🛒", f"{qty_item}x {nama_item}", "Masuk")
            else:
                self._step("⚠️", f"{qty_item}x {nama_item}", "HABIS/GAGAL")

        if not await self.get_checkout_nonce():
            self._step("❌", "Checkout Nonce", "Tidak ditemukan")
            return False
        self._step("🔐", "Checkout Nonce", f"{self.checkout_nonce[:8]}..." if self.checkout_nonce else "N/A")
        self._step(
            "🔐" if self.security_nonce else "⚠️",
            "Security Nonce",
            f"{self.security_nonce[:8]}..." if self.security_nonce else "Tidak ditemukan (update_order_review dilewati)"
        )

        return await self._process_checkout(dry_run=dry_run)

    # ------------------------------------------------------------------
    # PROSES CHECKOUT
    # ------------------------------------------------------------------

    async def _process_checkout(self, dry_run: bool = False):
        try:
            # 1) Verifikasi keranjang tidak kosong
            cart_res = await self._safe_request('GET', "https://siliwangibolukukus.com/cart/")
            if cart_res:
                soup_cart = BeautifulSoup(cart_res.text, 'html.parser')
                error_notices = soup_cart.find_all(class_=['woocommerce-error', 'error'])
                if error_notices:
                    pesan = " | ".join([e.get_text(strip=True) for e in error_notices])
                    logger.error(f"❌ [{self.username}] Error di keranjang: {pesan}")
                    self._simpan_snapshot_html(cart_res.text, "Error_Di_Keranjang")
                    return False

                cart_items = soup_cart.find_all('tr', class_='cart_item')
                if not cart_items:
                    logger.error(f"❌ [{self.username}] KERANJANG KOSONG setelah add_to_cart!")
                    return False

            # 2) Ambil halaman checkout & scrape semua field form
            res = await self._safe_request('GET', "https://siliwangibolukukus.com/checkout/")
            if not res or "checkout" not in str(res.url):
                logger.error(f"❌ [{self.username}] Terpental dari kasir: {res.url if res else 'N/A'}")
                if res:
                    self._simpan_snapshot_html(res.text, "Terpental_Kasir")
                return False

            soup = BeautifulSoup(res.text, 'html.parser')
            form = soup.find('form', {'name': 'checkout'})
            if not form:
                return False

            base_payload = {}
            for inp in form.find_all(['input', 'select', 'textarea']):
                name = inp.get('name')
                if not name:
                    continue
                val = inp.get('value', '')
                if inp.name == 'select':
                    sel = inp.find('option', selected=True)
                    val = sel['value'] if sel else ''
                base_payload[name] = val

            # 3) Isi field tanggal pengiriman (besok)
            zona_waktu = pytz.timezone('Asia/Jakarta')
            sekarang = datetime.now(zona_waktu)
            besok = sekarang + timedelta(days=1)
            bulan = ["", "Januari", "Februari", "Maret", "April", "Mei", "Juni",
                     "Juli", "Agustus", "September", "Oktober", "November", "Desember"]

            str_sekarang_h = f"{sekarang.day}-{sekarang.month}-{sekarang.year}"
            str_besok_h    = f"{besok.day}-{besok.month}-{besok.year}"
            str_besok_e    = f"{besok.day} {bulan[besok.month]}, {besok.year}"

            base_payload['woocommerce-process-checkout-nonce'] = self.checkout_nonce
            base_payload['payment_method']                     = 'cheque'
            base_payload['h_deliverydate']                     = str_besok_h
            base_payload['e_deliverydate']                     = str_besok_e
            base_payload['orddd_min_date_set']                 = str_besok_h
            base_payload['shipping_method[0]']                 = 'flat_rate:67'
            base_payload['orddd_lite_current_hour']            = sekarang.strftime("%H")
            base_payload['orddd_lite_current_minute']          = sekarang.strftime("%M")
            base_payload['orddd_lite_current_day']             = str_sekarang_h
            base_payload['orddd_lite_minimumOrderDays']        = str_besok_h
            base_payload['orddd_first_day_of_week']            = '0'
            base_payload['orddd_lite_delivery_date_format']    = 'd MM, yy'
            base_payload['orddd_lite_number_of_dates']         = '30'
            base_payload['orddd_lite_date_field_mandatory']    = 'checked'
            base_payload['orddd_lite_number_of_months']        = '1'
            base_payload['orddd_lite_lockout_days']            = ' '
            base_payload['orddd_admin_url']                    = 'https://siliwangibolukukus.com/wp-admin/'
            base_payload['orddd_lite_disable_for_holidays']    = 'no'
            base_payload['_wp_http_referer']                   = '/?wc-ajax=update_order_review'

            # 4) ✅ Panggil update_order_review sebelum checkout final (sesuai record)
            await self._call_update_order_review(base_payload)
            self._step(
                "✅" if self.security_nonce else "⚠️",
                "update_order_review",
                "Berhasil" if self.security_nonce else "Dilewati (nonce tidak ada)"
            )

            # ── DRY RUN: berhenti di sini, jangan POST ke /checkout ────────────
            if dry_run:
                self._step("🧪", "DRY RUN", "Berhenti sebelum POST checkout")
                checkout_nonce_preview = self.checkout_nonce[:8] + "..." if self.checkout_nonce else "N/A"
                security_preview = self.security_nonce[:8] + "..." if self.security_nonce else "N/A"
                self._step("📋", "Payload Siap",
                    f"payment={base_payload.get('payment_method')} | "
                    f"checkout_nonce={checkout_nonce_preview} | "
                    f"security={security_preview} | "
                    f"tanggal={base_payload.get('h_deliverydate', '?')}"
                )
                logger.info(f"🧪 [{self.username}] DRY RUN selesai — checkout TIDAK dieksekusi.")
                return True  # Sukses dry run

            # 5) POST checkout final — hanya metode 'cheque' (sesuai record)
            checkout_url = "https://siliwangibolukukus.com/?wc-ajax=checkout"

            logger.info(f"💳 [{self.username}] Mencoba checkout via CHEQUE (COD)...")
            final_res = await self._safe_request('POST', checkout_url, data=base_payload)
            if not final_res:
                logger.error(f"💀 [{self.username}] Tidak ada respons dari server checkout.")
                return False

            # Cek sukses via URL redirect
            if "order-received" in str(final_res.url):
                logger.info(f"🎉 [{self.username}] Checkout BERHASIL via redirect URL!")
                self._mark_success()
                return True

            # Cek sukses via JSON
            try:
                result = final_res.json()
                if result.get('result') == 'success':
                    logger.info(f"🎉 [{self.username}] Checkout BERHASIL via JSON response!")
                    self._mark_success()
                    return True
                elif result.get('result') == 'failure':
                    messages = result.get('messages', '')
                    logger.warning(f"⚠️ [{self.username}] Checkout DITOLAK: {messages[:200]}")
                    self._simpan_snapshot_html(final_res.text, "Checkout_Ditolak")
                    return False
            except Exception:
                pass

            # Cek sukses via teks HTML
            if "Pesanan" in final_res.text or "Order Complete" in final_res.text:
                logger.info(f"🎉 [{self.username}] Checkout BERHASIL via HTML text!")
                self._mark_success()
                return True

            logger.error(f"💀 [{self.username}] Checkout GAGAL. Response: {final_res.text[:200]}")
            self._simpan_snapshot_html(final_res.text, "Checkout_GAGAL")
            return False

        except Exception as e:
            logger.error(f"Fatal error checkout [{self.username}]: {e}", exc_info=True)
            return False

    # ------------------------------------------------------------------
    # MARK SUCCESS — hapus draft, simpan ke history
    # ------------------------------------------------------------------

    def _mark_success(self):
        """
        ✅ Perbaikan: Draft benar-benar DIHAPUS dari draft_orders setelah sukses,
        bukan hanya diubah statusnya. Data sukses disimpan ke order_history.
        """
        conn = sqlite3.connect(DB_NAME, timeout=10)
        cursor = conn.cursor()

        # Simpan ke order_history terlebih dahulu
        cursor.execute(
            "SELECT telegram_id, username, total_maxi, payload_json FROM draft_orders WHERE id=?",
            (self.order_id,)
        )
        row = cursor.fetchone()
        if row:
            cursor.execute('''
                INSERT INTO order_history (telegram_id, username, total_maxi, payload_json)
                VALUES (?, ?, ?, ?)
            ''', row)

        # Hapus draft dari draft_orders (bukan hanya update status)
        cursor.execute("DELETE FROM draft_orders WHERE id=?", (self.order_id,))

        conn.commit()
        conn.close()
        logger.info(f"✅ [{self.username}] Draft dihapus, riwayat tersimpan.")

    # ------------------------------------------------------------------
    # CLOSE HTTP CLIENT
    # ------------------------------------------------------------------

    async def close(self):
        await self.client.aclose()