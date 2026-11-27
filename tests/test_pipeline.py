import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pipeline_config  # noqa: E402
import vocab  # noqa: E402
from lecture_transcriber import (NO_META_WAIT, collapse_repeats, find_ready_files,  # noqa: E402
                                 group_paragraphs, render_markdown)
from record_class import current_class, is_silent, to_db  # noqa: E402

CFG = pipeline_config.load(ROOT / "config.example.json")
MON = datetime(2026, 11, 23)  # a Monday


def t(hh, mm, day=MON):
    return day.replace(hour=hh, minute=mm)


def test_example_config_loads():
    assert CFG.recordings_dir.name == "_Recordings"
    assert CFG.lectures_dir.name == "Lectures"
    assert all(s.course in CFG.courses for s in CFG.timetable)


def test_class_in_progress():
    c = current_class(t(9, 30), CFG.timetable)
    assert (c["code"], c["session"], c["mode"]) == ("FIN201", "Lecture", "Online")


def test_early_start_counts():
    c = current_class(t(8, 45), CFG.timetable, early_minutes=20)
    assert c["session"] == "Lecture"
    assert current_class(t(8, 30), CFG.timetable, early_minutes=20) is None


def test_back_to_back_stops_at_next_start():
    c = current_class(t(10, 50), CFG.timetable)
    assert c["session"] == "Lecture"
    assert c["stop_at"] == t(11, 0)


def test_started_class_wins_over_upcoming():
    # 10:55: the 9-11 lecture is on, the 11:00 tutorial is inside its early window
    assert current_class(t(10, 55), CFG.timetable)["session"] == "Lecture"


def test_auto_stop_after_last_class():
    c = current_class(t(11, 30), CFG.timetable, auto_stop_after_end=15)
    assert c["session"] == "Tutorial"
    assert c["stop_at"] == t(12, 15)


def test_online_only_skips_physical():
    assert current_class(t(11, 30), CFG.timetable, online_only=True) is None
    # and a physical class no longer cuts the online one short
    c = current_class(t(10, 50), CFG.timetable, auto_stop_after_end=15, online_only=True)
    assert c["stop_at"] == t(11, 15)


def test_bad_timetable_rejected(tmp_path):
    raw = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
    raw["timetable"][0]["course"] = "NOPE"
    p = tmp_path / "config.json"
    p.write_text(json.dumps(raw), encoding="utf-8")
    try:
        pipeline_config.load(p)
    except ValueError as e:
        assert "NOPE" in str(e)
    else:
        raise AssertionError("expected ValueError")


def test_ready_files_wait_for_stable_size(tmp_path):
    (tmp_path / "FIN201").mkdir()
    audio = tmp_path / "FIN201" / "FIN201 2026-11-23 Lecture.flac"
    audio.write_bytes(b"x" * 10)
    (tmp_path / ".tmp.drivedownload").mkdir()
    (tmp_path / ".tmp.drivedownload" / "partial.flac").write_bytes(b"x")
    sizes = {}
    assert find_ready_files(tmp_path, set(), sizes) == []          # first sighting
    assert find_ready_files(tmp_path, set(), sizes) == []          # stable, but no .json: may be a sync pause
    later = time.time() + NO_META_WAIT + 1
    assert find_ready_files(tmp_path, set(), sizes, now=later) == [audio]   # unchanged long enough
    audio.write_bytes(b"x" * 20)
    assert find_ready_files(tmp_path, set(), sizes, now=later) == []        # still growing
    done = {"FIN201/FIN201 2026-11-23 Lecture.flac"}
    assert find_ready_files(tmp_path, done, sizes, now=later) == []         # already transcribed


def test_ready_as_soon_as_recorder_json_arrives(tmp_path):
    audio = tmp_path / "FIN201 2026-11-23 Lecture.flac"
    audio.write_bytes(b"x" * 10)
    sizes = {}
    find_ready_files(tmp_path, set(), sizes)
    assert find_ready_files(tmp_path, set(), sizes) == []
    audio.with_suffix(".json").write_text("{}", encoding="utf-8")
    assert find_ready_files(tmp_path, set(), sizes) == [audio]
    # json newer than the audio by minutes: the final audio has not synced yet
    old = time.time() - 600
    os.utime(audio, (old, old))
    assert find_ready_files(tmp_path, set(), sizes) == []


def test_corrections_note(tmp_path):
    note = tmp_path / "Transcript corrections.md"
    note.write_text("# Notes\n## All\n- then the gara -> Bank Negara\n%% - ignored -> x\n"
                    "## FIN201\n- CPF -> EPF\n## ECO101\n- GDP -> XXX\n", encoding="utf-8")
    rules = vocab.load_corrections(note, "FIN201")
    assert len(rules) == 2
    out = vocab.apply_corrections("Then the gara raised the OPR. Withdraw from CPF, not CPFIS.", rules)
    assert out == "Bank Negara raised the OPR. Withdraw from EPF, not CPFIS."
    assert vocab.load_corrections(tmp_path / "missing.md", "FIN201") == []


def test_slide_terms_from_text():
    text = ("Weighted Average Cost of Capital (WACC)\nWACC formula\nTHE END\n"
            "Capital Asset Pricing Model\nCAPM and beta\nCapital Asset Pricing Model recap\nCAPM")
    terms = vocab.extract_terms(text)
    assert terms[:2] == ["WACC", "CAPM"]
    assert "Capital Asset Pricing Model" in terms
    assert "THE" not in terms and "END" not in terms


def test_paragraphs_and_markdown():
    segs = [SimpleNamespace(start=i * 20, end=i * 20 + 20, text=f" s{i}") for i in range(5)]
    paras = group_paragraphs(segs, 60)
    assert paras == [("0:00:00", "s0 s1 s2"), ("0:01:00", "s3 s4")]
    md = render_markdown("FIN201 2026-11-23 Lecture",
                         {"course": "FIN201 Corporate Finance", "session": "Lecture",
                          "mode": "Online", "date": "2026-11-23"}, "0:01:40", paras)
    assert md.startswith("---\ncourse: \"FIN201 Corporate Finance\"")
    assert "**[0:01:00]** s3 s4" in md


def test_collapse_repeats():
    assert collapse_repeats("the education, the education, the education, the end") == "the education, the end"
    assert collapse_repeats("the 5-5-5-5-5 answer") == "the 5 answer"
    assert collapse_repeats("a 50-50 split") == "a 50-50 split"
    assert collapse_repeats("An excellent cost. An excellent cost. An excellent cost.") == "An excellent cost."
    # said twice is kept: lecturers repeat themselves on purpose
    twice = "Will it be taxed? Will it be taxed? No."
    assert collapse_repeats(twice) == twice


def test_noise_segments_dropped():
    segs = [SimpleNamespace(start=0, end=5, text=" . . . ."),
            SimpleNamespace(start=5, end=10, text=" Hello class.")]
    assert group_paragraphs(segs, 60) == [("0:00:05", "Hello class.")]


def test_silence_alarm():
    import numpy as np
    assert to_db(np.zeros(16000)) == -100.0
    assert round(to_db(np.full(16000, 0.1))) == -20
    speech = [-35.0] * 30
    dead = [-66.0] * 30                        # a wrong/muted input, as on 27 Nov
    assert not is_silent(dead[:29], 30)        # not a full window yet
    assert is_silent(dead, 30)
    assert not is_silent(speech, 30)
    assert is_silent(dead[:27] + [-20.0] * 3, 30)       # a few bumps don't hide a dead mic
    assert not is_silent(dead[:20] + speech[:10], 30)   # someone talking for 10 s is enough


def test_live_meter_wave(capsys):
    from record_class import LiveMeter
    m = LiveMeter(width=5)
    m.chars = " .:-=+*#@"
    for db in (-100, -70, -45, -20, 0):
        m.update(db, 75)
    line = capsys.readouterr().out.split("\r")[-1]
    assert "REC 01:15" in line and "    =@@  " in line and "OK" in line
    m2 = LiveMeter(width=3)
    m2.update(-90, 0)
    assert "SILENT" in capsys.readouterr().out
