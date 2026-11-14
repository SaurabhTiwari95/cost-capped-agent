"""A scripted stand-in for anthropic.AsyncAnthropic. No network, no cost: every request is recorded
so tests can assert exactly what would have been sent."""

from __future__ import annotations

from types import SimpleNamespace


def usage(inp=1_000, out=200, cache_write=0, cache_read=0):
    return SimpleNamespace(
        input_tokens=inp, output_tokens=out, cache_creation_input_tokens=cache_write, cache_read_input_tokens=cache_read
    )


def text(t):
    return SimpleNamespace(type="text", text=t)


def thinking():
    return SimpleNamespace(type="thinking", thinking="", signature="sig")


def tool_use(name, args, id_="tu_1"):
    return SimpleNamespace(type="tool_use", name=name, input=args, id=id_)


def response(content, stop_reason="end_turn", model="claude-opus-5-5", use=None, stop_details=None):
    return SimpleNamespace(
        content=content, stop_reason=stop_reason, model=model, usage=use or usage(), stop_details=stop_details
    )


class _Endpoint:
    def __init__(self, owner, name):
        self.owner, self.name = owner, name

    async def create(self, **kwargs):
        self.owner.calls.append((self.name, kwargs))
        if not self.owner.script:
            raise AssertionError(f"unexpected extra call to {self.name}")
        nxt = self.owner.script.pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        return nxt(kwargs) if callable(nxt) else nxt


class FakeClient:
    def __init__(self, script=None):
        self.script = list(script or [])
        self.calls: list[tuple[str, dict]] = []
        self.messages = _Endpoint(self, "messages")
        self.beta = SimpleNamespace(messages=_Endpoint(self, "beta.messages"))
