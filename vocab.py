"""Vocabulary helpers for the transcriber.

- Corrections: a Markdown note in the vault with `- wrong -> right` lines,
  grouped under `## All` or `## <COURSE CODE>`. Applied to every new transcript.
- Slide terms: key terms pulled from the PDFs / PowerPoints in
  `Slides/<COURSE CODE>/`, fed to Whisper as hotwords so it spells them right.
"""

import json
import re
from collections import Counter
from pathlib import Path

# ---------------- corrections ----------------
_RULE = re.compile(r"^[-*]\s*(.+?)\s*->\s*(.+?)\s*$")


def load_corrections(note: Path, code: str):
    """Rules for this course: [(compiled pattern, replacement)]."""
    if not note.exists():
        return []
    rules, section = [], None
    for line in note.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s.startswith("## "):
            section = s[3:].strip().lower()
            continue
        if section not in ("all", (code or "").lower()) or s.startswith("%%"):
            continue
        m = _RULE.match(s)
        if m:
            pattern = re.compile(r"(?<!\w)" + re.escape(m[1]) + r"(?!\w)", re.IGNORECASE)
            rules.append((pattern, m[2]))
    return rules


def apply_corrections(text: str, rules) -> str:
    for pattern, right in rules:
        text = pattern.sub(lambda _m, r=right: r, text)
    return text


# ---------------- slide terms ----------------
SLIDE_EXTS = {".pdf", ".pptx"}

# All-caps words on slides that are headings, not terms.
_CAPS_STOP = {
    "I", "A", "AN", "THE", "AND", "OR", "OF", "TO", "IN", "ON", "FOR", "BY", "AT", "IS",
    "ARE", "BE", "IT", "AS", "IF", "NO", "NOT", "VS", "EG", "IE", "ETC", "PM", "AM",
    "TABLE", "NOTE", "NOTES", "PAGE", "WEEK", "TOPIC", "UNIT", "PART", "CHAPTER",
    "SUMMARY", "EXAMPLE", "QUIZ", "TEST", "EXAM", "END", "Q", "QA", "Q&A",
}
# Title-case phrases that are slide furniture.
_PHRASE_STOP_START = {"The", "A", "An", "This", "These", "That", "What", "How", "Why",
                      "Learning", "Chapter", "Topic", "Week", "Table", "Figure", "Source",
                      "Lecture", "Tutorial", "Workshop", "Example", "Question", "Answer"}

_ACRONYM = re.compile(r"\b[A-Z][A-Z0-9&]{1,5}s?\b")
# [ \t] not \s: a phrase must not run across lines (slide titles are separate lines)
_PHRASE = re.compile(r"\b[A-Z][a-z]+(?:[ \t]+(?:of|and|&|to)[ \t]+[A-Z][a-z]+|[ \t]+[A-Z][a-z]+){1,3}\b")


def _read_pdf(path: Path) -> str:
    from pypdf import PdfReader
    return "\n".join((p.extract_text() or "") for p in PdfReader(str(path)).pages)


def _read_pptx(path: Path) -> str:
    from pptx import Presentation
    out = []
    for slide in Presentation(str(path)).slides:
        for shape in slide.shapes:
            if shape.has_text_frame:
                out.append(shape.text_frame.text)
    return "\n".join(out)


def extract_terms(text: str, limit: int = 30):
    """Most frequent acronyms and Title Case phrases (seen at least twice)."""
    counts = Counter()
    for m in _ACRONYM.findall(text):
        if m.rstrip("s") not in _CAPS_STOP and m not in _CAPS_STOP:
            counts[m] += 1
    for m in _PHRASE.findall(text):
        phrase = " ".join(m.split())
        if phrase.split()[0] not in _PHRASE_STOP_START:
            counts[phrase] += 1
    return [t for t, n in counts.most_common() if n >= 2][:limit]


def slide_terms(folder: Path, limit: int = 30, log=print):
    """Terms from every slide file in folder, cached in folder/.terms.json."""
    if not folder.is_dir():
        return []
    files = sorted(f for f in folder.iterdir() if f.is_file() and f.suffix.lower() in SLIDE_EXTS)
    if not files:
        return []
    sig = [[f.name, f.stat().st_size, int(f.stat().st_mtime)] for f in files]
    cache = folder / ".terms.json"
    try:
        cached = json.loads(cache.read_text(encoding="utf-8"))
        if cached.get("sig") == sig and cached.get("limit") == limit:
            return cached["terms"]
    except Exception:
        pass

    text = []
    for f in files:
        try:
            text.append(_read_pdf(f) if f.suffix.lower() == ".pdf" else _read_pptx(f))
        except Exception as e:
            log(f"    (could not read slides {f.name}: {e})")
    terms = extract_terms("\n".join(text), limit)
    try:
        cache.write_text(json.dumps({"sig": sig, "limit": limit, "terms": terms}, indent=1), encoding="utf-8")
    except Exception:
        pass
    return terms
