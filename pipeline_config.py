"""Shared config loader for the recorder and the transcriber.

Looks for the config in this order:
  1. the path in the LECTURE_PIPELINE_CONFIG environment variable
  2. config.json next to this file
"""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

DAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
SESSIONS = ("Lecture", "Tutorial", "Workshop")
MODES = ("Physical", "Online")


@dataclass(frozen=True)
class Slot:
    weekday: int      # 0 = Monday
    start: str        # "HH:MM"
    end: str          # "HH:MM"
    course: str       # course code, key into Config.courses
    session: str      # Lecture / Tutorial / Workshop
    mode: str         # Physical (mic) / Online (speaker loopback)


@dataclass
class Config:
    vault_dir: Path
    recordings_dir: Path
    lectures_dir: Path
    courses: dict
    timetable: list
    recorder: dict = field(default_factory=dict)
    transcriber: dict = field(default_factory=dict)


def config_path() -> Path:
    env = os.environ.get("LECTURE_PIPELINE_CONFIG")
    return Path(env) if env else Path(__file__).with_name("config.json")


def _slot(raw: dict, courses: dict) -> Slot:
    day = str(raw["day"]).strip().lower()[:3]
    if day not in DAYS:
        raise ValueError(f"bad day {raw['day']!r} (use Mon..Sun)")
    if raw["course"] not in courses:
        raise ValueError(f"timetable course {raw['course']!r} is not in 'courses'")
    if raw["session"] not in SESSIONS:
        raise ValueError(f"bad session {raw['session']!r} (use one of {SESSIONS})")
    if raw["mode"] not in MODES:
        raise ValueError(f"bad mode {raw['mode']!r} (use one of {MODES})")
    return Slot(DAYS[day], raw["start"], raw["end"], raw["course"], raw["session"], raw["mode"])


def load(path: Path | None = None) -> Config:
    path = path or config_path()
    if not path.exists():
        raise SystemExit(
            f"Config not found: {path}\n"
            "Copy config.example.json to config.json and edit it."
        )
    raw = json.loads(path.read_text(encoding="utf-8"))
    vault = Path(raw["vault_dir"])
    courses = raw["courses"]
    return Config(
        vault_dir=vault,
        recordings_dir=vault / raw.get("recordings_subdir", "_Recordings"),
        lectures_dir=vault / raw.get("lectures_subdir", "Lectures"),
        courses=courses,
        timetable=[_slot(s, courses) for s in raw.get("timetable", [])],
        recorder=raw.get("recorder", {}),
        transcriber=raw.get("transcriber", {}),
    )
