"""供应商结算门户导出自动化模块。

模块职责边界：
- :mod:`export_automation.models`   输入条件、任务状态与结果清单的数据模型
- :mod:`export_automation.portal`   只通过页面 DOM/浏览器事件驱动门户，不借用运维接口
- :mod:`export_automation.verify`   下载文件首行元数据核对与隔离
- :mod:`export_automation.runner`   批量运行、轮询、恢复与清单输出
- :mod:`export_automation.cli`      命令行入口

本包处理的输入文件属于不可信数据，调用方应使用 ``python -I`` 运行入口脚本。
"""

from .models import (
    ExportRequest,
    RequestOutcome,
    RunStatus,
    SCHEMA_VERSION,
)
from .runner import BatchRunner, RunConfig

__all__ = [
    "ExportRequest",
    "RequestOutcome",
    "RunStatus",
    "SCHEMA_VERSION",
    "BatchRunner",
    "RunConfig",
]

__version__ = "1.0.0"
