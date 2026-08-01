#!/usr/bin/env python3
"""kvcache_tui.py - browse and manage the ds4-server disk KV cache.

Run this ON the machine that holds the cache (e.g. notible), in a real
terminal. It reads each <sha>.kv file's 48-byte header (hit count, tokens,
size, reason, timestamps) and lets you inspect the stored prompt text, copy it
to the clipboard, write it out to a file, bump a file's hit count (a soft
"protect"), or delete it.

Privacy: the prompt text is only read when you highlight a row, and it never
leaves this machine - keep the window on the box that owns the data. An export
(w) writes that prompt to wherever you point it, so keep the destination local.
Copying is the one exception: selecting text in the detail pane puts it on the
clipboard of the terminal you are sitting at, which over SSH is a different
machine than the one holding the cache.

Layout (matches the file format in ds4_kvstore.c):
    offset  0   'K''V''C', version(1), quant, reason, ext_flags, model_id
    offset  8   tokens u32, hits u32, ctx_size u32, [20]=payload_abi
    offset 24   created_at u64, last_used u64, payload_bytes u64
    offset 48   text_bytes u32   -> then text_bytes of rendered prompt, then KV payload

Requires: pip install textual
"""
from __future__ import annotations

import argparse
import os
import struct
import subprocess
import time
from dataclasses import dataclass
from typing import Optional

try:
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal, Vertical
    from textual.screen import ModalScreen
    from textual.widgets import (Button, DataTable, Footer, Header, Input,
                                 Label, Static, TextArea)
except ImportError:
    raise SystemExit(
        "This tool needs Textual.\n"
        "    pip install textual\n"
        "(or: python3 -m pip install --user textual)"
    )

FIXED_HEADER = 48
# Checkpoints larger than cold_max are unlink()'d when loaded (consumed-on-load),
# so their header hit count never climbs above 0. Flag them so the numbers are
# not misread. This deployment runs cold_max=100000 (see the "KV disk cache"
# startup banner in the server log); override with --cold-max if yours differs.
COLD_MAX_TOKENS = 100000
# Preview shows head + tail of the rendered prompt: the head is mostly shared
# system/tools scaffolding, while the tail carries the distinguishing request.
PREVIEW_HEAD_BYTES = 160
PREVIEW_TAIL_BYTES = 160
PREVIEW_HEAD_CHARS = 48
PREVIEW_TAIL_CHARS = 48
INSPECT_LIMIT = 200_000
# A clipboard copy travels as one OSC 52 escape sequence, and terminals cap how
# long that may be (tmux drops the whole sequence past its buffer limit, some
# emulators truncate silently). Refuse past this rather than hand over a prompt
# that is quietly cut short - (w) exports any size.
COPY_LIMIT = 64_000
# TextArea.SelectionChanged fires for every cell a drag crosses; wait for the
# gesture to settle so one selection means one copy, not a hundred.
COPY_DEBOUNCE = 0.25

REASONS = {0: "unknown", 1: "cold", 2: "continued", 3: "evict",
           4: "shutdown", 5: "agent-system", 6: "agent-session"}


def key_kind(ext: int) -> str:
    if ext & (1 << 1):
        return "responses-visible"
    if ext & (1 << 2):
        return "thinking-visible"
    return "token-text"


@dataclass
class Entry:
    path: str
    sha: str
    quant: int
    reason: int
    ext: int
    model: int
    tokens: int
    hits: int
    ctx: int
    created: int
    last_used: int
    payload_bytes: int
    text_bytes: int
    size: int
    preview: str = ""

    @property
    def consumed_on_load(self) -> bool:
        return self.tokens > COLD_MAX_TOKENS


def read_entry(path: str) -> Entry | None:
    """Parse the header plus a head+tail preview from one <sha>.kv file.

    The rendered prompt text lives at [FIXED_HEADER+4, +text_bytes). We read a
    head chunk (with the header) and, for long prompts, seek to read a tail
    chunk from the end of the text region - never into the KV payload.
    """
    text_start = FIXED_HEADER + 4
    try:
        with open(path, "rb") as f:
            head = f.read(text_start + PREVIEW_HEAD_BYTES)
            if len(head) < text_start or head[0:3] != b"KVC" or head[3] != 1:
                return None
            tokens = struct.unpack_from("<I", head, 8)[0]
            hits = struct.unpack_from("<I", head, 12)[0]
            ctx = struct.unpack_from("<I", head, 16)[0]
            created = struct.unpack_from("<Q", head, 24)[0]
            last_used = struct.unpack_from("<Q", head, 32)[0]
            payload = struct.unpack_from("<Q", head, 40)[0]
            text_bytes = struct.unpack_from("<I", head, 48)[0]
            head_raw = head[text_start:text_start + min(text_bytes, PREVIEW_HEAD_BYTES)]
            tail_raw = b""
            if text_bytes > PREVIEW_HEAD_BYTES + PREVIEW_TAIL_BYTES:
                f.seek(text_start + text_bytes - PREVIEW_TAIL_BYTES)
                tail_raw = f.read(PREVIEW_TAIL_BYTES)
        size = os.stat(path).st_size
    except OSError:
        return None
    head_txt = " ".join(head_raw.decode("utf-8", "replace").split())
    if tail_raw:
        tail_txt = " ".join(tail_raw.decode("utf-8", "replace").split())
        preview = f"{head_txt[:PREVIEW_HEAD_CHARS]} … {tail_txt[-PREVIEW_TAIL_CHARS:]}"
    else:
        preview = head_txt[:PREVIEW_HEAD_CHARS + PREVIEW_TAIL_CHARS]
    sha = os.path.basename(path)[:-3]
    return Entry(path, sha, head[4], head[5], head[6], head[7], tokens, hits, ctx,
                 created, last_used, payload, text_bytes, size, preview)


def read_prompt_text(e: Entry, limit: int = INSPECT_LIMIT) -> str:
    try:
        with open(e.path, "rb") as f:
            f.seek(FIXED_HEADER + 4)
            data = f.read(min(e.text_bytes, limit))
    except OSError as ex:
        return f"<could not read prompt: {ex}>"
    txt = data.decode("utf-8", "replace")
    if e.text_bytes > limit:
        txt += f"\n\n... [truncated {e.text_bytes - limit} more bytes]"
    return txt


def write_prompt_text(e: Entry, dest: str) -> int:
    """Copy the full rendered prompt out to dest. Returns bytes written.

    Streamed as raw bytes (no decode/re-encode round trip) so the export is
    byte-identical to what the server hashed, and untruncated - unlike the
    inspect pane, which stops at INSPECT_LIMIT. Written to a .part file and
    renamed, so an interrupted export never leaves a partial file behind.
    """
    parent = os.path.dirname(dest)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = dest + ".part"
    written = 0
    try:
        with open(e.path, "rb") as src, open(tmp, "wb") as out:
            src.seek(FIXED_HEADER + 4)
            while written < e.text_bytes:
                chunk = src.read(min(1 << 20, e.text_bytes - written))
                if not chunk:
                    break
                out.write(chunk)
                written += len(chunk)
        os.replace(tmp, dest)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return written


def patch_hits(path: str, new_hits: int, now: int) -> None:
    """Surgically rewrite only hits (offset 12) and last_used (offset 32).

    Everything else - created_at, payload_bytes, tokens - is left untouched so
    the file stays valid while the server is live.
    """
    with open(path, "r+b") as f:
        f.seek(12)
        f.write(struct.pack("<I", new_hits & 0xFFFFFFFF))
        f.seek(32)
        f.write(struct.pack("<Q", now))
        f.flush()
        os.fsync(f.fileno())


def human_size(n: int) -> str:
    return f"{n / 1048576:.1f}M" if n < 1073741824 else f"{n / 1073741824:.2f}G"


def human_age(sec: int) -> str:
    if sec < 0:
        sec = 0
    if sec < 3600:
        return f"{sec // 60}m"
    if sec < 86400:
        return f"{sec // 3600}h"
    return f"{sec // 86400}d"


class ConfirmScreen(ModalScreen[bool]):
    BINDINGS = [
        Binding("y", "yes", "Yes"),
        Binding("n", "no", "No"),
        Binding("escape", "no", "No"),
    ]

    def __init__(self, message: str) -> None:
        super().__init__()
        self.message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self.message, id="dialog-msg")
            with Horizontal(id="dialog-btns"):
                yield Button("Yes (y)", "error", id="yes")
                yield Button("No (n)", "primary", id="no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)


class InputScreen(ModalScreen[Optional[str]]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, prompt: str, default: str = "") -> None:
        super().__init__()
        self.prompt = prompt
        self.default = default

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self.prompt, id="dialog-msg")
            yield Input(value=self.default, id="dialog-input")

    def on_mount(self) -> None:
        inp = self.query_one("#dialog-input", Input)
        inp.focus()
        inp.cursor_position = len(inp.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)

    def action_cancel(self) -> None:
        self.dismiss(None)


class KVCacheApp(App):
    CSS = """
    #list { height: 3fr; }
    #detail { height: 2fr; border: round $primary; }
    #status { height: 1; background: $panel; color: $text-muted; padding: 0 1; }

    ConfirmScreen, InputScreen { align: center middle; }
    #dialog {
        width: 72; height: auto; padding: 1 2;
        background: $surface; border: thick $primary;
    }
    #dialog-msg { width: 100%; padding-bottom: 1; }
    #dialog-btns { height: auto; align-horizontal: center; }
    #dialog-btns Button { margin: 0 1; }
    """

    BINDINGS = [
        Binding("s", "sort", "Sort"),
        Binding("R", "reverse", "Reverse"),
        Binding("f", "filter", "Filter"),
        Binding("c", "copy_prompt", "Copy prompt"),
        Binding("w", "export", "Write prompt"),
        Binding("b", "bump", "Bump hits"),
        Binding("d", "delete", "Delete"),
        Binding("r", "refresh", "Rescan"),
        Binding("tab", "focus_next", "Focus pane"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, cache_dir: str, min_hits: int, read_only: bool,
                 out_dir: str, copy_cmd: str = "") -> None:
        super().__init__()
        self.cache_dir = cache_dir
        self.min_hits = min_hits
        self.read_only = read_only
        self.out_dir = out_dir
        self.copy_cmd = copy_cmd
        self.entries: list[Entry] = []
        self.shown: list[Entry] = []
        self.by_sha: dict[str, Entry] = {}
        self._copy_timer = None
        # "all" leads so a fresh window shows the whole cache: the never-reused
        # files are most of it, and hiding them by default understates what is
        # on disk. (f) cycles down to the narrower views.
        self.filters = [
            ("all", lambda e: True),
            (f"hits>={min_hits}", lambda e: e.hits >= min_hits),
            ("hits>=2", lambda e: e.hits >= 2),
            ("never hit", lambda e: e.hits == 0),
        ]
        self.filter_idx = 0
        self.sorts = [
            ("hits", lambda e: e.hits),
            ("size", lambda e: e.size),
            ("tokens", lambda e: e.tokens),
            ("age", lambda e: e.last_used),
        ]
        self.sort_idx = 0
        self.reverse = True

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        table = DataTable(id="list", cursor_type="row", zebra_stripes=True)
        yield table
        yield TextArea("", id="detail", read_only=True, soft_wrap=True)
        yield Static("", id="status")
        yield Footer()

    def on_mount(self) -> None:
        mode = "READ-ONLY" if self.read_only else "read-write"
        self.title = "ds4 KV cache"
        self.sub_title = f"{self.cache_dir}  ({mode})"
        table = self.query_one(DataTable)
        table.add_columns("sha", "hits", "size", "tokens", "reason", "key",
                          "age", "preview")
        self.scan()
        self.populate()
        table.focus()

    # ---- data ----------------------------------------------------------

    def scan(self) -> None:
        entries: list[Entry] = []
        try:
            names = os.listdir(self.cache_dir)
        except OSError as ex:
            self.notify(f"Cannot read {self.cache_dir}: {ex}", severity="error")
            names = []
        for name in names:
            if not name.endswith(".kv") or len(name) != 43:
                continue
            e = read_entry(os.path.join(self.cache_dir, name))
            if e:
                entries.append(e)
        self.entries = entries
        self.by_sha = {e.sha: e for e in entries}

    def populate(self, keep_sha: str | None = None) -> None:
        now = int(time.time())
        _, pred = self.filters[self.filter_idx]
        sort_label, keyfn = self.sorts[self.sort_idx]
        rows = [e for e in self.entries if pred(e)]
        rows.sort(key=keyfn, reverse=self.reverse)
        self.shown = rows

        table = self.query_one(DataTable)
        table.clear()
        for e in rows:
            table.add_row(
                e.sha[:12],
                str(e.hits),
                human_size(e.size),
                f"{e.tokens}{'*' if e.consumed_on_load else ''}",
                REASONS.get(e.reason, "?"),
                key_kind(e.ext),
                human_age(now - e.last_used),
                e.preview,
                key=e.sha,
            )

        shown_bytes = sum(e.size for e in rows)
        all_bytes = sum(e.size for e in self.entries)
        arrow = "v" if self.reverse else "^"
        mode = "READ-ONLY" if self.read_only else "read-write"
        self.query_one("#status", Static).update(
            f"{len(rows)} shown / {len(self.entries)} total   "
            f"shown {human_size(shown_bytes)} / all {human_size(all_bytes)}   "
            f"sort={sort_label}{arrow}  filter={self.filters[self.filter_idx][0]}   "
            f"[{mode}]   * = >{COLD_MAX_TOKENS // 1000}k tok (consumed-on-load)"
        )

        if keep_sha:
            for i, e in enumerate(rows):
                if e.sha == keep_sha:
                    table.move_cursor(row=i)
                    break
        self.show_detail()

    def current_entry(self) -> Entry | None:
        if not self.shown:
            return None
        i = self.query_one(DataTable).cursor_row
        if i is None or not (0 <= i < len(self.shown)):
            return None
        return self.shown[i]

    def show_detail(self) -> None:
        detail = self.query_one("#detail", TextArea)
        e = self.current_entry()
        if not e:
            detail.load_text("")
            return
        now = int(time.time())
        flag = f"  (>{COLD_MAX_TOKENS // 1000}k tokens: consumed-on-load, header hits reset each load)" \
            if e.consumed_on_load else ""
        meta = (
            f"{e.sha}\n"
            f"hits={e.hits}  tokens={e.tokens}{flag}\n"
            f"size={human_size(e.size)}  reason={REASONS.get(e.reason, '?')}  "
            f"key={key_kind(e.ext)}  ctx={e.ctx}  quant={e.quant}  "
            f"age={human_age(now - e.last_used)}  "
            f"reused-span={human_age(e.last_used - e.created)}\n"
            f"{'-' * 72}\n"
        )
        detail.load_text(meta + read_prompt_text(e))

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self.show_detail()

    # ---- clipboard -----------------------------------------------------

    def send_to_clipboard(self, text: str, what: str) -> None:
        """Put text on the clipboard of whatever terminal is displaying us.

        Textual writes an OSC 52 sequence, which the terminal emulator picks
        up - so over SSH this lands on the clipboard of the machine you are
        sitting at, not this one. macOS Terminal.app ignores OSC 52 entirely;
        --copy-cmd is the way out there (it pipes to a command on THIS host).
        """
        if not text:
            return
        if len(text) > COPY_LIMIT:
            self.notify(f"{what} is {len(text)} chars - past the {COPY_LIMIT} "
                        "clipboard limit. Use (w) to export it to a file.",
                        severity="warning", timeout=6)
            return
        self.copy_to_clipboard(text)
        if self.copy_cmd:
            try:
                subprocess.run(self.copy_cmd, shell=True, check=True,
                               input=text.encode("utf-8"))
            except (OSError, subprocess.SubprocessError) as ex:
                self.notify(f"--copy-cmd failed: {ex}", severity="error")
                return
        self.notify(f"Copied {what} ({len(text)} chars)", timeout=3)

    def on_text_area_selection_changed(
            self, event: TextArea.SelectionChanged) -> None:
        """Copy a highlighted range as soon as the selection settles.

        Textual holds the mouse, so the terminal's own drag-to-select never
        sees this pane and there is otherwise no way to get text out of it.
        """
        if self._copy_timer is not None:
            self._copy_timer.stop()
            self._copy_timer = None
        if not event.text_area.selected_text:
            return
        self._copy_timer = self.set_timer(COPY_DEBOUNCE, self.copy_selection)

    def copy_selection(self) -> None:
        self._copy_timer = None
        self.send_to_clipboard(
            self.query_one("#detail", TextArea).selected_text, "selection")

    # ---- actions -------------------------------------------------------

    def action_sort(self) -> None:
        self.sort_idx = (self.sort_idx + 1) % len(self.sorts)
        keep = e.sha if (e := self.current_entry()) else None
        self.populate(keep_sha=keep)

    def action_reverse(self) -> None:
        self.reverse = not self.reverse
        keep = e.sha if (e := self.current_entry()) else None
        self.populate(keep_sha=keep)

    def action_filter(self) -> None:
        self.filter_idx = (self.filter_idx + 1) % len(self.filters)
        self.populate()

    def action_refresh(self) -> None:
        keep = e.sha if (e := self.current_entry()) else None
        self.scan()
        self.populate(keep_sha=keep)
        self.notify("Rescanned cache directory")

    def action_copy_prompt(self) -> None:
        """Copy the whole prompt of the selected entry, no highlighting needed."""
        e = self.current_entry()
        if not e:
            return
        self.send_to_clipboard(read_prompt_text(e), f"prompt of {e.sha[:12]}")

    def action_export(self) -> None:
        """Write the selected entry's full prompt text to a file.

        Allowed in read-only mode: this reads the cache, it does not change it.
        """
        e = self.current_entry()
        if not e:
            return

        def do_write(dest: str) -> None:
            try:
                n = write_prompt_text(e, dest)
            except OSError as ex:
                self.notify(f"Export failed: {ex}", severity="error")
                return
            self.notify(f"Wrote {n} bytes to {dest}", timeout=6)

        def after(value: str | None) -> None:
            if value is None:
                return
            dest = os.path.expanduser(value.strip())
            if not dest:
                return
            if os.path.isdir(dest):
                dest = os.path.join(dest, f"{e.sha[:12]}.txt")
            if os.path.exists(dest):
                self.push_screen(
                    ConfirmScreen(f"{dest}\nalready exists. Overwrite?"),
                    lambda ok: do_write(dest) if ok else None,
                )
                return
            do_write(dest)

        self.push_screen(
            InputScreen(f"Write prompt of {e.sha[:12]}... "
                        f"({e.text_bytes} bytes) to:",
                        os.path.join(self.out_dir, f"{e.sha[:12]}.txt")),
            after,
        )

    def action_bump(self) -> None:
        e = self.current_entry()
        if not e:
            return
        if self.read_only:
            self.notify("Read-only mode: bump disabled", severity="warning")
            return
        if e.consumed_on_load:
            self.notify(f"This file is >{COLD_MAX_TOKENS // 1000}k tokens: "
                        "consumed-on-load, so a bumped hit count will not survive "
                        "its next load.", severity="warning", timeout=6)

        def after(value: str | None) -> None:
            if value is None:
                return
            try:
                new_hits = int(value)
                if new_hits < 0:
                    raise ValueError
            except ValueError:
                self.notify("Enter a non-negative integer", severity="error")
                return
            now = int(time.time())
            try:
                patch_hits(e.path, new_hits, now)
            except OSError as ex:
                self.notify(f"Write failed: {ex}", severity="error")
                return
            e.hits = new_hits
            e.last_used = now
            self.notify(f"Set hits={new_hits}, refreshed last_used")
            self.populate(keep_sha=e.sha)

        self.push_screen(
            InputScreen(f"Protect {e.sha[:12]}...  set hit count (was {e.hits}):",
                        str(max(e.hits + 100, 100))),
            after,
        )

    def action_delete(self) -> None:
        e = self.current_entry()
        if not e:
            return
        if self.read_only:
            self.notify("Read-only mode: delete disabled", severity="warning")
            return

        def after(confirmed: bool | None) -> None:
            if not confirmed:
                return
            try:
                os.unlink(e.path)
            except OSError as ex:
                self.notify(f"Delete failed: {ex}", severity="error")
                return
            self.entries = [x for x in self.entries if x.sha != e.sha]
            self.by_sha.pop(e.sha, None)
            self.notify(f"Deleted {e.sha[:12]}... ({human_size(e.size)} freed)")
            self.populate()

        self.push_screen(
            ConfirmScreen(f"Delete {e.sha[:12]}...\n"
                          f"{human_size(e.size)}, hits={e.hits}, "
                          f"reason={REASONS.get(e.reason, '?')}?"),
            after,
        )


def main() -> None:
    global COLD_MAX_TOKENS
    ap = argparse.ArgumentParser(description="Browse/manage the ds4 disk KV cache.")
    ap.add_argument("--dir", default="/Volumes/4TB-1/ds4-kv-cache",
                    help="cache directory (default: %(default)s)")
    ap.add_argument("--min-hits", type=int, default=1,
                    help="threshold for the hits>=N step of the (f) filter cycle; "
                         "the window opens on 'all' (default: 1 = reused at "
                         "least once)")
    ap.add_argument("--cold-max", type=int, default=COLD_MAX_TOKENS,
                    help="tokens above which a checkpoint is consumed-on-load; match "
                         "your server's cold_max from the 'KV disk cache' startup "
                         "banner (default: %(default)s)")
    ap.add_argument("--out-dir", default=".",
                    help="default destination for the (w) prompt export; the "
                         "path is editable at export time (default: %(default)s)")
    ap.add_argument("--copy-cmd", default="",
                    help="shell command to also pipe copied text into, e.g. "
                         "'pbcopy'. Copying normally goes through an OSC 52 "
                         "escape, which macOS Terminal.app ignores; this runs "
                         "on the machine the TUI runs on, so it is only useful "
                         "when that is also where you are sitting")
    ap.add_argument("--read-only", action="store_true",
                    help="disable bump/delete (browse, inspect and export only)")
    args = ap.parse_args()
    if not os.path.isdir(args.dir):
        raise SystemExit(f"No such directory: {args.dir}")
    COLD_MAX_TOKENS = args.cold_max
    KVCacheApp(args.dir, args.min_hits, args.read_only,
               os.path.expanduser(args.out_dir), args.copy_cmd).run()


if __name__ == "__main__":
    main()
