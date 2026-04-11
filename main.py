import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import traceback
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk
from PIL import Image

# ─── Constants ────────────────────────────────────────────────────────────────

APP_TITLE = "Video Compressor & HLS Generator"
APP_WIDTH = 900
APP_HEIGHT = 820

RESOLUTIONS = {
    "1080p": {"width": 1920, "height": 1080, "bitrate": "5000k", "audio": "192k"},
    "720p":  {"width": 1280, "height": 720,  "bitrate": "2800k", "audio": "128k"},
    "480p":  {"width": 854,  "height": 480,  "bitrate": "1400k", "audio": "128k"},
    "360p":  {"width": 640,  "height": 360,  "bitrate": "800k",  "audio": "96k"},
}

MASTER_BANDWIDTHS = {
    "1080p": 5000000,
    "720p":  2800000,
    "480p":  1400000,
    "360p":  800000,
}

VIDEO_EXTENSIONS = (
    ("Video files", "*.mp4 *.mkv *.mov *.avi *.webm *.flv *.wmv *.m4v"),
    ("All files", "*.*"),
)

VIDEO_SUFFIXES = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".flv", ".wmv", ".m4v"}

# ─── Hardware Encoder Detection ───────────────────────────────────────────────

def _detect_encoder() -> str:
    try:
        out = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True, text=True
        ).stdout
        if "h264_videotoolbox" in out:
            return "videotoolbox"
        if "h264_nvenc" in out:
            return "nvenc"
    except FileNotFoundError:
        pass
    return "cpu"


def _encoder_display(enc: str) -> str:
    return {
        "videotoolbox": "VideoToolbox (Mac HW)",
        "nvenc":        "NVENC (GPU)",
        "cpu":          "libx264 (CPU)",
    }.get(enc, enc)


def _double_bitrate(bitrate: str) -> str:
    """"5000k" → "10000k" (used for -bufsize)."""
    return str(int(bitrate.rstrip("k")) * 2) + "k"


def _video_flags(cfg: dict, encoder: str) -> list[str]:
    """Return FFmpeg video codec flags for the given encoder and resolution config."""
    if encoder == "videotoolbox":
        return [
            "-c:v", "h264_videotoolbox",
            "-b:v", cfg["bitrate"], "-maxrate", cfg["bitrate"],
            "-profile:v", "main",
        ]
    if encoder == "nvenc":
        return [
            "-c:v", "h264_nvenc", "-preset", "p2", "-tune", "ll",
            "-rc", "vbr", "-b:v", cfg["bitrate"], "-maxrate", cfg["bitrate"],
            "-profile:v", "main",
        ]
    # libx264: quality-based with a bitrate ceiling to control file size
    return [
        "-c:v", "libx264", "-crf", "23", "-preset", "medium",
        "-profile:v", "main",
        "-maxrate", cfg["bitrate"], "-bufsize", _double_bitrate(cfg["bitrate"]),
    ]


ENCODER = _detect_encoder()

# ─── App ──────────────────────────────────────────────────────────────────────

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


class VideoCompressorApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry(f"{APP_WIDTH}x{APP_HEIGHT}")
        self.resizable(True, True)
        self.minsize(700, 640)

        self.video_path: str = ""
        self.output_dir: str = ""
        self._processing = False
        self._video_duration: float = 0.0
        self._drive_tmp: str = ""
        # Each entry: ({"id": str, "name": str}, BooleanVar)
        self._drive_file_vars: list[tuple[dict, ctk.BooleanVar]] = []

        self._build_ui()

    # ── UI Construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(8, weight=1)  # log row expands

        # ── Source toggle ─────────────────────────────────────────────────────
        src_frame = ctk.CTkFrame(self)
        src_frame.grid(row=0, column=0, padx=16, pady=(16, 4), sticky="ew")

        ctk.CTkLabel(src_frame, text="Source:", width=100, anchor="w").grid(
            row=0, column=0, padx=(12, 6), pady=8
        )
        self._source_var = ctk.StringVar(value="local")
        ctk.CTkRadioButton(
            src_frame, text="Local File", variable=self._source_var,
            value="local", command=self._on_source_toggle,
        ).grid(row=0, column=1, padx=12, pady=8)
        ctk.CTkRadioButton(
            src_frame, text="Google Drive Folder", variable=self._source_var,
            value="drive", command=self._on_source_toggle,
        ).grid(row=0, column=2, padx=12, pady=8)

        # ── Local file row ────────────────────────────────────────────────────
        self._local_frame = ctk.CTkFrame(self)
        self._local_frame.grid(row=1, column=0, padx=16, pady=4, sticky="ew")
        self._local_frame.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(self._local_frame, text="Video File:", width=100, anchor="w").grid(
            row=0, column=0, padx=(12, 6), pady=10
        )
        self.video_label = ctk.CTkLabel(
            self._local_frame, text="No file selected", anchor="w",
            text_color="gray60", wraplength=560,
        )
        self.video_label.grid(row=0, column=1, padx=6, pady=10, sticky="ew")
        ctk.CTkButton(
            self._local_frame, text="Browse", width=90, command=self.select_video,
        ).grid(row=0, column=2, padx=(6, 12), pady=10)

        # ── Drive input row (hidden initially) ───────────────────────────────
        self._drive_frame = ctk.CTkFrame(self)
        self._drive_frame.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(self._drive_frame, text="Drive URL:", width=100, anchor="w").grid(
            row=0, column=0, padx=(12, 6), pady=10
        )
        self.drive_url_entry = ctk.CTkEntry(
            self._drive_frame,
            placeholder_text="https://drive.google.com/drive/folders/...",
        )
        self.drive_url_entry.grid(row=0, column=1, padx=6, pady=10, sticky="ew")
        self.fetch_btn = ctk.CTkButton(
            self._drive_frame, text="Fetch Files", width=110,
            command=self._fetch_drive_files_threaded,
        )
        self.fetch_btn.grid(row=0, column=2, padx=(6, 12), pady=10)

        # ── Drive file list (hidden until fetch) ──────────────────────────────
        self._drive_list_frame = ctk.CTkFrame(self)
        self._drive_list_frame.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            self._drive_list_frame, text="Select videos to process:", anchor="w",
        ).grid(row=0, column=0, padx=12, pady=(8, 2), sticky="w")

        self._drive_scroll = ctk.CTkScrollableFrame(self._drive_list_frame, height=110)
        self._drive_scroll.grid(row=1, column=0, padx=12, pady=(0, 8), sticky="ew")
        self._drive_scroll.grid_columnconfigure(0, weight=1)

        # ── Resolution checkboxes + encoder label ─────────────────────────────
        res_frame = ctk.CTkFrame(self)
        res_frame.grid(row=3, column=0, padx=16, pady=4, sticky="ew")

        ctk.CTkLabel(res_frame, text="Resolutions:", anchor="w").grid(
            row=0, column=0, padx=(12, 16), pady=10
        )
        self.res_vars: dict[str, ctk.BooleanVar] = {}
        for i, res in enumerate(["1080p", "720p", "480p", "360p"]):
            var = ctk.BooleanVar(value=(res in ("1080p", "720p")))
            self.res_vars[res] = var
            ctk.CTkCheckBox(res_frame, text=res, variable=var).grid(
                row=0, column=i + 1, padx=12, pady=10
            )
        ctk.CTkLabel(
            res_frame,
            text=f"Encoder: {_encoder_display(ENCODER)}",
            text_color="#3a9bd5" if ENCODER != "cpu" else "gray60",
            anchor="w",
        ).grid(row=0, column=6, padx=(24, 12), pady=10, sticky="w")

        # ── CPU Threads slider (only shown for CPU encoder) ───────────────────
        self._cpu_frame = ctk.CTkFrame(self)
        self._cpu_frame.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(self._cpu_frame, text="CPU Threads:", anchor="w", width=100).grid(
            row=0, column=0, padx=(12, 6), pady=10
        )
        self.cpu_threads_var = ctk.IntVar(value=2)
        self.cpu_slider = ctk.CTkSlider(
            self._cpu_frame, from_=1, to=os.cpu_count() or 8,
            number_of_steps=(os.cpu_count() or 8) - 1,
            variable=self.cpu_threads_var,
            command=lambda v: self.cpu_threads_label.configure(
                text=f"{int(v)} / {os.cpu_count() or 8}"
            ),
        )
        self.cpu_slider.grid(row=0, column=1, padx=6, pady=10, sticky="ew")
        self.cpu_threads_label = ctk.CTkLabel(
            self._cpu_frame, text=f"2 / {os.cpu_count() or 8}", width=60, anchor="w"
        )
        self.cpu_threads_label.grid(row=0, column=2, padx=(4, 6))
        ctk.CTkLabel(
            self._cpu_frame, text="(fewer = cooler)", text_color="gray60", anchor="w"
        ).grid(row=0, column=3, padx=(0, 12))

        if ENCODER == "cpu":
            self._cpu_frame.grid(row=4, column=0, padx=16, pady=4, sticky="ew")

        # ── Output folder row ─────────────────────────────────────────────────
        out_frame = ctk.CTkFrame(self)
        out_frame.grid(row=5, column=0, padx=16, pady=4, sticky="ew")
        out_frame.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(out_frame, text="Output Folder:", width=100, anchor="w").grid(
            row=0, column=0, padx=(12, 6), pady=10
        )
        self.output_label = ctk.CTkLabel(
            out_frame, text="No folder selected", anchor="w",
            text_color="gray60", wraplength=560,
        )
        self.output_label.grid(row=0, column=1, padx=6, pady=10, sticky="ew")
        ctk.CTkButton(
            out_frame, text="Browse", width=90, command=self.select_output,
        ).grid(row=0, column=2, padx=(6, 12), pady=10)

        # ── Start button + status ─────────────────────────────────────────────
        ctrl_frame = ctk.CTkFrame(self, fg_color="transparent")
        ctrl_frame.grid(row=6, column=0, padx=16, pady=4, sticky="ew")
        ctrl_frame.grid_columnconfigure(1, weight=1)

        self.start_btn = ctk.CTkButton(
            ctrl_frame, text="Start Processing", width=160,
            state="disabled", command=self.process_video,
            fg_color="#1f6aa5", hover_color="#144e7a",
        )
        self.start_btn.grid(row=0, column=0, padx=(0, 16), pady=4)

        self.status_label = ctk.CTkLabel(
            ctrl_frame, text="Idle", anchor="w",
            font=ctk.CTkFont(size=14, weight="bold"), text_color="gray70",
        )
        self.status_label.grid(row=0, column=1, pady=4, sticky="w")

        # ── Progress bar ──────────────────────────────────────────────────────
        prog_frame = ctk.CTkFrame(self, fg_color="transparent")
        prog_frame.grid(row=7, column=0, padx=16, pady=(0, 4), sticky="ew")
        prog_frame.grid_columnconfigure(0, weight=1)

        self.progress_bar = ctk.CTkProgressBar(prog_frame, height=18)
        self.progress_bar.grid(row=0, column=0, sticky="ew", padx=(0, 10))
        self.progress_bar.set(0)

        self.progress_label = ctk.CTkLabel(prog_frame, text="0%", width=44, anchor="e")
        self.progress_label.grid(row=0, column=1)

        # ── Bottom: log + thumbnail ───────────────────────────────────────────
        bottom_frame = ctk.CTkFrame(self, fg_color="transparent")
        bottom_frame.grid(row=8, column=0, padx=16, pady=(0, 16), sticky="nsew")
        bottom_frame.grid_columnconfigure(0, weight=1)
        bottom_frame.grid_rowconfigure(0, weight=1)

        self.log_box = ctk.CTkTextbox(
            bottom_frame, wrap="word", font=ctk.CTkFont(family="monospace", size=12)
        )
        self.log_box.grid(row=0, column=0, sticky="nsew", padx=(0, 10))

        thumb_frame = ctk.CTkFrame(bottom_frame, width=200)
        thumb_frame.grid(row=0, column=1, sticky="n")
        thumb_frame.grid_propagate(False)

        ctk.CTkLabel(thumb_frame, text="Thumbnail", font=ctk.CTkFont(weight="bold")).pack(pady=(10, 4))
        self.thumb_label = ctk.CTkLabel(thumb_frame, text="—", text_color="gray60")
        self.thumb_label.pack(padx=8, pady=8, fill="both", expand=True)

    # ── Source Toggle ─────────────────────────────────────────────────────────

    def _on_source_toggle(self):
        if self._source_var.get() == "local":
            self._drive_frame.grid_remove()
            self._drive_list_frame.grid_remove()
            self._local_frame.grid(row=1, column=0, padx=16, pady=4, sticky="ew")
        else:
            self._local_frame.grid_remove()
            self._drive_frame.grid(row=1, column=0, padx=16, pady=4, sticky="ew")
            if self._drive_file_vars:
                self._drive_list_frame.grid(row=2, column=0, padx=16, pady=4, sticky="ew")
        self._refresh_start_button()

    # ── File Selection ────────────────────────────────────────────────────────

    def select_video(self):
        path = filedialog.askopenfilename(
            title="Select Video File", filetypes=VIDEO_EXTENSIONS
        )
        if path:
            self.video_path = path
            self.video_label.configure(text=path, text_color="white")
            self._refresh_start_button()

    def select_output(self):
        path = filedialog.askdirectory(title="Select Output Folder")
        if path:
            self.output_dir = path
            self.output_label.configure(text=path, text_color="white")
            self._refresh_start_button()

    def _refresh_start_button(self):
        if self._source_var.get() == "local":
            ready = bool(self.video_path and self.output_dir and not self._processing)
        else:
            any_selected = any(v.get() for _, v in self._drive_file_vars)
            ready = bool(any_selected and self.output_dir and not self._processing)
        self.start_btn.configure(state="normal" if ready else "disabled")

    # ── Google Drive ──────────────────────────────────────────────────────────

    def _fetch_drive_files_threaded(self):
        url = self.drive_url_entry.get().strip()
        if not url:
            messagebox.showwarning("No URL", "Paste a Google Drive folder URL first.")
            return
        self.fetch_btn.configure(state="disabled", text="Listing…")
        self._set_status("Listing Drive folder…", "#3a9bd5")
        threading.Thread(
            target=self._fetch_drive_files, args=(url,), daemon=True
        ).start()

    def _fetch_drive_files(self, url: str):
        try:
            import gdown
        except ImportError:
            self.after(0, lambda: messagebox.showerror(
                "Missing Package", "gdown is not installed.\nRun: pip install gdown"
            ))
            self.after(0, lambda: self.fetch_btn.configure(state="normal", text="Fetch Files"))
            self.after(0, lambda: self._set_status("Idle", "gray70"))
            return

        try:
            self._log(f"[Drive] Listing folder: {url}")
            # skip_download=True returns file metadata without downloading
            entries = gdown.download_folder(url, skip_download=True, quiet=True)
            if entries is None:
                raise RuntimeError("Could not retrieve folder. Make sure it is publicly shared.")

            files = [
                {"id": e.id, "name": Path(e.path).name}
                for e in entries
                if Path(e.path).suffix.lower() in VIDEO_SUFFIXES
            ]

            if not files:
                self.after(0, lambda: messagebox.showwarning(
                    "No Videos",
                    "No video files found in the Drive folder.\n"
                    "Make sure the folder is publicly shared.",
                ))
                return

            self._log(f"[Drive] Found {len(files)} video file(s).")
            self.after(0, self._render_drive_file_list, files)

        except Exception:
            err = traceback.format_exc()
            self._log(f"[Drive] Error:\n{err}")
            self.after(0, lambda: messagebox.showerror(
                "Drive Error",
                "Failed to list folder. Check the URL and that it is publicly shared.\nSee log for details.",
            ))
        finally:
            self.after(0, lambda: self.fetch_btn.configure(state="normal", text="Fetch Files"))
            self.after(0, lambda: self._set_status("Idle", "gray70"))

    def _render_drive_file_list(self, files: list[dict]):
        """Show checkboxes for each Drive video file (no download yet)."""
        for widget in self._drive_scroll.winfo_children():
            widget.destroy()
        self._drive_file_vars = []

        for meta in files:
            var = ctk.BooleanVar(value=True)
            ctk.CTkCheckBox(
                self._drive_scroll, text=meta["name"], variable=var,
                command=self._refresh_start_button,
            ).pack(anchor="w", padx=8, pady=2)
            self._drive_file_vars.append((meta, var))

        self._drive_list_frame.grid(row=2, column=0, padx=16, pady=4, sticky="ew")
        self._log("[Drive] Select the videos you want and click Start Processing.")
        self._refresh_start_button()

    def _download_drive_file(self, file_id: str, file_name: str, out_path: str):
        """Download a Drive file via gdown (handles redirects/confirmations) with
        progress polling based on the growing file size on disk."""
        import gdown
        import requests

        # Try a HEAD request to get total size for accurate progress bar
        expected_size = 0
        try:
            r = requests.head(
                f"https://drive.google.com/uc?id={file_id}&export=download",
                allow_redirects=True, timeout=10,
            )
            expected_size = int(r.headers.get("content-length", 0))
        except Exception:
            pass

        self._set_status(f"Downloading: {file_name}", "#3a9bd5")
        self._set_progress(0)

        result_box: list = [None]
        error_box:  list = [None]

        def _do_download():
            try:
                result_box[0] = gdown.download(id=file_id, output=out_path, quiet=True)
            except Exception as exc:
                error_box[0] = exc

        dl_thread = threading.Thread(target=_do_download, daemon=True)
        dl_thread.start()

        # Poll the file size on disk every 0.5 s for live progress
        while dl_thread.is_alive():
            if os.path.exists(out_path) and expected_size > 0:
                current = os.path.getsize(out_path)
                pct = min(current / expected_size * 100, 99)
                dl_mb    = current       / 1024 / 1024
                total_mb = expected_size / 1024 / 1024
                self._set_progress(pct)
                self._set_status(
                    f"Downloading {file_name}: {dl_mb:.1f} / {total_mb:.1f} MB",
                    "#3a9bd5",
                )
            time.sleep(0.5)

        dl_thread.join()

        if error_box[0]:
            raise error_box[0]
        if result_box[0] is None:
            raise RuntimeError(
                f"gdown failed to download '{file_name}'. "
                "Make sure the file is publicly shared."
            )

        size = os.path.getsize(out_path) if os.path.exists(out_path) else 0
        if size == 0:
            raise RuntimeError(
                f"'{file_name}' downloaded as 0 bytes — "
                "file may require sign-in or the share link is restricted."
            )

        self._log(f"[Drive] ✓ {file_name} ({size / 1024 / 1024:.1f} MB)")
        self._set_progress(100)

    def _clear_drive_list(self):
        for widget in self._drive_scroll.winfo_children():
            widget.destroy()
        self._drive_file_vars = []
        self._drive_list_frame.grid_remove()
        self._refresh_start_button()

    # ── Logging helpers ───────────────────────────────────────────────────────

    def _log(self, message: str):
        self.after(0, self._append_log, message)

    def _append_log(self, message: str):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", message + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _set_status(self, text: str, color: str = "gray70"):
        self.after(0, lambda: self.status_label.configure(text=text, text_color=color))

    def _set_progress(self, value: float):
        self.after(0, self._update_progress_ui, value)

    def _update_progress_ui(self, value: float):
        self.progress_bar.set(value / 100)
        self.progress_label.configure(text=f"{int(value)}%")

    # ── Thumbnail ─────────────────────────────────────────────────────────────

    def generate_thumbnail(self, video_path: str, output_dir: str) -> str:
        thumb_path = os.path.join(output_dir, "thumbnail.jpg")
        cmd = [
            "ffmpeg", "-y", "-ss", "1", "-i", video_path,
            "-vframes", "1", "-q:v", "2", thumb_path,
        ]
        self._log("[Thumbnail] Extracting frame…")
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0 or not os.path.exists(thumb_path):
            self._log(f"[Thumbnail] Warning: {result.stderr.strip()}")
            return ""
        self._log(f"[Thumbnail] Saved → {thumb_path}")
        self.after(0, self._display_thumbnail, thumb_path)
        return thumb_path

    def _display_thumbnail(self, path: str):
        try:
            img = Image.open(path)
            img.thumbnail((184, 130))
            ctk_img = ctk.CTkImage(light_image=img, dark_image=img, size=img.size)
            self.thumb_label.configure(image=ctk_img, text="")
            self.thumb_label.image = ctk_img
        except Exception as e:
            self._log(f"[Thumbnail] Display error: {e}")

    # ── HLS Generation — single-pass, all resolutions simultaneously ──────────

    def generate_hls(self, video_path: str, output_dir: str, selected_res: list[str]):
        n = len(selected_res)
        duration = self._video_duration or 1.0

        # Create per-resolution output dirs
        for res in selected_res:
            os.makedirs(os.path.join(output_dir, res), exist_ok=True)

        # filter_complex: decode once → split → scale each branch
        split_labels = "".join(f"[v{i}]" for i in range(n))
        filter_parts = [f"[0:v]split={n}{split_labels}"]
        for i, res in enumerate(selected_res):
            cfg = RESOLUTIONS[res]
            filter_parts.append(f"[v{i}]scale={cfg['width']}:{cfg['height']}[s{i}]")
        filter_complex = ";".join(filter_parts)

        # NVENC needs hwaccel flag; VideoToolbox and CPU do not
        hwaccel = ["-hwaccel", "cuda"] if ENCODER == "nvenc" else []

        base_cmd = [
            "ffmpeg", "-y",
            *hwaccel,
            "-i", video_path,
            "-filter_complex", filter_complex,
        ]
        if ENCODER == "cpu":
            base_cmd += ["-threads", str(int(self.cpu_threads_var.get()))]

        # One output block per resolution
        per_output: list[str] = []
        for i, res in enumerate(selected_res):
            cfg = RESOLUTIONS[res]
            segment_pat = os.path.join(output_dir, res, "%03d.ts")
            playlist = os.path.join(output_dir, res, "playlist.m3u8")
            per_output += [
                "-map", f"[s{i}]", "-map", "0:a?",
                *_video_flags(cfg, ENCODER),
                "-c:a", "aac", "-b:a", cfg["audio"],
                "-hls_time", "6", "-hls_playlist_type", "vod",
                "-hls_segment_filename", segment_pat,
                playlist,
            ]

        cmd = base_cmd + per_output
        self._log(f"\n[HLS] Single-pass encode → {', '.join(selected_res)}")
        self._log(f"      Encoder: {_encoder_display(ENCODER)}")
        self._log("  " + " ".join(cmd))

        process = subprocess.Popen(
            cmd, stderr=subprocess.PIPE, stdout=subprocess.DEVNULL,
            text=True, bufsize=1,
        )

        for line in process.stderr:
            line = line.rstrip()
            if line:
                self._log(f"  {line}")
            m = re.search(r"time=(\d+):(\d+):([\d.]+)", line)
            if m:
                elapsed = (
                    int(m.group(1)) * 3600
                    + int(m.group(2)) * 60
                    + float(m.group(3))
                )
                pct = min(elapsed / duration * 100, 99.0)
                self._set_progress(pct)

        process.wait()
        if process.returncode != 0:
            raise RuntimeError(f"FFmpeg failed (exit {process.returncode})")

        for res in selected_res:
            self._log(f"[HLS] {res} → {os.path.join(output_dir, res, 'playlist.m3u8')}")

        # Write master playlist
        master_path = os.path.join(output_dir, "master.m3u8")
        with open(master_path, "w") as f:
            f.write("#EXTM3U\n#EXT-X-VERSION:3\n")
            for res in selected_res:
                cfg = RESOLUTIONS[res]
                bw = MASTER_BANDWIDTHS[res]
                f.write(
                    f"#EXT-X-STREAM-INF:BANDWIDTH={bw},"
                    f"RESOLUTION={cfg['width']}x{cfg['height']}\n"
                )
                f.write(f"{res}/playlist.m3u8\n")
        self._log(f"[Master] Written → {master_path}")

    # ── Video Duration ────────────────────────────────────────────────────────

    def _probe_duration(self, video_path: str) -> float:
        cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            video_path,
        ]
        try:
            out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, text=True)
            return float(out.strip())
        except Exception:
            return 0.0

    # ── Main Processing Orchestrator ──────────────────────────────────────────

    def process_video(self):
        selected_res = [r for r, v in self.res_vars.items() if v.get()]
        if not selected_res:
            messagebox.showwarning("No Resolution", "Select at least one resolution.")
            return

        if self._source_var.get() == "local":
            video_paths = [self.video_path]
            drive_metas = []
        else:
            drive_metas = [m for m, v in self._drive_file_vars if v.get()]
            if not drive_metas:
                messagebox.showwarning("No Files", "Select at least one video file.")
                return
            video_paths = []  # will be filled after download in _run_processing

        self._processing = True
        self.start_btn.configure(state="disabled")
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")
        self._set_progress(0)
        self._set_status("Processing…", "#3a9bd5")

        threading.Thread(
            target=self._run_processing,
            args=(video_paths, drive_metas, self.output_dir, selected_res),
            daemon=True,
        ).start()

    def _run_processing(
        self,
        video_paths: list[str],
        drive_metas: list[dict],
        output_dir: str,
        selected_res: list[str],
    ):
        # ── Download Drive files first (if any) ───────────────────────────────
        if drive_metas:
            tmp = tempfile.mkdtemp(prefix="vc_drive_")
            self._drive_tmp = tmp
            self._log(f"[Drive] Downloading {len(drive_metas)} file(s) to: {tmp}\n")

            for idx, meta in enumerate(drive_metas):
                self._log(f"[Drive] ↓ [{idx+1}/{len(drive_metas)}] {meta['name']}")
                out_path = os.path.join(tmp, meta["name"])
                try:
                    self._download_drive_file(meta["id"], meta["name"], out_path)
                    video_paths.append(out_path)
                except Exception:
                    self._log(f"[Drive] ✗ Failed: {meta['name']}\n{traceback.format_exc()}")

            if not video_paths:
                self._log("[Drive] No files downloaded successfully.")
                self._set_status("Error", "#e74c3c")
                self._processing = False
                shutil.rmtree(tmp, ignore_errors=True)
                self._drive_tmp = ""
                self.after(0, self._refresh_start_button)
                return

            self._log(f"\n[Drive] Download complete. Starting compression…\n")
            self._set_status("Processing…", "#3a9bd5")

        total = len(video_paths)
        try:
            for idx, video_path in enumerate(video_paths):
                stem = Path(video_path).stem
                job_dir = os.path.join(output_dir, stem)
                os.makedirs(job_dir, exist_ok=True)

                self._log(f"\n=== [{idx + 1}/{total}] {os.path.basename(video_path)} ===")
                self._log(f"Output: {job_dir}")
                self._log(f"Resolutions: {', '.join(selected_res)}\n")

                self._video_duration = self._probe_duration(video_path)
                if self._video_duration:
                    self._log(f"Duration: {self._video_duration:.1f}s\n")

                self.generate_thumbnail(video_path, job_dir)
                self.generate_hls(video_path, job_dir, selected_res)

            self._set_progress(100)
            self._set_status("Completed", "#2ecc71")
            self._log(f"\n=== Done! ({total} video(s)) ===")
            self.after(0, lambda: messagebox.showinfo(
                "Done", f"HLS output saved to:\n{output_dir}"
            ))

        except Exception:
            err = traceback.format_exc()
            self._log(f"\n[ERROR]\n{err}")
            self._set_status("Error", "#e74c3c")
            self.after(0, lambda: messagebox.showerror(
                "Error", "Processing failed. See log for details."
            ))

        finally:
            self._processing = False
            # Clean up Drive temp files after processing
            if self._drive_tmp:
                shutil.rmtree(self._drive_tmp, ignore_errors=True)
                self._drive_tmp = ""
                self.after(0, self._clear_drive_list)
            self.after(0, self._refresh_start_button)


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = VideoCompressorApp()
    app.mainloop()
