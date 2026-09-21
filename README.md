# MOGOPS Video Convert

**Universal media conversion with an explicit size ceiling.**  
Probe first. Plan in the open. Write to a temporary file. Verify. Rename. Never delete the source.

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![ffmpeg](https://img.shields.io/badge/requires-ffmpeg%20%2B%20ffprobe-007808?logo=ffmpeg&logoColor=white)](https://ffmpeg.org/)
[![License](https://img.shields.io/badge/license-specify%20before%20publish-555)](#license)

Repository target: [`TaoishTechy/mogops-video-convert`](https://github.com/TaoishTechy/mogops-video-convert)

---

## Why this exists

Most converters ask for a codec and a quality number, then hope the file lands near a size you can live with. This project inverts that. When `$size` is set, the encoder solves:

```text
maximize usable quality
subject to  output_bytes ≤ size
```

Audio is reserved first so speech remains intelligible. Video receives the residual budget. If the first write still exceeds the cap, bitrate contracts by the golden-ratio step \( \varphi \) and the encode is retried within a bounded number of passes.

The name **MOGOPS** is a design contract taken from the companion framework documents (efficiency metric \( \Xi \), ledger fidelity, plasticity reserve). It is a planning metaphor mapped onto ffmpeg decisions. It is not a claim of new physics.

| Framework term | What the software actually does |
|---|---|
| Predictive power | `ffprobe` JSON ledger before any write |
| Falsifiability | Temp file → probe → size gate → atomic replace |
| Compression | Duration-aware bit budget; two-pass when `$size` is armed |
| Computational cost | Four threads by default; stream-copy when the container allows it |
| Ambiguity reduction | Explicit container → codec matrix; no silent incompatible copies |
| Plasticity reserve | First pass does not spend the entire budget |

---

## Contents

```text
mogops_video_convert.py       Engine, CLI, size parser, planner, refine loop
mogops_video_convert_gui.py   Desktop GUI (Tk) over the same engine
README.md                     This document
```

Place both Python files in the same directory. The GUI imports the engine as a sibling module.

---

## Requirements

- Python 3.10 or newer
- `ffmpeg` and `ffprobe` on `PATH`
- Tk (`python3-tk` on Debian/Ubuntu) for the GUI
- A display session for the GUI

Install ffmpeg on Ubuntu/Debian:

```bash
sudo apt-get update
sudo apt-get install -y ffmpeg python3-tk
```

Confirm the tools:

```bash
ffmpeg -version
ffprobe -version
python3 --version
```

No third-party Python packages are required.

---

## Quick start

```bash
git clone https://github.com/TaoishTechy/mogops-video-convert.git
cd mogops-video-convert
```

### Command line

```bash
# Container change or quality-first encode
python3 mogops_video_convert.py INPUT.mkv -o OUTPUT.mp4

# Hard size ceiling
python3 mogops_video_convert.py INPUT.mkv -o OUTPUT.mp4 --size 50MB

# Audio only
python3 mogops_video_convert.py INPUT.mkv -o speech.mp3 --audio-only
python3 mogops_video_convert.py INPUT.mkv --to wav --size 20MB

# Inspect without writing
python3 mogops_video_convert.py INPUT.mkv --probe-only
python3 mogops_video_convert.py INPUT.mkv -o out.webm --size 12MiB --dry-run
```

### Desktop GUI

```bash
python3 mogops_video_convert_gui.py
```

1. Browse or paste a local source path. The stream ledger appears after probe.
2. Choose a container chip (`mp4`, `mkv`, `webm`, `mp3`, `wav`, …).
3. Optionally edit **Output filename** (default: source stem). The chip still owns the extension.
4. Optionally arm **size ceiling** (`50` + `MB`, `MiB`, `KB`, `GB`).
5. Convert. The result pane reports bytes in, bytes out, mode, and a bounded `xi_proxy` scorecard.

Keyboard: **Enter** starts convert when focus is not in a text field; **Ctrl+Enter** always starts convert; **Esc** requests cancel; **Ctrl+O** opens the last verified output. Letter `o` is not stolen while you type a filename.

---

## The `$size` contract

`--size` accepts human values:

| Input | Meaning |
|---|---|
| `50MB` / `50M` | 50 × 10³ × 10³ bytes (SI) |
| `50MiB` | 50 × 1024² bytes (IEC) |
| `800KB` | 800 × 1000 bytes |
| `1.5G` | 1.5 × 10⁹ bytes |
| `5000000` | raw bytes |

Procedure:

1. Parse the cap.
2. Reserve 2–4% for container overhead.
3. Allocate audio bitrate first (capped, never below a floor).
4. Give the residual to video.
5. Two-pass constrained VBR when video is present.
6. If the verified file is still over cap, divide the active bitrate by \( \varphi \approx 1.618 \) and retry (maximum five refinements).
7. Fail closed if the cap cannot be met. The source file is untouched.

Without `--size`, the engine remuxes with `-c copy` when codecs fit the destination container; otherwise it uses a quality-first CRF encode.

---

## Formats

**Video containers:** `.mp4` `.m4v` `.mov` `.mkv` `.webm` `.avi` `.wmv` `.flv` `.ts` `.mts` `.m2ts` `.ogv` `.gif`

**Audio-only targets** (video dropped): `.mp3` `.wav` `.flac` `.aac` `.m4a` `.ogg` `.opus` `.wma` `.aiff` `.aif`

Defaults prefer compatibility (`libx264` + `aac` for MP4). Override with `--video-codec` / `--audio-codec` or the GUI advanced panel. The planner refuses stream-copy into a container that cannot hold the source codec (for example VP9 into MP4).

---

## Safety rules

These are enforced, not suggested:

- Refuse overwrite unless `--overwrite` or an explicit GUI confirmation is given.
- Write `.{stem}.tmp{suffix}`, probe it, then `os.replace` onto the destination.
- Accept local file paths only. User-supplied URLs are not passed to ffmpeg.
- Never delete or truncate the source.
- Default thread count is 4.
- GUI “Open output” launches the desktop handler in a **new session** with stdio detached, so VLC/mpv log noise and teardown warnings do not attach to the converter process.

---

## CLI reference

```text
python3 mogops_video_convert.py INPUT
    -o / --output PATH
    --to EXTENSION
    --size SIZE
    --audio-only
    --video-codec NAME
    --audio-codec NAME
    --crf N
    --preset NAME
    --threads N
    --overwrite
    --dry-run
    --probe-only
```

Success prints a JSON ledger: paths, mode, output bytes, duration, plan notes, `xi_proxy`.

`xi_proxy` is a bounded scorecard in \( [0, 0.999] \). It is not a physical measurement.

---

## GUI notes

Session memory is stored at `~/.mogops_video_convert_gui.json` (last output folder and last container). It does not store file contents.

If the window is started from a terminal, close it from the window chrome. **Ctrl+C** in the terminal now shuts the application down without a raw `KeyboardInterrupt` traceback.

Copy **both** `mogops_video_convert.py` and `mogops_video_convert_gui.py` into the same folder (for example `~/Desktop/Utilities/`). The GUI will not start if the engine module is missing from that directory.

---

## Project status

Executable specification. Suitable for local conversion, calibration, and review. Hardware encoders, distributed workers, and a published test corpus are extension points — they are omitted here rather than pretended.

Companion documents that supplied the planning vocabulary (not runtime dependencies):

- MOGOPS v5.0 Complete Framework
- MOGOPS–THO Unified Framework v2.7
- Meta-Hyper Axiomatic Framework (MHAF v2.0)

Those texts include speculative material. This repository implements only the media pipeline described above.

---

## Contributing

1. Keep the engine and the GUI decoupled. GUI changes must not fork ffmpeg argument logic.
2. Do not add a Core encode path that skips probe or verification.
3. New containers belong in `CONTAINER_MATRIX` with legal video and audio encoder lists.
4. Size parsing tests should cover SI vs IEC units and rejection of non-positive values.
5. Do not pass remote URLs to ffmpeg.

Suggested first issues: unit tests for `parse_size` and `plan_encode`; a `--json-log` flag; optional `libsvtav1` preset documentation per platform.

---

## License

Specify the license before the first public push (`MIT` is a reasonable default for this utility). Until a `LICENSE` file exists, treat the code as source-available to the repository owner.

---

## Acknowledgements

Media work is performed by [FFmpeg](https://ffmpeg.org/). Interface controls use Tk. The size-refinement step uses \( \varphi \) only as a contraction factor.

---

*Collapse is what the old ledger calls survival when survival no longer needs the old ledger.*  
That line stays in the framework documents. This tool just writes a smaller file without losing the speech.
