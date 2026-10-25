"""Edge cache node for the Secure Software Update Mechanism.

The edge node sits on the local network (LAN) between the clients and the origin
server. Clients ask the edge for the manifest and for chunks. On a cache HIT the
chunk is served straight from the edge's disk. On a MISS the edge downloads the
chunk once from the origin, checks its SHA-256 against the signed manifest,
stores it and then serves it. The next client on the same LAN gets a HIT.
"""

from __future__ import annotations

import os
import shutil
import threading
from pathlib import Path
from urllib.parse import quote

import requests
from flask import Flask, Response, jsonify, request

from update_manifest import chunk_hash_matches, validate_manifest

BASE_DIR = Path(__file__).resolve().parent
CACHE_DIR = BASE_DIR / "edge_cache_store"
ORIGIN_URL = os.environ.get("ORIGIN_URL", "http://127.0.0.1:5000")
EDGE_PORT = int(os.environ.get("EDGE_PORT", "5001"))

CACHE_DIR.mkdir(exist_ok=True, parents=True)

app = Flask(__name__)

stats_lock = threading.Lock()
edge_stats = {
    "hits": 0,
    "misses": 0,
    "bytes_from_origin": 0,
    "bytes_served": 0,
    "manifest_requests": 0,
}

# One lock per chunk so that two clients missing the same chunk at the same time
# cause only one origin fetch (the second one waits and then gets a HIT).
chunk_locks: dict[str, threading.Lock] = {}
chunk_locks_guard = threading.Lock()

manifest_store: dict[str, dict] = {}
manifest_store_lock = threading.Lock()


def add_stat(name: str, amount: int = 1) -> None:
    with stats_lock:
        edge_stats[name] += amount


def reset_stats() -> None:
    with stats_lock:
        for name in edge_stats:
            edge_stats[name] = 0


def lock_for(key: str) -> threading.Lock:
    with chunk_locks_guard:
        if key not in chunk_locks:
            chunk_locks[key] = threading.Lock()
        return chunk_locks[key]


def fetch_manifest_from_origin(filename: str) -> dict:
    url = f"{ORIGIN_URL.rstrip('/')}/manifest?filename={quote(filename, safe='')}"
    response = requests.get(url, timeout=(5, 30))
    response.raise_for_status()
    return response.json()


def fetch_chunk_from_origin(filename: str, index: int) -> bytes:
    url = f"{ORIGIN_URL.rstrip('/')}/chunk/{quote(filename, safe='')}/{index}"
    response = requests.get(url, timeout=(5, 30))
    response.raise_for_status()
    return response.content


def get_verified_manifest(filename: str) -> dict:
    """The edge always re-checks the signature before trusting the chunk hashes."""
    manifest = validate_manifest(fetch_manifest_from_origin(filename))
    with manifest_store_lock:
        manifest_store[filename] = manifest
    return manifest


def known_manifest(filename: str) -> dict:
    with manifest_store_lock:
        manifest = manifest_store.get(filename)
    return manifest if manifest is not None else get_verified_manifest(filename)


def cache_path_for(manifest: dict, index: int) -> Path:
    # The folder name comes from the file hash, so a new version never reuses old chunks.
    version_folder = CACHE_DIR / f"{manifest['filename']}-{manifest['sha256'][:16]}"
    return version_folder / f"{index:05d}.chunk"


def read_cached_chunk(path: Path, entry: dict) -> bytes | None:
    if not path.exists():
        return None
    data = path.read_bytes()
    if chunk_hash_matches(data, entry):
        return data
    # A damaged cached chunk is removed and treated as a miss.
    path.unlink(missing_ok=True)
    return None


def store_chunk(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(".tmp")
    temp_path.write_bytes(data)
    os.replace(temp_path, path)


def valid_filename(filename: str) -> bool:
    return bool(filename) and filename not in {".", ".."} and "/" not in filename and "\\" not in filename


@app.route("/manifest", methods=["GET"])
def manifest_endpoint():
    filename = request.args.get("filename", "sample_update.bin")
    if not valid_filename(filename):
        return jsonify({"error": "Filename is invalid."}), 400
    try:
        manifest = get_verified_manifest(filename)
    except requests.RequestException as exc:
        return jsonify({"error": f"Origin server not reachable: {exc}"}), 502
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 502
    add_stat("manifest_requests")
    print(f"[edge] Manifest for {filename} relayed ({manifest['chunk_count']} chunks)")
    return jsonify(manifest)


@app.route("/chunk/<path:filename>/<int:index>", methods=["GET"])
def chunk_endpoint(filename: str, index: int):
    if not valid_filename(filename):
        return jsonify({"error": "Filename is invalid."}), 400
    try:
        manifest = known_manifest(filename)
    except (requests.RequestException, RuntimeError) as exc:
        return jsonify({"error": f"Manifest not available: {exc}"}), 502

    if index < 0 or index >= manifest["chunk_count"]:
        return jsonify({"error": f"Chunk index {index} is out of range."}), 404

    entry = manifest["chunks"][index]
    path = cache_path_for(manifest, index)

    with lock_for(str(path)):
        data = read_cached_chunk(path, entry)
        if data is not None:
            cache_status = "HIT"
            add_stat("hits")
        else:
            cache_status = "MISS"
            try:
                data = fetch_chunk_from_origin(filename, index)
            except requests.RequestException as exc:
                return jsonify({"error": f"Origin server not reachable: {exc}"}), 502
            if not chunk_hash_matches(data, entry):
                return jsonify({"error": f"Chunk {index} from origin failed the SHA-256 check."}), 502
            store_chunk(path, data)
            add_stat("misses")
            add_stat("bytes_from_origin", len(data))

    add_stat("bytes_served", len(data))
    print(f"[edge] Chunk {index} of {filename}: cache {cache_status}")
    response = Response(data, status=200, mimetype="application/octet-stream")
    response.headers["Content-Length"] = str(len(data))
    response.headers["X-Chunk-Index"] = str(index)
    response.headers["X-Chunk-SHA256"] = entry["sha256"]
    response.headers["X-Cache"] = cache_status
    response.headers["X-Served-By"] = "edge"
    return response


@app.route("/stats", methods=["GET"])
def stats_endpoint():
    with stats_lock:
        snapshot = dict(edge_stats)
    total = snapshot["hits"] + snapshot["misses"]
    snapshot["hit_ratio"] = round(snapshot["hits"] / total, 4) if total else 0.0
    return jsonify(snapshot)


@app.route("/cache/clear", methods=["POST"])
def clear_cache_endpoint():
    """Delete all cached chunks and reset the counters (used to repeat the demo with a cold cache)."""
    removed = sum(1 for _ in CACHE_DIR.rglob("*.chunk")) if CACHE_DIR.exists() else 0
    shutil.rmtree(CACHE_DIR, ignore_errors=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with manifest_store_lock:
        manifest_store.clear()
    reset_stats()
    print(f"[edge] Cache cleared ({removed} chunks removed)")
    return jsonify({"status": "cleared", "removed_chunks": removed})


@app.route("/health", methods=["GET"])
def health_check():
    return jsonify({"status": "ok", "role": "edge", "origin": ORIGIN_URL})


if __name__ == "__main__":
    print(f"[edge] Edge cache running on port {EDGE_PORT}, origin = {ORIGIN_URL}")
    app.run(host="127.0.0.1", port=EDGE_PORT, debug=False, threaded=True)
