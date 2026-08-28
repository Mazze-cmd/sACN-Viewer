# Signal — sACN / DMX Monitor

A small Windows desktop app that actually listens for sACN (E1.31) traffic
on the network and displays incoming DMX values live, in a 10-column grid
per universe.

## What it does
- Turns sACN listening on/off
- Lets you pick which local network adapter to listen on (including loopback)
- Lets you set a universe range to monitor
- Shows all 512 channels per universe as a grid (10 channels per row),
  updating live as packets arrive

## 1. Run it first (recommended before building an .exe)

On a Windows machine with Python 3.9+ installed:

```
pip install -r requirements.txt
python app.py
```

Turn a real sACN source on your network (a lighting console, ETC Nomad,
QLC+, an sACN test transmitter, etc.) and confirm values show up. This is
worth doing before packaging — it's much easier to debug a network/firewall
issue as a running script than inside a compiled .exe.

**Windows Firewall:** the first time you run it, Windows will likely prompt
to allow the app through the firewall for private networks — allow it, or
sACN's multicast packets won't reach the app.

**Multiple network adapters:** sACN join happens on the adapter you select
in the dropdown. If you don't see data, try switching adapters — consoles
often only send multicast on one NIC (e.g. a wired interface, not Wi-Fi).

## 2. Build the .exe

Also on Windows, in the same folder:

```
pip install -r requirements.txt
build.bat
```

This runs PyInstaller and produces `dist\Signal.exe` — a single
double-clickable file. Copy that file wherever you like; it doesn't need
Python installed on the target machine.

**Why build on Windows, not elsewhere:** PyInstaller bundles the
interpreter and native libraries for whatever OS it's run on — it can't
cross-compile a Windows .exe from macOS or Linux. Build it on the Windows
machine (or VM) you intend to run it on.

## Notes on the sACN library

The app uses the `sacn` PyPI package for the E1.31 protocol implementation.
If your installed version's `sACNreceiver` constructor doesn't accept a
`bind_address` argument (API has shifted slightly across versions), the
app falls back to the default constructor automatically — check
`pip show sacn` if adapter selection doesn't seem to take effect, and
consult that version's docs for the exact multicast-interface argument.

## Files
- `app.py` — the application
- `requirements.txt` — Python dependencies
- `build.bat` — one-click PyInstaller build script
- `README.md` — this file
