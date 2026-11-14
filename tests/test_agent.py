"""The answer loop: routing, request shape, tools, caps and stop reasons."""

from __future__ import annotations

import asyncio
from decimal import Decimal

import anthropic
import httpx2
import pytest

from costagent.agent import FALLBACK_BETA, TOOLS, Agent
from costagent.budget import RunBudget
from tests.conftest import NOW
from tests.fakes import FakeClient, response, text, thinking, tool_use, usage


def make_agent(settings, index, budget, script):
    client = FakeClient(script)
    return Agent(settings, index, client, budget), client


async def test_answer_uses_opus_with_effort_fallbacks_and_caching(settings, index, budget):
    agent, client = make_agent(settings, index, budget, [response([thinking(), text("30 days.")])])
    answer = await agent.ask("What are the standard credit terms?")
    assert (answer.text, answer.stopped, answer.steps) == ("30 days.", "done", 1)
    endpoint, req = client.calls[0]
    assert endpoint == "beta.messages"
    assert req["model"] == "claude-opus-5-5"
    assert req["output_config"] == {"effort": "medium"}
    assert req["betas"] == [FALLBACK_BETA] and req["fallbacks"] == "default"
    assert req["cache_control"] == {"type": "ephemeral"}
    assert req["system"][-1]["cache_control"] == {"type": "ephemeral"}
    assert "handbook/payment-terms.md" in req["system"][-1]["text"]  # the repo map is in the cached prefix
    assert "thinking" not in req and "temperature" not in req and "tool_choice" not in req
    assert all(t["strict"] and t["input_schema"]["additionalProperties"] is False for t in req["tools"])


async def test_prefix_is_byte_identical_across_questions(settings, index, store):
    systems = []
    for q in ("first question", "second question"):
        b = RunBudget(store, Decimal("1"), Decimal("5"), 512, clock=lambda: NOW)
        agent, client = make_agent(settings, index, b, [response([text("ok")])])
        await agent.ask(q)
        systems.append((client.calls[0][1]["system"], client.calls[0][1]["tools"]))
    assert systems[0] == systems[1]  # nothing volatile in the cached prefix


async def test_tool_loop_runs_tools_and_returns_all_results_in_one_message(settings, index, budget):
    script = [
        response(
            [
                thinking(),
                tool_use("search_index", {"query": "late fee overdue", "limit": 5}, "a"),
                tool_use("list_files", {"prefix": "handbook/"}, "b"),
            ],
            stop_reason="tool_use",
        ),
        response([tool_use("read_file", {"path": "handbook/payment-terms.md", "start_line": 20, "end_line": 26}, "c")], "tool_use"),
        response([text("1.5% a month after 30 days overdue (handbook/payment-terms.md:20-26).")]),
    ]  # fmt: skip
    agent, client = make_agent(settings, index, budget, script)
    answer = await agent.ask("When is a late fee charged?")
    assert answer.stopped == "done" and "1.5%" in answer.text
    assert [t["tool"] for t in answer.tool_calls] == ["search_index", "list_files", "read_file"]
    second = client.calls[1][1]["messages"]
    assert second[1]["role"] == "assistant" and second[1]["content"][0].type == "thinking"  # blocks passed back as-is
    results = second[2]["content"]
    assert second[2]["role"] == "user" and [r["tool_use_id"] for r in results] == ["a", "b"]
    assert "payment-terms.md" in results[0]["content"]
    assert "late fee" in client.calls[2][1]["messages"][4]["content"][0]["content"].lower()


async def test_bad_tool_input_becomes_an_error_result_not_a_crash(settings, index, budget):
    script = [
        response(
            [
                tool_use("read_file", {"path": "../../etc/passwd", "start_line": 1, "end_line": 5}, "a"),
                tool_use("read_file", {"path": "faq.md", "start_line": "x", "end_line": 2}, "b"),
                tool_use("delete_everything", {}, "c"),
            ],
            stop_reason="tool_use",
        ),
        response([text("done")]),
    ]
    agent, client = make_agent(settings, index, budget, script)
    await agent.ask("q")
    results = client.calls[1][1]["messages"][2]["content"]
    assert all(r.get("is_error") for r in results)
    assert "Not an indexed file" in results[0]["content"]


async def test_run_cap_shrinks_max_tokens_then_stops_cleanly(settings, index, store):
    tiny = RunBudget(store, Decimal("0.03"), Decimal("5"), 512, clock=lambda: NOW)
    script = [response([tool_use("list_files", {"prefix": ""}, "a")], "tool_use", use=usage(1_500, 600))] * 5
    agent, client = make_agent(settings, index, tiny, script)
    answer = await agent.ask("q")
    assert answer.stopped == "budget" and "spend cap" in answer.text
    assert client.calls[0][1]["max_tokens"] < settings.max_output_tokens  # shrunk to what's affordable
    assert answer.usd <= Decimal("0.03")
    assert len(client.calls) < 5


async def test_no_call_at_all_when_budget_cannot_cover_a_minimal_answer(settings, index, store):
    broke = RunBudget(store, Decimal("0.0001"), Decimal("5"), 512, clock=lambda: NOW)
    agent, client = make_agent(settings, index, broke, [])
    answer = await agent.ask("q")
    assert answer.stopped == "budget" and client.calls == [] and answer.steps == 0


async def test_day_cap_is_shared_across_runs(settings, index, store):
    first = RunBudget(store, Decimal("1"), Decimal("0.05"), 512, clock=lambda: NOW)
    agent, _ = make_agent(settings, index, first, [response([text("ok")], use=usage(2_000, 1_500))])
    await agent.ask("q")
    second = RunBudget(store, Decimal("1"), Decimal("0.05"), 512, clock=lambda: NOW)
    agent2, client2 = make_agent(settings, index, second, [response([text("ok")])])
    answer = await agent2.ask("q")
    assert answer.stopped == "budget" and "day" in answer.detail and client2.calls == []


async def test_concurrent_runs_cannot_overshoot_the_day_cap(settings, index, store):
    async def one():
        b = RunBudget(store, Decimal("1"), Decimal("0.10"), 512, clock=lambda: NOW)
        agent, _ = make_agent(settings, index, b, [response([text("ok")], use=usage(1_000, 100))])
        return await agent.ask("q")

    answers = await asyncio.gather(*(one() for _ in range(8)))
    assert sum(a.usd for a in answers) <= Decimal("0.10")
    assert any(a.stopped == "budget" for a in answers)


async def test_cost_is_settled_from_real_usage_including_cache(settings, index, budget):
    agent, _ = make_agent(
        settings,
        index,
        budget,
        [response([text("ok")], use=usage(inp=100, out=1_000, cache_write=2_000, cache_read=10_000))],
    )
    answer = await agent.ask("q")
    # 100 x 4 + 2000 x 5 + 10000 x 0.20 + 1000 x 20, per million tokens
    assert answer.usd == Decimal("0.032400")
    assert answer.usage_by_model["claude-opus-5-5"]["cache_read_tokens"] == 10_000


async def test_fallback_model_is_priced_at_its_own_rates(settings, index, budget):
    agent, _ = make_agent(
        settings, index, budget, [response([text("ok")], model="claude-opus-4-8", use=usage(1_000, 1_000))]
    )
    answer = await agent.ask("q")
    assert answer.usd == Decimal("0.030000") and "claude-opus-4-8" in answer.usage_by_model


async def test_refusal(settings, index, budget):
    from types import SimpleNamespace

    agent, _ = make_agent(
        settings, index, budget, [response([], "refusal", stop_details=SimpleNamespace(category="cyber"))]
    )
    answer = await agent.ask("q")
    assert answer.stopped == "refused" and "cyber" in answer.detail and answer.text == ""


async def test_max_tokens_stop_returns_partial_text(settings, index, budget):
    agent, _ = make_agent(settings, index, budget, [response([text("partial")], "max_tokens")])
    answer = await agent.ask("q")
    assert (answer.stopped, answer.text) == ("output_limit", "partial")


async def test_step_limit(settings, index, budget):
    settings.max_steps = 2
    loop = response([tool_use("list_files", {"prefix": ""}, "a")], "tool_use")
    agent, client = make_agent(settings, index, budget, [loop, loop])
    answer = await agent.ask("q")
    assert answer.stopped == "max_steps" and len(client.calls) == 2


async def test_pause_turn_continues(settings, index, budget):
    agent, client = make_agent(settings, index, budget, [response([], "pause_turn"), response([text("ok")])])
    assert (await agent.ask("q")).text == "ok" and len(client.calls) == 2


def _status_error(code):
    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.APIStatusError("boom", response=httpx2.Response(code, request=req), body=None)


async def test_api_status_error_releases_the_reservation(settings, index, budget, store):
    agent, _ = make_agent(settings, index, budget, [_status_error(529)])
    answer = await agent.ask("q")
    assert answer.stopped == "api_error" and "529" in answer.detail
    assert answer.usd == 0 and store.day_committed(NOW.date().isoformat()) == 0


async def test_timeout_counts_the_reserved_worst_case(settings, index, budget, store):
    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    agent, _ = make_agent(settings, index, budget, [anthropic.APITimeoutError(request=req)])
    answer = await agent.ask("q")
    assert answer.stopped == "api_error" and answer.usd > 0
    assert store.day_committed(NOW.date().isoformat()) == answer.usd


async def test_summaries_use_haiku_without_fallbacks(settings, index, budget):
    agent, client = make_agent(
        settings, index, budget, [response([text("Payment terms and credit limits.")], model="claude-haiku-5-5")]
    )
    summary = await agent.summarise_file("handbook/payment-terms.md", "# Payment terms")
    assert summary == "Payment terms and credit limits."
    endpoint, req = client.calls[0]
    assert endpoint == "messages" and req["model"] == "claude-haiku-5-5"
    assert req["output_config"] == {"effort": "low"}
    assert "fallbacks" not in req and "betas" not in req


async def test_summary_refusal_or_error_returns_none(settings, index, budget):
    agent, _ = make_agent(settings, index, budget, [response([], "refusal"), _status_error(400)])
    assert await agent.summarise_file("a.md", "x") is None
    assert await agent.summarise_file("a.md", "x") is None


def test_tool_definitions_are_fixed_and_ordered():
    assert [t["name"] for t in TOOLS] == ["search_index", "read_file", "list_files"]


async def test_missing_credentials_releases_the_reservation_and_explains(settings, index, budget, store):
    no_auth = TypeError('"Could not resolve authentication method. Expected one of api_key ..."')
    agent, _ = make_agent(settings, index, budget, [no_auth])
    answer = await agent.ask("q")
    assert answer.stopped == "client_error" and "authentication" in answer.detail
    assert store.day_committed(NOW.date().isoformat()) == 0  # nothing left reserved


async def test_unexpected_error_counts_worst_case_and_propagates(settings, index, budget, store):
    agent, _ = make_agent(settings, index, budget, [RuntimeError("bug")])
    with pytest.raises(RuntimeError):
        await agent.ask("q")
    rows = store.query("SELECT state, reserved_usd, usd FROM spend")
    assert [r["state"] for r in rows] == ["settled"] and rows[0]["usd"] == rows[0]["reserved_usd"]


async def test_summary_client_errors_release_and_propagate(settings, index, budget, store):
    agent, _ = make_agent(settings, index, budget, [TypeError("no auth"), RuntimeError("bug")])
    with pytest.raises(TypeError):
        await agent.summarise_file("a.md", "x")
    assert store.day_committed(NOW.date().isoformat()) == 0
    with pytest.raises(RuntimeError):
        await agent.summarise_file("a.md", "x")
    assert store.day_committed(NOW.date().isoformat()) > 0
