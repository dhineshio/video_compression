import os
import re
import subprocess
import threading
import traceback
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk
from PIL import Image

# ─── Constants ────────────────────────────────────────────────────────────────

APP_TITLE = "Video Compressor & HLS Generator"
APP_WIDTH = 900
APP_HEIGHT = 780

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

# ─── App ──────────────────────────────────────────────────────────────────────

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


class VideoCompressorApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry(f"{APP_WIDTH}x{APP_HEIGHT}")
        self.resizable(True, True)
        self.minsize(700, 600)

        self.video_path: str = ""
        self.output_dir: str = ""
        self._processing = False
        self._video_duration: float = 0.0

        self._build_ui()

    # ── UI Construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(6, weight=1)

        # ── Video file row ────────────────────────────────────────────────────
        file_frame = ctk.CTkFrame(self)
        file_frame.grid(row=0, column=0, padx=16, pady=(16, 6), sticky="ew")
        file_frame.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(file_frame, text="Video File:", width=100, anchor="w").grid(
            row=0, column=0, padx=(12, 6), pady=10
        )
        self.video_label = ctk.CTkLabel(
            file_frame, text="No file selected", anchor="w",
            text_color="gray60", wraplength=580
        )
        self.video_label.grid(row=0, column=1, padx=6, pady=10, sticky="ew")
        ctk.CTkButton(
            file_frame, text="Browse", width=90, command=self.select_video
        ).grid(row=0, column=2, padx=(6, 12), pady=10)

        # ── Resolution checkboxes ─────────────────────────────────────────────
        res_frame = ctk.CTkFrame(self)
        res_frame.grid(row=1, column=0, padx=16, pady=6, sticky="ew")

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

        # ── CPU Threads slider ────────────────────────────────────────────────
        cpu_frame = ctk.CTkFrame(self)
        cpu_frame.grid(row=2, column=0, padx=16, pady=6, sticky="ew")

        ctk.CTkLabel(cpu_frame, text="CPU Threads:", anchor="w", width=100).grid(
            row=0, column=0, padx=(12, 6), pady=10
        )
        self.cpu_threads_var = ctk.IntVar(value=2)
        self.cpu_slider = ctk.CTkSlider(
            cpu_frame, from_=1, to=os.cpu_count() or 8,
            number_of_steps=(os.cpu_count() or 8) - 1,
            variable=self.cpu_threads_var,
            command=lambda v: self.cpu_threads_label.configure(
                text=f"{int(v)} / {os.cpu_count() or 8}"
            ),
        )
        self.cpu_slider.grid(row=0, column=1, padx=6, pady=10, sticky="ew")
        cpu_frame.grid_columnconfigure(1, weight=1)
        self.cpu_threads_label = ctk.CTkLabel(
            cpu_frame, text=f"2 / {os.cpu_count() or 8}", width=60, anchor="w"
        )
        self.cpu_threads_label.grid(row=0, column=2, padx=(4, 6))
        ctk.CTkLabel(
            cpu_frame, text="(fewer = cooler)", text_color="gray60", anchor="w"
        ).grid(row=0, column=3, padx=(0, 12))

        # ── Output folder row ─────────────────────────────────────────────────
        out_frame = ctk.CTkFrame(self)
        out_frame.grid(row=3, column=0, padx=16, pady=6, sticky="ew")
        out_frame.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(out_frame, text="Output Folder:", width=100, anchor="w").grid(
            row=0, column=0, padx=(12, 6), pady=10
        )
        self.output_label = ctk.CTkLabel(
            out_frame, text="No folder selected", anchor="w",
            text_color="gray60", wraplength=560
        )
        self.output_label.grid(row=0, column=1, padx=6, pady=10, sticky="ew")
        ctk.CTkButton(
            out_frame, text="Browse", width=90, command=self.select_output
        ).grid(row=0, column=2, padx=(6, 12), pady=10)

        # ── Start button + status ─────────────────────────────────────────────
        ctrl_frame = ctk.CTkFrame(self, fg_color="transparent")
        ctrl_frame.grid(row=4, column=0, padx=16, pady=6, sticky="ew")
        ctrl_frame.grid_columnconfigure(1, weight=1)

        self.start_btn = ctk.CTkButton(
            ctrl_frame, text="Start Processing", width=160,
            state="disabled", command=self.process_video,
            fg_color="#1f6aa5", hover_color="#144e7a"
        )
        self.start_btn.grid(row=0, column=0, padx=(0, 16), pady=4)

        self.status_label = ctk.CTkLabel(
            ctrl_frame, text="Idle", anchor="w",
            font=ctk.CTkFont(size=14, weight="bold"), text_color="gray70"
        )
        self.status_label.grid(row=0, column=1, pady=4, sticky="w")

        # ── Progress bar ──────────────────────────────────────────────────────
        prog_frame = ctk.CTkFrame(self, fg_color="transparent")
        prog_frame.grid(row=5, column=0, padx=16, pady=(0, 6), sticky="ew")
        prog_frame.grid_columnconfigure(0, weight=1)

        self.progress_bar = ctk.CTkProgressBar(prog_frame, height=18)
        self.progress_bar.grid(row=0, column=0, sticky="ew", padx=(0, 10))
        self.progress_bar.set(0)

        self.progress_label = ctk.CTkLabel(prog_frame, text="0%", width=44, anchor="e")
        self.progress_label.grid(row=0, column=1)

        # ── Bottom: log + thumbnail ───────────────────────────────────────────
        bottom_frame = ctk.CTkFrame(self, fg_color="transparent")
        bottom_frame.grid(row=6, column=0, padx=16, pady=(0, 16), sticky="nsew")
        bottom_frame.grid_columnconfigure(0, weight=1)
        bottom_frame.grid_rowconfigure(0, weight=1)

        self.log_box = ctk.CTkTextbox(bottom_frame, wrap="word", font=ctk.CTkFont(family="monospace", size=12))
        self.log_box.grid(row=0, column=0, sticky="nsew", padx=(0, 10))

        thumb_frame = ctk.CTkFrame(bottom_frame, width=200)
        thumb_frame.grid(row=0, column=1, sticky="n")
        thumb_frame.grid_propagate(False)

        ctk.CTkLabel(thumb_frame, text="Thumbnail", font=ctk.CTkFont(weight="bold")).pack(pady=(10, 4))
        self.thumb_label = ctk.CTkLabel(thumb_frame, text="—", text_color="gray60")
        self.thumb_label.pack(padx=8, pady=8, fill="both", expand=True)

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
        if self.video_path and self.output_dir and not self._processing:
            self.start_btn.configure(state="normal")
        else:
            self.start_btn.configure(state="disabled")

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
            "-vframes", "1", "-q:v", "2", thumb_path
        ]
        self._log(f"[Thumbnail] Extracting frame...")
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
            self.thumb_label.image = ctk_img  # prevent GC
        except Exception as e:
            self._log(f"[Thumbnail] Display error: {e}")

    # ── HLS Generation ────────────────────────────────────────────────────────

    def generate_hls(
        self,
        video_path: str,
        output_dir: str,
        selected_res: list[str],
    ):
        total = len(selected_res)
        threads = str(int(self.cpu_threads_var.get()))
        for i, res in enumerate(selected_res):
            cfg = RESOLUTIONS[res]
            res_dir = os.path.join(output_dir, res)
            os.makedirs(res_dir, exist_ok=True)

            playlist = os.path.join(res_dir, "playlist.m3u8")
            segment_pattern = os.path.join(res_dir, "%03d.ts")

            cmd = [
                "ffmpeg", "-y", "-i", video_path,
                "-threads", threads,
                "-c:v", "libx264",
                "-crf", "23",
                "-preset", "medium",
                "-profile:v", "main",
                "-c:a", "aac",
                "-b:a", cfg["audio"],
                "-vf", f"scale={cfg['width']}:{cfg['height']}",
                "-hls_time", "6",
                "-hls_playlist_type", "vod",
                "-hls_segment_filename", segment_pattern,
                playlist,
            ]

            self._log(f"\n[HLS] Encoding {res}  ({i+1}/{total})")
            self._log("  " + " ".join(cmd))

            # Run FFmpeg and stream stderr for progress
            process = subprocess.Popen(
                cmd, stderr=subprocess.PIPE, stdout=subprocess.DEVNULL,
                text=True, bufsize=1
            )

            stream_duration = self._video_duration or 1.0
            res_base = i / total  # fraction already done

            for line in process.stderr:
                line = line.rstrip()
                if line:
                    self._log(f"  {line}")

                # Parse elapsed time to compute per-resolution progress
                m = re.search(r"time=(\d+):(\d+):([\d.]+)", line)
                if m:
                    elapsed = (
                        int(m.group(1)) * 3600
                        + int(m.group(2)) * 60
                        + float(m.group(3))
                    )
                    res_frac = min(elapsed / stream_duration, 1.0)
                    overall = (res_base + res_frac / total) * 100
                    self._set_progress(overall)

            process.wait()
            if process.returncode != 0:
                raise RuntimeError(f"FFmpeg failed for {res} (exit {process.returncode})")

            self._set_progress((i + 1) / total * 100)
            self._log(f"[HLS] {res} complete → {playlist}")

        # Write master playlist
        master_path = os.path.join(output_dir, "master.m3u8")
        with open(master_path, "w") as f:
            f.write("#EXTM3U\n")
            f.write("#EXT-X-VERSION:3\n")
            for res in selected_res:
                cfg = RESOLUTIONS[res]
                bw = MASTER_BANDWIDTHS[res]
                f.write(
                    f'#EXT-X-STREAM-INF:BANDWIDTH={bw},'
                    f'RESOLUTION={cfg["width"]}x{cfg["height"]}\n'
                )
                f.write(f"{res}/playlist.m3u8\n")
        self._log(f"\n[Master] Written → {master_path}")

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

        self._processing = True
        self.start_btn.configure(state="disabled")
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")
        self._set_progress(0)
        self._set_status("Processing…", "#3a9bd5")

        thread = threading.Thread(
            target=self._run_processing,
            args=(self.video_path, self.output_dir, selected_res),
            daemon=True,
        )
        thread.start()

    def _run_processing(self, video_path: str, output_dir: str, selected_res: list[str]):
        try:
            stem = Path(video_path).stem
            job_dir = os.path.join(output_dir, stem)
            os.makedirs(job_dir, exist_ok=True)

            self._log(f"=== Starting: {os.path.basename(video_path)} ===")
            self._log(f"Output: {job_dir}")
            self._log(f"Resolutions: {', '.join(selected_res)}\n")

            # Probe duration for accurate progress
            self._video_duration = self._probe_duration(video_path)
            if self._video_duration:
                self._log(f"Duration: {self._video_duration:.1f}s\n")

            # Step 1: Thumbnail
            self.generate_thumbnail(video_path, job_dir)

            # Step 2: HLS streams
            self.generate_hls(video_path, job_dir, selected_res)

            self._set_progress(100)
            self._set_status("Completed", "#2ecc71")
            self._log("\n=== Done! ===")
            self.after(0, lambda: messagebox.showinfo(
                "Done", f"HLS output saved to:\n{job_dir}"
            ))

        except Exception:
            err = traceback.format_exc()
            self._log(f"\n[ERROR]\n{err}")
            self._set_status("Error", "#e74c3c")
            self.after(0, lambda: messagebox.showerror("Error", "Processing failed. See log for details."))

        finally:
            self._processing = False
            self.after(0, self._refresh_start_button)


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = VideoCompressorApp()
    app.mainloop()
