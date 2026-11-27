# Lecture Pipeline

Record your classes on a laptop, transcribe them for free on your desktop's GPU, and get timestamped Markdown notes filed by course in an Obsidian vault.

```
 Laptop                     Google Drive (or any sync)          Desktop with NVIDIA GPU
 ┌──────────────────┐       ┌─────────────────────┐            ┌────────────────────────┐
 │ record_class.py  │──────▶│ vault/_Recordings/  │───────────▶│ lecture_transcriber.py │
 │ timetable aware, │       │   FIN201/*.flac     │            │ faster-whisper         │
 │ mic or loopback  │       │   FIN201/*.json     │            │ large-v3 on CUDA       │
 └──────────────────┘       └─────────────────────┘            └───────────┬────────────┘
                                                                           │
                            ┌─────────────────────────────────┐            │
                            │ vault/Lectures/<Course>/*.md    │◀───────────┘
                            │ YAML frontmatter + timestamps   │
                            └─────────────────────────────────┘
```

## What it does

**Recorder (`record_class.py`)**
- Reads your timetable and picks the class that is on now, from 20 minutes before it starts.
- Names the file `CODE YYYY-MM-DD Session.flac` and puts it in a folder per course.
- Physical classes record the microphone. Online classes record speaker output (WASAPI loopback), so you capture Teams or Zoom audio without a virtual cable.
- Records 16 kHz mono FLAC, about 60 MB per hour.
- Stops when you press Enter, 15 minutes after the class ends, or when the next back-to-back class starts. Each class gets its own file.
- Writes a `.json` sidecar with the course, session, mode, date and length.
- `--online-only` mode is for a desktop that only joins online classes.
- Shows a live wave of the input level while it records (`--test` or `Test mic.bat` shows it without recording), so you can see the mic is working.
- Warns you if nothing is being recorded. Ten seconds in it says whether it can hear sound. If a physical class stays near silent for 30 seconds (5 minutes for online), it prints a warning, changes the window title and flashes the taskbar button, and it says whether the plug looks loose. The per-minute progress line shows the level: speech in a classroom is about -45 to -25 dB, and a dead input is -65 or lower.

**Transcriber (`lecture_transcriber.py`)**
- Polls the recordings folder and waits until the recorder's `.json` has arrived and the audio has finished syncing. A sync tool uploads a recording that is still growing in bursts, so a stable size alone isn't proof the recording is done. Audio files you add by hand, with no `.json`, are picked up once they've been unchanged for 15 minutes.
- Transcribes with [faster-whisper](https://github.com/SYSTRAN/faster-whisper) `large-v3` at float16. It fits in 8 GB of VRAM, and a 2-hour lecture takes about 5 to 10 minutes on an RTX 4060.
- Uses VAD to skip silence, and a vocabulary hint so course terms like WACC or heteroskedasticity come out right.
- Writes one Markdown note per recording, with ~60 s timestamped paragraphs and frontmatter you can query with Dataview.
- Remembers finished files in `_Recordings/.transcribed.json`, so restarting is safe.

Example output:

```markdown
---
course: "FIN201 Corporate Finance"
session: Lecture
mode: Online
date: 2026-11-24
duration: "1:52:10"
status: transcribed
tags: [transcript]
---

# FIN201 2026-11-24 Lecture

**[0:00:04]** Okay, good afternoon everyone. Today we're moving on to the cost of capital...

**[0:01:06]** So the WACC is the weighted average of the cost of equity and the after-tax cost of debt...
```

## Requirements

- Windows 10 or 11 for the recorder. The loopback capture uses `soundcard`, which also works on Linux with PulseAudio.
- An NVIDIA GPU for the transcriber. For CPU only, set `"device": "cpu"`, `"compute_type": "int8"` and a smaller model such as `"small"`. It will be much slower.
- Python 3.10 or newer.
- A folder synced between both machines. Google Drive for desktop in **Mirror files** mode works well. Syncthing or OneDrive also work. One machine doing both jobs is fine too.

## Setup

1. Clone the repo on both machines and copy the config:
   ```
   git clone https://github.com/05Zac/lecture-pipeline.git
   cd lecture-pipeline
   copy config.example.json config.json
   ```
2. Edit `config.json`:
   - `vault_dir`: your Obsidian vault inside the synced folder. Use the same vault on both machines; the local path can differ.
   - `courses` and `timetable`: your classes. `day` is `Mon` to `Sun`. `mode` is `Physical` (microphone) or `Online` (speaker output).
   - `transcriber.vocab_hint`: terms from your courses.
3. Install:
   - Laptop (recorder): `pip install -r requirements-recorder.txt`
   - Desktop (transcriber): `pip install --only-binary av -r requirements-transcriber.txt`. The `--only-binary av` part stops pip from trying to compile PyAV.
4. In Obsidian, go to **Settings > Files and links > Excluded files** and add `_Recordings/`, so audio files stay out of search.

## Usage

**Recording:** double-click `Record Class.bat`. A desktop shortcut to it is handy, and if you don't want a console window, set the shortcut to run minimized. On a desktop used only for online classes, use `Record Class (online only).bat`.

**Transcribing:** test it once in a console:

```
python lecture_transcriber.py --once
```

To run it in the background at every logon, open PowerShell in the repo folder and run:

```powershell
$py = (Get-Command pythonw).Source
$action  = New-ScheduledTaskAction -Execute $py -Argument "`"$PWD\lecture_transcriber.py`"" -WorkingDirectory $PWD
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$trigger.Delay = "PT1M"
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit 0 -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName "Lecture Transcriber" -Action $action -Trigger $trigger -Settings $settings
```

Under `pythonw`, output goes to `transcriber.log` next to the script.

## Improving accuracy

**Fix words it keeps getting wrong.** Create `Transcript corrections.md` in the vault (copy [examples/Transcript corrections.md](examples/Transcript%20corrections.md)) and add lines like `- then the gara -> Bank Negara`. You can scope rules to every course with `## All` or to one course with `## FIN201`. Matching ignores case and only matches whole words. The note is plain Markdown, so you can edit it in Obsidian between classes.

**Teach it your course terms.** Drop lecture slides (PDF or PowerPoint) into `Slides/<CODE>/` in the vault. The transcriber picks out the most frequent acronyms and Title Case terms and feeds them to Whisper as hotwords, so it spells "Debt Service Ratio" or "WACC" correctly. Results are cached in `.terms.json`, and new slides are picked up automatically.

**Use a better mic.** This beats any setting. A clip-on or small directional USB mic pointed at the lecturer cuts out classmates' chatter. The recorder uses the first mic whose name contains an entry in `recorder.preferred_mics`, and falls back to the Windows default. Run `python record_class.py --mics` to see the names.

**Built in:** each 30 s chunk is transcribed on its own, so one bad chunk can't loop into the next. Made-up text over long silences is dropped. A phrase repeated 3 or more times in a row is collapsed to one, while a phrase said twice is kept, because lecturers repeat themselves on purpose.

## Studying with the transcripts

The notes are plain Markdown, so any LLM can read them. One prompt that works well: give it a transcript, have it ask what you already know first, then explain the gaps, point out anything the lecturer hinted will be in the exam, and finish with a short quiz.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `open() got an unexpected keyword argument 'metadata_errors'` | PyAV is too new for faster-whisper 1.2.x: `pip install --only-binary :all: "av<15"` (pinned in requirements) |
| `Could not load library cudnn_ops64_9.dll` or `cublas64_12.dll` | Install `nvidia-cublas-cu12` and `nvidia-cudnn-cu12==9.*`. The script adds their DLL folders to the path. |
| CUDA out of memory | Set `"compute_type": "int8_float16"` |
| Online class records silence | Audio must play through the **default** output device while you record |
| Duplicate folders such as `FIN201 (1)` in Drive | Both machines created the folder before the first sync. Merge the files into one folder and delete the other. |

## Privacy

Get permission before recording a class, and check your university's policy. Keep recordings and transcripts private. `.gitignore` excludes `config.json`, audio and notes so they don't end up in a fork by accident.

## License

MIT
