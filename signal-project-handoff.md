# Signal — Project Handoff

**Purpose of this document:** paste or upload this into a new Claude conversation to pick up exactly where this one left off. It replaces relying on Claude's automatic memory, which only keeps a lossy summary.

---

## What Signal is

A real, functional desktop app for **Windows** (Python, packaged to a standalone `.exe` via PyInstaller) that:

1. Listens for live **sACN / E1.31** multicast DMX data on a chosen network adapter and universe range.
2. Displays incoming DMX values live in a per-universe grid (512 channels, 10 per row).
3. Lets you define **virtual fixtures** (name, universe, start channel, channel layout, optional multi-pixel repeat) and shows each fixture's live computed color in a freeform, draggable/resizable preview area.

This is not a mockup — it's the real app the user runs locally and packages into an executable.

## Tech stack

- **Language / GUI:** Python 3, `tkinter` (stdlib) + `ttk`
- **sACN protocol:** `sacn` PyPI package (`sacn.sACNreceiver`)
- **Network adapters:** `psutil.net_if_addrs()`
- **Packaging:** PyInstaller → `pyinstaller --onefile --windowed --name Signal app.py`
- **File location on the user's machine:** `C:\Users\Mazze\Desktop\PyApp\app.py`
- Deliverable files also include `requirements.txt`, `build.bat`, `README.md`

**Important sandbox constraint (for Claude, not the user):** the environment used to build this has no display and no `tkinter` binding, and no network access. Every change was verified by `python3 -m py_compile` plus an AST-based static audit (script pattern below) — never by actually running the GUI. Bugs that only manifest at runtime (Tk rendering quirks, X11/Windows selection behavior) were diagnosed from the user's description, not observed directly. Keep flagging that limitation honestly when making further changes.

```python
# Static audit pattern used throughout — worth reusing after any big edit:
import ast
from collections import Counter
with open('app.py') as f:
    tree = ast.parse(f.read())
app_class = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == 'App')
names = [n.name for n in app_class.body if isinstance(n, ast.FunctionDef)]
print("Duplicate methods:", {k: v for k, v in Counter(names).items() if v > 1} or "NONE")
defined = set(names)
called = {node.func.attr for node in ast.walk(app_class)
          if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
          and isinstance(node.func.value, ast.Name) and node.func.value.id == 'self'}
print("Called-but-undefined:", called - defined or "NONE")
```

## Architecture

Single file, `app.py`, roughly in this order:

- **Module-level constants:** theme colors (`BG`, `PANEL`, `PANEL2`, `LINE`, `TEXT`, `TEXT_MUTED`, `SIGNAL` cyan accent, `DANGER`, `MONO` font), `TYPE_BLEND_TARGETS` (per-channel-type grid coloring), `FIXTURE_TYPE_OPTIONS` / `FIXTURE_TYPE_KEY` / `FIXTURE_TYPE_LABEL` (Intensity/Red/Green/Blue).
- **`list_adapters()`** — enumerates local IPv4 adapters via `psutil`, always includes Loopback.
- **`UniverseGrid(ttk.Frame)`** — one scrollable canvas-based 10×52 grid per universe tab. `update_values(values, channel_types=None)` paints cells; when `channel_types` is provided (Colorize is on), cells tint toward red/green/blue/white based on what fixture channel occupies them, otherwise falls back to the default cyan monochrome look.
- **`Fixture`** — the core data model. Fields: `name`, `universe`, `start_channel`, `channels` (list of `{type, role, pair_index}` — role is `single`/`coarse`/`fine` for 16-bit pairs), `pixel_count`, `repeat_from`/`repeat_till` (1-based positions in `channels` that repeat per pixel), `pixel_rows`/`pixel_cols`, and `preview_x/y/w/h` (position & size in the freeform preview canvas). Key methods: `set_channel_count`, `toggle_16`, `can_enable_16`, `set_type`, `clone`, `to_dict`/`from_dict` (JSON serialization), `is_repeating`, `_abs_offset` (maps a template channel position to an absolute DMX offset for a given pixel index), `channel_type_map()` (for grid coloring), `compute_pixel_colors(dmx_values)` (returns one `(r,g,b)` tuple per pixel; Intensity acts as a master dimmer over RGB).
- **`App`** — everything else: topbar (power switch, adapter dropdown, universe range), a `PanedWindow` with the DMX grid notebook on the left and a fixture sidebar on the right (Fixtures list / Fixture Config / Fixture Preview), status strip at the bottom, and the background refresh loops.

## Feature list (current state)

**sACN monitor (left side)**
- Start/stop listening, adapter selection, universe range with tabs (capped at 32 universes shown)
- Per-universe scrollable grid, live-updating
- **Colorize** toggle above the grid (default off = plain cyan monochrome; on = channels tinted by whichever fixture type occupies them)

**Fixtures (right sidebar)**
- Add/Rename/Duplicate/Remove, **multi-select** (ctrl/shift-click) for Duplicate and Remove
- Fixture Config panel: Universe, Start Channel, Channel Count, per-channel Type dropdown (Intensity/Red/Green/Blue) + 16-bit toggle, Pixels/Repeat From/Repeat Till/Rows/Cols for multi-pixel fixtures, Apply, Save As Preset
- **The config panel is never blank** — with nothing selected it shows the same form fully greyed out against a placeholder fixture, rather than an empty message
- **Presets:** "Presets..." button → "Manage Presets" dialog (positioned near the button, sized to fit) with a live details preview (mini pixel grid + channel layout text) for whichever preset is highlighted; Load Preset / Delete Preset
- **Save Setup / Load Setup:** serializes all fixtures (incl. pixel/repeat config and preview layout) to/from a JSON file via file dialogs
- **Fixture Preview:** freeform canvas, each fixture drawn as a rectangle (single big square for 1 pixel, rows×cols grid of smaller rectangles for multi-pixel). Drag body to move, drag any of 8 edge/corner handles to resize (edges resize one dimension anchored on the opposite side; corners resize both). **Snap** button toggles edge-snapping between fixtures. **Undock** button pops the whole preview into its own resizable `Toplevel` window (native maximize/minimize, can be dragged to another monitor); **Redock** brings it back.
- Selecting a fixture (in the list, or by clicking it directly in the preview) raises it to the front and is kept in sync both ways.

## Bugs hit and fixed — worth knowing before touching this again

1. **`ttk.Combobox` showing light-grey background / white text (unreadable):** `clam` theme overrides `fieldbackground` for the `readonly` state regardless of your base `configure()`. Fix: explicit `style.map("TCombobox", fieldbackground=[("readonly", PANEL2), ...])`, plus `root.option_add("*TCombobox*Listbox.background", ...)` for the popdown listbox (which is a separate plain `Listbox`, not styled by ttk at all).
2. **16-bit checkbox appearing checked/unchecked backwards:** a known Windows/Tk quirk where a custom `selectcolor` on a native-style `Checkbutton` indicator can invert the glyph. Fixed by using `indicatoron=False` (renders as a plain toggle pill we fully control) instead of relying on the native checkbox glyph.
3. **Resize handle on the fixture preview doing nothing:** the handler deleted-and-recreated the canvas item under the cursor on every mouse-move to redraw the pixel grid at the new size. Tk freezes which item a drag gesture belongs to at button-press time; deleting that exact item mid-drag silently kills the gesture. Fix: never delete/recreate items during a drag — create all canvas items once and only reposition them via `canvas.coords()`; drag/resize state is tracked via canvas-level `bind`/`unbind` on press/release, not per-item `tag_bind` for motion.
4. **Undock → Redock leaving a duplicated/broken preview area** (worse with each cycle): `_create_preview_canvas()` builds a wrapper `Frame` (holding the canvas + scrollbars) and packs *that* into the sidebar, but redocking only called `pack_forget()` on the inner canvas — the empty wrapper frame stayed packed forever, one more per cycle. Fix: track the wrapper (`self.preview_canvas_container`) and `.destroy()` it wholesale when undocking/redocking.
5. **Editing a channel's type (or toggling 16-bit) sometimes made the whole Fixture Config panel go blank**, looking like the fixture got deselected: the combobox/checkbox callback destroyed and rebuilt the entire panel **synchronously from within its own event handler** — i.e. a widget tearing itself down mid-click. On some Tk builds this can abort the rebuild partway through. Fix: defer those rebuilds via `self.root.after_idle(...)` so Tk finishes the current event before anything is destroyed.
6. **Fixture actually did get deselected when drag-selecting text in a Spinbox (e.g. the Universe number field), or right after picking a Combobox value:** root cause was `tk.Listbox`'s default `exportselection=True`, which ties the listbox's visible selection to ownership of the X "PRIMARY" text selection — any other widget claiming a text selection (a Spinbox drag-select, a Combobox commit) silently steals it and Tk clears the listbox selection as a side effect. Fix: `exportselection=False` on the fixture listbox. This was likely the *real* root cause behind #5's symptom too, though the `after_idle` fix in #5 is independently correct and worth keeping.

**General lesson for this codebase:** most of the "weird disappearing/broken UI" bugs have been Tk-specific gotchas (state-mapping, selection ownership, drag/item-identity), not logic errors in the app's own data model. When something looks broken in a way that doesn't match the code's apparent logic, suspect a Tk quirk before assuming the model logic is wrong.

## Known limitation / design decision to revisit if it matters

A fixture's color preview only updates if its `universe` is currently within the actively-joined listening range (`range_start`–`range_end`). A fixture configured for a universe outside that range shows as off/dark rather than auto-extending the listened range. Never explicitly asked to change this — flagged here in case it becomes an issue.

## Suggested next steps

- Actually run the app on Windows and click through the full feature list above — nothing past basic compilation and static analysis has been verified in a live Tk runtime.
- In particular stress-test: rapid channel-type changes, multi-pixel fixtures with repeat ranges at the very start/end of the layout, snapping between differently-sized fixtures, and the undock/redock cycle a few times in a row.
- No git repo has been set up yet for this project (see earlier discussion in this conversation about pushing to GitHub — options were: user pushes manually, or use Claude Code on the local machine for direct git integration).

## How to resume

Paste this file into a new conversation along with the current `app.py` (or just say "continue the Signal project" and attach `app.py` if it's changed since this document), and pick up from "Suggested next steps."
