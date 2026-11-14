"""Pricing, budget, store, indexer and CLI."""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal

import pytest

from costagent import pricing
from costagent.budget import BudgetExhausted, RunBudget, estimate_tokens
from costagent.cli import main
from costagent.indexer import Index, IndexReport, summarise
from costagent.pricing import Usage
from tests.conftest import NOW, ROOT
from tests.fakes import FakeClient, response, text

# --- pricing ------------------------------------------------------------------------------------


def test_cost_arithmetic_and_haiku_long_prompt_tier():
    assert pricing.cost(Usage(1_000_000, 0), "claude-opus-5-5") == Decimal("4.000000")
    assert pricing.cost(Usage(0, 0, 1_000_000, 0), "claude-opus-5-5") == Decimal("5.000000")  # 1.25x write
    assert pricing.cost(Usage(50_000, 1_000), "claude-haiku-5-5") == Decimal("0.005500")
    assert pricing.cost(Usage(150_000, 1_000), "claude-haiku-5-5") == Decimal("0.077500")  # long-prompt rates


def test_unknown_models_are_priced_at_the_worst_known_rates():
    assert pricing.price_for("some-future-model").output == Decimal("25.00")


def test_worst_case_includes_possible_fallback_models():
    assert pricing.worst_price("claude-opus-5-5").output == Decimal("25.00")  # opus 4.8 / 5 cost more per token
    assert pricing.worst_price("claude-haiku-5-5").output == Decimal("0.50")


def test_affordable_output_tokens():
    assert pricing.affordable_output_tokens("claude-haiku-5-5", 1_000, Decimal("0.001")) == 1_750
    assert pricing.affordable_output_tokens("claude-opus-5-5", 1_000_000, Decimal("1")) == 0


def test_estimate_tokens_over_counts():
    assert estimate_tokens("a" * 300) >= 100
    assert estimate_tokens({"x": response([text("hi")])}) > 0


# --- budget and store ---------------------------------------------------------------------------


async def test_reserve_settle_release(store):
    b = RunBudget(store, Decimal("0.50"), Decimal("1.00"), 512, clock=lambda: NOW)
    g = await b.reserve("answer", "claude-haiku-5-5", 1_000, 8_000)
    assert g.max_tokens == 8_000 and b.run_committed == g.reserved_usd
    await b.settle(g, "claude-haiku-5-5", Usage(1_000, 100))
    assert b.spent.usd == Decimal("0.000150") and b.run_committed == b.spent.usd
    g2 = await b.reserve("answer", "claude-haiku-5-5", 1_000, 8_000)
    await b.release(g2)
    assert store.day_committed(NOW.date().isoformat()) == Decimal("0.000150")


async def test_day_cap_resets_on_a_new_day(store):
    clock = [NOW]
    b = RunBudget(store, Decimal("1"), Decimal("0.02"), 512, clock=lambda: clock[0])
    g = await b.reserve("answer", "claude-opus-5-5", 100, 600)
    await b.settle(g, "claude-opus-5-5", Usage(100, 900))
    with pytest.raises(BudgetExhausted):
        await b.reserve("answer", "claude-opus-5-5", 100, 8_000)
    clock[0] = NOW + dt.timedelta(days=1)
    assert (await b.reserve("answer", "claude-opus-5-5", 100, 600)).max_tokens == 600


# --- indexer ------------------------------------------------------------------------------------


def make_tree(tmp_path):
    (tmp_path / "docs").mkdir(parents=True)
    (tmp_path / "docs" / "guide.md").write_text("# Guide\nInstall with pip.\n", encoding="utf-8")
    (tmp_path / ".env").write_text("TOKEN=abc\n", encoding="utf-8")
    (tmp_path / "server.pem").write_text("cert\n", encoding="utf-8")
    (tmp_path / "config.py").write_text('KEY = "AKIA' + "B" * 16 + '"\n', encoding="utf-8")  # prepublish-ignore
    (tmp_path / "image.png").write_bytes(b"\x89PNG")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "x.js").write_text("x", encoding="utf-8")
    (tmp_path / "bad.txt").write_bytes(b"\xff\xfe\xfa")
    return tmp_path


def test_index_skips_secrets_and_is_incremental(store, tmp_path):
    root = make_tree(tmp_path / "repo")
    idx = Index(store, chunk_lines=10)
    r1 = idx.build(root, 200_000)
    assert (r1.indexed, r1.skipped_secret) == (1, 3)  # .env, .pem, and the file holding a key
    assert [f["path"] for f in idx.list_files()] == ["docs/guide.md"]
    r2 = idx.build(root, 200_000)
    assert (r2.indexed, r2.unchanged) == (0, 1) and r2.paths_needing_summary == ["docs/guide.md"]
    idx.set_summary("docs/guide.md", "How to install.")
    assert idx.build(root, 200_000).paths_needing_summary == []  # summarised once, never again
    (root / "docs" / "guide.md").write_text("# Guide\nInstall with uv.\n", encoding="utf-8")
    (root / "docs" / "new.md").write_text("new\n", encoding="utf-8")
    r4 = idx.build(root, 200_000)
    assert r4.indexed == 2 and idx.search("uv")[0]["path"] == "docs/guide.md"
    (root / "docs" / "new.md").unlink()
    assert idx.build(root, 200_000).removed == 1


def test_search_is_safe_against_fts_syntax(index):
    assert index.search('"credit" OR NEAR(late* fee) -- ; DROP TABLE files') != []
    assert index.search("!!! ???") == []
    hits = index.search("restocking fee")
    assert hits[0]["path"] == "handbook/returns.md" and "[" in hits[0]["snippet"]


def test_read_lines_bounds_and_unknown_files(index):
    text = index.read_lines("handbook/returns.md", 0, 3)
    assert text.splitlines()[0].strip().startswith("1") and len(text.splitlines()) == 3
    assert len(index.read_lines("handbook/returns.md", 1, 10_000, max_lines=5).splitlines()) == 5
    assert index.read_lines("../secret.txt", 1, 2) is None


def test_repo_map_is_sorted_stable_and_capped(index):
    a, b = index.repo_map(100), index.repo_map(100)
    assert a == b and a.splitlines()[0].startswith("README.md")
    assert index.repo_map(2).splitlines()[-1].startswith("... and 4 more files")


def test_list_files_prefix_escapes_like_wildcards(index):
    assert [f["path"] for f in index.list_files("handbook/")][0] == "handbook/payment-terms.md"
    assert index.list_files("%") == [] and index.list_files("_") == []


async def test_summarise_stops_when_the_budget_runs_out(index):
    report = IndexReport()
    calls = []

    async def fake(rel, text):
        calls.append(rel)
        if len(calls) > 2:
            raise BudgetExhausted("run", Decimal("0"))
        return f"summary of {rel}" if len(calls) == 1 else None

    paths = [f["path"] for f in index.list_files()]
    stopped = await summarise(index, paths, fake, 1, report)
    assert stopped and report.summarised == 1 and report.summary_failures == 1 and len(calls) == 3


# --- CLI ----------------------------------------------------------------------------------------


def run(settings, capsys, *argv, factory=None):
    code = main(list(argv), settings=settings, client_factory=factory)
    return code, capsys.readouterr()


def test_cli_offline_commands(settings, capsys):
    code, out = run(settings, capsys, "index", str(ROOT / "sample_corpus"))
    assert code == 0 and "indexed 6 new or changed" in out.out
    code, out = run(settings, capsys, "search", "early payment discount")
    assert "handbook/payment-terms.md" in out.out
    code, out = run(settings, capsys, "files", "catalogue/")
    assert out.out.strip() == "catalogue/products.md (27 lines)"
    code, out = run(settings, capsys, "search", "zzzz")
    assert "no matches" in out.out
    code, out = run(settings, capsys, "cost-report")
    assert "no spend" in out.out


def test_cli_ask_end_to_end_with_fake_client(settings, capsys):
    run(settings, capsys, "index", str(ROOT / "sample_corpus"))
    client = FakeClient([response([text("Within 30 days of delivery.")], use=pricing_usage())])
    code, out = run(settings, capsys, "ask", "How long do I have to return items?", "--json", factory=lambda s: client)
    result = json.loads(out.out)
    assert code == 0 and result["stopped"] == "done" and result["answer"].startswith("Within 30 days")
    code, out = run(settings, capsys, "cost-report", "--days", "1")
    assert "claude-opus-5-5" in out.out and "answer" in out.out and "of the $2.0000 daily cap" in out.out


def pricing_usage():
    from tests.fakes import usage

    return usage(inp=500, out=100, cache_write=1_000, cache_read=0)


def test_cli_ask_plain_output_and_summaries(settings, capsys):
    run(settings, capsys, "index", str(ROOT / "sample_corpus"))
    client = FakeClient([response([text("Answer.")])])
    code, out = run(settings, capsys, "ask", "q", "--max-usd", "0.5", factory=lambda s: client)
    assert code == 0 and "[done] 1 steps" in out.out and "cap $0.5000" in out.out

    settings.summary_concurrency = 1
    summaries = FakeClient([response([text(f"Summary {i}.")], model="claude-haiku-5-5") for i in range(6)])
    code, out = run(settings, capsys, "index", str(ROOT / "sample_corpus"), "--summarize", factory=lambda s: summaries)
    assert code == 0 and "summarised 6 files with claude-haiku-5-5" in out.out
    code, out = run(settings, capsys, "files")
    assert "Summary" in out.out


def test_cli_errors(settings, capsys, tmp_path):
    def no_credentials(s):
        raise RuntimeError("no key")

    code, out = run(settings, capsys, "ask", "q")
    assert code == 2 and "index is empty" in out.err
    run(settings, capsys, "index", str(ROOT / "sample_corpus"))
    code, out = run(settings, capsys, "ask", "q", factory=no_credentials)
    assert code == 2 and "ANTHROPIC_API_KEY" in out.err
    code, out = run(settings, capsys, "index", str(tmp_path / "missing"))
    assert code == 2 and "Not a folder" in out.err
    code, out = run(settings, capsys, "ask", "q", "--max-usd", "0")
    assert code == 2


def test_default_client_factory_builds_an_async_client(settings, monkeypatch):
    from costagent.cli import default_client_factory

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    client = default_client_factory(settings)
    assert type(client).__name__ == "AsyncAnthropic"


def test_cli_without_credentials_explains_and_charges_nothing(settings, capsys):
    run(settings, capsys, "index", str(ROOT / "sample_corpus"))
    no_auth = TypeError("Could not resolve authentication method.")
    code, out = run(settings, capsys, "ask", "q", factory=lambda s: FakeClient([no_auth]))
    assert code == 2 and "ANTHROPIC_API_KEY" in out.err and "nothing was charged" in out.err


def test_cli_summarize_without_credentials(settings, capsys, tmp_path):
    no_auth = TypeError("Could not resolve authentication method.")
    settings.summary_concurrency = 1
    code, out = run(
        settings,
        capsys,
        "index",
        str(ROOT / "sample_corpus"),
        "--summarize",
        factory=lambda s: FakeClient([no_auth] * 6),
    )
    assert code == 2 and "ANTHROPIC_API_KEY" in out.err


def test_touched_but_unchanged_files_are_not_reindexed(store, tmp_path):
    import os

    root = tmp_path / "repo"
    root.mkdir()
    f = root / "a.md"
    f.write_text("hello\n", encoding="utf-8")
    idx = Index(store, chunk_lines=10)
    assert idx.build(root, 200_000).indexed == 1
    st = f.stat()
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))  # touched, content the same
    r = idx.build(root, 200_000)
    assert (r.indexed, r.unchanged) == (0, 1)
    assert store.query("SELECT mtime_ns FROM files")[0][0] == f.stat().st_mtime_ns  # next run skips the read
