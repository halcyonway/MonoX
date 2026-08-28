"""async-task 全链路 e2e（requirements/async-task.md Phase 4）。

真 RuntimeServer（随机端口 ws）+ SessionManager + AsyncTaskManager，按 run.py 的
装配方式接线（_register 前缀过滤 / inbound handler / on_event 广播）：

ws client hello(subscribe_async_tasks) → user_input 驱动 agent 调 fork_task
→ async_task_created 帧 fan-out 到订阅 conn → async_task_cancel inbound
→ child 协作中断 → async_task_status(cancelled) 帧 + 父 session 收到取消通知
→ async_task_list_query → async_task_list 响应。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
import websockets

from core.async_task import AsyncTaskManager
from core.loop.compression import CompressionService
from core.loop.tool_registry import ToolRegistry
from core.loop.tools.cancel_task import CancelTaskTool
from core.loop.tools.fork_task import ForkTaskTool
from core.loop.tools.poll_task import PollTaskTool
from core.loop.tools.read_tr_budget import ReadToolResultBudgetTool
from core.memory import FsMemoryStore
from core.protocol import LlmChunk
from core.protocol.wire_frames import FrameType
from core.runtime_server import RuntimeServer, RuntimeServerConfig
from core.session_manager import SessionManager
from tests.test_async_task_manager import ScriptLLM


def _make_sm(tmp: Path, llm: ScriptLLM) -> SessionManager:
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
            "MONOX_HOME": str(tmp), "MONOX_WORKSPACE_DIR": str(tmp / "ws"),
            "MONOX_MEMORY_DIR": str(tmp / "mem"), "MONOX_SKILLS_DIR": str(tmp / "s"),
            "MONOX_TMP_DIR": str(tmp / "t"),
        },
        enable_traces=False,
    )


async def _recv_until(ws: Any, pred, timeout: float = 6.0) -> dict[str, Any]:
    """循环 recv 直到 pred(frame) 命中；超时抛 TimeoutError。"""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        remain = deadline - asyncio.get_running_loop().time()
        if remain <= 0:
            raise TimeoutError("expected frame not received")
        raw = await asyncio.wait_for(ws.recv(), timeout=remain)
        frame = json.loads(raw)
        if pred(frame):
            return frame


async def _wait(pred, timeout: float = 6.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if pred():
            return
        await asyncio.sleep(0.03)
    raise TimeoutError("condition not met")


@pytest.mark.asyncio
async def test_async_task_full_flow_over_ws(tmp_path: Path):
    llm = ScriptLLM()
    # parent 的 FIFO 脚本：step1 fork tool_call → step2 final → 取消通知后 block
    llm.push([LlmChunk(delta_tool_calls=({
        "index": 0, "id": "c1", "type": "function",
        "function": {"name": "fork_task",
                     "arguments": json.dumps({"description": "child job", "timeout_sec": 60})},
    },))])
    llm.push([LlmChunk(delta_text="forked ok", finish_reason="stop")])
    llm.push("block")
    # child 用 matcher 固定消费（不抢 parent 的 FIFO）
    llm.push_when("child job", "block")

    sm = _make_sm(tmp_path, llm)
    server = RuntimeServer(RuntimeServerConfig(host="127.0.0.1", port=0))

    async def _register(sk: str, q: asyncio.Queue) -> None:
        if sk.startswith("async:"):
            return  # child 不注册 RuntimeServer consumer（单消费者保证）
        await server.register_outbound_queue(sk, q)

    async def _unregister(sk: str) -> None:
        await server.unregister_outbound_queue(sk)

    # SessionManager 构造需要 callbacks —— 先建再补（dataclass 无 setter，用 attribute 覆盖）
    sm._outbound_register = _register
    sm._outbound_unregister = _unregister
    server.set_inbound_handler(sm.dispatch_inbound)

    async def _broadcast_async(ftype: str, data: dict[str, Any]) -> None:
        await server.broadcast_async_task(ftype, data)

    mgr = AsyncTaskManager(session_manager=sm, state_root=tmp_path / "state", on_event=_broadcast_async)
    tools = sm._tools
    tools.add(ForkTaskTool(mgr))
    tools.add(PollTaskTool(mgr))
    tools.add(CancelTaskTool(mgr))

    async def _handle_async_inbound(ftype: str, data: dict[str, Any]) -> None:
        if ftype == FrameType.ASYNC_TASK_CANCEL:
            await mgr.cancel(data.get("task_id") or "", reason=data.get("reason") or "user")
        elif ftype == FrameType.ASYNC_TASK_LIST_QUERY:
            await mgr.emit_list(session_key=data.get("session_key") or "")

    server.set_async_task_handler(_handle_async_inbound)

    await sm.start()
    server_task = asyncio.create_task(server.run())
    try:
        # 等 server 起来拿端口
        for _ in range(200):
            if server._server is not None:
                break
            await asyncio.sleep(0.02)
        port = server._server.sockets[0].getsockname()[1]

        async with websockets.connect(f"ws://127.0.0.1:{port}", max_size=2**20) as ws:
            # hello：声明订阅 async task 帧
            await ws.send(json.dumps({
                "v": 1, "type": "hello", "seq": 0, "ts": 0,
                "data": {"session_key": "default", "source": "monodesk-e2e",
                         "subscribe_async_tasks": True},
            }))
            hello_reply = json.loads(await asyncio.wait_for(ws.recv(), timeout=3))
            assert hello_reply["type"] == "hello"

            # 驱动 parent：fork it
            await ws.send(json.dumps({
                "v": 1, "type": "user_input", "seq": 0, "ts": 0,
                "data": {"session_key": "default", "text": "fork it"},
            }))

            # created 帧 fan-out 到本 conn
            created = await _recv_until(
                ws, lambda f: f.get("type") == FrameType.ASYNC_TASK_CREATED
            )
            task_id = created["data"]["task_id"]
            assert created["data"]["description"] == "child job"
            assert created["data"]["parent_session_key"] == "default"
            assert created["data"]["timeout_sec"] == 60.0

            # task.json 已落盘
            tp = tmp_path / "state" / f"async:{task_id}" / "task.json"
            assert json.loads(tp.read_text())["status"] == "running"

            # child 在跑（有独立 session），parent 的 final 帧也回来
            await _wait(lambda: sm.get_loop(f"async:{task_id}") is not None
                        and sm.get_loop(f"async:{task_id}").loop_engine.is_busy)
            await _recv_until(ws, lambda f: f.get("type") == "final")

            # list query → 列表帧
            await ws.send(json.dumps({
                "v": 1, "type": FrameType.ASYNC_TASK_LIST_QUERY, "seq": 0, "ts": 0,
                "data": {"session_key": "default", "filter": None},
            }))
            listing = await _recv_until(
                ws, lambda f: f.get("type") == FrameType.ASYNC_TASK_LIST
            )
            assert any(t["task_id"] == task_id for t in listing["data"]["tasks"])

            # 手动 cancel（MonoDesk 按钮同路径：async_task_cancel inbound 帧）
            await ws.send(json.dumps({
                "v": 1, "type": FrameType.ASYNC_TASK_CANCEL, "seq": 0, "ts": 0,
                "data": {"task_id": task_id, "reason": "user"},
            }))
            status = await _recv_until(
                ws, lambda f: f.get("type") == FrameType.ASYNC_TASK_STATUS
            )
            assert status["data"]["task_id"] == task_id
            assert status["data"]["status"] == "cancelled"
            assert status["data"]["cancel_reason"] == "user"

            # child session 已收摊；task.json 落终态
            assert sm.get_loop(f"async:{task_id}") is None
            assert json.loads(tp.read_text())["status"] == "cancelled"

            # 父 agent 收到取消通知（engine messages 里有 async-task-result）
            await _wait(lambda: any(
                (m.get("content") or "").find("async-task-result") >= 0
                and "cancelled" in (m.get("content") or "")
                for m in sm.get_loop("default").loop_engine._messages
            ))
    finally:
        await mgr.shutdown()
        await sm.stop()
        await server.stop()
        server_task.cancel()
        try:
            await server_task
        except (asyncio.CancelledError, Exception):
            pass
