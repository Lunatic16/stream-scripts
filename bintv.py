#!/usr/bin/env python3
"""
bintv_picker.py — browse bintv.cc event index in your terminal and emit the
stream URLs for any event and any of its streams.

BINTV.cc loads all event data dynamically via JavaScript. This script uses
Playwright to render the page and extract event data from onclick handlers.

Each event has:
    - title    : display name
    - category : sport category  
    - date     : Unix timestamp (milliseconds)
    - status   : "Live" if currently live
    - sources  : list of stream sources with URLs

Stream URLs are either:
    - Direct m3u8 links
    - Proxy links through prabashsapkota.github.io/noooooads/?src=...
    - Embed pages at embedindia.st/embed/...

Usage:
    python bintv_picker.py                 # interactive event + stream picker
    python bintv_picker.py --raw           # disable ANSI colors (auto when piped)
    python bintv_picker.py --category Soccer  # filter to specific category
    python bintv_picker.py --live-only     # show only live events
    python bintv_picker.py --list          # plain text list (non-interactive)
    python bintv_picker.py --json          # JSON output for scripting

Dependencies: Python 3.10+ + playwright + httpx.
              Install: pip install playwright httpx
              Then: playwright install chromium
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import threading
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse, parse_qs

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
except ImportError:
    sys.stderr.write("error: this script needs playwright.\n")
    sys.stderr.write("       install with: pip install playwright\n")
    sys.stderr.write("       then run: playwright install chromium\n")
    sys.exit(2)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BASE_URL       = "https://www.bintv.cc"
TIMEOUT        = 30000  # 30 seconds for page load
ONCLICK_PATTERN = re.compile(r'handleMatchClick\(\s*({.+?})\s*\)', re.DOTALL)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class Stream:
    source_name: str
    url: str
    is_m3u8: bool = False
    is_proxy: bool = False
    
    def __post_init__(self):
        self.is_m3u8 = self.url.endswith('.m3u8')
        self.is_proxy = 'noooooads' in self.url
    
    @property
    def direct_url(self) -> str:
        """Extract the actual stream URL from behind the proxy."""
        if not self.is_proxy:
            return self.url
        parsed = urlparse(self.url)
        params = parse_qs(parsed.query)
        if 'src' in params:
            return params['src'][0]
        return self.url


@dataclass
class Event:
    title: str
    category: str
    date: int              # Unix timestamp (ms)
    status: str            # "Live" or empty
    poster: str | None
    sources: list[Stream] = field(default_factory=list)
    viewers: int = 0
    ends_at: int = 0
    
    @property
    def is_live(self) -> bool:
        return self.status == "Live"
    
    @property
    def timestamp_sec(self) -> int:
        return self.date // 1000 if self.date else 0
    
    @classmethod
    def from_json(cls, data: dict) -> "Event":
        sources = []
        for src in data.get('sources', []):
            sources.append(Stream(
                source_name=src.get('source', 'Unknown'),
                url=src.get('url', ''),
            ))
        
        return cls(
            title=data.get('title', '?'),
            category=data.get('category', 'Unknown'),
            date=data.get('date', 0),
            status=data.get('status', ''),
            poster=data.get('poster'),
            sources=sources,
            viewers=data.get('viewers', 0),
            ends_at=data.get('endsAt', 0),
        )


# ---------------------------------------------------------------------------
# Terminal helpers
# ---------------------------------------------------------------------------

import os

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
        self.dim    = "\033[2m"
        self.bold   = "\033[1m"
        self.reset  = "\033[0m"
        self.sky    = "\033[38;5;110m"
        self.amber  = "\033[38;5;179m"
        self.slate  = "\033[38;5;246m"
        self.sage   = "\033[38;5;108m"
        self.warn   = "\033[38;5;173m"
        self.rose   = "\033[38;5;204m"
        self.bg_sel = "\033[48;5;237m"

    def __getattr__(self, name: str) -> str:
        return ""


def _term_width(default: int = 100) -> int:
    try:
        return max(40, os.get_terminal_size().columns)
    except (OSError, ValueError):
        return default


def _term_height(default: int = 24) -> int:
    try:
        return max(10, os.get_terminal_size().lines)
    except (OSError, ValueError):
        return default


def truncate_str(s: str, width: int) -> str:
    if len(s) > width:
        return s[:width-3] + "..."
    return s.ljust(width)


def format_time(timestamp_ms: int) -> tuple[str, str]:
    if not timestamp_ms:
        return "—", "scheduled"
    ts = timestamp_ms // 1000
    now = int(time.time())
    if ts <= now:
        state = "live"
    elif ts - now < 86400:
        state = "soon"
    else:
        state = "scheduled"
    txt = time.strftime("%b %d %I:%M %p %Z", time.localtime(ts)).strip()
    return txt, state


# ---------------------------------------------------------------------------
# Banner + spinner
# ---------------------------------------------------------------------------

def print_banner(url: str, c: C) -> None:
    w = min(_term_width(), 30)
    inner = w - 2
    title = "BINTV.cc  Streams"
    
    t_pad = max(0, inner - len(title) - 2)
    
    print(f"{c.sky}╭{'─' * inner}╮{c.reset}")
    print(f"{c.sky}│{c.reset}  {c.bold}{c.sky}{title}{c.reset}{' ' * t_pad}{c.sky}│{c.reset}")
    print(f"{c.sky}│{c.reset}  {c.slate}{url}{c.reset}{' ' * (inner - len(url) - 2)}{c.sky}│{c.reset}")
    print(f"{c.sky}╰{'─' * inner}╯{c.reset}")
    print()


def run_spinner(label: str, fn, c: C):
    if not sys.stdout.isatty():
        sys.stdout.write(f"  {label}…\n")
        sys.stdout.flush()
        return fn()
    
    frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    result = [None]
    exc = [None]
    done = threading.Event()
    
    def worker():
        try:
            result[0] = fn()
        except Exception as e:
            exc[0] = e
        finally:
            done.set()
    
    threading.Thread(target=worker, daemon=True).start()
    
    i = 0
    while not done.wait(0.08):
        sys.stdout.write(f"\r  {c.sky}{frames[i % len(frames)]}{c.reset}  {c.slate}{label}{c.reset}\033[K")
        sys.stdout.flush()
        i += 1
    
    sys.stdout.write("\r\033[K")
    sys.stdout.flush()
    
    if exc[0]:
        raise exc[0]
    return result[0]


# ---------------------------------------------------------------------------
# Browser client
# ---------------------------------------------------------------------------

def fetch_events() -> list[Event]:
    """Use Playwright to load the page and extract event data from onclick handlers."""
    
    events: list[Event] = []
    seen: set[str] = set()
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        )
        
        try:
            page.goto(BASE_URL, wait_until="domcontentloaded", timeout=TIMEOUT)
            # Wait for content to render
            page.wait_for_timeout(3000)  # Give JS time to render the event cards
            
            # Extract all onclick handlers from the DOM
            onclick_data = page.evaluate("""
                () => {
                    return [...document.querySelectorAll('[onclick]')]
                        .map(el => el.getAttribute('onclick'))
                        .filter(onclick => onclick && onclick.includes('handleMatchClick'));
                }
            """)
            
            for onclick in onclick_data:
                match = ONCLICK_PATTERN.search(onclick)
                if not match:
                    continue
                
                try:
                    data = json.loads(match.group(1))
                    title = data.get('title', '')
                    
                    if title in seen:
                        continue
                    seen.add(title)
                    
                    events.append(Event.from_json(data))
                except (json.JSONDecodeError, KeyError) as e:
                    continue
        
        except PlaywrightTimeout:
            raise RuntimeError(f"Page load timed out after {TIMEOUT}ms")
        finally:
            browser.close()
    
    return events


# ---------------------------------------------------------------------------
# Pickers
# ---------------------------------------------------------------------------

import re as regex_module
ANSI_RE = regex_module.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")

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


def pick_from_list(title: str, rows: list[str], *, c: C, header_row: str | None = None) -> int | None:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return _pick_plain(title, rows, header_row)
    
    import termios, tty
    
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
        
        def render():
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
                legend = f"{c.dim}  ↑↓ navigate · type to filter · Enter select · Esc cancel  [{len(idxs)}/{len(rows)}]{c.reset}"
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
# Row formatting
# ---------------------------------------------------------------------------

def event_row(ev: Event, name_w: int, cat_w: int, time_w: int, c: C) -> str:
    when, state = format_time(ev.date)
    
    if ev.is_live:
        badge = f"{c.warn}{c.bold}●LIVE{c.reset}"
    elif state == "soon":
        badge = f"{c.amber}SOON {c.reset}"
    else:
        badge = f"{c.dim}SCHED{c.reset}"
    
    time_color = {"live": c.warn, "soon": c.amber, "scheduled": c.slate}.get(state, c.slate)
    
    disp_name = truncate_str(ev.title, name_w)
    disp_cat = truncate_str(ev.category, cat_w) if cat_w > 0 else ""
    disp_time = truncate_str(when, time_w)
    
    return f"{badge}  {c.bold}{disp_name}{c.reset}  {c.dim}{disp_cat}{c.reset}  {time_color}{disp_time}{c.reset}"


def stream_row(stream: Stream, c: C) -> str:
    type_badge = f"{c.sage}M3U8{c.reset}" if stream.is_m3u8 else f"{c.sky}LINK{c.reset}"
    proxy_badge = f" {c.dim}(proxy){c.reset}" if stream.is_proxy else ""
    return f" {type_badge}  {c.bold}{stream.source_name}{c.reset}{proxy_badge}"


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def print_stream_details(stream: Stream, event_title: str, c: C) -> None:
    type_badge = f"{c.sage}{c.bold}★ M3U8{c.reset}" if stream.is_m3u8 else f"{c.sky}{c.bold}▶ LINK{c.reset}"
    
    print(f"\n  {c.sky}┌──{c.reset} {c.bold}STREAM DETAILS{c.reset} {c.sky}{'─' * 50}┐{c.reset}")
    print(f"  {c.sky}│{c.reset}  {c.bold}Event:{c.reset}  {c.bold}{event_title}{c.reset}")
    print(f"  {c.sky}│{c.reset}  {c.bold}Source:{c.reset} {type_badge} {c.bold}{stream.source_name}{c.reset}")
    print(f"  {c.sky}│{c.reset}  {c.bold}URL:{c.reset}")
    
    url = stream.direct_url
    wrap_width = _term_width() - 14
    while url:
        chunk = url[:wrap_width]
        url = url[wrap_width:]
        print(f"  {c.sky}│{c.reset}    {c.sky}{chunk}{c.reset}")
    
    if stream.is_proxy:
        print(f"  {c.sky}│{c.reset}  {c.bold}Proxy:{c.reset} {c.dim}{stream.url}{c.reset}")
    
    print(f"  {c.sky}└────────────────────────────────────────────────────────────────────┘{c.reset}\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def get_column_widths(width: int) -> tuple[int, int, int]:
    overhead = 35
    rem = width - overhead
    if rem < 20:
        return max(20, rem), 0, 15
    cat_w = min(15, max(10, int(rem * 0.2)))
    time_w = 18
    name_w = rem - cat_w - time_w
    return name_w, cat_w, time_w


def run(category_filter: str | None, live_only: bool, use_color: bool, list_mode: bool = False, json_output: bool = False) -> int:
    c = C(use_color)
    
    if not json_output:
        print_banner(BASE_URL, c)
    
    try:
        events = run_spinner("fetching events", fetch_events, c)
    except Exception as e:
        sys.stderr.write(f"{c.rose}✗  {e}{c.reset}\n")
        return 1
    
    if not events:
        sys.stderr.write(f"{c.rose}✗  no events found{c.reset}\n")
        return 1
    
    # Filters
    if category_filter:
        events = [e for e in events if e.category.lower() == category_filter.lower()]
    if live_only:
        now = int(time.time())
        events = [e for e in events if e.is_live or (e.timestamp_sec and e.timestamp_sec <= now)]
    
    if not events:
        sys.stderr.write(f"{c.rose}✗  no events match the filter{c.reset}\n")
        return 1
    
    # Sort
    events.sort(key=lambda e: (not e.is_live, e.date or 0))
    
    # JSON mode
    if json_output:
        output = []
        for ev in events:
            output.append({
                "title": ev.title,
                "category": ev.category,
                "date": ev.date,
                "is_live": ev.is_live,
                "streams": [
                    {
                        "source": s.source_name,
                        "url": s.direct_url,
                        "proxy_url": s.url,
                        "is_m3u8": s.is_m3u8,
                    }
                    for s in ev.sources
                ],
            })
        print(json.dumps(output, indent=2))
        return 0
    
    # List mode
    if list_mode:
        for ev in events:
            when, state = format_time(ev.date)
            badge = "●LIVE" if ev.is_live or state == "live" else "SOON" if state == "soon" else "SCHED"
            print(f"\n{badge}  {ev.title}")
            print(f"  Category: {ev.category}  |  Time: {when}")
            for s in ev.sources:
                type_str = "[M3U8]" if s.is_m3u8 else "[LINK]"
                proxy_str = " (via proxy)" if s.is_proxy else ""
                print(f"    {type_str} {s.source_name}{proxy_str}: {s.direct_url}")
        return 0
    
    # Interactive
    if not sys.stdout.isatty():
        sys.stderr.write("note: interactive mode requires TTY; use --list or --json for scripted output\n")
        return 1
    
    width = _term_width()
    name_w, cat_w, time_w = get_column_widths(width - 4)
    
    hdr = f"{c.bold}{c.slate}{'STATUS'.ljust(5)}  {'EVENT'.ljust(name_w)}  {'CATEGORY'.ljust(cat_w) if cat_w > 0 else ''}  {'TIME'.ljust(time_w)}{c.reset}"
    rows = [event_row(e, name_w, cat_w, time_w, c) for e in events]
    
    idx = pick_from_list(f"Select an event  {c.dim}({len(events)} available){c.reset}", rows, c=c, header_row=hdr)
    if idx is None:
        print(f"\n{c.slate}  cancelled.{c.reset}")
        return 0
    
    chosen = events[idx]
    print(f"\n  {c.bold}Event:{c.reset} {c.sky}{chosen.title}{c.reset}")
    print(f"  {c.bold}Category:{c.reset} {c.dim}{chosen.category}{c.reset}")
    
    if not chosen.sources:
        sys.stderr.write(f"{c.rose}✗  no streams available{c.reset}\n")
        return 1
    
    rows2 = [stream_row(s, c) for s in chosen.sources]
    idx2 = pick_from_list(f"Select a stream  {c.dim}({len(chosen.sources)} available){c.reset}", rows2, c=c)
    if idx2 is None:
        print(f"\n{c.slate}  cancelled.{c.reset}")
        return 0
    
    print_stream_details(chosen.sources[idx2], chosen.title, c)
    
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="bintv_picker.py", description="Browse bintv.cc events from the terminal.")
    p.add_argument("--category", "-c", default=None, help="Filter to specific category")
    p.add_argument("--live-only", "-l", action="store_true", help="Show only live events")
    p.add_argument("--raw", action="store_true", help="Disable ANSI colors")
    p.add_argument("--list", action="store_true", dest="list_mode", help="Plain text list output")
    p.add_argument("--json", action="store_true", help="JSON output")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    return run(args.category, args.live_only, enable_colors(not args.raw), args.list_mode, args.json)


if __name__ == "__main__":
    raise SystemExit(main())
