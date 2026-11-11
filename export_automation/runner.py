"""批量运行、轮询恢复与交付清单。

关键约束在这里落实：
- 每项输入先查状态文件：已有可证明的任务编号就**找回同一任务**，绝不重新生成；
  没有编号时才允许点一次生成；若连编号都未取得则置 ``needs_manual``，不会反复点击。
- 下载只在“编号 + 创建者 + 筛选摘要”三属性匹配且状态为完成后触发。
- 落盘文件先入暂存区，核对首行元数据：通过才进正常输出目录，不符即隔离并保留原因。
- 超时、失败、用户关闭窗口分别产出独立状态；轮询次数或总时限任一耗尽即结束。
- 恢复路径不产生任何新的门户任务（不调用生成动作）。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from playwright.sync_api import Error as PlaywrightError, Playwright, sync_playwright

from .models import (
    ExportRequest,
    RequestOutcome,
    RunStatus,
    SCHEMA_VERSION,
)
from .portal import (
    STATE_DONE,
    STATE_FAILED,
    PortalError,
    PortalSession,
    WindowClosedError,
)
from .verify import accept, check_metadata, quarantine

# 门户合成页面的固定创建者（web/data.json: creator）。
PORTAL_OWNER = "demo-user"
STATE_FILE_NAME = "run-state.json"
MANIFEST_FILE_NAME = "manifest.json"


@dataclass
class RunConfig:
    base_url: str
    work_dir: Path
    headless: bool = True
    max_polls: int = 30
    overall_timeout_s: float = 90.0
    poll_interval_s: float = 0.6
    action_timeout_ms: int = 15_000


@dataclass
class _Dirs:
    root: Path
    output: Path
    quarantine: Path
    staging: Path
    profile: Path
    state_file: Path
    manifest_file: Path

    @classmethod
    def create(cls, work_dir: Path) -> "_Dirs":
        root = Path(work_dir)
        dirs = cls(
            root=root,
            output=root / "output",
            quarantine=root / "quarantine",
            staging=root / "staging",
            profile=root / "browser-profile",
            state_file=root / STATE_FILE_NAME,
            manifest_file=root / MANIFEST_FILE_NAME,
        )
        for path in (dirs.output, dirs.quarantine, dirs.staging, dirs.profile):
            path.mkdir(parents=True, exist_ok=True)
        return dirs


class BatchRunner:
    """编排一批 :class:`ExportRequest` 的完整生命周期。"""

    def __init__(self, config: RunConfig) -> None:
        self.config = config
        self.dirs = _Dirs.create(config.work_dir)
        self._outcomes: dict[str, RequestOutcome] = {}

    # ------------------------------------------------------------ 状态持久化
    def _load_state(self) -> dict[str, Any]:
        if not self.dirs.state_file.is_file():
            return {}
        try:
            data = json.loads(self.dirs.state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save_state(self) -> None:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "requests": [o.to_manifest_row() for o in self._outcomes.values()],
        }
        tmp = self.dirs.state_file.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(self.dirs.state_file)

    def _restore(self, requests: list[ExportRequest]) -> None:
        state = self._load_state()
        saved = {
            row.get("request_id"): row
            for row in state.get("requests", [])
            if isinstance(row, dict)
        }
        for req in requests:
            # 同一 BatchRunner 实例内预置的内存状态（如刚保存的任务编号）优先，
            # 不能被磁盘旧内容覆盖；只有内存中没有该项时才从状态文件恢复。
            existing = self._outcomes.get(req.request_id)
            if existing is not None:
                if existing.filter_digest != req.digest:
                    existing.record(
                        f"输入筛选已变化（旧摘要 {existing.filter_digest}），作废旧任务编号"
                    )
                    existing.job_id = None
                    existing.owner = None
                    existing.status = RunStatus.PENDING
                    existing.filter_digest = req.digest
                continue
            row = saved.get(req.request_id)
            if row is None:
                self._outcomes[req.request_id] = RequestOutcome(
                    request_id=req.request_id, filter_digest=req.digest
                )
                continue
            try:
                outcome = RequestOutcome.from_state_row(row)
            except (TypeError, ValueError):
                # 状态文件是本工具自己写的；损坏时按无历史处理，但保留备份线索。
                outcome = RequestOutcome(
                    request_id=req.request_id, filter_digest=req.digest
                )
                outcome.record("状态文件该行无法解析，按无历史处理")
            # 输入条件若发生变化，旧任务编号不再可信，防止错绑到别的筛选。
            if outcome.filter_digest != req.digest:
                outcome.record(
                    f"输入筛选已变化（旧摘要 {outcome.filter_digest}），作废旧任务编号"
                )
                outcome.job_id = None
                outcome.owner = None
                outcome.status = RunStatus.PENDING
                outcome.filter_digest = req.digest
            self._outcomes[req.request_id] = outcome

    # ------------------------------------------------------------ 主入口
    def run(
        self,
        requests: list[ExportRequest],
        playwright: Optional[Playwright] = None,
    ) -> list[RequestOutcome]:
        """批量运行；终态项（含恢复命中）不会重复触发门户动作。

        传入已启动的 ``playwright`` 实例时复用它（宿主进程或测试已持有驱动时
        必需，同步 API 不允许嵌套启动）；否则自行管理驱动生命周期。
        """
        if not requests:
            return []
        if playwright is None:
            with sync_playwright() as owned:
                return self.run(requests, owned)
        self._restore(requests)
        try:
            with PortalSession(
                playwright,
                self.config.base_url,
                user_data_dir=self.dirs.profile,
                download_staging=self.dirs.staging,
                headless=self.config.headless,
                action_timeout_ms=self.config.action_timeout_ms,
            ) as session:
                for req in requests:
                    outcome = self._outcomes[req.request_id]
                    if outcome.status.terminal:
                        outcome.record("恢复运行：该项已有终态，跳过多余动作")
                        continue
                    self._process_one(session, req, outcome)
                    self._save_state()
        except PlaywrightError as exc:
            self._fail_all(requests, f"浏览器会话无法建立: {exc}")
        self._write_manifest()
        return list(self._outcomes.values())

    def resume(
        self,
        requests: list[ExportRequest],
        playwright: Optional[Playwright] = None,
    ) -> list[RequestOutcome]:
        """恢复入口：只找回状态文件里已有编号的任务，不生成任何新任务。

        与 :meth:`run` 共用同一份恢复后的 outcome 对象，不会二次加载状态文件。
        """
        if not requests:
            return []
        if playwright is None:
            with sync_playwright() as owned:
                return self.resume(requests, owned)
        self._restore(requests)
        try:
            with PortalSession(
                playwright,
                self.config.base_url,
                user_data_dir=self.dirs.profile,
                download_staging=self.dirs.staging,
                headless=self.config.headless,
                action_timeout_ms=self.config.action_timeout_ms,
            ) as session:
                for req in requests:
                    outcome = self._outcomes[req.request_id]
                    if outcome.status.terminal:
                        outcome.record("恢复运行：该项已有终态，跳过")
                        continue
                    if not outcome.job_id:
                        # 红线：没有可证明编号时只报告人工确认，绝不点击生成。
                        outcome.status = RunStatus.NEEDS_MANUAL
                        outcome.detail = "恢复入口不允许新建任务，且状态文件无可证明任务编号"
                        outcome.record("恢复检查：无任务编号证据，转人工确认")
                        continue
                    outcome.record(f"恢复检查：按既有任务编号 {outcome.job_id} 找回")
                    self._track_and_download(session, req, outcome)
                    self._save_state()
        except PlaywrightError as exc:
            # 恢复时会话建不起来：已有编号的项也只能转人工，不能臆造结果。
            self._fail_all(requests, f"浏览器会话无法建立: {exc}")
        self._write_manifest()
        return list(self._outcomes.values())

    def _fail_all(self, requests: list[ExportRequest], detail: str) -> None:
        """会话级故障时把所有未终态项标记为需人工确认，保证整批不崩。"""
        for req in requests:
            outcome = self._outcomes[req.request_id]
            if outcome.status.terminal:
                continue
            outcome.status = RunStatus.NEEDS_MANUAL
            outcome.detail = detail
            outcome.record(f"会话级故障，转人工确认: {detail}")

    # ------------------------------------------------------------ 单项流程
    def _process_one(
        self,
        session: PortalSession,
        req: ExportRequest,
        outcome: RequestOutcome,
    ) -> None:
        if outcome.job_id:
            outcome.record(f"沿用已保存的任务编号 {outcome.job_id}，不重新生成")
        else:
            try:
                job_id = session.generate_export(req)
            except (PortalError, PlaywrightError) as exc:
                # 生成动作未能留下可证明编号：只报告，不重试点击。
                outcome.status = RunStatus.NEEDS_MANUAL
                outcome.detail = f"生成动作未取得可证明任务编号: {exc}"
                outcome.record("生成失败且无编号证据，转人工确认（不再重复点击）")
                return
            outcome.job_id = job_id
            outcome.owner = PORTAL_OWNER
            outcome.page_summary = req.digest
            outcome.record(f"生成动作取得页面可见任务编号 {job_id}")
            self._save_state()  # 先落盘编号，允许“保存编号后退出，下次找回”
        self._track_and_download(session, req, outcome)

    def _track_and_download(
        self,
        session: PortalSession,
        req: ExportRequest,
        outcome: RequestOutcome,
    ) -> None:
        """三属性匹配 → 轮询终态 → 下载 → 元数据核对。"""
        try:
            center, mode = session.ensure_center()
            outcome.center_open_mode = mode
            outcome.record(f"导出中心就绪（打开方式: {mode}）")
            # 测试钩子：中心已打开、匹配开始前注入受控场景（如失败终态）。
            hook = getattr(self, "_on_center_ready", None)
            if hook is not None:
                hook(session, req, outcome)

            match = session.locate_or_reopen(outcome.job_id, PORTAL_OWNER, req.digest)
            if match is None:
                # 不允许“取最新一行”兜底：匹配不上直接转人工，防止拿错文件。
                outcome.status = RunStatus.NEEDS_MANUAL
                outcome.detail = (
                    f"导出中心未找到同时满足 编号={outcome.job_id} "
                    f"创建者={PORTAL_OWNER} 摘要={req.digest} 的任务"
                )
                outcome.record("三属性匹配失败，拒绝按最新行认领")
                return
            outcome.owner = match["owner"]
            outcome.record(
                f"三属性匹配通过: 编号={match['job_id']} "
                f"创建者={match['owner']} 摘要={match['filter']} 状态={match['state']}"
            )

            state = session.wait_until_terminal(
                outcome.job_id,
                max_polls=self.config.max_polls,
                overall_timeout_s=self.config.overall_timeout_s,
                poll_interval_s=self.config.poll_interval_s,
            )
            outcome.record(f"轮询结束，门户状态={state}")
            if state == STATE_FAILED:
                outcome.status = RunStatus.FAILED
                outcome.detail = "门户任务返回失败终态"
                return
            if state != STATE_DONE:
                outcome.status = RunStatus.TIMEOUT
                outcome.detail = (
                    f"轮询预算耗尽（次数={self.config.max_polls}, "
                    f"总时限={self.config.overall_timeout_s}s），最后状态={state}"
                )
                return

            staged = session.download_job(outcome.job_id)
            outcome.record(f"浏览器下载事件落盘暂存: {staged.name}")
            try:
                self._verify_and_file(staged, req, outcome)
            finally:
                # 建议文件名留证文件随本次下载一起清理，暂存区不残留。
                name_note = staged.with_suffix(staged.suffix + ".suggested-name.txt")
                if name_note.exists():
                    name_note.unlink()
        except WindowClosedError as exc:
            outcome.status = RunStatus.WINDOW_CLOSED
            outcome.detail = str(exc)
            outcome.record("导出中心窗口被关闭，流程终止于独立状态")
        except (PortalError, PlaywrightError) as exc:
            # 单项交互异常（含 Playwright 选择器/动作超时）隔离在本项内，
            # 不允许击穿循环中断整批。
            outcome.status = RunStatus.NEEDS_MANUAL
            outcome.detail = f"门户交互异常: {exc}"
            outcome.record("交互异常，转人工确认")

    def _verify_and_file(
        self,
        staged: Path,
        req: ExportRequest,
        outcome: RequestOutcome,
    ) -> None:
        reason = check_metadata(
            staged,
            expected_job_id=outcome.job_id or "",
            expected_digest=req.digest,
        )
        if reason is not None:
            target = quarantine(staged, self.dirs.quarantine, reason)
            outcome.status = RunStatus.QUARANTINED
            outcome.quarantine_file = str(target)
            outcome.quarantine_reason = reason
            outcome.record(f"元数据核对不符，已隔离: {reason}")
            return
        target = accept(staged, self.dirs.output, req.request_id, outcome.job_id or "")
        outcome.status = RunStatus.COMPLETED
        outcome.output_file = str(target)
        outcome.record(
            f"元数据核对通过（job_id/filter_digest/schema_version 均相符）: {target.name}"
        )

    # ------------------------------------------------------------ 交付清单
    def _write_manifest(self) -> None:
        rows = [o.to_manifest_row() for o in self._outcomes.values()]
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "portal": self.config.base_url,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            # 列名与每行 requests[].* 的键保持一致，并给中文释义，
            # 让交付清单本身就能解释“文件属于哪次导出”。
            "columns": {
                "request_id": "输入条件标识",
                "filter_digest": "输入筛选摘要（账期|供应商组|币种）",
                "page_summary": "页面读出的规范化筛选摘要",
                "job_id": "门户任务编号（页面可见证据）",
                "owner": "门户任务创建者",
                "center_open_mode": "导出中心打开方式",
                "status": "机器可读状态",
                "status_label_zh": "中文状态说明",
                "output_file": "校验通过的最终文件",
                "quarantine_file": "异常文件隔离位置",
                "quarantine_reason": "隔离原因",
                "detail": "补充说明",
                "events": "按序事件轨迹",
            },
            "requests": rows,
        }
        tmp = self.dirs.manifest_file.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(self.dirs.manifest_file)

    @property
    def manifest_path(self) -> Path:
        return self.dirs.manifest_file
