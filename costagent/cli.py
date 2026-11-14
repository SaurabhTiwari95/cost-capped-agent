"""Command line.

python -m costagent index [PATH] [--summarize] [--max-usd 0.10]
python -m costagent search "words"
python -m costagent files [PREFIX]
python -m costagent ask "question" [--max-usd 0.25] [--json]
python -m costagent cost-report [--days 7]
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import sys
from decimal import Decimal
from pathlib import Path

from costagent import __version__
from costagent.budget import RunBudget
from costagent.config import Settings
from costagent.indexer import Index, summarise
from costagent.store import Store, utc_now

NO_CREDENTIALS_HINT = (
    "Set ANTHROPIC_API_KEY to your own key in this shell (PowerShell: $env:ANTHROPIC_API_KEY = '...'; "
    "macOS/Linux: export ANTHROPIC_API_KEY=...). Nothing was sent and nothing was charged."
)


def default_client_factory(settings: Settings):
    import anthropic

    # Credentials come from the environment (ANTHROPIC_API_KEY, or an `ant auth login` profile).
    return anthropic.AsyncAnthropic(timeout=settings.request_timeout_seconds, max_retries=settings.max_retries)


def _client_or_exit(factory, settings: Settings):
    try:
        return factory(settings)
    except Exception as exc:  # the SDK raises when no credentials are found
        print(
            f"Can't create the Claude client ({type(exc).__name__}). Set ANTHROPIC_API_KEY to your own key "
            "(see README, 'Configuration').",
            file=sys.stderr,
        )
        return None


def _usd(value: Decimal) -> str:
    return f"${value:.4f}"


def cmd_index(args, settings: Settings, store: Store, factory) -> int:
    root = Path(args.path).resolve()
    if not root.is_dir():
        print(f"Not a folder: {root}", file=sys.stderr)
        return 2
    index = Index(store, settings.chunk_lines)
    report = index.build(root, settings.max_file_bytes)
    print(
        f"indexed {report.indexed} new or changed, {report.unchanged} unchanged, {report.removed} removed; "
        f"skipped {report.skipped_secret} secret-looking and {report.skipped_other} other files"
    )
    if not args.summarize or not report.paths_needing_summary:
        return 0
    client = _client_or_exit(factory, settings)
    if client is None:
        return 2
    from costagent.agent import Agent

    run_cap = Decimal(str(args.max_usd)) if args.max_usd else settings.run_cap_usd
    budget = RunBudget(store, run_cap, settings.day_cap_usd, min_output_tokens=64)
    agent = Agent(settings, index, client, budget)
    stopped_early = asyncio.run(
        summarise(index, report.paths_needing_summary, agent.summarise_file, settings.summary_concurrency, report)
    )
    if report.client_error:
        print(f"Couldn't call Claude: {report.client_error}", file=sys.stderr)
        if "auth" in report.client_error.lower():
            print(NO_CREDENTIALS_HINT, file=sys.stderr)
        return 2
    print(
        f"summarised {report.summarised} files with {settings.summary_model} for {_usd(budget.spent.usd)}"
        + (" (stopped: spend cap reached)" if stopped_early else "")
        + (f"; {report.summary_failures} failed" if report.summary_failures else "")
    )
    return 0


def cmd_search(args, settings: Settings, store: Store, factory) -> int:
    hits = Index(store, settings.chunk_lines).search(args.query, args.limit)
    for h in hits:
        print(f"{h['path']}:{h['start_line']}-{h['end_line']}  {h['snippet']}")
    if not hits:
        print("no matches")
    return 0


def cmd_files(args, settings: Settings, store: Store, factory) -> int:
    for f in Index(store, settings.chunk_lines).list_files(args.prefix):
        print(f"{f['path']} ({f['lines']} lines)" + (f": {f['summary']}" if f["summary"] else ""))
    return 0


def cmd_ask(args, settings: Settings, store: Store, factory) -> int:
    index = Index(store, settings.chunk_lines)
    if not index.list_files("", 1):
        print("The index is empty. Run: python -m costagent index <folder>", file=sys.stderr)
        return 2
    client = _client_or_exit(factory, settings)
    if client is None:
        return 2
    from costagent.agent import Agent

    run_cap = Decimal(str(args.max_usd)) if args.max_usd else settings.run_cap_usd
    budget = RunBudget(store, run_cap, settings.day_cap_usd, settings.min_output_tokens)
    answer = asyncio.run(Agent(settings, index, client, budget).ask(args.question))
    if answer.stopped == "client_error":
        print(f"Couldn't call Claude: {answer.detail}", file=sys.stderr)
        if "auth" in answer.detail.lower():
            print(NO_CREDENTIALS_HINT, file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(answer.as_dict(), indent=2))
    else:
        print(answer.text)
        print(
            f"\n[{answer.stopped}] {answer.steps} steps, {answer.calls} API calls, "
            f"{len(answer.tool_calls)} tool calls, "
            f"cost {_usd(answer.usd)} (cap {_usd(run_cap)})" + (f" - {answer.detail}" if answer.detail else "")
        )
    return 0 if answer.stopped in ("done", "budget", "max_steps", "output_limit") else 1


def cmd_cost_report(args, settings: Settings, store: Store, factory) -> int:
    since = (utc_now() - dt.timedelta(days=args.days - 1)).date().isoformat()
    rows = store.report(since)
    if not rows:
        print(f"no spend since {since}")
        return 0
    print(
        f"{'day':<10}  {'model':<20} {'purpose':<8} {'calls':>5} {'input':>9} {'output':>8} "
        f"{'cache rd':>9} {'hit %':>6} {'usd':>9}"
    )
    total = Decimal("0")
    for r in rows:
        usd = sum((Decimal(x) for x in (r["usd_list"] or "0").split(",")), Decimal("0"))
        total += usd
        prompt = (r["input_tokens"] or 0) + (r["cache_write"] or 0) + (r["cache_read"] or 0)
        hit = 100 * (r["cache_read"] or 0) / prompt if prompt else 0
        print(
            f"{r['day']:<10}  {r['model']:<20} {r['purpose']:<8} {r['calls']:>5} {r['input_tokens']:>9} "
            f"{r['output_tokens']:>8} {r['cache_read']:>9} {hit:>5.0f}% {_usd(usd):>9}"
        )
    today = store.day_committed(utc_now().date().isoformat())
    print(
        f"\ntotal {_usd(total)} over {args.days} day(s); "
        f"today {_usd(today)} of the {_usd(settings.day_cap_usd)} daily cap"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="costagent", description="A Claude agent over your files, with hard spend caps.")
    p.add_argument("--version", action="version", version=f"costagent {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    i = sub.add_parser(
        "index", help="index a folder (offline); --summarize adds one-line summaries with the cheap model"
    )
    i.add_argument("path", nargs="?", default="sample_corpus")
    i.add_argument("--summarize", action="store_true")
    i.add_argument("--max-usd", type=float, help="spend cap for this run")
    i.set_defaults(func=cmd_index)

    s = sub.add_parser("search", help="full-text search the index (offline)")
    s.add_argument("query")
    s.add_argument("--limit", type=int, default=10)
    s.set_defaults(func=cmd_search)

    f = sub.add_parser("files", help="list indexed files (offline)")
    f.add_argument("prefix", nargs="?", default="")
    f.set_defaults(func=cmd_files)

    a = sub.add_parser("ask", help="ask a question; the agent searches and reads the index to answer")
    a.add_argument("question")
    a.add_argument("--max-usd", type=float, help="spend cap for this question")
    a.add_argument("--json", action="store_true", help="print the full result as JSON")
    a.set_defaults(func=cmd_ask)

    c = sub.add_parser("cost-report", help="spend per day, model and purpose, with cache hit rate")
    c.add_argument("--days", type=int, default=7)
    c.set_defaults(func=cmd_cost_report)
    return p


def main(argv: list[str] | None = None, settings: Settings | None = None, client_factory=None) -> int:
    args = build_parser().parse_args(argv)
    settings = settings or Settings()
    if getattr(args, "max_usd", None) is not None and not 0 < args.max_usd <= 100:
        print("--max-usd must be between 0 and 100", file=sys.stderr)
        return 2
    store = Store(settings.db_path)
    try:
        return args.func(args, settings, store, client_factory or default_client_factory)
    finally:
        store.close()
