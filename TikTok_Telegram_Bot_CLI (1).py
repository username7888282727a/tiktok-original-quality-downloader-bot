import os
import time
import logging
import json
import sqlite3
import threading
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from tenacity import retry, stop_after_attempt, wait_exponential

import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys
from telebot import TeleBot
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

# ============ AYARLAR ============
TELEGRAM_BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"  # BotFather'dan token al
bot = TeleBot(TELEGRAM_BOT_TOKEN)

# ============ LOGGING AYARLARI ============
class LoggerSetup:
    @staticmethod
    def setup_logger(base_path):
        log_dir = os.path.join(base_path, "logs")
        os.makedirs(log_dir, exist_ok=True)
        
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s',
            handlers=[
                logging.FileHandler(os.path.join(log_dir, f"download_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")),
                logging.StreamHandler()
            ]
        )
        return logging.getLogger(__name__)

# ============ KONFİGÜRASYON YÖNETICISI ============
class ConfigManager:
    def __init__(self, config_file="tiktok_config.json"):
        self.config_file = config_file
        self.load_config()
    
    def load_config(self):
        if os.path.exists(self.config_file):
            with open(self.config_file, 'r') as f:
                self.config = json.load(f)
        else:
            self.config = self.get_default_config()
            self.save_config()
    
    def get_default_config(self):
        return {
            "download_path": os.path.join(os.path.expanduser("~"), "Documents", "TikTok_Downloads"),
            "delay_between_downloads": 3,
            "timeout": 25,
            "max_workers": 2,
            "use_proxy": False,
            "proxy_server": "",
            "enable_logging": True,
            "scrape_scroll_count": 5,
            "headless_mode": True
        }
    
    def save_config(self):
        with open(self.config_file, 'w') as f:
            json.dump(self.config, f, indent=4)
    
    def get(self, key, default=None):
        return self.config.get(key, default)
    
    def set(self, key, value):
        self.config[key] = value
        self.save_config()

# ============ VERİTABANI YÖNETICISI ============
class DatabaseManager:
    def __init__(self, base_path):
        self.db_path = os.path.join(base_path, "downloads.db")
        self.init_database()
    
    def init_database(self):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS downloads (
                id INTEGER PRIMARY KEY,
                video_id TEXT UNIQUE,
                username TEXT,
                url TEXT,
                status TEXT,
                download_date TIMESTAMP,
                file_path TEXT
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS telegram_users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                downloads_count INTEGER DEFAULT 0,
                join_date TIMESTAMP
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS pending_downloads (
                id INTEGER PRIMARY KEY,
                user_id INTEGER,
                link TEXT,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP
            )
        ''')
        conn.commit()
        conn.close()
    
    def mark_as_downloaded(self, video_id, username, url, status, file_path=""):
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO downloads 
                (video_id, username, url, status, download_date, file_path)
                VALUES (?, ?, ?, ?, datetime('now'), ?)
            ''', (video_id, username, url, status, file_path))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Database error: {e}")
    
    def is_already_downloaded(self, video_id):
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM downloads WHERE video_id = ? AND status = "success"', (video_id,))
            result = cursor.fetchone()
            conn.close()
            return result is not None
        except:
            return False
    
    def get_download_stats(self):
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute('SELECT COUNT(*) FROM downloads WHERE status = "success"')
            success = cursor.fetchone()[0]
            cursor.execute('SELECT COUNT(*) FROM downloads WHERE status = "failed"')
            failed = cursor.fetchone()[0]
            conn.close()
            return success, failed
        except:
            return 0, 0
    
    def add_telegram_user(self, user_id, username):
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR IGNORE INTO telegram_users (user_id, username, join_date)
                VALUES (?, ?, datetime('now'))
            ''', (user_id, username))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Error adding telegram user: {e}")
    
    def add_pending_download(self, user_id, link):
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO pending_downloads (user_id, link, status, created_at)
                VALUES (?, ?, 'pending', datetime('now'))
            ''', (user_id, link))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Error adding pending download: {e}")

# ============ CHROME YÖNETICISI (HEADLESS) ============
class ChromeManager:
    @staticmethod
    def get_chrome_version():
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Google\Chrome\BLBeacon", 0, winreg.KEY_READ)
            v, _ = winreg.QueryValueEx(key, "version")
            return int(v.split(".")[0])
        except:
            return 144
    
    @staticmethod
    def create_driver(config):
        options = uc.ChromeOptions()
        options.page_load_strategy = 'eager'
        
        # HEADLESS MODE (Sunucuda çalışır)
        options.add_argument("--headless")
        options.add_argument("--start-maximized")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        
        if config.get("use_proxy") and config.get("proxy_server"):
            options.add_argument(f"--proxy-server={config.get('proxy_server')}")
        
        # Sessizlik seçenekleri
        options.add_argument("--disable-notifications")
        options.add_argument("--disable-popup-blocking")
        options.add_argument("--blink-settings=imagesEnabled=false")
        options.add_argument("--disable-extensions")
        options.add_argument("--disable-web-resources")
        options.add_argument("--disable-audio")
        
        try:
            driver = uc.Chrome(
                version_main=ChromeManager.get_chrome_version(),
                options=options,
                use_subprocess=True
            )
        except:
            # Linux sunucu için fallback
            driver = uc.Chrome(
                options=options,
                use_subprocess=True
            )
        
        driver.set_page_load_timeout(config.get("timeout", 25))
        return driver

# ============ İNDİRME MOTORU ============
class TikTokDownloader:
    def __init__(self, config_manager, db_manager):
        self.config_manager = config_manager
        self.db_manager = db_manager
        self.base_path = config_manager.get("download_path")
        os.makedirs(self.base_path, exist_ok=True)
        self.current_downloads = {}
    
    def send_telegram_message(self, chat_id, message):
        try:
            bot.send_message(chat_id, message, parse_mode='HTML')
        except Exception as e:
            logger.error(f"Telegram message error: {e}")
    
    def send_telegram_file(self, chat_id, file_path):
        try:
            with open(file_path, 'rb') as file:
                bot.send_document(chat_id, file)
        except Exception as e:
            logger.error(f"Telegram file error: {e}")
    
    @retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=1, min=2, max=5))
    def download_single_video(self, driver, link, save_dir, video_id, is_photo, username):
        try:
            before_count = len(os.listdir(save_dir))
            
            driver.execute_cdp_cmd("Page.setDownloadBehavior", {"behavior": "allow", "downloadPath": save_dir})

            if is_photo:
                driver.get("https://imaiger.com/tool/tiktok-slideshow-downloader")
                time.sleep(6)
                wait = WebDriverWait(driver, self.config_manager.get("timeout", 25))
                p_in = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "input")))
                driver.execute_script("arguments[0].value = ''; arguments[0].focus();", p_in)
                for char in link:
                    p_in.send_keys(char)
                    time.sleep(0.01)
                try:
                    driver.execute_script("arguments[0].click();", driver.find_element(By.XPATH, "//button[contains(., 'Load')]"))
                except:
                    p_in.send_keys(Keys.ENTER)
                time.sleep(5)
                driver.execute_script("arguments[0].click();", wait.until(EC.presence_of_element_located((By.XPATH, "//button[contains(text(), 'Download All')]"))))
                time.sleep(5)
            else:
                driver.get("https://www.tikwm.com/originalDownloader.html")
                wait = WebDriverWait(driver, self.config_manager.get("timeout", 25))
                input_f = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "input#url, .form-control")))
                js_script = "arguments[0].value = arguments[1]; arguments[0].dispatchEvent(new Event('input', { bubbles: true })); arguments[0].dispatchEvent(new Event('change', { bubbles: true }));"
                driver.execute_script(js_script, input_f, link)
                time.sleep(2)
                driver.execute_script("arguments[0].click();", wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "button#search_btn"))))
                dl_btn = wait.until(EC.presence_of_element_located((By.XPATH, "//a[contains(@class, 'download') and contains(@href, 'tikwm.com')]")))
                driver.execute_script("arguments[0].click();", dl_btn)
                time.sleep(6)

            # Dosya kontrolü
            if len(os.listdir(save_dir)) > before_count:
                self.db_manager.mark_as_downloaded(video_id, username, link, "success")
                logger.info(f"İndirildi: {link}")
                return True
            else:
                raise Exception("Dosya indirilmedi")
        except Exception as e:
            logger.error(f"Download error: {e}")
            raise
    
    def scrape_user(self, username):
        """Kullanıcıdan video linklerini çek"""
        driver = None
        try:
            driver = ChromeManager.create_driver(self.config_manager)
            if not username.startswith("@"):
                username = "@" + username
            
            driver.get(f"https://www.tiktok.com/{username}")
            time.sleep(6)
            
            found_links = set()
            scroll_count = self.config_manager.get("scrape_scroll_count", 5)
            
            for _ in range(scroll_count):
                elements = driver.find_elements(By.XPATH, "//a[contains(@href, '/video/') or contains(@href, '/photo/')]")
                for el in elements:
                    href = el.get_attribute("href")
                    if href:
                        found_links.add(href.split("?")[0])
                driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                time.sleep(3)
            
            logger.info(f"Scrape başarılı: {len(found_links)} link bulundu")
            return list(found_links)
        except Exception as e:
            logger.error(f"Scrape hatası: {e}")
            return []
        finally:
            if driver:
                driver.quit()
    
    def download_videos(self, links, chat_id=None):
        """Videoları indir"""
        total = len(links)
        success_count = 0
        fail_count = 0
        failed_links = []
        
        if chat_id:
            self.send_telegram_message(chat_id, f"⏳ <b>{total}</b> video indirme başlatılıyor...")
        
        try:
            max_workers = self.config_manager.get("max_workers", 2)
            drivers = [ChromeManager.create_driver(self.config_manager) for _ in range(max_workers)]
            
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {}
                
                for index, link in enumerate(links):
                    driver = drivers[index % max_workers]
                    
                    video_id = link.split('/')[-1].split('?')[0]
                    is_photo = "/photo/" in link
                    username = link.split('@')[1].split('/')[0] if '@' in link else "user"
                    save_dir = os.path.join(self.base_path, username)
                    os.makedirs(save_dir, exist_ok=True)

                    # Veritabanı kontrolü
                    if self.db_manager.is_already_downloaded(video_id):
                        success_count += 1
                        continue

                    future = executor.submit(
                        self.download_single_video,
                        driver, link, save_dir, video_id, is_photo, username
                    )
                    futures[future] = (link, username, is_photo)
                    
                    time.sleep(self.config_manager.get("delay_between_downloads", 3))

                for future in as_completed(futures):
                    link, username, is_photo = futures[future]
                    try:
                        future.result()
                        success_count += 1
                        if chat_id and success_count % 5 == 0:
                            self.send_telegram_message(chat_id, f"✅ {success_count}/{total} indirildi...")
                    except Exception as e:
                        fail_count += 1
                        failed_links.append(link)
                        video_id = link.split('/')[-1].split('?')[0]
                        self.db_manager.mark_as_downloaded(video_id, username, link, "failed")
                        logger.error(f"Failed: {link}")
        finally:
            for driver in drivers:
                try:
                    driver.quit()
                except:
                    pass
            
            logger.info(f"İndirme tamamlandı: {success_count} başarılı, {fail_count} hatalı")
            
            if chat_id:
                telegram_msg = f"""
✅ <b>İndirme Tamamlandı!</b>

📊 <b>Sonuçlar:</b>
✅ Başarılı: <b>{success_count}</b>
❌ Hatalı: <b>{fail_count}</b>
📁 Klasör: <code>{self.base_path}</code>
                """
                self.send_telegram_message(chat_id, telegram_msg)
            
            return success_count, fail_count, failed_links

# ============ TELEGRAM BOT KOMANDLARı ============
def init_telegram_bot():
    """Telegram Bot komutlarını başlat"""
    
    @bot.message_handler(commands=['start'])
    def handle_start(message):
        chat_id = message.chat.id
        username = message.from_user.username or message.from_user.first_name or "User"
        db_manager.add_telegram_user(chat_id, username)
        
        response = """
🎬 <b>TikTok Pro Downloader Bot</b>

Hoşgeldin! 👋

📌 <b>Komutlar:</b>
/download - Video/Foto indirmek için
/scrape - Kullanıcıdan videoları çekmek için
/stats - İstatistikleri görmek için
/help - Yardım almak için

💡 <b>Kullanım:</b>
1. /download yazıp TikTok linkini gönder
2. Bot otomatik olarak indir
3. İndirilen dosyalar sunucuda kaydedilir
        """
        bot.send_message(chat_id, response, parse_mode='HTML')
        logger.info(f"New telegram user: {username} ({chat_id})")
    
    @bot.message_handler(commands=['download'])
    def handle_download(message):
        chat_id = message.chat.id
        msg = bot.send_message(chat_id, "🔗 <b>Lütfen TikTok linkini gönder:</b>\n\n<i>Örnek: https://www.tiktok.com/@username/video/123456789</i>", parse_mode='HTML')
        bot.register_next_step_handler(msg, process_download_link, chat_id)
    
    @bot.message_handler(commands=['scrape'])
    def handle_scrape(message):
        chat_id = message.chat.id
        msg = bot.send_message(chat_id, "👤 <b>Lütfen TikTok kullanıcı adını gönder:</b>\n\n<i>Örnek: @username (@ işareti olmadan da olur)</i>", parse_mode='HTML')
        bot.register_next_step_handler(msg, process_scrape_user, chat_id)
    
    @bot.message_handler(commands=['stats'])
    def handle_stats(message):
        chat_id = message.chat.id
        success, failed = db_manager.get_download_stats()
        stats_text = f"""
📊 <b>İstatistikler:</b>

✅ Başarılı İndirmeler: <b>{success}</b>
❌ Hatalı İndirmeler: <b>{failed}</b>
📈 Toplam: <b>{success + failed}</b>
        """
        bot.send_message(chat_id, stats_text, parse_mode='HTML')
    
    @bot.message_handler(commands=['help'])
    def handle_help(message):
        chat_id = message.chat.id
        help_text = """
💡 <b>Yardım Rehberi</b>

<b>1️⃣ Tek Video İndirmek:</b>
/download yazıp linki gönder

<b>2️⃣ Kullanıcıdan Tüm Videoları Çekmek:</b>
/scrape yazıp kullanıcı adını gönder

<b>3️⃣ İstatistikleri Görmek:</b>
/stats yazarak toplam indirmeleri gör

⚠️ <b>Linkler doğru formatta olmalı:</b>
✓ https://www.tiktok.com/@username/video/123456789
✓ https://www.tiktok.com/@username/photo/123456789

🌐 <b>Desteklenen Sayfalar:</b>
• TikTok video
• TikTok foto/slideshow
        """
        bot.send_message(chat_id, help_text, parse_mode='HTML')
    
    @bot.message_handler(func=lambda message: True)
    def handle_message(message):
        chat_id = message.chat.id
        if "tiktok.com" in message.text.lower():
            msg = bot.send_message(chat_id, "⏳ <b>İndirme işlemi başlatılıyor...</b>", parse_mode='HTML')
            process_download_link(message, chat_id)
        else:
            bot.send_message(chat_id, "❌ <b>Geçersiz komut!</b>\n\n/help yazarak yardım alabilirsiniz.", parse_mode='HTML')

def process_download_link(message, chat_id):
    """Telegram'dan gelen linki işle"""
    link = message.text.strip()
    
    if "tiktok.com" not in link:
        bot.send_message(chat_id, "❌ <b>Geçerli bir TikTok linki gönder!</b>", parse_mode='HTML')
        return
    
    try:
        threading.Thread(
            target=downloader.download_videos,
            args=([link], chat_id),
            daemon=True
        ).start()
    except Exception as e:
        bot.send_message(chat_id, f"❌ <b>Hata:</b> {str(e)}", parse_mode='HTML')
        logger.error(f"Download error: {e}")

def process_scrape_user(message, chat_id):
    """Kullanıcıdan videoları çek"""
    username = message.text.strip()
    
    if not username:
        bot.send_message(chat_id, "❌ <b>Kullanıcı adı girin!</b>", parse_mode='HTML')
        return
    
    try:
        bot.send_message(chat_id, f"⏳ <b>{username} adlı kullanıcıdan videolar çekiliyor...</b>", parse_mode='HTML')
        
        links = downloader.scrape_user(username)
        
        if not links:
            bot.send_message(chat_id, f"❌ <b>{username} kullanıcısından video bulunamadı!</b>", parse_mode='HTML')
            return
        
        bot.send_message(chat_id, f"✅ <b>{len(links)} video bulundu!</b>\n\n⏳ <b>İndirme başlatılıyor...</b>", parse_mode='HTML')
        
        threading.Thread(
            target=downloader.download_videos,
            args=(links, chat_id),
            daemon=True
        ).start()
    except Exception as e:
        bot.send_message(chat_id, f"❌ <b>Hata:</b> {str(e)}", parse_mode='HTML')
        logger.error(f"Scrape error: {e}")

# ============ ANA PROGRAM ============
if __name__ == "__main__":
    print("""
    ╔════════════════════════════════════════════════════════════╗
    ║    TikTok Pro Downloader - Telegram Bot Edition v11.0     ║
    ║          🚀 Headless Mode (Linux Uyumlu) 🚀              ║
    ╚════════════════════════════════════════════════════════════╝
    """)
    
    # Sistem başlatma
    config_manager = ConfigManager()
    base_path = config_manager.get("download_path")
    os.makedirs(base_path, exist_ok=True)
    
    logger = LoggerSetup.setup_logger(base_path)
    db_manager = DatabaseManager(base_path)
    downloader = TikTokDownloader(config_manager, db_manager)
    
    logger.info("=" * 60)
    logger.info("TikTok Pro Downloader Bot başlatılıyor...")
    logger.info(f"Download Klasörü: {base_path}")
    logger.info(f"Headless Mode: {config_manager.get('headless_mode')}")
    logger.info("=" * 60)
    
    print(f"\n✅ Bot başarıyla başlatıldı!\n")
    print(f"📁 İndirme Klasörü: {base_path}")
    print(f"🔇 Headless Mode: {'ON' if config_manager.get('headless_mode') else 'OFF'}")
    print(f"⏳ Timeout: {config_manager.get('timeout')} sn")
    print(f"🔄 Thread Sayısı: {config_manager.get('max_workers')}")
    print(f"\n🤖 Telegram Bot dinleniyor...\n")
    
    try:
        init_telegram_bot()
        bot.infinity_polling()
    except Exception as e:
        logger.error(f"Bot hatası: {e}")
        print(f"\n❌ Bot hatası: {e}")