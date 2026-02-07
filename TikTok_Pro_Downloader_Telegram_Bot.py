import customtkinter as ctk
from tkinter import messagebox, ttk, filedialog
import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys
import threading
import os
import time
import winreg
import logging
import json
import sqlite3
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from tenacity import retry, stop_after_attempt, wait_exponential
from telebot import TeleBot
import requests
from io import BytesIO

ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")

# ============ TELEGRAM BOT AYARLARI ============
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
            "enable_notifications": True,
            "enable_logging": True,
            "scrape_scroll_count": 5,
            "headless_mode": True,
            "telegram_enabled": True
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

# ============ BİLDİRİM SİSTEMİ ============
class NotificationManager:
    @staticmethod
    def show_notification(title, message, duration=5):
        try:
            from win10toast import ToastNotifier
            notifier = ToastNotifier()
            notifier.show_toast(title, message, duration=duration, threaded=True)
        except:
            pass
    
    @staticmethod
    def send_telegram_message(chat_id, message):
        try:
            if config_manager.get("telegram_enabled"):
                bot.send_message(chat_id, message)
        except Exception as e:
            logger.error(f"Telegram message error: {e}")
    
    @staticmethod
    def send_telegram_file(chat_id, file_path):
        try:
            if config_manager.get("telegram_enabled"):
                with open(file_path, 'rb') as file:
                    bot.send_document(chat_id, file)
        except Exception as e:
            logger.error(f"Telegram file error: {e}")

# ============ CHROME YÖNETICISI (HEADLESS MOD) ============
class ChromeManager:
    @staticmethod
    def get_chrome_version():
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Google\Chrome\BLBeacon", 0, winreg.KEY_READ)
            v, _ = winreg.QueryValueEx(key, "version")
            return int(v.split(".")[0])
        except:
            return 144
    
    @staticmethod
    def create_driver(config, is_fast=True):
        options = uc.ChromeOptions()
        options.page_load_strategy = 'eager' if is_fast else 'normal'
        
        # HEADLESS MODE (Arka planda çalışır)
        if config.get("headless_mode", True):
            options.add_argument("--headless")
            options.add_argument("--start-maximized")
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-dev-shm-usage")
        
        if config.get("use_proxy") and config.get("proxy_server"):
            options.add_argument(f"--proxy-server={config.get('proxy_server')}")
        
        # Sessizlik seçenekleri
        options.add_argument("--disable-notifications")
        options.add_argument("--disable-popup-blocking")
        options.add_argument("--blink-settings=imagesEnabled=false")  # Resimleri yükleme (hız)
        options.add_argument("--disable-extensions")
        options.add_argument("--disable-web-resources")
        
        driver = uc.Chrome(
            version_main=ChromeManager.get_chrome_version(),
            options=options,
            use_subprocess=True
        )
        driver.set_page_load_timeout(config.get("timeout", 25))
        return driver

# ============ ANA UYGULAMA SINIFI ============
class TikTokProGUI(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("TikTok Pro Downloader v11 - Telegram Bot Edition")
        self.geometry("1100x950")
        
        # Konfigürasyon ve Veritabanı
        global config_manager
        config_manager = ConfigManager()
        self.config_manager = config_manager
        self.base_path = self.config_manager.get("download_path")
        os.makedirs(self.base_path, exist_ok=True)
        
        # Logger
        global logger
        logger = LoggerSetup.setup_logger(self.base_path)
        
        # Veritabanı
        self.db_manager = DatabaseManager(self.base_path)
        
        self.hatali_linkler = []
        self.success_count = 0
        self.fail_count = 0
        self.is_downloading = False
        self.telegram_user_id = None
        
        self.setup_ui()
        self.update_stats_from_db()
        self.start_telegram_bot()
    
    def setup_ui(self):
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        # --- SIDEBAR ---
        self.sidebar = ctk.CTkFrame(self, width=200, corner_radius=0)
        self.sidebar.grid(row=0, column=0, sticky="nsew")
        
        self.status_dot = ctk.CTkLabel(self.sidebar, text="● Durum: Bekliyor", text_color="orange")
        self.status_dot.grid(row=0, column=0, padx=20, pady=20)
        
        self.stats_label = ctk.CTkLabel(self.sidebar, text="Başarılı: 0\nHatalı: 0", justify="left")
        self.stats_label.grid(row=1, column=0, padx=20, pady=10)
        
        self.headless_status = ctk.CTkLabel(self.sidebar, text="🔇 Headless: ON", text_color="green")
        self.headless_status.grid(row=2, column=0, padx=20, pady=10)
        
        # Version Label
        ctk.CTkLabel(self.sidebar, text="v11.0 Telegram", text_color="gray", font=("Arial", 10)).grid(row=10, column=0, padx=20, pady=(50, 20))

        # --- ANA CONTAINER ---
        self.main_container = ctk.CTkFrame(self, corner_radius=15)
        self.main_container.grid(row=0, column=1, padx=20, pady=20, sticky="nsew")

        # --- YOL SEÇİMİ ---
        self.path_frame = ctk.CTkFrame(self.main_container, fg_color="transparent")
        self.path_frame.pack(fill="x", padx=20, pady=10)
        ctk.CTkLabel(self.path_frame, text="İndirme Klasörü:").pack(side="left", padx=5)
        self.path_entry = ctk.CTkEntry(self.path_frame, width=450)
        self.path_entry.insert(0, self.base_path)
        self.path_entry.pack(side="left", padx=5)
        self.btn_browse = ctk.CTkButton(self.path_frame, text="Klasör", width=80, command=self.browse_path)
        self.btn_browse.pack(side="left")

        # --- AYARLAR FRAME ---
        settings_frame = ctk.CTkFrame(self.main_container, fg_color="transparent")
        settings_frame.pack(fill="x", padx=20, pady=10)
        
        ctk.CTkLabel(settings_frame, text="İndirme Aralığı (sn):").pack(side="left", padx=5)
        self.delay_spinbox = ctk.CTkOptionMenu(
            settings_frame,
            values=["1", "2", "3", "5", "10"],
            command=self.update_delay
        )
        self.delay_spinbox.set(str(self.config_manager.get("delay_between_downloads")))
        self.delay_spinbox.pack(side="left", padx=5)
        
        ctk.CTkLabel(settings_frame, text="Timeout (sn):").pack(side="left", padx=5)
        self.timeout_spinbox = ctk.CTkOptionMenu(
            settings_frame,
            values=["15", "20", "25", "30"],
            command=self.update_timeout
        )
        self.timeout_spinbox.set(str(self.config_manager.get("timeout")))
        self.timeout_spinbox.pack(side="left", padx=5)
        
        ctk.CTkLabel(settings_frame, text="Thread Sayısı:").pack(side="left", padx=5)
        self.workers_spinbox = ctk.CTkOptionMenu(
            settings_frame,
            values=["1", "2", "3", "4"],
            command=self.update_workers
        )
        self.workers_spinbox.set(str(self.config_manager.get("max_workers")))
        self.workers_spinbox.pack(side="left", padx=5)

        # --- HEADLESS MODE TOGGLE ---
        headless_frame = ctk.CTkFrame(self.main_container, fg_color="transparent")
        headless_frame.pack(fill="x", padx=20, pady=10)
        
        self.headless_checkbox = ctk.CTkCheckBox(
            headless_frame,
            text="Headless Mode (Arka Planda)",
            command=self.toggle_headless
        )
        self.headless_checkbox.pack(side="left", padx=5)
        if self.config_manager.get("headless_mode", True):
            self.headless_checkbox.select()

        # --- PROXY FRAME ---
        proxy_frame = ctk.CTkFrame(self.main_container, fg_color="transparent")
        proxy_frame.pack(fill="x", padx=20, pady=10)
        
        self.proxy_checkbox = ctk.CTkCheckBox(proxy_frame, text="Proxy Kullan")
        self.proxy_checkbox.pack(side="left", padx=5)
        
        self.proxy_entry = ctk.CTkEntry(proxy_frame, placeholder_text="IP:PORT", width=200)
        self.proxy_entry.insert(0, self.config_manager.get("proxy_server", ""))
        self.proxy_entry.pack(side="left", padx=5)

        # --- FETCH ALANI ---
        self.user_entry = ctk.CTkEntry(self.main_container, placeholder_text="Kullanıcı Adı @username", width=450)
        self.user_entry.pack(padx=20, pady=10)
        self.btn_scrape = ctk.CTkButton(self.main_container, text="LİNKLERİ GETİR", fg_color="#cc0000", command=self.start_scraping)
        self.btn_scrape.pack(pady=5)

        # --- TEXTBOX ---
        self.textbox = ctk.CTkTextbox(self.main_container, width=700, height=150)
        self.textbox.pack(padx=20, pady=10)

        # --- İLERLEME SAYACI VE BAR ---
        self.progress_label = ctk.CTkLabel(self.main_container, text="İlerleme: 0 / 0")
        self.progress_label.pack(pady=(10, 0))
        self.progress_bar = ctk.CTkProgressBar(self.main_container, width=650)
        self.progress_bar.set(0)
        self.progress_bar.pack(pady=10)

        # --- TABLO ---
        self.tree = ttk.Treeview(self.main_container, columns=("type", "user", "status"), show="headings", height=6)
        self.tree.heading("type", text="Tür")
        self.tree.heading("user", text="Kullanıcı")
        self.tree.heading("status", text="Durum")
        self.tree.pack(padx=20, pady=10, fill="x")

        # --- BUTONLAR ---
        button_frame = ctk.CTkFrame(self.main_container, fg_color="transparent")
        button_frame.pack(pady=15)
        
        self.btn_start = ctk.CTkButton(button_frame, text="İNDİRMEYİ BAŞLAT", command=self.start_process, height=45, width=200)
        self.btn_start.pack(side="left", padx=10)
        
        self.btn_stop = ctk.CTkButton(button_frame, text="DURDUR", command=self.stop_process, height=45, width=100, fg_color="#666666")
        self.btn_stop.pack(side="left", padx=10)

    def toggle_headless(self):
        is_headless = self.headless_checkbox.get()
        self.config_manager.set("headless_mode", bool(is_headless))
        if is_headless:
            self.headless_status.configure(text="🔇 Headless: ON", text_color="green")
        else:
            self.headless_status.configure(text="👁️ Headless: OFF", text_color="red")

    def browse_path(self):
        d = filedialog.askdirectory()
        if d:
            self.path_entry.delete(0, "end")
            self.path_entry.insert(0, d)
            self.base_path = d
            self.config_manager.set("download_path", d)

    def update_delay(self, value):
        self.config_manager.set("delay_between_downloads", int(value))

    def update_timeout(self, value):
        self.config_manager.set("timeout", int(value))

    def update_workers(self, value):
        self.config_manager.set("max_workers", int(value))

    def update_stats_from_db(self):
        success, failed = self.db_manager.get_download_stats()
        self.after(0, lambda: self.stats_label.configure(text=f"Başarılı: {success}\nHatalı: {failed}"))

    def start_telegram_bot(self):
        """Telegram Bot'u arka planda çalıştır"""
        threading.Thread(target=self.run_telegram_bot, daemon=True).start()

    def run_telegram_bot(self):
        """Telegram Bot'un ana loop'u"""
        
        @bot.message_handler(commands=['start'])
        def handle_start(message):
            chat_id = message.chat.id
            username = message.from_user.username or "Unknown"
            self.db_manager.add_telegram_user(chat_id, username)
            self.telegram_user_id = chat_id
            
            response = """
🎬 TikTok Pro Downloader Bot'a Hoşgeldiniz!

📌 Komutlar:
/download - Video/Foto indirmek için
/stats - İstatistikler görmek için
/help - Yardım almak için
            """
            bot.send_message(chat_id, response)
            logger.info(f"New telegram user: {username} ({chat_id})")
        
        @bot.message_handler(commands=['download'])
        def handle_download(message):
            chat_id = message.chat.id
            msg = bot.send_message(chat_id, "🔗 Lütfen TikTok linkini gönder:")
            bot.register_next_step_handler(msg, self.process_link, chat_id)
        
        @bot.message_handler(commands=['stats'])
        def handle_stats(message):
            chat_id = message.chat.id
            success, failed = self.db_manager.get_download_stats()
            stats_text = f"""
📊 İstatistikler:
✅ Başarılı: {success}
❌ Hatalı: {failed}
📈 Toplam: {success + failed}
            """
            bot.send_message(chat_id, stats_text)
        
        @bot.message_handler(commands=['help'])
        def handle_help(message):
            chat_id = message.chat.id
            help_text = """
💡 Yardım:

1️⃣ /download - İndirme işlemi başlat
2️⃣ /stats - İstatistikleri görmek için
3️⃣ /help - Bu mesajı görmek için

⚠️ Linkleri doğru formatta gönderin:
- https://www.tiktok.com/@username/video/123456789
            """
            bot.send_message(chat_id, help_text)
        
        @bot.message_handler(func=lambda message: True)
        def handle_message(message):
            chat_id = message.chat.id
            if "tiktok.com" in message.text.lower():
                bot.send_message(chat_id, "⏳ İndirme işlemi başlatılıyor...")
                self.process_link(message, chat_id)
            else:
                bot.send_message(chat_id, "❌ Geçersiz komut! /help yazarak yardım alabilirsiniz.")
        
        logger.info("Telegram Bot başlatıldı!")
        bot.infinity_polling()

    def process_link(self, message, chat_id):
        """Telegram'dan gelen linki işle"""
        link = message.text.strip()
        
        if "tiktok.com" not in link:
            bot.send_message(chat_id, "❌ Geçerli bir TikTok linki gönder!")
            return
        
        # Linki textbox'a ekle
        self.after(0, lambda: self.textbox.insert("1.0", link + "\n"))
        self.telegram_user_id = chat_id
        
        bot.send_message(chat_id, "✅ Link eklendi! İndirmeyi başlatıyorum...")
        
        # Otomatik indirme başlat
        self.after(0, self.start_process)

    def start_scraping(self):
        user = self.user_entry.get().strip()
        if not user:
            messagebox.showwarning("Uyarı", "Kullanıcı adı girin!")
            return
        self.btn_scrape.configure(state="disabled")
        self.status_dot.configure(text="● Durum: Bağlanıyor...", text_color="yellow")
        threading.Thread(target=self.scrape_engine, args=(user,), daemon=True).start()

    def scrape_engine(self, username):
        driver = None
        try:
            driver = ChromeManager.create_driver(self.config_manager, is_fast=True)
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
            self.after(0, lambda: self.update_ui_after_scrape(found_links))
        except Exception as e:
            logger.error(f"Scrape hatası: {e}")
            self.after(0, lambda: messagebox.showerror("Hata", f"Scrape başarısız: {str(e)}"))
        finally:
            if driver:
                driver.quit()
            self.after(0, lambda: self.btn_scrape.configure(state="normal"))
            self.after(0, lambda: self.status_dot.configure(text="● Durum: Bekliyor", text_color="orange"))

    def update_ui_after_scrape(self, links):
        self.textbox.delete("1.0", "end")
        if links:
            self.textbox.insert("1.0", "\n".join(sorted(list(links))))
            messagebox.showinfo("Başarılı", f"{len(links)} link bulundu!")

    def start_process(self):
        links = [l.strip() for l in self.textbox.get("1.0", "end-1c").splitlines() if l.strip()]
        if not links:
            messagebox.showwarning("Uyarı", "Link listesi boş!")
            if self.telegram_user_id:
                NotificationManager.send_telegram_message(self.telegram_user_id, "❌ Link listesi boş!")
            return
        
        self.is_downloading = True
        self.btn_start.configure(state="disabled")
        self.btn_scrape.configure(state="disabled")
        self.status_dot.configure(text="● Durum: İndiriyor...", text_color="green")
        self.hatali_linkler = []
        self.success_count = 0
        self.fail_count = 0
        
        if self.telegram_user_id:
            NotificationManager.send_telegram_message(self.telegram_user_id, f"⏳ {len(links)} video indirme başlatılıyor...")
        
        threading.Thread(target=self.downloader_engine, args=(links,), daemon=True).start()

    def stop_process(self):
        self.is_downloading = False
        logger.info("İndirme durduruldu!")
        if self.telegram_user_id:
            NotificationManager.send_telegram_message(self.telegram_user_id, "⏹️ İndirme durduruldu!")

    @retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=1, min=2, max=5))
    def download_single_video(self, driver, link, save_dir, video_id, is_photo, username):
        """Tek bir video indirmek"""
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
                self.success_count += 1
                self.db_manager.mark_as_downloaded(video_id, username, link, "success")
                self.tree.insert("", 0, values=("FOTO" if is_photo else "VİDEO", username, "✅ TAMAM"))
                logger.info(f"İndirildi: {link}")
                return True
            else:
                raise Exception("Dosya indirilmedi")
        except Exception as e:
            logger.error(f"Download error: {e}")
            raise

    def downloader_engine(self, links):
        total = len(links)
        self.success_count = 0
        self.fail_count = 0
        
        try:
            max_workers = self.config_manager.get("max_workers", 2)
            drivers = [ChromeManager.create_driver(self.config_manager) for _ in range(max_workers)]
            
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {}
                
                for index, link in enumerate(links):
                    if not self.is_downloading:
                        break
                    
                    driver = drivers[index % max_workers]
                    
                    self.after(0, lambda i=index+1: self.progress_label.configure(text=f"İlerleme: {i} / {total}"))
                    self.after(0, lambda i=index+1: self.progress_bar.set(i / total))
                    
                    video_id = link.split('/')[-1].split('?')[0]
                    is_photo = "/photo/" in link
                    username = link.split('@')[1].split('/')[0] if '@' in link else "user"
                    save_dir = os.path.join(self.path_entry.get(), username)
                    os.makedirs(save_dir, exist_ok=True)

                    # Veritabanı kontrolü
                    if self.db_manager.is_already_downloaded(video_id):
                        self.success_count += 1
                        self.tree.insert("", 0, values=("FOTO" if is_photo else "VİDEO", username, "⏩ ATLANDI"))
                        continue

                    future = executor.submit(
                        self.download_single_video,
                        driver, link, save_dir, video_id, is_photo, username
                    )
                    futures[future] = (link, username, is_photo)
                    
                    time.sleep(self.config_manager.get("delay_between_downloads", 3))

                for future in as_completed(futures):
                    if not self.is_downloading:
                        break
                    
                    link, username, is_photo = futures[future]
                    try:
                        future.result()
                    except Exception as e:
                        self.fail_count += 1
                        self.hatali_linkler.append(link)
                        self.tree.insert("", 0, values=("FOTO" if is_photo else "VİDEO", username, "❌ HATA"))
                        video_id = link.split('/')[-1].split('?')[0]
                        self.db_manager.mark_as_downloaded(video_id, username, link, "failed")
                        logger.error(f"Failed: {link} - {str(e)}")
                    
                    self.after(0, lambda: self.stats_label.configure(text=f"Başarılı: {self.success_count}\nHatalı: {self.fail_count}"))
        finally:
            for driver in drivers:
                try:
                    driver.quit()
                except:
                    pass
            
            self.after(0, lambda: self.btn_start.configure(state="normal"))
            self.after(0, lambda: self.btn_scrape.configure(state="normal"))
            self.after(0, lambda: self.update_textbox_with_fails())
            self.after(0, lambda: self.status_dot.configure(text="● Durum: Tamamlandı", text_color="green"))
            self.is_downloading = False
            
            logger.info(f"İndirme tamamlandı: {self.success_count} başarılı, {self.fail_count} hatalı")
            
            if self.telegram_user_id:
                telegram_msg = f"""
✅ İndirme Tamamlandı!

📊 Sonuçlar:
✅ Başarılı: {self.success_count}
❌ Hatalı: {self.fail_count}
📁 Klasör: {self.path_entry.get()}
                """
                NotificationManager.send_telegram_message(self.telegram_user_id, telegram_msg)
            
            NotificationManager.show_notification(
                "TikTok Downloader",
                f"İndirme Tamamlandı!\nBaşarılı: {self.success_count}\nHatalı: {self.fail_count}"
            )

    def update_textbox_with_fails(self):
        self.textbox.delete("1.0", "end")
        if self.hatali_linkler:
            self.textbox.insert("1.0", "\n".join(self.hatali_linkler))
            messagebox.showwarning("Hatalı İndirmeler", f"{len(self.hatali_linkler)} hata oluştu")
        else:
            self.textbox.insert("1.0", "Tüm indirmeler başarılı! ✅")
            messagebox.showinfo("Başarılı", "Tüm indirmeler tamamlandı! ✅")

if __name__ == "__main__":
    try:
        app = TikTokProGUI()
        app.mainloop()
    except Exception as e:
        messagebox.showerror("Kritik Hata", f"Uygulama başlatılamadı: {str(e)}")