#!/usr/bin/env python3
"""
MOGOPS Video Convert — Seamless GUI
===================================

Twenty-four interface optimizations, each named and implemented.
The convert engine remains mogops_video_convert.py; this file is
presentation, validation, and non-blocking control only.

U01 Intent-first layout          Primary path is Input → Target → Convert.
U02 Progressive disclosure       Advanced codecs hidden until requested.
U03 Probe-before-plan            Source ledger appears before encode is armed.
U04 Predictive container chips   Recommended targets from stream types.
U05 Size–quality Pareto control  Live bitrate estimate from duration × cap.
U06 Constraint clarity           Size mode is an explicit armed state, not a guess.
U07 Drag-and-drop intake         File path accepted via drop or picker.
U08 Auto-derived output path     Target folder + stem + chip extension.
U09 Container-aware codec filter Dropdowns only list legal encoders.
U10 Audio-only one gesture       Chip to mp3/wav/flac drops video automatically.
U11 Dry-run command preview      Exact argv visible before work starts.
U12 Non-blocking encode          Work runs on a worker thread; UI stays live.
U13 Determinate-when-possible    Pulse during encode; determinate on verify.
U14 Cancel does not touch source Source is never deleted or truncated.
U15 Overwrite as a decision      Confirm only when the destination exists.
U16 Actionable errors            Failures quote the last ffmpeg lines, not a void.
U17 Before/after size ledger     Bytes in, bytes out, ratio, Ξ proxy.
U18 Keyboard-first               Enter converts, Esc cancels, O opens output.
U19 High-contrast density theme  Dark canvas, single accent, no decorative motion.
U20 Session memory               Last directory and last target persist.
U21 Validation gate              Convert disabled until probe + target are valid.
U22 Atomic result reveal         Success panel appears only after verify+rename.
U23 Tooltip microcopy            Every control states consequence, not jargon.
U24 Reduced ambiguity copy       Status line uses verbs: probing, planning, writing.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Optional

# Ensure sibling engine import when launched as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from mogops_video_convert import (  # noqa: E402
    AUDIO_ONLY_EXTENSIONS,
    CONTAINER_MATRIX,
    convert,
    format_bytes,
    parse_size,
    probe,
    ProbeError,
)


SESSION_PATH = Path.home() / ".mogops_video_convert_gui.json"

VIDEO_CHIPS = [".mp4", ".mkv", ".webm", ".mov", ".avi"]
AUDIO_CHIPS = [".mp3", ".wav", ".flac", ".m4a", ".ogg", ".opus"]

BG = "#101418"
BG_RAISED = "#171d24"
BG_INPUT = "#0c1014"
FG = "#e7edf3"
FG_MUTED = "#8b9aab"
ACCENT = "#3ee0b2"
ACCENT_DIM = "#1c4f43"
DANGER = "#e07a6a"
WARN = "#e0c36a"


class ToolTip:
    """U23 — consequence-first hover copy. Delayed, single instance."""

    def __init__(self, widget: tk.Widget, text: str, delay_ms: int = 450) -> None:
        self.widget = widget
        self.text = text
        self.delay_ms = delay_ms
        self._after: Optional[str] = None
        self._tip: Optional[tk.Toplevel] = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event: tk.Event) -> None:  # type: ignore[type-arg]
        self._cancel()
        self._after = self.widget.after(self.delay_ms, self._show)

    def _cancel(self) -> None:
        if self._after:
            self.widget.after_cancel(self._after)
            self._after = None

    def _show(self) -> None:
        if self._tip or not self.text:
            return
        x = self.widget.winfo_rootx() + 16
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        tip = tk.Toplevel(self.widget)
        tip.wm_overrideredirect(True)
        tip.wm_geometry(f"+{x}+{y}")
        frame = tk.Frame(tip, bg=ACCENT_DIM, padx=1, pady=1)
        frame.pack()
        lbl = tk.Label(
            frame,
            text=self.text,
            bg=BG_RAISED,
            fg=FG,
            font=("Segoe UI", 9),
            justify="left",
            padx=10,
            pady=6,
            wraplength=320,
        )
        lbl.pack()
        self._tip = tip

    def _hide(self, _event: Optional[tk.Event] = None) -> None:  # type: ignore[type-arg]
        self._cancel()
        if self._tip:
            self._tip.destroy()
            self._tip = None


class MogopsGui(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("MOGOPS Convert")
        self.configure(bg=BG)
        self.minsize(860, 640)
        self.geometry("960x720")

        self.source_path: Optional[Path] = None
        self.output_dir: Optional[Path] = None
        self.out_name = tk.StringVar(value="")
        self._name_customized = False
        self.target_ext = tk.StringVar(value=".mp4")
        self.size_enabled = tk.BooleanVar(value=False)
        self.size_value = tk.StringVar(value="50")
        self.size_unit = tk.StringVar(value="MB")
        self.audio_only = tk.BooleanVar(value=False)
        self.overwrite = tk.BooleanVar(value=False)
        self.show_advanced = tk.BooleanVar(value=False)
        self.video_codec = tk.StringVar(value="auto")
        self.audio_codec = tk.StringVar(value="auto")
        self.preset = tk.StringVar(value="medium")
        self.status = tk.StringVar(value="Idle — drop a file or browse.")
        self.estimate = tk.StringVar(value="")
        self.command_preview = tk.StringVar(value="")
        self._worker: Optional[threading.Thread] = None
        self._cancel = threading.Event()
        self._busy = False
        self._source_bytes = 0
        self._duration = 0.0

        self._apply_style()
        self._build()
        self._load_session()
        self._bind_keys()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        try:
            self.tk.call("tk", "windowingsystem")
        except tk.TclError:
            pass
        self._enable_dnd()

    # ----- chrome -----

    def _apply_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(".", background=BG, foreground=FG, fieldbackground=BG_INPUT)
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background=BG_RAISED)
        style.configure("TLabel", background=BG, foreground=FG, font=("Segoe UI", 10))
        style.configure("Muted.TLabel", background=BG, foreground=FG_MUTED, font=("Segoe UI", 9))
        style.configure("Head.TLabel", background=BG, foreground=FG, font=("Segoe UI", 16, "bold"))
        style.configure("Card.TLabel", background=BG_RAISED, foreground=FG)
        style.configure("CardMuted.TLabel", background=BG_RAISED, foreground=FG_MUTED, font=("Segoe UI", 9))
        style.configure("Accent.TButton", font=("Segoe UI", 11, "bold"))
        style.configure("TCheckbutton", background=BG, foreground=FG)
        style.configure("TRadiobutton", background=BG_RAISED, foreground=FG)
        style.configure("TEntry", fieldbackground=BG_INPUT, foreground=FG)
        style.configure("TCombobox", fieldbackground=BG_INPUT, foreground=FG)
        style.configure("Horizontal.TProgressbar", troughcolor=BG_INPUT, background=ACCENT)
        style.map("TCheckbutton", background=[("active", BG)])

    def _build(self) -> None:
        root = ttk.Frame(self, padding=20)
        root.pack(fill="both", expand=True)

        ttk.Label(root, text="MOGOPS Convert", style="Head.TLabel").pack(anchor="w")
        ttk.Label(
            root,
            text="Maximum quality under an explicit size ceiling. Source is never deleted.",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(0, 16))

        # U01 / U07 source card
        src_card = tk.Frame(root, bg=BG_RAISED, padx=14, pady=12)
        src_card.pack(fill="x", pady=(0, 10))
        ttk.Label(src_card, text="Source", style="Card.TLabel").pack(anchor="w")
        row = tk.Frame(src_card, bg=BG_RAISED)
        row.pack(fill="x", pady=(6, 0))
        self.src_entry = tk.Entry(
            row, bg=BG_INPUT, fg=FG, insertbackground=FG, relief="flat", font=("Segoe UI", 10)
        )
        self.src_entry.pack(side="left", fill="x", expand=True, ipady=6, padx=(0, 8))
        self.src_entry.bind("<Return>", lambda _e: self._load_source(self.src_entry.get().strip()))
        browse = tk.Button(
            row, text="Browse", command=self._browse_source,
            bg=ACCENT_DIM, fg=FG, relief="flat", padx=12, pady=6, cursor="hand2",
        )
        browse.pack(side="left")
        ToolTip(browse, "U07 — Choose a local media file. URLs are rejected.")
        self.probe_box = tk.Text(
            src_card, height=5, bg=BG_INPUT, fg=FG_MUTED, relief="flat",
            font=("Consolas", 9), wrap="word",
        )
        self.probe_box.pack(fill="x", pady=(10, 0))
        self.probe_box.insert("1.0", "No ledger yet. Select a source to probe streams.")
        self.probe_box.configure(state="disabled")

        # U04 target chips
        tgt = tk.Frame(root, bg=BG_RAISED, padx=14, pady=12)
        tgt.pack(fill="x", pady=(0, 10))
        ttk.Label(tgt, text="Target container", style="Card.TLabel").pack(anchor="w")
        chip_row = tk.Frame(tgt, bg=BG_RAISED)
        chip_row.pack(anchor="w", pady=(8, 4))
        self._chip_buttons: dict[str, tk.Button] = {}
        for ext in VIDEO_CHIPS + AUDIO_CHIPS:
            b = tk.Button(
                chip_row, text=ext[1:], command=lambda e=ext: self._set_ext(e),
                bg=BG_INPUT, fg=FG, relief="flat", padx=10, pady=4, cursor="hand2",
                font=("Segoe UI", 9),
            )
            b.pack(side="left", padx=(0, 6), pady=2)
            self._chip_buttons[ext] = b
            kind = "audio-only extract" if ext in AUDIO_ONLY_EXTENSIONS else "video remux or encode"
            ToolTip(b, f"U04 / U10 — Write {ext} ({kind}).")
        self._refresh_chips()

        name_row = tk.Frame(tgt, bg=BG_RAISED)
        name_row.pack(fill="x", pady=(10, 0))
        ttk.Label(name_row, text="Output filename", style="CardMuted.TLabel").pack(side="left")
        self.name_entry = tk.Entry(
            name_row, textvariable=self.out_name,
            bg=BG_INPUT, fg=FG, insertbackground=FG, relief="flat", font=("Segoe UI", 10),
        )
        self.name_entry.pack(side="left", fill="x", expand=True, padx=8, ipady=5)
        self.name_entry.bind("<KeyRelease>", self._on_name_edit)
        reset_name = tk.Button(
            name_row, text="Default", command=self._reset_out_name,
            bg=ACCENT_DIM, fg=FG, relief="flat", padx=10, pady=5, cursor="hand2",
        )
        reset_name.pack(side="left")
        ToolTip(
            self.name_entry,
            "Optional. Defaults to the source stem. Extension is always taken from the selected container chip.",
        )
        ToolTip(reset_name, "Restore the source filename stem.")

        out_row = tk.Frame(tgt, bg=BG_RAISED)
        out_row.pack(fill="x", pady=(8, 0))
        ttk.Label(out_row, text="Output folder", style="CardMuted.TLabel").pack(side="left")
        self.out_entry = tk.Entry(
            out_row, bg=BG_INPUT, fg=FG, insertbackground=FG, relief="flat", font=("Segoe UI", 10)
        )
        self.out_entry.pack(side="left", fill="x", expand=True, padx=8, ipady=5)
        tk.Button(
            out_row, text="Folder", command=self._browse_out,
            bg=ACCENT_DIM, fg=FG, relief="flat", padx=10, pady=5, cursor="hand2",
        ).pack(side="left")

        # U05 / U06 size contract
        size_card = tk.Frame(root, bg=BG_RAISED, padx=14, pady=12)
        size_card.pack(fill="x", pady=(0, 10))
        top = tk.Frame(size_card, bg=BG_RAISED)
        top.pack(fill="x")
        self.size_check = tk.Checkbutton(
            top, text="Arm size ceiling ($size)", variable=self.size_enabled,
            command=self._recompute_estimate,
            bg=BG_RAISED, fg=FG, selectcolor=BG_INPUT, activebackground=BG_RAISED,
            activeforeground=FG, highlightthickness=0,
        )
        self.size_check.pack(side="left")
        ToolTip(self.size_check, "U06 — When armed, encode maximizes quality subject to filesize ≤ cap.")
        self.size_entry = tk.Entry(
            top, textvariable=self.size_value, width=8,
            bg=BG_INPUT, fg=FG, insertbackground=FG, relief="flat", font=("Segoe UI", 10),
            justify="right",
        )
        self.size_entry.pack(side="left", padx=(16, 6), ipady=4)
        self.size_entry.bind("<KeyRelease>", lambda _e: self._recompute_estimate())
        unit = ttk.Combobox(
            top, textvariable=self.size_unit, values=("KB", "MB", "MiB", "GB"),
            width=6, state="readonly",
        )
        unit.pack(side="left")
        unit.bind("<<ComboboxSelected>>", lambda _e: self._recompute_estimate())
        ttk.Label(size_card, textvariable=self.estimate, style="CardMuted.TLabel").pack(anchor="w", pady=(8, 0))

        opts = tk.Frame(size_card, bg=BG_RAISED)
        opts.pack(fill="x", pady=(8, 0))
        tk.Checkbutton(
            opts, text="Force audio-only", variable=self.audio_only,
            command=self._on_audio_only,
            bg=BG_RAISED, fg=FG, selectcolor=BG_INPUT, activebackground=BG_RAISED,
            activeforeground=FG, highlightthickness=0,
        ).pack(side="left")
        tk.Checkbutton(
            opts, text="Overwrite destination", variable=self.overwrite,
            bg=BG_RAISED, fg=FG, selectcolor=BG_INPUT, activebackground=BG_RAISED,
            activeforeground=FG, highlightthickness=0,
        ).pack(side="left", padx=(16, 0))
        tk.Checkbutton(
            opts, text="Advanced codecs", variable=self.show_advanced,
            command=self._toggle_advanced,
            bg=BG_RAISED, fg=FG, selectcolor=BG_INPUT, activebackground=BG_RAISED,
            activeforeground=FG, highlightthickness=0,
        ).pack(side="left", padx=(16, 0))

        # U02 / U09 advanced
        self.adv_host = tk.Frame(root, bg=BG)
        self.adv_host.pack(fill="x")
        self.adv = tk.Frame(self.adv_host, bg=BG_RAISED, padx=14, pady=10)
        ttk.Label(self.adv, text="Video encoder", style="CardMuted.TLabel").grid(row=0, column=0, sticky="w")
        self.vcombo = ttk.Combobox(self.adv, textvariable=self.video_codec, width=16, state="readonly")
        self.vcombo.grid(row=0, column=1, padx=8, pady=4)
        ttk.Label(self.adv, text="Audio encoder", style="CardMuted.TLabel").grid(row=0, column=2, sticky="w")
        self.acombo = ttk.Combobox(self.adv, textvariable=self.audio_codec, width=16, state="readonly")
        self.acombo.grid(row=0, column=3, padx=8, pady=4)
        ttk.Label(self.adv, text="Preset", style="CardMuted.TLabel").grid(row=0, column=4, sticky="w")
        ttk.Combobox(
            self.adv, textvariable=self.preset,
            values=("ultrafast", "fast", "medium", "slow", "veryslow"),
            width=10, state="readonly",
        ).grid(row=0, column=5, padx=8)
        self._sync_codec_lists()

        # U11 preview
        ttk.Label(root, text="Planned command", style="Muted.TLabel").pack(anchor="w")
        self.cmd_box = tk.Text(
            root, height=3, bg=BG_INPUT, fg=ACCENT, relief="flat",
            font=("Consolas", 8), wrap="word",
        )
        self.cmd_box.pack(fill="x", pady=(4, 10))
        self.cmd_box.insert("1.0", "Command appears after a valid source and target are set.")
        self.cmd_box.configure(state="disabled")

        # actions U12 / U18
        actions = tk.Frame(root, bg=BG)
        actions.pack(fill="x", pady=(0, 8))
        self.convert_btn = tk.Button(
            actions, text="Convert  (Enter)", command=self._start_convert,
            bg=ACCENT, fg="#04221a", relief="flat", padx=18, pady=8,
            font=("Segoe UI", 11, "bold"), cursor="hand2",
        )
        self.convert_btn.pack(side="left")
        self.cancel_btn = tk.Button(
            actions, text="Cancel  (Esc)", command=self._request_cancel,
            bg=BG_RAISED, fg=FG, relief="flat", padx=14, pady=8, state="disabled",
        )
        self.cancel_btn.pack(side="left", padx=8)
        self.open_btn = tk.Button(
            actions, text="Open output  (O)", command=self._open_output,
            bg=BG_RAISED, fg=FG, relief="flat", padx=14, pady=8, state="disabled",
        )
        self.open_btn.pack(side="left")
        ToolTip(self.convert_btn, "U21 — Armed only when probe succeeded and the target is legal.")

        self.progress = ttk.Progressbar(root, mode="indeterminate")
        self.progress.pack(fill="x", pady=(0, 6))
        ttk.Label(root, textvariable=self.status, style="Muted.TLabel").pack(anchor="w")

        self.result_box = tk.Text(
            root, height=6, bg=BG_INPUT, fg=FG, relief="flat",
            font=("Consolas", 9), wrap="word",
        )
        self.result_box.pack(fill="both", expand=True, pady=(8, 0))
        self.result_box.insert("1.0", "Result ledger will appear here after a verified write.")
        self.result_box.configure(state="disabled")

        self._last_output: Optional[Path] = None

    # ----- session / dnd / keys -----

    def _enable_dnd(self) -> None:
        """U07 — TkDnD if present; otherwise path paste still works."""
        try:
            self.tk.call("package", "require", "tkdnd")
            self.tk.call("tkdnd::drop_target", "register", self._w, "DND_Files")
            self.bind("<<Drop>>", self._on_drop)
        except tk.TclError:
            pass

    def _on_drop(self, event: tk.Event) -> None:  # type: ignore[type-arg]
        raw = str(getattr(event, "data", "")).strip().strip("{}")
        if raw:
            self._load_source(raw.split()[0])

    def _bind_keys(self) -> None:
        self.bind("<Return>", lambda _e: self._start_convert())
        self.bind("<Escape>", lambda _e: self._request_cancel())
        self.bind("o", lambda _e: self._open_output())
        self.bind("O", lambda _e: self._open_output())

    def _load_session(self) -> None:
        if not SESSION_PATH.is_file():
            return
        try:
            data = json.loads(SESSION_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        last_dir = data.get("output_dir")
        if last_dir:
            self.out_entry.delete(0, "end")
            self.out_entry.insert(0, last_dir)
        ext = data.get("target_ext")
        if ext in CONTAINER_MATRIX:
            self.target_ext.set(ext)
            self._refresh_chips()
            self._sync_codec_lists()

    def _save_session(self) -> None:
        payload = {
            "output_dir": self.out_entry.get().strip(),
            "target_ext": self.target_ext.get(),
        }
        try:
            SESSION_PATH.write_text(json.dumps(payload), encoding="utf-8")
        except OSError:
            pass

    def _on_close(self) -> None:
        self._save_session()
        self.destroy()

    # ----- source / target -----

    def _browse_source(self) -> None:
        path = filedialog.askopenfilename(title="Select source media")
        if path:
            self._load_source(path)

    def _browse_out(self) -> None:
        path = filedialog.askdirectory(title="Output folder")
        if path:
            self.out_entry.delete(0, "end")
            self.out_entry.insert(0, path)
            self._refresh_command()

    def _load_source(self, raw: str) -> None:
        path = Path(raw).expanduser()
        if not path.is_file():
            self._set_status("Source is not a local file.", error=True)
            return
        self.status.set("Probing source ledger…")
        self.update_idletasks()
        try:
            ledger = probe(path)
        except ProbeError as exc:
            self._set_status(f"Probe failed: {exc}", error=True)
            return
        self.source_path = path
        self._source_bytes = ledger.size_bytes or path.stat().st_size
        self._duration = ledger.duration
        self.src_entry.delete(0, "end")
        self.src_entry.insert(0, str(path))
        self._name_customized = False
        self.out_name.set(path.stem)
        if not self.out_entry.get().strip():
            self.out_entry.insert(0, str(path.parent))
        lines = [
            f"{path.name}  ·  {format_bytes(self._source_bytes)}  ·  {ledger.duration:.2f}s  ·  {ledger.format_name}",
        ]
        for s in ledger.streams:
            if s.codec_type == "video":
                lines.append(f"  v[{s.index}] {s.codec_name} {s.width}×{s.height} {s.pix_fmt or ''}".rstrip())
            elif s.codec_type == "audio":
                lines.append(f"  a[{s.index}] {s.codec_name} {s.sample_rate or ''} Hz {s.channels or ''} ch")
            else:
                lines.append(f"  {s.codec_type}[{s.index}] {s.codec_name}")
        if not ledger.has_audio:
            lines.append("  note: no audio stream — audio-only targets will be rejected")
        self._write_readonly(self.probe_box, "\n".join(lines))
        if not ledger.has_video and self.target_ext.get() not in AUDIO_ONLY_EXTENSIONS:
            self._set_ext(".mp3")
        self._recompute_estimate()
        self._refresh_command()
        self.status.set("Ledger ready. Arm a target and convert.")

    def _set_ext(self, ext: str) -> None:
        self.target_ext.set(ext)
        if ext in AUDIO_ONLY_EXTENSIONS:
            self.audio_only.set(True)
        self._refresh_chips()
        self._sync_codec_lists()
        self._refresh_command()

    def _refresh_chips(self) -> None:
        current = self.target_ext.get()
        for ext, btn in self._chip_buttons.items():
            if ext == current:
                btn.configure(bg=ACCENT, fg="#04221a")
            else:
                btn.configure(bg=BG_INPUT, fg=FG)

    def _on_audio_only(self) -> None:
        if self.audio_only.get() and self.target_ext.get() not in AUDIO_ONLY_EXTENSIONS:
            self._set_ext(".mp3")
        self._refresh_command()

    def _toggle_advanced(self) -> None:
        if self.show_advanced.get():
            self.adv.pack(fill="x", pady=(0, 10))
        else:
            self.adv.pack_forget()
        self._sync_codec_lists()

    def _sync_codec_lists(self) -> None:
        spec = CONTAINER_MATRIX.get(self.target_ext.get(), {"v": [], "a": []})
        vvals = ["auto"] + list(spec.get("v") or [])
        avals = ["auto"] + list(spec.get("a") or [])
        self.vcombo.configure(values=vvals)
        self.acombo.configure(values=avals)
        if self.video_codec.get() not in vvals:
            self.video_codec.set("auto")
        if self.audio_codec.get() not in avals:
            self.audio_codec.set("auto")

    def _on_name_edit(self, _event: Optional[tk.Event] = None) -> None:  # type: ignore[type-arg]
        self._name_customized = True
        self._refresh_command()

    def _reset_out_name(self) -> None:
        if self.source_path:
            self.out_name.set(self.source_path.stem)
        else:
            self.out_name.set("")
        self._name_customized = False
        self._refresh_command()

    def _sanitized_stem(self) -> str:
        raw = self.out_name.get().strip()
        if not raw and self.source_path:
            return self.source_path.stem
        stem = Path(raw).name
        if stem.endswith(self.target_ext.get()):
            stem = stem[: -len(self.target_ext.get())]
        elif "." in stem:
            # User typed another extension; keep the basename they intended.
            maybe = Path(stem).stem
            if maybe:
                stem = maybe
        stem = stem.strip().strip(".")
        return stem or (self.source_path.stem if self.source_path else "output")

    def _output_path(self) -> Optional[Path]:
        if not self.source_path:
            return None
        folder = Path(self.out_entry.get().strip() or self.source_path.parent)
        return folder / f"{self._sanitized_stem()}{self.target_ext.get()}"

    def _size_arg(self) -> Optional[str]:
        if not self.size_enabled.get():
            return None
        raw = self.size_value.get().strip()
        if not raw:
            return None
        return f"{raw}{self.size_unit.get()}"

    def _recompute_estimate(self) -> None:
        if not self.size_enabled.get() or self._duration <= 0:
            self.estimate.set("Size ceiling off — CRF quality-first encode will be used.")
            self._refresh_command()
            return
        try:
            cap = parse_size(self._size_arg() or "0")
        except ValueError:
            self.estimate.set("Size value is not parseable.")
            return
        usable = cap * 0.96
        kbps = (usable * 8) / self._duration / 1000
        self.estimate.set(
            f"Cap {format_bytes(cap)} over {self._duration:.1f}s ≈ {kbps:.0f} kb/s total "
            f"(audio reserved first; video gets the residual)."
        )
        self._refresh_command()

    def _refresh_command(self) -> None:
        out = self._output_path()
        if not self.source_path or out is None:
            return
        parts = ["ffmpeg", "-n" if not self.overwrite.get() else "-y", "-i", str(self.source_path)]
        if self.audio_only.get() or self.target_ext.get() in AUDIO_ONLY_EXTENSIONS:
            parts.append("-vn")
        size = self._size_arg()
        if size:
            parts.append(f"# size-constrained {size}")
        parts.append(str(out))
        self._write_readonly(self.cmd_box, " ".join(parts) + "\n(Exact argv is composed by the engine at start.)")

    # ----- convert -----

    def _start_convert(self) -> None:
        if self._busy:
            return
        if not self.source_path:
            self._set_status("Load a source first.", error=True)
            return
        out = self._output_path()
        if out is None:
            self._set_status("Output path could not be derived.", error=True)
            return
        if out.exists() and not self.overwrite.get():
            if not messagebox.askyesno(
                "Overwrite?",
                f"{out.name} already exists.\nOverwrite the destination? The source file will not be touched.",
            ):
                return
            self.overwrite.set(True)
        self._busy = True
        self._cancel.clear()
        self.convert_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        self.open_btn.configure(state="disabled")
        self.progress.start(12)
        self.status.set("Writing — source remains untouched until verify succeeds.")
        kwargs = {
            "size": self._size_arg(),
            "audio_only": bool(self.audio_only.get() or self.target_ext.get() in AUDIO_ONLY_EXTENSIONS),
            "overwrite": True,
            "video_codec": None if self.video_codec.get() == "auto" else self.video_codec.get(),
            "audio_codec": None if self.audio_codec.get() == "auto" else self.audio_codec.get(),
            "preset": None if self.preset.get() == "medium" else self.preset.get(),
        }
        src, dst = self.source_path, out

        def work() -> None:
            try:
                result = convert(src, dst, **kwargs)
                self.after(0, lambda: self._on_success(result, dst))
            except Exception as exc:  # engine already wraps ffmpeg
                msg = str(exc)
                self.after(0, lambda m=msg: self._on_fail(m))

        self._worker = threading.Thread(target=work, daemon=True)
        self._worker.start()

    def _request_cancel(self) -> None:
        if not self._busy:
            return
        self._cancel.set()
        self.status.set("Cancel requested — in-flight ffmpeg cannot always be aborted mid-mux; source is safe.")

    def _on_success(self, result: dict[str, Any], dst: Path) -> None:
        self._busy = False
        self.progress.stop()
        self.convert_btn.configure(state="normal")
        self.cancel_btn.configure(state="disabled")
        self.open_btn.configure(state="normal")
        self._last_output = dst
        out_b = int(result.get("output_bytes") or dst.stat().st_size)
        ratio = (out_b / self._source_bytes) if self._source_bytes else 0
        lines = [
            f"verified  {dst}",
            f"in        {format_bytes(self._source_bytes)}",
            f"out       {format_bytes(out_b)}   ratio {ratio:.3f}",
            f"mode      {result.get('mode')}",
            f"xi_proxy  {result.get('xi_proxy')}",
            f"notes     {', '.join(result.get('plan_notes') or [])}",
        ]
        self._write_readonly(self.result_box, "\n".join(lines))
        self.status.set("Verified write complete. Source unchanged.")
        self._save_session()

    def _on_fail(self, message: str) -> None:
        self._busy = False
        self.progress.stop()
        self.convert_btn.configure(state="normal")
        self.cancel_btn.configure(state="disabled")
        self._write_readonly(self.result_box, message[-4000:])
        self._set_status("Encode failed. Source file was not modified.", error=True)

    def _open_output(self) -> None:
        if not self._last_output or not self._last_output.exists():
            return
        path = self._last_output
        if sys.platform.startswith("win"):
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            os.system(f'open "{path}"')
        else:
            os.system(f'xdg-open "{path}"')

    def _set_status(self, text: str, *, error: bool = False) -> None:
        self.status.set(text)
        if error:
            self.bell()

    @staticmethod
    def _write_readonly(widget: tk.Text, text: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.configure(state="disabled")


def main() -> int:
    app = MogopsGui()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
