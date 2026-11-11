"""Playwright 门户会话：只通过页面 DOM 与浏览器事件完成全部业务动作。

设计红线（对应需求）：
- 生成动作**之前**就挂好弹窗/下载/页面事件监听，动作产生的新窗口不会丢；
- 任务编号只采信页面可见证据（``#created`` 与任务行 ``data-job``），绝不臆造；
- 导出中心按 *任务编号 + 创建者 + 筛选摘要* 三个属性共同匹配，不取最新一行；
- 完成与否只看任务行 ``.job-state`` 的可见文本，不用 networkidle、不靠固定秒数；
- 下载通过浏览器 download 事件绑定到产生它的页面/任务，不按文件名认领；
- 不调用 ``/__control`` 等运维入口（:meth:`PortalSession.inject_failure` 仅供
  本仓库测试在受控门户内制造“失败”终态，业务流程代码不使用它）。
"""
from __future__ import annotations

import contextlib
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from playwright.sync_api import (
    BrowserContext,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    expect,
)

from .models import ExportRequest

# 门户可见的中文状态文本（来自 web/app.js 渲染的 .job-state）。
STATE_QUEUED = "排队"
STATE_RUNNING = "生成中"
STATE_DONE = "完成"
STATE_FAILED = "失败"
TERMINAL_STATES = {STATE_DONE, STATE_FAILED}


class PortalError(RuntimeError):
    """门户交互层面的可预期异常。"""


class WindowClosedError(PortalError):
    """导出中心窗口在流程中被用户关闭。"""


class SummaryMismatchError(PortalError):
    """页面规范化筛选摘要与输入条件不一致，禁止在错误条件下生成。"""


@dataclass
class PopupRecord:
    page: Page
    opened_by: str          # 'auto-on-generate' | 'center-button'
    related_job: Optional[str]
    opened_at: float


class PortalSession:
    """一个浏览器上下文内的门户会话，localStorage 随用户数据目录持久化。"""

    def __init__(
        self,
        playwright: Playwright,
        base_url: str,
        *,
        user_data_dir: Path,
        download_staging: Path,
        headless: bool = True,
        action_timeout_ms: int = 15_000,
    ) -> None:
        self._pw = playwright
        self._base_url = base_url.rstrip("/")
        self._user_data_dir = Path(user_data_dir)
        self._user_data_dir.mkdir(parents=True, exist_ok=True)
        self._staging = Path(download_staging)
        self._staging.mkdir(parents=True, exist_ok=True)
        self._headless = headless
        self._timeout = action_timeout_ms
        self._context: Optional[BrowserContext] = None
        self._main_page: Optional[Page] = None
        self._center_page: Optional[Page] = None
        # 弹窗证据链：生成前就挂监听，记录每个新窗口的来源与关联任务。
        self._popups: list[PopupRecord] = []
        self._closed_pages: list[str] = []

    # ------------------------------------------------------------------ 生命周期
    def __enter__(self) -> "PortalSession":
        self._context = self._pw.chromium.launch_persistent_context(
            user_data_dir=str(self._user_data_dir),
            headless=self._headless,
            args=["--no-sandbox"],
            accept_downloads=True,
            viewport={"width": 1366, "height": 940},
            locale="zh-CN",
        )
        self._context.set_default_timeout(self._timeout)
        # 生成前注入取证脚本：门户可能复用同名窗口（window.open 的第二个参数
        # 为固定名称 'exports'），复用时浏览器不再派发 page 事件。包装
        # window.open 可以把“本次生成动作确实触发了开窗”这一事实留在主页面上，
        # 与点击动作严格绑定，不依赖网络事件或固定等待。
        self._context.add_init_script(
            """
            (() => {
              window.__exportOpens = window.__exportOpens || [];
              if (!window.__exportOpenWrapped) {
                const originalOpen = window.open.bind(window);
                window.open = function (url, ...rest) {
                  window.__exportOpens.push(String(url));
                  return originalOpen(url, ...rest);
                };
                window.__exportOpenWrapped = true;
              }
            })();
            """
        )
        # 全上下文级监听：任何页面（含弹窗）上的下载事件都会被记录。
        self._context.on("page", self._on_new_page)
        if self._context.pages:
            self._main_page = self._context.pages[0]
        else:
            self._main_page = self._context.new_page()
        self._main_page.goto(self._base_url, wait_until="domcontentloaded")
        # 等页面脚本就绪：以角色状态文本离开“页面加载中”为准，而非固定等待。
        expect(self._main_page.locator("#status")).not_to_have_text("页面加载中")
        expect(self._main_page.locator("#generate")).to_be_visible()
        return self

    def __exit__(self, *exc) -> None:
        if self._context is not None:
            self._context.close()
            self._context = None
            self._main_page = None
            self._center_page = None

    @property
    def main_page(self) -> Page:
        assert self._main_page is not None, "会话尚未启动"
        return self._main_page

    @property
    def popups(self) -> list[PopupRecord]:
        return list(self._popups)

    @property
    def closed_page_urls(self) -> list[str]:
        return list(self._closed_pages)

    def _on_new_page(self, page: Page) -> None:
        # 监听先于动作挂好；具体 opened_by 在触发动作处补登记。
        page.on("close", lambda: self._closed_pages.append(page.url))
        # 弹窗可能刚打开就被关掉，读属性前不做任何阻塞假设。
        record = PopupRecord(
            page=page,
            opened_by="unknown",
            related_job=None,
            opened_at=time.monotonic(),
        )
        self._popups.append(record)
        page._export_record = record  # type: ignore[attr-defined]

    # ------------------------------------------------------------------ 筛选与生成
    def _fill_filters(self, page: Page, req: ExportRequest) -> None:
        page.select_option("#vendor", req.vendor)
        page.select_option("#currency", req.currency)
        # 门户摘要依赖 change 事件；fill 对部分 input 不保证事件时序，
        # 这里在赋值后显式派发 change，再等待规范化摘要变成预期值。
        page.fill("#period", req.period)
        page.eval_on_selector(
            "#period",
            "(el) => el.dispatchEvent(new Event('change', {bubbles: true}))",
        )
        expect(page.locator("#filter")).to_have_text(req.digest)
        page_summary = page.locator("#filter").inner_text()
        if page_summary != req.digest:
            raise SummaryMismatchError(
                f"页面筛选摘要 {page_summary!r} 与输入 {req.digest!r} 不一致"
            )

    def generate_export(self, req: ExportRequest) -> str:
        """在主页面按条件生成导出，返回页面可见的新任务编号。

        监听在点击前就位：若门户处于“生成即弹窗”阶段，新窗口在点击同步期间
        产生，也会被上下文级 ``page`` 事件捕获并关联到本次任务。
        """
        page = self.main_page
        self._fill_filters(page, req)

        before_ids = self._visible_job_ids(page)
        # 弹窗监听在动作前已挂（上下文级 page 事件 + 注入的 window.open 记录）。
        # 门户对同名窗口的 window.open 会复用旧窗口、不派发 page 事件，
        # 因此开窗事实以主页面 __exportOpens 计数为准，窗口对象再按身份区分。
        known_pages = {r.page for r in self._popups}
        opens_before = page.evaluate(
            "() => window.__exportOpens ? window.__exportOpens.length : 0"
        )
        page.click("#generate")
        # 任务编号以页面可见元素 #created 为唯一可信来源。
        expect(page.locator("#created")).not_to_have_text("", timeout=self._timeout)
        job_id = page.locator("#created").inner_text().strip()
        if not job_id:
            raise PortalError("生成后页面未给出任务编号")
        opened_by_generate = page.evaluate(
            "() => (window.__exportOpens ? window.__exportOpens.length : 0) > %d"
            % opens_before
        )
        # window.open 与点击同帧发生，但上下文 page 事件可能在 click 返回后
        # 才派发。这里以页面侧开窗证据为前提，有界等待新弹窗记录出现，
        # 避免事件时序竞态导致漏关联（不使用固定秒数等待业务完成）。
        if opened_by_generate:
            deadline = time.monotonic() + self._timeout / 1000
            while time.monotonic() < deadline:
                if any(r.page not in known_pages for r in self._popups):
                    break
                if self._center_page is not None:
                    break  # 同名窗口复用：不会再有新 page 事件
                page.wait_for_timeout(50)
        # 若门户处于“生成即弹窗”阶段：新窗口走上下文 page 事件；
        # 复用同名窗口则把已登记的中心页面关联到本次任务。
        new_records = [r for r in self._popups if r.page not in known_pages]
        for record in new_records:
            with contextlib.suppress(PlaywrightTimeoutError):
                expect(record.page.locator("#status")).not_to_have_text(
                    "页面加载中", timeout=self._timeout
                )
            self._adopt_popup(record.page, "auto-on-generate", job_id)
        if opened_by_generate and not new_records and self._center_page is not None:
            self._adopt_popup(self._center_page, "auto-on-generate", job_id)
        if opened_by_generate and not new_records and self._center_page is None:
            # 最终兜底：事件丢失时从上下文现存页面按中心 URL 识别，
            # 仍然只采信带 ?center=1 的页面，不凭空构造窗口。
            for candidate in self._context.pages:
                if candidate is page:
                    continue
                with contextlib.suppress(Exception):
                    if "center=1" in candidate.url:
                        self._adopt_popup(candidate, "auto-on-generate", job_id)
                        break
        # 双证据：新编号必须是点击前不存在、且能在任务行上找到的。
        if job_id in before_ids:
            raise PortalError(f"任务编号 {job_id} 在生成前已存在，拒绝采信")
        self._require_job_row(page, job_id, req.digest)
        return job_id

    def _adopt_popup(self, popup: Page, opened_by: str, job_id: Optional[str]) -> None:
        record = getattr(popup, "_export_record", None)
        if record is None:
            record = PopupRecord(popup, opened_by, job_id, time.monotonic())
            self._popups.append(record)
        else:
            record.opened_by = opened_by
            record.related_job = job_id
        self._center_page = popup

    def _visible_job_ids(self, page: Page) -> set[str]:
        page.wait_for_selector("[data-job]", timeout=self._timeout)
        return set(page.eval_on_selector_all(
            "[data-job]", "els => els.map(e => e.getAttribute('data-job'))"
        ))

    # ------------------------------------------------------------------ 导出中心
    def open_center(self) -> Page:
        """通过“导出中心”按钮（第二种打开方式）打开中心并等待任务行渲染。"""
        page = self.main_page
        with page.expect_popup(timeout=self._timeout) as popup_info:
            page.click("#center")
        center = popup_info.value
        expect(center.locator("#status")).not_to_have_text("页面加载中")
        center.wait_for_selector("[data-job]", timeout=self._timeout)
        self._adopt_popup(center, "center-button", None)
        return center

    def ensure_center(self) -> tuple[Page, str]:
        """返回可用的导出中心页面及打开方式（自动弹窗 / 按钮 / 复用窗口）。"""
        if self._center_page is not None and not self._center_page.is_closed():
            return self._center_page, "reused"
        # 自动弹窗可能已出现但尚未被 adopt（例如恢复场景）。
        for record in reversed(self._popups):
            if not record.page.is_closed():
                self._center_page = record.page
                return record.page, record.opened_by
        return self.open_center(), "center-button"

    def locate_or_reopen(
        self, job_id: str, owner: str, digest: str
    ) -> Optional[dict[str, str]]:
        """先在当前中心匹配；新会话没有弹窗记录时，用按钮打开中心再匹配一次。

        这正是“两种打开导出中心方式”的恢复语义：自动弹窗只在生成动作时存在，
        恢复运行时只能经由“导出中心”按钮重新进入，且仍按三属性匹配。
        """
        match = self.find_matching_job(job_id, owner, digest)
        if match is not None:
            return match
        self._center_page = None
        self.open_center()
        return self.find_matching_job(job_id, owner, digest)

    def _require_center_open(self) -> Page:
        if self._center_page is None or self._center_page.is_closed():
            raise WindowClosedError("导出中心窗口已关闭，且没有可复用的窗口")
        return self._center_page

    def _row_locator(self, page: Page, job_id: str):
        return page.locator(f'[data-job="{job_id}"]')

    def find_matching_job(
        self, job_id: str, owner: str, digest: str
    ) -> Optional[dict[str, str]]:
        """三属性共同匹配任务行；旧任务/其他用户任务即使同名也不会命中。"""
        page = self._require_center_open()
        row = self._row_locator(page, job_id)
        if row.count() == 0:
            return None
        actual_owner = row.get_attribute("data-owner")
        actual_filter = row.get_attribute("data-filter")
        if actual_owner != owner or actual_filter != digest:
            return None
        return {
            "job_id": job_id,
            "owner": actual_owner,
            "filter": actual_filter,
            "state": row.locator(".job-state").inner_text().strip(),
        }

    def _require_job_row(self, page: Page, job_id: str, digest: str) -> None:
        row = self._row_locator(page, job_id)
        row.wait_for(state="visible", timeout=self._timeout)
        if row.get_attribute("data-filter") != digest:
            raise PortalError(
                f"任务行筛选摘要异常: {row.get_attribute('data-filter')!r} != {digest!r}"
            )

    def job_state(self, job_id: str) -> str:
        page = self._require_center_open()
        if page.is_closed():
            raise WindowClosedError("导出中心窗口已被关闭")
        row = self._row_locator(page, job_id)
        if row.count() == 0:
            raise PortalError(f"导出中心找不到任务行 {job_id}")
        return row.locator(".job-state").inner_text().strip()

    def refresh_job(self, job_id: str) -> str:
        """点击该任务自己的“刷新任务”按钮推进来源状态，并回读可见状态。"""
        page = self._require_center_open()
        row = self._row_locator(page, job_id)
        row.locator(f'[data-ready="{job_id}"]').click()
        # 状态变化以 DOM 文本变化为准；门户本身同步重渲染，这里做短轮询确认。
        deadline = time.monotonic() + self._timeout / 1000
        last = ""
        while time.monotonic() < deadline:
            last = self.job_state(job_id)
            if last in TERMINAL_STATES:
                return last
            page.wait_for_timeout(200)
        return last

    def wait_until_terminal(
        self,
        job_id: str,
        *,
        max_polls: int,
        overall_timeout_s: float,
        poll_interval_s: float = 0.6,
    ) -> str:
        """按轮询次数与总时限双预算等待终态，返回门户中文状态。

        每轮先读状态，非终态才点本任务的刷新按钮；不使用 networkidle。
        窗口被关立即抛 :class:`WindowClosedError`，由上层输出独立状态。
        """
        deadline = time.monotonic() + overall_timeout_s
        state = self.job_state(job_id)
        polls = 0
        while state not in TERMINAL_STATES:
            if polls >= max_polls or time.monotonic() >= deadline:
                return state
            state = self.refresh_job(job_id)
            polls += 1
            if state in TERMINAL_STATES:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0 or polls >= max_polls:
                break
            self._require_center_open().wait_for_timeout(
                int(min(poll_interval_s, max(remaining, 0.0)) * 1000)
            )
            state = self.job_state(job_id)
        return state

    # ------------------------------------------------------------------ 下载
    def download_job(self, job_id: str) -> Path:
        """在匹配任务行上触发下载，经浏览器 download 事件落盘到暂存区。

        下载事件与页面刷新谁先到达都不影响：download 对象由点击所在页面
        的事件给出，暂存文件名使用唯一标识，与门户的不唯一文件名解耦。
        """
        page = self._require_center_open()
        row = self._row_locator(page, job_id)
        # 只有完成态的下载按钮可点；显式等待，避免竞态下点到禁用按钮。
        expect(row.locator(f'[data-download="{job_id}"]')).to_be_enabled(
            timeout=self._timeout
        )
        with page.expect_download(timeout=self._timeout) as download_info:
            row.locator(f'[data-download="{job_id}"]').click()
        download = download_info.value
        token = uuid.uuid4().hex[:10]
        target = self._staging / f"staging-{job_id}-{token}.csv"
        download.save_as(str(target))
        # 门户建议文件名不唯一（恒为“结算资料.csv”），仅留证、不用于认领。
        target.with_suffix(".csv.suggested-name.txt").write_text(
            download.suggested_filename, encoding="utf-8"
        )
        return target

    # ------------------------------------------------------------------ 测试辅助
    def inject_failure(self, job_id: str) -> None:
        """仅供本仓库测试：在受控门户 localStorage 内把任务置为失败。

        业务流程与 CLI 不调用此方法；它不访问任何 HTTP 运维接口。
        """
        page = self.main_page
        page.evaluate(
            """([id]) => {
                const jobs = JSON.parse(localStorage.getItem('jobs') || '[]');
                const job = jobs.find(j => j.id === id);
                if (!job) throw new Error('任务不存在: ' + id);
                job.state = '失败';
                localStorage.setItem('jobs', JSON.stringify(jobs));
                window.dispatchEvent(new Event('storage'));
            }""",
            [job_id],
        )
        # 主页面与已打开的中心页都要看到失败终态；未打开的中心页下次加载时
        # 会直接从 localStorage 读到新状态，无需额外动作。
        page.evaluate("() => window.dispatchEvent(new Event('storage'))")
        center = self._center_page
        if center is not None and not center.is_closed():
            center.reload(wait_until="domcontentloaded")
            center.wait_for_selector(f'[data-job="{job_id}"]', timeout=self._timeout)
