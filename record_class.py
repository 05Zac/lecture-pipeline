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


def pick_manually(courses, online_only=False):
    print("No class on right now. Pick one:")
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


WAVE_CHARS = " ▁▂▃▄▅▆▇█"
WAVE_LOW, WAVE_HIGH = -70.0, -20.0     # dB range drawn by the wave (quiet room ... loud voice)


class LiveMeter:
    """One self-updating console line: a scrolling wave of the input level.

        REC 12:34  ▁▂▅▇▆▃▁▁▂▆█▇▅▂▁▁▃▅▆▄  -32 dB  OK

    Bars moving when someone talks = the mic works. Flat line = it doesn't.
    """

    def __init__(self, width=40, label="REC"):
        self.width, self.label, self.dbs, self.shown = width, label, [], False
        self.color = sys.stdout.isatty()
        try:
            WAVE_CHARS.encode(sys.stdout.encoding or "ascii")
            self.chars = WAVE_CHARS
        except (UnicodeEncodeError, LookupError):
            self.chars = " .:-=+*#@"           # old consoles / redirected output
        if self.color and sys.platform == "win32":
            os.system("")                      # turns on ANSI colours in the Windows console

    def bar(self, db):
        frac = (db - WAVE_LOW) / (WAVE_HIGH - WAVE_LOW)
        n = len(self.chars) - 1
        return self.chars[max(0, min(n, round(frac * n)))]

    def update(self, db, elapsed):
        self.dbs = (self.dbs + [db])[-self.width:]
        wave = "".join(self.bar(d) for d in self.dbs).ljust(self.width)
        loud = max(self.dbs[-8:])               # last ~2 s
        status, col = ("OK", "32") if loud >= -50 else ("quiet", "33") if loud >= SILENT_DB else ("SILENT", "31")
        if self.color:
            wave, status = f"\033[{col}m{wave}\033[0m", f"\033[{col}m{status}\033[0m"
        m, s = divmod(int(elapsed), 60)
        sys.stdout.write(f"\r  {self.label} {m:02d}:{s:02d}  {wave}  {loud:4.0f} dB  {status}   ")
        sys.stdout.flush()
        self.shown = True

    def say(self, text=""):
        """Print a normal message above the meter line."""
        if self.shown:
            sys.stdout.write("\r" + " " * (self.width + 40) + "\r")
            self.shown = False
        print(text, flush=True)


def mic_test(source, np, sample_rate=16000):
    """Show the live wave without saving anything, until Enter."""
    stop = threading.Event()
    threading.Thread(target=lambda: (input(), stop.set()), daemon=True).start()
    meter, started = LiveMeter(label="TEST"), time.time()
    print(f"Mic test: {source.name}\nTalk or tap the mic: the wave should jump. Press Enter to finish.\n")
    with source.recorder(samplerate=sample_rate, channels=1) as rec:
        while not stop.is_set():
            chunk = np.asarray(rec.record(numframes=sample_rate // 4), dtype="float32").reshape(-1)
            meter.update(to_db(chunk), time.time() - started)
    meter.say()


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
    ) or pick_manually(cfg.courses, online_only)

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
    print("  Press Enter to stop.")
    print("=" * 56)

    def wait_enter():
        try:
            input()
        except EOFError:
            pass
        stop.set()

    threading.Thread(target=wait_enter, daemon=True).start()

    started = time.time()
    # online classes can be quiet before they start, so give them longer
    window = rc.get("silent_window_online", 300) if cls["mode"] == "Online" else rc.get("silent_window", 30)
    levels, silent_secs, warned, checked = [], 0, False, False
    alert(f"Recording {cls['code']}", on=False)
    meter = LiveMeter()
    say = meter.say
    quarter = sample_rate // 4
    with sf.SoundFile(out, "w", samplerate=sample_rate, channels=1, format="FLAC") as f,          source.recorder(samplerate=sample_rate, channels=1) as rec:
        last_print = 0
        while not stop.is_set() and datetime.now() < stop_at:
            parts = []
            for _ in range(4):                                  # 4 x 0.25 s = 1 s
                part = np.asarray(rec.record(numframes=quarter), dtype="float32").reshape(-1)
                parts.append(part)
                meter.update(to_db(part), time.time() - started)
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
        "started": f"{now:%H:%M}",
        "minutes": round(mins, 1),
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
