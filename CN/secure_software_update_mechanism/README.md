# Secure Software Update Mechanism

## What is new in this version (chunked distribution with edge caching)

The project now has two transfer modes:

1. **Chunked via edge cache (default in the GUI).** The origin server splits the update into fixed 1 MiB chunks and publishes a manifest (`/manifest`) that lists the offset, length and SHA-256 of every chunk plus the whole-file SHA-256. The manifest is signed with HMAC-SHA256 (`update_manifest.py`). The edge cache node (`edge_cache.py`, port 5001) serves chunks to clients on the LAN: on a cache MISS it downloads the chunk once from the origin, checks its SHA-256 against the signed manifest and stores it; later requests are cache HITs. The client keeps a bitmap (`downloads/<file>.bitmap.json`, 1 = received, 0 = missing) and, after an interruption, requests only the missing chunks.
2. **Direct HTTP Range (origin).** The original mode described below: resume with `Range: bytes=N-` and `206 Partial Content`.

Run three terminals in this folder:

```powershell
python server.py        # origin server, http://127.0.0.1:5000
python edge_cache.py    # edge cache,    http://127.0.0.1:5001
python client.py        # GUI client
```

Experiment (stop the two servers first because it uses the same ports):

```powershell
python simulate_clients.py --clients 5
```

It compares 5 clients downloading directly from the origin with 5 clients going through the edge cache and saves `results/simulation_results.json`. With the 25 MiB sample the origin sends 125 MiB without the cache and 25 MiB with it (80% offload).

New files: `update_manifest.py`, `edge_cache.py`, `simulate_clients.py`. New origin endpoints: `/manifest`, `/chunk/<file>/<index>`, `/stats`. The signing key can be changed with the `UPDATE_SIGNING_KEY` environment variable (all programs must use the same value). HMAC uses a shared key; a production system should use public-key signatures.

The client is a dashboard: online status of origin and edge (polled from `/health`), a colour-coded chunk map (grey = missing, amber = downloading, cyan = edge HIT, violet = edge MISS), a live throughput graph, edge statistics from `/stats` (hit ratio, origin offload), an event log, and buttons to clear the edge cache (`POST /cache/clear`) and reset the local download files.

The test suite now has 25 tests (`python -m unittest tests -v`).

---

## Objective and architecture

This Computer Networks project demonstrates downloading a software update from a local Flask server using HTTP. The Tkinter client receives update metadata, saves transfer data into a `.part` file, resumes a stopped or failed transfer with an HTTP Range request, and checks file size and SHA-256 before renaming the file. The downloaded file is never executed.

The server and client use a simple client-server architecture:

- `server.py` serves metadata and update bytes from `updates/`.
- `client.py` requests the update and stores it under `downloads/`.
- `tests.py` verifies server routes and client-side transfer validation with small temporary files.
- `requirements.txt` lists the third-party packages.

HTTP is the application-layer protocol. HTTP uses TCP here; TCP transports an ordered byte stream. The 64 KiB pieces read and written by Python are application-level file chunks, not TCP segments.

## Technologies

- Python 3
- Flask for the HTTP server
- Requests for HTTP client requests
- Tkinter for the desktop GUI
- `hashlib` SHA-256 for file integrity checking
- Python file handling, threads, queues, and `unittest`

Tkinter is included with many standard Windows Python installations. Flask and Requests are the only third-party dependencies.

## Windows installation and startup

Open PowerShell in this project directory:

```powershell
python -m pip install -r requirements.txt
python server.py
```

The server listens at `http://127.0.0.1:5000`. On its first run, it creates `updates/` and a 25 MiB `sample_update.bin` if the sample is missing. Keep this terminal open.

Open a second PowerShell window in the same project directory:

```powershell
python client.py
```

The default server URL and update filename are already filled in. The client creates `downloads/` automatically.

## Demonstration: interrupt and resume

1. Start the server and then the client using the commands above.
2. In the GUI, click **Start Download**. The server deliberately streams small chunks with a short delay so the transfer is interruptible.
3. While progress is increasing, click **Interrupt Download**. The client finishes the current write, closes the response in the worker thread, flushes/closes the `.part` file, and reports the saved byte count and its path.
4. Click **Resume Download**. The client reads the current `.part` size (for example, 500000 bytes) and sends `Range: bytes=500000-`. The server replies with `206 Partial Content`; the client validates the complete `Content-Range` and `Content-Length` before appending.
5. When all bytes arrive, the client checks expected size and SHA-256. Only after both checks succeed does it rename the `.part` file to `downloads/sample_update.bin`.

The same Resume button can be used after a network error. If the server is stopped, the GUI reports a network error and the `.part` file remains. Restart the server, then click **Resume Download**.

## Demonstrate a real server disconnection

1. Begin downloading in the GUI and wait until the progress has advanced.
2. In the server terminal, press **Ctrl+C** to stop the server while the client is receiving data.
3. The client should show a network error and the number of bytes retained in the `.part` file.
4. Run `python server.py` again in the server terminal.
5. Click **Resume Download**. The server log should show a `Range` header with the saved offset and the client should continue.

The test suite also simulates a connection failure within a response and verifies that already received bytes remain available for the next Range request. The actual Tkinter window and a manually clicked GUI/server-shutdown sequence should still be demonstrated on the target Windows laptop.

## Automated tests

Run from the project directory:

```powershell
python -m unittest tests -v
```

The tests use small temporary update files, not repeated downloads of the 25 MiB demonstration file. They cover health and metadata routes, a full download, valid and invalid ranges, resume response validation, protection against appending a full `200 OK` response, file size and hash checks, interrupt handling, and simulated connection failure/recovery.

## Expected output

The server terminal prints metadata requests and download requests, including `Range` offsets. A resumed transfer should show a line similar to:

```text
[server] Download request for sample_update.bin. Range header: bytes=500000-
[server] Serving bytes 500000-26214399 with HTTP 206, length=25714400
```

In the GUI, status changes from **Downloading...** to **Resuming from byte N**, **Interrupted**, or **Network error** as applicable. A successful transfer ends with **Download completed and verified** and **SHA-256 verified successfully**.

## HTTP status and integrity notes

- `200 OK` means a full download; it is used only when no resume offset is requested.
- `206 Partial Content` means the server accepted a valid Range request and is returning the requested byte range.
- `416 Range Not Satisfiable` means the requested range is invalid for the file. The client reports this and does not append the response.
- The server sends `Content-Length`, `Accept-Ranges`, and, for partial responses, `Content-Range`.
- SHA-256 confirms that the bytes match the hash received from this server. By itself it does not prove the publisher's identity; that requires a separately trusted signature or key, which is outside this simple demonstration.

## Troubleshooting

- **Connection refused:** Confirm the server terminal is running and the GUI URL is `http://127.0.0.1:5000`.
- **Port 5000 already in use:** Stop the other local server process or change the port in `server.py` and the GUI URL to match.
- **No partial file to resume:** Click **Start Download** first and allow some bytes to arrive. The partial file is `downloads/sample_update.bin.part`.
- **Resume says a transfer is running:** Wait for the current worker to report completion/interruption before starting another.
- **Tkinter import/window problem:** Install a standard Python build that includes Tcl/Tk, then retry `python client.py`.
- **Dependencies missing:** Run `python -m pip install -r requirements.txt`.

