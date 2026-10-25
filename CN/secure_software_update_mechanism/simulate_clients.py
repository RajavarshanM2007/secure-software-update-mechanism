"""Experiment script: compares direct origin downloads with downloads through the edge cache.

It starts the origin server (port 5000) and the edge cache (port 5001) inside this
process, then runs several headless clients that use the same functions as the GUI
client. Results are printed and saved to results/simulation_results.json.

Run:  python simulate_clients.py --clients 5
"""

from __future__ import annotations

import argparse
import json
import shutil
import tempfile
import threading
import time
from pathlib import Path

import requests
from werkzeug.serving import make_server

import edge_cache
import server
from client import (
    DownloadInterruptedError,
    download_missing_chunks,
    fetch_manifest,
    finalize_chunked_download,
    get_bitmap_state_path,
    get_chunked_part_path,
    load_bitmap_state,
    missing_chunks,
    new_bitmap,
    prepare_chunked_part,
    save_bitmap_state,
)

ORIGIN_PORT = 5000
EDGE_PORT = 5001
ORIGIN_URL = f"http://127.0.0.1:{ORIGIN_PORT}"
EDGE_URL = f"http://127.0.0.1:{EDGE_PORT}"
FILENAME = server.SAMPLE_FILENAME
MIB = 1024 * 1024


class BackgroundServer(threading.Thread):
    def __init__(self, app, port):
        super().__init__(daemon=True)
        self.http_server = make_server("127.0.0.1", port, app, threaded=True)

    def run(self):
        self.http_server.serve_forever()

    def stop(self):
        self.http_server.shutdown()


def run_one_client(base_url: str, download_dir: Path, stop_after: int | None = None) -> dict:
    """Download the update once (optionally stop after N chunks) and return timing details."""
    download_dir.mkdir(parents=True, exist_ok=True)
    part_file = get_chunked_part_path(download_dir, FILENAME)
    state_file = get_bitmap_state_path(download_dir, FILENAME)
    stop_event = threading.Event()

    started = time.perf_counter()
    manifest = fetch_manifest(base_url, FILENAME)
    bitmap = load_bitmap_state(state_file, manifest) or new_bitmap(manifest["chunk_count"])
    prepare_chunked_part(part_file, manifest["size"])
    save_bitmap_state(state_file, manifest, bitmap)
    missing_before = missing_chunks(bitmap)

    def on_progress(_downloaded, current_bitmap, _cache_status):
        if stop_after is not None and sum(current_bitmap) >= stop_after:
            stop_event.set()

    interrupted = False
    summary = {"requested": [], "hits": 0, "misses": 0, "origin": 0, "retries": 0}
    try:
        summary = download_missing_chunks(
            base_url, manifest, part_file, state_file, bitmap,
            stop_event=stop_event, progress_callback=on_progress,
        )
    except DownloadInterruptedError:
        interrupted = True
        summary["requested"] = [index for index in missing_before if bitmap[index]]

    if not interrupted:
        finalize_chunked_download(part_file, state_file, download_dir / FILENAME, manifest)

    elapsed = time.perf_counter() - started
    return {
        "seconds": round(elapsed, 2),
        "chunks_requested": len(summary["requested"]),
        "first_requested": summary["requested"][0] if summary["requested"] else None,
        "hits": summary["hits"],
        "misses": summary["misses"],
        "origin": summary["origin"],
        "interrupted": interrupted,
        "bitmap": "".join(str(bit) for bit in bitmap),
        "verified": not interrupted,
    }


def reset_counters(cache_dir: Path):
    server.reset_stats()
    edge_cache.reset_stats()
    shutil.rmtree(cache_dir, ignore_errors=True)
    cache_dir.mkdir(parents=True, exist_ok=True)


def origin_bytes() -> int:
    return requests.get(f"{ORIGIN_URL}/stats", timeout=5).json()["chunk_bytes_sent"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--clients", type=int, default=5)
    parser.add_argument("--delay", type=float, default=0.02, help="origin delay per 64 KiB piece")
    args = parser.parse_args()

    server.STREAM_DELAY_SECONDS = args.delay
    workdir = Path(tempfile.mkdtemp(prefix="ssum_sim_"))
    edge_cache.CACHE_DIR = workdir / "edge_cache_store"
    edge_cache.ORIGIN_URL = ORIGIN_URL

    origin_thread = BackgroundServer(server.app, ORIGIN_PORT)
    edge_thread = BackgroundServer(edge_cache.app, EDGE_PORT)
    origin_thread.start()
    edge_thread.start()
    time.sleep(0.5)

    file_size = (server.UPDATES_DIR / FILENAME).stat().st_size
    results = {"clients": args.clients, "file_size": file_size, "chunk_size": MIB, "delay": args.delay}

    try:
        # Scenario 1: every client goes straight to the origin server.
        reset_counters(edge_cache.CACHE_DIR)
        direct = [run_one_client(ORIGIN_URL, workdir / f"direct_{n}") for n in range(args.clients)]
        results["direct"] = {"per_client": direct, "origin_bytes": origin_bytes()}

        # Scenario 2: same clients, one after another, through the edge cache (cold cache at start).
        reset_counters(edge_cache.CACHE_DIR)
        via_edge = [run_one_client(EDGE_URL, workdir / f"edge_{n}") for n in range(args.clients)]
        edge_snapshot = requests.get(f"{EDGE_URL}/stats", timeout=5).json()
        results["edge_sequential"] = {
            "per_client": via_edge,
            "origin_bytes": origin_bytes(),
            "edge_stats": edge_snapshot,
        }

        # Scenario 3: all clients start together through a cold edge cache.
        reset_counters(edge_cache.CACHE_DIR)
        concurrent = [None] * args.clients

        def worker(slot):
            concurrent[slot] = run_one_client(EDGE_URL, workdir / f"concurrent_{slot}")

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(args.clients)]
        started = time.perf_counter()
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        results["edge_concurrent"] = {
            "per_client": concurrent,
            "wall_seconds": round(time.perf_counter() - started, 2),
            "origin_bytes": origin_bytes(),
            "edge_stats": requests.get(f"{EDGE_URL}/stats", timeout=5).json(),
        }

        # Scenario 4: interrupt after 10 chunks, then resume with the saved bitmap.
        reset_counters(edge_cache.CACHE_DIR)
        resume_dir = workdir / "resume_client"
        first = run_one_client(EDGE_URL, resume_dir, stop_after=10)
        second = run_one_client(EDGE_URL, resume_dir)
        results["resume"] = {"first_attempt": first, "second_attempt": second}

    finally:
        edge_thread.stop()
        origin_thread.stop()
        shutil.rmtree(workdir, ignore_errors=True)

    # ------------------------------------------------------------------ report
    total_served = file_size * args.clients
    print(f"\nFile: {FILENAME}  size = {file_size} bytes  chunk = 1 MiB  clients = {args.clients}\n")

    print("Scenario 1 - direct to origin")
    for number, row in enumerate(results["direct"]["per_client"], 1):
        print(f"  client {number}: {row['seconds']:6.2f} s, chunks from origin = {row['origin']}")
    print(f"  origin bytes sent = {results['direct']['origin_bytes']}")

    print("\nScenario 2 - through edge cache (sequential)")
    for number, row in enumerate(results["edge_sequential"]["per_client"], 1):
        print(f"  client {number}: {row['seconds']:6.2f} s, HIT = {row['hits']:2d}, MISS = {row['misses']:2d}")
    seq_origin = results["edge_sequential"]["origin_bytes"]
    offload = 1 - seq_origin / total_served
    results["edge_sequential"]["offload_percent"] = round(offload * 100, 2)
    print(f"  origin bytes sent = {seq_origin}, bytes delivered to clients = {total_served}")
    print(f"  origin offload = {offload * 100:.1f} %")

    print("\nScenario 3 - through edge cache (concurrent start)")
    conc = results["edge_concurrent"]
    print(f"  wall time = {conc['wall_seconds']} s, origin bytes sent = {conc['origin_bytes']}")
    print(f"  edge stats = {conc['edge_stats']}")
    results["edge_concurrent"]["offload_percent"] = round((1 - conc["origin_bytes"] / total_served) * 100, 2)

    print("\nScenario 4 - interrupt and resume")
    res = results["resume"]
    print(f"  first attempt : {res['first_attempt']['chunks_requested']} chunks, bitmap {res['first_attempt']['bitmap']}")
    print(
        f"  second attempt: {res['second_attempt']['chunks_requested']} chunks requested, "
        f"first requested index = {res['second_attempt']['first_requested']}, verified = {res['second_attempt']['verified']}"
    )

    output = Path(__file__).resolve().parent / "results"
    output.mkdir(exist_ok=True)
    (output / "simulation_results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\nSaved results to {output / 'simulation_results.json'}")


if __name__ == "__main__":
    main()
