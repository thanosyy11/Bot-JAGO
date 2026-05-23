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

from database import (
    decrypt_password,
    load_session_cookies,
    save_session_cookies,
    clear_session_cookies,
    get_setting
)

logging.basicConfig(
    filename='siliwangi_error.log',
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - [ENGINE] %(message)s'
)
logger = logging.getLogger(__name__)

DB_NAME = "siliwangi_bot.db"


class CloudflareBlockException(Exception):
    pass

class SiliwangiEngine:
    def __init__(self, telegram_id, username):
        self.telegram_id = telegram_id
        self.username = username
        self.password = None
        self.checkout_nonce = None
        self.security_nonce = None
        self.order_id = None
        self.order_id_woo = "UNKNOWN"
        self.step_log = []
        self.substitusi_log = []  # Varian habis/disubstitusi saat war

        # Session recovery tracking
        self.last_session_check_at = None
        self.reconnect_attempt = 0
        self.max_reconnect_attempts = 2

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

        self._load_saved_cookies()

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
            logger.info(f"📸 Screenshot Tersimpan: '{nama_file}'")
        except Exception as e:
            logger.error(f"Gagal menyimpan snapshot HTML: {e}")

    async def _safe_request(self, method, url, max_retries=4, **kwargs):
        for attempt in range(1, max_retries + 1):
            try:
                follow_redir = kwargs.pop('follow_redirects', True)
                if method.upper() == 'GET':
                    res = await self.client.get(url, follow_redirects=follow_redir, **kwargs)
                else:
                    res = await self.client.post(url, follow_redirects=follow_redir, **kwargs)

                # Cek Cloudflare
                if res.headers.get('cf-mitigated') or "Just a moment" in res.text[:300] or "Checking your browser" in res.text[:300]:
                    logger.error(f"🚨 [{self.username}] Terblokir Cloudflare di {url}!")
                    raise CloudflareBlockException("Terblokir oleh Cloudflare.")

                if res.status_code == 403:
                    logger.error(f"🚨 [{self.username}] Bot diblokir server (403 Forbidden).")
                    raise CloudflareBlockException("Diblokir Server (403).")

                if res.status_code == 429:
                    retry_after = int(res.headers.get("Retry-After", 5))
                    logger.warning(f"⚠️ [{self.username}] Rate limited (429). Tunggu {retry_after} detik.")
                    await asyncio.sleep(retry_after)
                    continue

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

    async def _safe_request_with_recovery(self, method, url, max_retries=4, **kwargs):
        """
        Wrapper untuk _safe_request() dengan auto-reconnect logic.
        Jika error 302/401/timeout, coba re-login & retry sekali.
        """
        try:
            res = await self._safe_request(method, url, max_retries=max_retries, **kwargs)

            # Cek jika error 301/302/401 (session invalid) - 403 sudah ditangani
            if res and res.status_code in [301, 302, 401]:
                logger.warning(f"🔄 [{self.username}] Terdeteksi error {res.status_code}, coba reconnect...")

                if self.reconnect_attempt < self.max_reconnect_attempts:
                    self.reconnect_attempt += 1

                    # Clear cookies & re-login
                    clear_session_cookies(self.telegram_id, self.username)
                    logger.info(f"🔄 [{self.username}] Reconnect attempt {self.reconnect_attempt}/{self.max_reconnect_attempts}...")

                    if await self.login():
                        logger.info(f"✅ [{self.username}] Reconnected successfully, retrying request...")
                        # Retry sekali saja
                        res = await self._safe_request(method, url, max_retries=2, **kwargs)
                        return res
                    else:
                        logger.error(f"❌ [{self.username}] Re-login failed")
                        return res

            return res

        except CloudflareBlockException:
            raise
        except Exception as e:
            logger.error(f"Error di _safe_request_with_recovery [{self.username}]: {e}")
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
            self.password = decrypt_password(row[0])
            return True
        return False

    # ------------------------------------------------------------------
    # PASSWORD PROTECTED BYPASS
    # ------------------------------------------------------------------

    async def bypass_site_password(self):
        """
        Mengecek apakah situs diproteksi oleh plugin 'Password Protected'.
        Jika ya, otomatis menembus dengan menggunakan password global dari DB.
        """
        global_pwd = get_setting("kode_akses", "")
        if not global_pwd:
            return  # Jika belum diset, abaikan saja

        try:
            res = await self._safe_request('GET', "https://siliwangibolukukus.com/")
            if not res:
                return

            # Cek apakah ada redirect ke password-protected atau form password
            if "password-protected=login" in str(res.url) or "password-protected-login" in res.text:
                logger.info(f"🛡️ [{self.username}] Terdeteksi Halaman Password! Mencoba bypass...")
                
                # Plugin biasanya menggunakan POST ke URL yang sama atau login action
                payload = {
                    "password_protected_pwd": global_pwd,
                    "wp_submit": "Log In"
                }
                
                # Kirim request POST form password
                bypass_url = "https://siliwangibolukukus.com/wp-login.php?action=password-protected-login"
                res_bypass = await self._safe_request('POST', bypass_url, data=payload)
                
                if res_bypass and "password-protected=login" not in str(res_bypass.url):
                    logger.info(f"✅ [{self.username}] Bypass Password BERHASIL!")
                else:
                    logger.warning(f"⚠️ [{self.username}] Bypass Password GAGAL! Kode mungkin salah.")
        except Exception as e:
            logger.error(f"Error saat bypass password: {e}")

    # ------------------------------------------------------------------
    # SESSION MANAGEMENT
    # ------------------------------------------------------------------

    def _load_saved_cookies(self):
        """Muat cookies tersimpan dari DB ke httpx client."""
        saved = load_session_cookies(self.telegram_id, self.username)
        if saved:
            if isinstance(saved, list):
                for c in saved:
                    self.client.cookies.set(
                        c['name'], c['value'],
                        domain=c.get('domain', ''),
                        path=c.get('path', '/')
                    )
            else:
                for name, value in saved.items():
                    self.client.cookies.set(name, value)
            logger.info(f"🔑 [{self.username}] Cookies dimuat dari DB.")
            self._session_is_valid()

    def _session_is_valid(self) -> bool:
        """
        Cek apakah session masih valid.
        Dulu: hanya check cookies key
        Baru: check cookies + timestamp (re-validate setiap 5 min)
        """
        # Check basic cookies
        has_wp_cookie = any(
            'wordpress_logged_in' in cookie.name or 'wordpress_sec' in cookie.name or 'wp_woocommerce_session_' in cookie.name
            for cookie in self.client.cookies.jar
        )
        if not has_wp_cookie:
            return False

        # Check timestamp: re-validate setiap 5 menit
        now = datetime.now()
        if self.last_session_check_at:
            age = (now - self.last_session_check_at).total_seconds()
            if age < 300:  # 5 menit
                return True  # Masih fresh

        # Need to refresh: do actual test GET /my-account/
        logger.debug(f"🔍 [{self.username}] Validating session freshness...")
        return False  # Will trigger re-validation in caller

    def _save_cookies(self):
        """Simpan cookies httpx saat ini ke DB dan update timestamp."""
        try:
            cookies_list = []
            for cookie in self.client.cookies.jar:
                cookies_list.append({
                    'name': cookie.name,
                    'value': cookie.value,
                    'domain': cookie.domain,
                    'path': cookie.path,
                    'secure': cookie.secure
                })
            if cookies_list:
                save_session_cookies(self.telegram_id, self.username, cookies_list)
                self.last_session_check_at = datetime.now()  # Update timestamp
                logger.info(f"💾 [{self.username}] {len(cookies_list)} cookies disimpan ke DB (session fresh).")
        except Exception as e:
            logger.warning(f"⚠️ Gagal simpan cookies [{self.username}]: {e}")

    # ------------------------------------------------------------------
    # LOGIN
    # ------------------------------------------------------------------

    async def login(self):
        # Panggil bypass password sebelum login
        await self.bypass_site_password()

        if not self._get_credentials():
            logger.error(f"Kredensial tidak ditemukan untuk: {self.username}")
            return False

        url_account = "https://siliwangibolukukus.com/my-account/"
        try:
            response = await self._safe_request('GET', url_account)
            if not response:
                return False

            # Cek apakah sesi masih aktif (via cookies tersimpan)
            if "Keluar" in response.text or "Logout" in response.text or "Pesanan" in response.text:
                logger.info(f"✅ [{self.username}] Sesi masih aktif (cookies).")
                self._save_cookies()
                return True

            # Session expired → clear lama, fresh login
            logger.info(f"🔐 [{self.username}] Session expired, login ulang...")
            clear_session_cookies(self.telegram_id, self.username)

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
                logger.info(f"✅ Login sukses (fresh): {self.username}")
                self._save_cookies()
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
    # ------------------------------------------------------------------

    def _parse_available_stock(self, error_msg: str) -> int:
        """
        Parse pesan error WooCommerce untuk mengetahui stok yang tersedia.
        Return 0 jika tidak bisa diparsing.
        Contoh error: '...hanya tersedia 13...' atau '...13 available...'
        """
        patterns = [
            r'(\d+)\s+(?:tersedia|available)',
            r'(?:tersedia|available)\s*[:\-]?\s*(\d+)',
            r'(?:stok|stock)\s*(?:tersisa|tersedia)?\s*[:\-]?\s*(\d+)',
            r'maximum\s+(?:is\s+)?(\d+)',
        ]
        for p in patterns:
            m = re.search(p, str(error_msg), re.IGNORECASE)
            if m:
                return int(m.group(1))
        return 0

    async def _add_to_cart(self, prod_id, qty) -> tuple:
        """
        Menambahkan produk ke keranjang via WooCommerce AJAX.
        Return: (success: bool, qty_added: int)

        Jika server menolak qty penuh tapi masih ada stok parsial,
        bot akan mengamankan stok yang tersedia.
        """
        url = "https://siliwangibolukukus.com/?wc-ajax=add_to_cart"

        try:
            ajax_headers = {"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"}
            res = await self._safe_request('POST', url, headers=ajax_headers, data={
                "product_id": str(prod_id),
                "quantity":   str(qty)
            })
            if not res:
                return False, 0

            try:
                data = self._parse_json_response(res.text)
            except ValueError:
                logger.error(f"❌ [{self.username}] Respons add_to_cart bukan JSON. Kemungkinan redirect.")
                return False, 0
                
            try:
                # Sukses penuh
                if 'fragments' in data or 'cart_hash' in data:
                    return True, qty

                # Server menolak — cek apakah ada stok parsial
                if data.get('error'):
                    err_txt = str(data.get('error', ''))
                    logger.warning(f"🕵️ [STOK] ID {prod_id} ditolak: {err_txt[:120]}")
                    available = self._parse_available_stock(err_txt)
                    if available and 0 < available < qty:
                        logger.info(f"📦 Stok parsial terdeteksi: {available}x tersedia untuk ID {prod_id}")
                        res2 = await self._safe_request('POST', url, headers=ajax_headers, data={
                            "product_id": str(prod_id),
                            "quantity":   str(available)
                        })
                        if res2:
                            try:
                                d2 = self._parse_json_response(res2.text)
                                if 'fragments' in d2 or 'cart_hash' in d2:
                                    return True, available  # Sukses parsial
                            except Exception:
                                pass
                    return False, 0
            except Exception:
                pass

            # Fallback: GET dengan parameter add-to-cart
            url_get = f"https://siliwangibolukukus.com/?add-to-cart={prod_id}&quantity={qty}"
            res_get = await self._safe_request('GET', url_get)
            if not res_get:
                return False, 0

            if any(kw in res_get.text.lower() for kw in ["tidak dapat menambahkan", "out of stock", "habis"]):
                return False, 0

            return True, qty

        except Exception as e:
            logger.error(f"Error _add_to_cart [{self.username}]: {e}")
            return False, 0

    async def add_to_cart_with_fallback(self, item):
        """Digunakan untuk DC dan PLASTIK. MAXI menggunakan _smart_maxi_fill."""
        target_id   = item['id']
        qty         = item['qty']
        nama        = item['nama']
        target_tier = item.get('tier', 0)
        kategori    = item.get('kategori', '')

        success, added = await self._add_to_cart(target_id, qty)
        if success:
            logger.info(f"✅ [{self.username}] Masuk: {added}x {nama}")
            return True

        if target_tier == 0:
            logger.error(f"❌ [{self.username}] {nama} HABIS (Tier 0, no fallback).")
            return False

        logger.warning(f"⚠️ [{self.username}] {nama} HABIS! Mencari pengganti...")
        conn = sqlite3.connect(DB_NAME, timeout=10)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT id, nama, tier FROM products
            WHERE kategori=? AND tier>0 AND id!=?
            ORDER BY ABS(tier - ?) ASC
        ''', (kategori, target_id, target_tier))
        alternatives = cursor.fetchall()
        conn.close()

        for alt_id, alt_nama, alt_tier in alternatives:
            logger.info(f"   🔄 [{self.username}] Mencoba: {alt_nama} (Tier {alt_tier})...")
            ok, added = await self._add_to_cart(alt_id, qty)
            if ok:
                logger.info(f"   🎯 [{self.username}] Disubstitusi dengan: {alt_nama}!")
                return True

        logger.error(f"💀 [{self.username}] GAGAL TOTAL! Semua varian {kategori} habis.")
        return False

    # ------------------------------------------------------------------
    # SMART MAXI FILL (v2.0)
    # Partial fill + tier chain + Tier 3 cut 40%
    # ------------------------------------------------------------------

    async def _smart_maxi_fill(self, maxi_items: list) -> int:
        """
        Mengisi keranjang MAXI dengan cerdas (Stable Version + Logging):
        - Priority queue: User item diutamakan, lalu sisa DB sebagai fallback.
        - Partial fill: amankan stok sisa, lanjut ke produk berikutnya
        - Tier chain: Tier 1 -> Tier 2 -> Tier 3
        - Tier 3 only cut: jika hanya Tier 3 tersedia, potong 40%

        Return: total qty MAXI yang berhasil masuk keranjang
        """
        total_needed = sum(item['qty'] for item in maxi_items)
        remaining    = total_needed
        total_added  = 0
        tiers_contributed = set()   # Tier mana saja yang berhasil masuk
        tier3_cut_applied = False

        # ── Bangun priority queue ───────────────────────────────────
        # 1. Item yang diminta user (urutan asli input)
        # 2. Produk MAXI lain dari DB (belum di-request, sebagai fallback)
        user_ids = {item['id'] for item in maxi_items}

        conn = sqlite3.connect(DB_NAME, timeout=10)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT id, nama, tier FROM products
            WHERE kategori='MAXI' AND tier > 0
            ORDER BY tier ASC, id ASC
        ''')
        all_maxi_db = {row[0]: row for row in cursor.fetchall()}
        conn.close()

        # Priority queue: user's items first, then DB fallbacks
        priority_queue = list(maxi_items)  # dicts dengan 'id','nama','qty','tier'
        for prod_id, (pid, pnama, ptier) in all_maxi_db.items():
            if prod_id not in user_ids:
                priority_queue.append({'id': pid, 'nama': pnama,
                                       'qty': 0, 'tier': ptier,
                                       'kategori': 'MAXI', '_fallback': True})

        # ── Proses setiap produk dalam antrian ───────────────────────
        for product in priority_queue:
            if remaining <= 0:
                break

            tier = product.get('tier', 1)

            # Tier 3 cut — hanya 1x, hanya jika Tier 1 & 2 tidak berkontribusi
            if (tier == 3
                    and not tier3_cut_applied
                    and tiers_contributed.isdisjoint({1, 2})):
                cut = max(12, (int(remaining * 0.60) // 12) * 12)
                logger.info(f"✂️ [{self.username}] Tier 3 only — potong 40%: {remaining}x → {cut}x")
                self._step("✂️", "Tier 3 Only",
                           f"Semua T1+T2 habis. Target dipotong 40%: {remaining}→{cut}x")
                remaining = cut
                tier3_cut_applied = True

            # Tentukan qty yang akan dicoba
            is_fallback = product.get('_fallback', False)
            qty_to_try  = remaining if is_fallback else min(product['qty'], remaining)
            if qty_to_try <= 0:
                continue

            ok, added = await self._add_to_cart(product['id'], qty_to_try)

            if added > 0:
                tiers_contributed.add(tier)
                total_added += added
                remaining   -= added
                if added < qty_to_try:
                    self._step("⚡", f"{product['nama']}",
                               f"Parsial {added}/{qty_to_try}x (sisa {remaining}x)")
                    if not product.get('_fallback'):  # Stok parsial dari item user request
                        self.substitusi_log.append(f"⚡ {product['nama']}: stok parsial {added}/{qty_to_try}x")
                else:
                    self._step("🛒", f"{added}x {product['nama']}", "Masuk")
                    if product.get('_fallback') and tier <= 2:
                        self.substitusi_log.append(f"🔄 Substitusi: +{added}x {product['nama']} (Tier {tier})")
            else:
                self._step("⚠️", f"{product['nama']}", "Habis / Gagal")
                if not product.get('_fallback'):
                    self.substitusi_log.append(f"❌ {product['nama']} (Tier {tier}): HABIS")

        if remaining > 0:
            logger.warning(
                f"⚠️ [{self.username}] MAXI kurang {remaining}x dari target {total_needed}x"
            )
            self._step("⚠️", "MAXI", f"Kurang {remaining}x dari {total_needed}x")

        logger.info(f"✅ [{self.username}] Total MAXI masuk keranjang: {total_added}x")
        return total_added

    # ------------------------------------------------------------------
    # AMBIL NONCE CHECKOUT & SECURITY
    # ------------------------------------------------------------------

    async def get_checkout_nonce(self):
        """
        Mengambil dua nonce dari halaman checkout.
        Dicoba 2x jika redirect terjadi (session mungkin baru saja di-refresh).
        """
        try:
            # Coba pertama: tanpa follow redirect agar bisa deteksi redirect
            res = await self._safe_request('GET', "https://siliwangibolukukus.com/checkout/",
                                           follow_redirects=False)
            if not res:
                return False

            # Jika redirect (cart kosong / session masalah), coba re-login + retry 1x
            if res.status_code in [301, 302]:
                loc = res.headers.get("Location", "")
                logger.warning(f"⚠️ [{self.username}] Checkout redirect ke {loc} — coba re-login...")
                # Re-login sekali
                clear_session_cookies(self.telegram_id, self.username)
                if not await self.login():
                    logger.error(f"❌ [{self.username}] Re-login sebelum nonce GAGAL")
                    return False
                # Retry ambil checkout page setelah re-login
                res = await self._safe_request('GET', "https://siliwangibolukukus.com/checkout/",
                                               follow_redirects=True)
                if not res:
                    return False
                if "checkout" not in str(res.url):
                    logger.error(f"❌ [{self.username}] Setelah re-login masih terpental: {res.url}")
                    return False

            soup = BeautifulSoup(res.text, 'html.parser')

            # Nonce 1: checkout nonce
            nonce_field = soup.find('input', {'name': 'woocommerce-process-checkout-nonce'})
            if nonce_field:
                self.checkout_nonce = nonce_field.get('value')
            else:
                logger.error(f"Gagal mendapatkan checkout nonce [{self.username}]")
                self._simpan_snapshot_html(res.text, "NoNonce_Checkout")
                return False

            # Nonce 2: security nonce untuk update_order_review
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
            ajax_headers = {"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"}
            res = await self._safe_request_with_recovery('POST', url, headers=ajax_headers, data=review_payload)
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

    async def execute_order(self):
        """Eksekusi order produksi penuh."""
        self.step_log = []
        self.substitusi_log = []
        self.reconnect_attempt = 0
        self.order_id_woo = "UNKNOWN"
        self._draft_payload_json = None
        self._draft_total_maxi   = 0
        self._step("🔑", "Memulai", "WAR")

        logger.info(f"🔑 [{self.username}] Mengamankan sesi login...")
        if not await self.login():
            self._step("❌", "Login", "GAGAL")
            logger.error(f"🛑 [{self.username}] Gagal login!")
            return False
        self._step("✅", "Login", "Sesi aktif")

        conn = sqlite3.connect(DB_NAME, timeout=10)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, payload_json, total_maxi FROM draft_orders "
            "WHERE telegram_id=? AND username=? AND status='PENDING' "
            "ORDER BY id DESC LIMIT 1",
            (self.telegram_id, self.username)
        )
        row = cursor.fetchone()
        conn.close()

        if not row:
            logger.error(f"🛑 [{self.username}] Draf KOSONG dari database saat eksekusi!")
            return False

        self.order_id, payload_json, total_maxi_draft = row
        # Simpan untuk _mark_failed
        self._draft_payload_json = payload_json
        self._draft_total_maxi   = total_maxi_draft or 0
        keranjang = json.loads(payload_json)

        if not await self._validate_kelipatan(keranjang):
            logger.error(f"🛑 [{self.username}] Draf ditolak sebelum masuk keranjang.")
            return False

        await self.clear_cart()
        self._step("🧹", "Clear Cart", "Selesai")

        # ── Pisahkan item berdasarkan kategori ───────────────────────────
        maxi_items    = [i for i in keranjang if i['kategori'] == 'MAXI']
        dc_items      = [i for i in keranjang if i['kategori'] == 'DC']
        plastik_items = [i for i in keranjang if i['kategori'] == 'PLASTIK']

        # MAXI: smart tier fill (v3.0 — tier-sorted priority queue)
        if maxi_items:
            await self._smart_maxi_fill(maxi_items)

        # ── DC & PLASTIK: Tambah secara PARALEL ────────────────────────
        # DC: jika habis → catat di log, checkout tetap lanjut
        # PLASTIK: selalu skip jika gagal, tidak batalkan order
        async def _add_item_task(item, fallback_msg):
            ok, added = await self._add_to_cart(item['id'], item['qty'])
            if ok:
                self._step("🛒", f"{added}x {item['nama']}", "Masuk")
            else:
                self._step("⚠️", f"{item['qty']}x {item['nama']}", fallback_msg)

        tasks = []
        for item in dc_items:
            tasks.append(_add_item_task(item, "DC HABIS — dilewati"))
        for item in plastik_items:
            tasks.append(_add_item_task(item, "Plastik HABIS — dilewati"))

        if tasks:
            await asyncio.gather(*tasks)

        if not await self.get_checkout_nonce():
            self._step("❌", "Checkout Nonce", "Tidak ditemukan")
            self._mark_failed("Gagal ambil checkout nonce")
            return False
        self._step("🔐", "Checkout Nonce",
                   f"{self.checkout_nonce[:8]}..." if self.checkout_nonce else "N/A")
        self._step(
            "🔐" if self.security_nonce else "⚠️",
            "Security Nonce",
            f"{self.security_nonce[:8]}..." if self.security_nonce
            else "Tidak ditemukan (update_order_review dilewati)"
        )

        result = await self._process_checkout()
        if not result:
            # Ambil alasan dari step_log terakhir yang error
            err_lines = [l for l in self.step_log if any(c in l for c in ["❌", "⚠️"])]
            reason = err_lines[-1][:120] if err_lines else "Checkout gagal"
            self._mark_failed(reason)
        return result

    # ------------------------------------------------------------------
    # PROSES CHECKOUT
    # ------------------------------------------------------------------

    async def _process_checkout(self):
        try:
            # 1) Verifikasi keranjang tidak kosong
            cart_res = await self._safe_request_with_recovery('GET', "https://siliwangibolukukus.com/cart/")
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
            res = await self._safe_request_with_recovery('GET', "https://siliwangibolukukus.com/checkout/")
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

            str_sekarang_h = sekarang.strftime("%d-%m-%Y")
            str_besok_h    = besok.strftime("%d-%m-%Y")
            str_besok_e    = f"{besok.strftime('%d')} {bulan[besok.month]}, {besok.year}"

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

            # 5) POST checkout final — hanya metode 'cheque' (sesuai record)
            checkout_url = "https://siliwangibolukukus.com/?wc-ajax=checkout"

            logger.info(f"💳 [{self.username}] Mencoba checkout via CHEQUE...")
            ajax_headers = {"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"}
            # Timeout lebih panjang untuk POST checkout (server bisa lambat saat war)
            try:
                final_res = await asyncio.wait_for(
                    self._safe_request_with_recovery('POST', checkout_url, headers=ajax_headers, data=base_payload),
                    timeout=30.0
                )
            except asyncio.TimeoutError:
                logger.error(f"⏰ [{self.username}] Checkout POST timeout (30s)! Server tidak merespons.")
                return False
            if not final_res:
                logger.error(f"💀 [{self.username}] Tidak ada respons dari server checkout.")
                return False

            # Cek sukses via JSON terlebih dahulu
            try:
                result = self._parse_json_response(final_res.text)
                if result.get('result') == 'success':
                    logger.info(f"🎉 [{self.username}] Checkout BERHASIL via JSON response!")
                    redirect_url = result.get('redirect', '')
                    m = re.search(r'/order-received/(\d+)/', redirect_url)
                    self.order_id_woo = m.group(1) if m else "UNKNOWN"
                    logger.info(f"🔖 [{self.username}] Order ID diekstrak: {self.order_id_woo}")
                    try:
                        nominal = await self._scrape_order_nominal(self.order_id_woo)
                        self._mark_success(nominal)
                    except Exception as e:
                        logger.warning(f"⚠️ Gagal membersihkan draf setelah sukses: {e}")
                    return True
                elif result.get('result') == 'failure':
                    messages = result.get('messages', '')
                    logger.warning(f"⚠️ [{self.username}] Checkout DITOLAK: {messages[:200]}")
                    self._simpan_snapshot_html(final_res.text, "Checkout_Ditolak")
                    return False
            except Exception:
                pass

            # Cek sukses via URL redirect (fallback)
            if "order-received" in str(final_res.url):
                logger.info(f"🎉 [{self.username}] Checkout BERHASIL via redirect URL!")
                m = re.search(r'/order-received/(\d+)/', str(final_res.url))
                self.order_id_woo = m.group(1) if m else "UNKNOWN"
                logger.warning(f"⚠️ Redirect history: {len(final_res.history)} redirects. Order ID: {self.order_id_woo}")
                nominal = await self._scrape_order_nominal(self.order_id_woo)
                self._mark_success(nominal)
                return True

            # Cek sukses via teks HTML
            if "Pesanan" in final_res.text or "Order Complete" in final_res.text:
                logger.info(f"🎉 [{self.username}] Checkout BERHASIL via HTML text!")
                nominal = await self._scrape_order_nominal(
                    getattr(self, 'order_id_woo', 'UNKNOWN')
                )
                self._mark_success(nominal)
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

    def _mark_success(self, total_nominal: str = ''):
        """
        Atomik: INSERT ke order_history (status=SUKSES) + DELETE dari draft_orders.
        Jika salah satu gagal, keduanya di-rollback agar data konsisten.
        """
        conn = sqlite3.connect(DB_NAME, timeout=10)
        cursor = conn.cursor()
        try:
            cursor.execute(
                "SELECT telegram_id, username, total_maxi, payload_json FROM draft_orders WHERE id=?",
                (self.order_id,)
            )
            row = cursor.fetchone()
            if row:
                cursor.execute('''
                    INSERT INTO order_history
                        (telegram_id, username, total_maxi, payload_json,
                         order_id, status, total_nominal)
                    VALUES (?, ?, ?, ?, ?, 'SUKSES', ?)
                ''', (row[0], row[1], row[2], row[3],
                      getattr(self, 'order_id_woo', 'UNKNOWN'), total_nominal))
                logger.info(f"📚 [{self.username}] Riwayat SUKSES tersimpan. Nominal: {total_nominal}")
            else:
                logger.warning(
                    f"⚠️ [{self.username}] Draft ID {self.order_id} tidak ditemukan saat _mark_success!"
                )

            cursor.execute("DELETE FROM draft_orders WHERE id=?", (self.order_id,))
            conn.commit()
            logger.info(f"✅ [{self.username}] Draft dihapus, riwayat tersimpan.")
        except Exception as e:
            conn.rollback()
            logger.error(f"❌ [{self.username}] _mark_success GAGAL, rollback: {e}", exc_info=True)
            raise
        finally:
            conn.close()

    def _mark_failed(self, reason: str = 'Checkout gagal'):
        """
        Simpan riwayat order GAGAL ke order_history.
        Dipanggil dari execute_order() saat checkout tidak berhasil.
        """
        if not self._draft_payload_json:
            return  # Belum ada draft yang diproses, jangan simpan
        conn = sqlite3.connect(DB_NAME, timeout=10)
        cursor = conn.cursor()
        try:
            cursor.execute('''
                INSERT INTO order_history
                    (telegram_id, username, total_maxi, payload_json,
                     order_id, status, total_nominal)
                VALUES (?, ?, ?, ?, 'N/A', 'GAGAL', ?)
            ''', (self.telegram_id, self.username, self._draft_total_maxi,
                  self._draft_payload_json, reason[:120]))
            conn.commit()
            logger.info(f"📚 [{self.username}] Riwayat GAGAL tersimpan: {reason[:60]}")
        except Exception as e:
            logger.error(f"❌ [{self.username}] _mark_failed error: {e}")
        finally:
            conn.close()

    async def _scrape_order_nominal(self, order_id_woo: str) -> str:
        """Scrape total nominal dari halaman order-received setelah checkout sukses."""
        try:
            url = f"https://siliwangibolukukus.com/checkout/order-received/{order_id_woo}/"
            res = await self._safe_request('GET', url)
            if not res:
                return ''
            soup = BeautifulSoup(res.text, 'html.parser')
            # Cari total di overview WooCommerce
            total_el = soup.find(class_='woocommerce-order-overview__total')
            if total_el:
                amount = total_el.find(class_='woocommerce-Price-amount')
                if amount:
                    return amount.get_text(strip=True)
            # Fallback: cari di tabel order
            total_row = soup.find('tr', class_='order-total')
            if total_row:
                amount = total_row.find(class_='woocommerce-Price-amount')
                if amount:
                    return amount.get_text(strip=True)
            return ''
        except Exception as e:
            logger.warning(f"[{self.username}] Gagal scrape nominal: {e}")
            return ''

    # ------------------------------------------------------------------
    # CLOSE HTTP CLIENT
    # ------------------------------------------------------------------

    async def close(self):
        await self.client.aclose()