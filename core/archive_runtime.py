"""Archive lifetime and native capture independent of memory providers."""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

from astrbot.api import logger

from ..storage.conversation_store import ConversationStore
from .managers.archive_query_service import ArchiveQueryService
from .memory_scope import is_event_memory_allowed
from .models.archive_models import capture_event
from .passive_group_capture import (PassiveGroupCaptureFilter,
    is_plugin_enabled_for_session,is_session_enabled)


class ArchiveRuntime:
    def __init__(self, context, config, data_dir):
        self.context,self.config,self.data_dir = context,config,Path(data_dir)
        self.store = ConversationStore(str(self.data_dir / "conversations.db"))
        self.service = ArchiveQueryService(self.store,
            result_chars=config.get("archive.result_chars",24000),
            max_candidates=config.get("archive.max_candidates",400),
            identity_aliases=config.get("access_control.identity_aliases",""))
        self.ready = False
        self.error = None
        self.queue = asyncio.Queue(maxsize=config.get("archive.capture_queue_size",512))
        self.worker = None
        self.index_task = None
        self.gaps = 0
        self.accepting = True
        self.migration = {}
        self.store.capture_authorizer = self.may_capture

    @property
    def enabled(self):
        return bool(self.config.get("archive.enabled",False))

    async def initialize(self):
        try:
            await self.store.initialize()
            self.store.archive_enabled = self.enabled
            if self.enabled:
                self.migration["working"] = await self.store.backfill_archive()
                self.migration["retained"] = await self.store.import_retained_sources(str(self.data_dir / "livingmemory.db"))
            self.ready = True
            self.worker = asyncio.create_task(self._work())
            if self.enabled:
                self.index_task = asyncio.create_task(self._index())
        except Exception as exc:
            self.error = type(exc).__name__
            logger.error("[ArchiveRuntime] 初始化失败",exc_info=True)
            raise

    def submit(self, event, *, triggered=False, global_config=None):
        self.accept_work(event)
        if not self.enabled or not self.accepting or self.config.get("archive.capture_mode","inherit")=="off":
            return
        if not is_event_memory_allowed(self.config,event):
            return
        if global_config is not None and not PassiveGroupCaptureFilter._passes_global_whitelist(event,global_config):
            return
        mode = self.config.get("archive.capture_mode","inherit")
        if not triggered and mode=="inherit" and not self.config.get("session_manager.enable_full_group_capture",True):
            return
        # Keep the raw event before processed provider prompts can replace it.
        if self.ready:
            capture_event(event,self.store.accepted_epoch(str(event.unified_msg_origin)))
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            self.gaps += 1
            logger.warning("[ArchiveRuntime] 捕获队列已满，known_gaps=%s",self.gaps)

    def accept_work(self,event):
        """Pin input lineage before any model/processing await, even query-only."""
        existing = event.get_extra("_lm_work_epoch",None)
        if isinstance(existing,int) or not self.ready:
            return
        captured = event.get_extra("_lm_archive_record",None)
        epoch = captured.epoch if hasattr(captured,"epoch") else self.store.accepted_epoch(str(event.unified_msg_origin))
        event.set_extra("_lm_work_epoch",epoch)

    async def authorized(self,event):
        """One fail-closed policy for tool reads and both capture entry paths."""
        from astrbot.api import sp
        try:
            scope = str(event.unified_msg_origin)
            if not is_event_memory_allowed(self.config,event):
                return False
            selected = getattr(event,"plugins_name",None)
            if selected is not None and not any(n in selected for n in ("LivingMemory","astrbot_plugin_livingmemory")):
                return False
            cfg = self.context.get_config(umo=scope)
            if not isinstance(cfg,dict) or not PassiveGroupCaptureFilter._passes_global_whitelist(event,cfg):
                return False
            session = await sp.get_async(scope="umo",scope_id=scope,key="session_service_config",default={})
            plugins = await sp.get_async(scope="umo",scope_id=scope,key="session_plugin_config",default={})
            if session is None:
                session = {}
            if plugins is None:
                plugins = {}
            if not isinstance(session,dict) or not isinstance(plugins,dict):
                return False
            if session.get("session_enabled") is False:
                return False
            disabled = plugins.get(scope,{}).get("disabled_plugins",[])
            return not any(n in disabled for n in ("LivingMemory","astrbot_plugin_livingmemory"))
        except Exception:
            logger.warning("[ArchiveRuntime] 权限读取失败，拒绝档案访问",exc_info=True)
            return False

    async def may_capture(self,event):
        return bool(self.enabled and self.config.get("archive.capture_mode","inherit")!="off"
                    and await self.authorized(event))

    async def prepare_tool_output(self, event, *, tool_name, items, delivery_mode):
        """Pin permissions, scope and clear epoch before the original sender runs."""
        if not self.ready or not self.accepting or not await self.may_capture(event):
            return None
        from .tool_output import snapshot_output
        self.accept_work(event)
        epoch = event.get_extra("_lm_work_epoch", None)
        if not isinstance(epoch, int) or epoch < 0:
            return None
        return snapshot_output(self, event, epoch, tool_name=tool_name,
                               items=items, delivery_mode=delivery_mode)

    async def record_tool_output(self, event, snapshot):
        """Only the caller's successful send return permits this notification.

        Persist synchronously for immediate follow-up reads. Failure affects
        evidence availability only, never the already completed delivery.
        """
        from .tool_output import ToolOutputSnapshot
        if not isinstance(snapshot, ToolOutputSnapshot) or snapshot.owner is not self:
            return {"status": "unavailable"}
        if (snapshot.conversation != str(event.unified_msg_origin)
                or snapshot.sender_id != str(event.get_self_id())
                or snapshot.platform != str(event.get_platform_name())):
            return {"status": "unavailable"}
        if not self.ready or not self.accepting or not await self.may_capture(event):
            return {"status": "unavailable"}
        try:
            uid = await self.store.archive_record(snapshot.record(time.time()))
            return {"status": "stored", "source_uid": uid}
        except asyncio.CancelledError:
            self.gaps += 1
            raise
        except Exception as exc:
            self.gaps += 1
            logger.warning("[ArchiveRuntime] 工具输出未归档，known_gaps=%s error=%s",
                           self.gaps, type(exc).__name__)
            return {"status": "not_stored"}

    async def flush(self):
        await asyncio.wait_for(self.queue.join(),timeout=20)

    async def _work(self):
        while True:
            event = await self.queue.get()
            try:
                scope = str(event.unified_msg_origin)
                if not await self.may_capture(event):
                    continue
                record = capture_event(event,self.store.accepted_epoch(scope))
                await self.store.archive_record(record)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.gaps += 1
                logger.warning("[ArchiveRuntime] 来源收录失败，known_gaps=%s",self.gaps,exc_info=True)
            finally:
                self.queue.task_done()

    async def _index(self):
        while True:
            try:
                count = await self.store.index_pending(250)
                await asyncio.sleep(0.05 if count else 10)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("[ArchiveRuntime] FTS降级，原文仍可查",exc_info=True)
                await asyncio.sleep(10)

    async def stop_capture(self):
        self.accepting = False
        if self.worker:
            try:
                await self.flush()
            except asyncio.TimeoutError:
                logger.warning("[ArchiveRuntime] 关闭时未落盘任务=%s",self.queue.qsize())
            self.worker.cancel()
        if self.index_task:
            self.index_task.cancel()
        await asyncio.gather(*(t for t in (self.worker,self.index_task) if t),return_exceptions=True)
