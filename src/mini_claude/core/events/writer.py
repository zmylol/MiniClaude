from __future__ import annotations

import logging
from pathlib import Path
from typing import IO

from pydantic import BaseModel

from mini_claude.core.events.bus import EventBus

logger = logging.getLogger(__name__)


class EventWriter:
    # 可选限定本次运行，子运行使用独立文件及持久化父子关系
    def __init__(self, path: Path, run_id: str | None = None) -> None:
        self._path = path
        self._run_id = run_id
        self._bus: EventBus | None = None
        self._file: IO[str] | None = None

    # 打开事件文件（追加模式），供 async with 使用
    async def __aenter__(self) -> EventWriter:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(self._path, "a", encoding="utf-8")
        return self

    # 关闭事件文件
    async def __aexit__(self, *args: object) -> None:
        if self._bus is not None:
            self._bus.unsubscribe(self.handle)
            self._bus = None
        if self._file is not None:
            self._file.close()
            self._file = None

    # 将事件序列化为 JSON 行并写入文件，写入失败时记录日志但不抛出异常
    async def handle(self, event: BaseModel) -> None:
        if self._file is None:
            return
        if self._run_id is not None and getattr(event, "run_id", None) != self._run_id:
            return
        try:
            self._file.write(event.model_dump_json() + "\n")
            self._file.flush()
        except (OSError, ValueError) as e:
            logger.error("EventWriter: failed to write event: %s", e)

    # 将 handle 注册为 bus 的订阅者
    def subscribe(self, bus: EventBus) -> None:
        self._bus = bus
        bus.subscribe(self.handle)
