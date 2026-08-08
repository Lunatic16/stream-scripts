# Live Stream Terminal Indexers & Extractors

Lightweight, terminal-native tools to search, extract, and stream live sports and 24/7 TV channels using `mpv`.

---

## ⚡ Features & Architecture

* **Zero Heavy TUI Dependencies**: Built directly on native terminal interfaces (`termios`, `tty`) for arrow-key navigation and live fuzzy filtering.
* **Stream Decryption & Extraction**: Resolves dynamic iFrames, unpacks Base64 payloads, and extracts direct `.m3u8` HLS manifest URLs.
* **Strict Header Forwarding**: Passes required `Referer`, `Origin`, and `User-Agent` headers directly to `mpv` demuxers to bypass access controls.
* **Failover Domain Resolution**: Automatically shifts requests across mirror endpoints (`ppv.to`, `ppv.st`, etc.) when primary API gateways are unresponsive.

---

## 🛠️ Included Utilities

### 1. `dlhd.py` — 24/7 Channel Picker & Extractor
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

### 2. `ppv.py` — Live Event & Substream Selector
Categorizes scheduled PPV broadcasts, alternative regional feeds, and audio/quality variants.

```bash
# Launch interactive event browser
python ppv.py

# Specify alternate API gateway endpoint
python ppv.py --api https://api.p..cx/api

# Show default stream parameters before entering substream menu
python ppv.py --show-default

# Plain text output (automatically enabled when piped)
python ppv.py --raw
```

---

### 3. `sportsbite.py` — SportsBite Live TV Indexer
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

## 🚀 Quick Start

### Prerequisites
* **Python**: `3.10+`
* **Dependencies**: `httpx`
* **Media Player**: `mpv`

### Installation
```bash
git clone https://github.com/Lunatic16/stream-scripts.git
cd stream-scripts
pip install httpx
```

---

## 📡 Header Handshake & Manual MPV Usage

When invoking extracted `.m3u8` manifests directly, pass the appropriate HTTP headers to prevent demuxer connection rejection:

```bash
mpv "<M3U8_STREAM_URL>" \
  --http-header-fields="Referer: <REFERER_URL>,Origin: <ORIGIN_URL>,User-Agent: Mozilla/5.0"
```
