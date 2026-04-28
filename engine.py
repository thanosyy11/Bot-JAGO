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
    def __init__(self, telegram_id):
        self.telegram_id = telegram_id
        self.username = None
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

    # ==========================================
    # MEKANISME RETRY (ANTI SERVER DOWN)
    # ==========================================
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
        cursor.execute("SELECT username, password FROM users WHERE telegram_id = ?", (self.telegram_id,))
        row = cursor.fetchone()
        conn.close()
        if row:
            self.username, self.password = row
            return True
        return False

    async def login(self):
        if not self._get_credentials():
            logger.error("Kredensial login tidak ditemukan.")
            return False

        url_account = "https://siliwangibolukukus.com/my-account/"
        try:
            response = await self._safe_request('GET', url_account)
            if not response: return False
            
            soup = BeautifulSoup(response.text, 'html.parser')
            nonce_field = soup.find('input', {'name': 'woocommerce-login-nonce'})
            
            if not nonce_field:
                logger.error("Gagal mendapatkan Login Nonce.")
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
                logger.info(f"Login sukses untuk user: {self.username}")
                return True
            else:
                logger.warning(f"Login gagal untuk user: {self.username}")
                return False
        except Exception as e:
            logger.error(f"Error saat login: {str(e)}", exc_info=True)
            return False

    # ==========================================
    # FITUR BARU: AUTO-CLEAR CART (TUKANG SAPU)
    # ==========================================
    async def clear_cart(self):
        """Memastikan keranjang kosong 100% sebelum perang dimulai."""
        logger.info("🧹 Mengecek dan membersihkan keranjang hantu...")
        try:
            res = await self._safe_request('GET', "https://siliwangibolukukus.com/cart/")
            if not res: return
            
            soup = BeautifulSoup(res.text, 'html.parser')
            remove_links = soup.find_all('a', class_='remove')
            
            if not remove_links:
                logger.info("✨ Keranjang sudah bersih. Aman untuk mulai.")
                return

            for link in remove_links:
                href = link.get('href')
                if href:
                    # Mengirim request ke link "x" (remove)
                    await self._safe_request('GET', href)
            
            logger.info(f"🗑️ Berhasil menghapus {len(remove_links)} item sisa dari keranjang!")
        except Exception as e:
            logger.error(f"Gagal membersihkan keranjang: {str(e)}")

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
            logger.error(f"Gagal mengambil Checkout Nonce: {str(e)}")
            return False

    async def _add_to_cart(self, prod_id, qty):
        payload = {"add-to-cart": prod_id, "quantity": qty}
        try:
            res = await self._safe_request('POST', "https://siliwangibolukukus.com/cart/", data=payload)
            if not res: return False
            if "tidak dapat menambahkan" in res.text.lower() or "out of stock" in res.text.lower():
                return False
            return True
        except Exception:
            return False

    # ==========================================
    # FITUR BARU: SUBSTITUSI LINTAS TIER
    # ==========================================
    async def add_to_cart_with_fallback(self, item):
        target_id = item['id']
        qty = item['qty']
        nama = item['nama']
        target_tier = item.get('tier', 0)
        kategori = item.get('kategori', '')
        
        # 1. Tembakan Pertama (Sesuai Draf)
        if await self._add_to_cart(target_id, qty): 
            logger.info(f"✅ Masuk: {qty}x {nama}")
            return True
            
        if target_tier == 0:
            logger.error(f"❌ {nama} HABIS (Tier 0). Dilewati.")
            return False
            
        logger.warning(f"⚠️ {nama} HABIS! Berburu varian pengganti...")
        
        # 2. Ambil semua alternatif di kategori yang sama.
        # Trik SQL: ORDER BY ABS(tier - target_tier)
        # Ini akan mencari Tier 1 dulu, kalau habis lompat ke Tier 2, lalu Tier 3!
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
        
        # 3. Eksekusi tembakan pengganti lintas Tier
        for alt_id, alt_nama, alt_tier in alternatives:
            logger.info(f"   🔄 Mencoba: {alt_nama} (Tier {alt_tier})...")
            if await self._add_to_cart(alt_id, qty):
                logger.info(f"   🎯 BERHASIL disubstitusi dengan: {alt_nama}!")
                return True
                
        logger.error(f"💀 GAGAL TOTAL! Seluruh varian {kategori} LUDES.")
        return False

    async def execute_order(self):
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("SELECT id, payload_json FROM draft_orders WHERE telegram_id=? AND status='PENDING' ORDER BY id DESC LIMIT 1", (self.telegram_id,))
        row = cursor.fetchone()
        conn.close()
        
        if not row: return False
        
        self.order_id, payload_json = row
        keranjang = json.loads(payload_json)
        
        # SAPU BERSIH KERANJANG LAMA DULU
        await self.clear_cart()
        
        for item in keranjang:
            await self.add_to_cart_with_fallback(item)
            
        if not await self.get_checkout_nonce(): return False
        return await self._process_checkout()

    async def _process_checkout(self):
        try:
            res = await self._safe_request('GET', "https://siliwangibolukukus.com/checkout/")
            if not res: return False
            
            soup = BeautifulSoup(res.text, 'html.parser')
            form = soup.find('form', {'name': 'checkout'})
            if not form: return False

            payload = {}
            for inp in form.find_all(['input', 'select', 'textarea']):
                name = inp.get('name')
                if not name: continue
                val = inp.get('value', '')
                if inp.name == 'select':
                    sel = inp.find('option', selected=True)
                    val = sel['value'] if sel else ''
                payload[name] = val

            besok = datetime.now() + timedelta(days=1)
            bulan = ["", "Januari", "Februari", "Maret", "April", "Mei", "Juni", "Juli", "Agustus", "September", "Oktober", "November", "Desember"]
            
            payload['payment_method'] = 'cheque'
            payload['woocommerce-process-checkout-nonce'] = self.checkout_nonce
            payload['h_deliverydate'] = besok.strftime("%d-%m-%Y")
            payload['e_deliverydate'] = f"{besok.day} {bulan[besok.month]}, {besok.year}"
            payload['orddd_min_date_set'] = payload['h_deliverydate']
            
            checkout_url = "https://siliwangibolukukus.com/?wc-ajax=checkout"
            final_res = await self._safe_request('POST', checkout_url, data=payload)
            if not final_res: return False
            
            # --- PERBAIKAN LOGIKA DETEKSI SUKSES ---
            # Cek 1: Apakah kita di-redirect ke halaman "Order Received/Order Complete"?
            if "order-received" in str(final_res.url) or "Pesanan" in final_res.text or "Order Complete" in final_res.text:
                logger.info(f"Checkout BERHASIL (Terdeteksi via Redirect HTML). Order ID DB: {self.order_id}")
                conn = sqlite3.connect(DB_NAME)
                cursor = conn.cursor()
                cursor.execute("UPDATE draft_orders SET status='SUCCESS' WHERE id=?", (self.order_id,))
                conn.commit()
                conn.close()
                return True

            # Cek 2: Jika server membalas dengan format JSON standar
            try:
                result = final_res.json()
                if result.get('result') == 'success':
                    logger.info(f"Checkout BERHASIL (Terdeteksi via JSON). Order ID DB: {self.order_id}")
                    conn = sqlite3.connect(DB_NAME)
                    cursor = conn.cursor()
                    cursor.execute("UPDATE draft_orders SET status='SUCCESS' WHERE id=?", (self.order_id,))
                    conn.commit()
                    conn.close()
                    return True
                else:
                    logger.error(f"Checkout DITOLAK: {final_res.text}")
                    return False
            except Exception as e:
                # Jika bukan JSON dan bukan halaman sukses, baru kita anggap gagal beneran
                logger.error(f"Checkout Gagal/Server Error. Respons Web: {final_res.text[:150]}...", exc_info=True)
                return False

        except Exception as e:
            logger.error(f"Gagal checkout: {str(e)}", exc_info=True)
            return False

    async def close(self):
        await self.client.aclose()