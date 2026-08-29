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
import copy
import json
from collections import defaultdict
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog, filedialog

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

# blend targets used to colour DMX grid cells by the fixture channel type
# assigned to them (falls back to SIGNAL cyan for unassigned channels)
TYPE_BLEND_TARGETS = {
    "red": (255, 70, 70),
    "green": (70, 220, 100),
    "blue": (80, 150, 255),
    "intensity": (235, 235, 235),
}
DEFAULT_BLEND_TARGET = (0x00, 0xd9, 0xff)


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

    def update_values(self, values, channel_types=None):
        c = self.canvas
        for ch, (rect, text) in self.cell_ids.items():
            v = values[ch] if values is not None and ch < len(values) else 0
            frac = v / 255.0
            type_key = channel_types.get(ch) if channel_types else None
            target = TYPE_BLEND_TARGETS.get(type_key, DEFAULT_BLEND_TARGET)
            c.itemconfig(rect, fill=self._blend(frac, target))
            c.itemconfig(text, text=str(v), fill=("#04181c" if v > 140 else TEXT))

    def clear(self):
        c = self.canvas
        for rect, text in self.cell_ids.values():
            c.itemconfig(rect, fill=PANEL)
            c.itemconfig(text, text="0", fill=TEXT_MUTED)

    @staticmethod
    def _blend(frac, target=DEFAULT_BLEND_TARGET):
        r1, g1, b1 = 0x11, 0x17, 0x1d
        r2, g2, b2 = target
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

        # multi-pixel support
        self.pixel_count = 1
        self.repeat_from = None   # 1-based index into self.channels
        self.repeat_till = None   # 1-based index into self.channels
        self.pixel_rows = 1
        self.pixel_cols = 1

        # position/size in the fixture preview canvas (None = not yet placed)
        self.preview_x = None
        self.preview_y = None
        self.preview_w = 120
        self.preview_h = 120

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

    def clone(self, new_name=None):
        fx = Fixture(new_name or (self.name + " copy"), universe=self.universe,
                      start_channel=self.start_channel, channel_count=self.channel_count())
        fx.channels = copy.deepcopy(self.channels)
        fx.pixel_count = self.pixel_count
        fx.repeat_from = self.repeat_from
        fx.repeat_till = self.repeat_till
        fx.pixel_rows = self.pixel_rows
        fx.pixel_cols = self.pixel_cols
        return fx

    def to_dict(self):
        return {
            "name": self.name,
            "universe": self.universe,
            "start_channel": self.start_channel,
            "channels": copy.deepcopy(self.channels),
            "pixel_count": self.pixel_count,
            "repeat_from": self.repeat_from,
            "repeat_till": self.repeat_till,
            "pixel_rows": self.pixel_rows,
            "pixel_cols": self.pixel_cols,
            "preview_x": self.preview_x,
            "preview_y": self.preview_y,
            "preview_w": self.preview_w,
            "preview_h": self.preview_h,
        }

    @classmethod
    def from_dict(cls, d):
        channels = d.get("channels") or []
        fx = cls(d.get("name", "Fixture"), universe=d.get("universe", 1),
                  start_channel=d.get("start_channel", 1),
                  channel_count=len(channels) or 4)
        if channels:
            fx.channels = copy.deepcopy(channels)
        fx.pixel_count = d.get("pixel_count", 1)
        fx.repeat_from = d.get("repeat_from")
        fx.repeat_till = d.get("repeat_till")
        fx.pixel_rows = d.get("pixel_rows", 1)
        fx.pixel_cols = d.get("pixel_cols", 1)
        fx.preview_x = d.get("preview_x")
        fx.preview_y = d.get("preview_y")
        fx.preview_w = d.get("preview_w", 120)
        fx.preview_h = d.get("preview_h", 120)
        return fx

    def is_repeating(self):
        return bool(
            self.pixel_count and self.pixel_count > 1
            and self.repeat_from and self.repeat_till
            and self.repeat_from >= 1
            and self.repeat_till >= self.repeat_from
            and self.repeat_till <= len(self.channels)
        )

    def _abs_offset(self, rel_idx, pixel_idx):
        """0-based channel offset from start_channel for template position
        rel_idx (0-based), for the given 0-based pixel_idx."""
        if not self.is_repeating():
            return rel_idx
        before_len = self.repeat_from - 1
        repeat_len = self.repeat_till - self.repeat_from + 1
        if rel_idx < before_len:
            return rel_idx
        elif rel_idx <= self.repeat_till - 1:
            return before_len + pixel_idx * repeat_len + (rel_idx - before_len)
        else:
            return before_len + repeat_len * self.pixel_count + (rel_idx - self.repeat_till)

    def channel_type_map(self):
        """{0-based offset from start_channel: type_key}, expanded across
        all pixels if this fixture repeats a channel block."""
        mapping = {}
        n_pixels = self.pixel_count if self.is_repeating() else 1
        for p in range(n_pixels):
            for idx, c in enumerate(self.channels):
                if c["type"] and c["role"] in ("single", "coarse"):
                    mapping[self._abs_offset(idx, p)] = c["type"]
        return mapping

    def compute_pixel_colors(self, dmx_values):
        """Returns a list of (r, g, b) tuples, one per pixel (length 1 for
        a non-repeating fixture), or None if there's no data to show."""
        if dmx_values is None:
            return None
        n_pixels = self.pixel_count if self.is_repeating() else 1
        colors = []
        for p in range(n_pixels):
            def get_val(type_key, p=p):
                for idx, c in enumerate(self.channels):
                    if c["type"] == type_key and c["role"] in ("single", "coarse"):
                        abs_ch = self.start_channel - 1 + self._abs_offset(idx, p)
                        if 0 <= abs_ch < len(dmx_values):
                            return dmx_values[abs_ch]
                        return 0
                return None
            r = get_val("red") or 0
            g = get_val("green") or 0
            b = get_val("blue") or 0
            i_val = get_val("intensity")
            dimmer = 1.0 if i_val is None else i_val / 255.0
            colors.append((int(r * dimmer), int(g * dimmer), int(b * dimmer)))
        return colors


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
        self.fixture_items = {}         # id(fixture) -> canvas item ids
        self.presets = {}               # preset name -> channel layout dict
        self.colorize_enabled = False
        self.snap_enabled = False
        self._drag_fx = None
        self._drag_mode = None
        self._drag_start_mouse = (0, 0)
        self._drag_start_geom = (0, 0, 0, 0)
        self.preview_undocked = False
        self.preview_window = None
        self.preview_host = None
        self.preview_canvas_container = None
        self._embedded_canvas_placeholder = None

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

        # Combobox: force the dark field colour in every state (clam's
        # default "readonly" state otherwise overrides fieldbackground with
        # a light grey), and style the popdown listbox to match.
        s.configure("TCombobox", fieldbackground=PANEL2, background=PANEL2,
                    foreground=TEXT, arrowcolor=TEXT_MUTED, selectbackground=PANEL2,
                    selectforeground=TEXT)
        s.map("TCombobox",
              fieldbackground=[("readonly", PANEL2), ("disabled", PANEL2), ("active", PANEL2)],
              background=[("readonly", PANEL2), ("active", PANEL2)],
              foreground=[("readonly", TEXT), ("disabled", TEXT_MUTED)],
              selectbackground=[("readonly", PANEL2)],
              selectforeground=[("readonly", TEXT)])
        self.root.option_add("*TCombobox*Listbox.background", PANEL2)
        self.root.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.root.option_add("*TCombobox*Listbox.selectBackground", SIGNAL)
        self.root.option_add("*TCombobox*Listbox.selectForeground", "#04181c")

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
        ttk.Label(title_row, text="sACN / DMX Monitor").pack(anchor="w")

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
        toolbar = tk.Frame(left, bg=BG)
        toolbar.pack(fill="x", pady=(0, 4))
        self.colorize_btn = tk.Button(toolbar, text="Colorize", command=self._toggle_colorize,
                                       bg=PANEL2, fg=TEXT_MUTED, activebackground=PANEL2,
                                       relief="flat", font=(MONO, 9), padx=10, pady=4,
                                       highlightbackground=LINE, highlightthickness=1)
        self.colorize_btn.pack(side="left")

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
        add_row.pack(fill="x", padx=10, pady=(0, 6))
        self.new_fixture_name = tk.StringVar()
        entry = tk.Entry(add_row, textvariable=self.new_fixture_name, bg=PANEL2, fg=TEXT,
                          insertbackground=TEXT, relief="flat", font=(MONO, 10))
        entry.pack(side="left", fill="x", expand=True, ipady=3)
        entry.bind("<Return>", lambda e: self._add_fixture())
        tk.Button(add_row, text="+ Add", command=self._add_fixture, bg=PANEL2, fg=SIGNAL,
                  relief="flat", font=(MONO, 9), padx=8).pack(side="left", padx=(6, 0))

        tools_row = tk.Frame(list_panel, bg=PANEL)
        tools_row.pack(fill="x", padx=10, pady=(0, 8))
        self.presets_btn = tk.Button(tools_row, text="Presets...", command=self._open_preset_dialog,
                                      bg=PANEL2, fg=SIGNAL, relief="flat", font=(MONO, 9), padx=8)
        self.presets_btn.pack(side="left")
        tk.Button(tools_row, text="Save Setup", command=self._save_setup, bg=PANEL2, fg=SIGNAL,
                  relief="flat", font=(MONO, 9), padx=8).pack(side="left", padx=(6, 0))
        tk.Button(tools_row, text="Load Setup", command=self._load_setup, bg=PANEL2, fg=SIGNAL,
                  relief="flat", font=(MONO, 9), padx=8).pack(side="left", padx=(6, 0))

        self.fixture_listbox = tk.Listbox(list_panel, bg=PANEL2, fg=TEXT, selectbackground=SIGNAL,
                                           selectforeground="#04181c", relief="flat", height=6,
                                           font=(MONO, 10), highlightthickness=0, activestyle="none",
                                           selectmode=tk.EXTENDED, exportselection=False)
        self.fixture_listbox.pack(fill="x", padx=10, pady=(0, 6))
        self.fixture_listbox.bind("<<ListboxSelect>>", self._on_fixture_select)

        btn_row = tk.Frame(list_panel, bg=PANEL)
        btn_row.pack(fill="x", padx=10, pady=(0, 10))
        tk.Button(btn_row, text="Rename", command=self._rename_fixture, bg=PANEL2, fg=SIGNAL,
                  relief="flat", font=(MONO, 9), padx=8).pack(side="left")
        tk.Button(btn_row, text="Duplicate", command=self._duplicate_fixture, bg=PANEL2, fg=SIGNAL,
                  relief="flat", font=(MONO, 9), padx=8).pack(side="left", padx=(6, 0))
        tk.Button(btn_row, text="Remove Selected", command=self._remove_fixture,
                  bg=PANEL2, fg=DANGER, relief="flat", font=(MONO, 9),
                  padx=8).pack(side="left", padx=(6, 0))

        # ---- 2) fixture config ------------------------------------------
        self.config_panel = tk.Frame(parent, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
        self.config_panel.pack(fill="x", pady=(0, 10))
        self._render_config_panel_empty()

        # ---- 3) color preview -------------------------------------------
        preview_panel = tk.Frame(parent, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
        preview_panel.pack(fill="both", expand=True)
        preview_header = tk.Frame(preview_panel, bg=PANEL)
        preview_header.pack(fill="x", padx=10, pady=(10, 6))
        tk.Label(preview_header, text="Fixture Preview", bg=PANEL, fg=TEXT,
                 font=(MONO, 10, "bold")).pack(side="left")
        self.snap_btn = tk.Button(preview_header, text="Snap: Off", command=self._toggle_snap,
                                   bg=PANEL2, fg=TEXT_MUTED, relief="flat", font=(MONO, 9), padx=8)
        self.snap_btn.pack(side="right")
        self.undock_btn = tk.Button(preview_header, text="Undock", command=self._toggle_undock,
                                     bg=PANEL2, fg=TEXT_MUTED, relief="flat", font=(MONO, 9), padx=8)
        self.undock_btn.pack(side="right", padx=(0, 6))

        self.preview_host = preview_panel
        self.preview_canvas = self._create_preview_canvas(self.preview_host)
        self._render_preview_canvas()

    def _create_preview_canvas(self, parent, padx=10, pady=(0, 10)):
        """Canvas + vertical/horizontal scrollbars, reused for both the
        embedded sidebar location and the undocked window. The wrapper
        container is stashed on self so callers can destroy it wholesale
        later (forgetting just the inner canvas would leave an empty,
        still-packed container frame behind)."""
        container = tk.Frame(parent, bg=PANEL)
        container.pack(fill="both", expand=True, padx=padx, pady=pady)
        container.rowconfigure(0, weight=1)
        container.columnconfigure(0, weight=1)
        self.preview_canvas_container = container

        canvas = tk.Canvas(container, bg=PANEL, highlightthickness=0)
        vbar = ttk.Scrollbar(container, orient="vertical", command=canvas.yview)
        hbar = ttk.Scrollbar(container, orient="horizontal", command=canvas.xview)
        canvas.configure(yscrollcommand=vbar.set, xscrollcommand=hbar.set)

        canvas.grid(row=0, column=0, sticky="nsew")
        vbar.grid(row=0, column=1, sticky="ns")
        hbar.grid(row=1, column=0, sticky="ew")

        def on_wheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        def on_shift_wheel(event):
            canvas.xview_scroll(int(-1 * (event.delta / 120)), "units")

        canvas.bind("<Enter>", lambda e: (canvas.bind_all("<MouseWheel>", on_wheel),
                                           canvas.bind_all("<Shift-MouseWheel>", on_shift_wheel)))
        canvas.bind("<Leave>", lambda e: (canvas.unbind_all("<MouseWheel>"),
                                           canvas.unbind_all("<Shift-MouseWheel>")))
        return canvas

    def _update_scrollregion(self):
        c = self.preview_canvas
        if not self.fixtures:
            c.configure(scrollregion=(0, 0, 0, 0))
            return
        max_x = max((fx.preview_x or 0) + fx.preview_w for fx in self.fixtures) + 40
        max_y = max((fx.preview_y or 0) + fx.preview_h for fx in self.fixtures) + 60
        c.configure(scrollregion=(0, 0, max_x, max_y))

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
        to_remove = [self.fixtures[i] for i in sel]
        self.fixtures = [fx for fx in self.fixtures if fx not in to_remove]
        self.selected_fixture_index = None
        self._refresh_fixture_list()
        self._render_config_panel_empty()

    def _refresh_fixture_list(self):
        self.fixture_listbox.delete(0, tk.END)
        for fx in self.fixtures:
            self.fixture_listbox.insert(tk.END, fx.name)
        self._render_preview_canvas()

    def _on_fixture_select(self, _event=None):
        sel = self.fixture_listbox.curselection()
        if len(sel) == 1:
            self._select_fixture(sel[0])
        elif len(sel) > 1:
            self.selected_fixture_index = None
            self._render_config_panel_multi(len(sel))
            for i in sel:
                self._raise_fixture(self.fixtures[i])
        else:
            self.selected_fixture_index = None
            self._render_config_panel_empty()

    def _select_fixture(self, idx):
        self.selected_fixture_index = idx
        self._render_config_panel()
        if 0 <= idx < len(self.fixtures):
            self._raise_fixture(self.fixtures[idx])

    def _select_fixture_from_canvas(self, fx):
        """Clicking a fixture directly in the preview area selects it in
        the fixture list too, keeping both views in sync."""
        try:
            idx = self.fixtures.index(fx)
        except ValueError:
            return
        self.fixture_listbox.selection_clear(0, tk.END)
        self.fixture_listbox.selection_set(idx)
        self.fixture_listbox.activate(idx)
        self.fixture_listbox.see(idx)
        self._select_fixture(idx)

    def _raise_fixture(self, fx):
        """Bring a fixture's canvas items to the top of the stacking order
        so it can't stay permanently hidden behind another fixture."""
        if id(fx) in self.fixture_items:
            self.preview_canvas.tag_raise(f"fx_{id(fx)}")

    def _rename_fixture(self):
        sel = self.fixture_listbox.curselection()
        if not sel:
            return
        if len(sel) > 1:
            messagebox.showinfo("Select One Fixture", "Rename works on a single fixture at a "
                                                        "time — select just one.")
            return
        idx = sel[0]
        fx = self.fixtures[idx]
        new_name = simpledialog.askstring("Rename Fixture", "New Name:",
                                           initialvalue=fx.name, parent=self.root)
        if new_name and new_name.strip():
            fx.name = new_name.strip()
            self._refresh_fixture_list()
            self.fixture_listbox.selection_set(idx)
            if self.selected_fixture_index == idx:
                self._render_config_panel()

    def _duplicate_fixture(self):
        sel = self.fixture_listbox.curselection()
        if not sel:
            return
        originals = [self.fixtures[i] for i in sel]
        clones = [fx.clone() for fx in originals]
        self.fixtures.extend(clones)
        self._refresh_fixture_list()
        self.fixture_listbox.selection_clear(0, tk.END)
        for fx in clones:
            self.fixture_listbox.selection_set(self.fixtures.index(fx))
        if len(clones) == 1:
            self._select_fixture(self.fixtures.index(clones[0]))
        else:
            self.selected_fixture_index = None
            self._render_config_panel_multi(len(clones))

    # ---------------------------------------------------------- presets
    def _save_preset(self, fx):
        name = simpledialog.askstring("Save Preset", "Preset Name:", parent=self.root)
        if not name or not name.strip():
            return
        self.presets[name.strip()] = {
            "channel_count": fx.channel_count(),
            "channels": copy.deepcopy(fx.channels),
            "pixel_count": fx.pixel_count,
            "repeat_from": fx.repeat_from,
            "repeat_till": fx.repeat_till,
            "pixel_rows": fx.pixel_rows,
            "pixel_cols": fx.pixel_cols,
        }
        messagebox.showinfo("Preset Saved", f"Saved channel layout as '{name.strip()}'.")

    def _open_preset_dialog(self):
        if not self.presets:
            messagebox.showinfo("No Presets Yet",
                                 "Save a channel layout as a preset first, in the Fixture "
                                 "Config panel, then it'll show up here.")
            return
        dlg = tk.Toplevel(self.root)
        dlg.title("Manage Presets")
        dlg.configure(bg=PANEL)
        dlg.transient(self.root)

        dlg_w, dlg_h = 580, 440
        btn = self.presets_btn
        btn.update_idletasks()
        bx, by = btn.winfo_rootx(), btn.winfo_rooty()
        y = by - dlg_h - 8
        if y < 0:
            y = by + btn.winfo_height() + 8
        x = max(0, bx)
        dlg.geometry(f"{dlg_w}x{dlg_h}+{x}+{y}")

        main = tk.Frame(dlg, bg=PANEL)
        main.pack(fill="both", expand=True)

        left = tk.Frame(main, bg=PANEL)
        left.pack(side="left", fill="both", expand=True, padx=(12, 6), pady=14)

        right = tk.Frame(main, bg=PANEL2, highlightbackground=LINE, highlightthickness=1)
        right.pack(side="left", fill="both", expand=True, padx=(6, 12), pady=14)

        tk.Label(left, text="Fixture Name", bg=PANEL, fg=TEXT_MUTED, font=(MONO, 9)).pack(
            anchor="w", pady=(0, 2))
        name_var = tk.StringVar(value="New Fixture")
        tk.Entry(left, textvariable=name_var, bg=PANEL2, fg=TEXT, insertbackground=TEXT,
                  relief="flat", font=(MONO, 10)).pack(fill="x", ipady=3)

        tk.Label(left, text="Preset", bg=PANEL, fg=TEXT_MUTED, font=(MONO, 9)).pack(
            anchor="w", pady=(14, 2))
        lb = tk.Listbox(left, bg=PANEL2, fg=TEXT, selectbackground=SIGNAL,
                         selectforeground="#04181c", relief="flat", font=(MONO, 10),
                         highlightthickness=0, activestyle="none")
        for pname in self.presets:
            lb.insert(tk.END, pname)
        lb.pack(fill="both", expand=True, pady=(0, 10))

        btn_row = tk.Frame(left, bg=PANEL)
        btn_row.pack(fill="x")

        def render_details(_event=None):
            for w in right.winfo_children():
                w.destroy()
            sel = lb.curselection()
            if not sel:
                tk.Label(right, text="Select a preset to see details.", bg=PANEL2, fg=TEXT_MUTED,
                         font=(MONO, 9), wraplength=220, justify="left").pack(
                    padx=12, pady=12, anchor="w")
                return
            pname = lb.get(sel[0])
            preset = self.presets[pname]

            tk.Label(right, text=pname, bg=PANEL2, fg=TEXT, font=(MONO, 10, "bold")).pack(
                anchor="w", padx=12, pady=(12, 8))

            rows = max(1, preset["pixel_rows"])
            cols = max(1, preset["pixel_cols"])
            cell = max(8, min(24, 140 // max(rows, cols)))
            gap = 2
            cw = cols * cell + (cols - 1) * gap
            ch = rows * cell + (rows - 1) * gap
            grid_canvas = tk.Canvas(right, width=cw, height=ch, bg=PANEL2, highlightthickness=0)
            for rr in range(rows):
                for cc in range(cols):
                    x0 = cc * (cell + gap)
                    y0 = rr * (cell + gap)
                    grid_canvas.create_rectangle(x0, y0, x0 + cell, y0 + cell,
                                                  fill=SIGNAL, outline=LINE)
            grid_canvas.pack(padx=12, pady=(0, 10), anchor="w")

            info_lines = [
                f"Channels: {preset['channel_count']}",
                f"Pixels: {preset['pixel_count']}",
                f"Layout: {rows} Row{'s' if rows != 1 else ''} \u00d7 {cols} Col{'s' if cols != 1 else ''}",
            ]
            if preset.get("repeat_from") and preset.get("repeat_till"):
                info_lines.append(f"Repeats: CH {preset['repeat_from']}\u2013{preset['repeat_till']}")
            for line in info_lines:
                tk.Label(right, text=line, bg=PANEL2, fg=TEXT_MUTED, font=(MONO, 9)).pack(
                    anchor="w", padx=12)

            tk.Label(right, text="Channel Layout", bg=PANEL2, fg=TEXT, font=(MONO, 9, "bold")).pack(
                anchor="w", padx=12, pady=(10, 2))
            for line in self._describe_preset_channels(preset):
                tk.Label(right, text=line, bg=PANEL2, fg=TEXT_MUTED, font=(MONO, 9)).pack(
                    anchor="w", padx=12)

        lb.bind("<<ListboxSelect>>", render_details)
        lb.selection_set(0)
        render_details()

        def load_preset():
            sel = lb.curselection()
            if not sel:
                return
            pname = lb.get(sel[0])
            preset = self.presets[pname]
            fx = Fixture(name_var.get().strip() or pname, channel_count=preset["channel_count"])
            fx.channels = copy.deepcopy(preset["channels"])
            fx.pixel_count = preset["pixel_count"]
            fx.repeat_from = preset["repeat_from"]
            fx.repeat_till = preset["repeat_till"]
            fx.pixel_rows = preset["pixel_rows"]
            fx.pixel_cols = preset["pixel_cols"]
            self.fixtures.append(fx)
            self._refresh_fixture_list()
            self.fixture_listbox.selection_clear(0, tk.END)
            self.fixture_listbox.selection_set(tk.END)
            self._select_fixture(len(self.fixtures) - 1)
            dlg.destroy()

        def delete_preset():
            sel = lb.curselection()
            if not sel:
                return
            pname = lb.get(sel[0])
            if messagebox.askyesno("Delete Preset", f"Delete preset '{pname}'?", parent=dlg):
                del self.presets[pname]
                lb.delete(sel[0])
                render_details()
                if not self.presets:
                    dlg.destroy()

        tk.Button(btn_row, text="Load Preset", command=load_preset, bg=PANEL2, fg=SIGNAL,
                  relief="flat", font=(MONO, 9), padx=8).pack(side="left")
        tk.Button(btn_row, text="Delete Preset", command=delete_preset, bg=PANEL2, fg=DANGER,
                  relief="flat", font=(MONO, 9), padx=8).pack(side="left", padx=(6, 0))

    @staticmethod
    def _describe_preset_channels(preset):
        lines = []
        channels = preset["channels"]
        i = 0
        while i < len(channels):
            c = channels[i]
            if c["role"] == "fine":
                i += 1
                continue
            if c["role"] == "coarse":
                label = f"CH {i + 1}\u2013{c['pair_index'] + 1}"
                type_txt = FIXTURE_TYPE_LABEL.get(c["type"], "Unassigned") + " (16-bit)"
            else:
                label = f"CH {i + 1}"
                type_txt = FIXTURE_TYPE_LABEL.get(c["type"], "Unassigned")
            lines.append(f"{label}: {type_txt}")
            i += 1
        return lines

    # ------------------------------------------------------ setup file I/O
    def _save_setup(self):
        path = filedialog.asksaveasfilename(
            defaultextension=".json",
            filetypes=[("Signal Fixture Setup", "*.json")],
            title="Save Fixture Setup",
        )
        if not path:
            return
        data = {"fixtures": [fx.to_dict() for fx in self.fixtures]}
        try:
            with open(path, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as exc:
            messagebox.showerror("Could Not Save", str(exc))
            return
        messagebox.showinfo("Setup Saved", f"Saved {len(self.fixtures)} fixture(s) to {path}.")

    def _load_setup(self):
        path = filedialog.askopenfilename(
            defaultextension=".json",
            filetypes=[("Signal Fixture Setup", "*.json")],
            title="Load Fixture Setup",
        )
        if not path:
            return
        try:
            with open(path, "r") as f:
                data = json.load(f)
        except Exception as exc:
            messagebox.showerror("Could Not Load", str(exc))
            return
        self.fixtures = [Fixture.from_dict(d) for d in data.get("fixtures", [])]
        self.selected_fixture_index = None
        self._refresh_fixture_list()
        self._render_config_panel_empty()

    # ----------------------------------------------------- fixture config
    def _clear_config_panel(self):
        for w in self.config_panel.winfo_children():
            w.destroy()

    def _render_config_panel_empty(self):
        # Never leave the panel truly blank: render the same form against a
        # neutral placeholder fixture, fully greyed out and non-interactive.
        placeholder = getattr(self, "_placeholder_fixture", None)
        if placeholder is None:
            placeholder = Fixture("No Fixture Selected")
            self._placeholder_fixture = placeholder
        self._render_config_panel_for(placeholder, interactive=False)

    def _render_config_panel_multi(self, count):
        self._clear_config_panel()
        tk.Label(self.config_panel, text="FIXTURE CONFIG", bg=PANEL, fg=TEXT,
                 font=(MONO, 10, "bold")).pack(anchor="w", padx=10, pady=(10, 4))
        tk.Label(self.config_panel,
                 text=f"{count} fixtures selected. Rename, Duplicate and Remove Selected apply "
                      "to the selection — select just one fixture to edit its channel layout.",
                 bg=PANEL, fg=TEXT_MUTED, font=(MONO, 9), wraplength=300,
                 justify="left").pack(anchor="w", padx=10, pady=(0, 10))

    def _render_config_panel(self):
        if self.selected_fixture_index is None or self.selected_fixture_index >= len(self.fixtures):
            self._render_config_panel_empty()
            return
        fx = self.fixtures[self.selected_fixture_index]
        self._render_config_panel_for(fx, interactive=True)

    def _render_config_panel_for(self, fx, interactive):
        self._clear_config_panel()
        state_normal = "normal" if interactive else "disabled"
        state_readonly = "readonly" if interactive else "disabled"
        title_fg = TEXT if interactive else TEXT_MUTED

        tk.Label(self.config_panel, text=f"CONFIG \u2014 {fx.name}", bg=PANEL, fg=title_fg,
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
                   buttonbackground=PANEL2, relief="flat", state=state_normal).grid(
            row=1, column=0, sticky="w")
        tk.Spinbox(row1, from_=1, to=512, textvariable=start_var, width=6, bg=PANEL2, fg=TEXT,
                   buttonbackground=PANEL2, relief="flat", state=state_normal).grid(
            row=1, column=1, sticky="w", padx=(10, 0))
        tk.Spinbox(row1, from_=1, to=64, textvariable=count_var, width=6, bg=PANEL2, fg=TEXT,
                   buttonbackground=PANEL2, relief="flat", state=state_normal).grid(
            row=1, column=2, sticky="w", padx=(10, 0))

        def apply_basics():
            try:
                fx.universe = int(uni_var.get())
                fx.start_channel = int(start_var.get())
                fx.set_channel_count(int(count_var.get()))
                fx.pixel_count = max(1, int(pixel_count_var.get()))
                rf = repeat_from_var.get().strip()
                rt = repeat_till_var.get().strip()
                fx.repeat_from = int(rf) if rf else None
                fx.repeat_till = int(rt) if rt else None
                fx.pixel_rows = max(1, int(rows_var.get()))
                fx.pixel_cols = max(1, int(cols_var.get()))
            except ValueError:
                messagebox.showerror("Invalid Value", "Universe, start channel, channel count "
                                                        "and pixel fields must be numbers.")
                return
            if fx.repeat_from and fx.repeat_till:
                if fx.repeat_from < 1 or fx.repeat_till > fx.channel_count() or fx.repeat_till < fx.repeat_from:
                    messagebox.showerror("Invalid Repeat Range",
                                          "Repeat From/Till must fall within the channel layout, "
                                          "with From \u2264 Till. Repeat range was cleared.")
                    fx.repeat_from = None
                    fx.repeat_till = None
            # Deferred: rebuilding this panel destroys the very Button whose
            # command is currently running. Doing that synchronously can
            # abort mid-rebuild on some Tk builds, leaving the panel empty.
            self.root.after_idle(self._render_config_panel)
            self.root.after_idle(self._render_preview_canvas)

        tk.Button(row1, text="Apply", command=apply_basics, bg=PANEL2, fg=SIGNAL, relief="flat",
                  font=(MONO, 9), padx=8, state=state_normal).grid(row=1, column=3, padx=(10, 0))
        tk.Button(row1, text="Save As Preset", command=lambda: self._save_preset(fx), bg=PANEL2,
                  fg=SIGNAL, relief="flat", font=(MONO, 9), padx=8,
                  state=state_normal).grid(row=1, column=4, padx=(10, 0))

        row2 = tk.Frame(self.config_panel, bg=PANEL)
        row2.pack(fill="x", padx=10, pady=(0, 8))
        for col, txt in enumerate(["Pixels", "Repeat From", "Repeat Till", "Rows", "Cols"]):
            tk.Label(row2, text=txt, bg=PANEL, fg=TEXT_MUTED, font=(MONO, 9)).grid(
                row=0, column=col, sticky="w", padx=(0 if col == 0 else 10, 0))

        pixel_count_var = tk.StringVar(value=str(fx.pixel_count))
        repeat_from_var = tk.StringVar(value=str(fx.repeat_from) if fx.repeat_from else "")
        repeat_till_var = tk.StringVar(value=str(fx.repeat_till) if fx.repeat_till else "")
        rows_var = tk.StringVar(value=str(fx.pixel_rows))
        cols_var = tk.StringVar(value=str(fx.pixel_cols))

        tk.Spinbox(row2, from_=1, to=64, textvariable=pixel_count_var, width=6, bg=PANEL2, fg=TEXT,
                   buttonbackground=PANEL2, relief="flat", state=state_normal).grid(
            row=1, column=0, sticky="w")
        tk.Entry(row2, textvariable=repeat_from_var, width=8, bg=PANEL2, fg=TEXT,
                  insertbackground=TEXT, relief="flat", state=state_normal).grid(
            row=1, column=1, sticky="w", padx=(10, 0))
        tk.Entry(row2, textvariable=repeat_till_var, width=8, bg=PANEL2, fg=TEXT,
                  insertbackground=TEXT, relief="flat", state=state_normal).grid(
            row=1, column=2, sticky="w", padx=(10, 0))
        tk.Spinbox(row2, from_=1, to=16, textvariable=rows_var, width=6, bg=PANEL2, fg=TEXT,
                   buttonbackground=PANEL2, relief="flat", state=state_normal).grid(
            row=1, column=3, sticky="w", padx=(10, 0))
        tk.Spinbox(row2, from_=1, to=16, textvariable=cols_var, width=6, bg=PANEL2, fg=TEXT,
                   buttonbackground=PANEL2, relief="flat", state=state_normal).grid(
            row=1, column=4, sticky="w", padx=(10, 0))
        tk.Label(self.config_panel, text="Leave Repeat From/Till empty for a single-pixel fixture.",
                 bg=PANEL, fg=TEXT_MUTED, font=(MONO, 8)).pack(anchor="w", padx=10, pady=(0, 6))

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
            tk.Label(row, text=ch_label, bg=PANEL2, fg=(TEXT if interactive else TEXT_MUTED),
                     font=(MONO, 9), width=11, anchor="w").pack(side="left", padx=(8, 4), pady=6)

            type_var = tk.StringVar(value=FIXTURE_TYPE_LABEL.get(c["type"], ""))
            combo = ttk.Combobox(row, textvariable=type_var, state=state_readonly, width=9,
                                  values=FIXTURE_TYPE_OPTIONS)
            combo.pack(side="left", padx=4)

            def make_type_handler(idx=i, var=type_var):
                def handler(_event=None):
                    fx.set_type(idx, FIXTURE_TYPE_KEY[var.get()])
                    # Deferred for the same reason as apply_basics above:
                    # this callback is running ON the combobox that's about
                    # to be destroyed and recreated by the rebuild.
                    self.root.after_idle(self._render_config_panel)
                    self.root.after_idle(self._render_preview_canvas)
                return handler
            combo.bind("<<ComboboxSelected>>", make_type_handler())

            can16 = interactive and (c["role"] == "coarse" or fx.can_enable_16(i))
            is_on = c["role"] == "coarse"
            bit_var = tk.BooleanVar(value=is_on)
            # indicatoron=False avoids a known Windows/Tk quirk where a
            # custom selectcolor on a native-style checkbutton indicator
            # can render the checked/unchecked glyph backwards; drawing it
            # as a plain toggle pill instead makes the state unambiguous.
            cb = tk.Checkbutton(row, text="16-bit", variable=bit_var, indicatoron=False,
                                 bg=(SIGNAL if is_on else PANEL2),
                                 fg=("#04181c" if is_on else TEXT_MUTED),
                                 activebackground=(SIGNAL if is_on else LINE),
                                 selectcolor=SIGNAL, disabledforeground=TEXT_MUTED,
                                 relief="flat", bd=1, padx=6, pady=1, font=(MONO, 8),
                                 state=("normal" if can16 else "disabled"))

            def make_bit_handler(idx=i):
                def handler():
                    fx.toggle_16(idx)
                    self.root.after_idle(self._render_config_panel)
                    self.root.after_idle(self._render_preview_canvas)
                return handler
            cb.config(command=make_bit_handler())
            cb.pack(side="left", padx=4)

            i += 1

    # ----------------------------------------------------- color preview
    SNAP_THRESHOLD = 8
    HANDLE_DIRS = ["n", "s", "e", "w", "ne", "nw", "se", "sw"]
    HANDLE_CURSORS = {
        "n": "sb_v_double_arrow", "s": "sb_v_double_arrow",
        "e": "sb_h_double_arrow", "w": "sb_h_double_arrow",
        "ne": "size_ne_sw", "sw": "size_ne_sw",
        "nw": "size_nw_se", "se": "size_nw_se",
    }

    def _ensure_layout(self, fx, idx):
        if fx.preview_x is None:
            col, row = idx % 3, idx // 3
            fx.preview_x = 16 + col * 150
            fx.preview_y = 16 + row * 150

    def _render_preview_canvas(self):
        c = self.preview_canvas
        c.delete("all")
        self.fixture_items = {}
        for idx, fx in enumerate(self.fixtures):
            self._ensure_layout(fx, idx)
            self._create_fixture_items(fx)
        if not self.fixtures:
            c.create_text(16, 16, text="No Fixtures yet — add one above.", fill=TEXT_MUTED,
                           font=(MONO, 9), anchor="nw")
        self._update_scrollregion()

    def _create_fixture_items(self, fx):
        """Create all canvas items for a fixture once. Dragging/resizing
        afterwards only repositions these existing items (via canvas.coords),
        it never deletes/recreates them — deleting the item currently under
        the mouse mid-drag breaks Tk's tracking of that drag gesture."""
        c = self.preview_canvas
        tag = f"fx_{id(fx)}"
        body_tag = f"body_{id(fx)}"

        items = {"cells": [], "handles": {}}
        items["body"] = c.create_rectangle(0, 0, 1, 1, outline=LINE, width=1, fill=PANEL2,
                                            tags=(tag, body_tag))

        rows = max(1, fx.pixel_rows)
        cols = max(1, fx.pixel_cols)
        for _ in range(rows * cols):
            rect = c.create_rectangle(0, 0, 1, 1, fill="#1a1a1a", outline=LINE,
                                       tags=(tag, body_tag))
            items["cells"].append(rect)

        items["text"] = c.create_text(0, 0, text=fx.name, fill=TEXT_MUTED,
                                       font=(MONO, 9), tags=(tag,))

        for d in self.HANDLE_DIRS:
            htag = f"handle_{id(fx)}_{d}"
            h = c.create_rectangle(0, 0, 1, 1, fill=SIGNAL, outline=SIGNAL, tags=(tag, htag))
            items["handles"][d] = h
            c.tag_bind(htag, "<ButtonPress-1>", lambda e, fx=fx, d=d: self._on_resize_press(e, fx, d))
            c.tag_bind(htag, "<Enter>", lambda e, d=d: c.config(cursor=self.HANDLE_CURSORS[d]))
            c.tag_bind(htag, "<Leave>", lambda e: c.config(cursor=""))

        c.tag_bind(body_tag, "<ButtonPress-1>", lambda e, fx=fx: self._on_move_press(e, fx))
        c.tag_bind(body_tag, "<Enter>", lambda e: c.config(cursor="fleur"))
        c.tag_bind(body_tag, "<Leave>", lambda e: c.config(cursor=""))

        self.fixture_items[id(fx)] = items
        self._layout_fixture_items(fx)

    def _layout_fixture_items(self, fx):
        """Reposition an existing fixture's canvas items to match its
        current preview_x/y/w/h — no items are deleted or recreated."""
        c = self.preview_canvas
        items = self.fixture_items.get(id(fx))
        if not items:
            return
        x, y, w, h = fx.preview_x, fx.preview_y, fx.preview_w, fx.preview_h
        c.coords(items["body"], x, y, x + w, y + h)

        rows = max(1, fx.pixel_rows)
        cols = max(1, fx.pixel_cols)
        gap = 2
        cell_w = (w - (cols - 1) * gap) / cols
        cell_h = (h - (rows - 1) * gap) / rows
        i = 0
        for rr in range(rows):
            for cc in range(cols):
                if i >= len(items["cells"]):
                    break
                cx0 = x + cc * (cell_w + gap)
                cy0 = y + rr * (cell_h + gap)
                c.coords(items["cells"][i], cx0, cy0, cx0 + cell_w, cy0 + cell_h)
                i += 1

        c.coords(items["text"], x + w / 2, y + h + 12)

        hs = 7
        positions = {
            "nw": (x, y), "n": (x + w / 2, y), "ne": (x + w, y),
            "w": (x, y + h / 2), "e": (x + w, y + h / 2),
            "sw": (x, y + h), "s": (x + w / 2, y + h), "se": (x + w, y + h),
        }
        for d, (hx, hy) in positions.items():
            c.coords(items["handles"][d], hx - hs / 2, hy - hs / 2, hx + hs / 2, hy + hs / 2)

    def _other_edges(self, fx):
        edges_x, edges_y = [], []
        for other in self.fixtures:
            if other is fx or other.preview_x is None:
                continue
            edges_x += [other.preview_x, other.preview_x + other.preview_w]
            edges_y += [other.preview_y, other.preview_y + other.preview_h]
        return edges_x, edges_y

    def _snap_position(self, fx, x, y, w, h):
        edges_x, edges_y = self._other_edges(fx)
        for ex in edges_x:
            if abs(x - ex) <= self.SNAP_THRESHOLD:
                x = ex
                break
            if abs((x + w) - ex) <= self.SNAP_THRESHOLD:
                x = ex - w
                break
        for ey in edges_y:
            if abs(y - ey) <= self.SNAP_THRESHOLD:
                y = ey
                break
            if abs((y + h) - ey) <= self.SNAP_THRESHOLD:
                y = ey - h
                break
        return x, y

    def _snap_resize(self, fx, mode, x, y, w, h):
        edges_x, edges_y = self._other_edges(fx)
        if "e" in mode:
            for ex in edges_x:
                if abs((x + w) - ex) <= self.SNAP_THRESHOLD:
                    w = ex - x
                    break
        if "w" in mode:
            for ex in edges_x:
                if abs(x - ex) <= self.SNAP_THRESHOLD:
                    w += (x - ex)
                    x = ex
                    break
        if "s" in mode:
            for ey in edges_y:
                if abs((y + h) - ey) <= self.SNAP_THRESHOLD:
                    h = ey - y
                    break
        if "n" in mode:
            for ey in edges_y:
                if abs(y - ey) <= self.SNAP_THRESHOLD:
                    h += (y - ey)
                    y = ey
                    break
        return x, y, max(40, w), max(40, h)

    # dragging uses canvas-wide binds (established on press, torn down on
    # release) rather than per-item tag_bind for motion, so the gesture
    # survives even though items are repositioned every frame.
    def _on_move_press(self, event, fx):
        self._select_fixture_from_canvas(fx)
        self._drag_fx = fx
        self._drag_mode = "move"
        self._drag_start_mouse = (event.x, event.y)
        self._drag_start_geom = (fx.preview_x, fx.preview_y, fx.preview_w, fx.preview_h)
        self.preview_canvas.bind("<B1-Motion>", self._on_drag_motion)
        self.preview_canvas.bind("<ButtonRelease-1>", self._on_drag_end)

    def _on_resize_press(self, event, fx, direction):
        self._select_fixture_from_canvas(fx)
        self._drag_fx = fx
        self._drag_mode = direction
        self._drag_start_mouse = (event.x, event.y)
        self._drag_start_geom = (fx.preview_x, fx.preview_y, fx.preview_w, fx.preview_h)
        self.preview_canvas.bind("<B1-Motion>", self._on_drag_motion)
        self.preview_canvas.bind("<ButtonRelease-1>", self._on_drag_end)

    def _on_drag_motion(self, event):
        fx = self._drag_fx
        if fx is None:
            return
        dx = event.x - self._drag_start_mouse[0]
        dy = event.y - self._drag_start_mouse[1]
        ox, oy, ow, oh = self._drag_start_geom
        mode = self._drag_mode

        if mode == "move":
            new_x, new_y, new_w, new_h = ox + dx, oy + dy, ow, oh
            if self.snap_enabled:
                new_x, new_y = self._snap_position(fx, new_x, new_y, new_w, new_h)
        else:
            new_x, new_y, new_w, new_h = ox, oy, ow, oh
            if "n" in mode:
                new_y = oy + dy
                new_h = oh - dy
            if "s" in mode:
                new_h = oh + dy
            if "w" in mode:
                new_x = ox + dx
                new_w = ow - dx
            if "e" in mode:
                new_w = ow + dx
            if new_w < 40:
                if "w" in mode:
                    new_x = ox + ow - 40
                new_w = 40
            if new_h < 40:
                if "n" in mode:
                    new_y = oy + oh - 40
                new_h = 40
            if self.snap_enabled:
                new_x, new_y, new_w, new_h = self._snap_resize(fx, mode, new_x, new_y, new_w, new_h)

        fx.preview_x, fx.preview_y, fx.preview_w, fx.preview_h = new_x, new_y, new_w, new_h
        self._layout_fixture_items(fx)
        self._update_scrollregion()

    def _on_drag_end(self, _event):
        self.preview_canvas.unbind("<B1-Motion>")
        self.preview_canvas.unbind("<ButtonRelease-1>")
        self._drag_fx = None
        self._drag_mode = None

    def _toggle_snap(self):
        self.snap_enabled = not self.snap_enabled
        self.snap_btn.config(text=f"Snap: {'On' if self.snap_enabled else 'Off'}",
                              fg=(SIGNAL if self.snap_enabled else TEXT_MUTED))

    def _toggle_undock(self):
        if self.preview_undocked:
            self._redock_preview()
        else:
            self._undock_preview()

    def _undock_preview(self):
        # Destroy the whole embedded container (canvas + scrollbars), not
        # just the canvas. Forgetting only the canvas left an empty wrapper
        # frame still packed in the sidebar — the "extra blank preview area"
        # that piled up on repeated dock/undock cycles.
        old_container = self.preview_canvas_container
        if old_container is not None:
            old_container.destroy()

        self._embedded_canvas_placeholder = tk.Frame(self.preview_host, bg=PANEL)
        self._embedded_canvas_placeholder.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        tk.Label(self._embedded_canvas_placeholder,
                 text="Preview undocked — see the separate window.",
                 bg=PANEL, fg=TEXT_MUTED, font=(MONO, 9), wraplength=280,
                 justify="left").pack(expand=True)

        win = tk.Toplevel(self.root)
        win.title("Signal \u2014 Fixture Preview")
        win.configure(bg=PANEL)
        win.geometry("900x600")
        win.minsize(400, 300)
        win.protocol("WM_DELETE_WINDOW", self._redock_preview)
        self.preview_window = win

        self.preview_canvas = self._create_preview_canvas(win, padx=12, pady=12)

        self.preview_undocked = True
        self.undock_btn.config(text="Redock", fg=SIGNAL)
        self._render_preview_canvas()

    def _redock_preview(self):
        if self.preview_window is not None:
            win = self.preview_window
            self.preview_window = None
            # Destroying the Toplevel also destroys its preview container
            # (a child of it), so no separate cleanup needed for that side.
            win.destroy()
        if self._embedded_canvas_placeholder is not None:
            self._embedded_canvas_placeholder.destroy()
            self._embedded_canvas_placeholder = None

        self.preview_canvas = self._create_preview_canvas(self.preview_host)

        self.preview_undocked = False
        self.undock_btn.config(text="Undock", fg=TEXT_MUTED)
        self._render_preview_canvas()

    def _update_fixture_previews(self):
        for fx in self.fixtures:
            items = self.fixture_items.get(id(fx))
            if not items:
                continue
            rect_ids = items["cells"]
            dmx_values = None
            if self.listening and self.range_start <= fx.universe <= self.range_end:
                with self.data_lock:
                    dmx_values = self.latest_data.get(fx.universe)
            colors = fx.compute_pixel_colors(dmx_values)
            for p, rect in enumerate(rect_ids):
                if colors and p < len(colors):
                    hexcolor = "#%02x%02x%02x" % colors[p]
                    self.preview_canvas.itemconfig(rect, fill=hexcolor, outline=hexcolor)
                else:
                    self.preview_canvas.itemconfig(rect, fill="#1a1a1a", outline=LINE)

    # ------------------------------------------------------- DMX grid view
    def _toggle_colorize(self):
        self.colorize_enabled = not self.colorize_enabled
        self.colorize_btn.config(fg=(SIGNAL if self.colorize_enabled else TEXT_MUTED))
        grid = self.grids.get(self.active_universe)
        if grid is not None:
            with self.data_lock:
                values = self.latest_data.get(self.active_universe)
            channel_types = self._build_channel_type_map(self.active_universe) if self.colorize_enabled else None
            if values is not None:
                grid.update_values(values, channel_types)

    def _build_status_strip(self):
        strip = tk.Frame(self.root, bg=PANEL)
        strip.pack(fill="x", side="bottom")
        self.status_var = tk.StringVar(value="Universe 1  •  512/512 Channels  •  No Signal")
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
            messagebox.showerror("Invalid Range", "Universe range must be numbers.")
            return
        if e < s:
            e = s
        if e - s + 1 > MAX_UNIVERSES_SHOWN:
            e = s + MAX_UNIVERSES_SHOWN - 1
            self.end_var.set(str(e))
            messagebox.showinfo("Range Limited", f"Showing max {MAX_UNIVERSES_SHOWN} universes at once.")
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
                "Missing Dependency",
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
            messagebox.showerror("Could Not Start Listening", str(exc))
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
    def _build_channel_type_map(self, universe):
        mapping = {}
        for fx in self.fixtures:
            if fx.universe != universe:
                continue
            for rel, type_key in fx.channel_type_map().items():
                abs_ch = fx.start_channel - 1 + rel
                if 0 <= abs_ch < CHANNELS:
                    mapping[abs_ch] = type_key
        return mapping

    def _refresh_loop(self):
        grid = self.grids.get(self.active_universe)
        if grid is not None:
            with self.data_lock:
                values = self.latest_data.get(self.active_universe)
            if values is not None:
                channel_types = self._build_channel_type_map(self.active_universe) if self.colorize_enabled else None
                grid.update_values(values, channel_types)
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
        rate_txt = f"{rate} Packets/Sec" if self.listening else "No Signal"
        adapter_label = self.adapter_var.get()
        self.status_var.set(
            f"Universe {self.active_universe}  •  512/512 Channels  •  {rate_txt}  •  {adapter_label}"
        )

    def _on_close(self):
        if self.listening:
            self._stop_receiver()
        if self.preview_window is not None:
            self.preview_window.destroy()
        self.root.destroy()


def main():
    if psutil is None:
        print("Warning: psutil not installed — only loopback will be listed as an adapter.")
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
