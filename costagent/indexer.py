"""Index a folder: files and line chunks into SQLite full-text search, plus optional one-line summaries.

Never indexed: secret-looking files (.env, keys, certificates) and any file whose content looks
like it holds a credential. Unchanged files (same SHA-256) are skipped, so re-indexing is cheap
and summaries are paid for once.
"""

from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

from costagent.store import Store, utc_now

TEXT_SUFFIXES = {
    ".md", ".txt", ".rst", ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".go", ".rb", ".php", ".cs",
    ".sql", ".yml", ".yaml", ".toml", ".json", ".html", ".css", ".sh", ".ps1", ".ini", ".cfg",
}  # fmt: skip
SKIP_DIRS = {
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    "dist",
    "build",
    ".mypy_cache",
    ".ruff_cache",
    "data",
}
SECRET_NAMES = [
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "id_rsa*",
    "id_ed25519*",
    "*credentials*",
    "*secret*",
]
SECRET_CONTENT = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"
    r"|\bsk-ant-[A-Za-z0-9_-]{20,}"
    r"|\bghp_[A-Za-z0-9]{30,}"
)
_FTS_WORD = re.compile(r"[A-Za-z0-9_]{2,}")


@dataclass
class IndexReport:
    indexed: int = 0
    unchanged: int = 0
    removed: int = 0
    skipped_secret: int = 0
    skipped_other: int = 0
    summarised: int = 0
    summary_failures: int = 0
    client_error: str = ""
    paths_needing_summary: list[str] = field(default_factory=list)


def _looks_secret(rel: str) -> bool:
    name = Path(rel).name.lower()
    return any(fnmatch.fnmatch(name, pat) for pat in SECRET_NAMES)


def scan(root: Path, max_bytes: int) -> tuple[dict[str, Path], int, int]:
    """Return {relative posix path: absolute path}, secret-skip count, other-skip count."""
    found: dict[str, Path] = {}
    secrets = other = 0
    for path in sorted(root.rglob("*")):
        rel_parts = path.relative_to(root).parts
        if any(p in SKIP_DIRS for p in rel_parts[:-1]) or not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if _looks_secret(rel):
            secrets += 1
        elif path.suffix.lower() not in TEXT_SUFFIXES or path.stat().st_size > max_bytes:
            other += 1
        else:
            found[rel] = path
    return found, secrets, other


class Index:
    def __init__(self, store: Store, chunk_lines: int = 60):
        self.store, self.chunk_lines = store, chunk_lines

    # --- building -------------------------------------------------------------------------------

    def build(self, root: Path, max_bytes: int) -> IndexReport:
        report = IndexReport()
        files, report.skipped_secret, report.skipped_other = scan(root, max_bytes)
        conn = self.store.conn
        with self.store.lock:
            known = {
                r["path"]: (r["id"], r["sha256"], r["summary"], r["size"], r["mtime_ns"])
                for r in conn.execute("SELECT id, path, sha256, summary, size, mtime_ns FROM files")
            }
            conn.execute("BEGIN")
            try:
                for rel in sorted(set(known) - set(files)):
                    conn.execute("DELETE FROM chunks WHERE file_id = ?", (known[rel][0],))
                    conn.execute("DELETE FROM files WHERE id = ?", (known[rel][0],))
                    report.removed += 1
                for rel, path in files.items():
                    st = path.stat()
                    if rel in known and (known[rel][3], known[rel][4]) == (st.st_size, st.st_mtime_ns):
                        # Same size and modified time: unchanged, without opening the file (opening is the slow part).
                        report.unchanged += 1
                        if not known[rel][2]:
                            report.paths_needing_summary.append(rel)
                        continue
                    raw = path.read_bytes()
                    try:
                        text = raw.decode("utf-8")
                    except UnicodeDecodeError:
                        report.skipped_other += 1
                        continue
                    if SECRET_CONTENT.search(text):
                        report.skipped_secret += 1
                        continue
                    digest = hashlib.sha256(raw).hexdigest()
                    if rel in known and known[rel][1] == digest:  # touched but not changed
                        conn.execute("UPDATE files SET mtime_ns = ? WHERE id = ?", (st.st_mtime_ns, known[rel][0]))
                        report.unchanged += 1
                        if not known[rel][2]:
                            report.paths_needing_summary.append(rel)
                        continue
                    if rel in known:
                        conn.execute("DELETE FROM chunks WHERE file_id = ?", (known[rel][0],))
                        conn.execute("DELETE FROM files WHERE id = ?", (known[rel][0],))
                    lines = text.splitlines()
                    cur = conn.execute(
                        "INSERT INTO files (path, sha256, size, mtime_ns, lines, indexed_at) VALUES (?, ?, ?, ?, ?, ?)",
                        (rel, digest, len(raw), st.st_mtime_ns, len(lines), utc_now().isoformat()),
                    )
                    for start in range(0, max(len(lines), 1), self.chunk_lines):
                        chunk = lines[start : start + self.chunk_lines]
                        conn.execute(
                            "INSERT INTO chunks (file_id, start_line, end_line, text) VALUES (?, ?, ?, ?)",
                            (cur.lastrowid, start + 1, start + len(chunk), "\n".join(chunk)),
                        )
                    report.indexed += 1
                    report.paths_needing_summary.append(rel)
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        return report

    def set_summary(self, rel: str, summary: str) -> None:
        with self.store.lock:
            self.store.conn.execute("UPDATE files SET summary = ? WHERE path = ?", (summary.strip()[:300], rel))

    # --- reading (used by the agent's tools) ----------------------------------------------------

    def file_text(self, rel: str) -> str | None:
        rows = self.store.query(
            "SELECT c.text FROM chunks c JOIN files f ON f.id = c.file_id WHERE f.path = ? ORDER BY c.start_line",
            (rel,),
        )
        return "\n".join(r["text"] for r in rows) if rows else None

    def search(self, query: str, limit: int = 8) -> list[dict]:
        words = _FTS_WORD.findall(query)[:12]
        if not words:
            return []
        fts_query = " OR ".join(f'"{w}"' for w in words)  # quoted terms: no FTS syntax from the caller
        rows = self.store.query(
            """SELECT f.path, c.start_line, c.end_line,
                      snippet(chunks_fts, 0, '[', ']', ' … ', 24) AS snippet
               FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.rowid JOIN files f ON f.id = c.file_id
               WHERE chunks_fts MATCH ? ORDER BY bm25(chunks_fts) LIMIT ?""",
            (fts_query, limit),
        )
        return [dict(r) for r in rows]

    def read_lines(self, rel: str, start: int, end: int, max_lines: int = 200) -> str | None:
        text = self.file_text(rel)
        if text is None:
            return None
        lines = text.splitlines()
        start = max(1, start)
        end = min(len(lines), end, start + max_lines - 1)
        return "\n".join(f"{n:>5}  {lines[n - 1]}" for n in range(start, end + 1))

    def list_files(self, prefix: str = "", limit: int = 200) -> list[dict]:
        rows = self.store.query(
            "SELECT path, lines, summary FROM files WHERE path LIKE ? ESCAPE '\\' ORDER BY path LIMIT ?",
            (prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%", limit),
        )
        return [dict(r) for r in rows]

    def repo_map(self, max_files: int) -> str:
        """A stable, sorted outline of the index. Same index, same bytes: it's the cached prompt prefix."""
        with self.store.lock:  # both reads see the same index
            rows = self.store.query("SELECT path, lines, summary FROM files ORDER BY path LIMIT ?", (max_files,))
            total = self.store.query("SELECT count(*) FROM files")[0][0]
        out = [f"{r['path']} ({r['lines']} lines)" + (f": {r['summary']}" if r["summary"] else "") for r in rows]
        if total > max_files:
            out.append(f"... and {total - max_files} more files (use list_files or search_index)")
        return "\n".join(out)


async def summarise(index: Index, paths: list[str], summarise_one, concurrency: int, report: IndexReport):
    """Summarise files with the cheap model, a few at a time. Stops starting new ones when the budget runs out."""
    from costagent.budget import BudgetExhausted

    sem = asyncio.Semaphore(concurrency)
    stop = asyncio.Event()

    async def one(rel: str) -> None:
        if stop.is_set():
            return
        async with sem:
            if stop.is_set():
                return
            text = await asyncio.to_thread(index.file_text, rel)
            try:
                summary = await summarise_one(rel, text or "")
            except BudgetExhausted:
                stop.set()
                return
            except (TypeError, ValueError) as exc:  # the client can't send at all (e.g. no credentials)
                report.client_error = report.client_error or str(exc)[:300]
                stop.set()
                return
            if summary:
                await asyncio.to_thread(index.set_summary, rel, summary)
                report.summarised += 1
            else:
                report.summary_failures += 1

    await asyncio.gather(*(one(p) for p in paths))
    return stop.is_set() and not report.client_error
