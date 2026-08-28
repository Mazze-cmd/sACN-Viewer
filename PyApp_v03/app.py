"""
Signal — sACN / DMX Monitor
A small desktop app that listens for real sACN (E1.31) traffic on the
network and displays incoming DMX values in a 10-column grid, per universe.

Dependencies (see requirements.txt):
    sacn      - E1.31 / sACN receiver implementation
    psutil    - used to enumerate local network adapters

Run directly with:
    python app.py

Package to a Windows .exe with PyInstaller (see build.bat / README.md).
"""

import socket
import threading
import time
from collections import defaultdict
import tkinter as tk
from tkinter import ttk, messagebox

try:
    import psutil
except ImportError:
    psutil = None

try:
    import sacn
except ImportError:
    sacn = None

CHANNELS = 512
COLS = 10
ROWS = (CHANNELS + COLS - 1) // COLS  # 52
MAX_UNIVERSES_SHOWN = 32              # keep the UI responsive

# ---- fixture channel types -------------------------------------------
FIXTURE_TYPE_OPTIONS = ["", "Intensity", "Red", "Green", "Blue"]
FIXTURE_TYPE_KEY = {"": None, "Intensity": "intensity", "Red": "red",
                     "Green": "green", "Blue": "blue"}
FIXTURE_TYPE_LABEL = {"intensity": "Intensity", "red": "Red",
                       "green": "Green", "blue": "Blue"}

# ---- theme -----------------------------------------------------------
BG = "#0a0e12"
PANEL = "#11171d"
PANEL2 = "#161d24"
LINE = "#232b33"
TEXT = "#e4e9ed"
TEXT_MUTED = "#6b7680"
SIGNAL = "#00d9ff"
DANGER = "#ff4d4d"
MONO = "Consolas"


def list_adapters():
    """Return [(ip, label), ...] including a loopback entry first."""
    adapters = [("127.0.0.1", "Loopback (127.0.0.1)")]
    if psutil is None:
        return adapters
    try:
        for iface, addrs in psutil.net_if_addrs().items():
            for addr in addrs:
                if addr.family == socket.AF_INET and addr.address != "127.0.0.1":
                    adapters.append((addr.address, f"{iface}  ({addr.address})"))
    except Exception:
        pass
    return adapters


class UniverseGrid(ttk.Frame):
    """Canvas-based 10-column x 52-row grid for one DMX universe."""

    def __init__(self, parent):
        super().__init__(parent)
        self.cell_w, self.cell_h = 56, 28
        self.label_w, self.header_h = 74, 24
        self.content_w = self.label_w + COLS * self.cell_w
        self.content_h = self.header_h + ROWS * self.cell_h

        container = ttk.Frame(self)
        container.pack(fill="both", expand=True, padx=10, pady=10)
        container.rowconfigure(0, weight=1)
        container.columnconfigure(0, weight=1)

        # width is fixed (columns don't change), height scrolls so all
        # 512 channels are reachable even in a short window.
        self.canvas = tk.Canvas(container, width=self.content_w, bg=PANEL,
                                 highlightthickness=1, highlightbackground=LINE)
        vbar = ttk.Scrollbar(container, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=vbar.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        vbar.grid(row=0, column=1, sticky="ns")

        self.cell_ids = {}  # channel index (0-based) -> (rect_id, text_id)
        self._draw_static()
        self.canvas.configure(scrollregion=(0, 0, self.content_w, self.content_h))

        # mouse wheel scrolling, only while the pointer is over this grid
        self.canvas.bind("<Enter>", lambda e: self.canvas.bind_all("<MouseWheel>", self._on_mousewheel))
        self.canvas.bind("<Leave>", lambda e: self.canvas.unbind_all("<MouseWheel>"))

    def _on_mousewheel(self, event):
        self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def _draw_static(self):
        c = self.canvas
        c.create_rectangle(0, 0, self.label_w, self.header_h, fill=PANEL2, outline=LINE)
        c.create_text(self.label_w / 2, self.header_h / 2, text="CH",
                       fill=TEXT_MUTED, font=(MONO, 9))
        for col in range(COLS):
            x0 = self.label_w + col * self.cell_w
            c.create_rectangle(x0, 0, x0 + self.cell_w, self.header_h, fill=PANEL2, outline=LINE)
            c.create_text(x0 + self.cell_w / 2, self.header_h / 2, text=str(col + 1),
                           fill=TEXT_MUTED, font=(MONO, 9))

        for row in range(ROWS):
            y0 = self.header_h + row * self.cell_h
            start_ch, end_ch = row * COLS + 1, min(row * COLS + COLS, CHANNELS)
            c.create_rectangle(0, y0, self.label_w, y0 + self.cell_h, fill=PANEL2, outline=LINE)
            c.create_text(self.label_w / 2, y0 + self.cell_h / 2, text=f"{start_ch}-{end_ch}",
                           fill=TEXT_MUTED, font=(MONO, 8))
            for col in range(COLS):
                ch = row * COLS + col
                x0 = self.label_w + col * self.cell_w
                if ch >= CHANNELS:
                    c.create_rectangle(x0, y0, x0 + self.cell_w, y0 + self.cell_h, fill=BG, outline=LINE)
                    continue
                rect = c.create_rectangle(x0, y0, x0 + self.cell_w, y0 + self.cell_h, fill=PANEL, outline=LINE)
                text = c.create_text(x0 + self.cell_w / 2, y0 + self.cell_h / 2, text="0",
                                      fill=TEXT_MUTED, font=(MONO, 10, "bold"))
                self.cell_ids[ch] = (rect, text)

    def update_values(self, values):
        c = self.canvas
        for ch, (rect, text) in self.cell_ids.items():
            v = values[ch] if values is not None and ch < len(values) else 0
            frac = v / 255.0
            c.itemconfig(rect, fill=self._blend(frac))
            c.itemconfig(text, text=str(v), fill=("#04181c" if v > 140 else TEXT))

    def clear(self):
        c = self.canvas
        for rect, text in self.cell_ids.values():
            c.itemconfig(rect, fill=PANEL)
            c.itemconfig(text, text="0", fill=TEXT_MUTED)

    @staticmethod
    def _blend(frac):
        r1, g1, b1 = 0x11, 0x17, 0x1d
        r2, g2, b2 = 0x00, 0xd9, 0xff
        r = int(r1 + (r2 - r1) * frac)
        g = int(g1 + (g2 - g1) * frac)
        b = int(b1 + (b2 - b1) * frac)
        return f"#{r:02x}{g:02x}{b:02x}"


class Fixture:
    """A virtual DMX fixture: a universe, a starting channel, and a small
    channel layout (intensity/red/green/blue, optionally 16-bit)."""

    def __init__(self, name, universe=1, start_channel=1, channel_count=4):
        self.name = name
        self.universe = universe
        self.start_channel = start_channel
        self.channels = []
        self.set_channel_count(channel_count)
        if channel_count >= 4:
            self.channels[0]["type"] = "intensity"
            self.channels[1]["type"] = "red"
            self.channels[2]["type"] = "green"
            self.channels[3]["type"] = "blue"

    @staticmethod
    def _blank():
        return {"type": None, "role": "single", "pair_index": None}

    def channel_count(self):
        return len(self.channels)

    def set_channel_count(self, n):
        n = max(1, min(64, n))
        cur = len(self.channels)
        if n > cur:
            self.channels += [self._blank() for _ in range(n - cur)]
        elif n < cur:
            self.channels = self.channels[:n]
            for c in self.channels:
                if c["role"] == "coarse" and (c["pair_index"] is None or c["pair_index"] >= n):
                    c["role"] = "single"
                    c["pair_index"] = None

    def can_enable_16(self, i):
        c = self.channels[i]
        if c["role"] != "single" or not c["type"]:
            return False
        j = i + 1
        return j < len(self.channels) and self.channels[j]["role"] == "single"

    def toggle_16(self, i):
        c = self.channels[i]
        if c["role"] == "single":
            if not self.can_enable_16(i):
                return
            j = i + 1
            c["role"], c["pair_index"] = "coarse", j
            self.channels[j] = {"type": c["type"], "role": "fine", "pair_index": i}
        elif c["role"] == "coarse":
            j = c["pair_index"]
            self.channels[j] = self._blank()
            c["role"], c["pair_index"] = "single", None

    def set_type(self, i, type_key):
        self.channels[i]["type"] = type_key
        if self.channels[i]["role"] == "coarse":
            self.channels[self.channels[i]["pair_index"]]["type"] = type_key

    def compute_color(self, dmx_values):
        """dmx_values: list of ints for this fixture's universe, or None
        if that universe currently has no data. Returns (r, g, b) 0-255,
        or None if there's no data to show at all."""
        if dmx_values is None:
            return None

        def get_val(type_key):
            for idx, c in enumerate(self.channels):
                if c["type"] == type_key and c["role"] in ("single", "coarse"):
                    abs_ch = self.start_channel - 1 + idx
                    if 0 <= abs_ch < len(dmx_values):
                        return dmx_values[abs_ch]
                    return 0
            return None

        r = get_val("red") or 0
        g = get_val("green") or 0
        b = get_val("blue") or 0
        i_val = get_val("intensity")
        dimmer = 1.0 if i_val is None else i_val / 255.0
        return (int(r * dimmer), int(g * dimmer), int(b * dimmer))


class App:
    def __init__(self, root):
        self.root = root
        root.title("Signal — sACN Monitor")
        root.configure(bg=BG)
        root.geometry("1320x760")
        root.minsize(1040, 620)

        self.style = ttk.Style()
        try:
            self.style.theme_use("clam")
        except tk.TclError:
            pass
        self._style_widgets()

        self.receiver = None
        self.listening = False
        self.data_lock = threading.Lock()
        self.latest_data = {}          # universe -> list[512] of ints
        self.packet_counts = defaultdict(int)
        self.rates = defaultdict(int)
        self.grids = {}                 # universe -> UniverseGrid
        self.range_start = 1
        self.range_end = 4
        self.active_universe = 1

        self.fixtures = []              # list[Fixture]
        self.selected_fixture_index = None
        self.swatch_canvases = {}       # fixture index -> (canvas, oval_id)

        self._build_topbar()
        self._build_body()
        self._build_status_strip()

        self._apply_range(initial=True)
        self.root.after(120, self._refresh_loop)
        self.root.after(1000, self._rate_loop)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------------------------------------------------------------- UI
    def _style_widgets(self):
        s = self.style
        s.configure("TFrame", background=BG)
        s.configure("Panel.TFrame", background=PANEL)
        s.configure("TLabel", background=BG, foreground=TEXT_MUTED, font=(MONO, 9))
        s.configure("Title.TLabel", background=BG, foreground=TEXT, font=("Segoe UI", 15, "bold"))
        s.configure("TButton", background=PANEL2, foreground=SIGNAL, font=(MONO, 9), padding=6)
        s.map("TButton", background=[("active", LINE)])
        s.configure("TCombobox", fieldbackground=PANEL2, background=PANEL2, foreground=TEXT)
        s.configure("TSpinbox", fieldbackground=PANEL2, foreground=TEXT)
        s.configure("TNotebook", background=BG, borderwidth=0)
        s.configure("TNotebook.Tab", background=PANEL2, foreground=TEXT_MUTED,
                    font=(MONO, 9), padding=(12, 6))
        s.map("TNotebook.Tab", foreground=[("selected", SIGNAL)],
              background=[("selected", PANEL)])

    def _build_topbar(self):
        bar = ttk.Frame(self.root, padding=(14, 10))
        bar.pack(fill="x")

        title_row = ttk.Frame(bar)
        title_row.pack(side="left", padx=(0, 24))
        ttk.Label(title_row, text="SIGNAL", style="Title.TLabel").pack(anchor="w")
        ttk.Label(title_row, text="sACN / DMX MONITOR").pack(anchor="w")

        # power switch
        power_col = ttk.Frame(bar)
        power_col.pack(side="left", padx=12)
        ttk.Label(power_col, text="SACN LISTENING").pack(anchor="w")
        self.power_btn = tk.Button(power_col, text="\u25CF  Stopped", command=self._toggle_listen,
                                    bg=PANEL2, fg=TEXT_MUTED, activebackground=PANEL2,
                                    relief="flat", font=(MONO, 10), padx=10, pady=4,
                                    highlightbackground=LINE, highlightthickness=1)
        self.power_btn.pack(anchor="w", pady=(3, 0))

        # adapter
        adapter_col = ttk.Frame(bar)
        adapter_col.pack(side="left", padx=12)
        ttk.Label(adapter_col, text="NETWORK ADAPTER").pack(anchor="w")
        self.adapters = list_adapters()
        self.adapter_var = tk.StringVar(value=self.adapters[0][1])
        self.adapter_box = ttk.Combobox(adapter_col, textvariable=self.adapter_var, state="readonly",
                                         width=26, values=[a[1] for a in self.adapters])
        self.adapter_box.pack(anchor="w", pady=(3, 0))
        self.adapter_box.bind("<<ComboboxSelected>>", self._on_adapter_change)

        # universe range
        range_col = ttk.Frame(bar)
        range_col.pack(side="left", padx=12)
        ttk.Label(range_col, text="UNIVERSE RANGE").pack(anchor="w")
        row = ttk.Frame(range_col)
        row.pack(anchor="w", pady=(3, 0))
        self.start_var = tk.StringVar(value="1")
        self.end_var = tk.StringVar(value="4")
        tk.Spinbox(row, from_=1, to=63999, textvariable=self.start_var, width=6,
                   bg=PANEL2, fg=TEXT, buttonbackground=PANEL2, relief="flat").pack(side="left")
        ttk.Label(row, text=" \u2013 ").pack(side="left")
        tk.Spinbox(row, from_=1, to=63999, textvariable=self.end_var, width=6,
                   bg=PANEL2, fg=TEXT, buttonbackground=PANEL2, relief="flat").pack(side="left")
        ttk.Button(row, text="Apply", command=self._apply_range).pack(side="left", padx=(8, 0))

    def _build_body(self):
        paned = tk.PanedWindow(self.root, orient=tk.HORIZONTAL, bg=BG,
                                sashwidth=6, sashrelief="flat", bd=0)
        paned.pack(fill="both", expand=True, padx=10, pady=(0, 4))

        left = ttk.Frame(paned)
        self.notebook = ttk.Notebook(left)
        self.notebook.pack(fill="both", expand=True)
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)
        paned.add(left, minsize=560)

        right = tk.Frame(paned, bg=BG)
        paned.add(right, minsize=320, width=360)
        self._build_fixture_sidebar(right)

    def _build_fixture_sidebar(self, parent):
        # ---- 1) fixture list -------------------------------------------
        list_panel = tk.Frame(parent, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
        list_panel.pack(fill="x", pady=(0, 10))
        tk.Label(list_panel, text="FIXTURES", bg=PANEL, fg=TEXT,
                 font=(MONO, 10, "bold")).pack(anchor="w", padx=10, pady=(10, 6))

        add_row = tk.Frame(list_panel, bg=PANEL)
        add_row.pack(fill="x", padx=10, pady=(0, 8))
        self.new_fixture_name = tk.StringVar()
        entry = tk.Entry(add_row, textvariable=self.new_fixture_name, bg=PANEL2, fg=TEXT,
                          insertbackground=TEXT, relief="flat", font=(MONO, 10))
        entry.pack(side="left", fill="x", expand=True, ipady=3)
        entry.bind("<Return>", lambda e: self._add_fixture())
        tk.Button(add_row, text="+ Add", command=self._add_fixture, bg=PANEL2, fg=SIGNAL,
                  relief="flat", font=(MONO, 9), padx=8).pack(side="left", padx=(6, 0))

        self.fixture_listbox = tk.Listbox(list_panel, bg=PANEL2, fg=TEXT, selectbackground=SIGNAL,
                                           selectforeground="#04181c", relief="flat", height=6,
                                           font=(MONO, 10), highlightthickness=0, activestyle="none")
        self.fixture_listbox.pack(fill="x", padx=10, pady=(0, 6))
        self.fixture_listbox.bind("<<ListboxSelect>>", self._on_fixture_select)

        tk.Button(list_panel, text="Remove selected", command=self._remove_fixture,
                  bg=PANEL2, fg=DANGER, relief="flat", font=(MONO, 9),
                  padx=8).pack(anchor="w", padx=10, pady=(0, 10))

        # ---- 2) fixture config ------------------------------------------
        self.config_panel = tk.Frame(parent, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
        self.config_panel.pack(fill="x", pady=(0, 10))
        self._render_config_panel_empty()

        # ---- 3) color preview -------------------------------------------
        preview_panel = tk.Frame(parent, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
        preview_panel.pack(fill="both", expand=True)
        tk.Label(preview_panel, text="FIXTURE PREVIEW", bg=PANEL, fg=TEXT,
                 font=(MONO, 10, "bold")).pack(anchor="w", padx=10, pady=(10, 6))
        self.preview_wrap = tk.Frame(preview_panel, bg=PANEL)
        self.preview_wrap.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self._render_preview_swatches()

    # ------------------------------------------------------- fixture list
    def _add_fixture(self):
        name = self.new_fixture_name.get().strip() or f"Fixture {len(self.fixtures) + 1}"
        self.fixtures.append(Fixture(name))
        self.new_fixture_name.set("")
        self._refresh_fixture_list()
        self.fixture_listbox.selection_clear(0, tk.END)
        self.fixture_listbox.selection_set(tk.END)
        self._select_fixture(len(self.fixtures) - 1)

    def _remove_fixture(self):
        sel = self.fixture_listbox.curselection()
        if not sel:
            return
        del self.fixtures[sel[0]]
        self.selected_fixture_index = None
        self._refresh_fixture_list()
        self._render_config_panel_empty()

    def _refresh_fixture_list(self):
        self.fixture_listbox.delete(0, tk.END)
        for fx in self.fixtures:
            self.fixture_listbox.insert(tk.END, fx.name)
        self._render_preview_swatches()

    def _on_fixture_select(self, _event=None):
        sel = self.fixture_listbox.curselection()
        if sel:
            self._select_fixture(sel[0])

    def _select_fixture(self, idx):
        self.selected_fixture_index = idx
        self._render_config_panel()

    # ----------------------------------------------------- fixture config
    def _clear_config_panel(self):
        for w in self.config_panel.winfo_children():
            w.destroy()

    def _render_config_panel_empty(self):
        self._clear_config_panel()
        tk.Label(self.config_panel, text="FIXTURE CONFIG", bg=PANEL, fg=TEXT,
                 font=(MONO, 10, "bold")).pack(anchor="w", padx=10, pady=(10, 4))
        tk.Label(self.config_panel, text="Select or add a fixture to configure it.",
                 bg=PANEL, fg=TEXT_MUTED, font=(MONO, 9)).pack(anchor="w", padx=10, pady=(0, 10))

    def _render_config_panel(self):
        if self.selected_fixture_index is None or self.selected_fixture_index >= len(self.fixtures):
            self._render_config_panel_empty()
            return
        self._clear_config_panel()
        fx = self.fixtures[self.selected_fixture_index]

        tk.Label(self.config_panel, text=f"CONFIG \u2014 {fx.name}", bg=PANEL, fg=TEXT,
                 font=(MONO, 10, "bold")).pack(anchor="w", padx=10, pady=(10, 6))

        row1 = tk.Frame(self.config_panel, bg=PANEL)
        row1.pack(fill="x", padx=10, pady=(0, 8))
        for col, txt in enumerate(["Universe", "Start ch.", "Channels"]):
            tk.Label(row1, text=txt, bg=PANEL, fg=TEXT_MUTED, font=(MONO, 9)).grid(
                row=0, column=col, sticky="w", padx=(0 if col == 0 else 10, 0))

        uni_var = tk.StringVar(value=str(fx.universe))
        start_var = tk.StringVar(value=str(fx.start_channel))
        count_var = tk.StringVar(value=str(fx.channel_count()))

        tk.Spinbox(row1, from_=1, to=63999, textvariable=uni_var, width=6, bg=PANEL2, fg=TEXT,
                   buttonbackground=PANEL2, relief="flat").grid(row=1, column=0, sticky="w")
        tk.Spinbox(row1, from_=1, to=512, textvariable=start_var, width=6, bg=PANEL2, fg=TEXT,
                   buttonbackground=PANEL2, relief="flat").grid(row=1, column=1, sticky="w", padx=(10, 0))
        tk.Spinbox(row1, from_=1, to=64, textvariable=count_var, width=6, bg=PANEL2, fg=TEXT,
                   buttonbackground=PANEL2, relief="flat").grid(row=1, column=2, sticky="w", padx=(10, 0))

        def apply_basics():
            try:
                fx.universe = int(uni_var.get())
                fx.start_channel = int(start_var.get())
                fx.set_channel_count(int(count_var.get()))
            except ValueError:
                messagebox.showerror("Invalid value", "Universe, start channel and channel "
                                                        "count must be numbers.")
                return
            self._render_config_panel()
            self._render_preview_swatches()

        tk.Button(row1, text="Apply", command=apply_basics, bg=PANEL2, fg=SIGNAL, relief="flat",
                  font=(MONO, 9), padx=8).grid(row=1, column=3, padx=(10, 0))

        rows_frame = tk.Frame(self.config_panel, bg=PANEL)
        rows_frame.pack(fill="x", padx=10, pady=(4, 10))

        i = 0
        while i < len(fx.channels):
            c = fx.channels[i]
            if c["role"] == "fine":
                i += 1
                continue

            row = tk.Frame(rows_frame, bg=PANEL2, highlightbackground=LINE, highlightthickness=1)
            row.pack(fill="x", pady=3)

            if c["role"] == "coarse":
                ch_label = f"CH {fx.start_channel + i}\u2013{fx.start_channel + c['pair_index']}"
            else:
                ch_label = f"CH {fx.start_channel + i}"
            tk.Label(row, text=ch_label, bg=PANEL2, fg=TEXT, font=(MONO, 9), width=11,
                     anchor="w").pack(side="left", padx=(8, 4), pady=6)

            type_var = tk.StringVar(value=FIXTURE_TYPE_LABEL.get(c["type"], ""))
            combo = ttk.Combobox(row, textvariable=type_var, state="readonly", width=9,
                                  values=FIXTURE_TYPE_OPTIONS)
            combo.pack(side="left", padx=4)

            def make_type_handler(idx=i, var=type_var):
                def handler(_event=None):
                    fx.set_type(idx, FIXTURE_TYPE_KEY[var.get()])
                    self._render_config_panel()
                    self._render_preview_swatches()
                return handler
            combo.bind("<<ComboboxSelected>>", make_type_handler())

            can16 = c["role"] == "coarse" or fx.can_enable_16(i)
            bit_var = tk.BooleanVar(value=(c["role"] == "coarse"))
            cb = tk.Checkbutton(row, text="16-bit", variable=bit_var, bg=PANEL2, fg=TEXT_MUTED,
                                 selectcolor=PANEL2, activebackground=PANEL2, font=(MONO, 8),
                                 state=("normal" if can16 else "disabled"))

            def make_bit_handler(idx=i):
                def handler():
                    fx.toggle_16(idx)
                    self._render_config_panel()
                    self._render_preview_swatches()
                return handler
            cb.config(command=make_bit_handler())
            cb.pack(side="left", padx=4)

            i += 1

    # ----------------------------------------------------- color preview
    def _render_preview_swatches(self):
        for w in self.preview_wrap.winfo_children():
            w.destroy()
        self.swatch_canvases = {}
        cols = 3
        for idx, fx in enumerate(self.fixtures):
            r, c = divmod(idx, cols)
            cell = tk.Frame(self.preview_wrap, bg=PANEL)
            cell.grid(row=r, column=c, padx=8, pady=8)
            canvas = tk.Canvas(cell, width=64, height=64, bg=PANEL, highlightthickness=1,
                                highlightbackground=LINE)
            oval = canvas.create_oval(6, 6, 58, 58, fill="#1a1a1a", outline=LINE)
            canvas.pack()
            tk.Label(cell, text=fx.name, bg=PANEL, fg=TEXT_MUTED, font=(MONO, 9)).pack(pady=(4, 0))
            self.swatch_canvases[idx] = (canvas, oval)
        if not self.fixtures:
            tk.Label(self.preview_wrap, text="No fixtures yet — add one above.",
                     bg=PANEL, fg=TEXT_DIM if False else TEXT_MUTED,
                     font=(MONO, 9)).grid(row=0, column=0, sticky="w")

    def _update_fixture_previews(self):
        for idx, fx in enumerate(self.fixtures):
            info = self.swatch_canvases.get(idx)
            if not info:
                continue
            canvas, oval = info
            dmx_values = None
            if self.listening and self.range_start <= fx.universe <= self.range_end:
                with self.data_lock:
                    dmx_values = self.latest_data.get(fx.universe)
            color = fx.compute_color(dmx_values)
            if color:
                hexcolor = "#%02x%02x%02x" % color
                canvas.itemconfig(oval, fill=hexcolor, outline=hexcolor)
            else:
                canvas.itemconfig(oval, fill="#1a1a1a", outline=LINE)

    def _build_status_strip(self):
        strip = tk.Frame(self.root, bg=PANEL)
        strip.pack(fill="x", side="bottom")
        self.status_var = tk.StringVar(value="Universe 1  •  512/512 channels  •  no signal")
        tk.Label(strip, textvariable=self.status_var, bg=PANEL, fg=TEXT_MUTED,
                 font=(MONO, 9), anchor="w", padx=12, pady=6).pack(fill="x")

    # ----------------------------------------------------------- helpers
    def _selected_adapter_ip(self):
        label = self.adapter_var.get()
        for ip, lbl in self.adapters:
            if lbl == label:
                return ip
        return "0.0.0.0"

    def _rebuild_tabs(self):
        for tab_id in self.notebook.tabs():
            self.notebook.forget(tab_id)
        self.grids = {}
        for u in range(self.range_start, self.range_end + 1):
            grid = UniverseGrid(self.notebook)
            self.notebook.add(grid, text=f"Universe {u}")
            self.grids[u] = grid
        self.active_universe = self.range_start

    def _apply_range(self, initial=False):
        try:
            s = int(self.start_var.get())
            e = int(self.end_var.get())
        except ValueError:
            messagebox.showerror("Invalid range", "Universe range must be numbers.")
            return
        if e < s:
            e = s
        if e - s + 1 > MAX_UNIVERSES_SHOWN:
            e = s + MAX_UNIVERSES_SHOWN - 1
            self.end_var.set(str(e))
            messagebox.showinfo("Range limited", f"Showing max {MAX_UNIVERSES_SHOWN} universes at once.")
        self.range_start, self.range_end = s, e
        self._rebuild_tabs()
        if self.listening:
            self._restart_receiver()
        self._update_status()

    def _on_tab_changed(self, _event=None):
        idx = self.notebook.index(self.notebook.select())
        self.active_universe = self.range_start + idx
        self._update_status()

    def _on_adapter_change(self, _event=None):
        if self.listening:
            self._restart_receiver()

    # ------------------------------------------------------------ sACN
    def _toggle_listen(self):
        if not self.listening:
            self._start_receiver()
        else:
            self._stop_receiver()

    def _start_receiver(self):
        if sacn is None:
            messagebox.showerror(
                "Missing dependency",
                "The 'sacn' package is not installed.\n\nInstall it with:\n    pip install sacn"
            )
            return
        bind_ip = self._selected_adapter_ip()
        try:
            self.receiver = sacn.sACNreceiver(bind_address=bind_ip)
        except TypeError:
            # older/newer versions of the library may not accept bind_address
            self.receiver = sacn.sACNreceiver()
        try:
            self.receiver.start()
            for u in range(self.range_start, self.range_end + 1):
                self.receiver.listen_on('universe', universe=u)(self._make_callback(u))
                self.receiver.join_multicast(u)
        except Exception as exc:
            messagebox.showerror("Could not start listening", str(exc))
            self.receiver = None
            return

        self.listening = True
        self.power_btn.config(text="\u25CF  Listening", fg=SIGNAL)
        self._update_status()

    def _stop_receiver(self):
        if self.receiver is not None:
            try:
                for u in range(self.range_start, self.range_end + 1):
                    self.receiver.leave_multicast(u)
                self.receiver.stop()
            except Exception:
                pass
            self.receiver = None
        self.listening = False
        self.power_btn.config(text="\u25CF  Stopped", fg=TEXT_MUTED)
        with self.data_lock:
            self.latest_data.clear()
        for grid in self.grids.values():
            grid.clear()
        self._update_status()

    def _restart_receiver(self):
        was_listening = self.listening
        if was_listening:
            self._stop_receiver()
        if was_listening:
            self._start_receiver()

    def _make_callback(self, universe):
        def callback(packet):
            with self.data_lock:
                self.latest_data[universe] = list(packet.dmxData)
            self.packet_counts[universe] += 1
        return callback

    # --------------------------------------------------------- refresh
    def _refresh_loop(self):
        grid = self.grids.get(self.active_universe)
        if grid is not None:
            with self.data_lock:
                values = self.latest_data.get(self.active_universe)
            if values is not None:
                grid.update_values(values)
        self._update_fixture_previews()
        self.root.after(120, self._refresh_loop)

    def _rate_loop(self):
        for u in list(self.packet_counts.keys()):
            self.rates[u] = self.packet_counts[u]
            self.packet_counts[u] = 0
        self._update_status()
        self.root.after(1000, self._rate_loop)

    def _update_status(self):
        rate = self.rates.get(self.active_universe, 0)
        rate_txt = f"{rate} packets/sec" if self.listening else "no signal"
        adapter_label = self.adapter_var.get()
        self.status_var.set(
            f"Universe {self.active_universe}  •  512/512 channels  •  {rate_txt}  •  {adapter_label}"
        )

    def _on_close(self):
        if self.listening:
            self._stop_receiver()
        self.root.destroy()


def main():
    if psutil is None:
        print("Warning: psutil not installed — only loopback will be listed as an adapter.")
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()