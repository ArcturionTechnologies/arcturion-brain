"""Live memory watcher — the standing guarantee behind "memory saved → searchable
in seconds". Watches every constellation agent's Memory dirs (+ the Brain's own
memory/) and ingests each changed *.md into the live index within one debounce
window, regardless of which agent or filesystem wrote it.

WHY THIS IS A PYTHON DAEMON (not a bash script): under a loaded launchd agent,
`/bin/bash` holds Full Disk Access but a *forked* homebrew python3 does not — TCC
denies it Desktop access ("No module named 'engine'" when importing from Desktop,
and it can't read the memory files either). brain-http sidesteps this by having
launchd `exec` python as its DIRECT child; we do the same. So this module is meant
to be launched via `exec /opt/homebrew/bin/python3 -m engine.watch`
(com.arcturion.brain-memory-watch), making python the FDA-holding direct child.
It then ingests IN-PROCESS — no forked python to be denied — and as a bonus the
embed model stays warm across bursts.

fswatch (a separate binary) still works when subprocessed here; only python needed
the direct-exec treatment. Debounce is delegated to fswatch via --latency +
--batch-marker, so a burst coalesces into one in-process ingest.

Disable: launchctl unload ~/Library/LaunchAgents/com.arcturion.brain-memory-watch.plist
"""

from __future__ import annotations
import os
import subprocess
import time
from pathlib import Path
from . import config as C
from .ingest import ingest_paths
from . import vega_index

DEBOUNCE = os.environ.get("BRAIN_WATCH_DEBOUNCE", "20")
MARKER = "__FSWATCH_EOB__"
MAX_BATCH = 100
FSWATCH = "/opt/homebrew/bin/fswatch"
if not os.path.exists(FSWATCH):
    from shutil import which
    FSWATCH = which("fswatch") or FSWATCH
LOG = C.DESKTOP_ROOT / "ARCTURION_HQ" / "11 📜 LOGS" / "brain" / "memory-watch.log"


def log(msg: str) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a") as f:
            f.write(f"[{time.strftime('%Y-%m-%dT%H:%M:%S%z')}] {msg}\n")
    except Exception:
        pass


def watch_dirs(*, include_desk=True) -> list[str]:
    """Canonical agent Memory + Knowledge dirs straight from the Brain config — never drifts
    from what the nightly reindex walks (walker.walk_root walks the whole agent root, so both
    Memory and Knowledge are already covered nightly; this just gives them ~20s live latency).
    Watches the canonical "000 */Memory" + "000 */Knowledge" rooms, the lowercase "memory"
    (used by auto_memory_hook.py), plus the Brain's own memory/.

    Knowledge added 2026-06-26 (ArcturionKnowledge): an agent's Knowledge shelves are the
    database it pulls from to inform decisions (Cmd 037) — they must be brain-queryable live."""
    dirs: list[str] = []
    for agent in C.CONSTELLATION_AGENTS:
        root = C.DESKTOP_ROOT / agent
        if not root.exists():
            continue
        for room in sorted(root.glob("000 */Memory")):
            if room.is_dir():
                dirs.append(str(room))
        for kroom in sorted(root.glob("000 */Knowledge")):
            if kroom.is_dir():
                dirs.append(str(kroom))
        low = root / "memory"
        if low.is_dir():
            dirs.append(str(low))
    brain_mem = C.BRAIN_ROOT / "memory"
    if brain_mem.is_dir():
        dirs.append(str(brain_mem))
    if include_desk and vega_index.enabled():
        dirs.extend(desk_watch_dirs())
    return list(dict.fromkeys(dirs))


def desk_watch_dirs() -> list[str]:
    """VEGA workspace inventory plus approved ATLAS shelves and pinned sources."""
    result = []
    vega_root = C.DESKTOP_ROOT / "VEGA"
    if vega_root.is_dir():
        result.append(str(vega_root))
    for name in vega_index.DECLARATION_ROOTS:
        path = C.DESKTOP_ROOT / name
        if path.is_dir():
            result.append(str(path))
    try:
        import json
        pack = json.loads(vega_index.PACK_PATH.read_text(encoding="utf-8"))
        vega_index.validate_pack(pack)
        for rule in pack["rules"]:
            path = vega_index._absolute_source(C.DESKTOP_ROOT, rule["source"])
            if path.parent.is_dir():
                result.append(str(path.parent))
    except (OSError, ValueError):
        pass
    return list(dict.fromkeys(result))


def want(p: str) -> bool:
    """True for an ingestable distilled-memory markdown file."""
    if not p.endswith(".md"):
        return False
    if "/Sessions/raw/" in p:          # raw transcripts → nightly sessions harvester
        return False
    if "/.git/" in p or "/.obsidian/" in p or "/.trash/" in p:
        return False
    if p.endswith(".last_autosave") or p.endswith("/.DS_Store"):
        return False
    return True


def _under(path: Path, directory: Path) -> bool:
    try:
        return path.resolve().is_relative_to(directory.resolve())
    except (OSError, RuntimeError):
        return False


def watch_event_allowed(value: str, base_dirs: list[str]) -> bool:
    """Do not broaden ingest just because a pinned file's parent is watched."""
    path = Path(value)
    if vega_index.enabled():
        try:
            rel = path.absolute().relative_to(C.DESKTOP_ROOT.absolute()).as_posix()
            if rel == "VEGA" or rel.startswith("VEGA/"):
                return True  # Scanner records exclusions without opening bodies.
        except ValueError:
            pass
    if not want(value):
        return False
    if any(_under(path, Path(d)) for d in base_dirs):
        return True
    if not vega_index.enabled():
        return False
    try:
        relative = path.absolute().relative_to(C.DESKTOP_ROOT.absolute()).as_posix()
        vega_index._absolute_source(C.DESKTOP_ROOT, relative)
    except (OSError, ValueError):
        return False
    if any(_under(path, C.DESKTOP_ROOT / shelf) for shelf in vega_index.DECLARATION_ROOTS):
        return True
    try:
        import json
        pack = json.loads(vega_index.PACK_PATH.read_text(encoding="utf-8"))
        vega_index.validate_pack(pack)
        return any(path.resolve() == vega_index._absolute_source(C.DESKTOP_ROOT, r["source"]) for r in pack["rules"])
    except (OSError, ValueError):
        return False


def watch_command(dirs: list[str]) -> list[str]:
    return [FSWATCH, "-0", f"--latency={DEBOUNCE}", f"--batch-marker={MARKER}",
            "--event=Created", "--event=Updated", "--event=Renamed", "--event=Removed", "-r", *dirs]


IDLE = object()   # yielded when every queued byte has been consumed


def _nul_tokens(stream):
    """Yield NUL-delimited tokens from fswatch -0 output. Reads with os.read on the
    raw fd so a small batch surfaces immediately — BufferedReader.read(n) would block
    until n bytes accumulate, and fswatch emits only a few bytes per burst."""
    import select
    fd = stream.fileno()
    buf = b""
    while True:
        # Tell the caller when the pipe is drained, so batches that queued up during a slow
        # flush coalesce into ONE flush instead of replaying one ~80 s flush per batch.
        if not select.select([fd], [], [], 0)[0]:
            yield IDLE
        chunk = os.read(fd, 4096)
        if not chunk:
            if buf:
                yield buf.decode("utf-8", "replace")
            return
        buf += chunk
        while b"\0" in buf:
            tok, buf = buf.split(b"\0", 1)
            yield tok.decode("utf-8", "replace")


def flush(batch: set) -> None:
    if not batch:
        return
    paths = sorted(batch)
    batch.clear()
    log(f"flushing {len(paths)} path(s)")
    normal, inventory_only = [], []
    base_dirs = watch_dirs(include_desk=False)
    for value in paths:
        path = Path(value)
        # Preserve the old Memory/Knowledge watcher contract exactly. The desk
        # projection independently classifies those events without opening
        # excluded source bodies.
        if want(value) and any(_under(path, Path(d)) for d in base_dirs):
            normal.append(value)
            continue
        try:
            rel = path.absolute().relative_to(C.DESKTOP_ROOT.absolute()).as_posix()
        except ValueError:
            rel = ""
        if vega_index.enabled() and (rel == "VEGA" or rel.startswith("VEGA/")):
            # Never give the ordinary walker a protected file, directory,
            # symlink, or non-Markdown event from the newly watched workspace.
            try:
                vega_index._absolute_source(C.DESKTOP_ROOT, rel)
                safe_path = True
            except ValueError:
                safe_path = False
            if safe_path and want(value) and not path.is_symlink() and not path.is_dir() and vega_index._vega_body_allowed(rel):
                normal.append(value)
            else:
                inventory_only.append(value)
        elif want(value):
            normal.append(value)
    log(f"classified: ingest={len(normal)} desk_inventory={len(inventory_only)}"
        + (f" first={Path(normal[0]).name}" if normal else ""))
    try:
        # In-process: this daemon holds FDA (direct launchd child), so reading the
        # memory files and writing brain.sqlite both succeed. busy_timeout (db.connect)
        # guards against write contention with the nightly reindex.
        if normal:
            stats = ingest_paths(normal, embed=True, verbose=False)
            log(f"ingested: {stats}")
    except Exception as e:  # noqa: BLE001 — never let one batch kill the daemon
        log(f"ingest error: {type(e).__name__}: {e}")
    # A changed decision / project note / handoff re-derives its verified journal in the same
    # batch, so the journal never lags its source. Failure costs only the journal refresh.
    try:
        from . import journals
        if normal and journals.touches_sources(normal):
            log(f"journals: {journals.refresh()}")
    except Exception as e:  # noqa: BLE001
        log(f"journal refresh error: {type(e).__name__}: {e}")
    if inventory_only and vega_index.enabled():
        try:
            from . import db
            conn = db.connect()
            try:
                started = time.monotonic()
                result = vega_index.auto_sync(conn, paths=inventory_only)
                conn.commit()
                corpus = (result or {}).get("corpus") or {}
                log(f"desk inventory: {time.monotonic() - started:.1f}s"
                    + (" (unchanged)" if corpus.get("unchanged") else ""))
            finally:
                conn.close()
        except Exception as e:  # noqa: BLE001
            log(f"desk inventory error: {type(e).__name__}: {e}")


def main() -> None:
    dirs = watch_dirs()
    base_dirs = watch_dirs(include_desk=False)
    if not dirs:
        log("no Memory dirs resolved; exiting")
        return
    log(f"watching {len(dirs)} dirs (debounce={DEBOUNCE}s, in-process ingest)")
    cmd = watch_command(dirs)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    batch: set = set()
    marker_seen = False
    try:
        for tok in _nul_tokens(proc.stdout):
            if tok is IDLE:
                if marker_seen:
                    flush(batch)
                    marker_seen = False
            elif tok == MARKER:
                marker_seen = True   # flush once the backlog behind this marker is read
            elif watch_event_allowed(tok, base_dirs):
                batch.add(tok)
                if len(batch) >= MAX_BATCH:
                    flush(batch)
    finally:
        flush(batch)
        try:
            if proc.stdout:
                proc.stdout.close()   # don't leak the pipe fd across daemon restarts
        except Exception:
            pass
        try:
            proc.terminate()
        except Exception:
            pass
    log("fswatch pipe closed; watcher stopping (launchd will relaunch)")


if __name__ == "__main__":
    main()
