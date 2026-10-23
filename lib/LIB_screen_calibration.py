"""
screen_calibration.py

What one screen pixel is worth in the real world, per monitor.

A monitor's true pixel density cannot be asked for: the physical size reported
through EDID is missing or wrong on a great many panels, so the only reliable
figure is one a person measured by holding a known object against the glass.
``ScreenRuler.py`` is the tool that does the measuring; this module is where the
answer lives, so anything else that needs to draw at a real-world size can read
it -- ``UI_island_editor.py`` uses it to show a province at the size it will
print.

Calibrations are stored **one per monitor**, because that is what a calibration
is about: a 27" 1440p panel and the 24" 1080p one beside it have genuinely
different pixel sizes, and a single machine-wide figure is simply wrong on one
of them.

A monitor is keyed on its EDID model code and resolution.  Re-ordering displays
or unplugging and replugging them therefore costs nothing, where keying on the
instance number would; changing a monitor's *resolution* does start a fresh
calibration, because that changes how many pixels an inch holds.  Two identical
monitors collapse onto one key, which is the right answer -- identical panels
have identical pixel sizes, so one measurement genuinely serves both.

Only Windows can be asked which monitor a window is on without a third-party
library, so elsewhere this falls back to one calibration for the machine.
"""

import json
import os
import sys
from collections import namedtuple

from lib.LIB_project_config import get as _config_get
from lib.LIB_project_config import update_local as _config_update_local

#: Where in the configuration the calibrations live. The **local** file, always:
#: a calibration is measured against particular monitors on one desk, so it is
#: not merely useless on another machine but actively wrong.
CONFIG_KEY = "screen.monitors"

#: The standalone file this used to be, kept only to migrate out of. Older runs
#: of ScreenRuler wrote here; the first read that finds it moves its contents
#: into the local project config, because nobody should have to hold a card
#: against the glass a second time for a file rename.
LEGACY_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    ".screen_ruler_config.json")

MM_PER_INCH = 25.4

#: What a monitor is, as far as a calibration is concerned.
Monitor = namedtuple("Monitor", "key label width height")

#: The monitor to fall back on where we cannot enumerate them. One key for the
#: whole machine is the pre-per-monitor behaviour, and is also where a migrated
#: v1 calibration is offered from.
UNKNOWN_MONITOR_KEY = "display"


# ----------------------------------------------------------------------
# Which monitor is at this point on the desktop?
# ----------------------------------------------------------------------
if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _MONITOR_DEFAULTTONEAREST = 2

    class _RECT(ctypes.Structure):
        _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                    ("right", wintypes.LONG), ("bottom", wintypes.LONG)]

    class _MONITORINFOEXW(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", _RECT),
                    ("rcWork", _RECT), ("dwFlags", wintypes.DWORD),
                    ("szDevice", wintypes.WCHAR * 32)]

    class _DISPLAY_DEVICEW(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("DeviceName", wintypes.WCHAR * 32),
                    ("DeviceString", wintypes.WCHAR * 128),
                    ("StateFlags", wintypes.DWORD),
                    ("DeviceID", wintypes.WCHAR * 128),
                    ("DeviceKey", wintypes.WCHAR * 128)]

    def _user32():
        """user32 with the signatures we use declared (needed on 64-bit)."""
        lib = ctypes.windll.user32
        lib.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
        lib.MonitorFromPoint.restype = ctypes.c_void_p
        lib.GetMonitorInfoW.argtypes = [ctypes.c_void_p,
                                        ctypes.POINTER(_MONITORINFOEXW)]
        lib.GetMonitorInfoW.restype = wintypes.BOOL
        lib.EnumDisplayDevicesW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD,
            ctypes.POINTER(_DISPLAY_DEVICEW), wintypes.DWORD]
        lib.EnumDisplayDevicesW.restype = wintypes.BOOL
        return lib

    def _monitor_hardware(device_name):
        """
        ``(model, human name)`` for the monitor on an adapter output, or
        ``(None, None)``.

        ``DeviceID`` looks like ``MONITOR\\DELA0C9\\{guid}\\0002``.  The second
        component is the EDID model code, which is what we key on: it survives
        re-ordering and replugging, where the trailing instance number does not.
        """
        device = _DISPLAY_DEVICEW()
        device.cb = ctypes.sizeof(_DISPLAY_DEVICEW)
        if not _user32().EnumDisplayDevicesW(device_name, 0,
                                             ctypes.byref(device), 0):
            return None, None
        parts = [p for p in device.DeviceID.split("\\") if p]
        model = parts[1] if len(parts) > 1 else None
        return model, (device.DeviceString or None)

    def _windows_monitor(x, y):
        """The monitor containing screen point ``(x, y)``, or None if unclear."""
        lib = _user32()
        handle = lib.MonitorFromPoint(wintypes.POINT(int(x), int(y)),
                                      _MONITOR_DEFAULTTONEAREST)
        if not handle:
            return None
        info = _MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(_MONITORINFOEXW)
        if not lib.GetMonitorInfoW(handle, ctypes.byref(info)):
            return None

        width = info.rcMonitor.right - info.rcMonitor.left
        height = info.rcMonitor.bottom - info.rcMonitor.top
        model, name = _monitor_hardware(info.szDevice)
        # Resolution joins the key because changing it changes the pixel size of
        # every real-world inch, which is precisely what a calibration records.
        stem = model or info.szDevice.strip("\\.") or "display"
        return Monitor(f"{stem}@{width}x{height}",
                       name or info.szDevice or "display", width, height)

    def enable_per_monitor_dpi():
        """
        Ask Windows to stop lying about pixels, before any window is made.

        Without this a DPI-unaware process is handed a fictitious resolution and
        its window is bitmap-stretched to fit -- so the "pixels" it draws into
        are not the monitor's, and on a mixed-DPI desk the stretch factor
        changes as the window crosses between displays.  A calibration taken on
        one monitor would then be wrong on the next for a reason no amount of
        recalibrating can express.  Per-monitor-v2 makes a window's pixels the
        monitor's pixels, which is the only footing real-world sizing can work
        from.  (Qt 6 already does this for itself; tkinter does not.)
        """
        for call in (
            lambda: ctypes.windll.user32.SetProcessDpiAwarenessContext(-4),
            lambda: ctypes.windll.shcore.SetProcessDpiAwareness(2),
            lambda: ctypes.windll.user32.SetProcessDPIAware(),
        ):
            try:
                if call():
                    return
            except (AttributeError, OSError):
                continue        # older Windows; try the next one down

else:
    def _windows_monitor(x, y):
        return None

    def enable_per_monitor_dpi():
        pass


def monitor_at(x, y, screen_size=None):
    """
    The monitor containing screen point ``(x, y)``.

    ``screen_size`` is a ``(width, height)`` used only for the fallback monitor,
    so a caller can hand over whatever its own toolkit knows about the display.
    Never returns None: where the displays cannot be told apart the answer is a
    single machine-wide monitor, which is the honest one.
    """
    try:
        found = _windows_monitor(x, y)
        if found is not None:
            return found
    except (OSError, ValueError):
        pass
    width, height = screen_size or (0, 0)
    return Monitor(UNKNOWN_MONITOR_KEY, "this display", width, height)


# ----------------------------------------------------------------------
# Stored calibrations
# ----------------------------------------------------------------------
def _legacy_file_monitors():
    """
    Calibrations from the standalone file this used to live in, or ``{}``.

    Two shapes have existed there: ``{"monitors": {...}}``, and before that a
    single machine-wide ``{"ppi_x": ...}``.  The older one is handed back under
    the sentinel key so the caller can adopt it for whichever monitor is in
    front of them now -- the only monitor it could have been measured on.
    """
    try:
        with open(LEGACY_CONFIG_PATH, "r") as f:
            data = json.load(f)
    except (OSError, ValueError):
        # OSError, not just FileNotFoundError: the file can also be unreadable
        # (wrong permissions, a directory in its place, a network share that has
        # gone away), and none of those are worth taking the caller down for.
        # ValueError covers json's own JSONDecodeError.
        return {}
    if not isinstance(data, dict):
        return {}
    monitors = data.get("monitors")
    if isinstance(monitors, dict):
        return monitors
    try:
        legacy = float(data["ppi_x"])
    except (KeyError, TypeError, ValueError):
        return {}
    return {UNKNOWN_MONITOR_KEY: {"ppi": legacy}} if legacy > 0 else {}


def load_config():
    """
    The calibrations as ``{monitor key: {...}}``, empty if there are none.

    Reads the local project config, falling back to the standalone file that
    predates it -- and carrying that file's contents forward on the way, so the
    move happens once and silently rather than costing a recalibration.
    """
    monitors = _config_get(CONFIG_KEY)
    if isinstance(monitors, dict) and monitors:
        return monitors
    inherited = _legacy_file_monitors()
    if inherited:
        _config_update_local(CONFIG_KEY, inherited)
    return inherited


def load_saved_ppi(monitor, config):
    """A previously saved calibration for ``monitor`` in ``config`` -- the
    calibrations :func:`load_config` read -- or None."""
    entry = config.get(monitor.key)
    try:
        ppi = float(entry["ppi"])
    except (KeyError, TypeError, ValueError):
        return None
    return ppi if ppi > 0 else None


def save_ppi(monitor, ppi):
    """File ``ppi`` under ``monitor``, leaving every other monitor's alone."""
    monitors = dict(load_config())
    monitors.pop(UNKNOWN_MONITOR_KEY, None)   # migrated; it has a real key now
    monitors[monitor.key] = {
        "ppi": ppi,
        "label": monitor.label,
        "size_px": [monitor.width, monitor.height],
    }
    _config_update_local(CONFIG_KEY, monitors)


def px_per_mm(ppi):
    """Screen pixels per millimetre of glass, from a calibrated PPI."""
    return ppi / MM_PER_INCH
