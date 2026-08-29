#!/usr/bin/env python3
"""
Recreate repo files from the Outline document produced by publish_to_outline.
Parses each "## `relative/path`" section (fenced code or raw markdown) and
writes the file to its path under an output directory -- so the folder
structure is reproduced on a different machine.

Env (put OUTLINE_* in .env.local):
  OUTLINE_URL          API base. e.g. https://docs.jdlab.us/api
  OUTLINE_TOKEN        API token  [required]
  OUTLINE_DOCUMENT_ID  urlId (.../doc/jdlab-ZereHWBBc7 -> ZereHWBBc7) or UUID.
  OUTLINE_DOCUMENT_URL Alternatively the full doc URL.

Usage:
  python scripts/import_from_outline.py                 # writes to ./outline-export
  python scripts/import_from_outline.py --out ../restored
  python scripts/import_from_outline.py --dry-run       # list files without writing
  python scripts/import_from_outline.py --out . --prune # reproduce in place AND delete
                                        # STALE files (ghosts) under reproduced
                                        # folders. Add --dry-run to preview the
                                        # deletions. Only removes files the
                                        # publisher would track -- never binaries,
                                        # .env*, lockfiles, or files > 256 KB.
"""
from __future__ import annotations

import json
import os
import re
import sys

import requests

# Windows consoles (cp1252) can't encode the status glyphs we print; force UTF-8.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def load_env_local() -> None:
    """Populate os.environ from .env.local / .env without overriding real env."""
    for name in (".env.local", ".env"):
        path = os.path.join(os.getcwd(), name)
        if not os.path.exists(path):
            continue
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


load_env_local()

API = re.sub(r"/+$", "", os.environ.get("OUTLINE_URL") or "https://app.getoutline.com/api")
TOKEN = os.environ.get("OUTLINE_TOKEN")


def arg(name: str, fallback: str = "") -> str:
    flag = f"--{name}"
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return fallback


DRY = "--dry-run" in sys.argv
PRUNE = "--prune" in sys.argv
OUT = arg("out", "outline-export")

# Prune scoping (mirrors publish_to_outline's ignore rules) so --prune only ever
# deletes STALE SOURCE files the publisher would have tracked -- never binaries,
# secrets, lockfiles, or oversized files it intentionally skips.
PRUNE_SKIP_DIRS = {
    "node_modules", ".git", ".next", "dist", "build", "out", "coverage",
    ".turbo", ".vercel",
}
PRUNE_SKIP_FILES = {
    "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "next-env.d.ts",
}
PRUNE_SKIP_EXT = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".woff", ".woff2",
    ".ttf", ".otf", ".pdf", ".zip", ".gz", ".mp4", ".mov", ".lock",
    ".sqlite", ".sqlite3", ".db",
}
PRUNE_MAX_BYTES = 256 * 1024


def resolve_doc_id() -> str:
    raw = os.environ.get("OUTLINE_DOCUMENT_URL") or os.environ.get("OUTLINE_DOCUMENT_ID")
    if not raw:
        print("ERROR: set OUTLINE_DOCUMENT_ID or OUTLINE_DOCUMENT_URL.", file=sys.stderr)
        sys.exit(1)
    if "/doc/" in raw:
        slug = re.split(r"[?#]", raw.split("/doc/")[1])[0]
        return slug.split("-")[-1]
    return raw


def api(method: str, body: dict) -> dict:
    res = requests.post(
        f"{API}/{method}",
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
        data=json.dumps(body),
    )
    if not res.ok:
        detail = ""
        try:
            detail = res.text
        except Exception:
            detail = ""
        raise RuntimeError(f"{method} -> {res.status_code} {res.reason} {detail}")
    return res.json()


def extract_content(body: str) -> str:
    """Pull file contents out of a section body: unwrap a fenced code block (of
    any backtick length, open-to-close), else treat the body as raw markdown."""
    b = body.replace("\r", "")
    b = re.sub(r"\n---\s*$", "", b)
    b = b.strip()
    fence = re.match(r"^(`{3,})[^\n]*\n(.*?)\n\1$", b, re.S)
    if fence:
        return fence.group(2)
    return b


def safe_join(out_dir: str, rel: str) -> str | None:
    """Guard against path traversal from a malicious/edited doc: resolve both to
    absolute paths and ensure the target stays inside out_dir."""
    base_abs = os.path.abspath(out_dir)
    target_abs = os.path.abspath(os.path.join(base_abs, rel))
    try:
        r = os.path.relpath(target_abs, base_abs)
    except ValueError:  # different drive on Windows
        return None
    if r and r != "." and not r.startswith("..") and not os.path.isabs(r):
        return target_abs
    return None


def write_sections(text: str, written: set[str]) -> int:
    """Parse "## `path`" sections from a document body and write each file."""
    idx = text.find("\n# Files")
    region = text[idx:] if idx >= 0 else text

    # Locate every file header, then take content as everything up to the NEXT
    # header (or end) -- robust to `#`/`$` inside file bodies.
    heads = []
    for m in re.finditer(r"^## `([^`]+)`[ \t]*\r?\n", region, re.M):
        heads.append({"path": m.group(1).strip(), "header_start": m.start(), "content_start": m.end()})

    count = 0
    for i, head in enumerate(heads):
        rel = head["path"]
        end = heads[i + 1]["header_start"] if i + 1 < len(heads) else len(region)
        content = extract_content(region[head["content_start"]:end])
        target = safe_join(OUT, rel)
        if not target:
            print(f"skip (unsafe path): {rel}", file=sys.stderr)
            continue
        count += 1
        written.add(rel)
        if DRY:
            print(f"would write: {os.path.join(OUT, rel)}  ({len(content)} bytes)")
            continue
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(content if content.endswith("\n") else content + "\n")
        print(f"wrote: {os.path.join(OUT, rel)}")
    return count


def is_publisher_ignored(name: str, size_bytes: int) -> bool:
    """True when a file would be IGNORED by the publisher (never a prune candidate)."""
    if name in PRUNE_SKIP_FILES:
        return True
    if os.path.splitext(name)[1].lower() in PRUNE_SKIP_EXT:
        return True
    if name == ".env" or name.startswith(".env."):
        return True
    if size_bytes > PRUNE_MAX_BYTES:
        return True
    return False


def collect_prunable(base_abs: str, dir_abs: str, acc: list[str]) -> None:
    """Recursively collect publishable file paths (relative, forward-slash) under
    dir_abs -- skipping ignored dirs and files the publisher wouldn't track."""
    try:
        entries = os.listdir(dir_abs)
    except Exception:
        return
    for name in entries:
        if name in PRUNE_SKIP_DIRS or name.endswith(".egg-info"):
            continue
        abs_p = os.path.join(dir_abs, name)
        if os.path.isdir(abs_p):
            collect_prunable(base_abs, abs_p, acc)
        else:
            try:
                size = os.path.getsize(abs_p)
            except Exception:
                size = 0
            if not is_publisher_ignored(name, size):
                acc.append(os.path.relpath(abs_p, base_abs).replace(os.sep, "/"))


def remove_empty_dirs(base_abs: str, dir_abs: str) -> None:
    """Recursively remove directories that are (or become) empty under dir_abs."""
    try:
        entries = os.listdir(dir_abs)
    except Exception:
        return
    for name in entries:
        abs_p = os.path.join(dir_abs, name)
        if os.path.isdir(abs_p):
            remove_empty_dirs(base_abs, abs_p)
    try:
        if len(os.listdir(dir_abs)) == 0:
            os.rmdir(dir_abs)
            print(f"pruned empty dir: {os.path.relpath(dir_abs, base_abs).replace(os.sep, '/')}")
    except Exception:
        pass


def prune_stale(written: set[str]) -> None:
    """Delete stale files under the top-level FOLDERS reproduced this run. Scoped
    to those folders only (root-level files have no '/', so the output root is
    never scanned). Opt-in --prune."""
    base_abs = os.path.abspath(OUT)

    roots = set()
    for rel in written:
        i = rel.find("/")
        if i > 0:
            roots.add(rel[:i])
    if not roots:
        print("prune: no reproduced folders to scan")
        return

    removed = 0
    for root in roots:
        existing: list[str] = []
        collect_prunable(base_abs, os.path.join(base_abs, root), existing)
        for rel in existing:
            if rel in written:
                continue
            if DRY:
                print(f"would prune (stale): {rel}")
            else:
                os.remove(os.path.join(base_abs, rel))
                print(f"pruned (stale): {rel}")
            removed += 1

    if not DRY:
        for root in roots:
            remove_empty_dirs(base_abs, os.path.join(base_abs, root))

    if removed == 0:
        print("prune: nothing stale")
    else:
        print(f"prune: {'would remove' if DRY else 'removed'} {removed} stale file(s)")


def main() -> None:
    if not TOKEN:
        print("ERROR: OUTLINE_TOKEN is required.", file=sys.stderr)
        sys.exit(1)

    doc_id = resolve_doc_id()
    parent = api("documents.info", {"id": doc_id})

    # Files live in child documents (one per top-level folder); the parent holds
    # only the tree. Parse the parent too (backward-compatible with the old
    # single-document format), then every child.
    written: set[str] = set()
    total = write_sections(parent["data"].get("text") or "", written)

    children = api("documents.list", {"parentDocumentId": parent["data"]["id"], "limit": 100})
    for child in children["data"]:
        doc = api("documents.info", {"id": child["id"]})
        n = write_sections(doc["data"].get("text") or "", written)
        if n > 0:
            print(f"  \u00b7 {child['title']}: {n} file(s)")
        total += n

    verb = "would reproduce" if DRY else "reproduced"
    print(f"\n{verb} {total} file(s) from \"{parent['data']['title']}\" into {OUT}/")
    if total == 0:
        print("No \"## `path`\" sections found -- was the doc created by publish_to_outline?",
              file=sys.stderr)
        return

    if PRUNE:
        prune_stale(written)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001 - top-level CLI error reporting
        print(e if isinstance(e, str) else str(e), file=sys.stderr)
        sys.exit(1)
