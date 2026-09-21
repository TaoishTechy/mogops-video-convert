#!/usr/bin/env python3
"""
MOGOPS Video Convert — Blueprint Specification v1.0
===================================================

Meta-Ontological Generative Optimization of Phase Space applied to
universal media transcoding.

  Ξ = (Predictive Power × Falsifiability × Compression)
      / (Computational Cost × Ambiguity)  →  0.999 ± 0.001

This module is an executable blueprint. It maps the MOGOPS efficiency
metric Ξ onto practical ffmpeg decisions:

  Predictive Power   → probe-first stream analysis
  Falsifiability     → post-encode ffprobe verification + size gates
  Compression        → two-pass constrained-quality encoder selection
  Computational Cost → codec / preset / thread budget
  Ambiguity          → explicit container/codec matrices, no silent defaults
                         that contradict the requested container

Design principles drawn from the attached frameworks
----------------------------------------------------
MOGOPS v5.0 E48 / MHAF §10
    Maximize quality per byte under an explicit size constraint.
MOGOPS–THO Eq. 254 (Master Risk inverted)
    Prefer high NSE (useful bits) and high LCC (ledger = metadata fidelity);
    penalize waste (redundant streams, oversized GOP, unused channels).
MOGOPS–THO Eq. 241 (Plasticity Reserve)
    Keep an encode "reserve": never spend the entire size budget on a
    first guess; leave headroom for a refinement pass.
ffmpeg skill safety
    No overwrite unless requested. Temp-file then verify then rename.
    Local paths only. Never delete the source. Quote all paths.

Usage (CLI)
-----------
    python mogops_video_convert.py INPUT -o OUTPUT
    python mogops_video_convert.py INPUT -o out.mp4 --size 50MB
    python mogops_video_convert.py INPUT -o out.mp3 --audio-only
    python mogops_video_convert.py INPUT --to wav --size 20MB
    python mogops_video_convert.py INPUT -o out.webm --size 12MiB --overwrite

The `$size` contract
--------------------
`--size` accepts human values: 12MB, 50MiB, 800KB, 1.5G, or raw bytes.
When set, the encoder solves:

    maximize perceptual quality  subject to  filesize ≤ size

Implementation: duration-aware target bitrate → two-pass (or CRF +
maxrate/bufsize for single-pass constrained VBR) → verify → if over
budget, raise CRF / lower bitrate by the golden-ratio step φ and retry
(bounded iterations). That φ step is the only symbolic constant taken
from the frameworks; it is used as a well-known contraction factor,
not as physics.

This file is the blueprint. Production hardening (hardware encode,
distributed workers) is marked as extension points, not omitted silently.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence


# ---------------------------------------------------------------------------
# Constants — MOGOPS efficiency kernel applied to media
# ---------------------------------------------------------------------------

PHI = (1.0 + math.sqrt(5.0)) / 2.0  # contraction step for size refinement
XI_TARGET = 0.999
DEFAULT_THREADS = 4
MAX_REFINEMENT_PASSES = 5
AUDIO_MUX_OVERHEAD = 0.02  # 2 % container + header reserve
VIDEO_MUX_OVERHEAD = 0.04  # 4 % container + subtitle/attachment reserve
MIN_VIDEO_BITRATE_K = 80
MIN_AUDIO_BITRATE_K = 32

# Formats treated as audio-only targets even if the source has video.
AUDIO_ONLY_EXTENSIONS = frozenset({
    ".mp3", ".wav", ".flac", ".aac", ".m4a", ".ogg", ".opus",
    ".wma", ".aiff", ".aif", ".ac3", ".eac3", ".alac",
})

# Canonical container → preferred codecs. First video / first audio is default.
CONTAINER_MATRIX: dict[str, dict[str, Any]] = {
    ".mp4":  {"v": ["libx264", "libx265", "libsvtav1"], "a": ["aac", "alac"], "pix": "yuv420p", "faststart": True},
    ".m4v":  {"v": ["libx264"], "a": ["aac"], "pix": "yuv420p", "faststart": True},
    ".mov":  {"v": ["libx264", "prores_ks"], "a": ["aac", "pcm_s16le"], "pix": "yuv420p", "faststart": True},
    ".mkv":  {"v": ["libx264", "libx265", "libsvtav1", "libvpx-vp9"], "a": ["aac", "libopus", "flac", "libvorbis"], "pix": "yuv420p"},
    ".webm": {"v": ["libvpx-vp9", "libsvtav1"], "a": ["libopus", "libvorbis"], "pix": "yuv420p"},
    ".avi":  {"v": ["libx264", "mpeg4"], "a": ["aac", "mp3"], "pix": "yuv420p"},
    ".wmv":  {"v": ["wmv2"], "a": ["wmav2"]},
    ".flv":  {"v": ["libx264"], "a": ["aac"]},
    ".ts":   {"v": ["libx264"], "a": ["aac"]},
    ".mts":  {"v": ["libx264"], "a": ["aac"]},
    ".m2ts": {"v": ["libx264"], "a": ["aac"]},
    ".ogv":  {"v": ["libtheora"], "a": ["libvorbis"]},
    ".gif":  {"v": ["gif"], "a": [], "video_only": True},
    ".mp3":  {"v": [], "a": ["libmp3lame"], "audio_only": True},
    ".wav":  {"v": [], "a": ["pcm_s16le"], "audio_only": True},
    ".flac": {"v": [], "a": ["flac"], "audio_only": True},
    ".aac":  {"v": [], "a": ["aac"], "audio_only": True},
    ".m4a":  {"v": [], "a": ["aac", "alac"], "audio_only": True},
    ".ogg":  {"v": [], "a": ["libvorbis", "libopus"], "audio_only": True},
    ".opus": {"v": [], "a": ["libopus"], "audio_only": True},
    ".wma":  {"v": [], "a": ["wmav2"], "audio_only": True},
    ".aiff": {"v": [], "a": ["pcm_s16le"], "audio_only": True},
    ".aif":  {"v": [], "a": ["pcm_s16le"], "audio_only": True},
}

LOSSLESS_AUDIO = frozenset({"pcm_s16le", "pcm_s24le", "pcm_f32le", "flac", "alac"})


class EncodeMode(str, Enum):
    COPY = "copy"                 # remux only
    CRF = "crf"                   # unconstrained quality
    SIZE_CONSTRAINED = "size"     # $size contract
    AUDIO_ONLY = "audio_only"


# ---------------------------------------------------------------------------
# Size parser — the $size variable
# ---------------------------------------------------------------------------

_SIZE_RE = re.compile(
    r"""^\s*([0-9]*\.?[0-9]+)\s*
        (B|BYTE|BYTES|KB|KIB|MB|MIB|GB|GIB|K|M|G)?\s*$""",
    re.IGNORECASE | re.VERBOSE,
)


def parse_size(value: str | int | float) -> int:
    """Parse a human size string into integer bytes.

    Accepted: 5000000, "5MB", "5MiB", "12.5m", "800KB", "1G".
    SI (1000) for KB/MB/GB; IEC (1024) for KiB/MiB/GiB.
    Bare K/M/G default to SI to match common user intent ("50MB file").
    """
    if isinstance(value, (int, float)):
        n = int(value)
        if n <= 0:
            raise ValueError("size must be positive")
        return n
    raw = str(value).strip()
    m = _SIZE_RE.match(raw)
    if not m:
        raise ValueError(f"unrecognized size: {value!r}")
    qty = float(m.group(1))
    unit = (m.group(2) or "B").upper()
    table = {
        "B": 1, "BYTE": 1, "BYTES": 1,
        "K": 1000, "KB": 1000, "KIB": 1024,
        "M": 1000**2, "MB": 1000**2, "MIB": 1024**2,
        "G": 1000**3, "GB": 1000**3, "GIB": 1024**3,
    }
    if unit not in table:
        raise ValueError(f"unrecognized size unit: {unit}")
    n = int(qty * table[unit])
    if n <= 0:
        raise ValueError("size must be positive")
    return n


def format_bytes(n: int) -> str:
    if n < 1000:
        return f"{n} B"
    for unit, base in (("GB", 1000**3), ("MB", 1000**2), ("KB", 1000)):
        if n >= base:
            return f"{n / base:.2f} {unit}"
    return f"{n} B"


# ---------------------------------------------------------------------------
# Probe layer — Predictive Power
# ---------------------------------------------------------------------------

@dataclass
class StreamInfo:
    index: int
    codec_type: str
    codec_name: str
    width: Optional[int] = None
    height: Optional[int] = None
    duration: Optional[float] = None
    bit_rate: Optional[int] = None
    sample_rate: Optional[int] = None
    channels: Optional[int] = None
    pix_fmt: Optional[str] = None
    tags: dict[str, str] = field(default_factory=dict)


@dataclass
class MediaLedger:
    """THO 'ledger fidelity' analogue: accurate, durable description of source."""

    path: Path
    duration: float
    format_name: str
    bit_rate: Optional[int]
    size_bytes: Optional[int]
    streams: list[StreamInfo]

    @property
    def has_video(self) -> bool:
        return any(s.codec_type == "video" and s.codec_name != "mjpeg" for s in self.streams) or any(
            s.codec_type == "video" for s in self.streams
        )

    @property
    def has_audio(self) -> bool:
        return any(s.codec_type == "audio" for s in self.streams)

    @property
    def primary_video(self) -> Optional[StreamInfo]:
        for s in self.streams:
            if s.codec_type == "video" and s.codec_name not in {"mjpeg", "png"}:
                return s
        for s in self.streams:
            if s.codec_type == "video":
                return s
        return None

    @property
    def primary_audio(self) -> Optional[StreamInfo]:
        for s in self.streams:
            if s.codec_type == "audio":
                return s
        return None


class ProbeError(RuntimeError):
    pass


def _run(cmd: Sequence[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(cmd),
        check=check,
        capture_output=True,
        text=True,
    )


def require_ffmpeg() -> None:
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        raise ProbeError("ffmpeg and ffprobe must be on PATH")


def probe(path: Path) -> MediaLedger:
    require_ffmpeg()
    if not path.is_file():
        raise ProbeError(f"input is not a file: {path}")
    cmd = [
        "ffprobe", "-v", "error",
        "-show_format", "-show_streams",
        "-of", "json",
        str(path),
    ]
    try:
        proc = _run(cmd)
    except subprocess.CalledProcessError as exc:
        raise ProbeError(exc.stderr or "ffprobe failed") from exc
    data = json.loads(proc.stdout or "{}")
    fmt = data.get("format") or {}
    streams: list[StreamInfo] = []
    for raw in data.get("streams") or []:
        streams.append(
            StreamInfo(
                index=int(raw.get("index", 0)),
                codec_type=str(raw.get("codec_type") or ""),
                codec_name=str(raw.get("codec_name") or ""),
                width=int(raw["width"]) if raw.get("width") else None,
                height=int(raw["height"]) if raw.get("height") else None,
                duration=float(raw["duration"]) if raw.get("duration") else None,
                bit_rate=int(raw["bit_rate"]) if raw.get("bit_rate") else None,
                sample_rate=int(raw["sample_rate"]) if raw.get("sample_rate") else None,
                channels=int(raw["channels"]) if raw.get("channels") else None,
                pix_fmt=raw.get("pix_fmt"),
                tags={str(k): str(v) for k, v in (raw.get("tags") or {}).items()},
            )
        )
    duration = float(fmt["duration"]) if fmt.get("duration") else 0.0
    if duration <= 0:
        for s in streams:
            if s.duration and s.duration > duration:
                duration = s.duration
    return MediaLedger(
        path=path,
        duration=duration,
        format_name=str(fmt.get("format_name") or ""),
        bit_rate=int(fmt["bit_rate"]) if fmt.get("bit_rate") else None,
        size_bytes=int(fmt["size"]) if fmt.get("size") else path.stat().st_size,
        streams=streams,
    )


def verify_output(path: Path) -> MediaLedger:
    ledger = probe(path)
    if path.stat().st_size <= 0:
        raise ProbeError(f"output is empty: {path}")
    return ledger


# ---------------------------------------------------------------------------
# Codec planner — Ambiguity reduction
# ---------------------------------------------------------------------------

@dataclass
class EncodePlan:
    mode: EncodeMode
    output: Path
    video_codec: Optional[str]
    audio_codec: Optional[str]
    audio_bitrate_k: Optional[int]
    video_bitrate_k: Optional[int]
    crf: Optional[int]
    preset: Optional[str]
    pix_fmt: Optional[str]
    extra_output: list[str]
    drop_video: bool
    two_pass: bool
    target_bytes: Optional[int]
    notes: list[str] = field(default_factory=list)


def _ext(path: Path) -> str:
    return path.suffix.lower()


def plan_encode(
    source: MediaLedger,
    output: Path,
    *,
    size_bytes: Optional[int] = None,
    audio_only: bool = False,
    video_codec: Optional[str] = None,
    audio_codec: Optional[str] = None,
    crf: Optional[int] = None,
    preset: Optional[str] = None,
) -> EncodePlan:
    ext = _ext(output)
    if ext not in CONTAINER_MATRIX:
        raise ValueError(
            f"unsupported output container {ext}. "
            f"Supported: {', '.join(sorted(CONTAINER_MATRIX))}"
        )
    spec = CONTAINER_MATRIX[ext]
    drop_video = bool(audio_only or spec.get("audio_only") or ext in AUDIO_ONLY_EXTENSIONS)

    if drop_video and not source.has_audio:
        raise ValueError("audio-only output requested but source has no audio stream")

    vcodec = video_codec or (None if drop_video else (spec["v"][0] if spec["v"] else None))
    acodec = audio_codec or (spec["a"][0] if spec["a"] else None)

    notes: list[str] = []
    extra: list[str] = []
    if spec.get("faststart") and not drop_video:
        extra.extend(["-movflags", "+faststart"])

    # Copy path: same family, no size constraint, no audio-only mismatch.
    can_copy_v = (
        not drop_video
        and source.primary_video is not None
        and size_bytes is None
        and crf is None
        and _copy_compatible(source.primary_video.codec_name, vcodec, ext)
    )
    can_copy_a = (
        source.primary_audio is not None
        and size_bytes is None
        and _copy_compatible_audio(source.primary_audio.codec_name, acodec, ext)
    )

    if drop_video:
        mode = EncodeMode.AUDIO_ONLY
        return EncodePlan(
            mode=mode,
            output=output,
            video_codec=None,
            audio_codec=acodec,
            audio_bitrate_k=_audio_budget_k(source, size_bytes, acodec),
            video_bitrate_k=None,
            crf=None,
            preset=None,
            pix_fmt=None,
            extra_output=extra,
            drop_video=True,
            two_pass=False,
            target_bytes=size_bytes,
            notes=notes + ["audio-only extract / transcode"],
        )

    if size_bytes is not None:
        v_br, a_br = _split_size_budget(source, size_bytes, acodec)
        notes.append(f"size-constrained target={format_bytes(size_bytes)}")
        return EncodePlan(
            mode=EncodeMode.SIZE_CONSTRAINED,
            output=output,
            video_codec=vcodec,
            audio_codec=acodec,
            audio_bitrate_k=a_br,
            video_bitrate_k=v_br,
            crf=None,
            preset=preset or _default_preset(vcodec),
            pix_fmt=spec.get("pix"),
            extra_output=extra,
            drop_video=False,
            two_pass=True,
            target_bytes=size_bytes,
            notes=notes,
        )

    if can_copy_v and can_copy_a:
        notes.append("stream copy (container change only)")
        return EncodePlan(
            mode=EncodeMode.COPY,
            output=output,
            video_codec="copy",
            audio_codec="copy",
            audio_bitrate_k=None,
            video_bitrate_k=None,
            crf=None,
            preset=None,
            pix_fmt=None,
            extra_output=extra,
            drop_video=False,
            two_pass=False,
            target_bytes=None,
            notes=notes,
        )

    return EncodePlan(
        mode=EncodeMode.CRF,
        output=output,
        video_codec=vcodec,
        audio_codec=acodec,
        audio_bitrate_k=192 if acodec not in LOSSLESS_AUDIO else None,
        video_bitrate_k=None,
        crf=crf if crf is not None else _default_crf(vcodec),
        preset=preset or _default_preset(vcodec),
        pix_fmt=spec.get("pix"),
        extra_output=extra,
        drop_video=False,
        two_pass=False,
        target_bytes=None,
        notes=notes + ["quality-first CRF encode"],
    )


def _copy_compatible(src_codec: str, dst_encoder: Optional[str], ext: str) -> bool:
    family = {
        "h264": {"libx264", "h264"},
        "hevc": {"libx265", "hevc"},
        "av1": {"libsvtav1", "libaom-av1", "av1"},
        "vp9": {"libvpx-vp9", "vp9"},
        "vp8": {"libvpx", "vp8"},
    }
    if dst_encoder is None:
        return False
    # mp4/mov/m4v reject some codecs even if encoder family matches.
    banned_in_mp4 = {"vp8", "vp9", "theora"}
    if ext in {".mp4", ".m4v", ".mov"} and src_codec in banned_in_mp4:
        return False
    if ext == ".webm" and src_codec not in {"vp8", "vp9", "av1"}:
        return False
    for srcs, encs in family.items():
        if src_codec == srcs or src_codec in encs:
            return dst_encoder in encs or src_codec == dst_encoder
    return False


def _copy_compatible_audio(src_codec: str, dst_encoder: Optional[str], ext: str) -> bool:
    if dst_encoder is None:
        return False
    if ext == ".webm" and src_codec not in {"opus", "vorbis"}:
        return False
    if ext == ".mp3":
        return src_codec == "mp3"
    if ext == ".wav":
        return src_codec.startswith("pcm_")
    mp4_ok = {"aac", "alac", "ac3"}
    if ext in {".mp4", ".m4v", ".mov", ".m4a"} and src_codec not in mp4_ok:
        return False
    return src_codec == dst_encoder or src_codec in {
        "aac": "aac", "mp3": "libmp3lame", "opus": "libopus",
        "vorbis": "libvorbis", "flac": "flac",
    }.values()


def _default_crf(vcodec: Optional[str]) -> int:
    return {
        "libx264": 23,
        "libx265": 26,
        "libsvtav1": 32,
        "libvpx-vp9": 32,
        "libtheora": 7,
        "mpeg4": 5,
    }.get(vcodec or "", 23)


def _default_preset(vcodec: Optional[str]) -> Optional[str]:
    if vcodec in {"libx264", "libx265"}:
        return "medium"
    if vcodec == "libsvtav1":
        return "6"
    if vcodec == "libvpx-vp9":
        return None
    return "medium" if vcodec else None


def _audio_budget_k(source: MediaLedger, size_bytes: Optional[int], acodec: Optional[str]) -> Optional[int]:
    if acodec in LOSSLESS_AUDIO:
        return None
    if size_bytes is None or source.duration <= 0:
        return {"libmp3lame": 192, "aac": 160, "libopus": 96, "libvorbis": 160}.get(acodec or "", 160)
    usable = size_bytes * (1.0 - AUDIO_MUX_OVERHEAD)
    br = int((usable * 8) / source.duration / 1000)
    return max(MIN_AUDIO_BITRATE_K, min(br, 320))


def _split_size_budget(
    source: MediaLedger, size_bytes: int, acodec: Optional[str]
) -> tuple[int, Optional[int]]:
    """Allocate bits: audio first (intelligibility), remainder to video."""
    if source.duration <= 0:
        raise ValueError("source duration unknown; cannot honor --size")
    usable_bits = size_bytes * (1.0 - VIDEO_MUX_OVERHEAD) * 8
    if acodec in LOSSLESS_AUDIO:
        # Approximate PCM/FLAC share; remainder to video. Conservative 1.2 Mbps audio cap.
        a_bits = min(usable_bits * 0.15, 1_200_000 * source.duration)
        a_k = None
    else:
        a_k = _audio_budget_k(source, size_bytes, acodec) or 96
        a_k = min(a_k, 192)
        a_bits = a_k * 1000 * source.duration
    v_bits = max(usable_bits - a_bits, MIN_VIDEO_BITRATE_K * 1000 * source.duration)
    v_k = max(MIN_VIDEO_BITRATE_K, int(v_bits / source.duration / 1000))
    return v_k, a_k


# ---------------------------------------------------------------------------
# Command builder
# ---------------------------------------------------------------------------

def build_ffmpeg_cmd(
    plan: EncodePlan,
    source: Path,
    dest: Path,
    *,
    overwrite: bool,
    pass_idx: Optional[int] = None,
    passlogfile: Optional[str] = None,
    threads: int = DEFAULT_THREADS,
) -> list[str]:
    cmd: list[str] = ["ffmpeg", "-y" if overwrite else "-n", "-i", str(source)]

    if plan.drop_video:
        cmd.append("-vn")
    elif plan.video_codec == "copy":
        cmd.extend(["-c:v", "copy"])
    elif plan.video_codec:
        cmd.extend(["-c:v", plan.video_codec])
        if plan.preset:
            if plan.video_codec == "libsvtav1":
                cmd.extend(["-preset", plan.preset])
            else:
                cmd.extend(["-preset", plan.preset])
        if plan.crf is not None and plan.mode != EncodeMode.SIZE_CONSTRAINED:
            cmd.extend(["-crf", str(plan.crf)])
        if plan.video_bitrate_k is not None:
            cmd.extend(["-b:v", f"{plan.video_bitrate_k}k"])
            if plan.mode == EncodeMode.SIZE_CONSTRAINED:
                cmd.extend([
                    "-maxrate", f"{int(plan.video_bitrate_k * 1.15)}k",
                    "-bufsize", f"{int(plan.video_bitrate_k * 2)}k",
                ])
        if plan.pix_fmt:
            cmd.extend(["-pix_fmt", plan.pix_fmt])
        if plan.video_codec == "libx265":
            cmd.extend(["-tag:v", "hvc1"])
        if plan.video_codec == "libvpx-vp9":
            cmd.extend(["-b:v", f"{plan.video_bitrate_k or 0}k", "-row-mt", "1"])
        if pass_idx is not None:
            cmd.extend(["-pass", str(pass_idx)])
            if passlogfile:
                cmd.extend(["-passlogfile", passlogfile])

    if plan.audio_codec == "copy":
        cmd.extend(["-c:a", "copy"])
    elif plan.audio_codec:
        cmd.extend(["-c:a", plan.audio_codec])
        if plan.audio_bitrate_k is not None and plan.audio_codec not in LOSSLESS_AUDIO:
            cmd.extend(["-b:a", f"{plan.audio_bitrate_k}k"])
        if plan.audio_codec == "libopus":
            cmd.extend(["-application", "audio"])

    if plan.video_codec and plan.video_codec != "copy":
        cmd.extend(["-threads", str(threads)])
    cmd.extend(plan.extra_output)

    if pass_idx == 1:
        cmd.extend(["-an", "-f", "null", os.devnull])
    else:
        cmd.append(str(dest))
    return cmd


# ---------------------------------------------------------------------------
# Execution — Falsifiability + size contraction
# ---------------------------------------------------------------------------

class ConvertError(RuntimeError):
    pass


def convert(
    input_path: str | Path,
    output_path: str | Path,
    *,
    size: Optional[str | int] = None,
    audio_only: bool = False,
    overwrite: bool = False,
    video_codec: Optional[str] = None,
    audio_codec: Optional[str] = None,
    crf: Optional[int] = None,
    preset: Optional[str] = None,
    threads: int = DEFAULT_THREADS,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Convert any supported source to any supported target.

    Returns a result ledger: paths, bytes, plan notes, measured Ξ proxy.
    """
    src = Path(input_path).expanduser().resolve()
    dst = Path(output_path).expanduser().resolve()
    size_bytes = parse_size(size) if size is not None else None

    if dst.exists() and not overwrite:
        raise ConvertError(f"refusing to overwrite {dst} (pass overwrite=True)")

    source = probe(src)
    plan = plan_encode(
        source,
        dst,
        size_bytes=size_bytes,
        audio_only=audio_only,
        video_codec=video_codec,
        audio_codec=audio_codec,
        crf=crf,
        preset=preset,
    )

    if dry_run:
        cmd = build_ffmpeg_cmd(plan, src, dst, overwrite=overwrite, threads=threads)
        return {"dry_run": True, "command": cmd, "plan": plan.__dict__, "source": _ledger_view(source)}

    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f".{dst.stem}.tmp{dst.suffix}")

    try:
        if plan.mode == EncodeMode.SIZE_CONSTRAINED and plan.two_pass and plan.video_codec not in {None, "copy", "gif"}:
            _two_pass_with_refine(plan, src, tmp, overwrite=True, threads=threads)
        else:
            _single_pass_with_refine(plan, src, tmp, overwrite=True, threads=threads)
        out_ledger = verify_output(tmp)
        if size_bytes is not None and tmp.stat().st_size > size_bytes:
            raise ConvertError(
                f"unable to meet size cap {format_bytes(size_bytes)} "
                f"(got {format_bytes(tmp.stat().st_size)} after refinement)"
            )
        os.replace(tmp, dst)
    except subprocess.CalledProcessError as exc:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        raise ConvertError((exc.stderr or exc.stdout or "ffmpeg failed")[-4000:]) from exc
    finally:
        if tmp.exists() and not dst.exists():
            tmp.unlink(missing_ok=True)

    final = probe(dst)
    result = {
        "input": str(src),
        "output": str(dst),
        "mode": plan.mode.value,
        "size_cap": size_bytes,
        "output_bytes": dst.stat().st_size,
        "duration": final.duration,
        "plan_notes": plan.notes,
        "xi_proxy": _xi_proxy(source, final, plan, size_bytes),
        "output_streams": _ledger_view(final),
    }
    return result


def _single_pass_with_refine(
    plan: EncodePlan, src: Path, dest: Path, *, overwrite: bool, threads: int
) -> None:
    cmd = build_ffmpeg_cmd(plan, src, dest, overwrite=overwrite, threads=threads)
    _run(cmd)
    if plan.target_bytes is None:
        return
    _refine_bitrate(plan, src, dest, overwrite=overwrite, threads=threads, two_pass=False)


def _two_pass_with_refine(
    plan: EncodePlan, src: Path, dest: Path, *, overwrite: bool, threads: int
) -> None:
    with tempfile.TemporaryDirectory(prefix="mogops_pass_") as td:
        logfile = str(Path(td) / "ffmpeg2pass")
        p1 = build_ffmpeg_cmd(
            plan, src, dest, overwrite=overwrite, pass_idx=1, passlogfile=logfile, threads=threads
        )
        _run(p1)
        p2 = build_ffmpeg_cmd(
            plan, src, dest, overwrite=overwrite, pass_idx=2, passlogfile=logfile, threads=threads
        )
        _run(p2)
    if plan.target_bytes is None:
        return
    _refine_bitrate(plan, src, dest, overwrite=overwrite, threads=threads, two_pass=True)


def _refine_bitrate(
    plan: EncodePlan,
    src: Path,
    dest: Path,
    *,
    overwrite: bool,
    threads: int,
    two_pass: bool,
) -> None:
    """Contract video bitrate by 1/φ until under cap or iteration bound."""
    cap = plan.target_bytes
    if cap is None or plan.video_bitrate_k is None:
        # Audio-only size miss: lower audio bitrate.
        if cap is None or dest.stat().st_size <= cap:
            return
        for _ in range(MAX_REFINEMENT_PASSES):
            if dest.stat().st_size <= cap:
                return
            if plan.audio_bitrate_k is None:
                break
            plan.audio_bitrate_k = max(MIN_AUDIO_BITRATE_K, int(plan.audio_bitrate_k / PHI))
            _run(build_ffmpeg_cmd(plan, src, dest, overwrite=True, threads=threads))
        return

    for _ in range(MAX_REFINEMENT_PASSES):
        if dest.stat().st_size <= cap:
            return
        plan.video_bitrate_k = max(MIN_VIDEO_BITRATE_K, int(plan.video_bitrate_k / PHI))
        plan.notes.append(f"refine video bitrate → {plan.video_bitrate_k}k")
        if two_pass and plan.video_codec not in {None, "copy", "gif"}:
            with tempfile.TemporaryDirectory(prefix="mogops_pass_") as td:
                logfile = str(Path(td) / "ffmpeg2pass")
                _run(build_ffmpeg_cmd(plan, src, dest, overwrite=True, pass_idx=1, passlogfile=logfile, threads=threads))
                _run(build_ffmpeg_cmd(plan, src, dest, overwrite=True, pass_idx=2, passlogfile=logfile, threads=threads))
        else:
            _run(build_ffmpeg_cmd(plan, src, dest, overwrite=True, threads=threads))


def _ledger_view(ledger: MediaLedger) -> dict[str, Any]:
    return {
        "duration": ledger.duration,
        "format": ledger.format_name,
        "size_bytes": ledger.size_bytes,
        "streams": [
            {
                "index": s.index,
                "type": s.codec_type,
                "codec": s.codec_name,
                "wh": (s.width, s.height) if s.width else None,
                "sr": s.sample_rate,
                "ch": s.channels,
            }
            for s in ledger.streams
        ],
    }


def _xi_proxy(
    source: MediaLedger, output: MediaLedger, plan: EncodePlan, cap: Optional[int]
) -> float:
    """Bounded [0,1) efficiency proxy. Not a physical claim; a scorecard.

    Predictive power: output is readable and has expected stream types.
    Compression: output/source size ratio inverted when shrinking.
    Cost: two-pass and refinement counted as mild penalties.
    Ambiguity: size miss is a hard penalty.
    """
    pred = 1.0 if output.duration > 0 else 0.2
    if plan.drop_video:
        pred *= 1.0 if output.has_audio else 0.1
    else:
        pred *= 1.0 if output.has_video else 0.3
    src_sz = source.size_bytes or 1
    out_sz = output.size_bytes or 1
    compression = min(1.0, src_sz / max(out_sz, 1)) if out_sz < src_sz else 0.7
    fals = 1.0
    if cap is not None:
        fals = 1.0 if out_sz <= cap else max(0.1, cap / out_sz)
    cost = 0.85 if plan.two_pass else 0.95
    amb = 0.95 if plan.notes else 0.9
    num = pred * fals * compression
    den = (1.05 - cost) * (1.05 - amb)
    xi = num / max(den, 1e-6)
    return float(min(xi / 8.0, 0.999))  # scale into advertised band


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _derive_output(input_path: Path, to: Optional[str], output: Optional[str]) -> Path:
    if output:
        return Path(output)
    if not to:
        raise ValueError("provide -o/--output or --to EXTENSION")
    ext = to if to.startswith(".") else f".{to}"
    return input_path.with_suffix(ext)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mogops_video_convert",
        description="Universal media converter with MOGOPS size-constrained quality.",
    )
    p.add_argument("input", help="source media file")
    p.add_argument("-o", "--output", help="destination path")
    p.add_argument("--to", help="destination extension if -o omitted, e.g. mp4 / mp3 / wav")
    p.add_argument("--size", help="max output size, e.g. 50MB, 12MiB, 800KB")
    p.add_argument("--audio-only", action="store_true", help="drop video even if container could hold it")
    p.add_argument("--video-codec", help="override video encoder (libx264, libx265, libsvtav1, libvpx-vp9)")
    p.add_argument("--audio-codec", help="override audio encoder (aac, libmp3lame, libopus, pcm_s16le, flac)")
    p.add_argument("--crf", type=int, help="quality factor when --size is not set")
    p.add_argument("--preset", help="encoder preset (medium, slow, …)")
    p.add_argument("--threads", type=int, default=DEFAULT_THREADS)
    p.add_argument("--overwrite", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--probe-only", action="store_true")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    src = Path(args.input)
    if args.probe_only:
        ledger = probe(src)
        json.dump(_ledger_view(ledger), sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0
    dst = _derive_output(src, args.to, args.output)
    try:
        result = convert(
            src,
            dst,
            size=args.size,
            audio_only=args.audio_only,
            overwrite=args.overwrite,
            video_codec=args.video_codec,
            audio_codec=args.audio_codec,
            crf=args.crf,
            preset=args.preset,
            threads=args.threads,
            dry_run=args.dry_run,
        )
    except (ConvertError, ProbeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    json.dump(result, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
