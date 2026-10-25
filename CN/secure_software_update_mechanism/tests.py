import hashlib
import threading
import tempfile
import unittest
from pathlib import Path

import requests

import edge_cache
import server
import update_manifest
from client import (
    ChunkIntegrityError,
    DownloadInterruptedError,
    bitmap_to_text,
    build_download_headers,
    download_missing_chunks,
    finalize_chunked_download,
    load_bitmap_state,
    missing_chunks,
    new_bitmap,
    prepare_chunked_part,
    save_bitmap_state,
    finalize_download,
    parse_content_range,
    save_response_to_part,
    validate_download_response,
    verify_download_file,
)


class FakeResponse:
    def __init__(self, status_code, headers, chunks=(), failure=None):
        self.status_code = status_code
        self.headers = headers
        self.chunks = chunks
        self.failure = failure

    def iter_content(self, chunk_size):
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        for chunk in self.chunks:
            yield chunk
        if self.failure is not None:
            raise self.failure


class SecureUpdateServerTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.original_updates_directory = server.UPDATES_DIR
        server.UPDATES_DIR = Path(self.temporary_directory.name)
        self.filename = "small_update.bin"
        self.payload = bytes(range(256)) * 8
        (server.UPDATES_DIR / self.filename).write_bytes(self.payload)
        self.client = server.app.test_client()

    def tearDown(self):
        server.UPDATES_DIR = self.original_updates_directory
        self.temporary_directory.cleanup()

    def test_health_endpoint(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"status": "ok"})

    def test_metadata_retrieval_returns_size_and_sha256(self):
        response = self.client.get("/metadata", query_string={"filename": self.filename})
        metadata = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(metadata["filename"], self.filename)
        self.assertEqual(metadata["version"], server.SAMPLE_VERSION)
        self.assertEqual(metadata["size"], len(self.payload))
        self.assertEqual(metadata["sha256"], hashlib.sha256(self.payload).hexdigest())

    def test_normal_download_returns_complete_file_with_200(self):
        response = self.client.get(f"/download/{self.filename}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_data(), self.payload)
        self.assertEqual(int(response.headers["Content-Length"]), len(self.payload))
        self.assertEqual(response.headers["Accept-Ranges"], "bytes")

    def test_valid_range_returns_206_and_correct_headers(self):
        start = 73
        response = self.client.get(
            f"/download/{self.filename}",
            headers={"Range": f"bytes={start}-"},
        )
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.headers["Content-Range"], f"bytes {start}-{len(self.payload) - 1}/{len(self.payload)}")
        self.assertEqual(int(response.headers["Content-Length"]), len(self.payload) - start)
        self.assertEqual(response.headers["Accept-Ranges"], "bytes")
        self.assertEqual(response.get_data(), self.payload[start:])

    def test_invalid_range_returns_416(self):
        response = self.client.get(
            f"/download/{self.filename}",
            headers={"Range": f"bytes={len(self.payload)}-"},
        )
        self.assertEqual(response.status_code, 416)
        self.assertEqual(response.headers["Content-Range"], f"bytes */{len(self.payload)}")

    def test_filename_validation_rejects_traversal(self):
        with self.assertRaises(ValueError):
            server.safe_validate_filename("../secret.txt")
        with self.assertRaises(ValueError):
            server.safe_validate_filename("folder\\secret.txt")

    def test_range_parser_accepts_resume_and_rejects_malformed_ranges(self):
        self.assertEqual(server.parse_range_header("bytes=73-", 100), (73, 99))
        self.assertEqual(server.parse_range_header("bytes=-10", 100), (90, 99))
        self.assertIsNone(server.parse_range_header("bytes=0-2,5-7", 100))
        self.assertIsNone(server.parse_range_header("bytes=100-", 100))


class SecureUpdateClientTransferTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary_directory.name)
        self.payload = b"small update contents for transfer tests"
        self.expected_hash = hashlib.sha256(self.payload).hexdigest()
        self.part_file = self.directory / "update.bin.part"

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_resume_from_nonzero_offset_and_verify_complete_file(self):
        offset = 9
        self.part_file.write_bytes(self.payload[:offset])
        response = FakeResponse(
            206,
            {
                "Content-Range": f"bytes {offset}-{len(self.payload) - 1}/{len(self.payload)}",
                "Content-Length": str(len(self.payload) - offset),
            },
            [self.payload[offset:]],
        )

        self.assertEqual(build_download_headers(offset), {"Range": f"bytes={offset}-"})
        final_size = save_response_to_part(response, self.part_file, offset, len(self.payload))
        actual_hash = verify_download_file(self.part_file, len(self.payload), self.expected_hash)
        self.assertEqual(final_size, len(self.payload))
        self.assertEqual(self.part_file.read_bytes(), self.payload)
        self.assertEqual(actual_hash, self.expected_hash)

    def test_incorrect_resume_offset_is_rejected_without_appending(self):
        self.part_file.write_bytes(self.payload[:9])
        response = FakeResponse(
            206,
            {
                "Content-Range": f"bytes 10-{len(self.payload) - 1}/{len(self.payload)}",
                "Content-Length": str(len(self.payload) - 10),
            },
            [self.payload[10:]],
        )
        with self.assertRaisesRegex(RuntimeError, "resumed at byte 10"):
            save_response_to_part(response, self.part_file, 9, len(self.payload))
        self.assertEqual(self.part_file.read_bytes(), self.payload[:9])

    def test_http_200_is_never_appended_to_partial_file(self):
        original_partial = self.payload[:9]
        self.part_file.write_bytes(original_partial)
        response = FakeResponse(
            200,
            {"Content-Length": str(len(self.payload))},
            [self.payload],
        )
        with self.assertRaisesRegex(RuntimeError, "expected HTTP 206"):
            save_response_to_part(response, self.part_file, len(original_partial), len(self.payload))
        self.assertEqual(self.part_file.read_bytes(), original_partial)

    def test_content_range_must_be_fully_valid_and_match_advertised_length(self):
        self.assertEqual(parse_content_range("bytes 5-9/10"), (5, 9, 10))
        with self.assertRaises(RuntimeError):
            parse_content_range("bytes 5-9/10 trailing-data")
        response = FakeResponse(
            206,
            {"Content-Range": "bytes 5-9/10", "Content-Length": "3"},
        )
        with self.assertRaisesRegex(RuntimeError, "Content-Length"):
            validate_download_response(response, 5, 10)

    def test_http_416_is_reported_without_modifying_partial_file(self):
        self.part_file.write_bytes(self.payload[:9])
        response = FakeResponse(
            416,
            {"Content-Range": f"bytes */{len(self.payload)}", "Content-Length": "0"},
        )
        with self.assertRaisesRegex(RuntimeError, "HTTP 416"):
            save_response_to_part(response, self.part_file, 9, len(self.payload))
        self.assertEqual(self.part_file.read_bytes(), self.payload[:9])

    def test_final_size_and_sha256_verification_detects_corruption(self):
        self.part_file.write_bytes(self.payload)
        self.assertEqual(
            verify_download_file(self.part_file, len(self.payload), self.expected_hash),
            self.expected_hash,
        )
        with self.assertRaisesRegex(ValueError, "size mismatch"):
            verify_download_file(self.part_file, len(self.payload) + 1, self.expected_hash)

        self.part_file.write_bytes(self.payload[:-1] + b"!")
        with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
            verify_download_file(self.part_file, len(self.payload), self.expected_hash)

    def test_final_file_is_renamed_only_after_successful_verification(self):
        final_file = self.directory / "update.bin"
        self.part_file.write_bytes(self.payload)
        finalize_download(self.part_file, final_file, len(self.payload), self.expected_hash)
        self.assertFalse(self.part_file.exists())
        self.assertEqual(final_file.read_bytes(), self.payload)

        self.part_file.write_bytes(self.payload[:-1] + b"!")
        with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
            finalize_download(self.part_file, final_file, len(self.payload), self.expected_hash)
        self.assertTrue(self.part_file.exists())

    def test_connection_failure_preserves_bytes_and_allows_resuming(self):
        offset = 9
        self.part_file.write_bytes(self.payload[:offset])
        interrupted_response = FakeResponse(
            206,
            {
                "Content-Range": f"bytes {offset}-{len(self.payload) - 1}/{len(self.payload)}",
                "Content-Length": str(len(self.payload) - offset),
            },
            [self.payload[offset:offset + 4]],
            requests.ConnectionError("simulated server shutdown"),
        )
        with self.assertRaises(requests.ConnectionError):
            save_response_to_part(interrupted_response, self.part_file, offset, len(self.payload))

        saved_offset = self.part_file.stat().st_size
        self.assertEqual(self.part_file.read_bytes(), self.payload[:saved_offset])
        self.assertEqual(build_download_headers(saved_offset), {"Range": f"bytes={saved_offset}-"})

        recovery_response = FakeResponse(
            206,
            {
                "Content-Range": f"bytes {saved_offset}-{len(self.payload) - 1}/{len(self.payload)}",
                "Content-Length": str(len(self.payload) - saved_offset),
            },
            [self.payload[saved_offset:]],
        )
        save_response_to_part(recovery_response, self.part_file, saved_offset, len(self.payload))
        verify_download_file(self.part_file, len(self.payload), self.expected_hash)
        self.assertEqual(self.part_file.read_bytes(), self.payload)

    def test_interrupt_event_preserves_existing_partial_bytes(self):
        self.part_file.write_bytes(self.payload[:9])
        stop_event = threading.Event()
        stop_event.set()
        response = FakeResponse(
            206,
            {
                "Content-Range": f"bytes 9-{len(self.payload) - 1}/{len(self.payload)}",
                "Content-Length": str(len(self.payload) - 9),
            },
            [self.payload[9:]],
        )
        with self.assertRaises(DownloadInterruptedError):
            save_response_to_part(response, self.part_file, 9, len(self.payload), stop_event=stop_event)
        self.assertEqual(self.part_file.read_bytes(), self.payload[:9])


class ManifestAndChunkTests(unittest.TestCase):
    """Tests for the 1 MiB chunk manifest, its HMAC signature and the bitmap resume."""

    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary_directory.name)
        self.original_updates_directory = server.UPDATES_DIR
        self.original_delay = server.STREAM_DELAY_SECONDS
        server.UPDATES_DIR = self.directory
        server.STREAM_DELAY_SECONDS = 0
        server.manifest_cache.clear()
        self.filename = "chunk_update.bin"
        # 3.5 chunks of 1 MiB, so the last chunk is shorter than the others.
        self.payload = bytes(range(256)) * (4096 * 3 + 2048)
        (self.directory / self.filename).write_bytes(self.payload)
        self.client = server.app.test_client()

    def tearDown(self):
        server.UPDATES_DIR = self.original_updates_directory
        server.STREAM_DELAY_SECONDS = self.original_delay
        server.manifest_cache.clear()
        self.temporary_directory.cleanup()

    def get_manifest(self):
        response = self.client.get("/manifest", query_string={"filename": self.filename})
        self.assertEqual(response.status_code, 200)
        return response.get_json()

    def test_manifest_lists_every_chunk_with_hash(self):
        manifest = self.get_manifest()
        self.assertEqual(manifest["chunk_size"], update_manifest.CHUNK_SIZE)
        self.assertEqual(manifest["chunk_count"], 4)
        self.assertEqual(manifest["chunks"][3]["length"], len(self.payload) - 3 * update_manifest.CHUNK_SIZE)
        self.assertEqual(manifest["sha256"], hashlib.sha256(self.payload).hexdigest())
        update_manifest.validate_manifest(manifest)

    def test_tampered_manifest_is_rejected(self):
        manifest = self.get_manifest()
        manifest["chunks"][1]["sha256"] = "0" * 64
        with self.assertRaisesRegex(RuntimeError, "signature"):
            update_manifest.validate_manifest(manifest)

    def test_chunk_endpoint_returns_exact_bytes(self):
        manifest = self.get_manifest()
        response = self.client.get(f"/chunk/{self.filename}/2")
        self.assertEqual(response.status_code, 200)
        data = response.get_data()
        self.assertEqual(data, self.payload[2 * update_manifest.CHUNK_SIZE:3 * update_manifest.CHUNK_SIZE])
        self.assertTrue(update_manifest.chunk_hash_matches(data, manifest["chunks"][2]))
        self.assertEqual(self.client.get(f"/chunk/{self.filename}/9").status_code, 404)

    def fetch_from_origin_test_client(self, _server_url, filename, index):
        response = self.client.get(f"/chunk/{filename}/{index}")
        return response.get_data(), "ORIGIN"

    def test_bitmap_resume_requests_only_missing_chunks(self):
        manifest = self.get_manifest()
        part_file = self.directory / "out.chunked.part"
        state_file = self.directory / "out.bitmap.json"
        prepare_chunked_part(part_file, manifest["size"])
        bitmap = new_bitmap(manifest["chunk_count"])
        stop_event = threading.Event()

        def stop_after_two(_downloaded, current_bitmap, _status):
            if sum(current_bitmap) == 2:
                stop_event.set()

        with self.assertRaises(DownloadInterruptedError):
            download_missing_chunks(
                "unused", manifest, part_file, state_file, bitmap,
                stop_event=stop_event, progress_callback=stop_after_two,
                fetch_chunk=self.fetch_from_origin_test_client,
            )
        saved = load_bitmap_state(state_file, manifest)
        self.assertEqual(bitmap_to_text(saved), "1100")
        self.assertEqual(missing_chunks(saved), [2, 3])

        summary = download_missing_chunks(
            "unused", manifest, part_file, state_file, saved,
            fetch_chunk=self.fetch_from_origin_test_client,
        )
        self.assertEqual(summary["requested"], [2, 3])
        final_file = self.directory / "out.bin"
        finalize_chunked_download(part_file, state_file, final_file, manifest)
        self.assertEqual(final_file.read_bytes(), self.payload)
        self.assertFalse(state_file.exists())

    def test_corrupted_chunk_is_never_marked_received(self):
        manifest = self.get_manifest()
        part_file = self.directory / "bad.chunked.part"
        state_file = self.directory / "bad.bitmap.json"
        prepare_chunked_part(part_file, manifest["size"])
        bitmap = new_bitmap(manifest["chunk_count"])

        def corrupt_fetch(_server_url, filename, index):
            data = self.client.get(f"/chunk/{filename}/{index}").get_data()
            return b"X" + data[1:], "ORIGIN"

        with self.assertRaises(ChunkIntegrityError):
            download_missing_chunks(
                "unused", manifest, part_file, state_file, bitmap, fetch_chunk=corrupt_fetch,
            )
        self.assertEqual(bitmap, [0, 0, 0, 0])

    def test_bitmap_from_other_version_is_rejected(self):
        manifest = self.get_manifest()
        state_file = self.directory / "old.bitmap.json"
        save_bitmap_state(state_file, manifest, [1, 0, 0, 0])
        changed = dict(manifest, sha256="f" * 64)
        with self.assertRaisesRegex(RuntimeError, "different update version"):
            load_bitmap_state(state_file, changed)


class EdgeCacheTests(unittest.TestCase):
    """The edge cache must fetch a chunk from the origin once and then serve HITs."""

    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        directory = Path(self.temporary_directory.name)
        self.original = (server.UPDATES_DIR, server.STREAM_DELAY_SECONDS, edge_cache.CACHE_DIR,
                         edge_cache.fetch_manifest_from_origin, edge_cache.fetch_chunk_from_origin)
        server.UPDATES_DIR = directory / "updates"
        server.UPDATES_DIR.mkdir()
        server.STREAM_DELAY_SECONDS = 0
        server.manifest_cache.clear()
        edge_cache.CACHE_DIR = directory / "edge"
        edge_cache.manifest_store.clear()
        edge_cache.reset_stats()
        self.filename = "edge_update.bin"
        self.payload = bytes(range(256)) * (4096 * 2 + 100)
        (server.UPDATES_DIR / self.filename).write_bytes(self.payload)
        origin = server.app.test_client()
        self.origin_calls = []

        def fake_manifest(filename):
            return origin.get("/manifest", query_string={"filename": filename}).get_json()

        def fake_chunk(filename, index):
            self.origin_calls.append(index)
            return origin.get(f"/chunk/{filename}/{index}").get_data()

        edge_cache.fetch_manifest_from_origin = fake_manifest
        edge_cache.fetch_chunk_from_origin = fake_chunk
        self.edge = edge_cache.app.test_client()

    def tearDown(self):
        (server.UPDATES_DIR, server.STREAM_DELAY_SECONDS, edge_cache.CACHE_DIR,
         edge_cache.fetch_manifest_from_origin, edge_cache.fetch_chunk_from_origin) = self.original
        server.manifest_cache.clear()
        edge_cache.manifest_store.clear()
        self.temporary_directory.cleanup()

    def test_first_request_is_miss_then_hit(self):
        self.assertEqual(self.edge.get("/manifest", query_string={"filename": self.filename}).status_code, 200)
        first = self.edge.get(f"/chunk/{self.filename}/0")
        second = self.edge.get(f"/chunk/{self.filename}/0")
        self.assertEqual(first.headers["X-Cache"], "MISS")
        self.assertEqual(second.headers["X-Cache"], "HIT")
        self.assertEqual(second.get_data(), self.payload[:update_manifest.CHUNK_SIZE])
        self.assertEqual(self.origin_calls, [0])
        stats = self.edge.get("/stats").get_json()
        self.assertEqual((stats["hits"], stats["misses"]), (1, 1))

    def test_clear_cache_removes_chunks_and_resets_stats(self):
        self.edge.get("/manifest", query_string={"filename": self.filename})
        self.edge.get(f"/chunk/{self.filename}/0")
        cleared = self.edge.post("/cache/clear").get_json()
        self.assertEqual(cleared["removed_chunks"], 1)
        self.assertEqual(self.edge.get("/stats").get_json()["misses"], 0)
        self.assertEqual(self.edge.get(f"/chunk/{self.filename}/0").headers["X-Cache"], "MISS")

    def test_damaged_cache_file_is_replaced_from_origin(self):
        self.edge.get("/manifest", query_string={"filename": self.filename})
        self.edge.get(f"/chunk/{self.filename}/1")
        manifest = edge_cache.manifest_store[self.filename]
        cached = edge_cache.cache_path_for(manifest, 1)
        cached.write_bytes(b"damaged")
        again = self.edge.get(f"/chunk/{self.filename}/1")
        self.assertEqual(again.headers["X-Cache"], "MISS")
        self.assertEqual(self.origin_calls, [1, 1])


if __name__ == "__main__":
    unittest.main()
