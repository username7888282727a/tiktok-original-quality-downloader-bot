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
TELEGRAM_BOT_TOKEN = "6632758014:AAFM6Xlt6sF6C3FBvy5JCsycIbB7n6vhvQ8" 
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
        # Linux uyumlu dosya yolu
        return {
            "download_path": os.path.join(os.getcwd(), "downloads"),
            "delay_between_downloads": 3,
            "timeout": 25,
            "max_workers": 1, # Choreo gibi kısıtlı sunucularda 1 idealdir
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

# ============ VERİTABANI YÖNETICISI ============
class DatabaseManager:
    def __init__(self, base_path):
        self.db_path = os.path.join(base_path, "downloads.db")
        self.init_database()
    
    def init_database(self):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''CREATE TABLE IF NOT EXISTS downloads (id INTEGER PRIMARY KEY, video_id TEXT UNIQUE, username TEXT, url TEXT, status TEXT, download_date TIMESTAMP, file_path TEXT)''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS telegram_users (user_id INTEGER PRIMARY KEY, username TEXT, downloads_count INTEGER DEFAULT 0, join_date TIMESTAMP)''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS pending_downloads (id INTEGER PRIMARY KEY, user_id INTEGER, link TEXT, status TEXT DEFAULT 'pending', created_at TIMESTAMP)''')
        conn.commit()
        conn.close()
    
    def mark_as_downloaded(self, video_id, username, url, status, file_path=""):
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute('INSERT OR REPLACE INTO downloads (video_id, username, url, status, download_date, file_path) VALUES (?, ?, ?, ?, datetime("now"), ?)', (video_id, username, url, status, file_path))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"DB Error: {e}")

    def is_already_downloaded(self, video_id):
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM downloads WHERE video_id = ? AND status = "success"', (video_id,))
            res = cursor.fetchone()
            conn.close()
            return res is not None
        except: return False

    def get_download_stats(self):
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute('SELECT COUNT(*) FROM downloads WHERE status = "success"')
            s = cursor.fetchone()[0]
            cursor.execute('SELECT COUNT(*) FROM downloads WHERE status = "failed"')
            f = cursor.fetchone()[0]
            conn.close()
            return s, f
        except: return 0, 0

    def add_telegram_user(self, user_id, username):
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute('INSERT OR IGNORE INTO telegram_users (user_id, username, join_date) VALUES (?, ?, datetime("now"))', (user_id, username))
            conn.commit()
            conn.close()
        except: pass

# ============ CHROME YÖNETICISI (LINUX) ============
class ChromeManager:
    @staticmethod
    def create_driver(config):
        options = uc.ChromeOptions()
        options.add_argument("--headless")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--start-maximized")
        options.add_argument("--disable-notifications")
        options.add_argument("--blink-settings=imagesEnabled=false")
        
        try:
            # Linux'ta versiyon belirtmeye gerek yok, uc otomatik bulur
            driver = uc.Chrome(options=options, use_subprocess=True)
            driver.set_page_load_timeout(config.get("timeout", 25))
            return driver
        except Exception as e:
            logger.error(f"Driver başlatılamadı: {e}")
            raise

# ============ İNDİRME MOTORU ============
class TikTokDownloader:
    def __init__(self, config_manager, db_manager):
        self.config_manager = config_manager
        self.db_manager = db_manager
        self.base_path = config_manager.get("download_path")
        os.makedirs(self.base_path, exist_ok=True)
    
    def send_telegram_message(self, chat_id, message):
        try: bot.send_message(chat_id, message, parse_mode='HTML')
        except: pass

    @retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=1, min=2, max=5))
    def download_single_video(self, driver, link, save_dir, video_id, is_photo, username):
        before_count = len(os.listdir(save_dir))
        driver.execute_cdp_cmd("Page.setDownloadBehavior", {"behavior": "allow", "downloadPath": save_dir})

        if is_photo:
            driver.get("https://imaiger.com/tool/tiktok-slideshow-downloader")
            wait = WebDriverWait(driver, 20)
            p_in = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "input")))
            p_in.send_keys(link)
            try: driver.execute_script("arguments[0].click();", driver.find_element(By.XPATH, "//button[contains(., 'Load')]"))
            except: p_in.send_keys(Keys.ENTER)
            time.sleep(7)
            driver.execute_script("arguments[0].click();", wait.until(EC.presence_of_element_located((By.XPATH, "//button[contains(text(), 'Download All')]"))))
        else:
            driver.get("https://www.tikwm.com/originalDownloader.html")
            wait = WebDriverWait(driver, 20)
            input_f = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "input#url, .form-control")))
            driver.execute_script("arguments[0].value = arguments[1];", input_f, link)
            time.sleep(2)
            driver.execute_script("arguments[0].click();", wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "button#search_btn"))))
            dl_btn = wait.until(EC.presence_of_element_located((By.XPATH, "//a[contains(@class, 'download') and contains(@href, 'tikwm.com')]")))
            driver.execute_script("arguments[0].click();", dl_btn)
        
        time.sleep(8)
        if len(os.listdir(save_dir)) > before_count:
            self.db_manager.mark_as_downloaded(video_id, username, link, "success")
            return True
        raise Exception("İndirme başarısız.")

    def scrape_user(self, username):
        driver = None
        try:
            driver = ChromeManager.create_driver(self.config_manager)
            if not username.startswith("@"): username = "@" + username
            driver.get(f"https://www.tiktok.com/{username}")
            time.sleep(7)
            found_links = set()
            for _ in range(self.config_manager.get("scrape_scroll_count", 5)):
                elements = driver.find_elements(By.XPATH, "//a[contains(@href, '/video/') or contains(@href, '/photo/')]")
                for el in elements:
                    href = el.get_attribute("href")
                    if href: found_links.add(href.split("?")[0])
                driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                time.sleep(3)
            return list(found_links)
        finally:
            if driver: driver.quit()

    def download_videos(self, links, chat_id=None):
        total = len(links); success_count = 0; fail_count = 0
        if chat_id: self.send_telegram_message(chat_id, f"⏳ <b>{total}</b> video indirme başlatılıyor...")
        
        driver = ChromeManager.create_driver(self.config_manager)
        try:
            for link in links:
                video_id = link.split('/')[-1].split('?')[0]
                username = link.split('@')[1].split('/')[0] if '@' in link else "user"
                save_dir = os.path.join(self.base_path, username)
                os.makedirs(save_dir, exist_ok=True)

                if self.db_manager.is_already_downloaded(video_id):
                    success_count += 1; continue

                try:
                    self.download_single_video(driver, link, save_dir, video_id, "/photo/" in link, username)
                    success_count += 1
                except:
                    fail_count += 1
                    self.db_manager.mark_as_downloaded(video_id, username, link, "failed")
                
                time.sleep(self.config_manager.get("delay_between_downloads", 3))
        finally:
            driver.quit()
            if chat_id:
                self.send_telegram_message(chat_id, f"✅ Bitti!\nBaşarılı: {success_count}\nHatalı: {fail_count}\nKonum: {self.base_path}")

# ============ BOT KOMUTLARI ============
def init_telegram_bot():
    @bot.message_handler(commands=['start'])
    def handle_start(message):
        db_manager.add_telegram_user(message.chat.id, message.from_user.username)
        bot.send_message(message.chat.id, "🎬 TikTok Bot Aktif!\n/download - Tek link\n/scrape - Tüm profil\n/stats - Durum")

    @bot.message_handler(commands=['download'])
    def handle_download(message):
        msg = bot.send_message(message.chat.id, "🔗 TikTok linkini gönder:")
        bot.register_next_step_handler(msg, lambda m: threading.Thread(target=downloader.download_videos, args=([m.text], m.chat.id)).start())

    @bot.message_handler(commands=['scrape'])
    def handle_scrape(message):
        msg = bot.send_message(message.chat.id, "👤 Kullanıcı adını gönder (@ olmadan):")
        bot.register_next_step_handler(msg, process_scrape_request)

    @bot.message_handler(commands=['stats'])
    def handle_stats(message):
        s, f = db_manager.get_download_stats()
        bot.send_message(message.chat.id, f"📊 Toplam Başarılı: {s}\nHatalı: {f}")

def process_scrape_request(message):
    def run():
        links = downloader.scrape_user(message.text)
        if links:
            bot.send_message(message.chat.id, f"✅ {len(links)} video bulundu, indiriliyor...")
            downloader.download_videos(links, message.chat.id)
        else:
            bot.send_message(message.chat.id, "❌ Video bulunamadı.")
    threading.Thread(target=run).start()

if __name__ == "__main__":
    config_manager = ConfigManager()
    base_path = config_manager.get("download_path")
    logger = LoggerSetup.setup_logger(base_path)
    db_manager = DatabaseManager(base_path)
    downloader = TikTokDownloader(config_manager, db_manager)
    init_telegram_bot()
    print("🤖 Bot Linux üzerinde çalışıyor...")
    bot.infinity_polling()
