"""命令行入口：批量运行与恢复。

推荐以隔离模式运行（输入文件为不可信数据）::

    python3 -I run_export.py batch  --input conditions.json --work-dir ./run1
    python3 -I run_export.py resume --input conditions.json --work-dir ./run1

``resume`` 只找回状态文件中已有任务编号的任务，绝不新建门户任务。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .models import ExportRequest, RequestOutcome, RunStatus, STATUS_LABELS_ZH
from .runner import BatchRunner, RunConfig


def load_requests(path: Path) -> list[ExportRequest]:
    """加载并严格校验输入条件；任何一条不合法都中止整批，不做部分放行。"""
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict) and isinstance(raw.get("requests"), list):
        items = raw["requests"]
    elif isinstance(raw, list):
        items = raw
    else:
        raise ValueError("输入应为条件数组，或含 requests 数组的 JSON 对象")
    requests = [ExportRequest.from_dict(item) for item in items]
    ids = [r.request_id for r in requests]
    if len(ids) != len(set(ids)):
        raise ValueError("request_id 必须唯一")
    return requests


def _print_report(outcomes: list[RequestOutcome], manifest: Path) -> None:
    header = (
        f"{'输入条件':<10}{'筛选摘要':<24}{'门户任务':<10}{'创建者':<12}"
        f"{'状态':<12}最终文件/隔离位置"
    )
    print(header)
    print("-" * 96)
    for o in outcomes:
        location = o.output_file or o.quarantine_file or "-"
        print(
            f"{o.request_id:<10}{o.filter_digest:<24}{(o.job_id or '-'):<10}"
            f"{(o.owner or '-'):<12}{STATUS_LABELS_ZH[o.status]:<12}{location}"
        )
        if o.quarantine_reason:
            print(f"  └─ 隔离原因: {o.quarantine_reason}")
        if o.detail and o.status is not RunStatus.COMPLETED:
            print(f"  └─ 说明: {o.detail}")
    print(f"\n完整清单（含事件轨迹）: {manifest}")


def _build_config(args: argparse.Namespace) -> RunConfig:
    return RunConfig(
        base_url=args.base_url.rstrip("/"),
        work_dir=Path(args.work_dir),
        headless=not args.headed,
        max_polls=args.max_polls,
        overall_timeout_s=args.timeout,
        poll_interval_s=args.poll_interval,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_export",
        description="供应商结算门户导出自动化：批量生成、任务关联、下载核对与恢复",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--input", required=True, type=Path, help="筛选条件 JSON 文件")
        sp.add_argument("--work-dir", required=True, type=Path, help="运行工作目录（状态/输出/隔离）")
        sp.add_argument("--base-url", default="http://127.0.0.1:8090", help="门户地址")
        sp.add_argument("--max-polls", type=int, default=30, help="单项最大轮询次数")
        sp.add_argument("--timeout", type=float, default=90.0, help="单项总时限（秒）")
        sp.add_argument("--poll-interval", type=float, default=0.6, help="轮询间隔（秒）")
        sp.add_argument("--headed", action="store_true", help="有头模式（本地 Chromium 窗口）")

    add_common(sub.add_parser("batch", help="批量运行：必要时生成新任务"))
    add_common(sub.add_parser("resume", help="恢复运行：只找回已有任务，不新建任务"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        requests = load_requests(args.input)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"输入文件无效: {exc}", file=sys.stderr)
        return 64  # EX_USAGE

    runner = BatchRunner(_build_config(args))
    if args.command == "batch":
        outcomes = runner.run(requests)
    else:
        outcomes = runner.resume(requests)
    _print_report(outcomes, runner.manifest_path)

    bad = {RunStatus.FAILED, RunStatus.TIMEOUT, RunStatus.WINDOW_CLOSED,
           RunStatus.NEEDS_MANUAL, RunStatus.QUARANTINED}
    return 0 if all(o.status is RunStatus.COMPLETED for o in outcomes) else 2
