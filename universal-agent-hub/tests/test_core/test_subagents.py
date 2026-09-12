"""تست استخر ساب‌ایجنت‌ها."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from src.core.subagents import DelegationResult, SubagentPool, SubagentSpec, SubagentStatus


@dataclass
class FakeRun:
    text: str = "sub-agent result"
    tool_names: list[str] = field(default_factory=lambda: ["read_file"])


class FakeAgent:
    def __init__(self, *, fail: bool = False, slow: bool = False) -> None:
        self.fail = fail
        self.slow = slow
        self.closed = False
        self.asked: list[str] = []

    async def ask(self, prompt: str) -> FakeRun:
        self.asked.append(prompt)
        if self.slow:
            await asyncio.sleep(5)
        if self.fail:
            raise RuntimeError("child exploded")
        return FakeRun(text=f"answered: {prompt[:10]}")

    async def close(self) -> None:
        self.closed = True


def make_pool(*, fail: bool = False, slow: bool = False, **pool_kwargs: object) -> SubagentPool:
    """استخر با ایجنت ساختگی. kwargs ایجنت و استخر عمداً جدا شده‌اند."""
    created: list[FakeAgent] = []

    def factory(profile: str) -> FakeAgent:
        agent = FakeAgent(fail=fail, slow=slow)
        created.append(agent)
        return agent

    pool = SubagentPool(factory=factory, **pool_kwargs)  # type: ignore[arg-type]
    pool._created = created  # type: ignore[attr-defined]
    return pool


def test_spec_normalizes() -> None:
    spec = SubagentSpec(prompt="  do the thing  ", timeout=1, max_depth=99)
    assert spec.prompt == "do the thing"
    assert spec.timeout == 5.0
    assert spec.max_depth == 8
    assert spec.label == "do the thing"


def test_check_without_factory() -> None:
    pool = SubagentPool(factory=None)
    allowed, reason = pool.check(SubagentSpec(prompt="x"))
    assert allowed is False
    assert "factory" in reason


def test_check_empty_prompt() -> None:
    pool = SubagentPool(factory=lambda p: FakeAgent())
    allowed, reason = pool.check(SubagentSpec(prompt=""))
    assert allowed is False
    assert "empty" in reason


def test_delegate_success() -> None:
    pool = make_pool()
    result = asyncio.run(pool.delegate(SubagentSpec(prompt="summarise", profile="read_only")))
    assert result.ok is True
    assert result.status is SubagentStatus.DONE
    assert result.text.startswith("answered:")
    assert result.tool_names == ["read_file"]
    assert result.duration_ms >= 0
    assert pool.depth == 0


def test_delegate_closes_child() -> None:
    pool = make_pool()
    asyncio.run(pool.delegate(SubagentSpec(prompt="x")))
    assert pool._created[0].closed is True  # type: ignore[attr-defined]


def test_depth_limit_blocks() -> None:
    pool = SubagentPool(factory=lambda p: FakeAgent(), max_depth=1)
    allowed, reason = pool.check(SubagentSpec(prompt="x"), depth=1)
    assert allowed is False
    assert "depth limit" in reason
    result = asyncio.run(pool.delegate(SubagentSpec(prompt="x"), depth=1))
    assert result.status is SubagentStatus.REJECTED


def test_profile_allowlist() -> None:
    pool = SubagentPool(factory=lambda p: FakeAgent(), allowed_profiles=frozenset({"read_only"}))
    allowed, reason = pool.check(SubagentSpec(prompt="x", profile="developer"))
    assert allowed is False
    assert "not allowed" in reason
    assert pool.check(SubagentSpec(prompt="x", profile="read_only"))[0] is True


def test_child_failure_is_contained() -> None:
    pool = make_pool(fail=True)
    result = asyncio.run(pool.delegate(SubagentSpec(prompt="x")))
    assert result.status is SubagentStatus.FAILED
    assert "exploded" in result.error


def test_timeout() -> None:
    """اجرای کندتر از مهلت ⇒ FAILED با پیام timeout (نه آویزان ماندن)."""

    class Hanging:
        async def ask(self, prompt: str) -> FakeRun:
            await asyncio.sleep(30)
            return FakeRun()

        async def close(self) -> None:
            return None

    pool = SubagentPool(factory=lambda profile: Hanging())
    spec = SubagentSpec(prompt="x")
    spec.timeout = 0.2  # مستقیم ست می‌کنیم؛ validator فقط در __init__ سخت‌گیر است
    result = asyncio.run(pool.delegate(spec))
    assert result.status is SubagentStatus.FAILED
    assert "timed out" in result.error


def test_factory_returning_none() -> None:
    pool = SubagentPool(factory=lambda p: None)
    result = asyncio.run(pool.delegate(SubagentSpec(prompt="x")))
    assert result.status is SubagentStatus.FAILED
    assert "factory returned nothing" in result.error


def test_history_and_active() -> None:
    pool = make_pool()
    asyncio.run(pool.delegate(SubagentSpec(prompt="a")))
    asyncio.run(pool.delegate(SubagentSpec(prompt="b")))
    assert len(pool.history()) == 2
    assert pool.history()[0].label == "b"
    assert pool.active == []


def test_delegate_many() -> None:
    pool = make_pool()
    results = asyncio.run(pool.delegate_many([SubagentSpec(prompt="a"), SubagentSpec(prompt="b")]))
    assert len(results) == 2
    assert all(r.ok for r in results)
    assert asyncio.run(pool.delegate_many([])) == []


def test_concurrency_cap() -> None:
    """بیش از max_concurrent هم‌زمان اجرا نمی‌شود."""
    peak = {"now": 0, "max": 0}

    class SlowAgent:
        async def ask(self, prompt: str) -> FakeRun:
            peak["now"] += 1
            peak["max"] = max(peak["max"], peak["now"])
            await asyncio.sleep(0.05)
            peak["now"] -= 1
            return FakeRun()

        async def close(self) -> None:
            return None

    pool = SubagentPool(factory=lambda p: SlowAgent(), max_concurrent=2, max_depth=5)
    asyncio.run(pool.delegate_many([SubagentSpec(prompt=str(i)) for i in range(6)]))
    assert peak["max"] <= 2


def test_audit_and_notify() -> None:
    import tempfile
    from pathlib import Path

    from src.core.audit import AuditLog

    tmp = Path(tempfile.mkdtemp())
    audit = AuditLog(tmp / "a.jsonl")
    notes: list[str] = []
    pool = SubagentPool(factory=lambda p: FakeAgent(), audit=audit, notify=lambda k, t, b: notes.append(t))
    asyncio.run(pool.delegate(SubagentSpec(prompt="x")))
    assert any(e.action.startswith("subagent.") for e in audit.entries())
    assert notes


def test_notify_error_swallowed() -> None:
    def broken(kind: str, title: str, body: str) -> None:
        raise RuntimeError("down")

    pool = SubagentPool(factory=lambda p: FakeAgent(), notify=broken)
    assert asyncio.run(pool.delegate(SubagentSpec(prompt="x"))).ok is True


def test_describe() -> None:
    pool = SubagentPool(factory=lambda p: FakeAgent(), max_depth=3, max_concurrent=2)
    asyncio.run(pool.delegate(SubagentSpec(prompt="x")))
    info = pool.describe()
    assert info["max_depth"] == 3
    assert info["factory"] is True
    assert len(info["history"]) == 1


def test_result_serializes() -> None:
    data = DelegationResult(label="l", status=SubagentStatus.DONE, text="t" * 5000).as_dict()
    assert len(data["text"]) == 2000
    assert data["status"] == "done"
