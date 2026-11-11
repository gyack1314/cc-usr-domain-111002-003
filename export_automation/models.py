"""数据模型：输入条件、运行状态与交付清单。

状态机（每项输入条件对应一条 :class:`RequestOutcome`）::

    pending ──生成并捕获任务编号──▶ pending（门户排队/生成中）
       │                              │
       │（恢复时无任何可证明的编号）   ├──门户任务失败────────▶ failed
       ▼                              ├──轮询预算耗尽────────▶ timeout
    needs_manual                      ├──导出窗口被用户关闭──▶ window_closed
                                      ├──完成且元数据相符────▶ completed
                                      └──完成但元数据不符────▶ quarantined

``quarantined`` 与 ``completed`` 都是终态：文件已经落盘，但隔离文件永远不会进入
正常输出目录。
"""
from __future__ import annotations

import dataclasses
import enum
import re
from dataclasses import dataclass, field
from typing import Any, Optional

# 门户下载文件首行声明的 schema_version，本模块按此版本解析与核对。
SCHEMA_VERSION = 1

# 与门户页面一致的规范化摘要：账期|供应商组|币种。
_DIGEST_SEP = "|"
_PERIOD_RE = re.compile(r"^\d{4}-(?:0[1-9]|1[0-2])$")
_ALLOWED_VENDOR = {"north", "south"}
_ALLOWED_CURRENCY = {"CNY", "USD"}


class RunStatus(str, enum.Enum):
    """单项导出的可解释终态/中间态。"""

    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"
    WINDOW_CLOSED = "window_closed"
    NEEDS_MANUAL = "needs_manual"
    QUARANTINED = "quarantined"

    @property
    def terminal(self) -> bool:
        return self is not RunStatus.PENDING


# 中文状态说明，交付清单与 CLI 输出共用，保证“能解释每个文件属于哪次导出”。
STATUS_LABELS_ZH = {
    RunStatus.PENDING: "排队/生成中",
    RunStatus.COMPLETED: "完成并校验通过",
    RunStatus.FAILED: "门户任务失败",
    RunStatus.TIMEOUT: "轮询超时",
    RunStatus.WINDOW_CLOSED: "导出窗口被用户关闭",
    RunStatus.NEEDS_MANUAL: "缺少可证明的任务编号，需人工确认",
    RunStatus.QUARANTINED: "元数据不符已隔离",
}


@dataclass(frozen=True)
class ExportRequest:
    """一项已经确认的筛选条件。

    ``request_id`` 由输入文件给出，用于在清单与状态文件中稳定标识本项条件，
    不参与门户交互。
    """

    request_id: str
    period: str
    vendor: str
    currency: str

    @property
    def digest(self) -> str:
        """本地预期的筛选摘要，必须与页面 ``#filter`` 读出的摘要逐字一致。"""
        return _DIGEST_SEP.join((self.period, self.vendor, self.currency))

    @classmethod
    def from_dict(cls, raw: Any) -> "ExportRequest":
        """解析并校验一条不可信输入；字段缺失或越界直接报错，不做静默修正。"""
        if not isinstance(raw, dict):
            raise ValueError("每条筛选条件必须是 JSON 对象")
        try:
            req = cls(
                request_id=str(raw["request_id"]),
                period=str(raw["period"]),
                vendor=str(raw["vendor"]),
                currency=str(raw["currency"]),
            )
        except KeyError as exc:
            raise ValueError(f"缺少字段: {exc.args[0]}") from exc
        if not req.request_id.strip():
            raise ValueError("request_id 不能为空")
        if not _PERIOD_RE.match(req.period):
            raise ValueError(f"账期格式应为 YYYY-MM: {req.period!r}")
        if req.vendor not in _ALLOWED_VENDOR:
            raise ValueError(f"供应商组仅支持 {sorted(_ALLOWED_VENDOR)}: {req.vendor!r}")
        if req.currency not in _ALLOWED_CURRENCY:
            raise ValueError(f"币种仅支持 {sorted(_ALLOWED_CURRENCY)}: {req.currency!r}")
        return req


@dataclass
class RequestOutcome:
    """一项条件从输入到文件的完整轨迹，也是清单的一行。"""

    request_id: str
    filter_digest: str
    # 页面上实际读到的规范化摘要；与 filter_digest 不一致时禁止生成。
    page_summary: Optional[str] = None
    # 门户可见任务编号。只有从页面 DOM/事件中取得证据后才允许写入。
    job_id: Optional[str] = None
    owner: Optional[str] = None
    status: RunStatus = RunStatus.PENDING
    # 导出中心是“生成时自动弹窗”还是“导出中心按钮打开”，或复用已开窗口。
    center_open_mode: Optional[str] = None
    output_file: Optional[str] = None
    quarantine_file: Optional[str] = None
    quarantine_reason: Optional[str] = None
    detail: Optional[str] = None
    events: list[str] = field(default_factory=list)

    def record(self, message: str) -> None:
        """追加一条带序号的事件，便于交付时解释文件来源。"""
        self.events.append(f"{len(self.events) + 1:02d}. {message}")

    def to_manifest_row(self) -> dict[str, Any]:
        data = dataclasses.asdict(self)
        data["status"] = self.status.value
        data["status_label_zh"] = STATUS_LABELS_ZH[self.status]
        return data

    @classmethod
    def from_state_row(cls, row: dict[str, Any]) -> "RequestOutcome":
        row = dict(row)
        row["status"] = RunStatus(row["status"])
        # 清单文件含展示用的额外字段，恢复时只取数据类声明过的键。
        allowed = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in row.items() if k in allowed})
