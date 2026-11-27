"""Render the recorder's pixel kitten into docs/kitten.gif for the README.

    python tools/make_kitten_gif.py

Plays the same frames the recorder uses: pops out of its box, runs (OK), slows down and
plods (quiet), lies down and sleeps (SILENT), wakes up, runs again, hops back in its box.
"""
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import record_class as rc  # noqa: E402

SCALE = 8                      # screen pixels per sprite pixel
BG = (30, 30, 36)
TEXT = (220, 220, 220)
MOOD_COLOR = {"OK": (80, 200, 120), "quiet": (230, 190, 70), "SILENT": (230, 80, 80), "": TEXT}
TICK_MS = 1000 / 16            # the recorder redraws 16 times a second


def xterm_rgb(n):
    """xterm-256 colour index -> RGB."""
    if n < 16:
        base = [(0, 0, 0), (128, 0, 0), (0, 128, 0), (128, 128, 0), (0, 0, 128), (128, 0, 128),
                (0, 128, 128), (192, 192, 192), (128, 128, 128), (255, 0, 0), (0, 255, 0),
                (255, 255, 0), (0, 0, 255), (255, 0, 255), (0, 255, 255), (255, 255, 255)]
        return base[n]
    if n < 232:
        n -= 16
        steps = [0, 95, 135, 175, 215, 255]
        return steps[n // 36], steps[(n // 6) % 6], steps[n % 6]
    v = 8 + (n - 232) * 10
    return v, v, v


def render(pixels, caption, mood, zzz=""):
    w, h = rc.CAT_WIDTH, rc.CAT_HEIGHT
    img = Image.new("RGB", (w * SCALE, h * SCALE + 40), BG)
    d = ImageDraw.Draw(img)
    for y, row in enumerate(pixels):
        for x, ch in enumerate(row):
            if ch in rc._CAT_COLORS:
                c = xterm_rgb(rc._CAT_COLORS[ch])
                d.rectangle([x * SCALE, y * SCALE, (x + 1) * SCALE - 1, (y + 1) * SCALE - 1], fill=c)
    font = ImageFont.load_default(size=18)
    if zzz:
        d.text((17 * SCALE, 2 * SCALE), zzz, fill=TEXT, font=font)
    d.text((10, h * SCALE + 10), caption, fill=MOOD_COLOR[mood], font=font)
    return img


def clip(name, caption, mood, ticks_per_frame, loops=1, zzz=False):
    frames = []
    poses = rc._CAT_POSES[name]
    for loop in range(loops):
        for i, pixels in enumerate(poses):
            z = rc._ZZZ[(loop * len(poses) + i) % len(rc._ZZZ)] if zzz else ""
            frames.append((render(pixels, caption, mood, z), ticks_per_frame * TICK_MS))
    return frames


T = rc._CAT_TRANSITION_TICKS
S = rc._CAT_SPEED
story = (clip("intro", "recording starts", "", T)
         + clip("run", "OK: hearing the lecturer", "OK", S["run"], loops=4)
         + clip("to_walk", "quiet: very faint", "quiet", T)
         + clip("walk", "quiet: very faint", "quiet", S["walk"], loops=2)
         + clip("to_sleep", "SILENT: check the mic!", "SILENT", T)
         + clip("sleep", "SILENT: check the mic!", "SILENT", S["sleep"], loops=3, zzz=True)
         + clip("from_sleep", "OK again", "OK", T)
         + clip("to_run", "OK again", "OK", T)
         + clip("run", "OK again", "OK", S["run"], loops=3)
         + clip("outro", "recording saved", "", T))

out = ROOT / "docs" / "kitten.gif"
out.parent.mkdir(exist_ok=True)
images = [im for im, _ in story]
durations = [round(ms) for _, ms in story]
images[0].save(out, save_all=True, append_images=images[1:], duration=durations, loop=0, optimize=True)
print(f"{out}  ({len(images)} frames, {sum(durations) / 1000:.1f} s, {out.stat().st_size // 1024} KB)")
