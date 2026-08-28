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
        width = self.label_w + COLS * self.cell_w
        height = self.header_h + ROWS * self.cell_h

        self.canvas = tk.Canvas(self, width=width, height=height, bg=PANEL,
                                 highlightthickness=1, highlightbackground=LINE)
        self.canvas.pack(fill="both", expand=True, padx=10, pady=10)

        self.cell_ids = {}  # channel index (0-based) -> (rect_id, text_id)
        self._draw_static()

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


class App:
    def __init__(self, root):
        self.root = root
        root.title("Signal — sACN Monitor")
        root.configure(bg=BG)
        root.geometry("980x680")

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

        self._build_topbar()
        self._build_tabs()
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

    def _build_tabs(self):
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=10, pady=(0, 4))
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)

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
