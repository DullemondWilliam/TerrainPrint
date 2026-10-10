# UI Guide

How the windows in this project are put together. Every rule here is taken from
`UI_island_editor.py`, the window that has had the most use and the most
correction. Where a rule exists because of a bug or a complaint, the reason is
given, because a rule without its reason gets "simplified" away.

New windows follow this guide. Existing windows move towards it when they are
touched. `UI_terrain_editor.py` follows it as well. The single-purpose test
harnesses in `harnesses/` have their own, lighter template in
`BUILDING_UI_HARNESSES.md`. This guide is for the editors that people work in.

---

## 1. Window anatomy

From top to bottom:

```
┌──────────────────────────────────────────────────────────────┐
│ document row: Import … · Save … │ Undo · Redo                │
│ control rows: [Group] [Group] [Group]                        │
│ control rows: [Group] [Group]                                │
├────────────────────────────────────────────┬─────────────────┤
│                                            │ side panel      │
│                 map canvas                 │ (measurements,  │
│              (takes the slack)             │  fixed width)   │
│                                            │                 │
├────────────────────────────────────────────┴─────────────────┤
│ status line (left, stretches)       nav toolbar │ view ctrls │
│ progress bar (hidden unless something is running)            │
└──────────────────────────────────────────────────────────────┘
```

* **Each row has one job.** In the Island editor, row 1 is the document (the
  file and its history), row 2 is what an edit applies to (the selection or the
  whole province), and row 3 is how the land is reshaped. A control in the
  wrong row still works, but the row no longer tells you what it is for.
* **The map takes the slack.** Every chrome row is fixed height and the map's
  row has `stretch=1`. A new row of controls costs the map height, so ask
  whether it could go in an existing row or in the side panel first.
* **Measurements go beside the map, not above it.** Readouts that describe the
  document (scores, layers, reports) go in a fixed-width side panel. They are
  read against the map, and there they cost the map no height.
* **The map's own controls go under it.** The navigation toolbar and anything
  that changes how the map is *viewed* (print scale, Real Size) share the
  bottom row with the status line. They are not edits, so they do not belong
  in the control rows.
* **The status line keeps the left** of that bottom row, where reading starts.
* **The progress bar is hidden** when nothing is running, so an idle window
  looks the same as it always does.
* **Margins:** root layout `setContentsMargins(8, 6, 8, 4)`, spacing 4. Spare
  pixels go to the canvas.

A window with stages, such as the terrain editor's Import → Borders → STL,
puts each stage's rows on a tab named `"N · Stage"`. Inside a tab the rules
above still apply.

## 2. Groups

Controls live in titled `QGroupBox`es, built with one helper:

```python
@staticmethod
def _group(title: str, *contents) -> QGroupBox:
    box = QGroupBox(title)
    row = QHBoxLayout(box)
    row.setContentsMargins(8, 2, 8, 4)
    row.setSpacing(4)
    for item in contents:
        row.addWidget(QLabel(item) if isinstance(item, str) else item)
    return box
```

* **The title names what every control inside acts on**, or how the controls
  work ("Selection", "Whole province", "Expansion Merge", "Freehand"). Because
  the title supplies the context, buttons can leave it out: "Merge", not
  "Merge Selected".
* **Bare strings become labels**, so a group reads the same in the source as
  on screen: `_group("Bridge Merge", "Width (mm)", self.bridge_width_spin, …)`.
* **A group is the enable handle** for everything in it. Lock a whole group
  while a worker runs instead of listing every widget. Each child keeps its own
  enabled flag, so unlocking the group restores the state it had before.
* **A row is a `QHBoxLayout` of groups with `addStretch(1)` at the end**, so the
  groups stay packed to the left as the window widens.
* A control that owns no parameter goes with the gesture it completes: the
  Island editor's **Remove** sits in *Selection*, not among the spin boxes.

## 3. Labels and units

* **Parameter labels are `Name (unit)`**, with no colon: `Min area (mm²)`,
  `Buffer (mm)`, `Angle (°)`. A unitless value takes just its name: `Ratio`.
* **The unit goes in the label, not in a spin-box suffix.** The one exception
  is a spin box with a `specialValueText` such as "native" or "default". It
  keeps its suffix so that the special value and the numbers read the same way.
* **Sizes are printed millimetres** wherever the constraint belongs to the
  paper (a neck, a tolerance, a clearance). They are converted to ground units
  through the print scale when they are used. The window never rewrites them
  when the scale changes.
* **A button that opens a dialog ends in " …"** (space, ellipsis): `Import
  GeoJSON …`, `Save STL …`. A button that acts at once has no ellipsis.
* **Button text is a verb or verb phrase in Title Case**: `Remove Small`,
  `Define Borders`, `Build STL`. A checkable button that switches on a layer or
  a mode is labelled with what it shows or does: `Show`, `Draw Bridge`,
  `Edit Connectors`.

## 4. Spin boxes and other entries

* Build them with a `_spin(lo, hi, value, decimals, …, tip)` helper. Ranges and
  defaults live in module-level constants or the config, never inline.
* **A spin box whose change triggers real work uses
  `setKeyboardTracking(False)`**, so typing "2.5" is one recompute, not three.
* **An entry never swallows Undo/Redo.** Install the window as an event filter
  on every spin box and editable line edit, and drop `ShortcutOverride` for the
  Undo/Redo key sequences (see `IslandEditor.eventFilter`). Otherwise Ctrl+Z
  in a focused box undoes a typed digit instead of the last edit.
* **A read-only figure that sits among editable controls** (the km/mm box) is a
  read-only `QLineEdit`, and its tooltip says how to change it.

## 5. Tooltips

* **Every control has one.** It says what the control does and, where it is not
  obvious, why you would use it and what it costs (time, a stale result).
* Write sentences. Separate paragraphs with a blank line (`"\n\n"`).
* Name other controls in ‘single quotes’ and say where they are: "‘Remove
  Lakes’", "the km/mm box under the map".
* **Name the config key** a control is saved to, when there is one:
  `(seam.min_angle_deg)`.
* **The keyboard shortcut is added by code, not written by hand.** `_hint()`
  appends `Shortcut: <key>` (in native text) as the last paragraph. Do not
  write "(Del)" or "(M)" into a tooltip or onto a button face.

## 6. Keyboard

* **Every button has a key**, set on the button with `setShortcut`, from one
  table in `_wire_shortcuts()`. Keeping them in one table makes clashes easy to
  spot. Because the key is on the button:
  * a disabled button is a dead key (no merging with nothing selected), and
  * the button flashes when its key fires, so you can see which control acted.
* **How keys are chosen:**
  * File and document actions use modified keys that work from anywhere:
    Ctrl+O to open, Ctrl+S to save, and Ctrl+Shift+… for a second variant.
  * Edits and modes use the bare initial of the word that tells them apart
    (M Merge, B Bridge, D Draw, E Erase). Delete removes the selected item.
  * Measurements, i.e. things the window reads rather than changes, use
    function keys in the order they sit on screen (F5, F6, F7).
  * View history uses Home, Alt+Left and Alt+Right, kept well away from Ctrl+Z.
    Pan and Zoom take P and Z.
* Bare letters are safe next to spin boxes. A focused entry claims the keys it
  accepts as input before shortcut dispatch, so typing a value cannot start an
  edit.
* **Undo/Redo is bound to every sequence the platform lists, plus Ctrl+Z,
  Ctrl+Shift+Z and Ctrl+Y**, through `_bind()`. On Windows,
  `StandardKey.Redo` is only Ctrl+Y. The Undo/Redo buttons get a `_hint`, but
  no `setShortcut`: a second binding would make the key ambiguous, and Qt
  fires neither shortcut when a key is ambiguous.
* **No key is claimed twice** in a window, including across tabs.
* **Arrow keys pan the map** by a fraction of the visible span (10%, Shift 50%,
  Ctrl 2%). They are bound to the *canvas* with `WidgetShortcut` context, so
  Up/Down still step a focused spin box.
* **Esc** cancels the gesture in progress, then leaves the mode.

## 7. Focus

* The canvas gets `setFocusPolicy(Qt.FocusPolicy.StrongFocus)`. Embedded
  matplotlib canvases default to `NoFocus`, which leaves focus parked in a spin
  box.
* **A button gives focus back to the canvas once it has been clicked.** The
  `_button()` helper connects `clicked` to `canvas.setFocus`. Without this,
  arrow keys move between toolbar buttons instead of panning.

```python
def _button(self, text, slot, enabled=True):
    b = QPushButton(text)
    b.clicked.connect(slot)
    b.clicked.connect(lambda *_: self.canvas.setFocus())
    b.setEnabled(enabled)
    return b
```

## 8. The map canvas

* Use a `MapToolbar`: matplotlib's navigation toolbar with only Home, Back,
  Forward, Pan and Zoom kept. *Subplots* and *Customize* undo the deliberate
  axes layout. *Save* writes a PNG one button away from a Save that writes
  data. Back and Forward's tooltips say "this is not Undo/Redo".
* The wheel zooms towards the pointer at all times. A plain left-drag pans
  unless a mode is on.
* Axes: equal aspect, no ticks.
* A click is a press and release less than `PAN_SLOP_PX` (4 px) apart. Hand
  tremor must not turn a click into a pan.

## 9. Status line

* One `QLabel` named `status`, set larger than the chrome by `_emphasise()`
  (+2 pt), with **no colour set**.
* It may shrink below its text (`setMinimumWidth(0)`, horizontal policy
  `Ignored`). Otherwise a long report sets the window's minimum width.
* It is the running commentary: what just happened and what to do next.
  Write full sentences ending in a full stop: `"Loaded X: 12 landmass(es).
  Click islands to select; edit; undo/redo as needed."`. Standing figures go in
  the side panel, not here.
* Idle text says what is missing: `"No file loaded."`.

## 10. Colour

* **Never hard-code a text colour.** `color:#555` is readable on a light theme
  and nearly invisible on a dark one. Leave the colour unset so the palette's
  own text colour is used. For hierarchy, use a group title or a font size.
* Map colours are module-level constants with a comment saying what each
  means, and each colour means one thing in a window. The shared vocabulary is:

  | Constant | Hex | Meaning |
  |---|---|---|
  | `ISLAND_COLOUR` / `KEEP_COLOUR` | `#AED6F1` | land that is kept / printed |
  | `EDGE_COLOUR` | `#1B4F72` | outlines |
  | `SELECT_COLOUR` | `#F5B041` | selected |
  | `REMOVE_COLOUR` / `MARK_COLOUR` | `#E6787A` | on its way out / about to be cut |
  | `THIN_COLOUR` / `REFUSED_COLOUR` | `#C0392B` | a fault: too thin, not allowed |
  | `ORIGINAL_COLOUR` / `PICKED_COLOUR` | `#7D3C98` | the one special overlay |

* Severity uses the good-to-critical ramp `SEVERITY_STOPS`, copied into each
  window rather than imported from another window.

## 11. Stability: nothing moves while you work

The map is clicked again and again. Anything that shifts between clicks can be
clicked by mistake.

* **A label whose text changes gets a fixed width**, measured from the font
  (`fontMetrics().horizontalAdvance(widest_text)`), not guessed in pixels.
* **Readouts reserve their lines** (`setMinimumHeight(lineSpacing * 3)`), so
  showing a layer does not push the buttons below it down.
* **The side panel holds one width**, again measured from the font.
* A wrapping label sets `heightForWidth` on its size policy. Otherwise it is
  cut off after the first line.

## 12. Enabling

* **One method re-derives every enabled state from the window's state.** In
  the Island editor that is `_sync_buttons()`. Call it after anything that
  changes what is possible. Do not enable a button in one place and hope that
  another place disables it again.
* A group is enabled when there is something for it to act on. Inside it,
  each button keeps its own rule ("needs two selected"), so the reason it is
  greyed stays visible.
* A button whose result is current is disabled ("Recalculate" is enabled only
  when the figures are stale).

## 13. Long work

* Anything slower than a moment runs on a `QThread` worker with `done` and
  `failed` signals. The worker is given immutable inputs, never the window.
* While it runs: the progress bar is shown in indeterminate mode, the groups
  that could change its inputs are locked, and the status line says what is
  running. A layer toggle stays live, so switching a layer off always works.
* On failure: `QMessageBox.critical(self, "<Thing> failed", message)` and a
  status line that says the same.
* `closeEvent` waits for the worker before the window is destroyed. Destroying
  a thread under running code is a crash, not a cancellation.

## 14. Files and messages

* File dialogs start in the configured directory (`paths.*`), falling back to
  the cwd. A save dialog suggests the source file's stem.
* Filters are module constants: `GEOJSON_FILTER`, `GEOTIFF_FILTER`,
  `STL_FILTER`.
* `critical` is for a failed operation, `warning` for a refused or suspicious
  input ("No CRS on input"). An edit that changes nothing goes to the status
  line, not a dialog (`"No change — …"`).

## 15. Window title

`"<Window name> — <file name><dirty>"`, where the file is the main open
document, `"(no file)"` before one is loaded, and `" *"` marks unsaved edits
when the window has an undo stack.

## 16. Settings that persist

* Every knob that is a *preference* rather than a property of the open file is
  listed in one `SAVED_SETTINGS` table of `(config key, attribute name)`.
* At launch the controls are built on the shipped values, then the saved values
  from `settings_files()` are applied, and only *then* are the change signals
  wired, so building the window is not mistaken for a choice.
* Writes go to `project_config.local.json` (gitignored) through
  `update_local`. They are debounced by `SETTINGS_WRITE_MS` (500 ms) and
  flushed in `closeEvent`.

## 17. Tests

UI tests are pytest-style with `qtbot` (see `tests/test_island_editor_layout.py`
and `tests/test_island_editor_keys.py`). Pin what a person relies on:

* the group each control is in, and the order of the rows;
* every button has a key, its tooltip names the key, and no key is claimed twice;
* undo fires from every spin box;
* labels that change text keep their width.

Run them with the venv's pytest:

```
QT_QPA_PLATFORM=offscreen MPLBACKEND=Agg ./.venv/Scripts/python.exe -m pytest -q
```
