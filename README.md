# Secure Software Update Mechanism with Edge Caching & Resumable Downloads

A Computer Networks project implementing a secure, resumable, and bandwidth-efficient software update distribution mechanism using Python, Flask, and Tkinter.

---

## Project Overview

Distributing large software updates reliably across distributed networks poses significant challenges:
- High origin server bandwidth consumption.
- Frequent transfer interruptions due to unstable network connections.
- Risk of file tampering or corruption during transit.

This project addresses these challenges by implementing **Chunked Distribution with Edge Caching** and **HTTP Range-based Resumable Downloads**, backed by cryptographic verification (HMAC-SHA256 and SHA-256).

---

## Key Highlights & Features

- **Dual Distribution Modes**:
  - **Chunked Distribution via Edge Cache (Default)**: Splits files into fixed 1 MiB chunks governed by a cryptographically signed manifest.
  - **Direct HTTP Range**: Traditional resume mechanism using `Range: bytes=N-` and HTTP `206 Partial Content`.
- **LAN Edge Caching Node**:
  - Acts as a local reverse/caching proxy on port `5001`.
  - Serves chunk requests to local network clients; downloads chunk once from origin on cache MISS and serves subsequent requests as cache HITs.
  - Achieves **~80% origin server offload** in multi-client test simulations.
- **Cryptographic Integrity & Authenticity**:
  - **Signed Manifests**: Update manifests contain individual chunk hashes and total file SHA-256, verified using HMAC-SHA256 signatures before caching.
  - **End-to-End Verification**: Client validates total size and whole-file SHA-256 checksum prior to finalization; updates are never executed blindly.
- **Resilient & Resumable Downloads**:
  - Client maintains a chunk bitmap (`<file>.bitmap.json`) tracking received chunks (1 = received, 0 = missing).
  - Automatically fetches only missing chunks after connection drops or user-initiated interruptions.
- **Real-Time Interactive Desktop Dashboard**:
  - Built with Python Tkinter.
  - Live server/edge health monitoring.
  - Color-coded visual chunk map (Missing, Downloading, Edge HIT, Edge MISS).
  - Real-time throughput graph and transfer rate statistics.
  - Edge cache hit/miss ratio, bandwidth savings counter, and event log.
- **Comprehensive Automated Testing**:
  - 25-unit test suite (`tests.py`) covering route handlers, range queries, integrity validation, simulated connection drops, and partial resume flows.

---

## System Architecture

```
                      +-----------------------------+
                      |        Origin Server        |
                      |    (Flask - Port 5000)      |
                      |  - Updates storage          |
                      |  - Signed Manifests         |
                      +--------------+--------------+
                                     |
                       (WAN / LAN)   | Chunk Fetch on MISS
                                     v
                      +-----------------------------+
                      |       Edge Cache Node       |
                      |    (Flask - Port 5001)      |
                      |  - HMAC verification        |
                      |  - Local chunk storage      |
                      +--------------+--------------+
                                     |
                                     | LAN HTTP Chunks (HITs)
                                     v
                      +-----------------------------+
                      |      Tkinter GUI Client     |
                      |  - Visual Chunk Map         |
                      |  - Bitmap Resume Manager    |
                      |  - SHA-256 Verifier         |
                      +-----------------------------+
```

---

## Repository Structure

```
.
├── CN/
│   ├── Report/
│   │   ├── CN_Project_Report_Secure_Software_Update.docx   # Comprehensive Project Report (DOCX)
│   │   └── CN_Project_Report_Secure_Software_Update.pdf    # Comprehensive Project Report (PDF)
│   └── secure_software_update_mechanism/
│       ├── client.py               # Tkinter desktop GUI client & visual dashboard
│       ├── edge_cache.py           # LAN Edge caching proxy server (Port 5001)
│       ├── server.py               # Origin update server (Port 5000)
│       ├── update_manifest.py      # Chunking, SHA-256 hashing & HMAC manifest generator
│       ├── simulate_clients.py     # Multi-client benchmark simulator
│       ├── tests.py                # Unit test suite (25 test cases)
│       ├── requirements.txt        # Python package dependencies
│       └── updates/                # Storage directory for origin update binaries
├── .gitignore
└── README.md
```

---

## Quick Start Guide

### 1. Prerequisites
- Python 3.10+ installed
- PowerShell or Terminal

### 2. Setup Environment & Dependencies
```powershell
# Clone the repository
git clone https://github.com/RajavarshanM2007/secure-software-update-mechanism.git
cd secure-software-update-mechanism

# Navigate to the project root
cd CN/secure_software_update_mechanism

# Install required dependencies
python -m pip install -r requirements.txt
```

### 3. Running the Architecture

Open three separate terminal windows:

**Terminal 1 — Origin Server:**
```powershell
python server.py
# Listens at http://127.0.0.1:5000 (Generates a 25 MiB sample binary automatically if missing)
```

**Terminal 2 — Edge Cache Node:**
```powershell
python edge_cache.py
# Listens at http://127.0.0.1:5001
```

**Terminal 3 — Client Dashboard:**
```powershell
python client.py
# Launches the interactive GUI dashboard
```

---

## Simulation & Automated Testing

### Multi-Client Performance Simulation
Stop running server instances first, then execute:
```powershell
python simulate_clients.py --clients 5
```
- Compares 5 clients downloading directly from origin vs. 5 clients downloading via edge cache.
- Exports metrics to `results/simulation_results.json`.

### Running Unit Tests
```powershell
python -m unittest tests -v
```

---

## Security Considerations
- **Manifest Authenticity**: Verified using HMAC-SHA256 (`UPDATE_SIGNING_KEY` environment variable). In production deployments, asymmetric digital signatures (RSA/ECDSA) should be utilized.
- **Execution Safeguard**: Downloaded binaries are cryptographically validated against their target digest and are never executed directly by the updater.
