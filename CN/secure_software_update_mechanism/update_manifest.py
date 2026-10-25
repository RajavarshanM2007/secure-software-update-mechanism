"""Shared helpers for the chunk manifest.

The origin server splits an update binary into fixed 1 MiB chunks and builds a
manifest that lists the SHA-256 hash of every chunk and of the whole file. The
manifest is signed with HMAC-SHA256 so that a client (or an edge cache) can
detect if anybody changed the hash list on the way.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path

CHUNK_SIZE = 1024 * 1024  # 1 MiB fixed chunk size
DEFAULT_SIGNING_KEY = "BCSE308P-secure-update-demo-key"


def get_signing_key() -> bytes:
    """Read the shared signing key (can be overridden with an environment variable)."""
    return os.environ.get("UPDATE_SIGNING_KEY", DEFAULT_SIGNING_KEY).encode("utf-8")


def canonical_bytes(manifest_body: dict) -> bytes:
    """Same dictionary must always give the same bytes, otherwise the HMAC changes."""
    return json.dumps(manifest_body, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign_manifest(manifest_body: dict, key: bytes | None = None) -> str:
    key = key if key is not None else get_signing_key()
    return hmac.new(key, canonical_bytes(manifest_body), hashlib.sha256).hexdigest()


def verify_manifest_signature(manifest: dict, key: bytes | None = None) -> bool:
    signature = manifest.get("signature")
    if not isinstance(signature, str):
        return False
    body = {name: value for name, value in manifest.items() if name != "signature"}
    expected = sign_manifest(body, key)
    return hmac.compare_digest(signature, expected)


def build_manifest(file_path: Path, version: str, chunk_size: int = CHUNK_SIZE) -> dict:
    """Read the file once and record offset, length and SHA-256 for every chunk."""
    total_size = file_path.stat().st_size
    whole_file_hash = hashlib.sha256()
    chunks = []
    with file_path.open("rb") as source_file:
        index = 0
        while True:
            data = source_file.read(chunk_size)
            if not data:
                break
            whole_file_hash.update(data)
            chunks.append(
                {
                    "index": index,
                    "offset": index * chunk_size,
                    "length": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
            )
            index += 1

    body = {
        "filename": file_path.name,
        "version": version,
        "size": total_size,
        "sha256": whole_file_hash.hexdigest(),
        "chunk_size": chunk_size,
        "chunk_count": len(chunks),
        "chunks": chunks,
    }
    body["signature"] = sign_manifest(body)
    return body


def validate_manifest(manifest: dict) -> dict:
    """Check the signature first and then check that the chunk table is consistent."""
    if not verify_manifest_signature(manifest):
        raise RuntimeError("Manifest signature is invalid. The manifest may have been modified.")
    try:
        size = int(manifest["size"])
        chunk_size = int(manifest["chunk_size"])
        chunk_count = int(manifest["chunk_count"])
        chunks = manifest["chunks"]
        file_hash = str(manifest["sha256"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("Manifest is missing required fields.") from exc

    if size <= 0 or chunk_size <= 0 or chunk_count != len(chunks):
        raise RuntimeError("Manifest size or chunk count is not consistent.")
    if chunk_count != (size + chunk_size - 1) // chunk_size:
        raise RuntimeError("Manifest chunk count does not match the file size.")
    if len(file_hash) != 64:
        raise RuntimeError("Manifest file hash is not a SHA-256 value.")

    expected_offset = 0
    for position, chunk in enumerate(chunks):
        if chunk.get("index") != position or chunk.get("offset") != expected_offset:
            raise RuntimeError(f"Manifest entry {position} has a wrong index or offset.")
        length = chunk.get("length")
        if not isinstance(length, int) or length <= 0 or length > chunk_size:
            raise RuntimeError(f"Manifest entry {position} has an invalid length.")
        if len(str(chunk.get("sha256", ""))) != 64:
            raise RuntimeError(f"Manifest entry {position} has an invalid hash.")
        expected_offset += length

    if expected_offset != size:
        raise RuntimeError("Chunk lengths in the manifest do not add up to the file size.")
    return manifest


def chunk_hash_matches(data: bytes, chunk_entry: dict) -> bool:
    return len(data) == chunk_entry["length"] and hashlib.sha256(data).hexdigest() == chunk_entry["sha256"]
