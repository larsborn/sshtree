#!/usr/bin/env python3
"""Take the README screenshots from docs/demo-sessions.json (Windows, Pillow).

Runs the real app against the demo config in a temporary directory, so the
user's own config and state are untouched.  Every window is captured twice,
over a black and over a white backdrop window placed right behind it; the two
captures give an exact alpha channel, so the rounded Windows 11 corners end up
transparent instead of showing a few pixels of desktop.
Output: docs/screenshots/*.png
"""

import ctypes
import os
import shutil
import sys
import tempfile
import time
import tkinter as tk
from ctypes import wintypes
from pathlib import Path

from PIL import Image, ImageGrab

HERE = Path(__file__).resolve().parent
# a neutral home directory, so expanded key paths in the status bar read
# C:\Users\alice\... instead of the real user's
os.environ["USERPROFILE"] = r"C:\Users\alice"
os.environ["HOME"] = r"C:\Users\alice"
sys.path.insert(0, str(HERE.parent))
import sshtree  # noqa: E402

OUT = HERE / "screenshots"
MARGIN = 24


def window_bounds(hwnd: int) -> tuple[int, int, int, int]:
    """Visible frame of a window (no invisible resize borders, no shadow)."""
    rect = wintypes.RECT()
    dwm = ctypes.WinDLL("dwmapi")
    if (
        dwm.DwmGetWindowAttribute(
            wintypes.HWND(hwnd), 9, ctypes.byref(rect), ctypes.sizeof(rect)
        )
        != 0
    ):
        ctypes.WinDLL("user32").GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect))
    return rect.left, rect.top, rect.right, rect.bottom


def display_scale() -> float:
    """Windows display scaling (1.25 for 125%).  Tk places windows in logical
    pixels, while DWM bounds and screen grabs are physical pixels."""
    try:
        return ctypes.WinDLL("shcore").GetScaleFactorForDevice(0) / 100.0
    except (OSError, AttributeError):
        return 1.0


def root_hwnd(widget) -> int:
    user32 = ctypes.WinDLL("user32")
    user32.GetAncestor.restype = wintypes.HWND
    return user32.GetAncestor(wintypes.HWND(widget.winfo_id()), 2) or widget.winfo_id()


def with_alpha(on_black: Image.Image, on_white: Image.Image) -> Image.Image:
    """Recover colour and alpha from captures over black and over white:
    white - black = (255 - alpha) per channel, black = colour * alpha."""
    black = on_black.convert("RGB").load()
    white = on_white.convert("RGB").load()
    out = Image.new("RGBA", on_black.size)
    px = out.load()
    for y in range(on_black.height):
        for x in range(on_black.width):
            b, w = black[x, y], white[x, y]
            alpha = 255 - max(w[i] - b[i] for i in range(3))
            alpha = max(0, min(255, alpha))
            if alpha == 0:
                px[x, y] = (0, 0, 0, 0)
            else:
                px[x, y] = tuple(min(255, round(c * 255 / alpha)) for c in b) + (alpha,)
    return out


class Shooter:
    def __init__(self, app):
        self.app = app
        self.backdrop = tk.Toplevel(app)
        self.backdrop.overrideredirect(True)
        self.backdrop.attributes("-topmost", True)

    def shoot(self, widget, name: str) -> None:
        top = widget.winfo_toplevel()
        top.update_idletasks()
        left, upper, right, lower = window_bounds(root_hwnd(widget))
        scale = display_scale()
        # backdrop geometry in Tk's logical pixels, rounded outwards
        lx, ly = int((left - MARGIN) / scale), int((upper - MARGIN) / scale)
        lw = int((right - left + 2 * MARGIN) / scale) + 2
        lh = int((lower - upper + 2 * MARGIN) / scale) + 2
        self.backdrop.geometry(f"{lw}x{lh}+{lx}+{ly}")
        captures = []
        for colour in ("#000000", "#ffffff"):
            self.backdrop.configure(bg=colour)
            self.backdrop.deiconify()
            self.backdrop.lift()
            top.lift()
            top.update()
            time.sleep(0.35)  # let the compositor settle
            captures.append(ImageGrab.grab(bbox=(left, upper, right, lower)))
        self.backdrop.withdraw()
        with_alpha(*captures).save(OUT / f"{name}.png")
        print("saved", OUT / f"{name}.png")


def main() -> None:
    OUT.mkdir(exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="sshtree-shots-"))
    shutil.copy(HERE / "demo-sessions.json", tmp / "sessions.json")
    cfg = sshtree.Config(tmp / "sessions.json")
    cfg.load()

    def dialog(app):
        return [
            w
            for w in app.winfo_children()
            if isinstance(w, tk.Toplevel) and not getattr(w, "_is_backdrop", False)
        ][-1]

    def step_main(app, shooter):
        app.set_all_open(True)
        app.select_path(("Clients", "Acme Corp", "acme-db (tunnel)"))
        app.on_select()
        app.update()
        app.after(300, lambda: (shooter.shoot(app, "main"), step_filter(app, shooter)))

    def step_filter(app, shooter):
        app.show_filter("db")
        app.update()
        app.after(
            300, lambda: (shooter.shoot(app, "filter"), step_dialog(app, shooter))
        )

    def step_dialog(app, shooter):
        app.hide_filter()
        app.select_path(("Clients", "Acme Corp", "acme-db (tunnel)"))
        # keep the main window out from under the dialog's corners
        app.geometry("+1000+120")
        app.update()

        def on_dialog():
            dlg = dialog(app)
            dlg.geometry("+100+120")
            dlg.update()
            time.sleep(0.3)
            shooter.shoot(dlg, "session-dialog")
            dlg.notebook.select(1)
            dlg.update()
            shooter.shoot(dlg, "session-dialog-tunnels")
            dlg.destroy()
            app.after(200, app.quit_app)

        app.after(500, on_dialog)
        app.edit_session()

    def drive(app):
        app.geometry("380x640+200+120")
        app.attributes("-topmost", True)
        app.deiconify()
        app.lift()
        app.update()
        shooter = Shooter(app)
        shooter.backdrop._is_backdrop = True
        app.after(500, lambda: step_main(app, shooter))

    sshtree.run_gui(cfg, on_ready=drive)
    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
