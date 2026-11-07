#!/usr/bin/env python3
"""Record docs/hero.gif: the one flow the README promises (Windows, Pillow).

Runs the real app against docs/demo-sessions.json in a temporary directory,
drives it from inside Tk (expand, select a session, type into the filter,
walk to a match) and grabs the window at ~12 fps.  The rounded Windows 11
corners are cut out with an alpha mask taken once with the two-backdrop
method from make_screenshots.py, so the GIF has transparent corners on both
GitHub themes.  While recording, the window ignores real keyboard and mouse
input, so stray keystrokes cannot end up in the recording.
Output: docs/hero.gif (and docs/hero-poster.png, the last frame).
"""

import os
import shutil
import sys
import tempfile
import time
import tkinter as tk
from pathlib import Path

from PIL import Image, ImageGrab

HERE = Path(__file__).resolve().parent
os.environ["USERPROFILE"] = r"C:\Users\alice"
os.environ["HOME"] = r"C:\Users\alice"
sys.path.insert(0, str(HERE.parent))
from make_screenshots import (  # noqa: E402
    MARGIN,
    display_scale,
    root_hwnd,
    window_bounds,
    with_alpha,
)

import sshtree  # noqa: E402

OUT_GIF = HERE / "hero.gif"
OUT_POSTER = HERE / "hero-poster.png"
FRAME_MS = 80  # ~12 fps
FILTER_TEXT = "web"


class Recorder:
    def __init__(self, app):
        self.app = app
        self.frames: list[Image.Image] = []
        self.bbox = None
        self.mask = None
        self.running = False
        self.saved_bindtags: dict = {}
        self.backdrop = tk.Toplevel(app)
        self.backdrop.overrideredirect(True)
        self.backdrop.attributes("-topmost", True)
        self.backdrop._is_backdrop = True

    def _place_backdrop(self, colour: str) -> None:
        left, upper, right, lower = self.bbox
        scale = display_scale()
        lx, ly = int((left - MARGIN) / scale), int((upper - MARGIN) / scale)
        lw = int((right - left + 2 * MARGIN) / scale) + 2
        lh = int((lower - upper + 2 * MARGIN) / scale) + 2
        self.backdrop.configure(bg=colour)
        self.backdrop.geometry(f"{lw}x{lh}+{lx}+{ly}")
        self.backdrop.deiconify()
        self.backdrop.lift()
        self.app.lift()
        self.app.update()
        time.sleep(0.35)

    def _ignore_real_input(self, ignore: bool) -> None:
        """Drop the class/toplevel/all binding tags so typed keys and clicks do
        nothing; the script calls the widgets directly."""
        widgets = (self.app, self.app.tree, self.app.filter_entry)
        if ignore:
            for w in widgets:
                self.saved_bindtags[w] = w.bindtags()
                w.bindtags((str(w),))
        else:
            for w, tags in self.saved_bindtags.items():
                w.bindtags(tags)

    def start(self) -> None:
        self.app.update_idletasks()
        self._ignore_real_input(True)
        self.bbox = window_bounds(root_hwnd(self.app))
        # alpha mask from one black/white pair; the window does not move later
        captures = []
        for colour in ("#000000", "#ffffff"):
            self._place_backdrop(colour)
            captures.append(ImageGrab.grab(bbox=self.bbox))
        self.mask = with_alpha(*captures).getchannel("A")
        self._place_backdrop("#000000")  # record over black; the mask cuts the corners
        self.running = True
        self._tick()

    def _tick(self) -> None:
        if not self.running:
            return
        self.frames.append(ImageGrab.grab(bbox=self.bbox).convert("RGB"))
        self.app.after(FRAME_MS, self._tick)

    def stop(self) -> None:
        self.running = False
        self.backdrop.withdraw()
        self._ignore_real_input(False)

    def save(self) -> None:
        # one shared palette for all frames (no flicker), 1-bit corner transparency
        base = self.frames[0].quantize(colors=255, method=Image.Quantize.MEDIANCUT)
        palette = base.getpalette()[: 255 * 3] + [
            255,
            0,
            255,
        ]  # index 255 = transparent
        pal_img = Image.new("P", (1, 1))
        pal_img.putpalette(palette)
        cut = self.mask.point(lambda a: 255 if a < 128 else 0)  # 255 where transparent
        out = []
        for frame in self.frames:
            q = frame.quantize(palette=pal_img, dither=Image.Dither.NONE)
            q.paste(255, mask=cut)
            out.append(q)
        out[0].save(
            OUT_GIF,
            save_all=True,
            append_images=out[1:],
            duration=FRAME_MS,
            loop=0,
            transparency=255,
            disposal=1,
            optimize=False,
        )
        poster = self.frames[-1].convert("RGBA")
        poster.putalpha(self.mask)
        poster.save(OUT_POSTER)
        secs = len(self.frames) * FRAME_MS / 1000
        print(
            f"saved {OUT_GIF} ({OUT_GIF.stat().st_size // 1024} KB, "
            f"{len(self.frames)} frames, {secs:.1f} s)"
        )


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="sshtree-hero-"))
    shutil.copy(HERE / "demo-sessions.json", tmp / "sessions.json")
    cfg = sshtree.Config(tmp / "sessions.json")
    cfg.load()

    def select(app, path):
        app.select_path(path)
        app.on_select()

    def drive(app):
        app.geometry("380x640+200+120")
        app.attributes("-topmost", True)
        app.deiconify()
        app.lift()
        app.set_all_open(True)
        app.tree.selection_set(())
        app.update()
        rec = Recorder(app)
        app.after(400, lambda: scene(app, rec))

    def scene(app, rec):
        # neutral status line (the temp config path would show the real user),
        # and no row selected yet (the startup focus logic selects the first one)
        count = sum(1 for _ in sshtree.walk_sessions(app.cfg.tree, []))
        app.set_status(
            f"{count} sessions from C:\\Users\\alice\\.config\\sshtree\\sessions.json"
        )
        app.tree.selection_set(())
        app.tree.focus("")
        app.update()
        rec.start()
        steps = []

        def at(delay_ms, fn):
            steps.append((delay_ms, fn))

        # 1. a session is selected: the status bar shows the exact ssh command
        at(1000, lambda: select(app, ("Clients", "Acme Corp", "acme-db (tunnel)")))
        # 2. start typing: the filter bar opens with the first letter
        t = 2600
        at(t, lambda: app.show_filter(FILTER_TEXT[0]))
        for ch in FILTER_TEXT[1:]:
            t += 330
            at(
                t,
                lambda ch=ch: (
                    app.filter_entry.insert("end", ch),
                    app.filter_entry.icursor("end"),
                ),
            )
        # 3. Enter: jump to the first match, then walk down the matches
        at(t + 900, app.focus_first_session)
        at(t + 1500, lambda: select(app, ("Clients", "Acme Corp", "acme-web-02")))
        at(t + 2000, lambda: select(app, ("Cloud", "web-eu-1")))
        # 4. hold, then stop
        at(t + 4200, lambda: (rec.stop(), rec.save(), app.quit_app()))
        for delay, fn in steps:
            app.after(delay, fn)

    sshtree.run_gui(cfg, on_ready=drive)
    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
