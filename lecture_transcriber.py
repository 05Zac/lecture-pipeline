"""
Lecture Transcriber
-------------------
Watches the synced recordings folder. When a new recording has finished
syncing (its size stops changing), it transcribes it on the GPU with
faster-whisper and saves a Markdown note into your Obsidian vault:

    <vault>/Lectures/FIN201 Corporate Finance/FIN201 2026-11-27 Tutorial.md

Run in a console:   python lecture_transcriber.py
Run in background:  pythonw lecture_transcriber.py   (logs to transcriber.log)
Run once and exit:  python lecture_transcriber.py --once
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import pipeline_config
import vocab

AUDIO_EXTS = {".flac", ".m4a", ".mp3", ".wav", ".aac", ".ogg", ".wma", ".webm", ".mp4"}


# ---------------- helpers ----------------
def add_cuda_dlls():
    """Make the pip-installed NVIDIA DLLs (cuBLAS, cuDNN) visible on Windows."""
    if os.name != "nt":
        return
    import site
    for sp in site.getsitepackages() + [site.getusersitepackages()]:
        for sub in ("nvidia/cublas/bin", "nvidia/cudnn/bin"):
            d = Path(sp) / sub
            if d.is_dir():
                os.add_dll_directory(str(d))
                os.environ["PATH"] = str(d) + os.pathsep + os.environ["PATH"]


_REPEAT = re.compile(r"\b((?:\w+[\s,]+){0,5}?\w+)(?:[\s,.!?-]+\1\b){2,}", re.IGNORECASE)


def collapse_repeats(text):
    """'the education, the education, the education' -> 'the education' (3+ in a row).

    Whisper sometimes gets stuck repeating a phrase on unclear audio. A phrase
    said twice is left alone, since lecturers often repeat themselves once.
    """
    prev = None
    while prev != text:
        prev, text = text, _REPEAT.sub(r"\1", text)
    return text


def fmt_ts(seconds):
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}"


class State:
    """Remembers which recordings are already transcribed."""

    def __init__(self, path: Path):
        self.path = path
        self.done = set()
        if path.exists():
            try:
                self.done = set(json.loads(path.read_text(encoding="utf-8")))
            except Exception:
                pass

    def add(self, key):
        self.done.add(key)
        self.path.write_text(json.dumps(sorted(self.done), indent=2), encoding="utf-8")


def read_meta(audio: Path, cfg) -> dict:
    """Details from the recorder's .json sidecar; falls back to folder/file name."""
    meta_file = audio.with_suffix(".json")
    if meta_file.exists():
        try:
            return json.loads(meta_file.read_text(encoding="utf-8"))
        except Exception:
            pass
    code = audio.parent.name if audio.parent != cfg.recordings_dir else ""
    session = next((s for s in pipeline_config.SESSIONS if s in audio.stem), None)
    return {
        "code": code,
        "course": cfg.courses.get(code),
        "session": session,
        "mode": None,
        "date": f"{datetime.fromtimestamp(audio.stat().st_mtime):%Y-%m-%d}",
    }


NO_META_WAIT = 15 * 60   # files without a recorder .json (e.g. dropped in by hand): wait this long unchanged


def is_finished(f: Path, now: float) -> bool:
    """A recording is finished when the recorder's .json exists and the audio synced after it.

    Sync tools upload a growing recording in bursts, so a stable size alone is
    not enough: a pause between bursts looks finished. The recorder writes the
    .json only after it closes the audio, and sync tools keep the original
    modified times, so audio mtime >= json mtime (minus slack) means the final
    version of the audio has arrived.
    """
    st = f.stat()
    meta = f.with_suffix(".json")
    if meta.exists():
        return st.st_mtime >= meta.stat().st_mtime - 60
    return now - st.st_mtime >= NO_META_WAIT


def find_ready_files(root: Path, done: set, sizes: dict, now: float | None = None):
    """Audio files that are new, finished recording, and whose size has not changed since the last poll."""
    ready, now = [], now if now is not None else time.time()
    for f in root.rglob("*"):
        if not f.is_file() or f.suffix.lower() not in AUDIO_EXTS:
            continue
        if any(p.startswith(".") for p in f.relative_to(root).parts):
            continue  # sync-tool temp files and hidden folders
        key = f.relative_to(root).as_posix()
        if key in done:
            continue
        size = f.stat().st_size
        if size > 0 and sizes.get(key) == size and is_finished(f, now):
            ready.append(f)
        sizes[key] = size
    return ready


def group_paragraphs(segments, paragraph_seconds, rules=()):
    """Join whisper segments into (timestamp, text) paragraphs of ~paragraph_seconds.

    Drops noise-only segments, collapses stuck repeats and applies the
    vault's correction rules.
    """
    paragraphs, current, para_start = [], [], None
    for seg in segments:
        text = seg.text.strip()
        if not re.search(r"\w", text):
            continue  # ". . . ." during videos or noise
        if getattr(seg, "no_speech_prob", 0) > 0.6 and getattr(seg, "avg_logprob", 0) < -1.0:
            continue  # probably not speech
        if para_start is None:
            para_start = seg.start
        current.append(vocab.apply_corrections(collapse_repeats(text), rules))
        if seg.end - para_start >= paragraph_seconds:
            paragraphs.append((fmt_ts(para_start), " ".join(current)))
            current, para_start = [], None
    if current:
        paragraphs.append((fmt_ts(para_start), " ".join(current)))
    return paragraphs


def render_markdown(title, meta, duration, paragraphs):
    course = meta.get("course") or "Unsorted"
    lines = [
        "---",
        f"course: \"{course}\"",
        f"session: {meta.get('session') or ''}",
        f"mode: {meta.get('mode') or ''}",
        f"date: {meta['date']}",
        f"duration: \"{duration}\"",
        "status: transcribed",
        "tags: [transcript]",
        "---",
        "",
        f"# {title}",
        "",
    ]
    lines += [f"**[{ts}]** {text}\n" for ts, text in paragraphs]
    return "\n".join(lines)


# ---------------- transcription ----------------
def transcribe(model, audio: Path, cfg):
    tc = cfg.transcriber
    print(f"[{datetime.now():%H:%M}] Transcribing {audio.name} ...", flush=True)
    t0 = time.time()
    meta = read_meta(audio, cfg)
    code = meta.get("code") or ""
    slides = cfg.vault_dir / tc.get("slides_subdir", "Slides") / code
    terms = vocab.slide_terms(slides, tc.get("slide_terms", 30)) if code else []
    if terms:
        print(f"    {len(terms)} slide terms: {', '.join(terms[:12])}{' ...' if len(terms) > 12 else ''}", flush=True)
    rules = vocab.load_corrections(cfg.vault_dir / tc.get("corrections_note", "Transcript corrections.md"), code)
    hotwords = ", ".join(w for w in [tc.get("hotwords")] + terms if w) or None

    segments, info = model.transcribe(
        str(audio),
        language=tc.get("language", "en"),
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 700},
        initial_prompt=tc.get("vocab_hint") or None,
        hotwords=hotwords,                    # reaches every window; vocab_hint only the first
        condition_on_previous_text=False,     # one bad window can't loop into the next
        repetition_penalty=1.05,
        word_timestamps=True,                 # needed for hallucination_silence_threshold
        hallucination_silence_threshold=2.0,  # drop made-up text over long silences/noise
        beam_size=5,
    )
    paragraphs = group_paragraphs(segments, tc.get("paragraph_seconds", 60), rules)

    out_dir = cfg.lectures_dir / (meta.get("course") or "Unsorted")
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{audio.stem}.md"
    out.write_text(render_markdown(audio.stem, meta, fmt_ts(info.duration), paragraphs), encoding="utf-8")
    print(f"    Done in {(time.time() - t0) / 60:.1f} min -> {out}", flush=True)


def main():
    ap = argparse.ArgumentParser(description="Transcribe new lecture recordings into an Obsidian vault.")
    ap.add_argument("--once", action="store_true", help="process what is ready now, then exit")
    args = ap.parse_args()

    # Under pythonw (Task Scheduler, no console) there is no stdout: log to a file instead.
    if sys.stdout is None:
        log = open(Path(__file__).with_name("transcriber.log"), "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = log

    cfg = pipeline_config.load()
    if not cfg.vault_dir.is_dir():
        sys.exit(f"Vault folder not found: {cfg.vault_dir}")
    cfg.recordings_dir.mkdir(parents=True, exist_ok=True)

    tc = cfg.transcriber
    add_cuda_dlls()
    from faster_whisper import WhisperModel

    model_name = tc.get("model", "large-v3")
    device = tc.get("device", "cuda")
    print(f"Loading {model_name} on {device} (first run downloads the model) ...", flush=True)
    model = WhisperModel(model_name, device=device, compute_type=tc.get("compute_type", "float16"))
    print(f"Watching {cfg.recordings_dir}  (Ctrl+C to stop)", flush=True)

    state = State(cfg.recordings_dir / ".transcribed.json")
    sizes = {}
    poll = tc.get("poll_seconds", 30)
    first = True
    while True:
        ready = find_ready_files(cfg.recordings_dir, state.done, sizes)
        for audio in ready:
            key = audio.relative_to(cfg.recordings_dir).as_posix()
            try:
                transcribe(model, audio, cfg)
                state.add(key)
            except Exception as e:
                print(f"    FAILED on {audio.name}: {e}  (will retry next round)", flush=True)
                sizes.pop(key, None)
        if args.once and not first:
            break
        first = False
        time.sleep(poll if not args.once else 5)


if __name__ == "__main__":
    main()
