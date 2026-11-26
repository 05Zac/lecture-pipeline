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
    ap.add_argument("--mics", action="store_true", help="list microphones and show which one would be used")
    args = ap.parse_args()

    cfg = pipeline_config.load()
    rc = cfg.recorder
    online_only = args.online_only or rc.get("online_only", False)
    sample_rate = rc.get("sample_rate", 16000)
    preferred = rc.get("preferred_mics", ["external", "headset", "usb"])

    import numpy as np
    import soundcard as sc
    import soundfile as sf

    if args.mics:
        print("Microphones on this computer:")
        for m in sc.all_microphones(include_loopback=False):
            print(f"  - {m.name}")
        print(f"\nDefault: {sc.default_microphone().name}")
        print(f"Would use for physical classes: {pick_mic(sc, preferred).name}")
        input("\nPress Enter to close.")
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
    with sf.SoundFile(out, "w", samplerate=sample_rate, channels=1, format="FLAC") as f, \
         source.recorder(samplerate=sample_rate, channels=1) as rec:
        last_print = 0
        while not stop.is_set() and datetime.now() < stop_at:
            data = rec.record(numframes=sample_rate)          # 1 second chunks
            f.write(np.asarray(data, dtype="float32").reshape(-1))
            elapsed = time.time() - started
            if elapsed - last_print >= 60:
                print(f"  ...{int(elapsed // 60)} min recorded", flush=True)
                last_print = elapsed

    mins = (time.time() - started) / 60
    meta = {
        "course": cfg.courses[cls["code"]],
        "code": cls["code"],
        "session": cls["session"],
        "mode": cls["mode"],
        "date": f"{now:%Y-%m-%d}",
        "started": f"{now:%H:%M}",
        "minutes": round(mins, 1),
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
