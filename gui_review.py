#!/usr/bin/env python3
"""
Простое оконное приложение для локального код-ревью (обёртка над agent_system.py).

Поля:
  - Папка проекта (git-репозиторий);
  - Многострочное описание того, что сделано в изменениях;
  - Модель LM Studio (по умолчанию автоопределение).

Кнопка "Запуск" стартует ревью, лог пишется прямо в окно.
После завершения отчёт автоматически открывается в Notepad++.

Запуск без консольного окна:
    pythonw gui_review.py
"""

import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from urllib.request import urlopen
import json

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

SCRIPT_DIR = Path(__file__).resolve().parent
AGENT = SCRIPT_DIR / "agent_system.py"
LM_STUDIO_BASE = os.environ.get("LM_STUDIO_URL", "http://localhost:1234/v1")

AUTO_MODEL_LABEL = "(автоопределение)"

CONFIG_DIR = Path(os.environ.get("APPDATA") or Path.home()) / "LocalCodeReview"
CONFIG_FILE = CONFIG_DIR / "gui_state.json"


def save_state(app):
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "project": app.proj_var.get(),
            "model": app.model_var.get(),
            "extra_open": bool(app.extra.winfo_manager()),
            "open_npp": app.open_npp_var.get(),
            "batch": app.batch_var.get(),
            "compile": app.compile_var.get(),
            "thinking": app.thinking_var.get(),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(CONFIG_FILE)
    except OSError:
        pass


def load_state(app):
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    proj = str(data.get("project", "")).strip().strip('"')
    if proj and os.path.isfile(proj):
        app.proj_var.set(os.path.normpath(proj))
    model = str(data.get("model", ""))
    if model and model in app.model_combo.cget("values"):
        app.model_var.set(model)
    if data.get("extra_open") is False:
        app._toggle_extra()
    for var, key in ((app.open_npp_var, "open_npp"),
                     (app.batch_var, "batch"),
                     (app.compile_var, "compile"),
                     (app.thinking_var, "thinking")):
        if isinstance(data.get(key), bool):
            var.set(data[key])


def find_notepadpp():
    candidates = [
        r"C:\Program Files\Notepad++\notepad++.exe",
        r"C:\Program Files (x86)\Notepad++\notepad++.exe",
        r"C:\Program Files\Notepad++\notepad++.exe",
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return shutil.which("notepad++.exe") or shutil.which("notepad++")


class ReviewApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Локальное код-ревью (LM Studio)")
        self.geometry("820x660")
        self.minsize(640, 520)

        self.proc = None
        self.out_path = None
        self.desc_file = None
        self.prompt_file = None
        self.q = queue.Queue()
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        load_state(self)
        threading.Thread(target=self._fetch_models, daemon=True).start()

    def _build_ui(self):
        root = ttk.Frame(self, padding=12)
        root.pack(fill="both", expand=True)

        pw = ttk.Panedwindow(root, orient="horizontal")
        pw.pack(fill="both", expand=True)

        left = ttk.Frame(pw, padding=(0, 0, 12, 0))
        left.columnconfigure(0, weight=1)
        left.rowconfigure(3, weight=1)
        left.rowconfigure(5, weight=2)
        pw.add(left, weight=3)

        right = ttk.Frame(pw)
        right.columnconfigure(0, weight=1)
        right.rowconfigure(0, weight=1)
        pw.add(right, weight=2)

        ttk.Label(left, text="Проект или солюшен (.sln/.slnx/.csproj):").grid(
            row=0, column=0, sticky="w")
        proj_row = ttk.Frame(left)
        proj_row.grid(row=1, column=0, sticky="ew", pady=(2, 8))
        proj_row.columnconfigure(0, weight=1)
        self.proj_var = tk.StringVar()
        ttk.Entry(proj_row, textvariable=self.proj_var).grid(
            row=0, column=0, sticky="ew")
        ttk.Button(proj_row, text="Обзор…", command=self._browse).grid(
            row=0, column=1, padx=(6, 0))

        ttk.Label(left, text="Что сделано в этих изменениях (описание):").grid(
            row=2, column=0, sticky="nw")
        self.desc_text = tk.Text(left, height=6, wrap="word", undo=True)
        self.desc_text.grid(row=3, column=0, sticky="nsew", pady=(2, 8))

        opts = ttk.Frame(left)
        opts.grid(row=4, column=0, sticky="ew", pady=(0, 8))
        opts.columnconfigure(1, weight=1)
        ttk.Label(opts, text="Модель:").grid(row=0, column=0, sticky="w")
        self.model_var = tk.StringVar(value=AUTO_MODEL_LABEL)
        self.model_combo = ttk.Combobox(
            opts, textvariable=self.model_var, values=[AUTO_MODEL_LABEL],
            width=30)
        self.model_combo.grid(row=0, column=1, sticky="w", padx=(6, 0))

        self.extra_toggle = ttk.Label(opts, text="\u25BE Дополнительные настройки",
                                      cursor="hand2")
        self.extra_toggle.grid(row=1, column=0, columnspan=2, sticky="w",
                               pady=(6, 0))
        self.extra_toggle.bind("<Button-1>", lambda e: self._toggle_extra())

        self.extra = ttk.Frame(opts)
        self.extra.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        self.extra.columnconfigure(1, weight=1)
        self.open_npp_var = tk.BooleanVar(value=True)
        self.batch_var = tk.BooleanVar(value=False)
        self.compile_var = tk.BooleanVar(value=True)
        self.thinking_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(self.extra, text="Открыть отчёт в Notepad++",
                        variable=self.open_npp_var).grid(
            row=0, column=0, sticky="w")
        ttk.Checkbutton(self.extra, text="Все файлы одним промптом (batch)",
                        variable=self.batch_var).grid(
            row=0, column=1, sticky="w", padx=(12, 0))
        ttk.Checkbutton(self.extra, text="Компилировать проект",
                        variable=self.compile_var).grid(
            row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Checkbutton(self.extra, text="Reasoning (thinking)",
                        variable=self.thinking_var).grid(
            row=1, column=1, sticky="w", padx=(12, 0), pady=(6, 0))

        btns = ttk.Frame(left)
        btns.grid(row=5, column=0, sticky="new")
        self.run_btn = ttk.Button(btns, text="Запуск", command=self._run)
        self.run_btn.pack(side="left")
        self.stop_btn = ttk.Button(btns, text="Стоп", command=self._stop, state="disabled")
        self.stop_btn.pack(side="left", padx=(6, 0))
        self.status_var = tk.StringVar(value="Готово к запуску")
        ttk.Label(btns, textvariable=self.status_var).pack(side="left", padx=(12, 0))

        log_frame = ttk.LabelFrame(left, text="Журнал", padding=4)
        log_frame.grid(row=5, column=0, sticky="nsew", pady=(40, 0))
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log = tk.Text(log_frame, state="disabled", wrap="word", height=10)
        self.log.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(log_frame, command=self.log.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=sb.set)


        prompt_frame = ttk.LabelFrame(right, text="Промпт (отправляется в модель)",
                                      padding=4)
        prompt_frame.grid(row=0, column=0, sticky="nsew")
        prompt_frame.columnconfigure(0, weight=1)
        prompt_frame.rowconfigure(0, weight=1)
        self.prompt_text = tk.Text(prompt_frame, state="disabled", wrap="none",
                                   font=("Consolas", 9))
        self.prompt_text.grid(row=0, column=0, sticky="nsew")
        psb_y = ttk.Scrollbar(prompt_frame, command=self.prompt_text.yview)
        psb_y.grid(row=0, column=1, sticky="ns")
        psb_x = ttk.Scrollbar(prompt_frame, orient="horizontal",
                              command=self.prompt_text.xview)
        psb_x.grid(row=1, column=0, sticky="ew")
        self.prompt_text.configure(yscrollcommand=psb_y.set,
                                   xscrollcommand=psb_x.set)
        self.prompt_text.insert("1.0", "Промпт появится после запуска ревью.")
        self._prompt_last = None

    def _browse(self):
        initial = os.path.dirname(self.proj_var.get()) or str(Path.home())
        chosen = filedialog.askopenfilename(
            initialdir=initial, title="Выберите проект или солюшен",
            filetypes=[("Проекты/солюшены",
                        "*.sln *.slnx *.csproj *.vbproj *.fsproj *.vcxproj *.sqlproj *.proj"),
                       ("Все файлы", "*.*")])
        if chosen:
            self.proj_var.set(os.path.normpath(chosen))
            save_state(self)

    def _fetch_models(self):
        try:
            with urlopen(LM_STUDIO_BASE + "/models", timeout=5) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
            models = [m.get("id", "") for m in data.get("data", [])]
            models = [m for m in models if m and "embed" not in m.lower()]
        except Exception:
            models = []
        self.after(0, lambda: self._set_models(models))

    def _set_models(self, models):
        values = [AUTO_MODEL_LABEL] + models
        self.model_combo.configure(values=values)
        if self.model_var.get() not in values:
            self.model_var.set(AUTO_MODEL_LABEL)

    def _toggle_extra(self):
        if bool(self.extra.winfo_manager()):
            self.extra.grid_remove()
            self.extra_toggle.configure(text="\u25B8 Дополнительные настройки")
        else:
            self.extra.grid()
            self.extra_toggle.configure(text="\u25BE Дополнительные настройки")

    def _append(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _set_running(self, running):
        self.run_btn.configure(state="disabled" if running else "normal")
        self.stop_btn.configure(state="normal" if running else "disabled")

    def _run(self):
        if self.proc is not None and self.proc.poll() is None:
            return
        proj = self.proj_var.get().strip().strip('"')
        if not proj or not os.path.isfile(proj):
            messagebox.showerror("Ошибка",
                                 "Укажите файл проекта/солюшена (.sln/.slnx/.csproj…).")
            return
        target = os.path.normpath(proj)
        if not AGENT.is_file():
            messagebox.showerror("Ошибка", f"Не найден {AGENT}")
            return
        save_state(self)

        description = self.desc_text.get("1.0", "end").strip()
        self.desc_file = None
        if description:
            fd, self.desc_file = tempfile.mkstemp(suffix=".txt", prefix="review_desc_")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(description)

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        reviews_dir = SCRIPT_DIR / "reviews"
        reviews_dir.mkdir(parents=True, exist_ok=True)
        self.out_path = reviews_dir / f"code_review_{stamp}.md"

        fd, self.prompt_file = tempfile.mkstemp(suffix=".txt", prefix="review_prompt_")
        os.close(fd)
        self._prompt_last = None
        self.prompt_text.configure(state="normal")
        self.prompt_text.delete("1.0", "end")
        self.prompt_text.configure(state="disabled")

        cmd = [sys.executable, str(AGENT), target, "--out", str(self.out_path)]
        cmd += ["--prompt-file", self.prompt_file]
        model = self.model_var.get().strip()
        if model and model != AUTO_MODEL_LABEL:
            cmd += ["--model", model]
        if self.desc_file:
            cmd += ["--description-file", self.desc_file]
        if self.batch_var.get():
            cmd += ["--batch"]
        if not self.compile_var.get():
            cmd += ["--no-compile"]
        if not self.thinking_var.get():
            cmd += ["--no-thinking"]

        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        self._append(f"[GUI] Запуск: {' '.join(cmd)}")
        self.status_var.set("Выполняется…")

        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self.proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            cwd=str(SCRIPT_DIR), creationflags=creationflags)

        self._set_running(True)
        threading.Thread(target=self._reader, daemon=True).start()
        self.after(100, self._pump)

    def _reader(self):
        try:
            for line in self.proc.stdout:
                self.q.put(("log", line.rstrip("\n")))
        finally:
            try:
                code = self.proc.wait()
            except Exception:
                code = -1
            self.q.put(("done", code))

    def _refresh_prompt(self):
        if not self.prompt_file or not os.path.isfile(self.prompt_file):
            return
        try:
            with open(self.prompt_file, "r", encoding="utf-8") as f:
                text = f.read()
        except OSError:
            return
        if text == self._prompt_last:
            return
        self._prompt_last = text
        self.prompt_text.configure(state="normal")
        self.prompt_text.delete("1.0", "end")
        self.prompt_text.insert("1.0", text)
        self.prompt_text.see("1.0")
        self.prompt_text.configure(state="disabled")

    def _pump(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self._append(payload)
                elif kind == "done":
                    self._finish(payload)
                    return
        except queue.Empty:
            pass
        self._refresh_prompt()
        if self.proc is not None and self.proc.poll() is None:
            self.after(100, self._pump)

    def _finish(self, code):
        self._set_running(False)
        self._refresh_prompt()
        if self.desc_file and os.path.isfile(self.desc_file):
            try:
                os.remove(self.desc_file)
            except OSError:
                pass
            self.desc_file = None
        if self.prompt_file and os.path.isfile(self.prompt_file):
            try:
                os.remove(self.prompt_file)
            except OSError:
                pass
            self.prompt_file = None

        if code == 0 and self.out_path and self.out_path.is_file():
            self.status_var.set(f"Готово: {self.out_path.name}")
            self._append(f"[GUI] Отчёт готов: {self.out_path}")
            if self.open_npp_var.get():
                where = self._open_in_editor(self.out_path)
                self._append(f"[GUI] Отчёт открыт: {where}")
        elif code == 0:
            self.status_var.set("Готово (изменений не найдено)")
            self._append("[GUI] Изменений не найдено — отчёт не создан.")
        else:
            self.status_var.set(f"Ошибка (код {code})")
            self._append(f"[GUI] Процесс завершился с кодом {code}.")
        self.proc = None

    def _open_in_editor(self, path):
        npp = find_notepadpp()
        try:
            if npp:
                subprocess.Popen([npp, str(path)])
                return "Notepad++"
            if os.name == "nt":
                os.startfile(str(path))
                return "программе по умолчанию"
            subprocess.Popen(["xdg-open", str(path)])
            return "xdg-open"
        except Exception as e:
            return f"не удалось открыть ({e})"

    def _stop(self):
        if self.proc is not None and self.proc.poll() is None:
            self._append("[GUI] Остановка процесса…")
            try:
                self.proc.terminate()
            except Exception:
                pass

    def _on_close(self):
        save_state(self)
        if self.proc is not None and self.proc.poll() is None:
            if not messagebox.askyesno("Выход", "Ревью ещё выполняется. Прервать и выйти?"):
                return
            try:
                self.proc.terminate()
            except Exception:
                pass
        if self.desc_file and os.path.isfile(self.desc_file):
            try:
                os.remove(self.desc_file)
            except OSError:
                pass
        if self.prompt_file and os.path.isfile(self.prompt_file):
            try:
                os.remove(self.prompt_file)
            except OSError:
                pass
        self.destroy()


if __name__ == "__main__":
    ReviewApp().mainloop()
