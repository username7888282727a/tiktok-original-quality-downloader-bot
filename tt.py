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

ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")

class TikTokProGUI(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("TikTok Pro Downloader v10.6 - Final Edition")
        self.geometry("980x850")
        
        # Varsayılan Yol Ayarı
        self.base_path = os.path.join(os.path.expanduser("~"), "Documents", "TikTok_Downloads")
        os.makedirs(self.base_path, exist_ok=True)
        
        self.hatali_linkler = []
        self.setup_ui()

    def setup_ui(self):
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        # --- Sidebar ---
        self.sidebar = ctk.CTkFrame(self, width=200, corner_radius=0)
        self.sidebar.grid(row=0, column=0, sticky="nsew")
        self.status_dot = ctk.CTkLabel(self.sidebar, text="● Durum: Bekliyor", text_color="orange")
        self.status_dot.grid(row=1, column=0, padx=20, pady=20)
        self.stats_label = ctk.CTkLabel(self.sidebar, text="Başarılı: 0\nHatalı: 0", justify="left")
        self.stats_label.grid(row=2, column=0, padx=20, pady=10)

        # --- Main Layout ---
        self.main_container = ctk.CTkFrame(self, corner_radius=15)
        self.main_container.grid(row=0, column=1, padx=20, pady=20, sticky="nsew")

        # Yol Seçimi
        self.path_frame = ctk.CTkFrame(self.main_container, fg_color="transparent")
        self.path_frame.pack(fill="x", padx=20, pady=10)
        self.path_entry = ctk.CTkEntry(self.path_frame, width=450)
        self.path_entry.insert(0, self.base_path)
        self.path_entry.pack(side="left", padx=5)
        self.btn_browse = ctk.CTkButton(self.path_frame, text="Klasör", width=80, command=self.browse_path)
        self.btn_browse.pack(side="left")

        # Fetch Alanı
        self.user_entry = ctk.CTkEntry(self.main_container, placeholder_text="Kullanıcı Adı @username", width=450)
        self.user_entry.pack(padx=20, pady=10)
        self.btn_scrape = ctk.CTkButton(self.main_container, text="LİNKLERİ GETİR", fg_color="#cc0000", command=self.start_scraping)
        self.btn_scrape.pack(pady=5)

        self.textbox = ctk.CTkTextbox(self.main_container, width=700, height=200)
        self.textbox.pack(padx=20, pady=10)

        # --- İLERLEME SAYACI VE BAR (YENİDEN DÜZENLENDİ) ---
        self.progress_label = ctk.CTkLabel(self.main_container, text="İlerleme: 0 / 0")
        self.progress_label.pack(pady=(10, 0))
        self.progress_bar = ctk.CTkProgressBar(self.main_container, width=650)
        self.progress_bar.set(0)
        self.progress_bar.pack(pady=10)

        # Tablo
        self.tree = ttk.Treeview(self.main_container, columns=("type", "user", "status"), show="headings", height=8)
        self.tree.heading("type", text="Tür")
        self.tree.heading("user", text="Kullanıcı")
        self.tree.heading("status", text="Durum")
        self.tree.pack(padx=20, pady=10, fill="x")

        self.btn_start = ctk.CTkButton(self.main_container, text="İNDİRMEYİ BAŞLAT", command=self.start_process, height=45)
        self.btn_start.pack(pady=15)

    def browse_path(self):
        d = filedialog.askdirectory()
        if d:
            self.path_entry.delete(0, "end"); self.path_entry.insert(0, d)
            self.base_path = d

    def get_chrome_version(self):
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Google\Chrome\BLBeacon", 0, winreg.KEY_READ)
            v, _ = winreg.QueryValueEx(key, "version")
            return int(v.split(".")[0])
        except: return 144

    def start_scraping(self):
        user = self.user_entry.get().strip()
        if not user: return
        self.btn_scrape.configure(state="disabled")
        threading.Thread(target=self.scrape_engine, args=(user,), daemon=True).start()

    def scrape_engine(self, username):
        driver = uc.Chrome(version_main=self.get_chrome_version(), use_subprocess=True)
        try:
            if not username.startswith("@"): username = "@" + username
            driver.get(f"https://www.tiktok.com/{username}"); time.sleep(6)
            found_links = set()
            for _ in range(5):
                elements = driver.find_elements(By.XPATH, "//a[contains(@href, '/video/') or contains(@href, '/photo/')]")
                for el in elements: found_links.add(el.get_attribute("href").split("?")[0])
                driver.execute_script("window.scrollTo(0, document.body.scrollHeight);"); time.sleep(3)
            self.after(0, lambda: self.update_ui_after_scrape(found_links))
        finally: driver.quit(); self.after(0, lambda: self.btn_scrape.configure(state="normal"))

    def update_ui_after_scrape(self, links):
        self.textbox.delete("1.0", "end")
        if links: self.textbox.insert("1.0", "\n".join(list(links)))

    def start_process(self):
        links = [l.strip() for l in self.textbox.get("1.0", "end-1c").splitlines() if l.strip()]
        if not links: return
        self.btn_start.configure(state="disabled")
        self.hatali_linkler = []
        threading.Thread(target=self.downloader_engine, args=(links,), daemon=True).start()

    def downloader_engine(self, links):
        total = len(links); self.success_count = 0; self.fail_count = 0
        
        # Hız Ayarları
        options = uc.ChromeOptions()
        options.page_load_strategy = 'eager'
        driver = uc.Chrome(version_main=self.get_chrome_version(), options=options, use_subprocess=True)
        driver.set_page_load_timeout(25)
        wait = WebDriverWait(driver, 20)
        
        try:
            for index, link in enumerate(links, start=1):
                # --- SAYAÇ GÜNCELLEME ---
                self.after(0, lambda i=index: self.progress_label.configure(text=f"İlerleme: {i} / {total}"))
                self.progress_bar.set(index / total)
                
                video_id = link.split('/')[-1].split('?')[0]
                is_photo = "/photo/" in link
                username = link.split('@')[1].split('/')[0] if '@' in link else "user"
                save_dir = os.path.join(self.path_entry.get(), username)
                os.makedirs(save_dir, exist_ok=True)

                # --- SMART SKIP (EĞER DOSYA VARSA ATLA) ---
                if any(video_id in f for f in os.listdir(save_dir)):
                    self.success_count += 1
                    self.tree.insert("", 0, values=("FOTO" if is_photo else "VİDEO", username, "⏩ ATLANDI"))
                    continue

                before_count = len(os.listdir(save_dir))
                
                try:
                    driver.execute_cdp_cmd("Page.setDownloadBehavior", {"behavior": "allow", "downloadPath": save_dir})

                    if is_photo:
                        # IMAIGER KODUN (DOKUNULMADI)
                        driver.get("https://imaiger.com/tool/tiktok-slideshow-downloader"); time.sleep(6)
                        p_in = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "input")))
                        driver.execute_script("arguments[0].value = ''; arguments[0].focus();", p_in)
                        for char in link: p_in.send_keys(char); time.sleep(0.01)
                        try: driver.execute_script("arguments[0].click();", driver.find_element(By.XPATH, "//button[contains(., 'Load')]"))
                        except: p_in.send_keys(Keys.ENTER)
                        time.sleep(5)
                        driver.execute_script("arguments[0].click();", wait.until(EC.presence_of_element_located((By.XPATH, "//button[contains(text(), 'Download All')]"))))
                        time.sleep(5)
                    else:
                        # TIKWM ULTRA FIX (DOKUNULMADI)
                        driver.get("https://www.tikwm.com/originalDownloader.html")
                        input_f = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "input#url, .form-control")))
                        js_script = "arguments[0].value = arguments[1]; arguments[0].dispatchEvent(new Event('input', { bubbles: true })); arguments[0].dispatchEvent(new Event('change', { bubbles: true }));"
                        driver.execute_script(js_script, input_f, link)
                        time.sleep(2)
                        driver.execute_script("arguments[0].click();", wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "button#search_btn"))))
                        dl_btn = wait.until(EC.presence_of_element_located((By.XPATH, "//a[contains(@class, 'download') and contains(@href, 'tikwm.com')]")))
                        driver.execute_script("arguments[0].click();", dl_btn)
                        time.sleep(6)

                    # --- DOSYA KONTROLÜ (SAYMA) ---
                    if len(os.listdir(save_dir)) > before_count:
                        self.success_count += 1
                        self.tree.insert("", 0, values=("FOTO" if is_photo else "VİDEO", username, "✅ TAMAM"))
                    else:
                        raise Exception("Inmedi")

                except Exception:
                    self.fail_count += 1
                    self.hatali_linkler.append(link)
                    self.tree.insert("", 0, values=("FOTO" if is_photo else "VİDEO", username, "❌ HATA"))
                
                self.stats_label.configure(text=f"Başarılı: {self.success_count}\nHatalı: {self.fail_count}")
        finally:
            driver.quit()
            self.after(0, lambda: self.btn_start.configure(state="normal"))
            self.after(0, lambda: self.update_textbox_with_fails())

    def update_textbox_with_fails(self):
        self.textbox.delete("1.0", "end")
        if self.hatali_linkler:
            self.textbox.insert("1.0", "\n".join(self.hatali_linkler))

if __name__ == "__main__":
    app = TikTokProGUI(); app.mainloop()
