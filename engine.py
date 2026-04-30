import asyncio
import httpx
from bs4 import BeautifulSoup
import sqlite3
import json
from datetime import datetime, timedelta
import logging

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

    # [PERBAIKAN V2.1]: Radar Intelijen Perekam Error
    async def _add_to_cart(self, prod_id, qty):
        payload = {"add-to-cart": prod_id, "quantity": qty}
        try:
            res = await self._safe_request('POST', "https://siliwangibolukukus.com/cart/", data=payload)
            if not res: return False
            
            res_text_lower = res.text.lower()
            
            if "tidak dapat menambahkan" in res_text_lower or "out of stock" in res_text_lower or "sisa" in res_text_lower:
                soup = BeautifulSoup(res.text, 'html.parser')
                error_notices = soup.find_all(class_=['woocommerce-error', 'woocommerce-message', 'error', 'woocommerce-info'])
                
                pesan_error = "Pesan tersembunyi (Tidak ditemukan di class standar)"
                if error_notices:
                    pesan_error = " | ".join([e.get_text(strip=True) for e in error_notices])
                
                logger.warning(f"🕵️ [INTEL STOK] Akun: {self.username} | ID Produk: {prod_id} | Qty: {qty}")
                logger.warning(f"📝 [PESAN SILIWANGI]: {pesan_error}")
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
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("SELECT id, payload_json FROM draft_orders WHERE telegram_id=? AND username=? AND status='PENDING' ORDER BY id DESC LIMIT 1", (self.telegram_id, self.username))
        row = cursor.fetchone()
        conn.close()
        
        if not row: return False
        
        self.order_id, payload_json = row
        keranjang = json.loads(payload_json)
        
        await self.clear_cart()
        
        for item in keranjang:
            await self.add_to_cart_with_fallback(item)
            
        if not await self.get_checkout_nonce(): return False
        return await self._process_checkout()

    # [PERBAIKAN V2.1]: Radar Keranjang & Fallback Metode Pembayaran Ganda
    async def _process_checkout(self):
        try:
            # 1. RADAR SEBELUM KASIR: CEK STATUS FINAL KERANJANG
            cart_res = await self._safe_request('GET', "https://siliwangibolukukus.com/cart/")
            if cart_res:
                soup_cart = BeautifulSoup(cart_res.text, 'html.parser')
                error_notices = soup_cart.find_all(class_=['woocommerce-error', 'error'])
                if error_notices:
                    pesan_error = " | ".join([e.get_text(strip=True) for e in error_notices])
                    logger.error(f"❌ [{self.username}] RADAR: Terdapat error di keranjang! Alasan Siliwangi: {pesan_error}")
                    return False
                
                # Memastikan barang benar-benar diikat oleh sesi Siliwangi
                cart_items = soup_cart.find_all('tr', class_='cart_item')
                if not cart_items:
                    logger.error(f"❌ [{self.username}] RADAR: KERANJANG KOSONG SECARA GAIB! Server gagal mengikat sesi.")
                    return False

            # 2. MASUK KASIR
            res = await self._safe_request('GET', "https://siliwangibolukukus.com/checkout/")
            if not res or "checkout" not in str(res.url): 
                logger.error(f"❌ [{self.username}] Terpental dari kasir (302). Dialihkan ke: {res.url if res else 'Unknown'}")
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

            besok = datetime.now() + timedelta(days=1)
            bulan = ["", "Januari", "Februari", "Maret", "April", "Mei", "Juni", "Juli", "Agustus", "September", "Oktober", "November", "Desember"]
            
            base_payload['woocommerce-process-checkout-nonce'] = self.checkout_nonce
            base_payload['h_deliverydate'] = besok.strftime("%d-%m-%Y")
            base_payload['e_deliverydate'] = f"{besok.day} {bulan[besok.month]}, {besok.year}"
            base_payload['orddd_min_date_set'] = base_payload['h_deliverydate']
            
            checkout_url = "https://siliwangibolukukus.com/?wc-ajax=checkout"

            # 3. STRATEGI FALLBACK GANDA (Opsi A: Cheque, Opsi B: COD)
            metode_pembayaran = ['cheque', 'cod']
            
            for metode in metode_pembayaran:
                logger.info(f"🔄 [{self.username}] Mencoba checkout dengan metode: {metode.upper()}")
                base_payload['payment_method'] = metode
                
                final_res = await self._safe_request('POST', checkout_url, data=base_payload)
                if not final_res: continue
                
                # Cek Redirect HTML
                if "order-received" in str(final_res.url) or "Pesanan" in final_res.text or "Order Complete" in final_res.text:
                    logger.info(f"🎉 Checkout BERHASIL [{self.username}] via {metode.upper()}. Order ID DB: {self.order_id}")
                    self._mark_success()
                    return True

                # Cek Balasan JSON
                try:
                    result = final_res.json()
                    if result.get('result') == 'success':
                        logger.info(f"🎉 Checkout BERHASIL [{self.username}] via {metode.upper()}. Order ID DB: {self.order_id}")
                        self._mark_success()
                        return True
                    else:
                        logger.warning(f"⚠️ DITOLAK via {metode.upper()} [{self.username}]: {final_res.text}")
                        # Jika Cheque ditolak, loop akan lanjut mengeksekusi COD
                except Exception:
                    logger.warning(f"⚠️ Gagal membaca JSON via {metode.upper()} [{self.username}]. Respons: {final_res.text[:100]}...")

            # Jika loop selesai dan tidak Return True, berarti Cheque & COD gagal semua
            logger.error(f"💀 [{self.username}] SEMUA METODE PEMBAYARAN GAGAL TOTAL.")
            return False

        except Exception as e:
            logger.error(f"Gagal checkout [{self.username}]: {str(e)}", exc_info=True)
            return False

    # [PERBAIKAN V2.1]: Sinkronisasi ke Tabel Riwayat
    def _mark_success(self):
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        
        # Ekstrak data untuk dipindahkan ke Riwayat
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