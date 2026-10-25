from __future__ import annotations

import hashlib
import os
import threading
import time
from pathlib import Path
from urllib.parse import quote

from flask import Flask, Response, jsonify, request

from update_manifest import CHUNK_SIZE, build_manifest

BASE_DIR = Path(__file__).resolve().parent
UPDATES_DIR = BASE_DIR / "updates"
DOWNLOADS_DIR = BASE_DIR / "downloads"
SAMPLE_FILENAME = "sample_update.bin"
SAMPLE_VERSION = "1.0.0"
TARGET_SIZE_BYTES = 25 * 1024 * 1024
STREAM_CHUNK_SIZE = 64 * 1024
# Small delay per 64 KiB piece so that a transfer can be interrupted during the demo.
STREAM_DELAY_SECONDS = float(os.environ.get("STREAM_DELAY_SECONDS", "0.02"))

UPDATES_DIR.mkdir(exist_ok=True, parents=True)
DOWNLOADS_DIR.mkdir(exist_ok=True, parents=True)

app = Flask(__name__)

# Counters used to measure how much traffic the origin server really carries.
stats_lock = threading.Lock()
origin_stats = {"chunk_requests": 0, "chunk_bytes_sent": 0, "range_requests": 0, "manifest_requests": 0}

# Building a manifest hashes the whole file, so it is cached until the file changes.
manifest_cache: dict[tuple, dict] = {}
manifest_lock = threading.Lock()


def add_stat(name: str, amount: int = 1) -> None:
    with stats_lock:
        origin_stats[name] += amount


def reset_stats() -> None:
    with stats_lock:
        for name in origin_stats:
            origin_stats[name] = 0


def get_manifest(target_path: Path) -> dict:
    file_info = target_path.stat()
    cache_key = (target_path.name, file_info.st_size, file_info.st_mtime_ns, CHUNK_SIZE)
    with manifest_lock:
        manifest = manifest_cache.get(cache_key)
        if manifest is None:
            manifest = build_manifest(target_path, SAMPLE_VERSION, CHUNK_SIZE)
            manifest_cache.clear()
            manifest_cache[cache_key] = manifest
    return manifest


def safe_validate_filename(filename: str) -> str:
    """Reject any path that tries to escape the updates directory."""
    if not filename:
        raise ValueError("Filename is required.")

    safe_name = filename.strip()
    if not safe_name or safe_name in {".", ".."}:
        raise ValueError("Filename is invalid.")

    if "/" in safe_name or "\\" in safe_name or ".." in Path(safe_name).parts:
        raise ValueError("Filename contains invalid path components.")

    candidate = (UPDATES_DIR / safe_name).resolve(strict=False)
    base = UPDATES_DIR.resolve()
    if candidate != base and base not in candidate.parents:
        raise ValueError("Filename escapes the update directory.")

    return safe_name


def calculate_sha256(file_path: Path) -> str:
    hash_object = hashlib.sha256()
    with file_path.open("rb") as source_file:
        while True:
            chunk = source_file.read(65536)
            if not chunk:
                break
            hash_object.update(chunk)
    return hash_object.hexdigest()


def generate_sample_update(file_path: Path, target_size: int = TARGET_SIZE_BYTES) -> None:
    pattern = (
        b"SecureSoftwareUpdatePayload-0123456789ABCDEF-" 
        b"ComputerNetworksLab-ThisIsAStudentDemo-"
    )
    with file_path.open("wb") as destination:
        remaining = target_size
        while remaining > 0:
            chunk = pattern * 64
            write_count = min(len(chunk), remaining)
            destination.write(chunk[:write_count])
            remaining -= write_count


def ensure_sample_update_exists() -> None:
    sample_path = UPDATES_DIR / SAMPLE_FILENAME
    if not sample_path.exists():
        print(f"[server] Creating sample update file: {sample_path}")
        generate_sample_update(sample_path)


def parse_range_header(range_header: str, total_size: int):
    """Return (start, end) for a valid Range request; otherwise return None."""
    if not range_header or total_size <= 0:
        return None

    header = range_header.strip().lower()
    if not header.startswith("bytes="):
        return None

    value = range_header[6:].strip()
    if not value or "," in value or value.count("-") != 1:
        return None

    if value.startswith("-"):
        suffix_text = value[1:]
        if not suffix_text.isdigit():
            return None
        try:
            suffix_length = int(suffix_text)
        except ValueError:
            return None
        if suffix_length <= 0:
            return None
        start = max(total_size - suffix_length, 0)
        end = total_size - 1
        return start, end

    if "-" not in value:
        return None

    start_text, end_text = value.split("-", 1)
    if not start_text.isdigit() or (end_text and not end_text.isdigit()):
        return None
    try:
        start = int(start_text)
    except ValueError:
        return None

    if start < 0:
        return None

    if start >= total_size:
        return None

    if end_text:
        try:
            end = int(end_text)
        except ValueError:
            return None
    else:
        end = total_size - 1

    if end < start:
        return None

    end = min(end, total_size - 1)
    return start, end


@app.route("/metadata", methods=["GET"])
def metadata_endpoint():
    requested_name = request.args.get("filename", SAMPLE_FILENAME)
    try:
        safe_name = safe_validate_filename(requested_name)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    target_path = UPDATES_DIR / safe_name
    if not target_path.exists():
        return jsonify({"error": "Update file not found."}), 404

    metadata = {
        "filename": safe_name,
        "version": SAMPLE_VERSION,
        "size": target_path.stat().st_size,
        "sha256": calculate_sha256(target_path),
    }
    print(f"[server] Metadata requested for {safe_name}: size={metadata['size']} bytes")
    return jsonify(metadata)


@app.route("/download/<path:filename>", methods=["GET"])
def download_endpoint(filename: str):
    try:
        safe_name = safe_validate_filename(filename)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    target_path = UPDATES_DIR / safe_name
    if not target_path.exists():
        return jsonify({"error": "Update file not found."}), 404

    total_size = target_path.stat().st_size
    range_header = request.headers.get("Range", "")
    print(f"[server] Download request for {safe_name}. Range header: {range_header or 'none'}")

    if range_header:
        range_tuple = parse_range_header(range_header, total_size)
        if range_tuple is None:
            response = Response(status=416)
            response.headers["Content-Range"] = f"bytes */{total_size}"
            response.headers["Accept-Ranges"] = "bytes"
            print(f"[server] Invalid Range request for {safe_name}: {range_header}")
            return response
        start, end = range_tuple
        status_code = 206
        add_stat("range_requests")
    else:
        start, end = 0, total_size - 1
        status_code = 200

    length = end - start + 1
    print(f"[server] Serving bytes {start}-{end} with HTTP {status_code}, length={length}")

    def stream_file():
        with target_path.open("rb") as source_file:
            source_file.seek(start)
            remaining = length
            while remaining > 0:
                chunk = source_file.read(min(STREAM_CHUNK_SIZE, remaining))
                if not chunk:
                    break
                time.sleep(STREAM_DELAY_SECONDS)
                yield chunk
                remaining -= len(chunk)

    response = Response(
        stream_file(),
        status=status_code,
        mimetype="application/octet-stream",
    )
    response.headers["Content-Length"] = str(length)
    response.headers["Accept-Ranges"] = "bytes"
    if status_code == 206:
        response.headers["Content-Range"] = f"bytes {start}-{end}/{total_size}"
    response.headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(safe_name)}"
    return response


@app.route("/manifest", methods=["GET"])
def manifest_endpoint():
    requested_name = request.args.get("filename", SAMPLE_FILENAME)
    try:
        safe_name = safe_validate_filename(requested_name)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    target_path = UPDATES_DIR / safe_name
    if not target_path.exists():
        return jsonify({"error": "Update file not found."}), 404

    manifest = get_manifest(target_path)
    add_stat("manifest_requests")
    print(
        f"[server] Manifest requested for {safe_name}: "
        f"{manifest['chunk_count']} chunks of {manifest['chunk_size']} bytes"
    )
    return jsonify(manifest)


@app.route("/chunk/<path:filename>/<int:index>", methods=["GET"])
def chunk_endpoint(filename: str, index: int):
    try:
        safe_name = safe_validate_filename(filename)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    target_path = UPDATES_DIR / safe_name
    if not target_path.exists():
        return jsonify({"error": "Update file not found."}), 404

    manifest = get_manifest(target_path)
    if index < 0 or index >= manifest["chunk_count"]:
        return jsonify({"error": f"Chunk index {index} is out of range."}), 404

    entry = manifest["chunks"][index]
    print(f"[server] Chunk {index} requested for {safe_name} (offset={entry['offset']}, length={entry['length']})")
    add_stat("chunk_requests")

    def stream_chunk():
        with target_path.open("rb") as source_file:
            source_file.seek(entry["offset"])
            remaining = entry["length"]
            while remaining > 0:
                piece = source_file.read(min(STREAM_CHUNK_SIZE, remaining))
                if not piece:
                    break
                time.sleep(STREAM_DELAY_SECONDS)
                add_stat("chunk_bytes_sent", len(piece))
                yield piece
                remaining -= len(piece)

    response = Response(stream_chunk(), status=200, mimetype="application/octet-stream")
    response.headers["Content-Length"] = str(entry["length"])
    response.headers["X-Chunk-Index"] = str(index)
    response.headers["X-Chunk-SHA256"] = entry["sha256"]
    response.headers["X-Served-By"] = "origin"
    return response


@app.route("/stats", methods=["GET"])
def stats_endpoint():
    with stats_lock:
        return jsonify(dict(origin_stats))


@app.route("/health", methods=["GET"])
def health_check():
    return jsonify({"status": "ok"})


ensure_sample_update_exists()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
