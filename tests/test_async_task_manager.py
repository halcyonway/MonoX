"""core/async_task.py 单测。

覆盖 requirements/async-task.md Phase 1 验收点：
- start 立即返回 + task.json 即落盘 + 契约进首条消息
- child 真跑：FinalMessage → completed + notify_parent 复用唤醒机制
- cancel 三入口统一（agent / user / timeout）：走 interrupt 路径、无孤儿 step
- 嵌套 fork：contextvar 定位 parent
- LLM 异常 → failed；重启扫描 running → interrupted；get/list 返回副本
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from unittest.mock import MagicMock

from core.async_task import (
    SK_PREFIX,
    SUBAGENT_CONTRACT,
    AsyncTaskBridge,
    AsyncTaskManager,
)
from core.loop.checkpoint import JsonlCheckpointStore
from core.loop.compression import CompressionService
from core.loop.engine import LoopEngine
from core.loop.tool_registry import ToolRegistry
from core.loop.tools.fork_task import ForkTaskTool
from core.loop.tools.read_tr_budget import ReadToolResultBudgetTool
from core.memory import FsMemoryStore
from core.protocol import InboundEvent, LlmChunk
from core.protocol.wire_frames import FrameType
from core.session_manager import SessionManager


class ScriptLLM:
    """流式 mock：script 为 chunk 列表 / "block"（挂起）/ Exception。

    - push(script)：FIFO，按 stream() 调用次序弹出（默认无匹配时也走这条队列）
    - push_when(text, script)：按「最后一条 user 消息包含 text」匹配，一次性消费；
      用于多个 session 并发跑（嵌套 fork）时确定化脚本分配
    """

    def __init__(self) -> None:
        self._scripts: list[Any] = []
        self._matched: list[list[Any]] = []  # [text, script, used]
        self.calls = 0

    def push(self, script: Any) -> None:
        self._scripts.append(script)

    def push_when(self, text: str, script: Any) -> None:
        self._matched.append([text, script, False])

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        options: dict[str, Any] | None = None,
    ) -> AsyncIterator[LlmChunk]:
        self.calls += 1
        script: Any = "block"
        last_user = next(
            (m["content"] for m in reversed(messages) if m.get("role") == "user"), ""
        )
        for entry in self._matched:
            if not entry[2] and entry[0] in last_user:
                entry[2] = True
                script = entry[1]
                break
        else:
            script = self._scripts.pop(0) if self._scripts else "block"
        if isinstance(script, Exception):
            raise script
        if script == "block":
            await asyncio.Event().wait()
            yield LlmChunk(finish_reason="stop")  # pragma: no cover — 不可达
            return
        for chunk in script:
            yield chunk
            await asyncio.sleep(0)


class _FakeTimer:
    def __init__(self, cb, arg) -> None:
        self._cb = cb
        self._arg = arg

    def cancel(self) -> None: ...

    def fire(self) -> None:
        self._cb(self._arg)


def _make_sm(tmp: Path, llm: ScriptLLM, **kw: Any) -> SessionManager:
    return SessionManager(
        llm=llm,
        compression_llm=ScriptLLM(),
        tools=ToolRegistry([]),
        compression=CompressionService(budget_tool=ReadToolResultBudgetTool(), llm=ScriptLLM()),
        memory=FsMemoryStore(tmp / "mem"),
        state_root=tmp / "state",
        traces_root=tmp / "traces",
        system_prompt="test prompt",
        path_vars={
            "MONOX_HOME": str(tmp),
            "MONOX_WORKSPACE_DIR": str(tmp / "ws"),
            "MONOX_MEMORY_DIR": str(tmp / "mem"),
            "MONOX_SKILLS_DIR": str(tmp / "skills"),
            "MONOX_TMP_DIR": str(tmp / "tmp"),
        },
        enable_traces=False,
        **kw,
    )


def _make_mgr(
    tmp: Path, sm: SessionManager, llm: ScriptLLM | None = None
) -> tuple[AsyncTaskManager, list[tuple[str, dict[str, Any]]]]:
    emitted: list[tuple[str, dict[str, Any]]] = []

    async def on_event(ftype: str, data: dict[str, Any]) -> None:
        emitted.append((ftype, data))

    timers: list[_FakeTimer] = []

    def timer_factory(delay: float, cb: Any, arg: Any) -> _FakeTimer:
        t = _FakeTimer(cb, arg)
        timers.append(t)
        return t

    mgr = AsyncTaskManager(
        session_manager=sm,
        state_root=tmp / "state",
        on_event=on_event,
        timer_factory=timer_factory,
    )
    mgr._test_timers = timers  # type: ignore[attr-defined] — timeout 测试用
    return mgr, emitted


async def _wait_for(pred: Any, timeout: float = 4.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if pred():
            return True
        await asyncio.sleep(0.02)
    return False


# ----------------------------------------------------------------------
# start / 契约 / 落盘
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_start_returns_and_persists_and_contract(tmp_path: Path):
    """start 同步返回 + task.json 即落盘 + child session 已起 + 首条消息含契约。"""
    llm = ScriptLLM()
    llm.push("block")
    sm = _make_sm(tmp_path, llm)
    mgr, emitted = _make_mgr(tmp_path, sm)

    task = await mgr.start(description="review this PR", parent_session_key="default")

    assert task.task_id.startswith("t_") and len(task.task_id) == 14
    assert task.status == "running"
    assert task.child_session_key == f"{SK_PREFIX}{task.task_id}"
    assert task.timeout_sec == 1800.0

    p = tmp_path / "state" / task.child_session_key / "task.json"
    assert p.exists()
    assert json.loads(p.read_text())["status"] == "running"

    sl = sm.get_loop(task.child_session_key)
    assert sl is not None and sl.task is not None
    assert await _wait_for(lambda: sl.loop_engine._messages), "child 应消费首条消息"
    first = sl.loop_engine._messages[0]
    assert first["role"] == "user"
    assert SUBAGENT_CONTRACT in first["content"]
    assert "review this PR" in first["content"]
    assert 'event_type="async-task-prompt"' in first["content"]

    created = [d for f, d in emitted if f == FrameType.ASYNC_TASK_CREATED]
    assert len(created) == 1
    assert created[0]["parent_session_key"] == "default"
    assert created[0]["task_id"] == task.task_id


@pytest.mark.asyncio
async def test_start_rejects_out_of_range_timeout(tmp_path: Path):
    llm = ScriptLLM()
    sm = _make_sm(tmp_path, llm)
    mgr, _ = _make_mgr(tmp_path, sm)
    with pytest.raises(ValueError):
        await mgr.start(description="x", parent_session_key="default", timeout_sec=0.5)


# ----------------------------------------------------------------------
# 完成通知（复用 wait_io 唤醒）
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_completion_notifies_parent(tmp_path: Path):
    """child FinalMessage → completed + parent input_q 收到 async-task-result。"""
    llm = ScriptLLM()
    llm.push([LlmChunk(delta_text="subagent final answer", finish_reason="stop")])
    sm = _make_sm(tmp_path, llm)
    mgr, emitted = _make_mgr(tmp_path, sm)

    task = await mgr.start(description="do it", parent_session_key="default")

    assert await _wait_for(
        lambda: (t := mgr.get(task.task_id)) is not None and t.status == "completed"
    ), "task 应 completed"
    t = mgr.get(task.task_id)
    assert t is not None
    assert t.final_text == "subagent final answer"
    assert t.finished_at is not None and t.cancel_reason is None
    # child session 已收摊
    assert sm.get_loop(task.child_session_key) is None

    # parent 被 lazy create，引擎 messages 里有 async-task-result 事件
    assert await _wait_for(lambda: sm.get_loop("default") is not None)
    parent_msgs = sm.get_loop("default").loop_engine._messages
    assert await _wait_for(
        lambda: any("async-task-result" in m["content"] for m in parent_msgs)
    )
    result_msg = [m for m in parent_msgs if "async-task-result" in m["content"]][0]
    assert "subagent final answer" in result_msg["content"]
    assert task.task_id in result_msg["content"]
    # kind="system"：runtime 内部通知，不渲染成 user_input（防 LLM 误以为是用户说话）
    assert 'kind="system"' in result_msg["content"]

    # wire：status 帧终态 + event 帧内嵌 StreamEvent
    statuses = [d for f, d in emitted if f == FrameType.ASYNC_TASK_STATUS]
    assert statuses and statuses[0]["status"] == "completed"
    assert statuses[0]["final_text"] == "subagent final answer"
    events = [d for f, d in emitted if f == FrameType.ASYNC_TASK_EVENT]
    assert events
    assert events[0]["task_id"] == task.task_id
    assert {"type", "data"} <= set(events[0]["event"].keys())

    # task.json 落终态
    p = tmp_path / "state" / task.child_session_key / "task.json"
    assert json.loads(p.read_text())["status"] == "completed"


# ----------------------------------------------------------------------
# cancel 三入口统一（interrupt 路径，无孤儿 step）
# ----------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["agent", "user"])
async def test_cancel_interrupt_path_no_orphan(tmp_path: Path, reason: str):
    llm = ScriptLLM()
    llm.push("block")
    sm = _make_sm(tmp_path, llm)
    mgr, emitted = _make_mgr(tmp_path, sm)

    task = await mgr.start(description="long job", parent_session_key="default")
    assert await _wait_for(
        lambda: (sl := sm.get_loop(task.child_session_key)) is not None and sl.loop_engine.is_busy
    ), "child 应进入 busy"

    assert await mgr.cancel(task.task_id, reason=reason) is True

    t = mgr.get(task.task_id)
    assert t is not None
    assert t.status == "cancelled"
    assert t.cancel_reason == reason
    # session 已销毁
    assert sm.get_loop(task.child_session_key) is None
    # parent 收到取消通知（llm 是共享实例，parent 引擎会消耗一次 stream 调用——先等它消化完）
    assert await _wait_for(
        lambda: sm.get_loop("default") is not None
        and any("cancelled" in m["content"] for m in sm.get_loop("default").loop_engine._messages)
    )
    # 无孤儿 step：此后 llm 调用数稳定
    calls = llm.calls
    await asyncio.sleep(0.1)
    assert llm.calls == calls
    # 终态后 cancel 容错返回 False
    assert await mgr.cancel(task.task_id, reason=reason) is False
    assert await mgr.cancel("t_missing00000", reason=reason) is False


@pytest.mark.asyncio
async def test_timeout_path(tmp_path: Path):
    llm = ScriptLLM()
    llm.push("block")
    sm = _make_sm(tmp_path, llm)
    mgr, emitted = _make_mgr(tmp_path, sm)

    task = await mgr.start(description="will time out", parent_session_key="default", timeout_sec=30)
    timer = mgr._test_timers[-1]  # type: ignore[attr-defined]
    timer.fire()  # TimerHandle 触发

    assert await _wait_for(
        lambda: (t := mgr.get(task.task_id)) is not None and t.status == "timed_out"
    )
    t = mgr.get(task.task_id)
    assert t is not None
    assert t.cancel_reason == "timeout"
    assert sm.get_loop(task.child_session_key) is None
    statuses = [d for f, d in emitted if f == FrameType.ASYNC_TASK_STATUS]
    assert statuses and statuses[0]["cancel_reason"] == "timeout"


# ----------------------------------------------------------------------
# 嵌套 fork（contextvar）
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_nested_fork_parent_is_child_sk(tmp_path: Path):
    """child 里调 fork_task → 孙任务 parent_session_key == child 的 sk（contextvar 生效）。"""
    llm = ScriptLLM()
    # 按消息内容匹配（多 session 并发，FIFO 时序不确定）：
    # child step1 → fork tool_call；grandchild → block；child step2 → final
    llm.push_when("child job", [LlmChunk(delta_tool_calls=({
        "index": 0,
        "id": "call1",
        "type": "function",
        "function": {"name": "fork_task", "arguments": json.dumps({"description": "grand task"})},
    },))])
    llm.push_when("grand task", "block")
    llm.push_when("child job", [LlmChunk(delta_text="child done", finish_reason="stop")])

    sm = _make_sm(tmp_path, llm)
    mgr, _ = _make_mgr(tmp_path, sm)
    sm._tools.add(ForkTaskTool(mgr))  # child session 创建时继承 registry

    child = await mgr.start(description="child job", parent_session_key="default")

    assert await _wait_for(
        lambda: any(t.parent_session_key == child.child_session_key for t in mgr.list())
    ), "孙任务应出现且 parent == child sk"
    gc = [t for t in mgr.list() if t.parent_session_key == child.child_session_key][0]
    assert gc.parent_session_key == f"{SK_PREFIX}{child.task_id}"

    # 收摊：等 child 真正 completed（script3 final），grandchild 还 block 着
    assert await _wait_for(
        lambda: (t := mgr.get(child.task_id)) is not None and t.status == "completed"
    )
    assert await mgr.cancel(gc.task_id, reason="agent") is True


# ----------------------------------------------------------------------
# 失败 / 重启恢复 / 副本语义
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_llm_error_marks_failed(tmp_path: Path):
    llm = ScriptLLM()
    llm.push(RuntimeError("boom"))
    sm = _make_sm(tmp_path, llm)
    mgr, emitted = _make_mgr(tmp_path, sm)

    task = await mgr.start(description="will fail", parent_session_key="default")
    assert await _wait_for(
        lambda: (t := mgr.get(task.task_id)) is not None and t.status == "failed"
    )
    t = mgr.get(task.task_id)
    assert t is not None and "boom" in (t.error or "")
    statuses = [d for f, d in emitted if f == FrameType.ASYNC_TASK_STATUS]
    assert statuses and statuses[0]["status"] == "failed"


def test_load_from_disk_marks_running_interrupted(tmp_path: Path):
    state_root = tmp_path / "state"
    d = state_root / f"{SK_PREFIX}t_deadbeefdead"
    d.mkdir(parents=True)
    running = {
        "task_id": "t_deadbeefdead",
        "kind": "subagent",
        "description": "was running",
        "meta": {},
        "parent_session_key": "default",
        "child_session_key": f"{SK_PREFIX}t_deadbeefdead",
        "status": "running",
        "created_at": 1.0,
        "timeout_sec": 1800.0,
    }
    (d / "task.json").write_text(json.dumps(running))
    # 坏文件不致命
    bad = state_root / f"{SK_PREFIX}t_broken00000"
    bad.mkdir(parents=True)
    (bad / "task.json").write_text("{not json")

    mgr = AsyncTaskManager(session_manager=MagicMock(), state_root=state_root)
    mgr.load_from_disk()

    t = mgr.get("t_deadbeefdead")
    assert t is not None
    assert t.status == "interrupted"
    assert t.finished_at is not None
    # interrupted 已回写
    assert json.loads((d / "task.json").read_text())["status"] == "interrupted"
    assert mgr.get("t_broken00000") is None


@pytest.mark.asyncio
async def test_get_list_return_copies(tmp_path: Path):
    llm = ScriptLLM()
    llm.push("block")
    sm = _make_sm(tmp_path, llm)
    mgr, _ = _make_mgr(tmp_path, sm)

    task = await mgr.start(description="copy test", parent_session_key="default")

    got = mgr.get(task.task_id)
    assert got is not None
    got.status = "hacked"
    assert mgr.get(task.task_id) is not None
    assert mgr.get(task.task_id).status == "running"  # type: ignore[union-attr]

    tasks = mgr.list(parent_session_key="default")
    assert len(tasks) == 1
    tasks[0].status = "hacked"
    assert mgr.list()[0].status == "running"


# ----------------------------------------------------------------------
# bridge 直测（manager 之外的协议行为）
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_bridge_final_and_error_paths():
    q: asyncio.Queue = asyncio.Queue()
    done: list[tuple[str, Any, Any]] = []
    seen: list[Any] = []

    async def on_event(task_id: str, ev: Any) -> None:
        seen.append(ev)

    async def on_done(task_id: str, final: Any, error: Any) -> None:
        done.append((task_id, final, error))

    bridge = AsyncTaskBridge(task_id="t_x", output_q=q, on_event=on_event, on_done=on_done)
    bridge.start()
    await q.put("evt")
    await q.put(None)  # stop 哨兵
    await asyncio.sleep(0.05)
    assert seen == ["evt"] and not done

    q2: asyncio.Queue = asyncio.Queue()
    from core.protocol import FinalMessage

    bridge2 = AsyncTaskBridge(task_id="t_y", output_q=q2, on_event=on_event, on_done=on_done)
    bridge2.start()
    final = FinalMessage(text="answer")
    await q2.put(final)
    await asyncio.sleep(0.05)
    assert done and done[0][1] is final


# ----------------------------------------------------------------------
# bash_long：后台 shell 命令
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_bash_long_completes_with_output(tmp_path: Path):
    llm = ScriptLLM()
    sm = _make_sm(tmp_path, llm)
    mgr, emitted = _make_mgr(tmp_path, sm)

    task = await mgr.start(
        description="run echo", parent_session_key="default",
        kind="bash_long", command="echo hello-stdout; echo hello-err >&2",
    )
    assert task.kind == "bash_long" and task.command
    assert await _wait_for(
        lambda: (t := mgr.get(task.task_id)) is not None and t.status == "completed"
    ), "bash 完成后应 completed"
    t = mgr.get(task.task_id)
    assert t is not None
    assert "hello-stdout" in (t.final_text or "")
    assert "hello-err" in (t.final_text or "")

    # 输出走 TokenChunk 进 ring buffer + wire（MonoDesk 详情页有内容）
    _, recent = mgr.snapshot(task.task_id) or (None, [])
    assert any(r.get("kind") == "token" and "hello-stdout" in r.get("text", "") for r in recent)

    # 父收到结果通知
    assert await _wait_for(
        lambda: sm.get_loop("default") is not None
        and any("async-task-result" in (m.get("content") or "")
                for m in sm.get_loop("default").loop_engine._messages)
    )


@pytest.mark.asyncio
async def test_bash_long_failure_marks_failed(tmp_path: Path):
    llm = ScriptLLM()
    sm = _make_sm(tmp_path, llm)
    mgr, emitted = _make_mgr(tmp_path, sm)

    task = await mgr.start(
        description="will fail", parent_session_key="default",
        kind="bash_long", command="echo oops >&2; exit 3",
    )
    assert await _wait_for(
        lambda: (t := mgr.get(task.task_id)) is not None and t.status == "failed"
    )
    t = mgr.get(task.task_id)
    assert t is not None
    assert "exit code 3" in (t.error or "")
    assert "oops" in (t.final_text or "")


@pytest.mark.asyncio
async def test_bash_long_cancel_kills_process(tmp_path: Path):
    llm = ScriptLLM()
    sm = _make_sm(tmp_path, llm)
    mgr, emitted = _make_mgr(tmp_path, sm)

    loop = asyncio.get_running_loop()
    t0 = loop.time()
    task = await mgr.start(
        description="long sleep", parent_session_key="default",
        kind="bash_long", command="sleep 30", timeout_sec=60,
    )
    await asyncio.sleep(0.15)  # 等进程起来
    import time as _time
    ok = await mgr.cancel(task.task_id, reason="user")
    assert ok
    assert loop.time() - t0 < 5, "kill 应秒级返回，不等到 sleep 30 结束"
    t = mgr.get(task.task_id)
    assert t is not None
    assert t.status == "cancelled" and t.cancel_reason == "user"


@pytest.mark.asyncio
async def test_bash_long_requires_command(tmp_path: Path):
    llm = ScriptLLM()
    sm = _make_sm(tmp_path, llm)
    mgr, _ = _make_mgr(tmp_path, sm)
    with pytest.raises(ValueError):
        await mgr.start(description="no cmd", parent_session_key="default", kind="bash_long")


# ----------------------------------------------------------------------
# poll：不带 task_ids 全量列出（跨 parent；与 Tasks 面板同口径）
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_poll_lists_all_tasks_across_parents(tmp_path: Path):
    llm = ScriptLLM()
    llm.push("block")
    sm = _make_sm(tmp_path, llm)
    mgr, _ = _make_mgr(tmp_path, sm)

    await mgr.start(description="in default", parent_session_key="default")
    # 另一个「session」fork 的任务（mock：直接换 parent key）
    llm2 = ScriptLLM()
    llm2.push("block")
    sm2 = _make_sm(tmp_path / "other", llm2)
    mgr2, _ = _make_mgr(tmp_path / "other", sm2)
    await mgr2.start(description="in chat-42", parent_session_key="chat-42")

    from core.loop.tools.poll_task import PollTaskTool
    tool = PollTaskTool(mgr)
    result = await tool.execute("c1", {})
    assert result.status == "ok"
    import json as _json
    data = _json.loads(result.stdout)
    assert data["tasks"], "不带 task_ids 应全量列出（bug 修复：之前按 parent 过滤查空）"
