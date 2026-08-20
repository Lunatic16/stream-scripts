#!/usr/bin/env python3
"""
streamed.py — browse Streamed.pk / Streamed.st sports streams in your terminal
and extract the direct stream (m3u8) playlist or iframe embed URLs.

No browser required for most streams. Fetches dynamically loaded events from:
    https://streamed.pk/  or  https://streamed.st/

Usage:
    python streamed.py
    python streamed.py --raw           # disable ANSI colors
    python streamed.py --play          # output only the decrypted m3u8 URL
    python streamed.py --base https://streamed.st

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
import urllib.parse
import webbrowser
from dataclasses import dataclass
from datetime import datetime
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

DEFAULT_BASE = os.environ.get("STREAMED_BASE", "https://streamed.pk").rstrip("/")
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
TIMEOUT = 15.0
MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 0.75


class StreamedClient:
    def __init__(self, base_url: str | None = None, client: httpx.Client | None = None):
        self.base = (base_url or DEFAULT_BASE).rstrip("/")
        self.client = client or httpx.Client(
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate, br",
                "Connection": "keep-alive",
            },
            timeout=TIMEOUT,
            follow_redirects=True,
        )
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client and self.client is not None:
            self.client.close()

    def __enter__(self) -> "StreamedClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _get_with_retry(self, url: str) -> httpx.Response:
        last_exc: Exception | None = None
        for attempt in range(MAX_RETRIES):
            try:
                r = self.client.get(url)
                if r.status_code >= 500:
                    r.raise_for_status()
                return r
            except (httpx.TransportError, httpx.HTTPStatusError) as e:
                last_exc = e
                if isinstance(e, httpx.HTTPStatusError) and e.response.status_code < 500:
                    raise
                if attempt < MAX_RETRIES - 1:
                    time.sleep(RETRY_BACKOFF_BASE * (2 ** attempt))
        assert last_exc is not None
        raise last_exc

    def fetch_sports(self) -> list[dict[str, Any]]:
        url = f"{self.base}/api/sports"
        r = self._get_with_retry(url)
        r.raise_for_status()
        return r.json()

    def fetch_popular_matches(self) -> list[dict[str, Any]]:
        url = f"{self.base}/api/matches/all/popular"
        r = self._get_with_retry(url)
        r.raise_for_status()
        matches = r.json()
        matches.sort(key=lambda m: m.get("date", 0))
        return matches

    def fetch_matches_by_sport(self, sport_id: str) -> list[dict[str, Any]]:
        url = f"{self.base}/api/matches/{sport_id}"
        r = self._get_with_retry(url)
        r.raise_for_status()
        matches = r.json()
        matches.sort(key=lambda m: m.get("date", 0))
        return matches

    def fetch_streams_for_match(self, match: "Match") -> list[dict[str, Any]]:
        all_streams: list[dict[str, Any]] = []
        for src in match.sources:
            url = f"{self.base}/api/stream/{src['source']}/{src['id']}"
            try:
                r = self._get_with_retry(url)
                r.raise_for_status()
                streams = r.json()
                all_streams.extend(streams)
            except Exception:
                continue
        regular = [s for s in all_streams if s.get("source", "").lower() != "admin"]
        admin = [s for s in all_streams if s.get("source", "").lower() == "admin"]
        return regular + admin

    def fetch_stream_url(self, embed_url: str) -> tuple[str | None, str]:
        """Resolve an embed URL down to a playable stream URL.

        Mirrors dlhd.py's multi-stage flow:
          1. Fetch the embed page.
          2. Follow nested iframes.
          3. Search for base64-encoded m3u8, direct m3u8 links, or file references.
        """
        if not embed_url:
            return None, ""

        referer = embed_url
        try:
            r = self._get_with_retry(embed_url)
            r.raise_for_status()
            html = r.text
        except Exception:
            return None, referer

        nested_iframe = re.search(r'<iframe[^>]+src=["\'](https?://[^"\']+)["\']', html, re.IGNORECASE)
        if nested_iframe:
            nested_url = nested_iframe.group(1)
            if nested_url != embed_url and "histats" not in nested_url:
                try:
                    r2 = self._get_with_retry(nested_url)
                    r2.raise_for_status()
                    html = r2.text
                    referer = nested_url
                except Exception:
                    pass

        match = re.search(r'window\.atob\([\'"]([A-Za-z0-9+/=]+)[\'"]\)', html)
        if match:
            try:
                decoded = base64.b64decode(match.group(1)).decode("utf-8")
                return decoded, referer
            except Exception:
                pass

        m3u8_match = re.search(r'(https?://[^\s\'"<>]+\.m3u8(?:[^\s\'"<>]*))', html)
        if m3u8_match:
            return m3u8_match.group(1), referer

        for b64_str in re.findall(r'atob\(["\']([A-Za-z0-9+/=]{20,})["\']\)', html):
            try:
                decoded = base64.b64decode(b64_str).decode("utf-8")
                if ".m3u8" in decoded or "http" in decoded:
                    clean_url = re.search(r'(https?://[^\s\'"<>]+)', decoded)
                    if clean_url:
                        return clean_url.group(1), referer
            except Exception:
                continue

        file_match = re.search(r'file\s*:\s*["\'](https?://[^"\']+)["\']', html)
        if file_match:
            return file_match.group(1), referer

        iframe_match = re.search(r'<iframe[^>]*id=["\']playerFrame["\'][^>]*src=["\']([^"\']+)["\']', html)
        if iframe_match:
            return iframe_match.group(1), referer

        player_match = re.search(r'data-url=["\']([^"\']*stream[^"\']*)["\']', html)
        if player_match:
            return player_match.group(1), referer

        return None, referer


# ---------------------------------------------------------------------------
# In-memory model
# ---------------------------------------------------------------------------

@dataclass
class Sport:
    id: str
    name: str

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> "Sport":
        return cls(
            id=str(raw.get("id") or ""),
            name=str(raw.get("name") or "Unknown"),
        )


@dataclass
class Team:
    name: str
    badge: str | None


@dataclass
class Match:
    id: str
    title: str
    category: str
    date: int
    poster: str | None
    popular: bool
    teams: dict[str, Team | None]
    sources: list[dict[str, str]]
    viewers: int

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> "Match":
        teams_raw = raw.get("teams") or {}
        home = teams_raw.get("home")
        away = teams_raw.get("away")
        return cls(
            id=str(raw.get("id") or ""),
            title=str(raw.get("title") or "Unknown"),
            category=str(raw.get("category") or "Other"),
            date=int(raw.get("date") or 0),
            poster=raw.get("poster"),
            popular=bool(raw.get("popular")),
            teams={
                "home": Team(name=home.get("name", ""), badge=home.get("badge")) if home else None,
                "away": Team(name=away.get("name", ""), badge=away.get("badge")) if away else None,
            },
            sources=raw.get("sources") or [],
            viewers=int(raw.get("viewers") or 0),
        )

    def display_title(self) -> str:
        home = self.teams.get("home")
        away = self.teams.get("away")
        if home and away and home.name and away.name:
            return f"{home.name} vs {away.name}"
        return self.title

    def display_time(self) -> str:
        if self.date:
            return datetime.fromtimestamp(self.date / 1000).strftime("%b %d %H:%M")
        return "Unknown time"


@dataclass
class Stream:
    id: str
    stream_no: int
    language: str
    hd: bool
    embed_url: str
    source: str
    viewers: int

    @property
    def is_admin(self) -> bool:
        return self.source.lower() == "admin"

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> "Stream":
        return cls(
            id=str(raw.get("id") or ""),
            stream_no=int(raw.get("streamNo") or 0),
            language=str(raw.get("language") or "Unknown"),
            hd=bool(raw.get("hd")),
            embed_url=str(raw.get("embedUrl") or ""),
            source=str(raw.get("source") or "unknown"),
            viewers=int(raw.get("viewers") or 0),
        )


# ---------------------------------------------------------------------------
# Terminal helpers (mirrored from dlhd.py)
# ---------------------------------------------------------------------------

def enable_colors(disable: bool) -> bool:
    if disable:
        return False
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
    if len(s) > width and width >= 3:
        return s[:width-3] + "..."
    return s.ljust(width)


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


def print_banner(c: C, base: str) -> None:
    w = min(_term_width(), 40)
    inner = w - 2
    title = "Streamed Sports"
    sub = base

    t_pad = max(0, inner - len(title) - 2)
    s_pad = max(0, inner - len(sub) - 2)

    print(f"{c.sky}╭{'─' * inner}╮{c.reset}")
    print(f"{c.sky}│{c.reset}  {c.bold}{c.sky}{title}{c.reset}{' ' * t_pad}{c.sky}│{c.reset}")
    print(f"{c.sky}│{c.reset}  {c.slate}{sub}{c.reset}{' ' * s_pad}{c.sky}│{c.reset}")
    print(f"{c.sky}╰{'─' * inner}╯{c.reset}")
    print()


# ---------------------------------------------------------------------------
# Selection Prompt (mirrored from dlhd.py)
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

def format_viewers(count: int) -> str:
    if count >= 1_000_000:
        return f"{count/1_000_000:.1f}m".replace(".0m", "m")
    if count >= 1000:
        return f"{count/1000:.1f}k".replace(".0k", "k")
    return str(count)


def sport_row(sp: Sport, c: C) -> str:
    return f"{c.bold}{sp.name}{c.reset}"


def match_row(mt: Match, c: C) -> str:
    when = mt.display_time()
    title = mt.display_title()
    viewers = format_viewers(mt.viewers) if mt.viewers > 0 else ""
    viewers_str = f" {c.dim}({viewers} viewers){c.reset}" if viewers else ""
    return f"{c.slate}{when}{c.reset}  {c.bold}{title}{c.reset}{viewers_str}  {c.dim}({mt.category}){c.reset}"


def stream_row(st: Stream, c: C) -> str:
    quality = f"{c.sage}HD{c.reset}" if st.hd else f"{c.dim}SD{c.reset}"
    viewers = format_viewers(st.viewers) if st.viewers > 0 else "0"
    admin_badge = f" {c.rose}[BROWSER ONLY]{c.reset}" if st.is_admin else ""
    return (
        f"#{st.stream_no} {c.bold}{st.language}{c.reset} ({quality}) – "
        f"{c.amber}{st.source}{c.reset} — ({viewers} viewers){admin_badge}"
    )


def print_stream_details(st: Stream, stream_url: str | None, c: C, referer: str | None = None) -> None:
    print(f"\n  {c.sky}┌──{c.reset} {c.bold}STREAM DETAILS{c.reset} {c.sky}{'─' * 50}┐{c.reset}")
    print(f"  {c.sky}│{c.reset}  {c.bold}Language:{c.reset}   {st.language}")
    print(f"  {c.sky}│{c.reset}  {c.bold}Quality:{c.reset}    {'HD' if st.hd else 'SD'}")
    print(f"  {c.sky}│{c.reset}  {c.bold}Source:{c.reset}     {st.source}")
    print(f"  {c.sky}│{c.reset}  {c.bold}Embed URL:{c.reset} {c.sky}{st.embed_url}{c.reset}")

    if stream_url:
        effective_referer = referer or st.embed_url
        origin = f"{urllib.parse.urlparse(effective_referer).scheme}://{urllib.parse.urlparse(effective_referer).netloc}"
        print(f"  {c.sky}│{c.reset}  {c.bold}M3U8 URL:{c.reset}  {c.sage}{stream_url}{c.reset}")
        print(f"  {c.sky}│{c.reset}")
        print(f"  {c.sky}│{c.reset}  {c.bold}To play with mpv:{c.reset}")
        print(
            f"  {c.sky}│{c.reset}  mpv \"{stream_url}\" "
            f"--http-header-fields=\"Referer: {effective_referer},Origin: {origin}\" "
            f"--user-agent=\"{USER_AGENT}\""
        )
    else:
        print(f"  {c.sky}│{c.reset}  {c.rose}Could not resolve a direct M3U8 URL.{c.reset}")
        if st.is_admin:
            print(f"  {c.sky}│{c.reset}  {c.warn}Admin streams require a browser due to heavy obfuscation.{c.reset}")

    print(f"  {c.sky}└────────────────────────────────────────────────────────────────────┘{c.reset}\n")


# ---------------------------------------------------------------------------
# Playback actions (mirrored from dlhd.py)
# ---------------------------------------------------------------------------

def is_valid_stream_url(url: str | None) -> bool:
    if not url:
        return False
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _require_binary(cmd: str, c: C) -> bool:
    if shutil.which(cmd) is None:
        sys.stderr.write(f"\n  {c.rose}✗ '{cmd}' was not found in your PATH.{c.reset}\n")
        try:
            input(f"  {c.dim}Press Enter to continue...{c.reset}")
        except (EOFError, KeyboardInterrupt):
            pass
        return False
    return True


def play_with_mpv(stream_url: str, c: C, referer: str | None = None) -> None:
    if not _require_binary("mpv", c):
        return
    if not is_valid_stream_url(stream_url):
        sys.stderr.write(
            f"\n  {c.rose}✗ Resolved value doesn't look like a playable URL: "
            f"{stream_url!r}{c.reset}\n"
        )
        try:
            input(f"  {c.dim}Press Enter to continue...{c.reset}")
        except (EOFError, KeyboardInterrupt):
            pass
        return

    effective_referer = referer or stream_url
    parsed = urllib.parse.urlparse(effective_referer)
    origin = f"{parsed.scheme}://{parsed.netloc}"

    cmd = [
        "mpv",
        stream_url,
        f"--http-header-fields=Referer: {effective_referer},Origin: {origin}",
        f"--user-agent={USER_AGENT}",
        "--demuxer-readahead-secs=10",
    ]
    print(f"\n  {c.sky}▶ Launching mpv…{c.reset}  {c.dim}(close the player to return){c.reset}\n")
    try:
        subprocess.run(cmd)
    except KeyboardInterrupt:
        pass
    except Exception as e:
        sys.stderr.write(f"  {c.rose}✗ mpv exited with an error: {e}{c.reset}\n")


def open_in_browser(url: str, c: C) -> None:
    if not is_valid_stream_url(url):
        sys.stderr.write(
            f"\n  {c.rose}✗ Resolved value doesn't look like a valid URL: {url!r}{c.reset}\n"
        )
        try:
            input(f"  {c.dim}Press Enter to continue...{c.reset}")
        except (EOFError, KeyboardInterrupt):
            pass
        return
    print(f"\n  {c.sky}▶ Opening embed URL in your browser…{c.reset}\n")
    try:
        opened = webbrowser.open(url, new=2)
        if not opened:
            sys.stderr.write(
                f"  {c.rose}✗ Could not find a browser to open the URL.{c.reset}\n"
                f"  {c.dim}URL: {url}{c.reset}\n"
            )
    except Exception as e:
        sys.stderr.write(f"  {c.rose}✗ Failed to open browser: {e}{c.reset}\n")


def choose_playback_action(c: C, is_admin: bool = False) -> str | None:
    if is_admin:
        rows = [
            f"{c.amber}⊞{c.reset}  Open embed URL in browser",
            f"{c.slate}i{c.reset}  Show stream details",
            f"{c.dim}←{c.reset}  Back to stream list",
        ]
    else:
        rows = [
            f"{c.sage}▶{c.reset}  Play in {c.bold}mpv{c.reset}",
            f"{c.amber}⊞{c.reset}  Open embed URL in browser",
            f"{c.slate}i{c.reset}  Show stream details",
            f"{c.dim}←{c.reset}  Back to stream list",
        ]
    idx = pick_from_list("What would you like to do?", rows, c=c)
    if idx is None:
        return None
    if is_admin:
        return ["browser", "details", None][idx]
    return ["mpv", "browser", "details", None][idx]


# ---------------------------------------------------------------------------
# Main CLI Flow — nested loops so ESC goes back one level
# ---------------------------------------------------------------------------

def run(use_color: bool, play_only: bool, base_url: str | None = None) -> int:
    c = C(use_color)
    base = (base_url or DEFAULT_BASE).rstrip("/")

    with StreamedClient(base_url=base) as client:
        print_banner(c, base)

        # --- Load sports once ---
        try:
            sports_raw = fetch_with_spinner("Fetching sports list", client.fetch_sports, c)
        except Exception as e:
            sys.stderr.write(f"{c.rose}✗ Failed to fetch sports: {e}{c.reset}\n")
            return 1

        sports = [Sport.from_raw(raw) for raw in sports_raw]
        if not any(s.id.lower() == "popular" for s in sports):
            sports.insert(0, Sport(id="popular", name="Popular"))

        sport_rows = [sport_row(s, c) for s in sports]

        # ========== LEVEL 1: SPORT ==========
        while True:
            sport_idx = pick_from_list(
                f"Select a sport  {c.dim}({len(sports)} sports){c.reset}",
                sport_rows,
                c=c,
            )
            if sport_idx is None:
                print(f"\n{c.slate}  cancelled.{c.reset}")
                return 0

            chosen_sport = sports[sport_idx]
            print(f"\n  {c.bold}Selected sport:{c.reset} {c.sky}{chosen_sport.name}{c.reset}\n")

            # --- Load matches for this sport ---
            try:
                if chosen_sport.id.lower() == "popular":
                    matches_raw = fetch_with_spinner(
                        "Fetching popular matches", client.fetch_popular_matches, c
                    )
                else:
                    matches_raw = fetch_with_spinner(
                        f'Fetching matches for "{chosen_sport.name}"',
                        lambda: client.fetch_matches_by_sport(chosen_sport.id),
                        c,
                    )
            except Exception as e:
                sys.stderr.write(f"{c.rose}✗ Failed to fetch matches: {e}{c.reset}\n")
                continue

            matches = [Match.from_raw(raw) for raw in matches_raw]
            if not matches:
                sys.stderr.write(f"{c.rose}✗ No matches found for {chosen_sport.name!r}{c.reset}\n")
                continue

            match_rows = [match_row(m, c) for m in matches]

            # ========== LEVEL 2: MATCH ==========
            while True:
                match_idx = pick_from_list(
                    f"Select a match  {c.dim}({len(matches)} matches){c.reset}",
                    match_rows,
                    c=c,
                )
                if match_idx is None:
                    break  # back to sport list

                chosen_match = matches[match_idx]
                print(f"\n  {c.bold}Selected match:{c.reset} {c.sky}{chosen_match.display_title()}{c.reset}\n")

                # --- Load streams for this match ---
                try:
                    streams_raw = fetch_with_spinner(
                        f'Fetching streams for "{chosen_match.display_title()}"',
                        lambda: client.fetch_streams_for_match(chosen_match),
                        c,
                    )
                except Exception as e:
                    sys.stderr.write(f"{c.rose}✗ Failed to fetch streams: {e}{c.reset}\n")
                    continue

                streams = [Stream.from_raw(raw) for raw in streams_raw]
                if not streams:
                    sys.stderr.write(f"{c.rose}✗ No streams available for this match{c.reset}\n")
                    continue

                stream_rows = [stream_row(s, c) for s in streams]

                # ========== LEVEL 3: STREAM ==========
                while True:
                    stream_idx = pick_from_list(
                        f"Select a stream  {c.dim}({len(streams)} streams){c.reset}",
                        stream_rows,
                        c=c,
                    )
                    if stream_idx is None:
                        break  # back to match list

                    chosen_stream = streams[stream_idx]
                    print(f"\n  {c.bold}Selected stream:{c.reset} {c.sky}#{chosen_stream.stream_no} {chosen_stream.language}{c.reset}")

                    # --- Admin stream handling ---
                    if chosen_stream.is_admin:
                        print(f"\n  {c.warn}⚠ Admin streams cannot be extracted to m3u8 automatically.{c.reset}")
                        print(f"  {c.dim}They require a browser with JavaScript execution.{c.reset}")

                        if play_only:
                            print(chosen_stream.embed_url)
                            return 0

                        # ========== LEVEL 4: ACTION (admin) ==========
                        while True:
                            action = choose_playback_action(c, is_admin=True)
                            if action is None:
                                break  # back to stream list
                            elif action == "browser":
                                open_in_browser(chosen_stream.embed_url, c)
                            elif action == "details":
                                print_stream_details(chosen_stream, None, c)
                                try:
                                    input(f"  {c.dim}Press Enter to continue...{c.reset}")
                                except (EOFError, KeyboardInterrupt):
                                    pass
                        continue

                    # --- Non-admin: resolve m3u8 ---
                    try:
                        stream_url, stream_referer = fetch_with_spinner(
                            f'Resolving stream URL for "{chosen_stream.language}"',
                            lambda: client.fetch_stream_url(chosen_stream.embed_url),
                            c,
                        )
                    except Exception as e:
                        sys.stderr.write(f"{c.rose}✗ Failed to resolve stream: {e}{c.reset}\n")
                        continue

                    if play_only:
                        if stream_url and is_valid_stream_url(stream_url):
                            print(stream_url)
                            return 0
                        sys.stderr.write(f"{c.rose}✗ Could not resolve a stream URL{c.reset}\n")
                        return 1

                    if not stream_url:
                        print_stream_details(chosen_stream, stream_url, c, referer=stream_referer)
                        try:
                            input(f"  {c.dim}Press Enter to continue...{c.reset}")
                        except (EOFError, KeyboardInterrupt):
                            pass
                        continue

                    # ========== LEVEL 4: ACTION (regular) ==========
                    while True:
                        action = choose_playback_action(c, is_admin=False)
                        if action is None:
                            break  # back to stream list
                        elif action == "mpv":
                            play_with_mpv(stream_url, c, referer=stream_referer)
                            break  # after mpv closes, back to stream list
                        elif action == "browser":
                            open_in_browser(chosen_stream.embed_url, c)
                        elif action == "details":
                            print_stream_details(chosen_stream, stream_url, c, referer=stream_referer)
                            try:
                                input(f"  {c.dim}Press Enter to continue...{c.reset}")
                            except (EOFError, KeyboardInterrupt):
                                pass

    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="streamed.py",
        description="Browse Streamed.pk / Streamed.st sports streams and extract m3u8 URLs."
    )
    p.add_argument(
        "--raw",
        action="store_true",
        help="Disable ANSI colors."
    )
    p.add_argument(
        "--play",
        action="store_true",
        help="Only print the decrypted m3u8 stream URL (or embed URL for admin streams)."
    )
    p.add_argument(
        "--base",
        metavar="URL",
        default=DEFAULT_BASE,
        help=f"API base URL to use (default: {DEFAULT_BASE}). "
             "You can also set the STREAMED_BASE environment variable."
    )
    args = p.parse_args(argv if argv is not None else sys.argv[1:])
    return run(enable_colors(args.raw), args.play, base_url=args.base)


if __name__ == "__main__":
    raise SystemExit(main())
