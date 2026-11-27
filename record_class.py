"""
Record Class
------------
Double-click (via "Record Class.bat") to start. It checks your timetable, picks
the class that is on right now, names the file automatically and starts recording.

- Physical class -> records the default microphone
- Online class   -> records the speaker output (what you hear in Teams/Zoom)
- Press Enter to stop. It also stops by itself a while after the class ends
  (recorder.auto_stop_after_end), or right when the next class starts if they
  are back-to-back.

Set recorder.online_only = true (or pass --online-only) on a desktop that only
joins online classes: physical slots are ignored and it always records speaker
output.

Output (inside the synced recordings folder):
    _Recordings/FIN201/FIN201 2026-11-27 Tutorial.flac
    _Recordings/FIN201/FIN201 2026-11-27 Tutorial.json   (details for the transcriber)
"""

import argparse
import json
import os
import signal
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import pipeline_config


def at(day: datetime, hhmm: str) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return day.replace(hour=h, minute=m, second=0, microsecond=0)


def current_class(now, timetable, early_minutes=20, auto_stop_after_end=15, online_only=False):
    """Return the class on now, or None.

    A class counts from `early_minutes` before its start until its end. If two
    overlap (back-to-back), the one already started wins. Recording stops
    `auto_stop_after_end` minutes after the end, or earlier if another class
    starts in between, so the next class gets its own file.
    """
    def eligible(slot):
        return slot.weekday == now.weekday() and not (online_only and slot.mode != "Online")

    hits = []
    for slot in filter(eligible, timetable):
        start, end = at(now, slot.start), at(now, slot.end)
        if start - timedelta(minutes=early_minutes) <= now < end:
            hits.append((start <= now, start, end, slot))
    if not hits:
        return None
    hits.sort(key=lambda h: (not h[0], h[1]))
    _, start, end, slot = hits[0]

    stop_at = end + timedelta(minutes=auto_stop_after_end)
    for other in filter(eligible, timetable):
        nxt = at(now, other.start)
        if end <= nxt < stop_at:
            stop_at = nxt
    return dict(code=slot.course, session=slot.session, mode=slot.mode, end=end, stop_at=stop_at)


def pick_manually(courses, online_only=False, heading="No class on right now. Pick one:"):
    print(heading)
    codes = list(courses)
    for i, c in enumerate(codes, 1):
        print(f"  {i}. {courses[c]}")
    code = codes[int(input("Course number: ")) - 1]
    session = pipeline_config.SESSIONS[int(input("1 Lecture / 2 Tutorial / 3 Workshop: ")) - 1]
    if online_only:
        mode = "Online"
    else:
        mode = pipeline_config.MODES[int(input("1 Physical (mic) / 2 Online (speaker audio): ")) - 1]
    return dict(code=code, session=session, mode=mode, stop_at=datetime.now() + timedelta(hours=3))


def pick_mic(sc, preferred):
    """First plugged-in mic whose name contains one of `preferred` (in order), else the default."""
    mics = sc.all_microphones(include_loopback=False)
    for want in preferred:
        for m in mics:
            if want.lower() in m.name.lower():
                return m
    return sc.default_microphone()


SILENT_DB = -58   # default; override with recorder.silent_db


def to_db(chunk) -> float:
    """RMS level of an audio chunk in dBFS (0 = full scale, -100 = digital silence)."""
    import numpy as np
    rms = float(np.sqrt(np.mean(np.square(chunk, dtype="float64")))) if len(chunk) else 0.0
    return max(-100.0, 20 * np.log10(rms + 1e-10))


def is_silent(levels, window: int, threshold: float = SILENT_DB) -> bool:
    """True when a full window of per-second levels has its loud seconds (90th pct) below threshold.

    A working mic in a classroom sits around -45 to -25 dB; a dead, muted or
    wrong input sits near -65 or lower. The 90th percentile (not the max) means
    a few clicks or a bumped desk don't hide a mic that is otherwise dead.
    """
    if len(levels) < window:
        return False
    recent = sorted(levels[-window:])
    return recent[int(0.9 * (len(recent) - 1))] < threshold


def alert(text: str, on: bool, beep: bool = False):
    """Make the console window hard to miss: title + taskbar flash (+ optional sound). Windows only."""
    if sys.platform != "win32":
        return
    import ctypes
    try:
        ctypes.windll.kernel32.SetConsoleTitleW(text)
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if on and hwnd:
            ctypes.windll.user32.FlashWindow(hwnd, True)
        if on and beep:
            import winsound
            winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
    except Exception:
        pass


PIE = "\u25cb\u25d4\u25d1\u25d5\u25cf"   # empty, quarter, half, three quarters, full circle
PIE_ASCII = ".oO@@"


def pie(frac: float, chars: str = PIE) -> str:
    """A circle that fills up as frac goes 0 -> 1."""
    return chars[max(0, min(len(chars) - 1, int(frac * (len(chars) - 1) + 0.5)))]


def can_print(text: str) -> bool:
    try:
        text.encode(sys.stdout.encoding or "ascii")
        return True
    except (UnicodeEncodeError, LookupError):
        return False


def fmt_elapsed(seconds) -> str:
    """0:12:34, the same h:mm:ss format as the transcript timestamps."""
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}"


WAVE_LOW, WAVE_HIGH = -70.0, -20.0     # dB range drawn by the wave (quiet room ... loud voice)
_DOTS = ((0, 0, 0x01), (1, 0, 0x02), (2, 0, 0x04), (0, 1, 0x08),   # braille dot -> bit
         (1, 1, 0x10), (2, 1, 0x20), (3, 0, 0x40), (3, 1, 0x80))


# ---- the cat: an original chibi kitten in colour pixel art (2 square pixels per character) ----
# . clear  K outline  B brown  S stripe  C cream  P pink  E eye  R collar  G bell  D dust
_CAT_COLORS = {"K": 52, "B": 137, "S": 94, "C": 223, "P": 211, "E": 16, "R": 160, "G": 220, "D": 248}
CAT_WIDTH, CAT_HEIGHT = 26, 16       # pixels; 16 px = 8 text lines
_CAT_POSES = {   # run: 6-frame trot (each leg pair alternates), bounce, tail wave, dust
    "run": [
        [
            "..........................",
            "..........................",
            "...............K.....K....",
            "..............KPK...KPK...",
            "..............KBPKKKPBK...",
            "KK...........KBBSBBBSBBK..",
            "KBKK.........KBBBBBBBBBK..",
            ".KBBKKKKKKKKKKBEBBBBBEBK..",
            "..KKKBSBBSBBSKBEBCCCBEBK..",
            "..KBBSBBSBBSBKBBCCPCCBBK..",
            ".KBBBSBBSBBSBBKBBCCCBBK...",
            ".KBBBBBBBBBBBBBKRRGRRK....",
            ".KBBBBBBBBBBBBCCCCK.......",
            "..KKKBBBBBBBBBKKKK........",
            "....KBKK..KBK....KBK......",
            "...KKKKKKKKK......KKK.....",
        ],
        [
            "..........................",
            "...............K.....K....",
            "..............KPK...KPK...",
            "K.............KBPKKKPBK...",
            "KBK..........KBBSBBBSBBK..",
            ".KBKK........KBBBBBBBBBK..",
            "..KBKKKKKKKKKKBEBBBBBEBK..",
            "..KKKBSBBSBBSKBEBCCCBEBK..",
            "..KBBSBBSBBSBKBBCCPCCBBK..",
            ".KBBBSBBSBBSBBKBBCCCBBK...",
            ".KBBBBBBBBBBBBBKRRGRRK....",
            ".KBBBBBBBBBBBBCCCCK.......",
            "..KKKBBBBBBBBBKKKK........",
            "....KKBK...KBK..KKK.......",
            "....KKK...KKK.............",
            "..........................",
        ],
        [
            "..........................",
            "..........................",
            "...............K.....K....",
            "..............KPK...KPK...",
            "..............KBPKKKPBK...",
            ".............KBBSBBBSBBK..",
            "KKKK.........KBBBBBBBBBK..",
            "KBBBKKKKKKKKKKBEBBBBBEBK..",
            ".KKKKBSBBSBBSKBEBCCCBEBK..",
            "..KBBSBBSBBSBKBBCCPCCBBK..",
            ".KBBBSBBSBBSBBKBBCCCBBK...",
            ".KBBBBBBBBBBBBBKRRGRRK....",
            ".KBBBBBBBBBBBBCCCCK.......",
            "..KKKBBBBBBBBBKKKK........",
            ".D.KKKKBK...KBKKKK........",
            "D......KKK...KKK..........",
        ],
        [
            "..........................",
            "..........................",
            "...............K.....K....",
            "..............KPK...KPK...",
            "..............KBPKKKPBK...",
            "KK...........KBBSBBBSBBK..",
            "KBKK.........KBBBBBBBBBK..",
            ".KBBKKKKKKKKKKBEBBBBBEBK..",
            "..KKKBSBBSBBSKBEBCCCBEBK..",
            "..KBBSBBSBBSBKBBCCPCCBBK..",
            ".KBBBSBBSBBSBBKBBCCCBBK...",
            ".KBBBBBBBBBBBBBKRRGRRK....",
            ".KBBBBBBBBBBBBCCCCK.......",
            "..KKKBBBBBBBBBKKKK........",
            ".KBK....KBK..KBKK.........",
            "KKK......KKKKKKKKK........",
        ],
        [
            "..........................",
            "...............K.....K....",
            "..............KPK...KPK...",
            "K.............KBPKKKPBK...",
            "KBK..........KBBSBBBSBBK..",
            ".KBKK........KBBBBBBBBBK..",
            "..KBKKKKKKKKKKBEBBBBBEBK..",
            "..KKKBSBBSBBSKBEBCCCBEBK..",
            "..KBBSBBSBBSBKBBCCPCCBBK..",
            ".KBBBSBBSBBSBBKBBCCCBBK...",
            ".KBBBBBBBBBBBBBKRRGRRK....",
            ".KBBBBBBBBBBBBCCCCK.......",
            "..KKKBBBBBBBBBKKKK........",
            "..KBK..KKK...KKBK.........",
            ".KKK.........KKK..........",
            "..........................",
        ],
        [
            "..........................",
            "..........................",
            "...............K.....K....",
            "..............KPK...KPK...",
            "..............KBPKKKPBK...",
            ".............KBBSBBBSBBK..",
            "KKKK.........KBBBBBBBBBK..",
            "KBBBKKKKKKKKKKBEBBBBBEBK..",
            ".KKKKBSBBSBBSKBEBCCCBEBK..",
            "..KBBSBBSBBSBKBBCCPCCBBK..",
            ".KBBBSBBSBBSBBKBBCCCBBK...",
            ".KBBBBBBBBBBBBBKRRGRRK....",
            ".KBBBBBBBBBBBBCCCCK.......",
            "..KKKBBBBBBBBBKKKK........",
            ".D..KKKK...KKK..KBK.......",
            "D....KKK.........KKK......",
        ],
    ],
    "walk": [
        [
            "..........................",
            "..........................",
            "K..............K.....K....",
            "KBK...........KPK...KPK...",
            ".KBK..........KBPKKKPBK...",
            ".KBK.........KBBSBBBSBBK..",
            "..KBK........KBBBBBBBBBK..",
            "..KBKKKKKKKKKKBKKBBBKKBK..",
            "...KKBSBBSBBSKBEBCCCBEBK..",
            "..KBBSBBSBBSBKBBCCPCCBBK..",
            ".KBBBSBBSBBSBBKBBCCCBBK...",
            ".KBBBBBBBBBBBBBKRRGRRK....",
            ".KBBBBBBBBBBBBCCCCK.......",
            "..KKKBBBBBBBBBKKKKBK......",
            "...KBKKBK.....KBKKBK......",
            "...KKKKKK.....KKKKKK......",
        ],
        [
            "..........................",
            "..........................",
            "K..............K.....K....",
            "KBK...........KPK...KPK...",
            ".KBK..........KBPKKKPBK...",
            ".KBK.........KBBSBBBSBBK..",
            "..KBK........KBBBBBBBBBK..",
            "..KBKKKKKKKKKKBKKBBBKKBK..",
            "...KKBSBBSBBSKBEBCCCBEBK..",
            "..KBBSBBSBBSBKBBCCPCCBBK..",
            ".KBBBSBBSBBSBBKBBCCCBBK...",
            ".KBBBBBBBBBBBBBKRRGRRK....",
            ".KBBBBBBBBBBBBCCCCK.......",
            "..KKKBBBBBBBBBKKKKBK......",
            "..KBK..KBK...KBK..KBK.....",
            "..KKK..KKK...KKK..KKK.....",
        ],
    ],
    "sleep": [
        [
            "..........................",
            "..........................",
            "..........................",
            "..........................",
            "..........................",
            "..........................",
            "..............K.....K.....",
            ".............KPK...KPK....",
            "....KKKKKKKKKKBPKKKPBK....",
            "...KBSBBSBBSKBBSBBBSBBK...",
            "..KBBSBBSBBSKBBBBBBBBBK...",
            ".KBBBBBBBBBBKBKKCCCKKBK...",
            ".KBBBBBBBBBBKBBCCPCCBBK...",
            ".KBBBBBBBBBBBKBBCCCBBK....",
            ".KKBBBBBBBBBBBKKKKKKK.....",
            "..KKKKKKKKKKKKKKKKKKKK....",
        ],
        [
            "..........................",
            "..........................",
            "..........................",
            "..........................",
            "..........................",
            "..........................",
            "..............K.....K.....",
            ".............KPK...KPK....",
            "...KKKKKKKKKKKBPKKKPBK....",
            "...KBSBBSBBSKBBSBBBSBBK...",
            "..KBBSBBSBBSKBBBBBBBBBK...",
            ".KBBBBBBBBBBKBKKCCCKKBK...",
            ".KBBBBBBBBBBKBBCCPCCBBK...",
            ".KBBBBBBBBBBBKBBCCCBBK....",
            ".KKBBBBBBBBBBBKKKKKKK.....",
            "..KKKKKKKKKKKKKKKKKKKK....",
        ],
    ],
}


def _cat_text(pixels, color=True):
    """Two pixel rows per text line: upper half block in the top colour on the bottom colour."""
    lines = []
    for y in range(0, CAT_HEIGHT, 2):
        out = []
        for top, bot in zip(pixels[y], pixels[y + 1]):
            t, b = _CAT_COLORS.get(top), _CAT_COLORS.get(bot)
            if t is None and b is None:
                out.append(" ")
            elif not color:
                out.append("\u2588" if t is not None and b is not None else "\u2580" if t is not None else "\u2584")
            elif t is not None and b is not None:
                out.append(f"\033[38;5;{t};48;5;{b}m\u2580\033[0m")
            elif t is not None:
                out.append(f"\033[38;5;{t}m\u2580\033[0m")
            else:
                out.append(f"\033[38;5;{b}m\u2584\033[0m")
        lines.append("".join(out))
    return lines


_CAT_SPEED = {"run": 2, "walk": 8, "sleep": 16}      # meter updates (1/16 s) per frame
_ZZZ = ("z", "z Z", "z Z z")
_CAT_CACHE = {}


def cat_lines(mood: str, tick: int, color: bool = True):
    """The cat's 8 text lines: 'run' (OK), 'walk' (quiet, worn out), 'sleep' (SILENT)."""
    poses = _CAT_POSES[mood]
    i = tick // _CAT_SPEED[mood]
    key = (mood, i % len(poses), color)
    if key not in _CAT_CACHE:
        _CAT_CACHE[key] = _cat_text(poses[key[1]], color)
    lines = list(_CAT_CACHE[key])
    if mood == "sleep":                                 # z's floating up from the sleeping head
        zzz = _ZZZ[i % len(_ZZZ)]
        lines[2] = (" " * 17 + zzz).ljust(CAT_WIDTH)          # above the head (right side)
    return lines


def clock_rows(frac: float, rows: int, erase: bool = False):
    """A round clock face `rows` text lines tall, filled clockwise from 12 o'clock to `frac`.

    With erase=True the hand sweeps the fill away instead, so alternating seconds
    look like one continuous spin rather than a blink back to empty.
    """
    import math
    import numpy as np
    H = W = rows * 4                     # braille dots are roughly square, so this is round
    y, x = np.mgrid[0:H, 0:W]
    dx, dy = x + 0.5 - W / 2, y + 0.5 - H / 2
    r = np.hypot(dx, dy)
    angle = np.arctan2(dx, -dy) % (2 * math.pi)           # 0 at 12 o'clock, clockwise
    R = H / 2 - 0.3
    ring = (r <= R) & (r >= R - 1.3)
    swept = angle <= frac * 2 * math.pi
    wedge = (r <= R) & (~swept if erase else swept)
    return braille_rows(ring | wedge)


def braille_rows(grid):
    """Turn a boolean dot grid (height multiple of 4, even width) into braille text lines."""
    H, W = grid.shape
    lines = []
    for R in range(0, H, 4):
        line = []
        for C in range(0, W, 2):
            code = 0
            for dy, dx, bit in _DOTS:
                if grid[R + dy, C + dx]:
                    code |= bit
            line.append(chr(0x2800 + code))
        lines.append("".join(line))
    return lines


class LiveMeter:
    """Live 'voice memo' wave of the input level, redrawn in place in the console.

          REC 12:34   -32 dB  OK
          ⢀⣠⣀⣀⣄⠀⠀⠀⠀⢀⣄⠀⠀⠀⠀⢀⣤⡀⠀⣀⣀⣄⣀⣴⣤⣤⣀
          ⣿⣿⣿⣿⣿⡷⠶⠶⠶⣿⣿⡷⠶⠶⠶⢾⣿⣿⠶⣿⣿⣿⣿⣿⣿⣿⣿⣿
          ⠈⠙⠉⠉⠋⠀⠀⠀⠀⠈⠋⠀⠀⠀⠀⠈⠛⠁⠀⠉⠉⠋⠉⠻⠛⠛⠉

    The wave swelling when someone talks = the mic works. A flat line = it doesn't.
    Falls back to one line of ASCII bars when the output isn't a console that can
    show braille characters.
    """

    ROWS = 6                 # text rows of wave (x4 braille dots = 24 dots tall)

    def __init__(self, width=40, label="REC", splits=1):
        self.width, self.label, self.splits = width, label, splits   # splits: columns per update
        self.dbs, self.lines = [], 0
        tty = sys.stdout.isatty()
        try:
            "\u28ff\u2581".encode(sys.stdout.encoding or "ascii")
            braille = True
        except (UnicodeEncodeError, LookupError):
            braille = False
        self.fancy = tty and braille
        self.pie = PIE if can_print(PIE) else PIE_ASCII
        self.cat = self.fancy and can_print("\u2588\u2580\u2584")   # pixel cat needs block characters
        self.tick = 0
        self.color = tty
        if tty and sys.platform == "win32":
            os.system("")                      # turns on ANSI cursor moves/colours in the Windows console

    def _rows(self):
        """Mirrored braille wave, ROWS tall, newest on the right."""
        import numpy as np
        cols = self.width * 2
        v = np.array(([0.0] * cols + self.view())[-cols:])
        v = np.convolve(v, [0.25, 0.5, 0.25], mode="same")         # soften into curves
        H = self.ROWS * 4
        d = np.abs(np.arange(H) + 0.5 - H / 2)[:, None]            # distance of each dot row from the centre
        grid = d <= np.maximum(v * H / 2, 0.5)[None, :]
        return braille_rows(grid)

    def view(self):
        """Levels 0..1 for drawing, measured from the room's own noise floor.

        Steady background noise (fans, aircon, a hot mic) sits at the floor and
        draws as a thin line; speech rises above it and swells the wave. A dead
        input has no floor above WAVE_LOW, so it stays flat too.
        """
        import numpy as np
        recent = self.dbs[-self.width * 2:]
        floor = max(WAVE_LOW, float(np.percentile(self.dbs, 20)) + 2)
        span = max(20.0, WAVE_HIGH - floor) if floor < WAVE_HIGH else 20.0
        return [min(1.0, max(0.0, (d - floor) / span)) for d in recent]

    def update(self, samples, elapsed):
        """Feed the newest audio (any length); redraws the meter."""
        import numpy as np
        for part in np.array_split(np.asarray(samples).reshape(-1), self.splits):
            self.dbs.append(to_db(part))
        self.dbs = self.dbs[-self.width * 8:]                    # ~20 s, for the noise floor

        loud = max(self.dbs[-32:])                               # loudest of the last ~2 s (16 cols/s)
        status, col = ("OK", "32") if loud >= -50 else ("quiet", "33") if loud >= SILENT_DB else ("SILENT", "31")
        paint = (lambda s: f"\033[{col}m{s}\033[0m") if self.color else (lambda s: s)
        dot = "\033[31m\u25cf\033[0m " if self.color and self.fancy else ""
        head = f"  {dot}{self.label} {fmt_elapsed(elapsed)}   {loud:4.0f} dB  {paint(status)}"

        if self.fancy:
            rows = ["  " + paint(r) for r in self._rows()]
            if self.cat:
                self.tick += 1
                mood = {"OK": "run", "quiet": "walk"}.get(status, "sleep")
                cat = cat_lines(mood, self.tick, self.color)
                blank = " " * (self.width + 2)
                rows = [blank] * (len(cat) - len(rows)) + rows     # the cat stands taller than the wave
                rows = [w + "   " + c for w, c in zip(rows, cat)]
            self._draw([head] + rows)
        else:
            chars = " .:-=+*#@"
            bars = "".join(chars[round(v * (len(chars) - 1))] for v in self.view()[-self.width:])
            sys.stdout.write(f"\r{head} {pie((elapsed % 60) / 60, self.pie)}  {bars.ljust(self.width)}   ")
            sys.stdout.flush()
            self.lines = 1

    def _draw(self, lines):
        up = f"\033[{self.lines - 1}A" if self.lines > 1 else ""
        sys.stdout.write(up + "\r" + "\n".join(line + "\033[K" for line in lines))
        sys.stdout.flush()
        self.lines = len(lines)

    def say(self, text=""):
        """Print a normal message above the meter (the meter redraws on the next update)."""
        if self.lines:
            if self.fancy:
                up = f"\033[{self.lines - 1}A" if self.lines > 1 else ""
                sys.stdout.write(up + "\r\033[J")
            else:
                sys.stdout.write("\r" + " " * (self.width + 40) + "\r")
            self.lines = 0
        print(text, flush=True)


def wait_for_enter(stop):
    """Set `stop` when Enter is pressed (or stdin closes)."""
    try:
        input()
    except (EOFError, OSError):
        pass
    stop.set()


def mic_test(source, np, sample_rate=16000):
    """Show the live wave without saving anything, until Enter."""
    stop = threading.Event()
    threading.Thread(target=wait_for_enter, args=(stop,), daemon=True).start()
    signal.signal(signal.SIGINT, lambda *_: stop.set())     # Ctrl+C stops cleanly too
    meter, started = LiveMeter(label="TEST"), time.time()
    print(f"Mic test: {source.name}\nTalk or tap the mic: the wave should jump. Press Enter to finish.\n")
    with source.recorder(samplerate=sample_rate, channels=1) as rec:
        while not stop.is_set():
            chunk = np.asarray(rec.record(numframes=sample_rate // 16), dtype="float32").reshape(-1)
            meter.update(chunk, time.time() - started)
    meter.say()


def confirm_class(cls, name, seconds=5):
    """Show the class the timetable picked and count down before recording.

    M = pick a different class (replacement or swapped class), Enter = start now.
    Doing nothing starts it after `seconds`, so a missed glance never costs a recording.
    """
    print(f"  Timetable: {name} ({cls['session']}, {cls['mode']}) until {cls['end']:%H:%M}")
    if sys.platform != "win32" or not sys.stdin.isatty():
        return True
    import msvcrt
    big = sys.stdout.isatty() and can_print("\u28ff")          # braille clock, else one line
    if big:
        os.system("")                                          # ANSI cursor moves in the Windows console
    drawn = 0
    begin = time.time()
    deadline = begin + seconds
    while time.time() < deadline:
        now = time.time()
        left = max(1, int(deadline - now + 0.999))             # round up: 5, 4, 3, 2, 1
        if big:
            spent = now - begin                                # one turn of the clock per second
            clock = clock_rows(spent % 1, 6, erase=int(spent) % 2 == 1)
            side = ["", f"Starting in {left} s", "", "[M]     different class", "[Enter] start now", ""]
            lines = [f"     \033[36m{c}\033[0m     {s}" for c, s in zip(clock, side)]
            up = f"\033[{drawn - 1}A" if drawn > 1 else ""
            sys.stdout.write(up + "\r" + "\n".join(line + "\033[K" for line in lines))
            drawn = len(lines)
        else:
            circle = pie(1 - (deadline - now) / seconds, PIE if can_print(PIE) else PIE_ASCII)
            sys.stdout.write(f"\r  {circle} Starting in {left} s   [M] different class   [Enter] start now ")
        sys.stdout.flush()
        while msvcrt.kbhit():
            key = msvcrt.getwch()
            if key in "mM":
                print()
                return False
            if key in "\r\n ":
                deadline = 0
        time.sleep(0.03)
    print()
    return True


def unique_path(folder: Path, stem: str) -> Path:
    p = folder / f"{stem}.flac"
    n = 2
    while p.exists():
        p = folder / f"{stem} ({n}).flac"
        n += 1
    return p


def main():
    ap = argparse.ArgumentParser(description="Record the class that is on now.")
    ap.add_argument("--online-only", action="store_true", help="ignore physical classes, always record speaker output")
    ap.add_argument("--test", action="store_true", help="show the live mic wave without recording")
    ap.add_argument("--mics", action="store_true", help="list microphones and show which one would be used")
    args = ap.parse_args()

    cfg = pipeline_config.load()
    rc = cfg.recorder
    online_only = args.online_only or rc.get("online_only", False)
    sample_rate = rc.get("sample_rate", 16000)
    preferred = rc.get("preferred_mics", ["external", "headset", "usb"])
    silent_db = rc.get("silent_db", SILENT_DB)
    beep = rc.get("beep_on_silence", False)

    import numpy as np
    import soundcard as sc
    import soundfile as sf
    import warnings
    warnings.filterwarnings("ignore", message="data discontinuity")  # soundcard noise; hides the real warnings

    if args.mics:
        print("Microphones on this computer:")
        for m in sc.all_microphones(include_loopback=False):
            print(f"  - {m.name}")
        print(f"\nDefault: {sc.default_microphone().name}")
        print(f"Would use for physical classes: {pick_mic(sc, preferred).name}")
        input("\nPress Enter to close.")
        return

    if args.test:
        mic_test(pick_mic(sc, preferred), np, sample_rate)
        return

    now = datetime.now()
    cls = current_class(
        now, cfg.timetable,
        early_minutes=rc.get("early_minutes", 20),
        auto_stop_after_end=rc.get("auto_stop_after_end", 15),
        online_only=online_only,
    )
    if cls and not confirm_class(cls, cfg.courses[cls["code"]]):
        cls = pick_manually(cfg.courses, online_only, "Pick the class you're recording:")
    cls = cls or pick_manually(cfg.courses, online_only)

    folder = cfg.recordings_dir / cls["code"]
    folder.mkdir(parents=True, exist_ok=True)
    out = unique_path(folder, f"{cls['code']} {now:%Y-%m-%d} {cls['session']}")

    if cls["mode"] == "Online":
        speaker = sc.default_speaker()
        source = sc.get_microphone(id=str(speaker.name), include_loopback=True)
    else:
        source = pick_mic(sc, preferred)

    stop_at = cls["stop_at"]
    stop = threading.Event()

    print("=" * 56)
    print(f"  Recording : {cfg.courses[cls['code']]} ({cls['session']}, {cls['mode']})")
    print(f"  Source    : {source.name}")
    print(f"  File      : {out.name}")
    print(f"  Auto-stop : {stop_at:%H:%M}")
    print("  Press Enter (or Ctrl+C) to stop.")
    print("=" * 56)

    threading.Thread(target=wait_for_enter, args=(stop,), daemon=True).start()
    signal.signal(signal.SIGINT, lambda *_: stop.set())     # Ctrl+C saves and stops, like Enter

    started = time.time()
    began = datetime.now()                                # actual start, to the second
    # online classes can be quiet before they start, so give them longer
    window = rc.get("silent_window_online", 300) if cls["mode"] == "Online" else rc.get("silent_window", 30)
    levels, silent_secs, warned, checked = [], 0, False, False
    alert(f"Recording {cls['code']}", on=False)
    meter = LiveMeter()
    say = meter.say
    step = sample_rate // 16
    with sf.SoundFile(out, "w", samplerate=sample_rate, channels=1, format="FLAC") as f,          source.recorder(samplerate=sample_rate, channels=1) as rec:
        last_print = 0
        while not stop.is_set() and datetime.now() < stop_at:
            parts = []
            for _ in range(16):                                 # 16 x 1/16 s = 1 s (smooth redraw)
                part = np.asarray(rec.record(numframes=step), dtype="float32").reshape(-1)
                parts.append(part)
                meter.update(part, time.time() - started)
            chunk = np.concatenate(parts)
            f.write(chunk)
            levels.append(to_db(chunk))
            elapsed = time.time() - started

            if not checked and len(levels) >= 10:          # quick check after 10 s
                checked = True
                loud = sorted(levels)[-2]
                if loud < silent_db and cls["mode"] == "Online":
                    say(f"  Sound check: nothing playing yet (fine if the class hasn't started). Warns after {window // 60} min.")
                elif loud < silent_db:
                    say(f"  MIC CHECK: hearing almost nothing ({loud:.0f} dB). Is the mic plugged in / unmuted?")
                else:
                    say(f"  Mic check: OK, hearing sound ({loud:.0f} dB)")

            silent = is_silent(levels, window, silent_db)
            silent_secs += silent
            if silent and (not warned or elapsed - warned >= 60):
                dropouts = sum(lv <= -99 for lv in levels[-window:])
                say("\n  !!! SILENT INPUT: NOTHING IS BEING RECORDED !!!")
                if dropouts >= window // 3:
                    say(f"  !!! The input keeps cutting out ({dropouts} of the last {window}s were dead air).")
                    say("  !!! Likely a loose plug: push the mic jack in fully, don't move the cable.\n")
                else:
                    say(f"  !!! Level {levels[-1]:.0f} dB for {window}s. Check: mic plugged into the right jack,")
                    say("  !!! not muted (Settings > System > Sound > Input), right device. Then restart.\n")
                alert(f"!! MIC SILENT {cls['code']} !!", on=True, beep=beep)
                warned = elapsed
            elif warned and not silent and levels[-1] >= silent_db:
                say("  Sound is coming through again.")
                alert(f"Recording {cls['code']}", on=False)
                warned = False

            if elapsed - last_print >= 60:
                say(f"  ...{int(elapsed // 60)} min recorded  (level {max(levels[-60:]):.0f} dB)")
                last_print = elapsed
    say()

    mins = (time.time() - started) / 60
    meta = {
        "course": cfg.courses[cls["code"]],
        "code": cls["code"],
        "session": cls["session"],
        "mode": cls["mode"],
        "date": f"{now:%Y-%m-%d}",
        "started": f"{began:%H:%M:%S}",
        "ended": f"{datetime.now():%H:%M:%S}",
        "minutes": round(mins, 1),
        "seconds": round(mins * 60),
        "level_db": round(sorted(levels)[len(levels) // 2], 1) if levels else None,  # median
        "silent_minutes": round(silent_secs / 60, 1),
    }
    out.with_suffix(".json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(f"Saved {out.name} ({mins:.0f} min). The transcriber will pick it up once it syncs.")
    time.sleep(3)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        print(f"ERROR: {e}")
        input("Press Enter to close.")
        sys.exit(1)
