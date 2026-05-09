import asyncio
import httpx
from bs4 import BeautifulSoup
import sqlite3
import json
from datetime import datetime, timedelta
import pytz
import logging
import os

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
        self.order_id = None
        
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9,id;q=0.8",
            "Referer": "https://siliwangibolukukus.com/"
        }
        
        self.client = httpx.AsyncClient(headers=self.headers, follow_redirects=True, timeout=15.0)

    def _simpan_snapshot_html(self, html_text, nama_kejadian):
        """Menyimpan kode HTML ke file agar bisa dibuka di browser saat terjadi error."""
        try:
            waktu_sekarang = datetime.now().strftime("%H%M%S")
            username_pendek = self.username.split('@')[0] if self.username else "unknown"
            nama_file = f"snapshot_{nama_kejadian}_{username_pendek}_{waktu_sekarang}.html"
            with open(nama_file, 'w', encoding='utf-8') as file:
                file.write(html_text)
            logger.info(f"📸 SNAPSHOT TERSIMPAN: Buka file '{nama_file}' untuk melihat error!")
        except Exception as e:
            logger.error(f"Gagal menyimpan snapshot HTML: {str(e)}")

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
                        await asyncio.sleep(1)
                        continue
                return res
            except httpx.RequestError as e:
                logger.error(f"Koneksi terputus ({str(e)}). Percobaan {attempt}/{max_retries}...")
                if attempt < max_retries:
                    await asyncio.sleep(1)
                    continue
                return None
        return None

    def _get_credentials(self):
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("SELECT password FROM users WHERE telegram_id = ? AND username = ?", (self.telegram_id, self.username))
        row = cursor.fetchone()
        conn.close()
        if row:
            self.password = row[0]
            return True
        return False

    async def login(self):
        if not self._get_credentials():
            logger.error(f"Kredensial login tidak ditemukan untuk: {self.username}")
            return False

        url_account = "https://siliwangibolukukus.com/my-account/"
        try:
            response = await self._safe_request('GET', url_account)
            if not response: return False
            
            # [PERBAIKAN KRUSIAL] Cek apakah bot sebenarnya SUDAH login dari sesi pemanasan
            if "Keluar" in response.text or "Logout" in response.text or "Pesanan" in response.text:
                logger.info(f"✅ [{self.username}] Sesi login masih aktif! Lanjut ke eksekusi...")
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
                logger.info(f"✅ Login sukses untuk user: {self.username}")
                return True
            else:
                logger.warning(f"❌ Login gagal untuk user: {self.username}")
                return False
        except Exception as e:
            logger.error(f"Error saat login {self.username}: {str(e)}", exc_info=True)
            return False
        
    async def _validate_kelipatan_12(self, keranjang):
        """Mengecek apakah total pesanan memenuhi syarat kelipatan 12."""
        total_kue = sum(item['qty'] for item in keranjang if 'plastik' not in item['nama'].lower())
        sisa = total_kue % 12
        logger.info(f"🧮 [{self.username}] Mengecek draf: Total item = {total_kue} pcs")
        if sisa == 0:
            logger.info(f"✅ [{self.username}] Total {total_kue} pcs memenuhi kelipatan 12. Aman!")
            return True
        else:
            kekurangan = 12 - sisa
            logger.error(f"❌ [{self.username}] ATURAN KELIPATAN 12 DILANGGAR!")
            logger.error(f"📝 [SOLUSI]: Total {total_kue} pcs. Kamu harus menambah {kekurangan} pcs, atau mengurangi {sisa} pcs.")
            return False

    async def clear_cart(self):
        logger.info(f"🧹 [{self.username}] Mengecek dan membersihkan keranjang hantu...")
        try:
            res = await self._safe_request('GET', "https://siliwangibolukukus.com/cart/")
            if not res: return
            soup = BeautifulSoup(res.text, 'html.parser')
            remove_links = soup.find_all('a', class_='remove')
            if not remove_links:
                logger.info(f"✨ [{self.username}] Keranjang sudah bersih.")
                return
            for link in remove_links:
                href = link.get('href')
                if href:
                    await self._safe_request('GET', href)
            logger.info(f"🗑️ [{self.username}] Menghapus {len(remove_links)} item sisa.")
        except Exception as e:
            logger.error(f"Gagal membersihkan keranjang {self.username}: {str(e)}")

    async def get_checkout_nonce(self):
        try:
            res = await self._safe_request('GET', "https://siliwangibolukukus.com/checkout/")
            if not res: return False
            soup = BeautifulSoup(res.text, 'html.parser')
            nonce_field = soup.find('input', {'name': 'woocommerce-process-checkout-nonce'})
            if nonce_field:
                self.checkout_nonce = nonce_field.get('value')
                return True
            return False
        except Exception as e:
            logger.error(f"Gagal mengambil Checkout Nonce {self.username}: {str(e)}")
            return False

    async def _add_to_cart(self, prod_id, qty):
        url = "https://siliwangibolukukus.com/?wc-ajax=add_to_cart"
        payload = {"product_id": str(prod_id), "quantity": str(qty)}
        
        try:
            res = await self._safe_request('POST', url, data=payload)
            if not res: return False
            
            # 1. Coba baca respons mesin sebagai JSON (Jalur VIP WooCommerce)
            try:
                data = res.json()
                if data.get('error'):
                    logger.warning(f"🕵️ [INTEL STOK] Server menolak ID {prod_id}. Alasan: Habis / Limit.")
                    return False
                if 'fragments' in data or 'cart_hash' in data:
                    return True
            except Exception:
                pass # Jika server ngambek dan tidak membalas JSON, lanjut ke Rencana B

            # 2. RENCANA B (Fallback): Gunakan jalur GET klasik jika AJAX diblokir
            url_get = f"https://siliwangibolukukus.com/?add-to-cart={prod_id}&quantity={qty}"
            res_get = await self._safe_request('GET', url_get)
            if not res_get: return False
            
            res_text_lower = res_get.text.lower()
            if "tidak dapat menambahkan" in res_text_lower or "out of stock" in res_text_lower or "sisa" in res_text_lower:
                logger.warning(f"🕵️ [INTEL STOK] Gagal via GET. ID: {prod_id} | Pesan: Out of stock")
                return False
                
            return True

        except Exception as e:
            logger.error(f"Error pada _add_to_cart [{self.username}]: {str(e)}")
            return False

    async def add_to_cart_with_fallback(self, item):
        target_id = item['id']
        qty = item['qty']
        nama = item['nama']
        target_tier = item.get('tier', 0)
        kategori = item.get('kategori', '')
        
        if await self._add_to_cart(target_id, qty): 
            logger.info(f"✅ [{self.username}] Masuk: {qty}x {nama}")
            return True
            
        if target_tier == 0:
            logger.error(f"❌ [{self.username}] {nama} HABIS (Tier 0). Dilewati.")
            return False
            
        logger.warning(f"⚠️ [{self.username}] {nama} HABIS! Berburu varian pengganti...")
        conn = sqlite3.connect(DB_NAME)
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
                logger.info(f"   🎯 [{self.username}] BERHASIL disubstitusi dengan: {alt_nama}!")
                return True
                
        logger.error(f"💀 [{self.username}] GAGAL TOTAL! Seluruh varian {kategori} LUDES.")
        return False

    async def execute_order(self):
        logger.info(f"🔑 [{self.username}] Mengamankan sesi login...")
        if not await self.login():
            logger.error(f"🛑 [{self.username}] Gagal login!")
            return False

        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("SELECT id, payload_json FROM draft_orders WHERE telegram_id=? AND username=? AND status='PENDING' ORDER BY id DESC LIMIT 1", (self.telegram_id, self.username))
        row = cursor.fetchone()
        conn.close()
        
        if not row: 
            logger.error(f"🛑 [{self.username}] Draf KOSONG/HILANG dari database saat eksekusi!")
            return False
        
        self.order_id, payload_json = row
        keranjang = json.loads(payload_json)
        
        # Validasi Kelipatan 12 diaktifkan sesuai aturan
        if not await self._validate_kelipatan_12(keranjang):
            logger.error(f"🛑 [{self.username}] Draf ditolak sebelum masuk keranjang.")
            return False
        
        await self.clear_cart()
        
        for item in keranjang:
            await self.add_to_cart_with_fallback(item)
            
        if not await self.get_checkout_nonce(): return False
        return await self._process_checkout()

    async def _process_checkout(self):
        try:
            cart_res = await self._safe_request('GET', "https://siliwangibolukukus.com/cart/")
            if cart_res:
                soup_cart = BeautifulSoup(cart_res.text, 'html.parser')
                error_notices = soup_cart.find_all(class_=['woocommerce-error', 'error'])
                if error_notices:
                    pesan_error = " | ".join([e.get_text(strip=True) for e in error_notices])
                    logger.error(f"❌ [{self.username}] RADAR: Terdapat error di keranjang! Alasan: {pesan_error}")
                    self._simpan_snapshot_html(cart_res.text, "Error_Di_Keranjang")
                    return False
                
                cart_items = soup_cart.find_all('tr', class_='cart_item')
                if not cart_items:
                    logger.error(f"❌ [{self.username}] RADAR: KERANJANG KOSONG SECARA GAIB! Server gagal mengikat sesi.")
                    return False

            res = await self._safe_request('GET', "https://siliwangibolukukus.com/checkout/")
            if not res or "checkout" not in str(res.url): 
                logger.error(f"❌ [{self.username}] Terpental dari kasir (302). Dialihkan ke: {res.url if res else 'Unknown'}")
                if res: self._simpan_snapshot_html(res.text, "Terpental_Kasir_302")
                return False
            
            soup = BeautifulSoup(res.text, 'html.parser')
            form = soup.find('form', {'name': 'checkout'})
            if not form: return False

            base_payload = {}
            for inp in form.find_all(['input', 'select', 'textarea']):
                name = inp.get('name')
                if not name: continue
                val = inp.get('value', '')
                if inp.name == 'select':
                    sel = inp.find('option', selected=True)
                    val = sel['value'] if sel else ''
                base_payload[name] = val

            zona_waktu = pytz.timezone('Asia/Jakarta')
            sekarang = datetime.now(zona_waktu)
            besok = sekarang + timedelta(days=1)
            bulan = ["", "Januari", "Februari", "Maret", "April", "Mei", "Juni", "Juli", "Agustus", "September", "Oktober", "November", "Desember"]
            
            # Formating string identik dengan record-all.txt browser
            str_sekarang_h = f"{sekarang.day}-{sekarang.month}-{sekarang.year}"
            str_besok_h = f"{besok.day}-{besok.month}-{besok.year}"
            str_besok_e = f"{besok.day} {bulan[besok.month]}, {besok.year}"
            
            base_payload['woocommerce-process-checkout-nonce'] = self.checkout_nonce
            base_payload['h_deliverydate'] = str_besok_h
            base_payload['e_deliverydate'] = str_besok_e
            base_payload['orddd_min_date_set'] = str_besok_h
            
            # --- PENGUATAN PAYLOAD HASIL ANALISA record-all.txt ---
            base_payload['shipping_method[0]'] = 'flat_rate:67'
            base_payload['orddd_lite_current_hour'] = sekarang.strftime("%H")
            base_payload['orddd_lite_current_minute'] = sekarang.strftime("%M")
            base_payload['orddd_lite_current_day'] = str_sekarang_h
            base_payload['orddd_lite_minimumOrderDays'] = str_besok_h
            base_payload['orddd_first_day_of_week'] = '0'
            base_payload['orddd_lite_delivery_date_format'] = 'd MM, yy'
            base_payload['orddd_lite_number_of_dates'] = '30'
            base_payload['orddd_lite_date_field_mandatory'] = 'checked'
            base_payload['orddd_lite_number_of_months'] = '1'
            base_payload['orddd_lite_lockout_days'] = ' '
            base_payload['orddd_admin_url'] = 'https://siliwangibolukukus.com/wp-admin/'
            base_payload['orddd_lite_disable_for_holidays'] = 'no'
            base_payload['_wp_http_referer'] = '/?wc-ajax=update_order_review'
            # ------------------------------------------------------
            
            checkout_url = "https://siliwangibolukukus.com/?wc-ajax=checkout"
            metode_pembayaran = ['cheque', 'cod']
            
            for metode in metode_pembayaran:
                logger.info(f"🔄 [{self.username}] Mencoba checkout dengan metode: {metode.upper()}")
                base_payload['payment_method'] = metode
                
                final_res = await self._safe_request('POST', checkout_url, data=base_payload)
                if not final_res: continue
                
                if "order-received" in str(final_res.url) or "Pesanan" in final_res.text or "Order Complete" in final_res.text:
                    logger.info(f"🎉 Checkout BERHASIL [{self.username}] via {metode.upper()}. Order ID DB: {self.order_id}")
                    self._mark_success()
                    return True

                try:
                    result = final_res.json()
                    if result.get('result') == 'success':
                        logger.info(f"🎉 Checkout BERHASIL [{self.username}] via {metode.upper()}. Order ID DB: {self.order_id}")
                        self._mark_success()
                        return True
                    else:
                        logger.warning(f"⚠️ DITOLAK via {metode.upper()} [{self.username}]: {final_res.text}")
                        self._simpan_snapshot_html(final_res.text, f"Ditolak_JSON_{metode}")
                except Exception:
                    logger.warning(f"⚠️ Gagal membaca JSON via {metode.upper()} [{self.username}]. Respons: {final_res.text[:100]}...")

            logger.error(f"💀 [{self.username}] SEMUA METODE PEMBAYARAN GAGAL TOTAL.")
            return False

        except Exception as e:
            logger.error(f"Gagal checkout [{self.username}]: {str(e)}", exc_info=True)
            return False

    def _mark_success(self):
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("SELECT telegram_id, username, total_maxi, payload_json FROM draft_orders WHERE id=?", (self.order_id,))
        row = cursor.fetchone()
        
        if row:
            cursor.execute('''
                INSERT INTO order_history (telegram_id, username, total_maxi, payload_json)
                VALUES (?, ?, ?, ?)
            ''', row)
            
        cursor.execute("UPDATE draft_orders SET status='SUCCESS' WHERE id=?", (self.order_id,))
        conn.commit()
        conn.close()

    async def close(self):
        await self.client.aclose()