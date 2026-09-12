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

Usage
-----
    python screen_ruler.py

Controls in calibration mode:
    Left/Right arrows, or +/- : nudge scale
    Enter                     : accept calibration and show ruler
    r                         : reset to OS-reported guess
    c                         : re-enter calibration mode from ruler view
    q / Esc                   : quit

No third-party dependencies -- uses only the standard library (tkinter).
"""

import json
import os
import sys
import tkinter as tk
from tkinter import font as tkfont

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".screen_ruler_config.json")

# Standard credit-card / ID-card size in mm (ISO/IEC 7810 ID-1) -- a handy,
# widely-available real-world reference object for calibration.
CREDIT_CARD_WIDTH_MM = 85.60
CREDIT_CARD_HEIGHT_MM = 53.98

MM_PER_INCH = 25.4


def load_saved_ppi():
    """Return a previously saved, calibrated PPI value, or None."""
    try:
        with open(CONFIG_PATH, "r") as f:
            data = json.load(f)
            ppi = float(data.get("ppi_x"))
            if ppi > 0:
                return ppi
    except (FileNotFoundError, ValueError, TypeError, json.JSONDecodeError):
        pass
    return None


def save_ppi(ppi):
    try:
        with open(CONFIG_PATH, "w") as f:
            json.dump({"ppi_x": ppi}, f)
    except OSError:
        pass  # non-fatal if we can't write the config


def os_reported_ppi(root):
    """
    Ask the OS for screen width in pixels and millimeters, and derive PPI.
    This is frequently inaccurate (see module docstring) but gives a
    reasonable starting point for calibration.
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
        self.saved_ppi = load_saved_ppi()
        self.ppi = self.saved_ppi if self.saved_ppi else self.guess_ppi

        self.canvas = tk.Canvas(root, bg="white", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)

        self.mode = "calibrate" if self.saved_ppi is None else "ruler"

        self._bind_keys()
        self.canvas.bind("<Configure>", lambda e: self.redraw())

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
        save_ppi(self.ppi)
        self.mode = "ruler"
        self.redraw()

    def enter_calibration(self):
        self.mode = "calibrate"
        self.redraw()

    # ------------------------------------------------------------------
    # Drawing
    # ------------------------------------------------------------------
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
            "CALIBRATION MODE",
            "Hold a real credit/ID card (or bank card) up to the rectangle above.",
            "Press Left/Right (or -/+) until the rectangle matches the card exactly.",
            f"Current estimate: {self.ppi:.1f} px/inch   (OS guess was {self.guess_ppi:.1f})",
            "Press Enter to accept and show the ruler.  Press q to quit.",
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
            f"Calibrated: {self.ppi:.1f} px/inch on this display.  "
            "Press 'c' to recalibrate.  Press q to quit."
        )
        self.canvas.create_text(
            w / 2, h - 20, text=footer, font=small_font, fill="gray40",
        )


def main():
    root = tk.Tk()
    root.geometry("900x400")
    root.minsize(500, 350)
    ScreenRulerApp(root)
    root.mainloop()


if __name__ == "__main__":
    sys.exit(main())