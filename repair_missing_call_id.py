"""Detect and repair automation-trigger items that lack call_id, in any thread.

Dry run:      python repair_poison_any.py
Apply:        python repair_poison_any.py --apply

What it does:
  1. scans every rollout JSONL under sessions/ and archived_sessions/
  2. drops `function_call_output` items that have no `call_id`
  3. deletes the matching rows in thread_history_1.sqlite (thread_items)
  4. realigns thread_history_projection_state with the new file size
  5. writes a backup of everything it touches (unless nothing to fix)
Stop any running automation first, and restart Codex afterwards.
"""

import datetime
import json
import os
import shutil
import sqlite3
import sys
import time

HOME = os.path.expandvars(r"%USERPROFILE%\.codex")
WORK = os.path.dirname(os.path.abspath(__file__))
HISTORY_DB = os.path.join(HOME, "thread_history_1.sqlite")
APPLY = "--apply" in sys.argv


def is_poison(line: bytes) -> bool:
    text = line.decode("utf-8", "replace")
    if '"function_call_output"' not in text or '"call_id"' in text:
        return False
    try:
        record = json.loads(text)
    except Exception:
        return False
    payload = record.get("payload") or {}
    return (
        record.get("type") == "response_item"
        and payload.get("type") == "function_call_output"
        and not payload.get("call_id")
    )


def find_rollouts():
    for root in ("sessions", "archived_sessions"):
        base = os.path.join(HOME, root)
        for dirpath, _dirnames, filenames in os.walk(base):
            for filename in filenames:
                if filename.endswith(".jsonl"):
                    yield os.path.join(dirpath, filename)


def plan(path):
    with open(path, "rb") as handle:
        raw = handle.read()
    lines = raw.splitlines(keepends=True)
    drop = [i for i, line in enumerate(lines) if is_poison(line)]
    if not drop:
        return None
    last_ordinal = 0
    for line in lines:
        try:
            ordinal = json.loads(line).get("ordinal")
        except Exception:
            continue
        if ordinal is not None:
            last_ordinal = ordinal
    removed = sum(len(lines[i]) for i in drop)
    return {
        "path": path,
        "lines": len(lines),
        "drop": drop,
        "removed_bytes": removed,
        "size": len(raw),
        "new_size": len(raw) - removed,
        "new_lines": len(lines) - len(drop),
        "last_ordinal": last_ordinal,
    }


def write_back(path, payload):
    tmp = path + ".repair-tmp"
    with open(tmp, "wb") as handle:
        handle.write(payload)
    for _ in range(10):
        try:
            os.replace(tmp, path)
            return "replace"
        except PermissionError:
            time.sleep(0.4)
    with open(path, "r+b") as handle:
        handle.seek(0)
        handle.write(payload)
        handle.truncate()
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.remove(tmp)
    except OSError:
        pass
    return "in-place"


def main():
    plans = [p for p in (plan(path) for path in find_rollouts()) if p]

    con = sqlite3.connect(f"file:{HISTORY_DB}?mode=ro", uri=True)
    db_rows = con.execute(
        "SELECT rowid, thread_id, item_id FROM thread_items"
        " WHERE item_type='functionCallOutput' AND item_json NOT LIKE '%call_id%'"
    ).fetchall()
    projections = con.execute(
        "SELECT thread_id, next_rollout_byte_offset, next_rollout_ordinal"
        " FROM thread_history_projection_state"
    ).fetchall()
    con.close()

    if not plans and not db_rows:
        print("clean: no item is missing call_id. nothing to do.")
        return

    for item in plans:
        print(
            f"rollout {os.path.basename(item['path'])}: drop lines {item['drop']},"
            f" -{item['removed_bytes']} bytes -> {item['new_lines']} lines"
        )
    print("db rows to delete:", db_rows)

    if not APPLY:
        print("\nDRY RUN - nothing changed. re-run with --apply to repair.")
        return

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = os.path.join(WORK, f"repair-backup-{stamp}")
    os.makedirs(backup_dir, exist_ok=True)
    for item in plans:
        shutil.copy2(item["path"], os.path.join(backup_dir, os.path.basename(item["path"])))
    con = sqlite3.connect(HISTORY_DB)
    con.execute("VACUUM INTO ?", (os.path.join(backup_dir, "thread_history_1.sqlite"),))
    con.close()
    print("backup:", backup_dir)

    for item in plans:
        with open(item["path"], "rb") as handle:
            lines = handle.read().splitlines(keepends=True)
        payload = b"".join(line for i, line in enumerate(lines) if i not in item["drop"])
        mode = write_back(item["path"], payload)
        print(f"  {mode}: {os.path.basename(item['path'])} -> {item['new_lines']} lines")

    con = sqlite3.connect(HISTORY_DB, timeout=15)
    con.execute("BEGIN IMMEDIATE")
    deleted = con.execute(
        "DELETE FROM thread_items WHERE item_type='functionCallOutput'"
        " AND item_json NOT LIKE '%call_id%'"
    ).rowcount
    fixed = 0
    for item in plans:
        for thread_id, offset, _ordinal in projections:
            if offset == item["size"]:
                con.execute(
                    "UPDATE thread_history_projection_state"
                    " SET next_rollout_byte_offset=?, next_rollout_ordinal=?"
                    " WHERE thread_id=?",
                    (item["new_size"], item["last_ordinal"] + 1, thread_id),
                )
                fixed += 1
    con.commit()
    con.close()
    print(f"deleted db rows: {deleted}; realigned projections: {fixed}")
    print("now restart Codex.")


main()
