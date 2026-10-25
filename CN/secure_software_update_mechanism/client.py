from __future__ import annotations

import hashlib
import os
import queue
import re
import threading
import time
from pathlib import Path
from urllib.parse import quote

import json

import requests
import tkinter as tk
from tkinter import ttk

from update_manifest import chunk_hash_matches, validate_manifest

BASE_DIR = Path(__file__).resolve().parent
DOWNLOADS_DIR = BASE_DIR / "downloads"
DOWNLOADS_DIR.mkdir(exist_ok=True, parents=True)
APPLICATION_CHUNK_SIZE = 64 * 1024
MAX_CHUNK_RETRIES = 2
DEFAULT_ORIGIN_URL = "http://127.0.0.1:5000"
DEFAULT_EDGE_URL = "http://127.0.0.1:5001"


class DownloadInterruptedError(RuntimeError):
    pass


def format_bytes(value: int) -> str:
    units = ["B", "KB", "MB", "GB"]
    size = float(value)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{value} B"


def calculate_sha256(file_path: Path) -> str:
    hash_object = hashlib.sha256()
    with file_path.open("rb") as source_file:
        while True:
            chunk = source_file.read(APPLICATION_CHUNK_SIZE)
            if not chunk:
                break
            hash_object.update(chunk)
    return hash_object.hexdigest()


def get_partial_file_path(download_dir: Path, filename: str) -> Path:
    return download_dir / f"{filename}.part"


def get_resume_offset(file_path: Path) -> int:
    return file_path.stat().st_size if file_path.exists() else 0


def build_download_headers(resume_offset: int) -> dict[str, str]:
    if resume_offset < 0:
        raise ValueError("Resume offset cannot be negative.")
    if resume_offset == 0:
        return {}
    return {"Range": f"bytes={resume_offset}-"}


def parse_content_range(content_range: str) -> tuple[int, int, int]:
    match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", content_range.strip(), re.IGNORECASE)
    if match is None:
        raise RuntimeError(f"Invalid Content-Range header: {content_range!r}")
    start, end, total = (int(value) for value in match.groups())
    if total <= 0 or start < 0 or end < start or end >= total:
        raise RuntimeError(f"Inconsistent Content-Range header: {content_range!r}")
    return start, end, total


def validate_download_response(response, resume_offset: int, total_size: int) -> int:
    """Validate status and headers before any bytes are written to the partial file."""
    if response.status_code == 416:
        content_range = response.headers.get("Content-Range", "")
        raise RuntimeError(
            f"Server returned HTTP 416 for resume offset {resume_offset}. "
            f"Server range information: {content_range or 'not supplied'}."
        )

    try:
        content_length = int(response.headers.get("Content-Length", ""))
    except ValueError as exc:
        raise RuntimeError("Server response has a missing or invalid Content-Length header.") from exc

    if resume_offset > 0:
        if response.status_code != 206:
            raise RuntimeError(
                f"Resume expected HTTP 206, but the server returned HTTP {response.status_code}. "
                "The partial file was not appended to."
            )
        content_range = response.headers.get("Content-Range", "")
        start, end, advertised_total = parse_content_range(content_range)
        if start != resume_offset:
            raise RuntimeError(
                f"Server resumed at byte {start}, but byte {resume_offset} was requested."
            )
        if advertised_total != total_size or end != total_size - 1:
            raise RuntimeError("Content-Range end or total size does not match the expected file.")
        expected_length = end - start + 1
        if content_length != expected_length:
            raise RuntimeError("Content-Length does not match the advertised Content-Range.")
        return expected_length

    if response.status_code != 200:
        raise RuntimeError(f"New download expected HTTP 200, but the server returned HTTP {response.status_code}.")
    if content_length != total_size:
        raise RuntimeError("Content-Length does not match the expected file size.")
    return total_size


def save_response_to_part(
    response,
    part_file: Path,
    resume_offset: int,
    total_size: int,
    stop_event: threading.Event | None = None,
    progress_callback=None,
) -> int:
    """Validate first, then save exactly the advertised bytes to a .part file."""
    expected_response_length = validate_download_response(response, resume_offset, total_size)
    existing_size = get_resume_offset(part_file)
    if existing_size != resume_offset:
        raise RuntimeError(
            f"Partial file changed during transfer setup: expected {resume_offset} bytes, found {existing_size}."
        )

    file_mode = "ab" if resume_offset > 0 else "wb"
    received = 0
    with part_file.open(file_mode) as destination:
        for chunk in response.iter_content(chunk_size=APPLICATION_CHUNK_SIZE):
            if stop_event is not None and stop_event.is_set():
                raise DownloadInterruptedError("The user interrupted the download.")
            if not chunk:
                continue
            if received + len(chunk) > expected_response_length:
                raise RuntimeError("Server sent more bytes than its response headers advertised.")
            destination.write(chunk)
            destination.flush()
            received += len(chunk)
            if progress_callback is not None:
                progress_callback(resume_offset + received)

        if stop_event is not None and stop_event.is_set():
            raise DownloadInterruptedError("The user interrupted the download.")
        if received != expected_response_length:
            raise RuntimeError(
                f"Connection ended after {received} of {expected_response_length} expected response bytes."
            )

    return resume_offset + received


def verify_download_file(part_file: Path, expected_size: int, expected_hash: str) -> str:
    actual_size = part_file.stat().st_size
    if actual_size != expected_size:
        raise ValueError(f"File size mismatch: expected {expected_size}, found {actual_size}.")
    actual_hash = calculate_sha256(part_file)
    if actual_hash.lower() != expected_hash.lower():
        raise ValueError("SHA-256 mismatch: downloaded file contents are corrupted or inconsistent.")
    return actual_hash


def finalize_download(
    part_file: Path,
    final_file: Path,
    expected_size: int,
    expected_hash: str,
) -> str:
    actual_hash = verify_download_file(part_file, expected_size, expected_hash)
    os.replace(part_file, final_file)
    return actual_hash


def load_metadata(server_url: str, filename: str) -> dict:
    if not filename or filename in {".", ".."} or "/" in filename or "\\" in filename:
        raise ValueError("Enter a filename without folder paths.")
    metadata_url = f"{server_url.rstrip('/')}/metadata?filename={quote(filename, safe='')}"
    response = requests.get(metadata_url, timeout=(5, 10))
    response.raise_for_status()
    metadata = response.json()
    try:
        size = int(metadata["size"])
        digest = str(metadata["sha256"])
        version = str(metadata["version"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("Server returned incomplete update metadata.") from exc
    if size <= 0 or re.fullmatch(r"[0-9a-fA-F]{64}", digest) is None:
        raise RuntimeError("Server returned invalid file size or SHA-256 metadata.")
    return {"filename": filename, "size": size, "sha256": digest, "version": version}


# ---------------------------------------------------------------------------
# Chunk based transfer with a bitmap (used with the edge cache node)
# ---------------------------------------------------------------------------

class ChunkIntegrityError(RuntimeError):
    pass


def get_chunked_part_path(download_dir: Path, filename: str) -> Path:
    return download_dir / f"{filename}.chunked.part"


def get_bitmap_state_path(download_dir: Path, filename: str) -> Path:
    return download_dir / f"{filename}.bitmap.json"


def fetch_manifest(server_url: str, filename: str) -> dict:
    if not filename or filename in {".", ".."} or "/" in filename or "\\" in filename:
        raise ValueError("Enter a filename without folder paths.")
    manifest_url = f"{server_url.rstrip('/')}/manifest?filename={quote(filename, safe='')}"
    response = requests.get(manifest_url, timeout=(5, 30))
    response.raise_for_status()
    manifest = validate_manifest(response.json())
    if manifest.get("filename") != filename:
        raise RuntimeError("Manifest is for a different file than the one requested.")
    return manifest


def new_bitmap(chunk_count: int) -> list[int]:
    return [0] * chunk_count


def bitmap_to_text(bitmap: list[int]) -> str:
    return "".join("1" if bit else "0" for bit in bitmap)


def missing_chunks(bitmap: list[int]) -> list[int]:
    return [index for index, bit in enumerate(bitmap) if not bit]


def received_bytes(bitmap: list[int], manifest: dict) -> int:
    return sum(manifest["chunks"][index]["length"] for index, bit in enumerate(bitmap) if bit)


def save_bitmap_state(state_file: Path, manifest: dict, bitmap: list[int]) -> None:
    """Write the bitmap to a temp file first and then rename it, so a crash never leaves half a JSON file."""
    state = {
        "filename": manifest["filename"],
        "version": manifest["version"],
        "file_sha256": manifest["sha256"],
        "chunk_size": manifest["chunk_size"],
        "chunk_count": manifest["chunk_count"],
        "bitmap": bitmap_to_text(bitmap),
    }
    temp_file = state_file.with_name(state_file.name + ".tmp")
    temp_file.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(temp_file, state_file)


def load_bitmap_state(state_file: Path, manifest: dict) -> list[int] | None:
    if not state_file.exists():
        return None
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
        bitmap_text = str(state["bitmap"])
    except (OSError, ValueError, KeyError) as exc:
        raise RuntimeError("Saved bitmap state file is unreadable.") from exc
    if state.get("file_sha256") != manifest["sha256"] or state.get("chunk_count") != manifest["chunk_count"]:
        raise RuntimeError("Saved bitmap belongs to a different update version. Start a new download.")
    if len(bitmap_text) != manifest["chunk_count"] or set(bitmap_text) - {"0", "1"}:
        raise RuntimeError("Saved bitmap has an invalid format.")
    return [1 if bit == "1" else 0 for bit in bitmap_text]


def prepare_chunked_part(part_file: Path, total_size: int) -> None:
    """Create the .part file with its final size so every chunk can be written at its own offset."""
    if part_file.exists():
        if part_file.stat().st_size != total_size:
            raise RuntimeError("Existing chunked .part file has the wrong size.")
        return
    with part_file.open("wb") as destination:
        destination.truncate(total_size)


def request_chunk(server_url: str, filename: str, index: int) -> tuple[bytes, str]:
    url = f"{server_url.rstrip('/')}/chunk/{quote(filename, safe='')}/{index}"
    response = requests.get(url, timeout=(5, 10))
    if response.status_code != 200:
        raise RuntimeError(f"Chunk {index} request failed with HTTP {response.status_code}.")
    data = response.content
    try:
        content_length = int(response.headers.get("Content-Length", ""))
    except ValueError as exc:
        raise RuntimeError(f"Chunk {index} response has no valid Content-Length.") from exc
    if content_length != len(data):
        raise RuntimeError(f"Chunk {index} ended early ({len(data)} of {content_length} bytes).")
    return data, response.headers.get("X-Cache", "ORIGIN")


def write_chunk(part_file: Path, offset: int, data: bytes) -> None:
    with part_file.open("r+b") as destination:
        destination.seek(offset)
        destination.write(data)
        destination.flush()


def download_missing_chunks(
    server_url: str,
    manifest: dict,
    part_file: Path,
    state_file: Path,
    bitmap: list[int],
    stop_event: threading.Event | None = None,
    progress_callback=None,
    fetch_chunk=request_chunk,
) -> dict:
    """Request only the chunks whose bit is 0. Each chunk is verified before its bit becomes 1."""
    summary = {"requested": [], "hits": 0, "misses": 0, "origin": 0, "retries": 0}
    for index in missing_chunks(bitmap):
        if stop_event is not None and stop_event.is_set():
            raise DownloadInterruptedError("The user interrupted the download.")
        entry = manifest["chunks"][index]

        for attempt in range(MAX_CHUNK_RETRIES + 1):
            data, cache_status = fetch_chunk(server_url, manifest["filename"], index)
            if chunk_hash_matches(data, entry):
                break
            summary["retries"] += 1
            if attempt == MAX_CHUNK_RETRIES:
                raise ChunkIntegrityError(f"Chunk {index} failed the SHA-256 check {attempt + 1} times.")

        write_chunk(part_file, entry["offset"], data)
        bitmap[index] = 1
        save_bitmap_state(state_file, manifest, bitmap)

        summary["requested"].append(index)
        if cache_status == "HIT":
            summary["hits"] += 1
        elif cache_status == "MISS":
            summary["misses"] += 1
        else:
            summary["origin"] += 1
        if progress_callback is not None:
            progress_callback(received_bytes(bitmap, manifest), list(bitmap), cache_status)
    return summary


def finalize_chunked_download(part_file: Path, state_file: Path, final_file: Path, manifest: dict) -> str:
    actual_hash = finalize_download(part_file, final_file, manifest["size"], manifest["sha256"])
    state_file.unlink(missing_ok=True)
    return actual_hash


class UpdateClientApp(tk.Tk):
    """Dashboard style client: server status, chunk grid, speed graph, edge statistics and event log."""

    # colour palette used by the whole window
    BG = "#0f172a"          # window background (dark navy)
    PANEL = "#1e293b"       # card background
    PANEL_LIGHT = "#273449"
    TEXT = "#e2e8f0"
    MUTED = "#94a3b8"
    ACCENT = "#38bdf8"      # cyan
    GREEN = "#22c55e"
    AMBER = "#f59e0b"
    RED = "#ef4444"
    VIOLET = "#a78bfa"
    MISSING = "#334155"

    GRID_COLUMNS = 13

    def __init__(self):
        super().__init__()
        self.title("Secure Software Update Mechanism - Client Dashboard")
        self.geometry("1120x800")
        self.minsize(1060, 780)
        self.configure(background=self.BG)

        self.mode_var = tk.StringVar(value="chunked")
        self.server_url_var = tk.StringVar(value=DEFAULT_EDGE_URL)
        self.filename_var = tk.StringVar(value="sample_update.bin")
        self.version_var = tk.StringVar(value="-")
        self.size_var = tk.StringVar(value="-")
        self.downloaded_var = tk.StringVar(value="0 B / 0 B")
        self.speed_var = tk.StringVar(value="0 B/s")
        self.status_var = tk.StringVar(value="Ready")
        self.verification_var = tk.StringVar(value="Not verified")
        self.progress_var = tk.DoubleVar(value=0.0)
        self.percent_var = tk.StringVar(value="0 %")
        self.chunk_var = tk.StringVar(value="0 / 0")
        self.cache_var = tk.StringVar(value="0 HIT  |  0 MISS")
        self.bitmap_text_var = tk.StringVar(value="Bitmap: -")
        self.manifest_var = tk.StringVar(value="Manifest: not loaded")
        self.hash_var = tk.StringVar(value="SHA-256: -")
        self.edge_stats_vars = {
            "hit_ratio": tk.StringVar(value="-"),
            "offload": tk.StringVar(value="-"),
            "from_origin": tk.StringVar(value="-"),
            "served": tk.StringVar(value="-"),
        }

        self.bitmap = []
        self.chunk_source = {}          # index -> "HIT" / "MISS" / "ORIGIN"
        self.previous_bitmap = []
        self.cache_counts = {"HIT": 0, "MISS": 0, "ORIGIN": 0}
        self.speed_history = [0.0] * 120
        self.current_speed = 0.0
        self.last_progress_time = 0.0
        self.server_online = {"origin": None, "edge": None}

        self.ui_queue = queue.Queue()
        self.stop_event = threading.Event()
        self.transfer_thread = None
        self.transfer_running = False
        self.closing = False

        self._setup_styles()
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._process_ui_queue)
        self.after(250, self._tick_speed_graph)
        self.after(300, self._start_status_poller)
        self._log("Client started. Press Start Download to fetch the update.", "info")

    # ------------------------------------------------------------------ styles
    def _setup_styles(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        base_font = ("Segoe UI", 10)
        style.configure(".", background=self.BG, foreground=self.TEXT, font=base_font)
        style.configure("Card.TFrame", background=self.PANEL)
        style.configure("Card.TLabel", background=self.PANEL, foreground=self.TEXT)
        style.configure("Muted.TLabel", background=self.PANEL, foreground=self.MUTED, font=("Segoe UI", 9))
        style.configure("Value.TLabel", background=self.PANEL, foreground=self.ACCENT, font=("Segoe UI", 10, "bold"))
        style.configure("Big.TLabel", background=self.PANEL, foreground=self.TEXT, font=("Segoe UI", 18, "bold"))
        style.configure("CardTitle.TLabel", background=self.PANEL, foreground=self.MUTED, font=("Segoe UI", 9, "bold"))
        style.configure("Card.TRadiobutton", background=self.PANEL, foreground=self.TEXT)
        style.map("Card.TRadiobutton", background=[("active", self.PANEL)])
        style.configure("Card.TEntry", fieldbackground=self.PANEL_LIGHT, foreground=self.TEXT, insertcolor=self.TEXT,
                        bordercolor=self.MISSING, lightcolor=self.PANEL_LIGHT, darkcolor=self.PANEL_LIGHT)
        style.configure("Accent.Horizontal.TProgressbar", troughcolor=self.PANEL_LIGHT, background=self.ACCENT,
                        bordercolor=self.PANEL, lightcolor=self.ACCENT, darkcolor=self.ACCENT, thickness=16)
        for name, colour in (("Start", self.GREEN), ("Stop", self.RED), ("Resume", self.ACCENT), ("Plain", self.PANEL_LIGHT)):
            style.configure(f"{name}.TButton", background=colour, foreground="#0b1220" if name != "Plain" else self.TEXT,
                            font=("Segoe UI", 10, "bold"), padding=(16, 7) if name != "Plain" else (10, 4), borderwidth=0)
            style.map(f"{name}.TButton", background=[("active", self.TEXT), ("disabled", self.MISSING)])

    def _card(self, parent, title):
        frame = ttk.Frame(parent, style="Card.TFrame", padding=(12, 10))
        if title:
            ttk.Label(frame, text=title.upper(), style="CardTitle.TLabel").pack(anchor="w", pady=(0, 6))
        return frame

    # ------------------------------------------------------------------ layout
    def _build_ui(self):
        # header bar
        header = tk.Frame(self, background="#0b1220", height=64)
        header.pack(fill=tk.X)
        titles = tk.Frame(header, background="#0b1220")
        titles.pack(side=tk.LEFT, padx=16, pady=8)
        tk.Label(titles, text="\u25C6  Secure Software Update Mechanism", background="#0b1220", foreground=self.TEXT,
                 font=("Segoe UI", 16, "bold")).pack(anchor="w")
        tk.Label(titles, text="1 MiB chunks  •  HMAC-signed manifest  •  Edge cache  •  Bitmap resume",
                 background="#0b1220", foreground=self.MUTED, font=("Segoe UI", 9)).pack(anchor="w")
        self.status_dots = {}
        for key, label in (("edge", "Edge :5001"), ("origin", "Origin :5000")):
            box = tk.Frame(header, background="#0b1220")
            box.pack(side=tk.RIGHT, padx=10)
            dot = tk.Canvas(box, width=14, height=14, background="#0b1220", highlightthickness=0)
            dot.pack(side=tk.LEFT, padx=(0, 6))
            dot.create_oval(2, 2, 12, 12, fill=self.MUTED, outline="", tags="dot")
            text = tk.Label(box, text=f"{label}  checking", background="#0b1220", foreground=self.MUTED,
                            font=("Segoe UI", 9, "bold"))
            text.pack(side=tk.LEFT)
            self.status_dots[key] = (dot, text, label)

        body = tk.Frame(self, background=self.BG)
        body.pack(fill=tk.BOTH, expand=True, padx=12, pady=10)
        body.columnconfigure(0, weight=3, uniform="columns")
        body.columnconfigure(1, weight=2, uniform="columns")
        body.rowconfigure(1, weight=1)

        # ---- left column
        left = tk.Frame(body, background=self.BG)
        left.grid(row=0, column=0, rowspan=2, sticky="nsew", padx=(0, 10))

        settings = self._card(left, "Update source")
        settings.pack(fill=tk.X)
        grid = ttk.Frame(settings, style="Card.TFrame")
        grid.pack(fill=tk.X)
        ttk.Label(grid, text="Server URL", style="Muted.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Entry(grid, textvariable=self.server_url_var, style="Card.TEntry", width=34).grid(row=1, column=0, sticky="ew", padx=(0, 10))
        ttk.Label(grid, text="Update file", style="Muted.TLabel").grid(row=0, column=1, sticky="w")
        ttk.Entry(grid, textvariable=self.filename_var, style="Card.TEntry", width=22).grid(row=1, column=1, sticky="ew")
        grid.columnconfigure(0, weight=3)
        grid.columnconfigure(1, weight=2)
        modes = ttk.Frame(settings, style="Card.TFrame")
        modes.pack(fill=tk.X, pady=(8, 0))
        ttk.Label(modes, text="Transfer mode:", style="Card.TLabel").pack(side=tk.LEFT, padx=(0, 8))
        ttk.Radiobutton(modes, text="Chunked via edge cache", value="chunked", variable=self.mode_var,
                        command=self._mode_changed, style="Card.TRadiobutton").pack(side=tk.LEFT, padx=(0, 12))
        ttk.Radiobutton(modes, text="Direct HTTP Range (origin)", value="range", variable=self.mode_var,
                        command=self._mode_changed, style="Card.TRadiobutton").pack(side=tk.LEFT)

        progress_card = self._card(left, "Transfer progress")
        progress_card.pack(fill=tk.X, pady=(10, 0))
        top = ttk.Frame(progress_card, style="Card.TFrame")
        top.pack(fill=tk.X)
        ttk.Label(top, textvariable=self.percent_var, style="Big.TLabel").pack(side=tk.LEFT)
        right_info = ttk.Frame(top, style="Card.TFrame")
        right_info.pack(side=tk.RIGHT)
        ttk.Label(right_info, textvariable=self.downloaded_var, style="Value.TLabel").pack(anchor="e")
        ttk.Label(right_info, textvariable=self.speed_var, style="Muted.TLabel").pack(anchor="e")
        ttk.Progressbar(progress_card, variable=self.progress_var, maximum=100,
                        style="Accent.Horizontal.TProgressbar").pack(fill=tk.X, pady=(6, 4))
        ttk.Label(progress_card, textvariable=self.status_var, style="Card.TLabel", wraplength=600).pack(anchor="w")

        chunk_card = self._card(left, "Chunk map (1 box = 1 MiB chunk)")
        chunk_card.pack(fill=tk.X, pady=(10, 0))
        self.grid_canvas = tk.Canvas(chunk_card, height=96, background=self.PANEL, highlightthickness=0)
        self.grid_canvas.pack(fill=tk.X)
        self.grid_canvas.bind("<Configure>", lambda _event: self._draw_chunk_grid())
        legend = ttk.Frame(chunk_card, style="Card.TFrame")
        legend.pack(fill=tk.X, pady=(6, 0))
        for colour, text in ((self.MISSING, "missing"), (self.AMBER, "downloading"), (self.ACCENT, "edge HIT"),
                             (self.VIOLET, "edge MISS (origin)")):
            swatch = tk.Canvas(legend, width=12, height=12, background=self.PANEL, highlightthickness=0)
            swatch.create_rectangle(1, 1, 11, 11, fill=colour, outline="")
            swatch.pack(side=tk.LEFT, padx=(0, 4))
            ttk.Label(legend, text=text, style="Muted.TLabel").pack(side=tk.LEFT, padx=(0, 12))
        ttk.Label(chunk_card, textvariable=self.bitmap_text_var, style="Muted.TLabel",
                  font=("Consolas", 9)).pack(anchor="w", pady=(6, 0))

        speed_card = self._card(left, "Throughput (last 30 s)")
        speed_card.pack(fill=tk.BOTH, expand=True, pady=(10, 0))
        self.speed_canvas = tk.Canvas(speed_card, height=90, background=self.PANEL, highlightthickness=0)
        self.speed_canvas.pack(fill=tk.BOTH, expand=True)

        buttons = tk.Frame(left, background=self.BG)
        buttons.pack(fill=tk.X, pady=(10, 0))
        ttk.Button(buttons, text="▶  Start Download", style="Start.TButton", command=self.start_download).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(buttons, text="■  Interrupt", style="Stop.TButton", command=self.interrupt_download).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(buttons, text="↻  Resume", style="Resume.TButton", command=self.resume_download).pack(side=tk.LEFT, padx=(0, 8))

        # ---- right column
        right = tk.Frame(body, background=self.BG)
        right.grid(row=0, column=1, rowspan=2, sticky="nsew")

        integrity = self._card(right, "Integrity")
        integrity.pack(fill=tk.X)
        ttk.Label(integrity, textvariable=self.manifest_var, style="Card.TLabel").pack(anchor="w")
        ttk.Label(integrity, textvariable=self.verification_var, style="Card.TLabel").pack(anchor="w", pady=(2, 0))
        ttk.Label(integrity, textvariable=self.hash_var, style="Muted.TLabel", font=("Consolas", 9)).pack(anchor="w", pady=(2, 0))
        info = ttk.Frame(integrity, style="Card.TFrame")
        info.pack(fill=tk.X, pady=(6, 0))
        for column, (label, variable) in enumerate((("Version", self.version_var), ("File size", self.size_var),
                                                    ("Chunks", self.chunk_var))):
            ttk.Label(info, text=label, style="Muted.TLabel").grid(row=0, column=column, sticky="w", padx=(0, 18))
            ttk.Label(info, textvariable=variable, style="Value.TLabel").grid(row=1, column=column, sticky="w", padx=(0, 18))

        this_session = self._card(right, "This download")
        this_session.pack(fill=tk.X, pady=(10, 0))
        ttk.Label(this_session, textvariable=self.cache_var, style="Big.TLabel").pack(anchor="w")

        edge_card = self._card(right, "Edge cache statistics (live from /stats)")
        edge_card.pack(fill=tk.X, pady=(10, 0))
        stats_grid = ttk.Frame(edge_card, style="Card.TFrame")
        stats_grid.pack(fill=tk.X)
        for position, (key, label) in enumerate((("hit_ratio", "Hit ratio"), ("offload", "Origin offload"),
                                                 ("from_origin", "Fetched from origin"), ("served", "Served to clients"))):
            cell = ttk.Frame(stats_grid, style="Card.TFrame")
            cell.grid(row=position // 2, column=position % 2, sticky="w", padx=(0, 26), pady=3)
            ttk.Label(cell, text=label, style="Muted.TLabel").pack(anchor="w")
            ttk.Label(cell, textvariable=self.edge_stats_vars[key], style="Value.TLabel").pack(anchor="w")
        tools = ttk.Frame(edge_card, style="Card.TFrame")
        tools.pack(fill=tk.X, pady=(8, 0))
        ttk.Button(tools, text="Clear edge cache", style="Plain.TButton", command=self.clear_edge_cache).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(tools, text="Reset local files", style="Plain.TButton", command=self.reset_local_files).pack(side=tk.LEFT)

        log_card = self._card(right, "Event log")
        log_card.pack(fill=tk.BOTH, expand=True, pady=(10, 0))
        self.log_text = tk.Text(log_card, height=12, background="#0b1220", foreground=self.TEXT, relief="flat",
                                font=("Consolas", 9), wrap="word", state="disabled", padx=8, pady=6)
        self.log_text.pack(fill=tk.BOTH, expand=True)
        for tag, colour in (("info", self.MUTED), ("ok", self.GREEN), ("hit", self.ACCENT), ("miss", self.VIOLET),
                            ("warn", self.AMBER), ("error", self.RED)):
            self.log_text.tag_configure(tag, foreground=colour)

    # ------------------------------------------------------------------ small helpers
    def _log(self, message: str, tag: str = "info"):
        stamp = time.strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{stamp}] {message}\n", tag)
        line_count = int(self.log_text.index("end-1c").split(".")[0])
        if line_count > 400:
            self.log_text.delete("1.0", "50.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _mode_changed(self):
        current = self.server_url_var.get().strip().rstrip("/")
        if self.mode_var.get() == "chunked" and current in {"", DEFAULT_ORIGIN_URL}:
            self.server_url_var.set(DEFAULT_EDGE_URL)
        elif self.mode_var.get() == "range" and current in {"", DEFAULT_EDGE_URL}:
            self.server_url_var.set(DEFAULT_ORIGIN_URL)
        self._log(f"Transfer mode changed to {'chunked (edge cache)' if self.mode_var.get() == 'chunked' else 'direct HTTP Range'}.")

    def _draw_chunk_grid(self):
        canvas = self.grid_canvas
        canvas.delete("all")
        count = len(self.bitmap)
        if count == 0:
            message = ("Not used in direct HTTP Range mode (the file is resumed by byte offset)."
                       if self.mode_var.get() == "range" else
                       "The chunk map appears after the signed manifest is received.")
            canvas.create_text(10, 40, anchor="w", fill=self.MUTED, font=("Segoe UI", 10), text=message)
            return
        width = max(canvas.winfo_width(), 200)
        columns = min(self.GRID_COLUMNS, count)
        rows = (count + columns - 1) // columns
        gap = 6
        box_w = (width - gap * (columns + 1)) / columns
        box_h = min(38, (96 - gap * (rows + 1)) / rows)
        next_missing = next((i for i, bit in enumerate(self.bitmap) if not bit), None)
        for index, bit in enumerate(self.bitmap):
            row, column = divmod(index, columns)
            x0 = gap + column * (box_w + gap)
            y0 = gap + row * (box_h + gap)
            if bit:
                source = self.chunk_source.get(index, "OLD")
                colour = {"HIT": self.ACCENT, "MISS": self.VIOLET, "ORIGIN": self.GREEN}.get(source, "#64748b")
            elif self.transfer_running and index == next_missing and self.mode_var.get() == "chunked":
                colour = self.AMBER
            else:
                colour = self.MISSING
            canvas.create_rectangle(x0, y0, x0 + box_w, y0 + box_h, fill=colour, outline="")
            canvas.create_text(x0 + box_w / 2, y0 + box_h / 2, text=str(index),
                               fill="#0b1220" if bit or colour == self.AMBER else self.MUTED, font=("Segoe UI", 8, "bold"))

    def _set_bitmap(self, bitmap):
        new_bitmap = list(bitmap)
        self.bitmap = new_bitmap
        done = sum(new_bitmap)
        self.chunk_var.set(f"{done} / {len(new_bitmap)}")
        self.bitmap_text_var.set(f"Bitmap: {bitmap_to_text(new_bitmap)}")
        self._draw_chunk_grid()

    def _draw_speed_graph(self):
        canvas = self.speed_canvas
        canvas.delete("all")
        width = max(canvas.winfo_width(), 200)
        height = max(canvas.winfo_height(), 80)
        peak = max(max(self.speed_history), 1.0)
        top = peak * 1.15
        for fraction in (0.25, 0.5, 0.75):
            y = height - fraction * (height - 14)
            canvas.create_line(76, y, width - 6, y, fill=self.PANEL_LIGHT)
            canvas.create_text(72, y, anchor="e", fill=self.MUTED, font=("Segoe UI", 7),
                               text=format_bytes(int(top * fraction)) + "/s")
        step = (width - 84) / (len(self.speed_history) - 1)
        points = []
        for index, value in enumerate(self.speed_history):
            x = 78 + index * step
            y = height - (value / top) * (height - 14) - 2
            points.extend((x, y))
        polygon = [78, height] + points + [78 + (len(self.speed_history) - 1) * step, height]
        canvas.create_polygon(polygon, fill="#12384d", outline="")
        canvas.create_line(points, fill=self.ACCENT, width=2, smooth=True)

    def _tick_speed_graph(self):
        if time.monotonic() - self.last_progress_time > 1.0:
            self.current_speed = 0.0
        self.speed_history = self.speed_history[1:] + [self.current_speed]
        self._draw_speed_graph()
        if not self.closing:
            self.after(250, self._tick_speed_graph)

    def _set_server_status(self, key, online):
        dot, text, label = self.status_dots[key]
        colour = self.GREEN if online else self.RED
        dot.itemconfigure("dot", fill=colour)
        text.configure(text=f"{label}  {'online' if online else 'offline'}", foreground=colour)
        if self.server_online[key] is not None and self.server_online[key] != online:
            self._log(f"{label} is now {'online' if online else 'OFFLINE'}.", "ok" if online else "error")
        self.server_online[key] = online

    # ------------------------------------------------------------------ background polling of /health and /stats
    def _start_status_poller(self):
        threading.Thread(target=self._status_poller, daemon=True).start()

    def _status_poller(self):
        while not self.closing:
            for key, url in (("origin", DEFAULT_ORIGIN_URL), ("edge", DEFAULT_EDGE_URL)):
                try:
                    online = requests.get(f"{url}/health", timeout=(0.5, 1)).status_code == 200
                except requests.RequestException:
                    online = False
                self._post_ui("server_status", (key, online))
            try:
                stats = requests.get(f"{DEFAULT_EDGE_URL}/stats", timeout=(0.5, 1)).json()
                self._post_ui("edge_stats", stats)
            except (requests.RequestException, ValueError):
                pass
            time.sleep(1.5)

    # ------------------------------------------------------------------ queue processing (runs in the Tk thread)
    def _post_ui(self, event_name: str, value=None):
        self.ui_queue.put((event_name, value))

    def _process_ui_queue(self):
        while True:
            try:
                event_name, value = self.ui_queue.get_nowait()
            except queue.Empty:
                break

            if event_name == "status":
                self.status_var.set(value)
                self._log(value)
            elif event_name == "log":
                self._log(*value)
            elif event_name == "metadata":
                metadata, downloaded = value
                self.version_var.set(metadata["version"])
                self.size_var.set(format_bytes(metadata["size"]))
                self.hash_var.set(f"SHA-256: {metadata['sha256'][:32]}...")
                self._update_progress_gui(downloaded, metadata["size"], 0)
            elif event_name == "manifest_ok":
                self.manifest_var.set(f"✔ Manifest signature valid ({value} chunks)")
                self._log(f"Signed manifest verified with HMAC-SHA256: {value} chunks of 1 MiB.", "ok")
            elif event_name == "progress":
                downloaded, total, speed = value
                self._update_progress_gui(downloaded, total, speed)
            elif event_name == "verification":
                self.verification_var.set(value)
            elif event_name == "complete":
                self.status_var.set("Download completed and verified.")
                self.verification_var.set("✔ Full-file SHA-256 verified")
                self.progress_var.set(100.0)
                self.percent_var.set("100 %")
                self.downloaded_var.set(f"{format_bytes(value)} / {format_bytes(value)}")
                self.current_speed = 0.0
                self._draw_chunk_grid()
                self._log("Full file SHA-256 matches the manifest. Update saved in downloads/.", "ok")
            elif event_name == "interrupted":
                saved_bytes, part_path = value
                self.status_var.set(f"Interrupted. Preserved {format_bytes(saved_bytes)} in {Path(part_path).name}")
                self.verification_var.set("Partial file retained")
                self._log(f"Interrupted by user. {format_bytes(saved_bytes)} kept for resume.", "warn")
            elif event_name == "chunk_interrupted":
                done, total, saved_bytes, state_path = value
                self.status_var.set(
                    f"Interrupted. {done}/{total} chunks ({format_bytes(saved_bytes)}) kept; bitmap saved in {Path(state_path).name}"
                )
                self.verification_var.set("Partial chunks retained")
                self._draw_chunk_grid()
                self._log(f"Interrupted. Bitmap {bitmap_to_text(self.bitmap)} saved to disk.", "warn")
            elif event_name == "network_error":
                message, saved_bytes, part_path = value
                self.status_var.set(f"Network error. Preserved {format_bytes(saved_bytes)}; resume when connected.")
                self.verification_var.set("Connection failure; partial data retained")
                self._log(f"Network error: {message}", "error")
            elif event_name == "error":
                message, saved_bytes, part_path = value
                self.status_var.set(f"Download failed: {message}")
                self.verification_var.set("Verification or transfer failed")
                self._log(f"Error: {message}", "error")
            elif event_name == "bitmap":
                self._set_bitmap(value)
            elif event_name == "chunk_done":
                index, cache_status = value
                self.chunk_source[index] = cache_status
                self.cache_counts[cache_status] = self.cache_counts.get(cache_status, 0) + 1
                self.cache_var.set(f"{self.cache_counts['HIT']} HIT  |  {self.cache_counts['MISS']} MISS")
                tag = {"HIT": "hit", "MISS": "miss"}.get(cache_status, "info")
                where = {"HIT": "from edge cache", "MISS": "origin -> edge"}.get(cache_status, "from origin")
                self._log(f"Chunk {index:2d}  {cache_status:<4} {where}  \u2714", tag)
            elif event_name == "server_status":
                self._set_server_status(*value)
            elif event_name == "edge_stats":
                served = value.get("bytes_served", 0)
                from_origin = value.get("bytes_from_origin", 0)
                self.edge_stats_vars["hit_ratio"].set(f"{value.get('hit_ratio', 0) * 100:.1f} %")
                self.edge_stats_vars["offload"].set(f"{(1 - from_origin / served) * 100:.1f} %" if served else "-")
                self.edge_stats_vars["from_origin"].set(format_bytes(from_origin))
                self.edge_stats_vars["served"].set(format_bytes(served))
            elif event_name == "finished":
                self.transfer_running = False
                self.transfer_thread = None
                self._draw_chunk_grid()
                if self.closing:
                    self.destroy()
                    return

        if not self.closing:
            self.after(100, self._process_ui_queue)

    def _update_progress_gui(self, downloaded: int, total: int, speed: float):
        percent = min((downloaded / total) * 100.0, 100.0) if total > 0 else 0.0
        self.progress_var.set(percent)
        self.percent_var.set(f"{percent:.0f} %")
        self.downloaded_var.set(f"{format_bytes(downloaded)} / {format_bytes(total)}")
        self.speed_var.set(f"{format_bytes(int(speed))}/s")
        if speed > 0:
            self.current_speed = speed
            self.last_progress_time = time.monotonic()

    # ------------------------------------------------------------------ direct HTTP Range mode worker
    def _download_worker(self, mode: str, server_url: str, filename: str):
        part_file = get_partial_file_path(DOWNLOADS_DIR, filename)
        try:
            metadata = load_metadata(server_url, filename)
            total_size = metadata["size"]
            expected_hash = metadata["sha256"]
            resume_offset = get_resume_offset(part_file) if mode == "resume" else 0

            if mode == "new" and resume_offset > 0:
                raise RuntimeError("A partial file already exists. Use Resume Download instead.")
            if mode == "resume" and resume_offset == 0:
                raise RuntimeError("No saved partial file was found. Start a new download instead.")
            if resume_offset > total_size:
                raise RuntimeError(
                    f"The saved partial file ({resume_offset} bytes) is larger than the server file ({total_size} bytes)."
                )

            self._post_ui("metadata", (metadata, resume_offset))

            if resume_offset == total_size:
                try:
                    verify_download_file(part_file, total_size, expected_hash)
                except ValueError:
                    backup_path = part_file.with_name(part_file.name + ".corrupt")
                    os.replace(part_file, backup_path)
                    self._post_ui(
                        "status",
                        f"Complete partial file failed SHA-256; restarting. Old copy kept at {backup_path}",
                    )
                    resume_offset = 0
                    self._post_ui("metadata", (metadata, resume_offset))
                else:
                    finalize_download(part_file, DOWNLOADS_DIR / filename, total_size, expected_hash)
                    self._post_ui("complete", total_size)
                    return

            if resume_offset == 0:
                self._post_ui("status", "Downloading the whole file with HTTP GET (expecting 200 OK)...")
            else:
                self._post_ui("status", f"Resuming from byte {resume_offset}.")
                self._post_ui("log", (f"Sending header Range: bytes={resume_offset}- (expecting 206 Partial Content)", "hit"))
            request_url = f"{server_url.rstrip('/')}/download/{quote(filename, safe='')}"
            request_headers = build_download_headers(resume_offset)
            last_speed_time = time.monotonic()
            last_speed_bytes = resume_offset

            with requests.get(request_url, headers=request_headers, stream=True, timeout=(5, 1)) as response:
                self._post_ui("log", (f"Server answered HTTP {response.status_code}", "ok" if response.status_code in (200, 206) else "error"))

                def report_progress(downloaded: int):
                    nonlocal last_speed_time, last_speed_bytes
                    now = time.monotonic()
                    elapsed = max(now - last_speed_time, 0.001)
                    speed = (downloaded - last_speed_bytes) / elapsed
                    if now - last_speed_time >= 0.5:
                        last_speed_time = now
                        last_speed_bytes = downloaded
                    self._post_ui("progress", (downloaded, total_size, speed))

                downloaded = save_response_to_part(
                    response, part_file, resume_offset, total_size,
                    stop_event=self.stop_event, progress_callback=report_progress,
                )

            finalize_download(part_file, DOWNLOADS_DIR / filename, total_size, expected_hash)
            self._post_ui("complete", downloaded)

        except DownloadInterruptedError:
            self._post_ui("interrupted", (get_resume_offset(part_file), part_file))
        except requests.RequestException as exc:
            if self.stop_event.is_set():
                self._post_ui("interrupted", (get_resume_offset(part_file), part_file))
            else:
                self._post_ui("network_error", (str(exc), get_resume_offset(part_file), part_file))
        except (RuntimeError, ValueError, OSError) as exc:
            self._post_ui("error", (str(exc), get_resume_offset(part_file), part_file))
        finally:
            self._post_ui("finished")

    # ------------------------------------------------------------------ chunked mode worker
    def _chunked_worker(self, mode: str, server_url: str, filename: str):
        part_file = get_chunked_part_path(DOWNLOADS_DIR, filename)
        state_file = get_bitmap_state_path(DOWNLOADS_DIR, filename)
        manifest = None
        bitmap = []
        try:
            manifest = fetch_manifest(server_url, filename)
            self._post_ui("manifest_ok", manifest["chunk_count"])
            self._post_ui("verification", "Waiting for chunks...")

            if mode == "new":
                if state_file.exists():
                    raise RuntimeError("A saved chunk bitmap already exists. Use Resume Download instead.")
                part_file.unlink(missing_ok=True)
                bitmap = new_bitmap(manifest["chunk_count"])
            else:
                bitmap = load_bitmap_state(state_file, manifest)
                if bitmap is None:
                    raise RuntimeError("No saved chunk bitmap was found. Start a new download instead.")

            prepare_chunked_part(part_file, manifest["size"])
            save_bitmap_state(state_file, manifest, bitmap)
            already = received_bytes(bitmap, manifest)
            self._post_ui("metadata", (manifest, already))
            self._post_ui("bitmap", bitmap)

            pending = missing_chunks(bitmap)
            if mode == "new":
                self._post_ui("status", f"Downloading {len(pending)} chunks of 1 MiB...")
            else:
                self._post_ui(
                    "status",
                    f"Resuming: requesting only {len(pending)} missing chunks (first missing = {pending[0] if pending else '-'}).",
                )

            last_time = time.monotonic()
            last_bytes = already
            previous = list(bitmap)

            def report_progress(downloaded: int, current_bitmap, cache_status: str):
                nonlocal last_time, last_bytes, previous
                now = time.monotonic()
                speed = (downloaded - last_bytes) / max(now - last_time, 0.001)
                last_time, last_bytes = now, downloaded
                for index, (old, new) in enumerate(zip(previous, current_bitmap)):
                    if new and not old:
                        self._post_ui("chunk_done", (index, cache_status))
                previous = list(current_bitmap)
                self._post_ui("progress", (downloaded, manifest["size"], speed))
                self._post_ui("bitmap", current_bitmap)

            download_missing_chunks(
                server_url, manifest, part_file, state_file, bitmap,
                stop_event=self.stop_event, progress_callback=report_progress,
            )
            finalize_chunked_download(part_file, state_file, DOWNLOADS_DIR / filename, manifest)
            self._post_ui("complete", manifest["size"])

        except DownloadInterruptedError:
            done = sum(bitmap)
            saved = received_bytes(bitmap, manifest) if manifest else 0
            self._post_ui("chunk_interrupted", (done, len(bitmap), saved, state_file))
        except requests.RequestException as exc:
            done = sum(bitmap)
            saved = received_bytes(bitmap, manifest) if manifest else 0
            if self.stop_event.is_set():
                self._post_ui("chunk_interrupted", (done, len(bitmap), saved, state_file))
            else:
                self._post_ui("network_error", (str(exc), saved, state_file))
        except (RuntimeError, ValueError, OSError) as exc:
            saved = received_bytes(bitmap, manifest) if manifest and bitmap else 0
            self._post_ui("error", (str(exc), saved, state_file))
        finally:
            self._post_ui("finished")

    # ------------------------------------------------------------------ button handlers
    def _start_worker(self, mode: str):
        if self.transfer_running:
            self.status_var.set("A transfer is already running.")
            return

        server_url = self.server_url_var.get().strip().rstrip("/")
        filename = self.filename_var.get().strip() or "sample_update.bin"
        if not server_url:
            self.status_var.set("Enter a server URL.")
            return
        if not filename or filename in {".", ".."} or "/" in filename or "\\" in filename:
            self.status_var.set("Enter a filename without folder paths.")
            return

        self.stop_event.clear()
        self.transfer_running = True
        self.cache_counts = {"HIT": 0, "MISS": 0, "ORIGIN": 0}
        self.cache_var.set("0 HIT  |  0 MISS")
        if mode == "new":
            self.chunk_source = {}
        if self.mode_var.get() == "range":
            self.manifest_var.set("Direct mode: size + SHA-256 from /metadata")
            self.cache_var.set("Direct from origin")
            self.bitmap = []
            self.bitmap_text_var.set("Bitmap: - (not used in direct Range mode)")
            self.chunk_var.set("-")
            self._draw_chunk_grid()
        self._log(f"{'Start' if mode == 'new' else 'Resume'} requested ({'chunked via edge' if self.mode_var.get() == 'chunked' else 'direct Range'}).")
        worker = self._chunked_worker if self.mode_var.get() == "chunked" else self._download_worker
        self.transfer_thread = threading.Thread(target=worker, args=(mode, server_url, filename), daemon=True)
        self.transfer_thread.start()

    def start_download(self):
        self._start_worker("new")

    def interrupt_download(self):
        if not self.transfer_running:
            self.status_var.set("No transfer is running.")
            return
        self.stop_event.set()
        self.status_var.set("Interrupt requested; finishing the current file write safely.")

    def resume_download(self):
        filename = self.filename_var.get().strip() or "sample_update.bin"
        if not filename or filename in {".", ".."} or "/" in filename or "\\" in filename:
            self.status_var.set("Enter a filename without folder paths.")
            return
        if self.mode_var.get() == "chunked":
            saved_state = get_bitmap_state_path(DOWNLOADS_DIR, filename)
        else:
            saved_state = get_partial_file_path(DOWNLOADS_DIR, filename)
        if not saved_state.exists():
            self.status_var.set("No saved partial download found. Start a new download first.")
            return
        self._start_worker("resume")

    def reset_local_files(self):
        if self.transfer_running:
            self.status_var.set("Stop the transfer before resetting.")
            return
        filename = self.filename_var.get().strip() or "sample_update.bin"
        removed = 0
        for path in (get_chunked_part_path(DOWNLOADS_DIR, filename), get_bitmap_state_path(DOWNLOADS_DIR, filename),
                     get_partial_file_path(DOWNLOADS_DIR, filename), DOWNLOADS_DIR / filename):
            if path.exists():
                path.unlink()
                removed += 1
        self.bitmap, self.chunk_source = [], {}
        self.progress_var.set(0)
        self.percent_var.set("0 %")
        self.downloaded_var.set("0 B / 0 B")
        self.chunk_var.set("0 / 0")
        self.bitmap_text_var.set("Bitmap: -")
        self.verification_var.set("Not verified")
        self.status_var.set("Ready")
        self._draw_chunk_grid()
        self._log(f"Local download files removed ({removed} file(s)).", "warn")

    def clear_edge_cache(self):
        try:
            response = requests.post(f"{DEFAULT_EDGE_URL}/cache/clear", timeout=(1, 3))
            response.raise_for_status()
            self._log(f"Edge cache cleared ({response.json().get('removed_chunks', 0)} chunks removed).", "warn")
        except requests.RequestException as exc:
            self._log(f"Could not clear edge cache: {exc}", "error")

    def _on_close(self):
        if self.transfer_running:
            self.closing = True
            self.stop_event.set()
            self.status_var.set("Closing after the current file write is safely saved...")
            return
        self.closing = True
        self.destroy()


if __name__ == "__main__":
    app = UpdateClientApp()
    app.mainloop()
