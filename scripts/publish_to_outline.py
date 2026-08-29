#!/usr/bin/env python3
"""
Sync repo files into ONE Outline document via the API, preserving the folder
structure so the tree can be reproduced on another machine.

The document gets:
  1. a directory tree (```text fence),
  2. one section per file titled with its full forward-slash relative path,
     code fenced by language (.md inlined as-is).
Idempotent: re-running replaces the body so it mirrors the current repo.

A companion importer can parse the "## `path`" + fenced-block format to
recreate files at their paths.

Env (put OUTLINE_* in .env.local):
  OUTLINE_URL          API base. e.g. https://docs.jdlab.us/api
  OUTLINE_TOKEN        API token (Outline -> Settings -> API Tokens)  [required]
  OUTLINE_DOCUMENT_ID  urlId at the end of the doc URL (.../doc/jdlab-ZereHWBBc7
                       -> ZereHWBBc7) or the UUID.
  OUTLINE_DOCUMENT_URL Alternatively the full doc URL (parsed for the id).

Usage:
  python scripts/publish_to_outline.py                    # default roots (full sync)
  python scripts/publish_to_outline.py app lib README.md  # specific files/dirs
  python scripts/publish_to_outline.py --changed          # only what changed since baseline
  python scripts/publish_to_outline.py --since=<ref>      # override the baseline
  python scripts/publish_to_outline.py --dry-run          # print plan, change nothing

Passing explicit targets is a PARTIAL publish: only those top-level folders'
child documents are rewritten; every other child doc is left untouched, and the
parent index tree is MERGED (kept complete) rather than shrunk to just the
folders passed. Pass whole FOLDERS (e.g. `components`), not single files -- a
folder's child doc is always rewritten in full, so publishing one file would
drop its siblings from that page.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone

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
        path = os.path.join(ROOT, name)
        if not os.path.exists(path):
            continue
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


ROOT = os.getcwd()
load_env_local()

API = re.sub(r"/+$", "", os.environ.get("OUTLINE_URL") or "https://app.getoutline.com/api")
TOKEN = os.environ.get("OUTLINE_TOKEN")
MAX_BYTES = 256 * 1024  # skip files larger than this
MAX_CHUNK = 40_000      # bytes of text per update request (gateway rejects large bodies)

# Default roots walked when nothing is passed on the CLI (option-tracker repo).
DEFAULT_TARGETS = [
    "src", "scripts",
    "main.py", "pyproject.toml", "Dockerfile", "docker-compose.yml", "README.md",
]

IGNORE_DIRS = {
    "node_modules", ".git", ".next", "dist", "build", "out", "coverage",
    ".turbo", ".vercel", "public", ".agents", "__pycache__", ".venv", "inbox",
}
IGNORE_FILES = {
    "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "next-env.d.ts",
    # Local DB dump -- never publish (may contain real data), it's not source.
    "prostore_backup.sql",
}
IGNORE_EXT = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".woff", ".woff2",
    ".ttf", ".otf", ".pdf", ".zip", ".gz", ".mp4", ".mov", ".lock", ".pyc",
    ".sqlite", ".sqlite3", ".db",
}

LANG = {
    ".ts": "ts", ".tsx": "tsx", ".js": "js", ".jsx": "jsx", ".mjs": "js",
    ".py": "python",
    ".json": "json", ".css": "css", ".scss": "scss", ".html": "html",
    ".sql": "sql", ".yml": "yaml", ".yaml": "yaml", ".sh": "bash", ".toml": "toml",
    ".svg": "xml",
}

# Baseline = the last commit whose state is mirrored in Outline. Stored locally
# (gitignored, machine-local) and advanced only after a full or --changed run.
BASELINE_FILE = os.path.join(ROOT, ".outline", "last-published")


def is_secret(rel: str) -> bool:
    """Never leak secrets."""
    base = rel.split("/")[-1] if rel else ""
    return base == ".env" or base.startswith(".env.")


def to_rel(abs_path: str) -> str:
    return os.path.relpath(abs_path, ROOT).replace(os.sep, "/")


def walk(abs_path: str, out: list[str]) -> None:
    rel = to_rel(abs_path)
    base = rel.split("/")[-1] if rel else rel
    if os.path.isdir(abs_path):
        if base in IGNORE_DIRS or base.endswith(".egg-info"):
            return
        for entry in sorted(os.listdir(abs_path)):
            walk(os.path.join(abs_path, entry), out)
        return
    # file
    if base in IGNORE_FILES or os.path.splitext(base)[1].lower() in IGNORE_EXT:
        return
    if is_secret(rel):
        return
    size = os.path.getsize(abs_path)
    if size > MAX_BYTES:
        print(f"skip (too large {size / 1024:.0f}KB): {rel}", file=sys.stderr)
        return
    out.append(rel)


def resolve_doc_id() -> str:
    raw = os.environ.get("OUTLINE_DOCUMENT_URL") or os.environ.get("OUTLINE_DOCUMENT_ID")
    if not raw:
        print("ERROR: set OUTLINE_DOCUMENT_ID (urlId, e.g. ZereHWBBc7) or OUTLINE_DOCUMENT_URL.",
              file=sys.stderr)
        sys.exit(1)
    if "/doc/" in raw:
        slug = re.split(r"[?#]", raw.split("/doc/")[1])[0]
        return slug.split("-")[-1]
    return raw


def api(method: str, body: dict) -> dict:
    attempts = 5
    for i in range(1, attempts + 1):
        res = requests.post(
            f"{API}/{method}",
            headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
            data=json.dumps(body),
        )
        if res.ok:
            return res.json()
        detail = ""
        try:
            detail = res.text
        except Exception:
            detail = ""
        # Retry transient gateway/server errors (502/503/504/429) with backoff.
        if res.status_code in (502, 503, 504, 429) and i < attempts:
            wait = 1000 * 2 ** (i - 1)
            print(f"  \u21bb {method} {res.status_code}; retry {i}/{attempts - 1} in {wait}ms")
            time.sleep(wait / 1000)
            continue
        raise RuntimeError(f"{method} -> {res.status_code} {res.reason} {detail}")
    raise RuntimeError(f"{method} -> exhausted retries")


def render_tree(paths: list[str]) -> str:
    root: dict = {}
    for p in paths:
        node = root
        for part in p.split("/"):
            node = node.setdefault(part, {})
    lines = ["."]
    tee, elbow, pipe, space = "\u251c\u2500\u2500 ", "\u2514\u2500\u2500 ", "\u2502   ", "    "

    def draw(node: dict, prefix: str) -> None:
        keys = sorted(node.keys())
        for i, k in enumerate(keys):
            last = i == len(keys) - 1
            branch = elbow if last else tee
            lines.append(f"{prefix}{branch}{k}")
            draw(node[k], prefix + (space if last else pipe))

    draw(root, "")
    return "\n".join(lines)


def section(path: str, content: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    trimmed = re.sub(r"\s+$", "", content)
    if ext in (".md", ".markdown"):
        lang = "markdown"
    elif ext in LANG:
        lang = LANG[ext]
    elif "dockerfile" in path.lower():
        lang = "dockerfile"
    else:
        lang = ""
    # Fence every file (incl. Markdown) so Outline stores it verbatim. The fence
    # is one backtick longer than the longest backtick run inside the file, so
    # content that itself contains ``` can't close the fence early.
    max_ticks = 0
    for run in re.finditer(r"`+", trimmed):
        max_ticks = max(max_ticks, len(run.group(0)))
    fence = "`" * max(3, max_ticks + 1)
    return f"## `{path}`\n\n{fence}{lang}\n{trimmed}\n{fence}\n"


def build_chunks(sections: list[str]) -> list[str]:
    """Group file sections into <=MAX_CHUNK bodies, split at section boundaries."""
    chunks: list[str] = []
    cur = ""
    for s in sections:
        piece = ("\n---\n\n" if cur else "") + s
        if cur and len((cur + piece).encode("utf-8")) > MAX_CHUNK:
            chunks.append(cur)
            cur = s
        else:
            cur += piece
    if cur:
        chunks.append(cur)
    return chunks


def replace_doc_body(doc_id: str, header: str, sections: list[str]) -> int:
    """Replace a document body with header + sections, chunked (first replaces, rest append)."""
    chunks = build_chunks(sections)
    api("documents.update", {"id": doc_id, "text": header + (chunks[0] if chunks else ""), "append": False})
    for i in range(1, len(chunks)):
        time.sleep(0.4)  # let the gateway/server settle between appends
        api("documents.update", {"id": doc_id, "text": f"\n---\n\n{chunks[i]}", "append": True})
    return len(chunks)


def find_or_create_child(parent_id: str, collection_id: str, title: str) -> str:
    """Idempotently find (by title) or create a child document under the parent."""
    lst = api("documents.list", {"parentDocumentId": parent_id, "limit": 100})
    for d in lst["data"]:
        if d["title"] == title:
            return d["id"]
    created = api("documents.create", {
        "title": title, "collectionId": collection_id, "parentDocumentId": parent_id,
        "text": "", "publish": True,
    })
    return created["data"]["id"]


def top_of(rel: str) -> str:
    """Top-level segment of a path; root-level files grouped under 'root'."""
    i = rel.find("/")
    return "root" if i == -1 else rel[:i]


def existing_tree_files_excluding(parent_id: str, exclude_groups: set[str]) -> list[str]:
    """Recover file paths already published in Outline, from every child document
    EXCEPT the folders being (re)published now (used to MERGE the index tree)."""
    lst = api("documents.list", {"parentDocumentId": parent_id, "limit": 100})
    out: list[str] = []
    for child in lst["data"]:
        if child["title"] in exclude_groups:
            continue  # this folder is being refreshed now
        doc = api("documents.info", {"id": child["id"]})
        text = doc["data"].get("text") or ""
        for m in re.finditer(r"^## `([^`]+)`", text, re.M):
            out.append(m.group(1).strip())
    return out


# --- Incremental publish support -------------------------------------------
def is_dir(p: str) -> bool:
    try:
        return os.path.isdir(os.path.join(ROOT, p))
    except Exception:
        return False


# Folder targets each own their own child doc; the top-level files all share the
# ONE "root" child doc, so touching any of them must republish the WHOLE set.
TARGET_FOLDERS = [t for t in DEFAULT_TARGETS if is_dir(t)]
ROOT_FILES = [t for t in DEFAULT_TARGETS if not is_dir(t)]


def git(cmd: str) -> str:
    return subprocess.run(
        ["git", *cmd.split()], cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout.strip()


def git_lines(cmd: str) -> list[str]:
    try:
        return [s.strip() for s in git(cmd).split("\n") if s.strip()]
    except Exception:
        return []


def is_valid_ref(ref: str) -> bool:
    try:
        git(f"rev-parse --verify --quiet {ref}")
        return True
    except Exception:
        return False


def write_baseline() -> None:
    try:
        os.makedirs(os.path.dirname(BASELINE_FILE), exist_ok=True)
        with open(BASELINE_FILE, "w", encoding="utf-8") as fh:
            fh.write(git("rev-parse HEAD") + "\n")
    except Exception as e:
        print(f"warn: could not write publish baseline: {e}", file=sys.stderr)


def targets_for_changes(baseline: str) -> tuple[list[str], list[str]]:
    """Files changed since `baseline` -> the CLI targets to republish."""
    committed = git_lines(f"diff --name-only {baseline} -- .")
    untracked = git_lines("ls-files --others --exclude-standard")
    changed = list(dict.fromkeys(committed + untracked))

    folder_set = set(TARGET_FOLDERS)
    root_file_set = set(ROOT_FILES)
    groups: set[str] = set()
    root_touched = False
    for f in changed:
        if "/" in f:
            top = f.split("/")[0]
            if top in folder_set:
                groups.add(top)
        elif f in root_file_set:
            root_touched = True
    targets = list(groups)
    if root_touched:
        targets += ROOT_FILES
    return targets, changed


def main() -> None:
    if not TOKEN:
        print("ERROR: OUTLINE_TOKEN is required (Outline -> Settings -> API Tokens).", file=sys.stderr)
        sys.exit(1)

    doc_id = resolve_doc_id()
    argv = sys.argv[1:]
    changed_mode = "--changed" in argv
    dry_run = "--dry-run" in argv
    since_arg = next((a.split("=", 1)[1] for a in argv if a.startswith("--since=")), None)
    arg_targets = [a for a in argv if not a.startswith("--")]

    if changed_mode:
        baseline = since_arg or (
            open(BASELINE_FILE, encoding="utf-8").read().strip() if os.path.exists(BASELINE_FILE) else ""
        )
        if baseline and not is_valid_ref(baseline):
            print(f"baseline {baseline[:8]} is not a valid commit -- doing a FULL publish to reset it.",
                  file=sys.stderr)
            baseline = ""
        if not baseline:
            print("No publish baseline (.outline/last-published) -- FULL publish to establish it.",
                  file=sys.stderr)
            targets = DEFAULT_TARGETS
            is_partial = False
        else:
            t, changed = targets_for_changes(baseline)
            if not t:
                print(f"Nothing to publish: no tracked changes under known targets since {baseline[:8]}.")
                write_baseline()
                return
            shown = sorted({("root" if x in ROOT_FILES else x) for x in t})
            print(f"Changed since {baseline[:8]} ({len(changed)} file(s)) -> groups: {', '.join(shown)}")
            targets = t
            is_partial = True
        advance_baseline = True
    else:
        is_partial = len(arg_targets) > 0
        targets = arg_targets if is_partial else DEFAULT_TARGETS
        # A full publish covers everything -> safe to advance the baseline. A
        # manual partial does NOT (other folders may have unpublished changes).
        advance_baseline = not is_partial

    files: list[str] = []
    for t in targets:
        p = os.path.join(ROOT, t)
        if not os.path.exists(p):
            print(f"skip (not found): {t}", file=sys.stderr)
            continue
        walk(p, files)
    unique = sorted(set(files))
    if not unique:
        print("Nothing to sync.", file=sys.stderr)
        sys.exit(1)
    for rel in unique:
        print(f"+ {rel}")

    if dry_run:
        g = sorted({top_of(x) for x in unique})
        plan = f"partial-publish: {', '.join(g)}" if is_partial else "FULL publish"
        print(f"\n[dry-run] would {plan} ({len(unique)} file(s)). No changes made to Outline.")
        return

    # A single Outline document can't hold the whole repo (gateway 502 past
    # ~560KB), so publish a parent index doc + one child doc per top-level folder.
    info = api("documents.info", {"id": doc_id})
    parent_id = info["data"]["id"]
    collection_id = info["data"]["collectionId"]

    groups: dict[str, list[str]] = {}
    for rel in unique:
        groups.setdefault(top_of(rel), []).append(rel)
    group_names = sorted(groups.keys())

    # On a partial publish, merge freshly-walked files with those already held in
    # the OTHER child docs, so the parent index tree stays complete.
    tree_files = unique
    if is_partial:
        kept = existing_tree_files_excluding(parent_id, set(group_names))
        tree_files = sorted(set(kept) | set(unique))
    tree_group_count = len({top_of(x) for x in tree_files})

    stamp = datetime.now(timezone.utc).isoformat()
    tree = render_tree(tree_files)
    parent_header = (
        f"> Synced from the jdlab repo \u00b7 {stamp} \u00b7 {len(tree_files)} file(s) "
        f"across {tree_group_count} section(s)"
        + (f" \u00b7 partial publish: {', '.join(group_names)}" if is_partial else "")
        + "\n\n"
        + "Each top-level folder is a **child document** below. Run "
        "`npm run outline:import` to reproduce the tree.\n\n"
        + "# Repository structure\n\n```text\n" + tree + "\n```\n"
    )
    api("documents.update", {"id": parent_id, "text": parent_header, "append": False})
    print(f"\nindex: \"{info['data']['title']}\"  {info['data']['url']}"
          + ("  (partial -- tree merged)" if is_partial else ""))

    for g in group_names:
        rels = sorted(groups[g])
        sections: list[str] = []
        for rel in rels:
            with open(os.path.join(ROOT, rel), "r", encoding="utf-8") as fh:
                content = fh.read()
            sections.append(section(rel, content))
        child_id = find_or_create_child(parent_id, collection_id, g)
        header = f"> `{g}` \u00b7 {len(rels)} file(s) \u00b7 {stamp}\n\n# Files\n\n"
        n_chunks = replace_doc_body(child_id, header, sections)
        print(f"  \u2713 {g}  ({len(rels)} files, {n_chunks} chunk{'' if n_chunks == 1 else 's'})")

    print(
        f"\nDone: {'partial ' if is_partial else ''}sync of {len(unique)} file(s) across "
        f"{len(group_names)} child document(s)"
        + (f" ({len(tree_files)} in the merged index tree)" if is_partial else "")
        + "."
    )

    if advance_baseline:
        write_baseline()
        print(f"baseline -> {git('rev-parse --short HEAD')} (.outline/last-published)")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001 - top-level CLI error reporting
        print(e if isinstance(e, str) else str(e), file=sys.stderr)
        sys.exit(1)
