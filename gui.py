"""
gui.py — Job Scraper Control Panel
Run with: python gui.py
Requires job_scraper.py and config.json in the same folder.
"""

import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
import json
import os
import sys
import threading
import subprocess
import time
import webbrowser
from datetime import datetime

CONFIG_FILE  = "config.json"
SEEN_FILE    = "seen_jobs.json"
JOBS_FILE    = "jobs.json"
SCRAPER_FILE = "job_scraper.py"


def get_python() -> str:
    import shutil
    if not getattr(sys, "frozen", False):
        return sys.executable
    found = shutil.which("python") or shutil.which("python3")
    if found:
        return found
    exe_dir = os.path.dirname(sys.executable)
    for name in ("python.exe", "python3.exe"):
        candidate = os.path.join(exe_dir, name)
        if os.path.isfile(candidate):
            return candidate
    for name in ("python.exe", "python3.exe"):
        candidate = shutil.which(name)
        if candidate:
            return candidate
    return "python"


SITE_LABELS = {
    "heartland": "Heartland Bank",
    "mtf":       "MTF Finance",
    "avanti":    "Avanti Finance",
    "kiwibank":  "Kiwibank",
    "bnz":       "BNZ",
    "anz":       "ANZ",
    "westpac":   "Westpac",
    "asb":       "ASB",
}

BG       = "#1e1e2e"
BG2      = "#2a2a3e"
BG3      = "#313145"
ACCENT   = "#7c6af7"
ACCENT2  = "#5a4fcf"
GREEN    = "#50fa7b"
RED      = "#ff5555"
YELLOW   = "#f1fa8c"
CYAN     = "#8be9fd"
TEXT     = "#cdd6f4"
TEXT_DIM = "#6c7086"
BORDER   = "#45475a"


def load_config() -> dict:
    default = {
        "keywords":         ["analyst", "developer", "finance"],
        "discord_webhook":  "",
        "interval_minutes": 30,
        "fuzzy_threshold":  80,
        "location":         "Auckland",
        "sites":            {k: True for k in SITE_LABELS},
    }
    if not os.path.exists(CONFIG_FILE):
        return default
    try:
        with open(CONFIG_FILE) as f:
            data = json.load(f)
        for k, v in default.items():
            if k not in data:
                data[k] = v
        for site in SITE_LABELS:
            if site not in data["sites"]:
                data["sites"][site] = True
        return data
    except Exception:
        return default


def save_config(cfg: dict):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)


class JobsWindow(tk.Toplevel):
    """Pop-up window that shows all jobs currently in jobs.json."""

    COLS = ("Site", "Title", "Location", "Link")

    def __init__(self, master):
        super().__init__(master)
        self.title("Current Job Listings")
        self.geometry("980x560")
        self.minsize(700, 380)
        self.configure(bg=BG)
        self.resizable(True, True)
        self._build_ui()
        self._load_jobs()

    # ── UI ──────────────────────────────────────────────────────────────────

    def _build_ui(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        # ── Toolbar ────────────────────────────────────────────────────────
        toolbar = tk.Frame(self, bg=BG2, pady=6, padx=10)
        toolbar.grid(row=0, column=0, sticky="ew")
        toolbar.columnconfigure(2, weight=1)

        self._btn(toolbar, "⟳  Refresh", GREEN,  self._load_jobs).grid(row=0, column=0, padx=(0,6))
        self._btn(toolbar, "🌐  Open",    ACCENT, self._open_link).grid(row=0, column=1, padx=(0,6))

        # Filter
        tk.Label(toolbar, text="Filter:", bg=BG2, fg=TEXT_DIM,
                 font=("Segoe UI", 9)).grid(row=0, column=2, sticky="e", padx=(0,4))
        self._filter_var = tk.StringVar()
        self._filter_var.trace_add("write", lambda *_: self._apply_filter())
        fe = tk.Entry(toolbar, textvariable=self._filter_var,
                      bg=BG3, fg=TEXT, insertbackground=TEXT,
                      relief="flat", font=("Segoe UI", 10), width=24)
        fe.configure(highlightbackground=BORDER, highlightthickness=1,
                     highlightcolor=ACCENT)
        fe.grid(row=0, column=3, padx=(0,10))

        self._count_lbl = tk.Label(toolbar, text="", bg=BG2, fg=TEXT_DIM,
                                   font=("Segoe UI", 9))
        self._count_lbl.grid(row=0, column=4, padx=(0,4))

        # ── Treeview ───────────────────────────────────────────────────────
        frame = tk.Frame(self, bg=BG)
        frame.grid(row=1, column=0, sticky="nsew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)

        style = ttk.Style(self)
        style.theme_use("default")
        style.configure("Jobs.Treeview",
                        background=BG2, foreground=TEXT,
                        fieldbackground=BG2, rowheight=26,
                        font=("Segoe UI", 9))
        style.configure("Jobs.Treeview.Heading",
                        background=BG3, foreground=ACCENT,
                        font=("Segoe UI", 9, "bold"), relief="flat")
        style.map("Jobs.Treeview",
                  background=[("selected", ACCENT2)],
                  foreground=[("selected", "white")])

        self._tree = ttk.Treeview(
            frame, columns=self.COLS, show="headings",
            style="Jobs.Treeview", selectmode="browse")

        col_widths = {"Site": 120, "Title": 320, "Location": 160, "Link": 340}
        for col in self.COLS:
            self._tree.heading(col, text=col,
                               command=lambda c=col: self._sort(c))
            self._tree.column(col, width=col_widths[col], minwidth=80,
                              anchor="w", stretch=(col == "Title"))

        vsb = ttk.Scrollbar(frame, orient="vertical",   command=self._tree.yview)
        hsb = ttk.Scrollbar(frame, orient="horizontal", command=self._tree.xview)
        self._tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        self._tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")

        self._tree.bind("<Double-1>",         self._on_double_click)
        self._tree.bind("<Return>",           self._on_double_click)
        self._tree.tag_configure("odd",  background=BG2)
        self._tree.tag_configure("even", background=BG3)

        # ── Status bar ─────────────────────────────────────────────────────
        self._status = tk.Label(self, text="", bg=BG, fg=TEXT_DIM,
                                font=("Segoe UI", 8), anchor="w", padx=8)
        self._status.grid(row=2, column=0, sticky="ew")

        # Internal state for sorting
        self._sort_col = None
        self._sort_rev = False
        self._all_rows: list[tuple] = []

    # ── Data ────────────────────────────────────────────────────────────────

    def _load_jobs(self):
        self._all_rows.clear()
        if not os.path.exists(JOBS_FILE):
            self._status.config(text="jobs.json not found — run a scan first.")
            self._refresh_tree([])
            return
        try:
            with open(JOBS_FILE, "r", encoding="utf-8") as f:
                jobs = json.load(f)
        except Exception as e:
            self._status.config(text=f"Error reading jobs.json: {e}")
            self._refresh_tree([])
            return

        for j in jobs:
            self._all_rows.append((
                j.get("site",     ""),
                j.get("title",    ""),
                j.get("location", ""),
                j.get("link",     ""),
            ))

        mtime = os.path.getmtime(JOBS_FILE)
        stamp = datetime.fromtimestamp(mtime).strftime("%d %b %Y %H:%M")
        self._status.config(text=f"  jobs.json — last updated {stamp}")
        self._apply_filter()

    def _apply_filter(self):
        q = self._filter_var.get().lower().strip()
        if q:
            rows = [r for r in self._all_rows
                    if any(q in cell.lower() for cell in r)]
        else:
            rows = list(self._all_rows)
        self._refresh_tree(rows)

    def _refresh_tree(self, rows: list):
        self._tree.delete(*self._tree.get_children())
        for i, row in enumerate(rows):
            tag = "even" if i % 2 == 0 else "odd"
            self._tree.insert("", "end", values=row, tags=(tag,))
        total   = len(self._all_rows)
        showing = len(rows)
        self._count_lbl.config(
            text=f"{showing} of {total}" if showing != total else f"{total} job(s)")

    def _sort(self, col: str):
        idx = self.COLS.index(col)
        self._sort_rev = (col == self._sort_col) and not self._sort_rev
        self._sort_col = col
        rows = sorted(self._all_rows, key=lambda r: r[idx].lower(),
                      reverse=self._sort_rev)
        self._all_rows = rows
        self._apply_filter()

    # ── Actions ─────────────────────────────────────────────────────────────

    def _selected_link(self) -> str:
        sel = self._tree.selection()
        if not sel:
            return ""
        return self._tree.item(sel[0], "values")[3]   # Link column

    def _open_link(self):
        link = self._selected_link()
        if link:
            webbrowser.open(link)
        else:
            messagebox.showinfo("No selection", "Select a job row first.",
                                parent=self)

    def _on_double_click(self, event=None):
        link = self._selected_link()
        if link:
            webbrowser.open(link)

    # ── Helper ──────────────────────────────────────────────────────────────

    def _btn(self, parent, text, color, cmd, **kw):
        return tk.Button(
            parent, text=text, bg=BG3, fg=color,
            activebackground=BG2, activeforeground=color,
            relief="flat", bd=0, font=("Segoe UI", 9, "bold"),
            cursor="hand2", padx=10, pady=4, command=cmd, **kw)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Job Scraper — Control Panel")
        self.geometry("900x700")
        self.minsize(800, 580)
        self.configure(bg=BG)
        self.resizable(True, True)

        self.cfg             = load_config()
        self.scraper_process = None
        self.running         = False
        self.next_run_at     = None

        self._build_ui()
        self._populate()
        self._tick()

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        # Header
        hdr = tk.Frame(self, bg=ACCENT2, pady=10)
        hdr.grid(row=0, column=0, sticky="ew")
        tk.Label(hdr, text="🔍  NZ Job Scraper", font=("Segoe UI", 16, "bold"),
                 bg=ACCENT2, fg="white").pack(side="left", padx=18)
        self.status_dot = tk.Label(hdr, text="⬤  Stopped", font=("Segoe UI", 10),
                                   bg=ACCENT2, fg=RED)
        self.status_dot.pack(side="right", padx=18)

        # Paned layout
        pane = tk.PanedWindow(self, orient="horizontal", bg=BG,
                              sashrelief="flat", sashwidth=6)
        pane.grid(row=1, column=0, sticky="nsew")

        left  = tk.Frame(pane, bg=BG, padx=14, pady=12)
        right = tk.Frame(pane, bg=BG)
        pane.add(left,  minsize=330)
        pane.add(right, minsize=330)
        pane.paneconfigure(left,  stretch="never")
        pane.paneconfigure(right, stretch="always")

        self._build_left(left)
        self._build_right(right)

        # Footer
        foot = tk.Frame(self, bg=BG2, pady=8, padx=14)
        foot.grid(row=2, column=0, sticky="ew")
        foot.columnconfigure(4, weight=1)

        self.btn_start = self._btn(foot, "▶  Start",       GREEN,    self._start_scraper)
        self.btn_start.grid(row=0, column=0, padx=(0, 6))
        self.btn_stop  = self._btn(foot, "■  Stop",        RED,      self._stop_scraper, state="disabled")
        self.btn_stop.grid(row=0, column=1, padx=(0, 6))
        self._btn(foot, "⟳  Run Now",    YELLOW,   self._run_now   ).grid(row=0, column=2, padx=(0, 6))
        self._btn(foot, "📋  View Jobs",  CYAN,     self._view_jobs  ).grid(row=0, column=3, padx=(0, 6))
        self._btn(foot, "🗑  Reset Seen", TEXT_DIM, self._reset_seen).grid(row=0, column=4, sticky="e")

        self.countdown_lbl = tk.Label(foot, text="", font=("Segoe UI", 9),
                                      bg=BG2, fg=TEXT_DIM)
        self.countdown_lbl.grid(row=0, column=5, padx=(12, 0), sticky="e")

    def _build_left(self, parent):
        parent.columnconfigure(0, weight=1)

        # ── Keywords ──────────────────────────────────────────────────────────
        self._section(parent, "Keywords", row=0)
        kw_frame = self._card(parent, row=1)
        kw_frame.columnconfigure(0, weight=1)

        self.kw_listbox = tk.Listbox(
            kw_frame, bg=BG2, fg=TEXT, selectbackground=ACCENT,
            selectforeground="white", font=("Segoe UI", 10),
            bd=0, relief="flat", height=5, activestyle="none", exportselection=False)
        self.kw_listbox.grid(row=0, column=0, columnspan=3, sticky="ew", padx=6, pady=6)

        kw_row = tk.Frame(kw_frame, bg=BG2)
        kw_row.grid(row=1, column=0, sticky="ew", padx=6, pady=(0, 6))
        kw_row.columnconfigure(0, weight=1)
        self.kw_entry = self._entry(kw_row, placeholder="Add keyword…")
        self.kw_entry.grid(row=0, column=0, sticky="ew")
        self.kw_entry.bind("<Return>", lambda e: self._add_keyword())
        self._btn(kw_row, "+", ACCENT,   self._add_keyword,   width=3).grid(row=0, column=1, padx=(4,0))
        self._btn(kw_row, "−", TEXT_DIM, self._remove_keyword, width=3).grid(row=0, column=2, padx=(4,0))

        # ── Sites ─────────────────────────────────────────────────────────────
        self._section(parent, "Sites", row=2)
        sites_frame = self._card(parent, row=3)
        sites_frame.columnconfigure(0, weight=1)
        sites_frame.columnconfigure(1, weight=1)

        self.site_vars = {}
        for i, (key, label) in enumerate(SITE_LABELS.items()):
            var = tk.BooleanVar()
            self.site_vars[key] = var
            tk.Checkbutton(
                sites_frame, text=label, variable=var,
                bg=BG2, fg=TEXT, selectcolor=BG3,
                activebackground=BG2, activeforeground=TEXT,
                font=("Segoe UI", 10), bd=0, cursor="hand2",
                highlightthickness=0
            ).grid(row=i // 2, column=i % 2, sticky="w", padx=10, pady=3)

        # ── Settings ──────────────────────────────────────────────────────────
        self._section(parent, "Settings", row=4)
        sf = self._card(parent, row=5)
        sf.columnconfigure(1, weight=1)

        rows = [
            ("Location",        "location_entry",  "Auckland"),
            ("Discord Webhook", "webhook_entry",   ""),
            ("Interval (mins)", "interval_entry",  "30"),
            ("Fuzzy Threshold", "threshold_entry", "80"),
        ]
        for i, (lbl, attr, _) in enumerate(rows):
            pad_top = 6 if i == 0 else 3
            tk.Label(sf, text=lbl, bg=BG2, fg=TEXT_DIM,
                     font=("Segoe UI", 9)).grid(row=i, column=0, sticky="w",
                                                padx=10, pady=(pad_top, 3))
            e = self._entry(sf)
            e.grid(row=i, column=1, sticky="ew", padx=(0, 10), pady=(pad_top, 3))
            setattr(self, attr, e)

        # Webhook visibility toggle
        self.webhook_entry.config(show="•")
        self._show_webhook = False
        tk.Button(sf, text="👁", bg=BG2, fg=TEXT_DIM, relief="flat", bd=0,
                  cursor="hand2", command=self._toggle_webhook
                  ).grid(row=1, column=2, padx=(0, 6))

        # Location hint label
        self.loc_hint = tk.Label(sf, text="e.g. Auckland, Wellington, Christchurch",
                                 bg=BG2, fg=TEXT_DIM, font=("Segoe UI", 8))
        self.loc_hint.grid(row=0, column=2, padx=(0, 8), sticky="w")

        self._btn(sf, "💾  Save Settings", ACCENT, self._save_settings
                  ).grid(row=len(rows), column=0, columnspan=3,
                         sticky="ew", padx=10, pady=(6, 10))

    def _build_right(self, parent):
        parent.rowconfigure(1, weight=1)
        parent.columnconfigure(0, weight=1)

        log_hdr = tk.Frame(parent, bg=BG2, pady=6)
        log_hdr.grid(row=0, column=0, sticky="ew")
        log_hdr.columnconfigure(0, weight=1)
        tk.Label(log_hdr, text="  Log Output", font=("Segoe UI", 10, "bold"),
                 bg=BG2, fg=TEXT).grid(row=0, column=0, sticky="w")
        self._btn(log_hdr, "Clear", TEXT_DIM, self._clear_log,
                  pady=0).grid(row=0, column=1, padx=8)

        self.log = scrolledtext.ScrolledText(
            parent, bg="#11111b", fg=TEXT, insertbackground=TEXT,
            font=("Consolas", 9), relief="flat", bd=0,
            wrap="word", state="disabled")
        self.log.grid(row=1, column=0, sticky="nsew")

        self.log.tag_config("info",   foreground=TEXT)
        self.log.tag_config("good",   foreground=GREEN)
        self.log.tag_config("warn",   foreground=YELLOW)
        self.log.tag_config("error",  foreground=RED)
        self.log.tag_config("accent", foreground=ACCENT)
        self.log.tag_config("dim",    foreground=TEXT_DIM)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _card(self, parent, row, pady_bottom=10):
        f = tk.Frame(parent, bg=BG2, bd=0,
                     highlightbackground=BORDER, highlightthickness=1)
        f.grid(row=row, column=0, sticky="ew", pady=(0, pady_bottom))
        return f

    def _btn(self, parent, text, color, cmd, width=None, state="normal", pady=4):
        kw = dict(text=text, bg=BG3, fg=color, activebackground=BG2,
                  activeforeground=color, relief="flat", bd=0,
                  font=("Segoe UI", 9, "bold"), cursor="hand2",
                  padx=10, pady=pady, command=cmd, state=state)
        if width:
            kw["width"] = width
        return tk.Button(parent, **kw)

    def _entry(self, parent, placeholder=""):
        e = tk.Entry(parent, bg=BG3, fg=TEXT, insertbackground=TEXT,
                     relief="flat", bd=0, font=("Segoe UI", 10))
        e.configure(highlightbackground=BORDER, highlightthickness=1,
                    highlightcolor=ACCENT)
        if placeholder:
            e.insert(0, placeholder)
            e.config(fg=TEXT_DIM)
            e.bind("<FocusIn>",  lambda ev, _e=e, _p=placeholder: self._ph_in(_e, _p))
            e.bind("<FocusOut>", lambda ev, _e=e, _p=placeholder: self._ph_out(_e, _p))
        return e

    @staticmethod
    def _ph_in(e, ph):
        if e.get() == ph:
            e.delete(0, "end"); e.config(fg=TEXT)

    @staticmethod
    def _ph_out(e, ph):
        if not e.get():
            e.insert(0, ph); e.config(fg=TEXT_DIM)

    def _section(self, parent, title, row):
        tk.Label(parent, text=title.upper(), font=("Segoe UI", 8, "bold"),
                 bg=BG, fg=TEXT_DIM).grid(row=row, column=0, sticky="w", pady=(8, 2))

    def _log(self, msg: str, tag="info"):
        ts = datetime.now().strftime("%H:%M:%S")
        self.log.config(state="normal")
        self.log.insert("end", f"[{ts}] ", "dim")
        self.log.insert("end", msg + "\n", tag)
        self.log.see("end")
        self.log.config(state="disabled")

    def _clear_log(self):
        self.log.config(state="normal")
        self.log.delete("1.0", "end")
        self.log.config(state="disabled")

    # ── Populate ──────────────────────────────────────────────────────────────

    def _populate(self):
        self.kw_listbox.delete(0, "end")
        for kw in self.cfg.get("keywords", []):
            self.kw_listbox.insert("end", kw)

        for key, var in self.site_vars.items():
            var.set(self.cfg["sites"].get(key, True))

        self.location_entry.delete(0, "end")
        self.location_entry.insert(0, self.cfg.get("location", "Auckland"))
        self.location_entry.config(fg=TEXT)

        self.webhook_entry.delete(0, "end")
        self.webhook_entry.insert(0, self.cfg.get("discord_webhook", ""))
        self.webhook_entry.config(fg=TEXT)

        self.interval_entry.delete(0, "end")
        self.interval_entry.insert(0, str(self.cfg.get("interval_minutes", 30)))
        self.interval_entry.config(fg=TEXT)

        self.threshold_entry.delete(0, "end")
        self.threshold_entry.insert(0, str(self.cfg.get("fuzzy_threshold", 80)))
        self.threshold_entry.config(fg=TEXT)

    # ── Keywords ──────────────────────────────────────────────────────────────

    def _add_keyword(self):
        kw = self.kw_entry.get().strip().lower()
        if not kw or kw == "add keyword…":
            return
        if kw in list(self.kw_listbox.get(0, "end")):
            self._log(f"Keyword '{kw}' already exists.", "warn"); return
        self.kw_listbox.insert("end", kw)
        self.kw_entry.delete(0, "end")
        self._log(f"Added keyword: {kw}", "good")

    def _remove_keyword(self):
        sel = self.kw_listbox.curselection()
        if not sel:
            self._log("Select a keyword to remove.", "warn"); return
        kw = self.kw_listbox.get(sel[0])
        self.kw_listbox.delete(sel[0])
        self._log(f"Removed keyword: {kw}", "warn")

    # ── Settings ──────────────────────────────────────────────────────────────

    def _save_settings(self):
        location = self.location_entry.get().strip()
        if not location or location == "Auckland":
            location = "Auckland"
        try:
            interval  = int(self.interval_entry.get().strip())
            threshold = int(self.threshold_entry.get().strip())
            assert 1 <= interval <= 1440, "Interval must be 1–1440 minutes"
            assert 0 <= threshold <= 100, "Threshold must be 0–100"
            assert location,              "Location cannot be empty"
        except (ValueError, AssertionError) as e:
            messagebox.showerror("Invalid input", str(e)); return

        self.cfg.update({
            "keywords":         list(self.kw_listbox.get(0, "end")),
            "discord_webhook":  self.webhook_entry.get().strip(),
            "interval_minutes": interval,
            "fuzzy_threshold":  threshold,
            "location":         location,
            "sites":            {k: v.get() for k, v in self.site_vars.items()},
        })
        save_config(self.cfg)
        self._log(f"Settings saved. Location: {location} ✓", "good")
        if self.running:
            self._log("Restart the scraper to apply new settings.", "warn")

    def _toggle_webhook(self):
        self._show_webhook = not self._show_webhook
        self.webhook_entry.config(show="" if self._show_webhook else "•")

    # ── View Jobs popup ───────────────────────────────────────────────────────

    def _view_jobs(self):
        win = JobsWindow(self)
        win.grab_set()
        win.focus_force()

    # ── Scraper control ───────────────────────────────────────────────────────

    def _start_scraper(self):
        if self.running:
            return
        self._save_settings()
        if not os.path.exists(SCRAPER_FILE):
            messagebox.showerror("Error", f"{SCRAPER_FILE} not found."); return

        self.running = True
        self.btn_start.config(state="disabled")
        self.btn_stop.config(state="normal")
        self.status_dot.config(text="⬤  Running", fg=GREEN)
        self._log("Scraper started.", "good")
        threading.Thread(target=self._scraper_loop, daemon=True).start()

    def _stop_scraper(self):
        self.running = False
        if self.scraper_process and self.scraper_process.poll() is None:
            self.scraper_process.terminate()
            self.scraper_process = None
        self.btn_start.config(state="normal")
        self.btn_stop.config(state="disabled")
        self.status_dot.config(text="⬤  Stopped", fg=RED)
        self.next_run_at = None
        self._log("Scraper stopped.", "warn")

    def _run_now(self):
        if not os.path.exists(SCRAPER_FILE):
            messagebox.showerror("Error", f"{SCRAPER_FILE} not found."); return
        self._log("Manual scan triggered…", "accent")
        threading.Thread(target=self._run_once_thread, daemon=True).start()

    def _run_once_thread(self):
        self._stream_process([get_python(), SCRAPER_FILE, "--once"])

    def _scraper_loop(self):
        interval_secs = self.cfg.get("interval_minutes", 30) * 60
        while self.running:
            self.after(0, self._log, "─── Scan started ───", "accent")
            self._stream_process([get_python(), SCRAPER_FILE, "--once"])
            if not self.running:
                break
            self.next_run_at = time.time() + interval_secs
            self.after(0, self._log, f"Next scan in {interval_secs // 60} min.", "dim")
            for _ in range(interval_secs * 2):
                if not self.running:
                    break
                time.sleep(0.5)
        self.after(0, self._on_loop_done)

    def _stream_process(self, cmd: list):
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, bufsize=1)
            self.scraper_process = proc
            for line in proc.stdout:
                line = line.rstrip()
                if not line:
                    continue
                tag = ("error" if any(x in line for x in ["[ERROR]", "[HTTP", "[SKIP]"])
                       else "good"  if any(x in line for x in ["found", "new job", "✓"])
                       else "warn"  if "[WARNING]" in line
                       else "info")
                self.after(0, self._log, line, tag)
            proc.wait()
        except Exception as e:
            self.after(0, self._log, f"Process error: {e}", "error")

    def _on_loop_done(self):
        self.running = False
        self.next_run_at = None
        self.btn_start.config(state="normal")
        self.btn_stop.config(state="disabled")
        self.status_dot.config(text="⬤  Stopped", fg=RED)

    def _reset_seen(self):
        if not messagebox.askyesno("Reset seen jobs",
                                   "Clear seen_jobs.json?\nAll current jobs will be "
                                   "re-sent to Discord on the next scan."):
            return
        if os.path.exists(SEEN_FILE):
            os.remove(SEEN_FILE)
            self._log("seen_jobs.json cleared — all jobs will be re-announced.", "warn")
        else:
            self._log("Nothing to clear (seen_jobs.json doesn't exist).", "dim")

    # ── Countdown ─────────────────────────────────────────────────────────────

    def _tick(self):
        if self.next_run_at and self.running:
            rem  = max(0, int(self.next_run_at - time.time()))
            m, s = divmod(rem, 60)
            self.countdown_lbl.config(text=f"Next scan in {m:02d}:{s:02d}")
        else:
            self.countdown_lbl.config(text="")
        self.after(1000, self._tick)


if __name__ == "__main__":
    App().mainloop()