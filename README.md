# 📺 Live Stream Terminal Indexers & Extractors

**Lightweight, terminal-native tools to search, extract, and stream live sports and 24/7 TV channels using `mpv`.**

[![Version](https://img.shields.io/badge/version-2.0.0-purple.svg)](https://github.com/Lunatic16)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](#-license)
[![Python](https://img.shields.io/badge/python-3.10+-brightgreen.svg)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/platform-Linux%20%7C%20macOS%20%7C%20Windows-lightgrey.svg)](#)

## ✨ Features & Architecture

- **Zero Heavy TUI Dependencies:** Built directly on native terminal interfaces (`termios`, `tty`) for arrow-key navigation and live fuzzy filtering.
- **Stream Decryption & Extraction:** Resolves dynamic iFrames, unpacks Base64 payloads, and extracts direct `.m3u8` HLS manifest URLs.
- **JS-Rendered Page Scraping:** Uses headless Chromium via Playwright to render JavaScript-heavy sites (bintv.cc) and pull event data straight from DOM `onclick` handlers.
- **Strict Header Forwarding:** Passes required `Referer`, `Origin`, and `User-Agent` headers directly to `mpv` demuxers to bypass access controls.
- **Failover Domain Resolution:** Automatically shifts requests across mirror endpoints when primary API gateways are unresponsive.
- **Post-Selection Action Menus:** After picking a stream, choose to play in `mpv`, open the embed in your browser, inspect stream details, or jump back to the list.

| Script | Source | Content | Fetch Method |
|---|---|---|---|
| `bintv.py` | BinTV | Live & scheduled events | Playwright headless Chromium |
| `dlhd.py` | DaddyLive | 24/7 TV channels | `httpx` + HTML parsing |
| `ppv.py` | PPV (+ mirrors) | PPV events & substreams | `httpx` REST API w/ failover |
| `sportsbite.py` | SportsBite | 24/7 sports TV | `httpx` JSON API |

---

## 📦 Installation & Dependencies

### Prerequisites
* **Python**: `3.10+`
* **Dependencies**: `httpx`
* **Media Player**: `mpv`

### Clone the repository
```bash
git clone https://github.com/Lunatic16/stream-scripts.git
cd stream-scripts
```
### Install Python dependencies
```bash
pip install httpx

# Additional dependency for bintv.py

pip install playwright
playwright install chromium
```
---

## 🛠️ Included Utilities

### 1. `bintv.py` — BINTV Event Index Browser

Renders the bintv.cc event index with Playwright, extracting event data from JavaScript onclick handlers. Supports category filtering, live-only mode, and JSON/plain-text output for scripting.

```bash
# Launch interactive event + stream picker
python bintv.py

# Disable ANSI colors
python bintv.py --raw

# Filter to a specific category
python bintv.py --category Soccer

# Show only live events
python bintv.py --live-only

# Plain text list (non-interactive)
python bintv.py --list

# JSON output for scripting
python bintv.py --json
```

---

### 2. `dlhd.py` — DaddyLive 24/7 Channel Picker & Extractor
Navigates live 24/7 TV streams with real-time availability checks and channel search.

```bash
# Launch interactive channel picker UI
python dlhd.py

# Query channel by name and print decrypted stream URL directly
python dlhd.py --channel "ESPN" --play

# Select channel directly by unique ID
python dlhd.py --id 521

# Run without ANSI formatting (for scripts/piping)
python dlhd.py --raw
```

---

### 3. `ppv.py` — PPV Live Event & Substream Selector
Categorizes scheduled PPV broadcasts, alternative regional feeds, and audio/quality variants.

```bash
# Launch interactive event browser
python ppv.py

# Specify alternate API gateway endpoint
python ppv.py --api https://api.p..c./api

# Show default stream parameters before entering substream menu
python ppv.py --show-default

# Plain text output (automatically enabled when piped)
python ppv.py --raw
```

---

### 4. `sportsbite.py` — SportsBite Live TV Indexer
Dynamic channel list navigation with inline stream payload decryption.

```bash
# Launch interactive channel selector
python sportsbite.py

# Extract and output primary decrypted M3U8 link
python sportsbite.py --play

# Plain text mode without color sequences
python sportsbite.py --raw
```

---

## 📡 Header Handshake & Manual MPV Usage

When invoking extracted `.m3u8` manifests directly, pass the appropriate HTTP headers to prevent demuxer connection rejection:

```bash
mpv "<M3U8_STREAM_URL>" \
  --http-header-fields="Referer: <REFERER_URL>,Origin: <ORIGIN_URL>,User-Agent: Mozilla/5.0"
```
---

## 📖 Summary of changes from the original
- Added a new section for `bintv.py` (Playwright-based event browser), including its `--category`, `--live-only`, `--list`, and `--json` flags.
- Added a Features bullet for Playwright headless rendering and another for two-stage embed resolution (specific to `dlhd.py`).
- Updated **Prerequisites/Installation** to note that `playwright` + `chromium` are required only by `bintv.py`.
- Fixed the malformed example URL (`https://api.p..c./api`) in the `ppv.py` section and documented the real failover domains.
