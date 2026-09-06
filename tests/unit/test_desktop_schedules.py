from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from mini_claude.core.bus.envelope import HandlerError
from mini_claude.core.config import MiniConfig
from mini_claude.web.desktop_services import DesktopServices
from mini_claude.web.workspace import Workspace as DesktopWorkspace


class FakeWorkspace:
    # 功能：记录调度请求目标项目和命令，不连接模型或运行工具。
    # 设计：可注入发送失败以验证不确定响应不会触发重复执行。
    def __init__(self, project_path: Path) -> None:
        self.project_path = project_path
        self.project_selected = True
        self.config = MiniConfig()
        self.calls: list[tuple[Path, str, dict[str, Any]]] = []
        self.fail_send = False
        self.send_gate: asyncio.Event | None = None
        self.outcome: dict[str, Any] = {"run_id": "run-test"}

    # 功能：默认所有测试项目仍登记，让既有跨项目调度用例保留原行为。
    # 设计：移除用例可单独覆盖成员检查，避免调度单元测试依赖完整工作区服务。
    def has_project(self, path: Path) -> bool:
        return True

    # 功能：模拟 core 创建会话和接收用户消息的确认响应。
    # 设计：只操作内存，精确观察调度是否使用捕获的项目端点。
    async def request_core_for(
        self, project_path: Path, method: str, params: dict[str, Any],
    ) -> dict[str, Any]:
        self.calls.append((project_path, method, params))
        if method == "session.create":
            return {"session_id": f"sess-{len(self.calls)}"}
        if self.fail_send:
            raise TimeoutError("confirmation missing")
        if self.send_gate is not None:
            await self.send_gate.wait()
        return self.outcome


# 功能：构造带时区的最小计划输入。
# 设计：所有时间均由测试传入，避免依赖机器时钟和随机等待。
def schedule_params(when: datetime, repeat: str = "once") -> dict[str, Any]:
    return {"title": "检查项目", "prompt": "检查测试状态", "next_run": when.isoformat(), "repeat": repeat}


# 功能：移除项目停止未来计划并释放调度锁，磁盘计划保留且重新添加后能再次读取。
# 设计：使用真实工作区与固定时间推进到期边界，确认未选项目不会被调度循环偷偷恢复。
async def test_removed_project_detaches_schedules_without_deleting_records(tmp_path: Path) -> None:
    workspace = DesktopWorkspace(MiniConfig(), tmp_path, pick_project=lambda: str(tmp_path))
    workspace._request_core = AsyncMock(return_value={"sessions": []})
    now = datetime(2026, 9, 5, tzinfo=UTC)
    service = DesktopServices(workspace, now=lambda: now)
    workspace.before_remove = service.detach_project
    result = await service.handle("schedules.create", schedule_params(now + timedelta(hours=1)))
    record = tmp_path / ".mini/desktop_schedules.json"
    contents = record.read_bytes()
    await workspace.handle("workspace.remove", {"path": str(tmp_path)})
    service._now = lambda: now + timedelta(hours=2)
    await service.tick()
    assert service._jobs == {} and service._leases == {}
    assert record.read_bytes() == contents
    assert workspace._request_core.await_count == 1
    with pytest.raises(ValueError, match="先选择"):
        await service.handle("schedules.list", {})
    await workspace.handle("workspace.pick", {})
    restored = await service.handle("schedules.list", {})
    assert restored["schedules"][0]["id"] == result["schedule"]["id"]
    await service.stop()


# 功能：活动计划阻止移除项目，避免任务状态回写到已解除的调度缓存。
# 设计：直接构造服务已持久化的运行状态，独立验证计划守卫而不依赖 core 会话守卫。
async def test_running_schedule_blocks_project_removal(tmp_path: Path) -> None:
    workspace = DesktopWorkspace(MiniConfig(), tmp_path)
    workspace._request_core = AsyncMock(return_value={"sessions": []})
    now = datetime(2026, 9, 5, tzinfo=UTC)
    service = DesktopServices(workspace, now=lambda: now)
    workspace.before_remove = service.detach_project
    created = await service.handle("schedules.create", schedule_params(now))
    service._jobs[tmp_path][created["schedule"]["id"]].status = "running"
    with pytest.raises(ValueError, match="运行的计划"):
        await workspace.handle("workspace.remove", {"path": str(tmp_path)})
    assert workspace.project_selected
    assert tmp_path in service._leases
    await service.stop()


# 功能：计划新增、编辑和删除均在项目内持久化。
# 设计：创建第二个服务实例读取同一文件，确认数据跨应用启动保留。
async def test_schedule_crud_persists_per_project(tmp_path: Path) -> None:
    workspace = FakeWorkspace(tmp_path)
    now = datetime(2026, 9, 5, tzinfo=UTC)
    service = DesktopServices(workspace, now=lambda: now)
    result = await service.handle("schedules.create", schedule_params(now + timedelta(hours=1)))
    schedule_id = result["schedule"]["id"]
    await service.handle("schedules.update", {"id": schedule_id, "title": "新标题", "enabled": False})
    await service.stop()

    second = DesktopServices(workspace, now=lambda: now)
    listing = await second.handle("schedules.list", {})
    assert listing["schedules"][0]["title"] == "新标题"
    assert listing["schedules"][0]["enabled"] is False
    assert listing["runs_only_while_open"] is True
    await second.handle("schedules.delete", {"id": schedule_id})
    assert (await second.handle("schedules.list", {}))["schedules"] == []
    assert workspace.calls == []


# 功能：到期单次计划只创建一个会话并发送一次消息。
# 设计：连续扫描和重新加载都不能重复已领取的执行机会。
async def test_once_schedule_runs_only_once_across_reload(tmp_path: Path) -> None:
    workspace = FakeWorkspace(tmp_path)
    now = datetime(2026, 9, 5, tzinfo=UTC)
    service = DesktopServices(workspace, now=lambda: now)
    await service.handle("schedules.create", schedule_params(now))
    await service.tick()
    await service.tick()
    await service.stop()
    second = DesktopServices(workspace, now=lambda: now)
    await second.tick()

    assert [method for _, method, _ in workspace.calls] == ["session.create", "session.send_message"]
    job = (await second.handle("schedules.list", {}))["schedules"][0]
    assert job["enabled"] is False
    assert job["status"] == "success"
    assert job["last_session_id"] == "sess-1"


# 功能：错过多天的每日计划只补执行一次，并将下一次推进到未来。
# 设计：使用固定时区时间跨多天扫描，避免一次启动连续补发历史任务。
async def test_daily_schedule_advances_without_backlog(tmp_path: Path) -> None:
    workspace = FakeWorkspace(tmp_path)
    now = datetime(2026, 9, 5, tzinfo=UTC)
    service = DesktopServices(workspace, now=lambda: now)
    await service.handle("schedules.create", schedule_params(now - timedelta(days=3), "daily"))
    await service.tick()
    await service.tick()

    job = (await service.handle("schedules.list", {}))["schedules"][0]
    assert len(workspace.calls) == 2
    assert datetime.fromisoformat(job["next_run"]) > now
    assert job["enabled"] is True


# 功能：发送请求超时保留已创建会话并停止自动重试。
# 设计：模拟服务端是否接收未知的场景，防止同一计划重复运行用户任务。
async def test_uncertain_submission_is_not_retried(tmp_path: Path) -> None:
    workspace = FakeWorkspace(tmp_path)
    workspace.fail_send = True
    now = datetime(2026, 9, 5, tzinfo=UTC)
    service = DesktopServices(workspace, now=lambda: now)
    await service.handle("schedules.create", schedule_params(now))
    await service.tick()
    await service.tick()

    job = (await service.handle("schedules.list", {}))["schedules"][0]
    assert len(workspace.calls) == 2
    assert job["status"] == "error"
    assert job["last_session_id"] == "sess-1"
    assert "重试" in job["last_error"]


# 功能：切换界面项目后，旧计划仍使用它所属项目的 core。
# 设计：两个项目共享服务实例，检查每条执行请求携带的捕获路径。
async def test_schedules_keep_their_project_after_switch(tmp_path: Path) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    workspace = FakeWorkspace(first)
    now = datetime(2026, 9, 5, tzinfo=UTC)
    service = DesktopServices(workspace, now=lambda: now)
    await service.handle("schedules.create", schedule_params(now))
    workspace.project_path = second
    assert (await service.handle("schedules.list", {}))["schedules"] == []
    await service.tick()

    assert all(project == first for project, _, _ in workspace.calls)
    assert len(workspace.calls) == 2


# 功能：上次关闭时处于提交中的计划显示中断而不自动重复提交。
# 设计：修改已持久化的领取记录模拟进程终止，验证重启恢复规则。
async def test_interrupted_schedule_is_not_retried(tmp_path: Path) -> None:
    workspace = FakeWorkspace(tmp_path)
    now = datetime(2026, 9, 5, tzinfo=UTC)
    service = DesktopServices(workspace, now=lambda: now)
    await service.handle("schedules.create", schedule_params(now))
    await service.stop()
    path = tmp_path / ".mini/desktop_schedules.json"
    saved = json.loads(path.read_text())
    saved["schedules"][0]["status"] = "submitting"
    path.write_text(json.dumps(saved))
    restored = DesktopServices(workspace, now=lambda: now)
    await restored.tick()

    job = (await restored.handle("schedules.list", {}))["schedules"][0]
    assert job["status"] == "interrupted"
    assert workspace.calls == []


# 功能：计划时间必须包含时区，避免不同机器按不同时间执行。
# 设计：直接提交不带时区的 ISO 时间并确认未产生任何计划文件。
async def test_schedule_rejects_naive_time(tmp_path: Path) -> None:
    service = DesktopServices(FakeWorkspace(tmp_path))
    with pytest.raises(ValidationError):
        await service.handle("schedules.create", schedule_params(datetime(2026, 9, 5)))
    assert not (tmp_path / ".mini/desktop_schedules.json").exists()


# 功能：立即运行在模型执行完成前返回会话编号，不堵住界面后续命令。
# 设计：用事件阻塞替身执行，同时验证通知、重复扫描及退出中断状态。
async def test_run_now_returns_session_before_completion_and_stops_cleanly(tmp_path: Path) -> None:
    workspace = FakeWorkspace(tmp_path)
    workspace.send_gate = asyncio.Event()
    now = datetime(2026, 9, 5, tzinfo=UTC)
    events: list[dict[str, Any]] = []

    # 功能：收集调度事件，验证前端可以被动收到状态变化。
    # 设计：事件内容留在内存，不引入真实 WebSocket 或用户会话。
    async def changed(event: dict[str, Any]) -> None:
        events.append(event)

    service = DesktopServices(workspace, now=lambda: now, on_change=changed)
    created = await service.handle("schedules.create", schedule_params(now))
    result = await asyncio.wait_for(
        service.handle("schedules.run_now", {"id": created["schedule"]["id"]}), timeout=1,
    )
    assert result["schedule"]["status"] == "running"
    assert result["schedule"]["last_session_id"] == "sess-1"
    await service.tick()
    assert len(workspace.calls) == 2
    await service.stop()

    assert (await service.handle("schedules.list", {}))["schedules"][0]["status"] == "interrupted"
    assert any(event["schedule"]["status"] == "running" for event in events)
    assert events[-1]["schedule"]["status"] == "interrupted"


# 功能：并发扫描不能重复领取同一项已到期计划。
# 设计：两个扫描同时启动，断言只创建一个会话且只发送一次提示词。
async def test_concurrent_ticks_do_not_duplicate_submission(tmp_path: Path) -> None:
    workspace = FakeWorkspace(tmp_path)
    now = datetime(2026, 9, 5, tzinfo=UTC)
    service = DesktopServices(workspace, now=lambda: now)
    await service.handle("schedules.create", schedule_params(now))

    await asyncio.gather(service.tick(), service.tick())

    assert len(workspace.calls) == 2


# 功能：模型运行失败不能被计划界面误报为成功。
# 设计：使用 core 新增的真实结果字段，覆盖 RPC 正常完成但模型执行失败的情况。
async def test_failed_run_is_not_reported_as_success(tmp_path: Path) -> None:
    workspace = FakeWorkspace(tmp_path)
    workspace.outcome = {"run_id": "run-test", "status": "failed", "reason": "llm_error"}
    now = datetime(2026, 9, 5, tzinfo=UTC)
    service = DesktopServices(workspace, now=lambda: now)
    await service.handle("schedules.create", schedule_params(now))

    await service.tick()

    job = (await service.handle("schedules.list", {}))["schedules"][0]
    assert job["status"] == "error"
    assert job["last_error"] == "llm_error"


# 功能：两个网关实例同时检查同一项目时只能提交一次计划。
# 设计：使用独立服务和独立请求记录共享真实存储，覆盖进程内锁无法保护的调度边界。
async def test_two_gateways_do_not_duplicate_scheduled_run(tmp_path: Path) -> None:
    now = datetime(2026, 9, 5, tzinfo=UTC)
    first_workspace, second_workspace = FakeWorkspace(tmp_path), FakeWorkspace(tmp_path)
    first = DesktopServices(first_workspace, now=lambda: now)
    second = DesktopServices(second_workspace, now=lambda: now)
    try:
        await first.handle("schedules.create", schedule_params(now))
        await asyncio.gather(first.tick(), second.tick())
        await asyncio.gather(first.tick(), second.tick())

        assert len(first_workspace.calls) == 2
        assert second_workspace.calls == []
        await first.stop()
        await second.tick()
        assert second_workspace.calls == []
        assert (await second.handle("schedules.list", {}))["schedules"][0]["status"] == "success"
    finally:
        await first.stop()
        await second.stop()


# 功能：第二个网关不能误恢复或覆盖仍由第一个网关执行的计划。
# 设计：阻塞真实领取后的发送，检查次级实例的读写均报告占用且持久状态保持运行中。
async def test_secondary_gateway_cannot_recover_or_overwrite_live_schedule(tmp_path: Path) -> None:
    now = datetime(2026, 9, 5, tzinfo=UTC)
    first_workspace, second_workspace = FakeWorkspace(tmp_path), FakeWorkspace(tmp_path)
    first_workspace.send_gate = asyncio.Event()
    first = DesktopServices(first_workspace, now=lambda: now)
    second = DesktopServices(second_workspace, now=lambda: now)
    try:
        created = await first.handle("schedules.create", schedule_params(now))
        job_id = created["schedule"]["id"]
        await first.handle("schedules.run_now", {"id": job_id})
        commands = [
            ("schedules.list", {}),
            ("schedules.create", schedule_params(now)),
            ("schedules.update", {"id": job_id, "title": "覆盖"}),
            ("schedules.delete", {"id": job_id}),
            ("schedules.run_now", {"id": job_id}),
        ]
        for method, params in commands:
            with pytest.raises(HandlerError, match="另一个"):
                await second.handle(method, params)
        await second.tick()
        saved = json.loads((tmp_path / ".mini/desktop_schedules.json").read_text())
        assert saved["schedules"][0]["status"] == "running"
        assert saved["schedules"][0]["title"] == "检查项目"
        assert second_workspace.calls == []

        await first.stop()
        await second.tick()
        assert (await second.handle("schedules.list", {}))["schedules"][0]["status"] == "interrupted"
        assert second_workspace.calls == []
    finally:
        await first.stop()
        await second.stop()
