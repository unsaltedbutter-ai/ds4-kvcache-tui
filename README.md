# ds4-kvcache-tui

A terminal UI for the [ds4](https://github.com/antirez/ds4) server's disk KV
cache. It lists the cached checkpoints, shows how often each one has actually
been reused, lets you read the prompt that produced it, copy or export that
prompt, artificially raise a file's hit count to protect it from eviction, or
delete it.

ds4 stores each cached prefix as `<sha1>.kv`, where the name is the hash of the
rendered prompt text. The filename tells you nothing, and `ls` only tells you
size and mtime. This tool reads the 48-byte header inside each file, so you can
see what is in your cache and why the server is keeping it.

## Setup

Requires Python 3.9+ and a terminal. Run it on the machine that holds the
cache.

```sh
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

## Running

```sh
./venv/bin/python kvcache_tui.py --dir /Volumes/4TB-1/ds4-kv-cache
```

It needs a real terminal (it will not work through a pipe). Over SSH is fine.

It opens on the whole cache. In practice most files are written once and never
read again, so that first screen is mostly never-hit entries; `f` narrows to
the reused ones.

### Options

| Flag | Default | Meaning |
| --- | --- | --- |
| `--dir` | `/Volumes/4TB-1/ds4-kv-cache` | Cache directory to browse. |
| `--min-hits` | `1` | Threshold for the `hits>=N` step of the `f` filter cycle. |
| `--cold-max` | `100000` | Tokens above which a checkpoint is consumed on load. Match your server's `cold_max` (see below). |
| `--out-dir` | `.` | Default destination for the `w` prompt export. |
| `--copy-cmd` | none | Shell command to also pipe copied text into, e.g. `pbcopy`. See [Copying](#copying). |
| `--read-only` | off | Disable bump and delete. Browsing, inspecting, copying and exporting still work. |

`--cold-max` should match the running server. Get it from the `KV disk cache`
banner the server prints at startup:

```sh
grep -m1 "KV disk cache" ~/logs/jumbo-server-stderr.log
```

## Keys

| Key | Action |
| --- | --- |
| `up` / `down` | Move the selection. The prompt of the selected file loads in the bottom pane. |
| `s` | Cycle sort: hits, size, tokens, age. |
| `R` | Reverse the sort order. |
| `f` | Cycle filter: all, `hits>=N`, `hits>=2`, never hit. |
| `c` | Copy the selected file's whole prompt to the clipboard. |
| `w` | Write the selected file's full prompt text out to a file. |
| `b` | Bump the hit count (a soft "protect", see below). |
| `d` | Delete the file, with a confirmation. |
| `r` | Rescan the cache directory. |
| `tab` | Move focus between the list and the prompt pane (focus the pane to scroll it). |
| `q` | Quit. |

## Copying

Drag over the prompt pane, or hold shift and move the cursor in it, and the
highlighted text goes to the clipboard on its own once the selection settles.
`c` copies the selected file's whole prompt without highlighting anything.

The copy travels as an OSC 52 escape sequence, which means it lands on the
clipboard of the terminal you are sitting at, not the machine running the
tool. That is what you want over SSH, but the terminal has to cooperate:

* **iTerm2, WezTerm, Kitty, Ghostty, Alacritty**: works. iTerm2 gates it behind
  Settings, General, Selection, "Applications in terminal may access clipboard".
* **tmux**: needs `set -g set-clipboard on` in `.tmux.conf`, otherwise tmux eats
  the sequence.
* **macOS Terminal.app**: does not implement OSC 52 at all. Nothing will arrive.
  Use a different terminal, or `w` to export and copy from the file.

`--copy-cmd` pipes the same text into a shell command *on the machine running
the tool*, so `--copy-cmd pbcopy` only helps if that is also the machine you are
sitting at. It runs in addition to the escape sequence, not instead of it.

Copies are capped at 64,000 characters. Past that the escape sequence gets long
enough that terminals start truncating it silently or dropping it, so the tool
refuses and tells you to use `w` instead. Roughly a third of a full-length
prompt fits; anything bigger is an export, not a copy.

Textual holds the mouse while the app runs, so your terminal's own click-drag
selection does not reach the app. Hold `option` (iTerm2, Terminal.app) or
`shift` (most Linux terminals) while dragging if you want the terminal's native
selection instead, for example to grab something out of the table.

## Reading the columns

* **hits** is how many times the server has *reused* this checkpoint since it
  was stored. It is not a request counter. Most files sit at 0 forever.
* **tokens** is the length of the cached prefix. A trailing `*` means the file
  is larger than `cold_max`, which matters (see gotchas).
* **reason** is why ds4 wrote the file:
  * `cold` a session entry prefix, the start of a conversation. These are the
    ones that get reused across independent sessions.
  * `continued` a mid-conversation waypoint.
  * `evict` the live slot was flushed to disk because another conversation took
    the GPU cache.
  * `shutdown` written when the server stopped.
* **key** is what the cache key covers: `token-text`, or `responses-visible` /
  `thinking-visible` when the server is keying on rendered output.
* **age** is time since `last_used`. This is the number the eviction score
  decays against, so it is the one that decides what dies first.

## Protecting a file (`b`)

When the cache exceeds its budget, ds4 evicts the lowest-scoring files. The
score is roughly:

```
score = (decayed_hit_count + 1) * tokens / file_size
```

The hit count decays with a half-life on `last_used`, so a prefix you reuse
monthly can score like one that was never reused at all, and get evicted while
a large one-shot conversation survives.

`b` counteracts that by hand: it sets the hit count to a value you choose and
refreshes `last_used` to now, which raises the score and buys the file another
decay window. It is a blunt instrument, but it is the only lever the on-disk
format gives you.

Protection only matters when the cache is actually over budget. If your
`--kv-disk-space-mb` is generous enough that eviction never runs, nothing is at
risk and bumping changes nothing.

## Gotchas

**Files over `cold_max` are consumed on load.** ds4 unlinks them when it loads
them, so their hit count is always 0 no matter how many times they were used,
and bumping the count will not survive the next load. These are flagged with a
`*` on the token count and a warning if you try to bump one. Do not read their
`hits=0` as "never used".

**The prompt text is your real data.** It is the full rendered prompt, system
message and all. It is only read when you select a row, and it stays on this
machine unless you export or copy it. Point `w` somewhere local, not at a synced
or shared directory. Copying is the one thing that moves it off the box by
design: highlighting text in the prompt pane puts it on the clipboard of
whatever machine your terminal is running on, which over SSH is not this one.

Because `w` defaults to writing `<sha>.txt` into the working directory, and
this repo has a remote, `.gitignore` denies everything by default and
allowlists only the four files the tool is made of. An exported prompt cannot
be committed by accident, not even by `git add -A`. The cost is that a genuinely
new source file will not show up in `git status` until you add an exception for
it in `.gitignore`.

## Safety

The tool only ever writes two fields, the hit count at offset 12 and
`last_used` at offset 32, in place. It never rewrites the KV payload or the
header's size fields, so a file stays valid and the server can be running while
you use it. Deleting a file is a plain `unlink`, which is what the server's own
eviction does. Exports are written to a `.part` file and renamed, so an
interrupted export cannot leave a truncated file that looks complete.
