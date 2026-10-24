#!/usr/bin/env python3
"""
Screen Ruler
============

Renders an on-screen ruler (inches and/or cm) that is scaled to match
real-world size as closely as possible.

How it works
------------
1. On startup, it asks the OS for the screen's reported physical size
   (via Tkinter's winfo_screenmmwidth/height) and computes an initial
   pixels-per-inch (PPI) guess. This is often WRONG because many monitors
   report incorrect or missing EDID physical-size data.
2. It then shows a calibration bar and lets you correct the scale by
   comparing it to a real-world reference object (a credit/ID card,
   or a ruler you already own). You drag a slider (or press +/- / arrow
   keys) until the on-screen bar matches the real object exactly.
3. Once calibrated, it draws a ruler using the corrected PPI value and
   saves that value to a small config file next to the script, so future
   runs on the same machine start pre-calibrated.

Per monitor
-----------
Calibrations are stored **one per monitor**, because that is what a calibration
is about: a 27" 1440p panel and a 24" 1080p one beside it have genuinely
different pixel sizes, and a single machine-wide figure is simply wrong on one
of them. Drag the window to another monitor and it switches to that monitor's
ruler, or asks to calibrate if it has never met it.

A monitor is keyed on its EDID model code and resolution, so re-ordering
displays or unplugging and replugging them costs nothing. Two identical
monitors share one calibration, which is correct -- identical panels have
identical pixel sizes. Changing a monitor's resolution does start a fresh
calibration, because that changes how many pixels an inch holds.

Only Windows can be asked which monitor a window is on without a third-party
library, so elsewhere this falls back to one calibration for the machine, as
before.

Usage
-----
    python ScreenRuler.py

Controls in calibration mode:
    Left/Right arrows, or +/- : nudge scale
    Enter                     : accept calibration and show ruler
    r                         : reset to OS-reported guess
    c                         : re-enter calibration mode from ruler view
    q / Esc                   : quit

No third-party dependencies -- uses only the standard library (tkinter, and
ctypes for the Windows monitor query).
"""

import sys
import tkinter as tk
from tkinter import font as tkfont

from lib.LIB_screen_calibration import (MM_PER_INCH, UNKNOWN_MONITOR_KEY,
                                        enable_per_monitor_dpi, load_config,
                                        load_saved_ppi, monitor_at, save_ppi)

# Standard credit-card / ID-card size in mm (ISO/IEC 7810 ID-1) -- a handy,
# widely-available real-world reference object for calibration.
CREDIT_CARD_WIDTH_MM = 85.60
CREDIT_CARD_HEIGHT_MM = 53.98

#: How often to notice the window has been dragged to another monitor. A window
#: move raises <Configure> on Windows, so this is only the backstop for changes
#: no event announces -- a monitor unplugged, or the arrangement rearranged
#: underneath a window that never moved.
MONITOR_POLL_MS = 700


def describe_monitor(root):
    """The monitor this Tk window is sitting on, however little we can tell."""
    try:
        centre_x = root.winfo_rootx() + root.winfo_width() // 2
        centre_y = root.winfo_rooty() + root.winfo_height() // 2
        size = (root.winfo_screenwidth(), root.winfo_screenheight())
    except tk.TclError:
        return monitor_at(0, 0)
    return monitor_at(centre_x, centre_y, size)


def os_reported_ppi(root):
    """
    Ask the OS for screen width in pixels and millimeters, and derive PPI.
    This is frequently inaccurate (see module docstring) but gives a
    reasonable starting point for calibration.

    tkinter only ever answers for the primary display, so on a second monitor
    this is the *first* monitor's figure. It is deliberately left at that: the
    pixel counts are known but the physical widths are not, so anything better
    would mean assuming the panels are the same size -- a guess on top of a
    guess, and one that would quietly mislead on a 4K 32" beside a 1080p 24".
    It is a place for the slider to start, nothing more.
    """
    width_px = root.winfo_screenwidth()
    width_mm = root.winfo_screenmmwidth()
    if width_mm <= 0:
        # OS gave us nothing useful; fall back to the common default.
        return 96.0
    return width_px / (width_mm / MM_PER_INCH)


class ScreenRulerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Screen Ruler")
        self.root.configure(bg="white")

        self.guess_ppi = os_reported_ppi(root)

        self.canvas = tk.Canvas(root, bg="white", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)

        self.monitor = describe_monitor(root)
        self.saved_ppi = self._adopt_saved_calibration()
        self.ppi = self.saved_ppi if self.saved_ppi else self.guess_ppi
        self.mode = "calibrate" if self.saved_ppi is None else "ruler"

        self._bind_keys()
        self.canvas.bind("<Configure>", lambda e: self.redraw())
        # A window move raises <Configure> on the toplevel but not on the canvas
        # inside it, so the drag between monitors has to be watched for here.
        self.root.bind("<Configure>", lambda e: self.check_monitor())
        self._poll_job = self.root.after(MONITOR_POLL_MS, self._poll_monitor)

        self.redraw()

    # ------------------------------------------------------------------
    # Following the window between monitors
    # ------------------------------------------------------------------
    def _adopt_saved_calibration(self):
        """
        This monitor's stored PPI, taking over a pre-per-monitor one if found.

        A v1 config holds a single number with no monitor attached. Whoever
        measured it was sitting at *some* monitor, and the one they are at now
        is the best candidate there will ever be -- so it is claimed for this
        monitor and rewritten under its key, rather than thrown away and the
        card-holding asked for again.
        """
        config = load_config()
        found = load_saved_ppi(self.monitor, config)
        if found is not None:
            return found
        legacy = config.get(UNKNOWN_MONITOR_KEY, {}).get("ppi")
        if legacy:
            save_ppi(self.monitor, float(legacy))
            return float(legacy)
        return None

    def _poll_monitor(self):
        """Backstop for rearrangements that move no window and raise no event."""
        self.check_monitor()
        self._poll_job = self.root.after(MONITOR_POLL_MS, self._poll_monitor)

    def stop_watching(self):
        """
        Stop the poll. A repeating callback outliving the widgets it draws into
        would come back to a destroyed canvas, so anything taking this window
        apart wants to say so first.
        """
        if self._poll_job is not None:
            self.root.after_cancel(self._poll_job)
            self._poll_job = None

    def check_monitor(self):
        """
        Notice the window has crossed to another monitor, and switch to its
        calibration -- or ask for one, if that monitor has never had one.

        Leaving a half-finished calibration behind is deliberate: it was being
        measured against the panel it was started on, and carrying it across to
        a different one would silently attribute it to the wrong monitor.
        """
        now = describe_monitor(self.root)
        if now.key == self.monitor.key:
            return
        self.monitor = now
        self.saved_ppi = self._adopt_saved_calibration()
        if self.saved_ppi is None:
            self.ppi = self.guess_ppi
            self.mode = "calibrate"
        else:
            self.ppi = self.saved_ppi
            self.mode = "ruler"
        self.redraw()

    # ------------------------------------------------------------------
    # Key bindings
    # ------------------------------------------------------------------
    def _bind_keys(self):
        self.root.bind("<Left>", lambda e: self.nudge(-0.5))
        self.root.bind("<Right>", lambda e: self.nudge(0.5))
        self.root.bind("<Down>", lambda e: self.nudge(-2))
        self.root.bind("<Up>", lambda e: self.nudge(2))
        self.root.bind("-", lambda e: self.nudge(-0.5))
        self.root.bind("+", lambda e: self.nudge(0.5))
        self.root.bind("=", lambda e: self.nudge(0.5))
        self.root.bind("<Return>", lambda e: self.accept_calibration())
        self.root.bind("r", lambda e: self.reset_guess())
        self.root.bind("c", lambda e: self.enter_calibration())
        self.root.bind("q", lambda e: self.root.destroy())
        self.root.bind("<Escape>", lambda e: self.root.destroy())

    def nudge(self, delta_px_per_inch):
        if self.mode != "calibrate":
            return
        self.ppi = max(20.0, self.ppi + delta_px_per_inch)
        self.redraw()

    def reset_guess(self):
        if self.mode != "calibrate":
            return
        self.ppi = self.guess_ppi
        self.redraw()

    def accept_calibration(self):
        if self.mode != "calibrate":
            return
        save_ppi(self.monitor, self.ppi)
        self.saved_ppi = self.ppi
        self.mode = "ruler"
        self.redraw()

    def enter_calibration(self):
        self.mode = "calibrate"
        self.redraw()

    # ------------------------------------------------------------------
    # Drawing
    # ------------------------------------------------------------------
    def _monitor_caption(self):
        """
        How the current monitor is named on screen.

        The EDID model code is included, ugly as it is, because Windows hands
        back "Generic PnP Monitor" for any panel without a vendor driver -- so
        on a desk of two such monitors the label alone would name them both the
        same thing, and telling them apart is the entire point of showing it.
        """
        model = self.monitor.key.split("@")[0]
        name = self.monitor.label
        if model.lower() not in name.lower():
            name = f"{name} [{model}]"
        return f"{name} {self.monitor.width}x{self.monitor.height}"

    def redraw(self):
        self.canvas.delete("all")
        if self.mode == "calibrate":
            self.draw_calibration()
        else:
            self.draw_ruler()

    def draw_calibration(self):
        w = self.canvas.winfo_width()
        h = self.canvas.winfo_height()
        if w < 10 or h < 10:
            return

        card_w_px = self.ppi * (CREDIT_CARD_WIDTH_MM / MM_PER_INCH)
        card_h_px = self.ppi * (CREDIT_CARD_HEIGHT_MM / MM_PER_INCH)

        x0 = (w - card_w_px) / 2
        y0 = (h - card_h_px) / 2 - 40
        x1 = x0 + card_w_px
        y1 = y0 + card_h_px

        self.canvas.create_rectangle(x0, y0, x1, y1, outline="black", width=2)
        self.canvas.create_text(
            (x0 + x1) / 2, (y0 + y1) / 2,
            text="Credit / ID card\n(hold a real card here)",
            font=("Helvetica", 12), fill="gray40", justify="center",
        )

        info_lines = [
            f"CALIBRATION MODE — {self._monitor_caption()}",
            "Hold a real credit/ID card (or bank card) up to the rectangle above.",
            "Press Left/Right (or -/+) until the rectangle matches the card exactly.",
            f"Current estimate: {self.ppi:.1f} px/inch   (OS guess was {self.guess_ppi:.1f})",
            "Press Enter to accept and show the ruler.  Press q to quit.",
            "Each monitor keeps its own calibration; drag this window to another"
            " to set that one up.",
        ]
        ty = y1 + 30
        for i, line in enumerate(info_lines):
            weight = "bold" if i == 0 else "normal"
            self.canvas.create_text(
                w / 2, ty + i * 22,
                text=line, font=("Helvetica", 11, weight), fill="black",
            )

    def draw_ruler(self):
        w = self.canvas.winfo_width()
        h = self.canvas.winfo_height()
        if w < 10 or h < 10:
            return

        margin = 20
        baseline_y = 70

        # Baseline
        self.canvas.create_line(margin, baseline_y, w - margin, baseline_y, width=2)

        max_inches = (w - 2 * margin) / self.ppi
        small_font = tkfont.Font(family="Helvetica", size=9)
        label_font = tkfont.Font(family="Helvetica", size=10, weight="bold")

        # Draw inch and fractional (1/8") tick marks
        eighth = 0.125
        n_ticks = int(max_inches / eighth) + 1
        for i in range(n_ticks):
            inch_val = i * eighth
            x = margin + inch_val * self.ppi
            if abs(inch_val - round(inch_val)) < 1e-9:
                tick_len = 25  # whole inch
                self.canvas.create_line(x, baseline_y, x, baseline_y - tick_len, width=2)
                self.canvas.create_text(
                    x, baseline_y - tick_len - 10,
                    text=str(int(round(inch_val))), font=label_font,
                )
            elif abs((inch_val * 2) - round(inch_val * 2)) < 1e-9:
                tick_len = 16  # half inch
                self.canvas.create_line(x, baseline_y, x, baseline_y - tick_len, width=1)
            elif abs((inch_val * 4) - round(inch_val * 4)) < 1e-9:
                tick_len = 11  # quarter inch
                self.canvas.create_line(x, baseline_y, x, baseline_y - tick_len, width=1)
            else:
                tick_len = 7  # eighth inch
                self.canvas.create_line(x, baseline_y, x, baseline_y - tick_len, width=1)

        self.canvas.create_text(
            margin, baseline_y - 45, anchor="w",
            text="inches", font=small_font, fill="gray30",
        )

        # Draw a cm ruler below it too
        cm_baseline_y = baseline_y + 90
        px_per_cm = self.ppi / 2.54
        max_cm = (w - 2 * margin) / px_per_cm
        self.canvas.create_line(margin, cm_baseline_y, w - margin, cm_baseline_y, width=2)

        n_mm = int(max_cm * 10) + 1
        for i in range(n_mm):
            mm_val = i
            x = margin + (mm_val / 10.0) * px_per_cm
            if mm_val % 10 == 0:
                tick_len = 25
                self.canvas.create_line(x, cm_baseline_y, x, cm_baseline_y - tick_len, width=2)
                self.canvas.create_text(
                    x, cm_baseline_y - tick_len - 10,
                    text=str(mm_val // 10), font=label_font,
                )
            elif mm_val % 5 == 0:
                tick_len = 16
                self.canvas.create_line(x, cm_baseline_y, x, cm_baseline_y - tick_len, width=1)
            else:
                tick_len = 8
                self.canvas.create_line(x, cm_baseline_y, x, cm_baseline_y - tick_len, width=1)

        self.canvas.create_text(
            margin, cm_baseline_y - 45, anchor="w",
            text="centimeters", font=small_font, fill="gray30",
        )

        # Footer info
        footer = (
            f"Calibrated: {self.ppi:.1f} px/inch on {self._monitor_caption()}.  "
            "Drag to another monitor for its own ruler.  "
            "Press 'c' to recalibrate.  Press q to quit."
        )
        self.canvas.create_text(
            w / 2, h - 20, text=footer, font=small_font, fill="gray40",
        )


def main():
    # Before Tk makes a window, or the pixels we draw into are not the
    # monitor's -- see enable_per_monitor_dpi.
    enable_per_monitor_dpi()
    root = tk.Tk()
    root.geometry("900x430")
    root.minsize(500, 380)
    ScreenRulerApp(root)
    root.mainloop()


if __name__ == "__main__":
    sys.exit(main())