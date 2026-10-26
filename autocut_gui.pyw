#!/usr/bin/env python3
"""
AutoCut GUI - point-and-click front end for autocut.py.

Double-click this file (Windows) or run:  python autocut_gui.pyw
Needs autocut.py in the same folder, Python 3.8+ and ffmpeg on PATH.
Optional: pip install tkinterdnd2  to drag videos straight into the window.
"""
import json
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from dataclasses import asdict
from tkinter import filedialog, messagebox, ttk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import autocut as core  # noqa: E402

try:  # optional drag & drop support
    from tkinterdnd2 import DND_FILES, TkinterDnD
except ImportError:
    TkinterDnD = None

if os.name == "nt":  # crisp text on high-DPI screens
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

CONFIG = os.path.join(os.path.expanduser("~"), ".autocut_settings.json")

# Colours
BG, PANEL, FIELD, BORDER = "#16181d", "#1f2229", "#272b33", "#343944"
TEXT, MUTED, ACCENT, ACCENT_HI = "#e8eaee", "#9aa1ad", "#ff5a36", "#ff7756"
OK, WARN, ERR = "#5ccf8a", "#f2c14e", "#ff6b6b"

QUALITY = {"High (bigger file)": 18, "Normal": 20, "Smaller file": 24}

SLIDERS = [
    # key, label, from, to, step, fmt, hint
    ("min_boring", "Shortest cut", 1.0, 15.0, 0.5, "{:.1f} s",
     "Boring stretches shorter than this are kept"),
    ("pad", "Breathing room", 0.0, 2.0, 0.1, "{:.1f} s",
     "Footage kept either side of each cut so it doesn't feel abrupt"),
    ("quiet_db", "Quiet threshold", 4.0, 30.0, 1.0, "{:.0f} dB",
     "Lower = more counts as quiet = more cuts (lower it for games with music)"),
    ("motion", "Still threshold", 0.002, 0.06, 0.001, "{:.3f}",
     "Higher = more counts as still = more cuts"),
]


def load_config():
    try:
        with open(CONFIG, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_config(data):
    try:
        with open(CONFIG, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except OSError:
        pass


def open_path(path):
    if os.name == "nt":
        os.startfile(path)  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


class App:
    def __init__(self, root, initial_files=()):
        self.root = root
        self.events = queue.Queue()
        self.job = None
        self.worker = None
        self.items = {}  # tree iid -> {"path":..., "result":..., "error":...}
        cfg = load_config()

        root.title(f"AutoCut {core.__version__}")
        root.configure(bg=BG)
        root.minsize(800, 660)
        root.geometry(cfg.get("geometry", "900x800"))
        self._style()

        self.preset = tk.StringVar(value=cfg.get("preset", next(iter(core.PRESETS))))
        self.vars = {k: tk.DoubleVar() for k, *_ in SLIDERS}
        self.nvenc = tk.BooleanVar(value=cfg.get("nvenc", False))
        self.quality = tk.StringVar(value=cfg.get("quality", "Normal"))
        self.out_mode = tk.StringVar(value=cfg.get("out_mode", "same"))
        self.out_dir = tk.StringVar(value=cfg.get("out_dir", ""))
        self.show_adv = tk.BooleanVar(value=cfg.get("show_advanced", False))
        self.status = tk.StringVar(value="Add some gameplay videos to get started.")

        self._build()
        self._apply_settings(cfg.get("custom") if self.preset.get() == "Custom"
                             else asdict(core.PRESETS.get(self.preset.get(), core.Settings())))
        self._toggle_adv()
        self._set_running(False)
        for p in initial_files:
            self.add_path(p)

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(100, self._poll)
        root.after(200, self._check_ffmpeg)

    # ---------- look ----------
    def _style(self):
        st = ttk.Style(self.root)
        st.theme_use("clam")
        from tkinter import font as tkfont
        families = set(tkfont.families(self.root))
        family = next((f for f in ("Segoe UI", "SF Pro Text", "Inter", "Helvetica Neue", "Cantarell", "DejaVu Sans")
                       if f in families), "Helvetica")
        base = (family, 10)
        self.font = base
        self.root.option_add("*TCombobox*Listbox.font", base)
        st.configure(".", background=BG, foreground=TEXT, fieldbackground=FIELD, bordercolor=BORDER,
                     lightcolor=BORDER, darkcolor=BORDER, troughcolor=FIELD, focuscolor=ACCENT, font=base)
        st.configure("TFrame", background=BG)
        st.configure("Panel.TFrame", background=PANEL)
        st.configure("TLabel", background=BG, foreground=TEXT)
        st.configure("Panel.TLabel", background=PANEL)
        st.configure("Muted.TLabel", background=PANEL, foreground=MUTED, font=(base[0], 9))
        st.configure("Title.TLabel", background=BG, font=(base[0], 20, "bold"))
        st.configure("Sub.TLabel", background=BG, foreground=MUTED)
        st.configure("Section.TLabel", background=PANEL, font=(base[0], 11, "bold"))
        st.configure("Value.TLabel", background=PANEL, foreground=ACCENT, font=(base[0], 10, "bold"))
        st.configure("TButton", background=FIELD, foreground=TEXT, padding=(12, 6), borderwidth=1)
        st.map("TButton", background=[("disabled", PANEL), ("active", BORDER)],
               foreground=[("disabled", "#5c626d")])
        st.configure("Accent.TButton", background=ACCENT, foreground="#ffffff", font=(base[0], 10, "bold"),
                     bordercolor=ACCENT, padding=(18, 7))
        st.map("Accent.TButton", background=[("disabled", "#6b3a2f"), ("active", ACCENT_HI)],
               foreground=[("disabled", "#c9a59c")])
        st.configure("TCheckbutton", background=PANEL, foreground=TEXT, indicatorbackground=FIELD,
                     indicatorforeground=ACCENT)
        st.map("TCheckbutton", background=[("active", PANEL)], indicatorbackground=[("selected", FIELD)])
        st.configure("TRadiobutton", background=PANEL, foreground=TEXT, indicatorbackground=FIELD,
                     indicatorforeground=ACCENT)
        st.map("TRadiobutton", background=[("active", PANEL)], indicatorbackground=[("selected", FIELD)],
               indicatorforeground=[("selected", ACCENT)])
        st.configure("TCombobox", fieldbackground=FIELD, background=FIELD, foreground=TEXT, arrowcolor=TEXT,
                     padding=4)
        st.map("TCombobox", fieldbackground=[("readonly", FIELD)], foreground=[("readonly", TEXT)],
               selectbackground=[("readonly", FIELD)], selectforeground=[("readonly", TEXT)])
        self.root.option_add("*TCombobox*Listbox.background", FIELD)
        self.root.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        st.configure("TEntry", fieldbackground=FIELD, foreground=TEXT, insertcolor=TEXT, padding=4)
        st.configure("Horizontal.TScale", background=ACCENT, troughcolor=FIELD, bordercolor=PANEL,
                     lightcolor=ACCENT, darkcolor=ACCENT)
        st.configure("Horizontal.TProgressbar", background=ACCENT, troughcolor=FIELD, bordercolor=BORDER,
                     lightcolor=ACCENT, darkcolor=ACCENT, thickness=10)
        st.configure("Treeview", background=FIELD, fieldbackground=FIELD, foreground=TEXT, rowheight=26,
                     borderwidth=0)
        st.map("Treeview", background=[("selected", BORDER)], foreground=[("selected", TEXT)])
        st.configure("Treeview.Heading", background=PANEL, foreground=MUTED, relief="flat",
                     font=(base[0], 9, "bold"))
        st.map("Treeview.Heading", background=[("active", PANEL)])
        st.configure("Vertical.TScrollbar", background=BORDER, troughcolor=FIELD, arrowcolor=MUTED,
                     bordercolor=FIELD, lightcolor=BORDER, darkcolor=BORDER, gripcount=0)

    def _panel(self, parent, title):
        outer = ttk.Frame(parent, style="Panel.TFrame", padding=(14, 10, 14, 12))
        ttk.Label(outer, text=title, style="Section.TLabel").pack(anchor="w", pady=(0, 8))
        return outer

    # ---------- layout ----------
    def _build(self):
        main = ttk.Frame(self.root, padding=16)
        main.pack(fill="both", expand=True)

        head = ttk.Frame(main)
        head.pack(fill="x", pady=(0, 12))
        ttk.Label(head, text="AutoCut", style="Title.TLabel").pack(side="left")
        ttk.Label(head, text="  cuts the quiet, still parts out of gameplay videos",
                  style="Sub.TLabel").pack(side="left", pady=(8, 0))

        # Bottom first: run bar, then settings, so they always stay visible when space is tight.
        bottom = ttk.Frame(main)
        bottom.pack(side="bottom", fill="x", pady=(14, 0))
        self._build_runbar(bottom)
        sets = self._panel(main, "Settings")
        sets.pack(side="bottom", fill="x", pady=(12, 0))
        self._build_settings(sets)

        # Videos
        vids = self._panel(main, "Videos")
        vids.pack(fill="both", expand=True)
        row = ttk.Frame(vids, style="Panel.TFrame")
        row.pack(fill="both", expand=True)
        tf = ttk.Frame(row, style="Panel.TFrame")
        tf.pack(side="left", fill="both", expand=True)
        self.tree = ttk.Treeview(tf, columns=("status", "result"), height=5, selectmode="extended")
        self.tree.heading("#0", text="FILE", anchor="w")
        self.tree.heading("status", text="STATUS", anchor="w")
        self.tree.heading("result", text="RESULT", anchor="w")
        self.tree.column("#0", width=300, stretch=True)
        self.tree.column("status", width=120, stretch=False)
        self.tree.column("result", width=230, stretch=True)
        self.tree.tag_configure("done", foreground=OK)
        self.tree.tag_configure("error", foreground=ERR)
        self.tree.tag_configure("busy", foreground=WARN)
        sb = ttk.Scrollbar(tf, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._show_details())
        self.tree.bind("<Double-1>", lambda e: self._open_selected())
        self.tree.bind("<Delete>", lambda e: self.remove_selected())

        btns = ttk.Frame(row, style="Panel.TFrame")
        btns.pack(side="left", fill="y", padx=(10, 0))
        self.b_add = ttk.Button(btns, text="Add videos…", command=self.browse)
        self.b_remove = ttk.Button(btns, text="Remove", command=self.remove_selected)
        self.b_clear = ttk.Button(btns, text="Clear", command=self.clear)
        for b in (self.b_add, self.b_remove, self.b_clear):
            b.pack(fill="x", pady=(0, 6))
        hint = "Tip: drag videos into this window" if TkinterDnD else "Double-click a finished video to open it"
        ttk.Label(vids, text=hint, style="Muted.TLabel").pack(anchor="w", pady=(6, 0))

        # Details
        det = self._panel(main, "Details")
        det.pack(fill="both", expand=True, pady=(12, 0))
        self.details = tk.Text(det, height=5, bg=FIELD, fg=TEXT, insertbackground=TEXT, relief="flat",
                               font=("Consolas", 9) if os.name == "nt" else ("Courier", 9), wrap="none",
                               highlightthickness=0, padx=8, pady=6)
        self.details.pack(fill="both", expand=True)
        self._details("Select a video to see what was cut.\n\n"
                      "Preview cuts  = only work out the cuts (fast, no new video).\n"
                      "Start         = cut and save a new video. Your original is never changed.")

        if TkinterDnD:
            self.root.drop_target_register(DND_FILES)
            self.root.dnd_bind("<<Drop>>", self._on_drop)

    def _build_settings(self, sets):
        g = ttk.Frame(sets, style="Panel.TFrame")
        g.pack(fill="x")
        g.columnconfigure(1, weight=1)
        ttk.Label(g, text="How much to cut", style="Panel.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 12))
        self.cb_preset = ttk.Combobox(g, textvariable=self.preset, state="readonly", width=28,
                                      values=list(core.PRESETS) + ["Custom"])
        self.cb_preset.grid(row=0, column=1, sticky="w")
        self.cb_preset.bind("<<ComboboxSelected>>", lambda e: self._preset_changed())
        self.cb_adv = ttk.Checkbutton(g, text="Show advanced", variable=self.show_adv, command=self._toggle_adv)
        self.cb_adv.grid(row=0, column=2, sticky="e")

        self.adv = ttk.Frame(sets, style="Panel.TFrame")
        self.adv.columnconfigure(1, weight=1)
        self.value_labels = {}
        self.scales = []
        for r, (key, label, lo, hi, step, fmt, hint) in enumerate(SLIDERS):
            ttk.Label(self.adv, text=label, style="Panel.TLabel").grid(row=r * 2, column=0, sticky="w",
                                                                         padx=(0, 12), pady=(8, 0))
            sc = ttk.Scale(self.adv, from_=lo, to=hi, variable=self.vars[key],
                           command=lambda v, k=key, s=step, f=fmt: self._slider_moved(k, s, f))
            sc.grid(row=r * 2, column=1, sticky="ew", pady=(8, 0))
            self.scales.append(sc)
            vl = ttk.Label(self.adv, style="Value.TLabel", width=9, anchor="e")
            vl.grid(row=r * 2, column=2, sticky="e", padx=(10, 0), pady=(8, 0))
            self.value_labels[key] = (vl, fmt)
            ttk.Label(self.adv, text=hint, style="Muted.TLabel").grid(row=r * 2 + 1, column=1, sticky="w")

        self.misc = ttk.Frame(sets, style="Panel.TFrame")
        self.misc.pack(fill="x", pady=(10, 0))
        self.misc.columnconfigure(3, weight=1)
        ttk.Label(self.misc, text="Save to", style="Panel.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 12))
        self.rb_same = ttk.Radiobutton(self.misc, text="Next to original", value="same", variable=self.out_mode,
                                       command=self._out_changed)
        self.rb_same.grid(row=0, column=1, sticky="w")
        self.rb_dir = ttk.Radiobutton(self.misc, text="Folder:", value="dir", variable=self.out_mode,
                                      command=self._out_changed)
        self.rb_dir.grid(row=0, column=2, sticky="w", padx=(14, 4))
        self.e_dir = ttk.Entry(self.misc, textvariable=self.out_dir)
        self.e_dir.grid(row=0, column=3, sticky="ew")
        self.b_dir = ttk.Button(self.misc, text="Browse…", command=self.pick_dir)
        self.b_dir.grid(row=0, column=4, padx=(6, 0))

        ttk.Label(self.misc, text="Quality", style="Panel.TLabel").grid(row=1, column=0, sticky="w",
                                                                         padx=(0, 12), pady=(10, 0))
        self.cb_quality = ttk.Combobox(self.misc, textvariable=self.quality, state="readonly", width=18,
                                       values=list(QUALITY))
        self.cb_quality.grid(row=1, column=1, sticky="w", pady=(10, 0))
        self.cb_nvenc = ttk.Checkbutton(self.misc, text="Use NVIDIA GPU (much faster)", variable=self.nvenc)
        self.cb_nvenc.grid(row=1, column=2, columnspan=3, sticky="w", padx=(14, 0), pady=(10, 0))
        self._out_changed()

    def _build_runbar(self, main):
        self.pbar = ttk.Progressbar(main, mode="determinate", maximum=1000)
        self.pbar.pack(fill="x")
        bar = ttk.Frame(main)
        bar.pack(fill="x", pady=(8, 0))
        self.b_start = ttk.Button(bar, text="Start", style="Accent.TButton", command=lambda: self.start(False))
        self.b_preview = ttk.Button(bar, text="Preview cuts", command=lambda: self.start(True))
        self.b_cancel = ttk.Button(bar, text="Cancel", command=self.cancel)
        self.b_open = ttk.Button(bar, text="Open folder", command=self.open_folder)
        for b in (self.b_start, self.b_preview, self.b_cancel, self.b_open):
            b.pack(side="right", padx=(6, 0))
        # Packed after the buttons so a long status gets clipped instead of squashing them.
        ttk.Label(bar, textvariable=self.status, style="Sub.TLabel").pack(side="left", fill="x", expand=True)

    # ---------- settings ----------
    def _toggle_adv(self):
        if self.show_adv.get():
            self.adv.pack(fill="x", before=self.misc, pady=(4, 0))
        else:
            self.adv.pack_forget()

    def _apply_settings(self, d):
        d = d or asdict(core.Settings())
        self._loading = True
        for key, *_ in SLIDERS:
            self.vars[key].set(d.get(key, getattr(core.Settings(), key)))
            self._refresh_value(key)
        self._loading = False

    def _refresh_value(self, key):
        lbl, fmt = self.value_labels[key]
        lbl.configure(text=fmt.format(self.vars[key].get()))

    def _slider_moved(self, key, step, fmt):
        v = round(round(self.vars[key].get() / step) * step, 4)
        self.vars[key].set(v)
        self._refresh_value(key)
        if not getattr(self, "_loading", False):
            self.preset.set("Custom")

    def _preset_changed(self):
        p = self.preset.get()
        if p in core.PRESETS:
            self._apply_settings(asdict(core.PRESETS[p]))

    def _out_changed(self):
        st = "normal" if self.out_mode.get() == "dir" else "disabled"
        self.e_dir.configure(state=st)
        self.b_dir.configure(state=st)

    def settings(self):
        return core.Settings(min_boring=self.vars["min_boring"].get(), pad=self.vars["pad"].get(),
                             quiet_db=self.vars["quiet_db"].get(), motion=self.vars["motion"].get(),
                             nvenc=self.nvenc.get(), crf=QUALITY.get(self.quality.get(), 20))

    # ---------- file list ----------
    def add_path(self, path):
        path = os.path.abspath(path.strip().strip("{}"))
        if os.path.isdir(path):
            for name in sorted(os.listdir(path)):
                if name.lower().endswith(core.VIDEO_EXTS) and "_cut." not in name:
                    self.add_path(os.path.join(path, name))
            return
        if not os.path.isfile(path) or any(i["path"] == path for i in self.items.values()):
            return
        iid = self.tree.insert("", "end", text=os.path.basename(path), values=("Waiting", ""))
        self.items[iid] = {"path": path, "result": None, "error": None}
        self.status.set(f"{len(self.items)} video(s) ready.")

    def browse(self):
        exts = " ".join(f"*{e}" for e in core.VIDEO_EXTS)
        for p in filedialog.askopenfilenames(title="Choose gameplay videos",
                                             filetypes=[("Videos", exts), ("All files", "*.*")]):
            self.add_path(p)

    def _on_drop(self, event):
        for p in self.root.tk.splitlist(event.data):
            self.add_path(p)

    def remove_selected(self):
        if self.worker:
            return
        for iid in self.tree.selection():
            self.tree.delete(iid)
            self.items.pop(iid, None)

    def clear(self):
        if self.worker:
            return
        self.tree.delete(*self.tree.get_children())
        self.items.clear()
        self.status.set("Add some gameplay videos to get started.")

    def pick_dir(self):
        d = filedialog.askdirectory(title="Save cut videos to…")
        if d:
            self.out_dir.set(d)

    # ---------- details ----------
    def _details(self, text):
        self.details.configure(state="normal")
        self.details.delete("1.0", "end")
        self.details.insert("1.0", text)
        self.details.configure(state="disabled")

    def _show_details(self):
        sel = self.tree.selection()
        if not sel:
            return
        it = self.items.get(sel[0])
        if not it:
            return
        if it["error"]:
            self._details(f"{os.path.basename(it['path'])}\n\nProblem:\n{it['error']}")
        elif it["result"]:
            try:
                with open(it["result"].report, encoding="utf-8") as f:
                    text = f.read()
            except OSError:
                text = "(cut list file not found)"
            if it["result"].rendered:
                text += f"\nSaved: {it['result'].output}\n(double-click the row to play it)"
            self._details(text)
        else:
            self._details(f"{it['path']}\n\nNot processed yet.")

    def _open_selected(self):
        sel = self.tree.selection()
        it = self.items.get(sel[0]) if sel else None
        if it and it["result"] and it["result"].rendered and os.path.exists(it["result"].output):
            open_path(it["result"].output)

    def open_folder(self):
        sel = self.tree.selection()
        it = self.items.get(sel[0]) if sel else (next(iter(self.items.values()), None))
        if self.out_mode.get() == "dir" and self.out_dir.get() and os.path.isdir(self.out_dir.get()):
            open_path(self.out_dir.get())
        elif it:
            open_path(os.path.dirname(it["path"]))

    # ---------- running ----------
    def _set_running(self, running):
        on, off = ("disabled", "normal") if running else ("normal", "disabled")
        for w in (self.b_start, self.b_preview, self.b_add, self.b_remove, self.b_clear, self.cb_adv,
                  self.rb_same, self.rb_dir, self.cb_nvenc, *self.scales):
            w.configure(state=on)
        for w in (self.cb_preset, self.cb_quality):
            w.configure(state="disabled" if running else "readonly")
        self.b_cancel.configure(state=off)
        if not running:
            self._out_changed()
        else:
            self.e_dir.configure(state="disabled")
            self.b_dir.configure(state="disabled")

    def start(self, dry_run):
        todo = [iid for iid in self.tree.get_children()]
        if not todo:
            self.browse()
            todo = list(self.tree.get_children())
            if not todo:
                return
        out_dir = None
        if self.out_mode.get() == "dir":
            out_dir = self.out_dir.get().strip()
            if not out_dir or not os.path.isdir(out_dir):
                messagebox.showwarning("AutoCut", "Choose a folder to save the cut videos to.")
                return
        s = self.settings()
        self._save()
        jobs = [(iid, self.items[iid]["path"]) for iid in todo]
        for iid, _ in jobs:
            self.tree.item(iid, values=("Waiting", ""), tags=())
            self.items[iid].update(result=None, error=None)
        self.job = core.Job()
        self._set_running(True)
        self.worker = threading.Thread(target=self._work, args=(jobs, s, out_dir, dry_run, self.job), daemon=True)
        self.worker.start()

    def _work(self, jobs, s, out_dir, dry_run, job):
        n = len(jobs)
        done = 0
        for k, (iid, path) in enumerate(jobs):
            if job.cancelled:
                break
            self.events.put(("item", iid, "Working…", "", "busy"))
            self.events.put(("status", f"[{k + 1}/{n}] {os.path.basename(path)} — Starting…"))

            def prog(f, k=k):
                self.events.put(("progress", (k + f) / n))

            def stat(t, k=k, path=path):
                self.events.put(("status", f"[{k + 1}/{n}] {os.path.basename(path)} — {t}"))

            try:
                out = core.default_output(path, out_dir)
                r = core.process(path, s, out, dry_run, job, prog, stat)
                self.events.put(("result", iid, r))
                pct = 100 * r.removed / r.total if r.total else 0
                if not r.cuts:
                    msg, tag = "Nothing boring found", "done"
                else:
                    msg = f"{len(r.cuts)} cut(s), −{core.fmt(r.removed)} ({pct:.0f}%)"
                    tag = "done"
                state = "Previewed" if dry_run else ("Done" if r.rendered else "Unchanged")
                self.events.put(("item", iid, state, msg, tag))
                done += 1
            except core.Cancelled:
                self.events.put(("item", iid, "Cancelled", "", ""))
                break
            except core.AutoCutError as e:
                self.events.put(("error", iid, str(e)))
            except Exception as e:  # keep the GUI alive whatever happens
                self.events.put(("error", iid, f"{type(e).__name__}: {e}"))
        self.events.put(("finished", done, n, job.cancelled, dry_run))

    def _poll(self):
        try:
            while True:
                ev = self.events.get_nowait()
                kind = ev[0]
                if kind == "progress":
                    self.pbar["value"] = ev[1] * 1000
                elif kind == "status":
                    self.status.set(ev[1])
                elif kind == "item":
                    _, iid, state, msg, tag = ev
                    if self.tree.exists(iid):
                        self.tree.item(iid, values=(state, msg), tags=(tag,) if tag else ())
                elif kind == "result":
                    _, iid, r = ev
                    self.items[iid]["result"] = r
                    if self.tree.selection() == (iid,):
                        self._show_details()
                elif kind == "error":
                    _, iid, msg = ev
                    self.items[iid]["error"] = msg
                    self.tree.item(iid, values=("Failed", msg.splitlines()[0][:60]), tags=("error",))
                    self.tree.selection_set(iid)
                    self._show_details()
                elif kind == "finished":
                    _, done, n, cancelled, dry = ev
                    self.worker = None
                    self._set_running(False)
                    if cancelled:
                        self.status.set("Cancelled.")
                    else:
                        self.pbar["value"] = 1000
                        verb = "previewed" if dry else "processed"
                        self.status.set(f"Finished: {done} of {n} video(s) {verb}.")
                        if done and not self.tree.selection():
                            self.tree.selection_set(self.tree.get_children()[0])
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def cancel(self):
        if self.job:
            self.job.cancel()
            self.status.set("Cancelling…")

    # ---------- misc ----------
    def _check_ffmpeg(self):
        try:
            core.check_ffmpeg()
        except core.AutoCutError as e:
            messagebox.showerror("ffmpeg needed", str(e))

    def _save(self):
        save_config({
            "preset": self.preset.get(), "custom": asdict(self.settings()), "nvenc": self.nvenc.get(),
            "quality": self.quality.get(), "out_mode": self.out_mode.get(), "out_dir": self.out_dir.get(),
            "show_advanced": self.show_adv.get(), "geometry": self.root.geometry(),
        })

    def on_close(self):
        if self.worker and not messagebox.askyesno("AutoCut", "A video is still being processed. Stop and quit?"):
            return
        if self.job:
            self.job.cancel()
        self._save()
        self.root.destroy()


def main():
    root = TkinterDnD.Tk() if TkinterDnD else tk.Tk()
    App(root, sys.argv[1:])
    root.mainloop()


if __name__ == "__main__":
    main()
