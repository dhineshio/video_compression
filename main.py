import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import traceback
import webbrowser
import zipfile
from datetime import datetime, timezone
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

CLOUD_CONFIG_DIR = Path.home() / ".video_compressor"
DRIVE_ROOT_FOLDER_NAME = "VideoCompressor"
POLL_INTERVAL_SECONDS = 20

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

# ─── Colab Cloud Manager ──────────────────────────────────────────────────────

class ColabCloudManager:
    """Handles Google Drive OAuth2, folder setup, upload, polling, and download."""

    SCOPES = ["https://www.googleapis.com/auth/drive"]

    def __init__(self, log_fn, status_fn, progress_fn):
        self._log = log_fn
        self._set_status = status_fn
        self._set_progress = progress_fn
        self.service = None
        self.folder_ids: dict = {}

    def authenticate(self) -> bool:
        try:
            from google.oauth2.credentials import Credentials
            from google_auth_oauthlib.flow import InstalledAppFlow
            from google.auth.transport.requests import Request
            from googleapiclient.discovery import build
        except ImportError:
            self._log("[Cloud] Missing packages. Run: pip install google-auth-oauthlib google-api-python-client")
            return False

        token_path = CLOUD_CONFIG_DIR / "token.json"
        creds_path = CLOUD_CONFIG_DIR / "credentials.json"

        creds = None
        if token_path.exists():
            creds = Credentials.from_authorized_user_file(str(token_path), self.SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                except Exception:
                    creds = None

            if not creds:
                if not creds_path.exists():
                    self._log(
                        "[Cloud] credentials.json not found.\n"
                        f"        Place your Google OAuth credentials at:\n"
                        f"        {creds_path}\n"
                        "        (Google Cloud Console → APIs & Services → Credentials\n"
                        "         → Create OAuth 2.0 Client ID → Desktop App → Download JSON)"
                    )
                    return False
                flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), self.SCOPES)
                creds = flow.run_local_server(port=0)

            CLOUD_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            token_path.write_text(creds.to_json())

        self.service = build("drive", "v3", credentials=creds, cache_discovery=False)
        self._log("[Cloud] Google Drive authenticated.")
        return True

    def _find_or_create_folder(self, name: str, parent_id: str | None = None) -> str:
        q = f"name='{name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
        if parent_id:
            q += f" and '{parent_id}' in parents"
        results = self.service.files().list(q=q, fields="files(id)").execute()
        files = results.get("files", [])
        if files:
            return files[0]["id"]
        meta = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
        if parent_id:
            meta["parents"] = [parent_id]
        f = self.service.files().create(body=meta, fields="id").execute()
        return f["id"]

    def ensure_folder_structure(self) -> dict:
        root_id   = self._find_or_create_folder(DRIVE_ROOT_FOLDER_NAME)
        input_id  = self._find_or_create_folder("input",  root_id)
        config_id = self._find_or_create_folder("config", root_id)
        output_id = self._find_or_create_folder("output", root_id)
        self.folder_ids = {
            "root":   root_id,
            "input":  input_id,
            "config": config_id,
            "output": output_id,
        }
        self._log("[Cloud] Drive folder structure ready.")
        return self.folder_ids

    def sync_notebook_to_drive(self, local_notebook_path: str) -> str:
        """Upload colab_hls.ipynb to VideoCompressor/ in Drive and return its file ID.
        Always overwrites so the notebook stays up-to-date with local edits."""
        from googleapiclient.http import MediaFileUpload

        root_id = self.folder_ids["root"]
        nb_name = Path(local_notebook_path).name

        # Delete any existing copy first
        self._delete_drive_file_by_name(nb_name, root_id)

        media = MediaFileUpload(local_notebook_path, mimetype="application/json", resumable=False)
        f = self.service.files().create(
            body={"name": nb_name, "parents": [root_id]},
            media_body=media, fields="id",
        ).execute()
        file_id = f["id"]
        self._log(f"[Cloud] Notebook synced to Drive (id: {file_id})")
        return file_id

    def upload_file(self, local_path: str, drive_folder_id: str,
                    mime: str = "application/octet-stream",
                    progress_cb=None) -> str:
        from googleapiclient.http import MediaFileUpload

        media = MediaFileUpload(
            local_path, mimetype=mime, resumable=True, chunksize=4 * 1024 * 1024
        )
        request = self.service.files().create(
            body={"name": Path(local_path).name, "parents": [drive_folder_id]},
            media_body=media, fields="id",
        )
        response = None
        while response is None:
            status, response = request.next_chunk()
            if status and progress_cb:
                progress_cb(status.progress() * 100)
        return response.get("id")

    def _delete_drive_file_by_name(self, filename: str, folder_id: str):
        results = self.service.files().list(
            q=f"'{folder_id}' in parents and name='{filename}' and trashed=false",
            fields="files(id)",
        ).execute()
        for f in results.get("files", []):
            self.service.files().delete(fileId=f["id"]).execute()

    def write_json_to_drive(self, data: dict, filename: str, folder_id: str) -> str:
        from googleapiclient.http import MediaIoBaseUpload

        content = json.dumps(data, indent=2).encode()
        media = MediaIoBaseUpload(io.BytesIO(content), mimetype="application/json")
        self._delete_drive_file_by_name(filename, folder_id)
        f = self.service.files().create(
            body={"name": filename, "parents": [folder_id]},
            media_body=media, fields="id",
        ).execute()
        return f["id"]

    def read_json_from_drive(self, filename: str, folder_id: str) -> dict | None:
        results = self.service.files().list(
            q=f"'{folder_id}' in parents and name='{filename}' and trashed=false",
            fields="files(id)",
        ).execute()
        files = results.get("files", [])
        if not files:
            return None
        raw = self.service.files().get_media(fileId=files[0]["id"]).execute()
        return json.loads(raw)

    def find_file_in_folder(self, name_contains: str, folder_id: str) -> dict | None:
        results = self.service.files().list(
            q=f"'{folder_id}' in parents and name contains '{name_contains}' and trashed=false",
            fields="files(id, name, size)",
        ).execute()
        files = results.get("files", [])
        return files[0] if files else None

    def download_file(self, file_id: str, local_path: str):
        from googleapiclient.http import MediaIoBaseDownload

        request = self.service.files().get_media(fileId=file_id)
        with open(local_path, "wb") as fh:
            downloader = MediaIoBaseDownload(fh, request, chunksize=4 * 1024 * 1024)
            done = False
            while not done:
                status, done = downloader.next_chunk()
                if status and self._set_progress:
                    # 98–100% range for download phase
                    self._set_progress(98 + status.progress() * 2)

    def poll_for_result(
        self,
        output_folder_id: str,
        config_folder_id: str,
        zip_prefix: str,
        on_status_update,
        on_complete,
        stop_event: threading.Event,
    ):
        self._log(f"[Cloud] Polling Drive every {POLL_INTERVAL_SECONDS}s for results...")
        while not stop_event.is_set():
            try:
                status_data = self.read_json_from_drive("colab_status.json", config_folder_id)
                if status_data:
                    on_status_update(status_data)
                    if status_data.get("state") == "complete":
                        zip_meta = self.find_file_in_folder(zip_prefix, output_folder_id)
                        if zip_meta:
                            on_complete(zip_meta)
                            return
                    elif status_data.get("state") == "error":
                        on_status_update(status_data)
                        return
            except Exception as e:
                self._log(f"[Cloud] Poll error (will retry): {e}")

            stop_event.wait(POLL_INTERVAL_SECONDS)


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
        self._drive_file_vars: list[tuple[dict, ctk.BooleanVar]] = []

        # Cloud mode state
        self._colab_authenticated = False
        self._colab_mgr: ColabCloudManager | None = None
        self._stop_polling_event = threading.Event()
        self._colab_selected_file: dict = {}   # {"id": ..., "name": ..., "size": ...}
        self._colab_file_var = ctk.StringVar(value="")

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

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
        ctk.CTkRadioButton(
            src_frame, text="Cloud (Colab)", variable=self._source_var,
            value="colab", command=self._on_source_toggle,
        ).grid(row=0, column=3, padx=12, pady=8)

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

        # ── Cloud (Colab) row (hidden initially) ──────────────────────────────
        self._colab_frame = ctk.CTkFrame(self)
        self._colab_frame.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(self._colab_frame, text="Cloud Mode:", width=100, anchor="w").grid(
            row=0, column=0, padx=(12, 6), pady=10
        )
        self._colab_status_label = ctk.CTkLabel(
            self._colab_frame,
            text="Authenticate Google Drive, then pick a video from your Drive to compress on Colab.",
            anchor="w", text_color="gray60", wraplength=440,
        )
        self._colab_status_label.grid(row=0, column=1, padx=6, pady=10, sticky="ew")

        self._colab_auth_btn = ctk.CTkButton(
            self._colab_frame, text="Authenticate Google", width=160,
            command=self._colab_authenticate_threaded,
        )
        self._colab_auth_btn.grid(row=0, column=2, padx=(6, 6), pady=10)

        self._colab_browse_btn = ctk.CTkButton(
            self._colab_frame, text="Browse Drive", width=120,
            command=self._colab_browse_drive_threaded, state="disabled",
        )
        self._colab_browse_btn.grid(row=0, column=3, padx=(0, 12), pady=10)

        # ── Cloud Drive file list (hidden until Browse) ───────────────────────
        self._colab_list_frame = ctk.CTkFrame(self)
        self._colab_list_frame.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            self._colab_list_frame,
            text="Select a video from your Drive to compress on Colab:", anchor="w",
        ).grid(row=0, column=0, padx=12, pady=(8, 2), sticky="w")

        self._colab_scroll = ctk.CTkScrollableFrame(self._colab_list_frame, height=120)
        self._colab_scroll.grid(row=1, column=0, padx=12, pady=(0, 8), sticky="ew")
        self._colab_scroll.grid_columnconfigure(0, weight=1)

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

    # ── Window Close ──────────────────────────────────────────────────────────

    def _on_close(self):
        self._stop_polling_event.set()
        self.destroy()

    # ── Source Toggle ─────────────────────────────────────────────────────────

    def _on_source_toggle(self):
        mode = self._source_var.get()
        self._local_frame.grid_remove()
        self._drive_frame.grid_remove()
        self._drive_list_frame.grid_remove()
        self._colab_frame.grid_remove()
        self._colab_list_frame.grid_remove()

        if mode == "local":
            self._local_frame.grid(row=1, column=0, padx=16, pady=4, sticky="ew")
        elif mode == "drive":
            self._drive_frame.grid(row=1, column=0, padx=16, pady=4, sticky="ew")
            if self._drive_file_vars:
                self._drive_list_frame.grid(row=2, column=0, padx=16, pady=4, sticky="ew")
        elif mode == "colab":
            self._colab_frame.grid(row=1, column=0, padx=16, pady=4, sticky="ew")
            if self._colab_selected_file:
                self._colab_list_frame.grid(row=2, column=0, padx=16, pady=4, sticky="ew")

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
        mode = self._source_var.get()
        if mode == "local":
            ready = bool(self.video_path and self.output_dir and not self._processing)
        elif mode == "drive":
            any_selected = any(v.get() for _, v in self._drive_file_vars)
            ready = bool(any_selected and self.output_dir and not self._processing)
        else:  # colab
            ready = bool(
                self._colab_authenticated
                and self._colab_selected_file
                and self.output_dir
                and not self._processing
            )
        self.start_btn.configure(state="normal" if ready else "disabled")

    # ── Google Drive (folder mode) ────────────────────────────────────────────

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
        for widget in self._drive_scroll.winfo_children():
            widget.destroy()
        self._drive_file_vars = []

        for meta in files:
            var = ctk.BooleanVar(value=False)
            ctk.CTkCheckBox(
                self._drive_scroll, text=meta["name"], variable=var,
                command=self._refresh_start_button,
            ).pack(anchor="w", padx=8, pady=2)
            self._drive_file_vars.append((meta, var))

        self._drive_list_frame.grid(row=2, column=0, padx=16, pady=4, sticky="ew")
        self._log("[Drive] Select the videos you want and click Start Processing.")
        self._refresh_start_button()

    def _download_drive_file(self, file_id: str, file_name: str, out_path: str):
        import gdown
        import requests

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

    # ── Cloud (Colab) Mode ────────────────────────────────────────────────────

    def _colab_authenticate_threaded(self):
        self._colab_auth_btn.configure(state="disabled", text="Authenticating…")
        threading.Thread(target=self._colab_authenticate, daemon=True).start()

    def _colab_authenticate(self):
        mgr = ColabCloudManager(self._log, self._set_status, self._set_progress)
        success = mgr.authenticate()
        if success:
            self._colab_mgr = mgr
            self._colab_authenticated = True
            self.after(0, lambda: self._colab_auth_btn.configure(
                text="✓ Authenticated", state="disabled",
                fg_color="#2ecc71", hover_color="#27ae60",
            ))
            self.after(0, lambda: self._colab_browse_btn.configure(state="normal"))
            self.after(0, lambda: self._colab_status_label.configure(
                text="Authenticated. Click 'Browse Drive' to pick a video from your Drive.",
                text_color="white",
            ))
        else:
            self.after(0, lambda: self._colab_auth_btn.configure(
                state="normal", text="Authenticate Google",
            ))
            self.after(0, lambda: messagebox.showerror(
                "Auth Failed", "Google authentication failed. See the log for details."
            ))
        self.after(0, self._refresh_start_button)

    def _colab_browse_drive_threaded(self):
        self._colab_browse_btn.configure(state="disabled", text="Loading…")
        threading.Thread(target=self._colab_browse_drive, daemon=True).start()

    def _colab_browse_drive(self):
        VIDEO_MIME_TYPES = (
            "video/mp4", "video/quicktime", "video/x-msvideo",
            "video/x-matroska", "video/webm", "video/x-flv", "video/x-m4v",
        )
        try:
            self._log("[Cloud] Listing video files from your Google Drive…")
            q = " or ".join(f"mimeType='{m}'" for m in VIDEO_MIME_TYPES)
            results = self._colab_mgr.service.files().list(
                q=f"({q}) and trashed=false",
                fields="files(id, name, size)",
                orderBy="modifiedTime desc",
                pageSize=100,
            ).execute()
            files = results.get("files", [])
            if not files:
                self.after(0, lambda: messagebox.showwarning(
                    "No Videos", "No video files found in your Google Drive."
                ))
                return
            self._log(f"[Cloud] Found {len(files)} video(s) in Drive.")
            self.after(0, self._colab_render_file_list, files)
        except Exception:
            err = traceback.format_exc()
            self._log(f"[Cloud] Browse error:\n{err}")
            self.after(0, lambda: messagebox.showerror(
                "Browse Error", "Failed to list Drive files. See log for details."
            ))
        finally:
            self.after(0, lambda: self._colab_browse_btn.configure(
                state="normal", text="Browse Drive"
            ))

    def _colab_render_file_list(self, files: list[dict]):
        for widget in self._colab_scroll.winfo_children():
            widget.destroy()
        self._colab_file_var.set("")
        self._colab_selected_file = {}

        for meta in files:
            size_mb = int(meta.get("size", 0)) / 1024 / 1024
            label = f"{meta['name']}  ({size_mb:.0f} MB)" if size_mb > 0 else meta["name"]
            ctk.CTkRadioButton(
                self._colab_scroll,
                text=label,
                variable=self._colab_file_var,
                value=meta["id"],
                command=lambda m=meta: self._colab_on_file_select(m),
            ).pack(anchor="w", padx=8, pady=2)

        self._colab_list_frame.grid(row=2, column=0, padx=16, pady=4, sticky="ew")
        self._refresh_start_button()

    def _colab_on_file_select(self, meta: dict):
        self._colab_selected_file = meta
        self._colab_status_label.configure(
            text=f"Selected: {meta['name']}", text_color="white"
        )
        self._refresh_start_button()

    def _run_colab_cloud(self, drive_file: dict, output_dir: str, selected_res: list[str]):
        mgr = self._colab_mgr
        video_name = drive_file["name"]
        video_file_id = drive_file["id"]
        zip_prefix = Path(video_name).stem

        try:
            # 1. Ensure Drive folders (config + output only — no upload needed)
            self._set_status("Setting up Drive folders…", "#3a9bd5")
            mgr.ensure_folder_structure()
            self._set_progress(10)

            # 2. Write config (file is already in Drive — just pass its ID)
            self._set_status("Writing config to Drive…", "#3a9bd5")
            config = {
                "input_filename": video_name,
                "input_file_id": video_file_id,
                "selected_resolutions": selected_res,
                "output_folder_id": mgr.folder_ids["output"],
                "config_folder_id": mgr.folder_ids["config"],
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            mgr.write_json_to_drive(config, "colab_config.json", mgr.folder_ids["config"])
            # Clear any old status from a previous run
            mgr._delete_drive_file_by_name("colab_status.json", mgr.folder_ids["config"])
            self._set_progress(20)
            self._log(f"[Cloud] Config written for: {video_name}")
            self._log(f"[Cloud] Drive file ID: {video_file_id}")

            # 3. Sync notebook to Drive and open it in Colab
            self._set_status("Syncing notebook to Drive…", "#3a9bd5")
            local_nb = Path(__file__).parent / "colab_hls.ipynb"
            nb_id = mgr.sync_notebook_to_drive(str(local_nb))
            colab_url = f"https://colab.research.google.com/drive/{nb_id}"
            webbrowser.open(colab_url)
            self._set_status("Waiting for Colab… Open the browser tab → Runtime → Run All", "#f39c12")
            self._log(f"[Cloud] Colab URL: {colab_url}")
            self._set_progress(25)

            # 4. Poll Drive for results
            self._stop_polling_event.clear()

            def on_status(data):
                msg = data.get("message", "Running…")
                pct = data.get("progress_pct", 0)
                self._set_status(f"Colab: {msg}", "#3a9bd5")
                self._set_progress(25 + pct * 0.72)  # 25–97%

            def on_complete(zip_meta):
                local_zip = os.path.join(output_dir, zip_meta["name"])
                self._set_status("Downloading result…", "#3a9bd5")
                self._log(f"[Cloud] Downloading result: {zip_meta['name']}")
                mgr.download_file(zip_meta["id"], local_zip)
                with zipfile.ZipFile(local_zip, "r") as zf:
                    zf.extractall(output_dir)
                os.remove(local_zip)
                self._set_progress(100)
                self._set_status("Completed", "#2ecc71")
                self._log(f"[Cloud] Done. Extracted to: {output_dir}")
                self._processing = False
                self.after(0, lambda: messagebox.showinfo(
                    "Done", f"Cloud output saved to:\n{output_dir}"
                ))
                self.after(0, self._refresh_start_button)

            mgr.poll_for_result(
                output_folder_id=mgr.folder_ids["output"],
                config_folder_id=mgr.folder_ids["config"],
                zip_prefix=zip_prefix,
                on_status_update=lambda d: self.after(0, on_status, d),
                on_complete=lambda z: self.after(0, on_complete, z),
                stop_event=self._stop_polling_event,
            )

        except Exception:
            err = traceback.format_exc()
            self._log(f"\n[Cloud ERROR]\n{err}")
            self._set_status("Error", "#e74c3c")
            self.after(0, lambda: messagebox.showerror(
                "Cloud Error", "Cloud processing failed. See log for details."
            ))
            self._processing = False
            self.after(0, self._refresh_start_button)

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

        for res in selected_res:
            os.makedirs(os.path.join(output_dir, res), exist_ok=True)

        split_labels = "".join(f"[v{i}]" for i in range(n))
        filter_parts = [f"[0:v]split={n}{split_labels}"]
        for i, res in enumerate(selected_res):
            cfg = RESOLUTIONS[res]
            filter_parts.append(f"[v{i}]scale={cfg['width']}:{cfg['height']}[s{i}]")
        filter_complex = ";".join(filter_parts)

        hwaccel = ["-hwaccel", "cuda"] if ENCODER == "nvenc" else []

        base_cmd = [
            "ffmpeg", "-y",
            *hwaccel,
            "-i", video_path,
            "-filter_complex", filter_complex,
        ]
        if ENCODER == "cpu":
            base_cmd += ["-threads", str(int(self.cpu_threads_var.get()))]

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

        mode = self._source_var.get()

        self._processing = True
        self.start_btn.configure(state="disabled")
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")
        self._set_progress(0)
        self._set_status("Processing…", "#3a9bd5")

        # ── Cloud (Colab) mode ────────────────────────────────────────────────
        if mode == "colab":
            threading.Thread(
                target=self._run_colab_cloud,
                args=(self._colab_selected_file, self.output_dir, selected_res),
                daemon=True,
            ).start()
            return

        # ── Local / Drive mode ────────────────────────────────────────────────
        if mode == "local":
            video_paths = [self.video_path]
            drive_metas = []
        else:
            drive_metas = [m for m, v in self._drive_file_vars if v.get()]
            if not drive_metas:
                messagebox.showwarning("No Files", "Select at least one video file.")
                self._processing = False
                self._refresh_start_button()
                return
            video_paths = []

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
            if self._drive_tmp:
                shutil.rmtree(self._drive_tmp, ignore_errors=True)
                self._drive_tmp = ""
                self.after(0, self._clear_drive_list)
            self.after(0, self._refresh_start_button)


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = VideoCompressorApp()
    app.mainloop()
