#!/usr/bin/env python3
"""
ppv.py — browse the ppv.st event index in your terminal and emit the
embed URLs / details for any event and any of its substreams.

This version adds a post-source-selection action menu:

    What would you like to do?
      filter › ▌
      ⊞  Open embed URL in browser
      i  Show stream details
      ←  Back to channel list

Usage:
    python ppv.py
    python ppv.py --raw
    python ppv.py --api https://api.ppv.st/api
    python ppv.py --show-default

Dependencies: Python 3.10+ stdlib + httpx.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
import threading
import urllib.parse
import webbrowser
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

DEFAULT_API_BASE = "https://api.ppv.st/api"
ALT_API_BASES = (
    "https://api.ppv.is/api",
    "https://api.ppv.lc/api",
    "https://api.ppv.cx/api",
)

USER_AGENT = "ppv_picker/1.0 (+https://ppv.st) curl/8"
TIMEOUT = 15.0

API_DOMAINS = ("ppv.st", "ppv.is", "ppv.lc", "ppv.cx")


def _build_api_chain(requested_base: str) -> list[str]:
    """Return an ordered list of API bases to try."""
    seen: set[str] = set()
    chain: list[str] = []

    def add(b: str) -> None:
        b = b.rstrip("/")
        if b and b not in seen:
            seen.add(b)
            chain.append(b)

    # 1) user-requested base
    add(requested_base or DEFAULT_API_BASE)

    # 2) recognized alternates
    for alt in ALT_API_BASES:
        add(alt)

    # 3) any API_DOMAINS not already in the chain
    for d in API_DOMAINS:
        add(f"https://api.{d}/api")

    return chain


class _AllBasesFailed(RuntimeError):
    """Raised when every API base in the failover chain errors out."""

    def __init__(self, attempted: list[str], last_error: str):
        super().__init__(
            f"all {len(attempted)} API base(s) failed; last error: {last_error}"
        )
        self.attempted = attempted
        self.last_error = last_error


def _recover_uri_from_iframe(iframe_url: str) -> str:
    if not iframe_url:
        return ""
    return re.sub(r"^https?://[^/]+/embed/", "", iframe_url)


@dataclass
class Embed:
    label: str
    uri: str | None
    locale: str | None
    iframe_url: str
    is_default: bool = False

    def ppv_url(self, host: str = "ppv.st", event_uri: str | None = None) -> str:
        """Best-effort shareable URL."""
        tail = ""

        if self.uri:
            tail = self.uri
            if event_uri and not tail.startswith(event_uri):
                tail = f"{event_uri.rstrip('/')}/{tail.lstrip('/')}"
        elif self.iframe_url:
            tail = _recover_uri_from_iframe(self.iframe_url)

        return f"https://{host}/live/{tail}" if tail else f"https://{host}/"


# ---------------------------------------------------------------------------
# Terminal helpers
# ---------------------------------------------------------------------------


def enable_colors(disable: bool) -> bool:
    """Return True if we should emit ANSI colors."""
    if disable:
        return False
    if not sys.stdout.isatty():
        return False
    if os.environ.get("NO_COLOR"):
        return False
    return os.environ.get("TERM", "") != "dumb"


class C:
    """ANSI palette — 256-color sky/amber/slate theme."""

    def __init__(self, on: bool):
        self.on = on
        if not on:
            return

        self.dim = "\033[2m"
        self.bold = "\033[1m"
        self.reset = "\033[0m"

        # 256-color foregrounds
        self.sky = "\033[38;5;110m"    # cornflower blue
        self.amber = "\033[38;5;179m"  # warm gold
        self.slate = "\033[38;5;246m"  # muted grey
        self.sage = "\033[38;5;108m"   # muted green
        self.warn = "\033[38;5;173m"   # orange-red
        self.rose = "\033[38;5;204m"   # bright rose

        # backgrounds
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
    if width <= 0:
        return ""
    if len(s) > width:
        if width >= 3:
            return s[: width - 3] + "..."
        return s[:width]
    return s.ljust(width)


def get_column_widths(width: int) -> tuple[int, int, int, int]:
    overhead = 47
    rem = width - overhead

    if rem < 20:
        name_w = max(15, rem)
        cat_w = 0
    else:
        cat_w = min(20, max(10, int(rem * 0.25)))
        name_w = rem - cat_w

    return name_w, 15, 19, cat_w


def format_start(unix_ts: int, ends_at: int = 0) -> tuple[str, str]:
    """Return (human-readable time string, state)."""
    if not unix_ts:
        return "—", "info"

    now = int(time.time())

    if ends_at and ends_at < now:
        state = "ended"
    elif unix_ts <= now:
        state = "live"
    elif unix_ts - now < 86400:
        state = "soon"
    else:
        state = "info"

    txt = time.strftime("%b %d %I:%M %p %Z", time.localtime(unix_ts)).strip()
    return txt, state


def pause(c: C, message: str = "Press Enter to continue...") -> None:
    try:
        input(f"  {c.dim}{message}{c.reset}")
    except (EOFError, KeyboardInterrupt):
        pass


# ---------------------------------------------------------------------------
# Banner + spinner
# ---------------------------------------------------------------------------


def print_banner(api_base: str, c: C) -> None:
    w = min(_term_width(), 28)
    inner = w - 2

    title = "ppv.st  Stream Links"
    sub = api_base

    t_pad = max(0, inner - len(title) - 2)
    s_pad = max(0, inner - len(sub) - 2)

    print(f"{c.sky}╭{'─' * inner}╮{c.reset}")
    print(f"{c.sky}│{c.reset}  {c.bold}{c.sky}{title}{c.reset}{' ' * t_pad}{c.sky}│{c.reset}")
    print(f"{c.sky}│{c.reset}  {c.slate}{sub}{c.reset}{' ' * s_pad}{c.sky}│{c.reset}")
    print(f"{c.sky}╰{'─' * inner}╯{c.reset}")
    print()


def fetch_with_spinner(label: str, fn, c: C):
    """Call fn() while animating a braille spinner. Returns the result."""
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


# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------


class PPVClient:
    def __init__(
        self,
        api_base: str = DEFAULT_API_BASE,
        client: httpx.Client | None = None,
        api_chain: list[str] | None = None,
    ):
        self.api_chain = list(api_chain) if api_chain else _build_api_chain(api_base)
        self.api_base = self.api_chain[0]

        own_client = client is None
        self.client = client or httpx.Client(
            headers={
                "User-Agent": USER_AGENT,
                "Origin": "https://ppv.st",
                "Referer": "https://ppv.st/",
                "Accept": "application/json",
            },
            timeout=TIMEOUT,
            follow_redirects=True,
        )
        self._owns_client = own_client

    def close(self) -> None:
        if self._owns_client and self.client is not None:
            self.client.close()

    def __enter__(self) -> "PPVClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---------- failover plumbing ----------

    @staticmethod
    def _unrecoverable(exc: BaseException) -> bool:
        """
        A direct LookupError means a real 'not found' result; do not failover
        past it. KeyError/IndexError are treated as malformed responses and
        are failover-eligible.
        """
        return type(exc) is LookupError

    def _with_failover(self, op_name: str, call) -> tuple[Any, str]:
        """
        Try call(self.client, base) against each base in api_chain in order.
        Returns (result, used_base) on success.
        """
        attempted: list[str] = []
        last_err: BaseException | None = None

        for base in self.api_chain:
            attempted.append(base)
            self.api_base = base

            try:
                result = call(self.client, base)
                return result, base
            except Exception as e:
                if self._unrecoverable(e):
                    raise
                last_err = e
                continue

        raise _AllBasesFailed(
            attempted=attempted,
            last_error=repr(last_err) if last_err else "unknown",
        )

    # ---------- endpoints ----------

    def index(self) -> list[dict[str, Any]]:
        """Returns the flat streams list from /api/streams."""

        def call(client: httpx.Client, base: str) -> list[dict[str, Any]]:
            r = client.get(f"{base}/streams")
            r.raise_for_status()

            d = r.json()
            if not d.get("success"):
                raise RuntimeError(f"index: server returned success=false: {d}")

            return d.get("streams") or []

        result, _ = self._with_failover("index", call)
        return result

    def event(self, uri_path: str) -> dict[str, Any]:
        """Returns the data object from /api/streams/<uri-path>."""

        def call(client: httpx.Client, base: str) -> dict[str, Any]:
            r = client.get(f"{base}/streams/{uri_path}")

            if r.status_code == 404:
                raise LookupError(f"event: not found: {uri_path}")

            r.raise_for_status()

            d = r.json()
            if not d.get("success"):
                s = d.get("statusCode") or d.get("status_code")
                if s == 404:
                    raise LookupError(f"event: not found: {uri_path}")
                raise RuntimeError(f"event: server returned success=false: {d}")

            return d["data"]

        result, _ = self._with_failover("event", call)
        return result


# ---------------------------------------------------------------------------
# In-memory model
# ---------------------------------------------------------------------------


@dataclass
class Event:
    id: int
    name: str
    tag: str | None
    source_tag: str | None
    locale: str | None
    category_name: str | None
    uri: str
    poster: str | None
    starts_at: int
    ends_at: int
    viewers: int
    always_live: bool
    iframe: str | None
    substreams: list[dict] = field(default_factory=list)

    @classmethod
    def from_index(cls, cat_name: str, raw: dict) -> "Event":
        return cls(
            id=int(raw.get("id") or 0),
            name=str(raw.get("name") or "?"),
            tag=raw.get("tag"),
            source_tag=raw.get("source_tag"),
            locale=raw.get("locale"),
            category_name=cat_name,
            uri=str(raw.get("uri_name") or ""),
            poster=raw.get("poster"),
            starts_at=int(raw.get("starts_at") or 0),
            ends_at=int(raw.get("ends_at") or 0),
            viewers=int(raw.get("viewers") or 0),
            always_live=bool(raw.get("always_live")),
            iframe=raw.get("iframe"),
            substreams=list(raw.get("substreams") or []),
        )

    @classmethod
    def from_event(cls, raw: dict) -> "Event":
        """Newer events may not be in the index yet — use event endpoint as primary."""
        cat = raw.get("category_name") or "(?)"
        starts = int(raw.get("start_timestamp") or raw.get("starts_at") or 0)
        ends = int(raw.get("end_timestamp") or raw.get("ends_at") or 0)

        return cls(
            id=int(raw.get("id") or 0),
            name=str(raw.get("name") or "?"),
            tag=raw.get("tag"),
            source_tag=raw.get("source_tag"),
            locale=raw.get("locale"),
            category_name=cat,
            uri=str(raw.get("uri") or raw.get("uri_name") or ""),
            poster=raw.get("poster"),
            starts_at=starts,
            ends_at=ends,
            viewers=int(raw.get("viewers") or 0),
            always_live=bool(raw.get("always_live", raw.get("always_live_feed"))),
            iframe=raw.get("iframe") or raw.get("default_iframe"),
            substreams=list(raw.get("substreams") or []),
        )

    def embeds(self, default_iframe: str | None = None) -> list[Embed]:
        out: list[Embed] = []

        dflt = default_iframe or self.iframe
        if dflt:
            out.append(
                Embed(
                    label=f"{self.source_tag or 'Default'} (default)",
                    uri=None,
                    locale=self.locale,
                    iframe_url=dflt,
                    is_default=True,
                )
            )

        for sub in self.substreams:
            uri = sub.get("uri") or ""
            if not uri:
                uri = _recover_uri_from_iframe(sub.get("iframe") or "")

            out.append(
                Embed(
                    label=sub.get("source_tag") or uri or "Stream",
                    uri=uri,
                    locale=sub.get("locale"),
                    iframe_url=sub.get("iframe") or "",
                    is_default=False,
                )
            )

        return out


# ---------------------------------------------------------------------------
# Pretty-printer for embed output
# ---------------------------------------------------------------------------

IFRAME_TPL = (
    '<iframe id="player"\n'
    '        src="{src}"\n'
    '        marginheight="0" marginwidth="0"\n'
    '        scrolling="no" allowfullscreen="yes"\n'
    '        allow="encrypted-media; picture-in-picture;"\n'
    '        width="100%" height="100%" frameborder="0"\n'
    '        style="position:absolute;"></iframe>'
)


def print_embed(emb: Embed, ppv_host: str, event_uri: str | None, c: C) -> None:
    badge = (
        f"{c.sage}{c.bold}★ default{c.reset}"
        if emb.is_default
        else f"{c.sky}▶ substream{c.reset}"
    )
    loc = f" {c.dim}[{emb.locale}]{c.reset}" if emb.locale else ""
    ppv_url = emb.ppv_url(ppv_host, event_uri)

    print(f"\n{c.sky}┌──{c.reset} {c.bold}STREAM DETAILS{c.reset} {c.sky}{'─' * 50}┐{c.reset}")
    print(f"  {c.sky}│{c.reset}  {c.bold}Source:{c.reset}   {badge}{loc} {c.bold}{emb.label}{c.reset}")
    print(f"  {c.sky}│{c.reset}  {c.bold}PPV URL:{c.reset}  {c.sky}{ppv_url}{c.reset}")
    print(f"  {c.sky}│{c.reset}  {c.bold}Embed:{c.reset}    {c.sky}{emb.iframe_url}{c.reset}")
    print(f"  {c.sky}└────────────────────────────────────────────────────────────────────┘{c.reset}\n")


# ---------------------------------------------------------------------------
# Selection prompt — arrow keys + live filter, no third-party deps
# ---------------------------------------------------------------------------

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def strip_ansi(s: str) -> str:
    return ANSI_RE.sub("", s)


def visible_len(s: str) -> int:
    return len(strip_ansi(s))


def _read_key() -> str:
    """Read a single keypress from stdin (raw mode)."""
    ch = sys.stdin.read(1)

    if ch == "\x1b":
        nxt = sys.stdin.read(1)
        if nxt in ("[", "O"):
            code = sys.stdin.read(1)

            if code == "A":
                return "UP"
            if code == "B":
                return "DOWN"
            if code == "C":
                return "RIGHT"
            if code == "D":
                return "LEFT"
            if code == "H":
                return "UP"
            if code == "F":
                return "DOWN"

            # consume remaining CSI parameters
            while code not in ("~",):
                code = sys.stdin.read(1)
            return "?"

        return "ESC"

    if ch in ("\r", "\n"):
        return "ENTER"
    if ch in ("\x7f", "\b"):
        return "BACK"
    if ch == "\x03":
        return "CTRL_C"
    if ch == "\x04":
        return "CTRL_D"
    if ch.isprintable():
        return "TEXT:" + ch

    return ""


def _pick_plain(
    title: str,
    rows: list[str],
    prefilled: str,
    header_row: str | None = None,
) -> int | None:
    """Non-TTY fallback: numbered list + numeric input."""
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
    """
    Erase the block_height lines of the block, assuming the cursor is
    currently on the last line of the block.
    """
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
    prefilled: str = "",
    c: C,
    header_row: str | None = None,
) -> int | None:
    """
    Render a filterable arrow-key picker on a TTY.
    Returns the chosen row index, or None if cancelled.
    Falls back to a plain text prompt if stdin is not a TTY.
    """
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return _pick_plain(title, rows, prefilled, header_row)

    import termios, tty  # type: ignore

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)

    try:
        tty.setraw(fd)

        out = sys.stdout
        out.write("\033[?25l")  # hide cursor
        out.flush()

        header_len = 1 if header_row is not None else 0
        block_height = max(8, _term_height() - 1)
        list_room = max(3, block_height - 3 - header_len)

        query = prefilled
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

            # clamp cursor + scroll window
            if not idxs:
                cursor, scroll = 0, 0
            else:
                cursor = max(0, min(cursor, len(idxs) - 1))
                if cursor < scroll:
                    scroll = cursor
                elif cursor >= scroll + list_room:
                    scroll = cursor - list_room + 1

            # erase block and reposition at top
            if not first_render:
                out.write(f"\033[{block_height - 1}A\r")
            else:
                first_render = False

            # title
            out.write(f"{c.bold}{title}{c.reset}\033[K\r\n")

            # filter line
            out.write(
                f"  {c.slate}filter ›{c.reset} {query}"
                f"{c.dim}▌{c.reset}\033[K\r\n"
            )

            # header row
            if header_row is not None:
                out.write(f"  {header_row}\033[K\r\n")

            # list rows
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

            # legend
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
# Row formatting
# ---------------------------------------------------------------------------


def event_row(
    ev: Event,
    name_w: int,
    src_w: int,
    time_w: int,
    cat_w: int,
    c: C,
) -> str:
    when, state = format_start(ev.starts_at, ev.ends_at)

    if ev.always_live:
        badge = f"{c.sage}{c.bold}24/7 {c.reset}"
    elif state == "live":
        badge = f"{c.warn}{c.bold}●LIVE{c.reset}"
    elif state == "soon":
        badge = f"{c.amber}SOON {c.reset}"
    elif state == "ended":
        badge = f"{c.dim}DONE {c.reset}"
    else:
        badge = "     "

    time_color = {
        "live": c.warn,
        "soon": c.amber,
        "ended": c.dim,
        "info": c.slate,
    }.get(state, c.slate)

    disp_name = truncate_str(ev.name, name_w)
    name_str = f"{c.bold}{disp_name}{c.reset}"

    disp_src = truncate_str(ev.source_tag or "", src_w)
    src_str = f"{c.slate}{disp_src}{c.reset}"

    disp_time = truncate_str(when, time_w)
    time_str = f"{time_color}{disp_time}{c.reset}"

    disp_cat = truncate_str(ev.category_name or "", cat_w) if cat_w > 0 else ""
    cat_str = f"{c.dim}{disp_cat}{c.reset}"

    return f"{badge}  {name_str}  {src_str}  {time_str}  {cat_str}"


def embed_row(emb: Embed, c: C) -> str:
    if emb.is_default:
        marker = f"{c.sage}{c.bold}★{c.reset}"
        suffix = f"  {c.dim}(default){c.reset}"
    else:
        marker = f"{c.slate}◦{c.reset}"
        suffix = ""

    label = f"{c.bold}{emb.label}{c.reset}"
    loc = f"  {c.dim}{emb.locale}{c.reset}" if emb.locale else ""

    return f" {marker}  {label}{loc}{suffix}"


# ---------------------------------------------------------------------------
# Playback actions
# ---------------------------------------------------------------------------


def is_valid_url(url: str | None) -> bool:
    if not url:
        return False

    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return False

    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def open_in_browser(url: str, c: C) -> None:
    if not is_valid_url(url):
        sys.stderr.write(
            f"\n{c.rose}✗ Invalid URL: {url!r}{c.reset}\n"
        )
        pause(c)
        return

    print(f"\n{c.sky}▶ Opening embed URL in your browser…{c.reset}\n")

    try:
        opened = webbrowser.open(url, new=2)
        if not opened:
            sys.stderr.write(
                f"  {c.rose}✗ Could not find a browser to open the URL.{c.reset}\n"
                f"  {c.dim}URL: {url}{c.reset}\n"
            )
    except Exception as e:
        sys.stderr.write(f"  {c.rose}✗ Failed to open browser: {e}{c.reset}\n")


def choose_playback_action(c: C) -> str | None:
    rows = [
        f"{c.amber}⊞{c.reset}  Open embed URL in browser",
        f"{c.slate}i{c.reset}  Show stream details",
        f"{c.dim}←{c.reset}  Back to channel list",
    ]

    idx = pick_from_list("What would you like to do?", rows, c=c)
    if idx is None:
        return None

    return ["browser", "details", "back"][idx]


# ---------------------------------------------------------------------------
# CLI flow
# ---------------------------------------------------------------------------


def derive_ppv_host(api_base: str) -> str:
    for d in API_DOMAINS:
        if d in api_base:
            return d
    return "ppv.st"


def run(api_base: str, show_default: bool, use_color: bool) -> int:
    c = C(use_color)
    ppv_host = derive_ppv_host(api_base)
    chain = _build_api_chain(api_base)

    print_banner(chain[0], c)

    with PPVClient(api_base=api_base) as client:
        # ── fetch event index ────────────────────────────────────────────
        try:
            index = fetch_with_spinner(
                f"Fetching event index [{client.api_base}]",
                client.index,
                c,
            )
        except _AllBasesFailed as e:
            sys.stderr.write(
                f"\n{c.rose}✗  every API base failed ({len(e.attempted)} tried){c.reset}\n"
                f"{c.dim}   last error: {e.last_error}{c.reset}\n"
            )
            return 1
        except Exception as e:
            sys.stderr.write(f"{c.rose}✗  {e}{c.reset}\n")
            return 1

        if client.api_base != chain[0]:
            sys.stderr.write(
                f"  {c.amber}↻{c.reset}  {c.dim}using fallback {client.api_base}{c.reset}\n"
            )

        events: list[Event] = []

        for cat in index:
            cname = cat.get("category") or cat.get("category_name") or "(?)"
            for raw in cat.get("streams") or []:
                events.append(Event.from_index(cname, raw))

        if not events:
            sys.stderr.write(f"{c.rose}✗  no events found in index{c.reset}\n")
            return 1

        # Calculate dynamic column layout
        width = _term_width()
        name_w, src_w, time_w, cat_w = get_column_widths(width - 4)

        hdr_status = "STATUS".ljust(5)
        hdr_name = "EVENT NAME".ljust(name_w)
        hdr_source = "SOURCE".ljust(src_w)
        hdr_time = "START TIME".ljust(time_w)
        hdr_category = "CATEGORY".ljust(cat_w) if cat_w > 0 else ""

        header_row = (
            f"{c.bold}{c.slate}"
            f"{hdr_status}  {hdr_name}  {hdr_source}  {hdr_time}  {hdr_category}"
            f"{c.reset}"
        )

        events.sort(key=lambda e: (e.always_live, e.starts_at, e.category_name or ""))
        rows = [event_row(e, name_w, src_w, time_w, cat_w, c) for e in events]

        # ── event picker loop ─────────────────────────────────────────────
        while True:
            idx = pick_from_list(
                f"Select an event  {c.dim}({len(events)} on offer){c.reset}",
                rows,
                c=c,
                header_row=header_row,
            )

            if idx is None:
                print(f"\n{c.slate}  cancelled.{c.reset}")
                return 0

            chosen = events[idx]

            print(
                f"\n{c.bold}Event:{c.reset} {c.sky}{chosen.name}{c.reset}"
                f"  {c.dim}({chosen.uri}){c.reset}"
            )

            # ── fetch per-event detail ───────────────────────────────────
            detail: dict[str, Any] | None = None

            try:
                detail = fetch_with_spinner(
                    f"Fetching detail for \"{chosen.name}\"",
                    lambda: client.event(chosen.uri),
                    c,
                )
            except LookupError as e:
                sys.stderr.write(f"\n{c.warn}  ⚠  {e}{c.reset}\n")
            except Exception as e:
                sys.stderr.write(f"\n{c.warn}  ⚠  event detail failed: {e}{c.reset}\n")

            if detail:
                fresh = Event.from_event(detail)

                # Prefer index-provided lists/iframe when present, but keep
                # anything extra the detail endpoint supplied.
                fresh.substreams = chosen.substreams or fresh.substreams
                fresh.iframe = chosen.iframe or fresh.iframe
                chosen = fresh

            embeds = chosen.embeds()

            if not embeds:
                sys.stderr.write(f"{c.rose}✗  no playable sources found{c.reset}\n")
                pause(c, "Press Enter to return to the event list...")
                continue

            if show_default and embeds and embeds[0].is_default:
                print(f"\n{c.sage}↳ default feed (auto-included){c.reset}")
                print_embed(embeds[0], ppv_host, chosen.uri, c)

            # ── source picker loop ───────────────────────────────────────
            while True:
                rows2 = [embed_row(e, c) for e in embeds]

                idx2 = pick_from_list(
                    f"Select a source  {c.dim}({len(embeds)} available){c.reset}",
                    rows2,
                    c=c,
                )

                if idx2 is None:
                    # Esc / cancel from source picker -> back to event list
                    break

                emb = embeds[idx2]

                print(
                    f"\n{c.bold}Source:{c.reset} {c.sky}{emb.label}{c.reset}"
                    + (f"  {c.dim}[{emb.locale}]{c.reset}" if emb.locale else "")
                )

                back_to_channel_list = False

                # ── playback action loop ─────────────────────────────────
                while True:
                    action = choose_playback_action(c)

                    # Esc from action menu returns to source list.
                    if action is None:
                        break

                    if action == "browser":
                        target_url = emb.iframe_url
                        if not is_valid_url(target_url):
                            target_url = emb.ppv_url(ppv_host, chosen.uri)

                        open_in_browser(target_url, c)

                    elif action == "details":
                        print_embed(emb, ppv_host, chosen.uri, c)
                        pause(c)

                    elif action == "back":
                        back_to_channel_list = True
                        break

                if back_to_channel_list:
                    break

    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="ppv.py",
        description="Browse ppv.st events from the terminal and print embed URLs.",
    )

    p.add_argument(
        "--api",
        default=os.environ.get("PPV_API_BASE", DEFAULT_API_BASE),
        help=f"API base URL (default: {DEFAULT_API_BASE})",
    )

    p.add_argument(
        "--raw",
        action="store_true",
        help="Disable ANSI colors (also auto-enabled when stdout is not a TTY)",
    )

    p.add_argument(
        "--show-default",
        action="store_true",
        help="Also print the default embed before showing the source picker",
    )

    p.add_argument(
        "--list-only",
        action="store_true",
        help="(reserved) skip the embed picker — only future use",
    )

    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])

    try:
        return run(args.api, args.show_default, enable_colors(args.raw))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
