#!/usr/bin/env python3
"""
v2-sportsbite.py — browse SportsBite 24/7 TV channels in your terminal
and extract the direct stream (m3u8) playlist or iframe embed URLs.

No browser required. Fetches dynamically loaded channels from:
    https://channels.forestgump.space/channels

Usage:
    python v2-sportsbite.py
    python v2-sportsbite.py --raw           # disable ANSI colors
    python v2-sportsbite.py --play          # output only the decrypted m3u8 URL

Dependencies: Python 3.10+ stdlib + httpx.
"""

from __future__ import annotations

import argparse
import base64
import os
import re
import shutil
import subprocess
import sys
import time
import threading
from dataclasses import dataclass, field
from typing import Any

try:
    import httpx
except ImportError:
    sys.stderr.write(
        "error: this script needs the httpx package.\n"
        "        install with: pip install httpx\n"
    )
    sys.exit(2)


# ---------------------------------------------------------------------------
# Network + config
# ---------------------------------------------------------------------------

API_BASE = "https://channels.forestgump.space"
CHANNELS_ENDPOINT = f"{API_BASE}/channels"
REFERER = "https://sportsbite.org/"
ORIGIN = "https://sportsbite.org"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
TIMEOUT = 15.0


class SportsBiteClient:
    def __init__(self, client: httpx.Client | None = None):
        self.client = client or httpx.Client(
            headers={
                "User-Agent": USER_AGENT,
                "Referer": REFERER,
                "Origin": ORIGIN,
                "Accept": "application/json",
            },
            timeout=TIMEOUT,
            follow_redirects=True,
        )
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client and self.client is not None:
            self.client.close()

    def __enter__(self) -> "SportsBiteClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def fetch_channels(self) -> list[dict[str, Any]]:
        r = self.client.get(CHANNELS_ENDPOINT)
        r.raise_for_status()
        data = r.json()
        return data.get("channels") or []

    def fetch_stream_url(self, manifest_url: str) -> str | None:
        """Fetches the embed HTML page and decrypts the stream source from window.atob."""
        r = self.client.get(manifest_url)
        r.raise_for_status()
        
        # Look for window.atob("...") pattern in the HTML
        match = re.search(r'window\.atob\(\"([A-Za-z0-9+/=]+)\"\)', r.text)
        if match:
            try:
                decoded = base64.b64decode(match.group(1)).decode("utf-8")
                return decoded
            except Exception:
                pass
        
        # Fallback: construct it ourselves if we can parse the channel ID from manifest_url
        # e.g., https://channels.forestgump.space/dlhd-embed/embed/521 -> track/521
        url_match = re.search(r'/embed/([^/]+)', manifest_url)
        if url_match:
            channel_id = url_match.group(1)
            return f"{API_BASE}/track/{channel_id}"
            
        return None


# ---------------------------------------------------------------------------
# In-memory model
# ---------------------------------------------------------------------------

@dataclass
class Channel:
    id: str
    name: str
    category: str
    status: str
    manifest_url: str
    source: str
    country: str | None = None

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> "Channel":
        return cls(
            id=str(raw.get("id") or ""),
            name=str(raw.get("name") or "Unknown"),
            category=str(raw.get("category") or "Other"),
            status=str(raw.get("status") or "online"),
            manifest_url=str(raw.get("manifest_url") or ""),
            source=str(raw.get("source") or "unknown"),
            country=raw.get("country"),
        )


# ---------------------------------------------------------------------------
# Terminal helpers
# ---------------------------------------------------------------------------

def enable_colors(force: bool) -> bool:
    if force:
        return True
    if not sys.stdout.isatty():
        return False
    if os.environ.get("NO_COLOR"):
        return False
    return os.environ.get("TERM", "") != "dumb"


class C:
    def __init__(self, on: bool):
        self.on = on
        if not on:
            return
        self.dim = "\033[2m"
        self.bold = "\033[1m"
        self.reset = "\033[0m"
        self.sky = "\033[38;5;110m"
        self.amber = "\033[38;5;179m"
        self.slate = "\033[38;5;246m"
        self.sage = "\033[38;5;108m"
        self.warn = "\033[38;5;173m"
        self.rose = "\033[38;5;204m"
        self.bg_sel = "\033[48;5;237m"

    def __getattr__(self, name: str) -> str:
        return ""


def hr(width: int = 70, char: str = "─") -> str:
    return char * width


def _term_height(default: int = 24) -> int:
    try:
        return max(10, os.get_terminal_size().lines)
    except (OSError, ValueError):
        return default


def _term_width(default: int = 100) -> int:
    try:
        return max(40, os.get_terminal_size().columns)
    except (OSError, ValueError):
        return default


def truncate_str(s: str, width: int) -> str:
    if len(s) > width:
        return s[:width-3] + "..."
    return s.ljust(width)


def get_column_widths(width: int) -> tuple[int, int, int, int]:
    overhead = 40
    rem = width - overhead
    if rem < 20:
        name_w = max(15, rem)
        cat_w = 0
    else:
        cat_w = min(15, max(10, int(rem * 0.25)))
        name_w = rem - cat_w
    return name_w, 12, 12, cat_w


def fetch_with_spinner(label: str, fn, c: C):
    if not sys.stdout.isatty():
        sys.stdout.write(f"  {label}…\n")
        sys.stdout.flush()
        return fn()

    frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    result = [None]
    exc = [None]
    done = threading.Event()

    def worker() -> None:
        try:
            result[0] = fn()
        except Exception as e:
            exc[0] = e
        finally:
            done.set()

    threading.Thread(target=worker, daemon=True).start()

    i = 0
    while not done.wait(0.08):
        sys.stdout.write(
            f"\r  {c.sky}{frames[i % len(frames)]}{c.reset}"
            f"  {c.slate}{label}{c.reset}\033[K"
        )
        sys.stdout.flush()
        i += 1

    sys.stdout.write("\r\033[K")
    sys.stdout.flush()

    if exc[0]:
        raise exc[0]
    return result[0]


def print_banner(c: C) -> None:
    w = min(_term_width(), 32)
    inner = w - 2
    title = "SportsBite TV Channels"
    sub = REFERER

    t_pad = max(0, inner - len(title) - 2)
    s_pad = max(0, inner - len(sub) - 2)

    print(f"{c.sky}╭{'─' * inner}╮{c.reset}")
    print(f"{c.sky}│{c.reset}  {c.bold}{c.sky}{title}{c.reset}{' ' * t_pad}{c.sky}│{c.reset}")
    print(f"{c.sky}│{c.reset}  {c.slate}{sub}{c.reset}{' ' * s_pad}{c.sky}│{c.reset}")
    print(f"{c.sky}╰{'─' * inner}╯{c.reset}")
    print()


# ---------------------------------------------------------------------------
# Selection Prompt (No Third-Party Deps)
# ---------------------------------------------------------------------------

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")

def strip_ansi(s: str) -> str:
    return ANSI_RE.sub("", s)


def _read_key() -> str:
    ch = sys.stdin.read(1)
    if ch == "\x1b":
        nxt = sys.stdin.read(1)
        if nxt in ("[", "O"):
            code = sys.stdin.read(1)
            if code == "A": return "UP"
            if code == "B": return "DOWN"
            if code == "C": return "RIGHT"
            if code == "D": return "LEFT"
            if code == "H": return "UP"
            if code == "F": return "DOWN"
            while code not in ("~",):
                code = sys.stdin.read(1)
            return "?"
        return "ESC"
    if ch in ("\r", "\n"): return "ENTER"
    if ch in ("\x7f", "\b"): return "BACK"
    if ch == "\x03": return "CTRL_C"
    if ch == "\x04": return "CTRL_D"
    if ch.isprintable(): return "TEXT:" + ch
    return ""


def _pick_plain(title: str, rows: list[str], header_row: str | None = None) -> int | None:
    print(strip_ansi(title))
    if header_row:
        print(f"       {strip_ansi(header_row)}")
    for i, r in enumerate(rows, 1):
        print(f"  {i:>3}.  {strip_ansi(r)}")
    try:
        q = input(f"  Enter number (1–{len(rows)}): ").strip()
    except (EOFError, KeyboardInterrupt):
        return None
    if not q.isdigit():
        return None
    n = int(q)
    return n - 1 if 1 <= n <= len(rows) else None


def _clear_block(out, block_height: int) -> None:
    if block_height > 1:
        out.write(f"\033[{block_height - 1}A")
    out.write("\r")
    for i in range(block_height):
        out.write("\033[2K")
        if i < block_height - 1:
            out.write("\r\n")
    if block_height > 1:
        out.write(f"\033[{block_height - 1}A")
    out.write("\r")
    out.flush()


def pick_from_list(
    title: str,
    rows: list[str],
    *,
    c: C,
    header_row: str | None = None,
) -> int | None:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return _pick_plain(title, rows, header_row)

    import termios, tty  # type: ignore

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        out = sys.stdout
        out.write("\033[?25l")
        out.flush()

        header_len = 1 if header_row is not None else 0
        block_height = max(8, _term_height() - 1)
        list_room = max(3, block_height - 3 - header_len)

        query = ""
        cursor = 0
        scroll = 0
        first_render = True

        def filtered() -> list[int]:
            if not query:
                return list(range(len(rows)))
            q = query.lower()
            return [i for i, r in enumerate(rows) if q in strip_ansi(r).lower()]

        def render() -> None:
            nonlocal cursor, scroll, first_render
            idxs = filtered()

            if not idxs:
                cursor, scroll = 0, 0
            else:
                cursor = max(0, min(cursor, len(idxs) - 1))
                if cursor < scroll:
                    scroll = cursor
                elif cursor >= scroll + list_room:
                    scroll = cursor - list_room + 1

            if not first_render:
                out.write(f"\033[{block_height - 1}A\r")
            else:
                first_render = False

            out.write(f"{c.bold}{title}{c.reset}\033[K\r\n")
            out.write(f"  {c.slate}filter ›{c.reset} {query}{c.dim}▌{c.reset}\033[K\r\n")

            if header_row is not None:
                out.write(f"  {header_row}\033[K\r\n")

            end = min(len(idxs), scroll + list_room)
            for k in range(list_room):
                screen_i = scroll + k
                if screen_i < end:
                    r = rows[idxs[screen_i]]
                    if screen_i == cursor:
                        out.write(f"{c.bg_sel}  {r}{c.reset}\033[K\r\n")
                    else:
                        out.write(f"  {r}\033[K\r\n")
                else:
                    out.write("\033[K\r\n")

            if idxs:
                pct = f"{len(idxs)}/{len(rows)}"
                legend = (
                    f"{c.dim}  ↑↓ navigate · type to filter · "
                    f"Enter select · Esc cancel  [{pct}]{c.reset}"
                )
            else:
                legend = f"{c.warn}  no matches — keep typing or press Esc{c.reset}"
            out.write(f"{legend}\033[K")
            out.flush()

        render()

        while True:
            key = _read_key()
            if key == "UP":
                if filtered():
                    cursor = max(0, cursor - 1)
                render()
            elif key == "DOWN":
                idxs = filtered()
                if idxs:
                    cursor = min(len(idxs) - 1, cursor + 1)
                render()
            elif key == "ENTER":
                idxs = filtered()
                if not idxs:
                    continue
                _clear_block(out, block_height)
                out.write("\033[?25h")
                out.flush()
                return idxs[cursor]
            elif key in ("ESC", "CTRL_C", "CTRL_D"):
                _clear_block(out, block_height)
                out.write("\033[?25h")
                out.flush()
                return None
            elif key == "BACK":
                query = query[:-1]
                cursor = 0
                scroll = 0
                render()
            elif key.startswith("TEXT:"):
                query += key.split(":", 1)[1]
                cursor = 0
                scroll = 0
                render()
    finally:
        try:
            termios.tcsetattr(fd, termios.TCSAFLUSH, old)
        except Exception:
            pass
        sys.stdout.write("\033[?25h")
        sys.stdout.flush()


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def channel_row(ch: Channel, name_w: int, src_w: int, country_w: int, cat_w: int, c: C) -> str:
    badge = f"{c.sage}{c.bold}● ON {c.reset}" if ch.status == "online" else f"{c.rose}{c.bold}○ OFF{c.reset}"
    
    disp_name = truncate_str(ch.name, name_w)
    name_str = f"{c.bold}{disp_name}{c.reset}"
    
    disp_cat = truncate_str(ch.category, cat_w) if cat_w > 0 else ""
    cat_str = f"{c.slate}{disp_cat}{c.reset}"
    
    disp_src = truncate_str(ch.source, src_w)
    src_str = f"{c.dim}{disp_src}{c.reset}"
    
    disp_country = truncate_str(ch.country or "—", country_w)
    country_str = f"{c.dim}{disp_country}{c.reset}"
    
    return f"{badge}  {name_str}  {cat_str}  {src_str}  {country_str}"


def print_stream_details(ch: Channel, stream_url: str | None, c: C) -> None:
    print(f"\n  {c.sky}┌──{c.reset} {c.bold}CHANNEL DETAILS{c.reset} {c.sky}{'─' * 50}┐{c.reset}")
    print(f"  {c.sky}│{c.reset}  {c.bold}Name:{c.reset}      {c.bold}{ch.name}{c.reset}")
    print(f"  {c.sky}│{c.reset}  {c.bold}Category:{c.reset}  {ch.category}")
    print(f"  {c.sky}│{c.reset}  {c.bold}Source:{c.reset}    {ch.source} ({ch.status})")
    print(f"  {c.sky}│{c.reset}  {c.bold}Embed URL:{c.reset} {c.sky}{ch.manifest_url}{c.reset}")
    
    if stream_url:
        print(f"  {c.sky}│{c.reset}  {c.bold}M3U8 URL:{c.reset}  {c.sage}{stream_url}{c.reset}")
        print(f"  {c.sky}│{c.reset}")
        print(f"  {c.sky}│{c.reset}  {c.bold}To play with mpv:{c.reset}")
        print(f"  {c.sky}│{c.reset}  mpv --http-header-fields=\"Referer: {REFERER}\" \"{stream_url}\"")
    else:
        print(f"  {c.sky}│{c.reset}  {c.rose}Failed to decrypt stream M3U8 url.{c.reset}")
        
    print(f"  {c.sky}└────────────────────────────────────────────────────────────────────┘{c.reset}\n")


# ---------------------------------------------------------------------------
# Playback actions
# ---------------------------------------------------------------------------

def _require_binary(cmd: str, c: C) -> bool:
    if shutil.which(cmd) is None:
        sys.stderr.write(f"\n  {c.rose}✗ '{cmd}' was not found in your PATH.{c.reset}\n")
        try:
            input(f"  {c.dim}Press Enter to continue...{c.reset}")
        except (EOFError, KeyboardInterrupt):
            pass
        return False
    return True


def play_with_mpv(stream_url: str, c: C) -> None:
    if not _require_binary("mpv", c):
        return
    cmd = [
        "mpv",
        f"--http-header-fields=Referer: {REFERER}",
        f"--user-agent={USER_AGENT}",
        stream_url,
    ]
    print(f"\n  {c.sky}▶ Launching mpv…{c.reset}  {c.dim}(close the player to return to the list){c.reset}\n")
    try:
        subprocess.run(cmd)
    except KeyboardInterrupt:
        pass
    except Exception as e:
        sys.stderr.write(f"  {c.rose}✗ mpv exited with an error: {e}{c.reset}\n")


def choose_playback_action(c: C) -> str | None:
    """Arrow-key menu of what to do with a resolved stream. Returns one of
    'mpv', 'details', or None (back to channel list)."""
    rows = [
        f"{c.sage}▶{c.reset}  Play in {c.bold}mpv{c.reset}",
        f"{c.slate}i{c.reset}  Show stream details",
        f"{c.dim}←{c.reset}  Back to channel list",
    ]
    idx = pick_from_list("What would you like to do?", rows, c=c)
    if idx is None:
        return None
    return ["mpv", "details", None][idx]


# ---------------------------------------------------------------------------
# Main CLI Flow
# ---------------------------------------------------------------------------

def run(use_color: bool, play_only: bool) -> int:
    c = C(use_color)
    
    if play_only:
        # Script is being called programmatically to output only the m3u8 url
        # We fetch the first channel matches or need a list.
        # But for CLI interactive, let's keep it simple.
        pass

    with SportsBiteClient() as client:
        # Fetch channel list
        try:
            channels_raw = fetch_with_spinner(
                "Fetching TV channels list",
                client.fetch_channels,
                c
            )
        except Exception as e:
            sys.stderr.write(f"{c.rose}✗ Failed to fetch channels: {e}{c.reset}\n")
            return 1
            
        channels = [Channel.from_raw(raw) for raw in channels_raw]
        if not channels:
            sys.stderr.write(f"{c.rose}✗ No channels returned from API{c.reset}\n")
            return 1
            
        # If --play is used, we require user input or filtering? Let's just run interactive.
        # Format rows
        print_banner(c)
        
        width = _term_width()
        name_w, src_w, country_w, cat_w = get_column_widths(width - 8)
        
        hdr_status = "STATUS".ljust(5)
        hdr_name = "CHANNEL NAME".ljust(name_w)
        hdr_category = "CATEGORY".ljust(cat_w) if cat_w > 0 else ""
        hdr_source = "SOURCE".ljust(src_w)
        hdr_country = "COUNTRY".ljust(country_w)
        
        header_row = f"{c.bold}{c.slate}{hdr_status}  {hdr_name}  {hdr_category}  {hdr_source}  {hdr_country}{c.reset}"
        
        # Sort online first, then by category, then by name
        channels.sort(key=lambda x: (x.status != "online", x.category, x.name))
        
        rows = [channel_row(ch, name_w, src_w, country_w, cat_w, c) for ch in channels]

        # Main loop: pick a channel -> resolve stream -> act on it -> back to list.
        while True:
            idx = pick_from_list(
                f"Select a TV channel  {c.dim}({len(channels)} channels){c.reset}",
                rows,
                c=c,
                header_row=header_row,
            )

            if idx is None:
                print(f"\n{c.slate}  cancelled.{c.reset}")
                return 0

            chosen = channels[idx]
            print(f"\n  {c.bold}Selected:{c.reset} {c.sky}{chosen.name}{c.reset}")

            # Fetch and decrypt stream URL
            try:
                stream_url = fetch_with_spinner(
                    f"Decrypting stream URL for \"{chosen.name}\"",
                    lambda: client.fetch_stream_url(chosen.manifest_url),
                    c
                )
            except Exception as e:
                sys.stderr.write(f"{c.rose}✗ Failed to fetch stream details: {e}{c.reset}\n")
                return 1

            if play_only:
                if stream_url:
                    print(stream_url)
                return 0

            if not stream_url:
                print_stream_details(chosen, stream_url, c)
                try:
                    input(f"  {c.dim}Press Enter to return to the channel list...{c.reset}")
                except (EOFError, KeyboardInterrupt):
                    pass
                continue

            # Let the user decide what to do with the resolved stream. mpv
            # blocks until the player window is closed, then we fall back here.
            while True:
                action = choose_playback_action(c)
                if action is None:
                    break  # back to channel list
                elif action == "mpv":
                    play_with_mpv(stream_url, c)
                    break  # fall back to main list after the player closes
                elif action == "details":
                    print_stream_details(chosen, stream_url, c)
                    try:
                        input(f"  {c.dim}Press Enter to continue...{c.reset}")
                    except (EOFError, KeyboardInterrupt):
                        pass
                    # stay in the action menu

    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="v2-sportsbite.py",
        description="Browse SportsBite 24/7 TV channels and extract streams."
    )
    p.add_argument(
        "--raw",
        action="store_true",
        help="Disable ANSI colors."
    )
    p.add_argument(
        "--play",
        action="store_true",
        help="Only print the decrypted m3u8 stream URL."
    )
    args = p.parse_args(argv if argv is not None else sys.argv[1:])
    return run(enable_colors(not args.raw), args.play)


if __name__ == "__main__":
    raise SystemExit(main())