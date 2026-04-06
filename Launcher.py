import os
import sys
import json
import time
import shutil
import hashlib
import zipfile
import threading
import subprocess
import configparser
from pathlib import Path
from datetime import datetime

import requests
import gdown
import customtkinter as ctk

# ══════════════════════════════════════════
#  КОНФИГУРАЦИЯ
# ══════════════════════════════════════════

APP_NAME    = "Signal Launcher"
APP_VERSION = "1.0.0"
CONFIG_FILE = "launcher_config.ini"

# ── Единственная ссылка которую нужно менять ──────────────
# Загрузи version.json на Google Drive / Dropbox и вставь ссылку.
# Google Drive: https://drive.google.com/file/d/FILE_ID/view?usp=sharing
# Dropbox:      https://www.dropbox.com/s/XXXXX/version.json?dl=1
VERSION_MANIFEST_URL = "https://drive.google.com/file/d/REPLACE_WITH_VERSION_JSON_ID/view?usp=sharing"

VERSION_CACHE_FILE = "version_cache.json"

# ══════════════════════════════════════════
#  ФОРМАТ version.json
# ══════════════════════════════════════════
#
# {
#   "launcher_version": "1.0.0",
#   "packs": [
#     {
#       "id":          "textures",
#       "version":     "1.2",
#       "url":         "https://drive.google.com/file/d/XXX/view?usp=sharing",
#       "install_dir": "Mods",
#       "checksum":    "abc123md5hash"
#     },
#     {
#       "id":          "missions",
#       "version":     "2.0",
#       "url":         "https://www.dropbox.com/s/XXX/missions.zip?dl=1",
#       "install_dir": "Mods",
#       "checksum":    ""
#     }
#   ]
# }
#
# Чтобы обновить пак — измени "version" и "url" в version.json на сервере.
# При следующем запуске лаунчер тихо обновит только этот пак.

# ══════════════════════════════════════════
#  УТИЛИТЫ
# ══════════════════════════════════════════

def load_config() -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    if os.path.exists(CONFIG_FILE):
        cfg.read(CONFIG_FILE, encoding="utf-8")
    if not cfg.has_section("LAUNCHER"):
        cfg["LAUNCHER"] = {"game_path": "", "installed_packs": "{}"}
        save_config(cfg)
    return cfg

def save_config(cfg: configparser.ConfigParser):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        cfg.write(f)

def get_installed(cfg: configparser.ConfigParser) -> dict:
    try:
        return json.loads(cfg.get("LAUNCHER", "installed_packs", fallback="{}"))
    except Exception:
        return {}

def set_installed(cfg: configparser.ConfigParser, installed: dict):
    cfg["LAUNCHER"]["installed_packs"] = json.dumps(installed)
    save_config(cfg)

def md5(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()

def find_game_exe(base: str) -> str | None:
    for root, _, files in os.walk(base):
        for f in files:
            if f.lower() == "flashinglights.exe":
                return os.path.join(root, f)
    return None

def load_version_cache() -> dict:
    if os.path.exists(VERSION_CACHE_FILE):
        try:
            with open(VERSION_CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_version_cache(data: dict):
    with open(VERSION_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# ══════════════════════════════════════════
#  МЕНЕДЖЕР ВЕРСИЙ
# ══════════════════════════════════════════

class VersionManager:
    """
    Скачивает version.json с сервера, сравнивает с локальным кэшем
    и определяет какие паки нужно установить или обновить.
    """

    def __init__(self, log_cb, status_cb):
        self.log        = log_cb
        self.set_status = status_cb
        self._manifest: dict = {}

    def fetch_manifest(self) -> bool:
        """Скачивает version.json и кэширует его локально."""
        self.set_status("Проверка обновлений...")
        self.log("🔍 Проверяю версии контента на сервере...")
        tmp = "_version_tmp.json"
        try:
            if "drive.google.com" in VERSION_MANIFEST_URL:
                gdown.download(VERSION_MANIFEST_URL, tmp, quiet=True, fuzzy=True)
                if not os.path.exists(tmp):
                    raise FileNotFoundError("Файл не скачан")
                with open(tmp, "r", encoding="utf-8") as f:
                    self._manifest = json.load(f)
                os.remove(tmp)
            else:
                r = requests.get(VERSION_MANIFEST_URL, timeout=15)
                r.raise_for_status()
                self._manifest = r.json()

            save_version_cache(self._manifest)
            self.log("✅ Манифест версий получен с сервера.")
            return True

        except Exception as e:
            self.log(f"⚠️  Не удалось получить манифест: {e}")
            cached = load_version_cache()
            if cached:
                self._manifest = cached
                self.log("📋 Использую кэшированный манифест.")
                return True
            self.log("❌ Нет ни манифеста, ни кэша. Установка невозможна.")
            return False

    def get_packs_to_update(self, installed: dict) -> list:
        """Возвращает только паки которые нужно установить или обновить."""
        to_update = []
        for pack in self._manifest.get("packs", []):
            local  = installed.get(pack["id"], {})
            l_ver  = local.get("version", "") if isinstance(local, dict) else local
            r_ver  = pack.get("version", "")
            if l_ver != r_ver:
                reason = "новая версия" if l_ver else "первая установка"
                to_update.append({**pack, "_reason": reason, "_old_ver": l_ver})
        return to_update

    def get_content_version(self) -> str:
        """Возвращает общую версию контента из манифеста."""
        return self._manifest.get("launcher_version", "?")

    def has_manifest(self) -> bool:
        return bool(self._manifest)

# ══════════════════════════════════════════
#  УСТАНОВЩИК КОНТЕНТА
# ══════════════════════════════════════════

class ContentInstaller:
    def __init__(self, game_path: str, log_cb, progress_cb, status_cb):
        self.game_path    = game_path
        self.log          = log_cb
        self.set_progress = progress_cb
        self.set_status   = status_cb
        self.tmp_dir      = Path("_launcher_tmp")
        self.tmp_dir.mkdir(exist_ok=True)

    def install_packs(self, packs: list, cfg: configparser.ConfigParser) -> bool:
        if not packs:
            self.log("✅ Весь контент актуален — обновлений нет.")
            self.set_progress(1.0)
            return True

        installed = get_installed(cfg)
        total     = len(packs)

        for idx, pack in enumerate(packs):
            old_ver = pack.get("_old_ver", "")
            new_ver = pack.get("version", "")

            if old_ver:
                self.log(f"🔄 Обновляю пак {idx+1}/{total}: v{old_ver} → v{new_ver}")
            else:
                self.log(f"📦 Устанавливаю пак {idx+1}/{total} (v{new_ver})")

            self.set_status(f"Загрузка {idx+1}/{total}...")
            tmp_file = self.tmp_dir / f"pack_{pack['id']}.zip"

            # Скачиваем
            ok = self._download(pack["url"], str(tmp_file), idx, total)
            if not ok:
                self.log(f"❌ Ошибка загрузки пакета {idx+1}.")
                return False

            # Проверка MD5
            if pack.get("checksum"):
                self.set_status("Проверка целостности...")
                actual = md5(str(tmp_file))
                if actual != pack["checksum"]:
                    self.log(f"❌ Контрольная сумма не совпадает!")
                    return False
                self.log("🔒 Целостность подтверждена.")

            # Удаляем старую версию папки пака
            dest         = Path(self.game_path) / pack["install_dir"]
            old_pack_dir = dest / pack["id"]
            if old_pack_dir.exists():
                self.set_status("Удаление старой версии...")
                shutil.rmtree(str(old_pack_dir), ignore_errors=True)
                self.log("🗑  Старая версия удалена.")

            # Распаковка
            self.set_status("Установка...")
            dest.mkdir(parents=True, exist_ok=True)
            try:
                with zipfile.ZipFile(str(tmp_file), "r") as z:
                    z.extractall(str(dest))
            except Exception as e:
                self.log(f"❌ Ошибка распаковки: {e}")
                return False

            # Сохраняем версию
            installed[pack["id"]] = {
                "version":      new_ver,
                "installed_at": datetime.now().isoformat(),
            }
            set_installed(cfg, installed)
            tmp_file.unlink(missing_ok=True)

            self.set_progress((idx + 1) / total)
            self.log(f"✅ Пак {idx+1}/{total} установлен (v{new_ver}).")

        shutil.rmtree(str(self.tmp_dir), ignore_errors=True)
        return True

    def _download(self, url: str, dest: str, idx: int, total: int) -> bool:
        try:
            if "drive.google.com" in url:
                gdown.download(url, dest, quiet=True, fuzzy=True)
                return os.path.exists(dest)
            else:
                r = requests.get(url, stream=True, timeout=60)
                r.raise_for_status()
                file_size  = int(r.headers.get("content-length", 0))
                downloaded = 0
                with open(dest, "wb") as f:
                    for chunk in r.iter_content(8192):
                        f.write(chunk)
                        downloaded += len(chunk)
                        if file_size:
                            self.set_progress((idx + downloaded / file_size) / total)
                return True
        except Exception as e:
            self.log(f"❌ Ошибка загрузки: {e}")
            return False

# ══════════════════════════════════════════
#  GUI
# ══════════════════════════════════════════

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("dark-blue")

CLR_BG      = "#0d0f14"
CLR_PANEL   = "#13161e"
CLR_CARD    = "#1a1d28"
CLR_ACCENT  = "#3d8ef0"
CLR_ACCENT2 = "#5ba3ff"
CLR_SUCCESS = "#3ddc84"
CLR_ERROR   = "#ff4f4f"
CLR_WARNING = "#f0a030"
CLR_TEXT    = "#e8eaf0"
CLR_MUTED   = "#6b7280"
CLR_BORDER  = "#252836"
CLR_UPDATE  = "#b06ef0"

FONT_TITLE = ("Segoe UI", 26, "bold")
FONT_SUB   = ("Segoe UI", 11)
FONT_MONO  = ("Consolas", 9)
FONT_SMALL = ("Segoe UI", 9)


class AnimatedProgressBar(ctk.CTkFrame):
    def __init__(self, master, **kwargs):
        super().__init__(master, fg_color=CLR_BORDER, corner_radius=6, height=8, **kwargs)
        self._bar       = ctk.CTkFrame(self, fg_color=CLR_ACCENT, corner_radius=6, height=8)
        self._bar.place(relx=0, rely=0, relwidth=0.0, relheight=1.0)
        self._pulse     = 0
        self._animating = False

    def set(self, value: float):
        v = max(0.0, min(1.0, value))
        self._bar.place_configure(relwidth=v)
        self._bar.configure(fg_color=CLR_SUCCESS if v >= 1.0 else CLR_ACCENT)

    def start_pulse(self):
        self._animating = True
        self._do_pulse()

    def stop_pulse(self):
        self._animating = False

    def _do_pulse(self):
        if not self._animating:
            return
        self._pulse = (self._pulse + 3) % 360
        import math
        a = 0.7 + 0.3 * math.sin(math.radians(self._pulse))
        r = int(61  + (91  - 61)  * (1 - a))
        g = int(142 + (163 - 142) * (1 - a))
        b = int(240 + (255 - 240) * (1 - a))
        self._bar.configure(fg_color=f"#{r:02x}{g:02x}{b:02x}")
        self.after(30, self._do_pulse)


class LauncherApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.cfg          = load_config()
        self.game_path    = self.cfg.get("LAUNCHER", "game_path", fallback="")
        self._ready       = False
        self._installing  = False
        self._version_mgr = VersionManager(
            log_cb=self._log,
            status_cb=self._set_phase,
        )

        self.title(APP_NAME)
        self.geometry("780x560")
        self.resizable(False, False)
        self.configure(fg_color=CLR_BG)
        self.update_idletasks()
        x = (self.winfo_screenwidth()  - 780) // 2
        y = (self.winfo_screenheight() - 560) // 2
        self.geometry(f"780x560+{x}+{y}")

        self._build_ui()
        self.after(300, self._on_start)

    # ── UI ────────────────────────────────

    def _build_ui(self):
        # Боковая панель
        sidebar = ctk.CTkFrame(self, fg_color=CLR_PANEL, width=220, corner_radius=0)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)

        badge_frame = ctk.CTkFrame(sidebar, fg_color="transparent")
        badge_frame.pack(pady=(32, 8), padx=20)
        badge = ctk.CTkFrame(badge_frame, fg_color=CLR_ACCENT, width=48, height=48, corner_radius=12)
        badge.pack()
        badge.pack_propagate(False)
        ctk.CTkLabel(badge, text="FL", font=("Segoe UI Black", 18), text_color="white").place(relx=.5, rely=.5, anchor="center")

        ctk.CTkLabel(sidebar, text="Signal Launcher", font=("Segoe UI Black", 14), text_color=CLR_TEXT).pack(pady=(10, 2))
        ctk.CTkLabel(sidebar, text="Launcher", font=("Segoe UI", 11), text_color=CLR_MUTED).pack()
        ctk.CTkFrame(sidebar, fg_color=CLR_BORDER, height=1).pack(fill="x", padx=20, pady=18)

        # Карточка статуса
        sc = ctk.CTkFrame(sidebar, fg_color=CLR_CARD, corner_radius=10)
        sc.pack(fill="x", padx=16, pady=4)

        for label, attr, default in [
            ("СТАТУС",           "_status_dot",       "⬤  Инициализация"),
            ("ВЕРСИЯ ЛАУНЧЕРА",  "_launcher_ver_lbl", APP_VERSION),
            ("ВЕРСИЯ КОНТЕНТА",  "_content_ver_label","—"),
        ]:
            ctk.CTkLabel(sc, text=label, font=("Segoe UI", 8, "bold"),
                         text_color=CLR_MUTED).pack(anchor="w", padx=12, pady=(8, 1))
            lbl = ctk.CTkLabel(sc, text=default, font=FONT_SMALL,
                               text_color=CLR_WARNING if label == "СТАТУС" else CLR_MUTED)
            lbl.pack(anchor="w", padx=12, pady=(0, 4))
            setattr(self, attr, lbl)

        ctk.CTkFrame(sidebar, fg_color=CLR_BORDER, height=1).pack(fill="x", padx=20, pady=12)

        self._update_badge = ctk.CTkLabel(sidebar, text="", font=("Segoe UI", 8, "bold"),
                                          text_color=CLR_UPDATE)
        self._update_badge.pack(padx=16, pady=(0, 8))

        ctk.CTkButton(
            sidebar, text="📁  Путь к игре", font=FONT_SMALL, height=32,
            fg_color=CLR_CARD, hover_color=CLR_BORDER,
            text_color=CLR_MUTED, corner_radius=8,
            command=self._browse_game
        ).pack(fill="x", padx=16)

        ctk.CTkLabel(sidebar, text=f"v{APP_VERSION}  •  FL Launcher",
                     font=("Segoe UI", 8), text_color=CLR_BORDER).pack(side="bottom", pady=12)

        # Основная область
        main = ctk.CTkFrame(self, fg_color=CLR_BG, corner_radius=0)
        main.pack(side="right", fill="both", expand=True)

        header = ctk.CTkFrame(main, fg_color="transparent")
        header.pack(fill="x", padx=28, pady=(28, 0))
        ctk.CTkLabel(header, text="Добро пожаловать",
                     font=FONT_TITLE, text_color=CLR_TEXT).pack(anchor="w")
        self._subtitle = ctk.CTkLabel(header, text="Проверяю обновления...",
                                      font=FONT_SUB, text_color=CLR_MUTED)
        self._subtitle.pack(anchor="w", pady=(4, 0))

        ctk.CTkFrame(main, fg_color=CLR_BORDER, height=1).pack(fill="x", padx=28, pady=14)

        # Прогресс
        pc = ctk.CTkFrame(main, fg_color=CLR_CARD, corner_radius=14)
        pc.pack(fill="x", padx=28, pady=4)
        ci = ctk.CTkFrame(pc, fg_color="transparent")
        ci.pack(fill="x", padx=20, pady=14)

        top = ctk.CTkFrame(ci, fg_color="transparent")
        top.pack(fill="x", pady=(0, 7))
        ctk.CTkLabel(top, text="ПОДГОТОВКА КОНТЕНТА",
                     font=("Segoe UI", 9, "bold"), text_color=CLR_MUTED).pack(side="left")
        self._pct_label = ctk.CTkLabel(top, text="0%",
                                       font=("Segoe UI Semibold", 9), text_color=CLR_ACCENT)
        self._pct_label.pack(side="right")

        self._progress = AnimatedProgressBar(ci)
        self._progress.pack(fill="x", pady=(0, 8))

        self._phase_label = ctk.CTkLabel(ci, text="Ожидание...",
                                         font=FONT_SMALL, text_color=CLR_MUTED)
        self._phase_label.pack(anchor="w")

        # Лог
        lf = ctk.CTkFrame(main, fg_color=CLR_CARD, corner_radius=14)
        lf.pack(fill="both", expand=True, padx=28, pady=(10, 0))
        lh = ctk.CTkFrame(lf, fg_color="transparent")
        lh.pack(fill="x", padx=16, pady=(10, 4))
        ctk.CTkLabel(lh, text="ЛОГ", font=("Segoe UI", 8, "bold"),
                     text_color=CLR_MUTED).pack(side="left")

        self._log_box = ctk.CTkTextbox(
            lf, fg_color="transparent", text_color=CLR_MUTED,
            font=FONT_MONO, border_width=0, wrap="word", state="disabled"
        )
        self._log_box.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        # Кнопка запуска
        bf = ctk.CTkFrame(main, fg_color="transparent")
        bf.pack(fill="x", padx=28, pady=14)
        self._launch_btn = ctk.CTkButton(
            bf, text="▶   ЗАПУСТИТЬ ИГРУ",
            font=("Segoe UI Black", 13), height=48,
            fg_color=CLR_ACCENT, hover_color=CLR_ACCENT2,
            text_color="white", corner_radius=12,
            state="disabled", command=self._launch_game,
        )
        self._launch_btn.pack(fill="x")

    # ── логика ────────────────────────────

    def _on_start(self):
        if not self.game_path or not os.path.isdir(self.game_path):
            self._log("⚠️  Укажите путь к папке с игрой (кнопка слева).")
            self._set_status("Ожидание пути к игре", CLR_WARNING)
            return
        self._start_check_and_install()

    def _browse_game(self):
        from tkinter import filedialog
        path = filedialog.askdirectory(title="Выбери папку с Flashing Lights")
        if path:
            self.game_path = path
            self.cfg["LAUNCHER"]["game_path"] = path
            save_config(self.cfg)
            self._log(f"📂 Путь: {path}")
            if not self._installing:
                self._start_check_and_install()

    def _start_check_and_install(self):
        if self._installing:
            return
        self._installing = True
        self._ready      = False
        self._launch_btn.configure(state="disabled")
        self._progress.start_pulse()
        self._set_status("Проверка...", CLR_WARNING)

        def worker():
            # Шаг 1: получаем манифест
            ok = self._version_mgr.fetch_manifest()
            if not ok:
                self.after(0, lambda: self._on_install_done(False))
                return

            # Показываем версию контента
            cv = self._version_mgr.get_content_version()
            self.after(0, lambda: self._content_ver_label.configure(
                text=f"v{cv}", text_color=CLR_TEXT
            ))

            # Шаг 2: определяем что обновить
            installed = get_installed(self.cfg)
            to_update = self._version_mgr.get_packs_to_update(installed)

            if to_update:
                n = len(to_update)
                noun = "обновление" if n == 1 else "обновления" if n < 5 else "обновлений"
                self.after(0, lambda: self._update_badge.configure(
                    text=f"🔄 {n} {noun}", text_color=CLR_UPDATE
                ))
                self.after(0, lambda: self._subtitle.configure(
                    text=f"Найдено обновлений: {n}. Устанавливаю...",
                    text_color=CLR_UPDATE
                ))
                self._log(f"🔄 Найдено обновлений: {n}")
            else:
                self.after(0, lambda: self._subtitle.configure(
                    text="Контент актуален. Готово к запуску!",
                    text_color=CLR_SUCCESS
                ))

            # Шаг 3: устанавливаем
            installer = ContentInstaller(
                self.game_path,
                log_cb=self._log,
                progress_cb=self._set_progress,
                status_cb=self._set_phase,
            )
            result = installer.install_packs(to_update, self.cfg)
            self.after(0, lambda: self._on_install_done(result))

        threading.Thread(target=worker, daemon=True).start()

    def _on_install_done(self, success: bool):
        self._installing = False
        self._progress.stop_pulse()
        if success:
            self._ready = True
            self._set_progress(1.0)
            self._set_phase("Готово к запуску!")
            self._set_status("Актуально", CLR_SUCCESS)
            self._update_badge.configure(text="✅ Всё актуально", text_color=CLR_SUCCESS)
            self._log("🚀 Всё готово! Нажмите «Запустить игру».")
            self._launch_btn.configure(state="normal")
        else:
            self._set_status("Ошибка", CLR_ERROR)
            self._set_phase("Ошибка. Проверьте лог.")
            self._log("❌ Установка завершилась с ошибкой.")

    def _launch_game(self):
        if not self._ready:
            return
        exe = find_game_exe(self.game_path)
        if not exe:
            self._log("❌ FlashingLights.exe не найден!")
            return
        self._log("🎮 Запускаю игру...")
        try:
            subprocess.Popen([exe], cwd=os.path.dirname(exe))
            self.after(1500, self.withdraw)
        except Exception as e:
            self._log(f"❌ Ошибка: {e}")

    # ── helpers ───────────────────────────

    def _log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        def _w():
            self._log_box.configure(state="normal")
            self._log_box.insert("end", f"[{ts}]  {msg}\n")
            self._log_box.see("end")
            self._log_box.configure(state="disabled")
        self.after(0, _w)

    def _set_progress(self, value: float):
        def _u():
            self._progress.set(value)
            self._pct_label.configure(text=f"{int(value*100)}%")
        self.after(0, _u)

    def _set_phase(self, text: str):
        self.after(0, lambda: self._phase_label.configure(text=text))

    def _set_status(self, text: str, color: str):
        self.after(0, lambda: self._status_dot.configure(
            text=f"⬤  {text}", text_color=color
        ))


# ══════════════════════════════════════════
#  ТОЧКА ВХОДА
# ══════════════════════════════════════════

if __name__ == "__main__":
    app = LauncherApp()
    app.mainloop()