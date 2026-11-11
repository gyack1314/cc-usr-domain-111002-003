"""有头 Chromium 演示：两个导出任务交错完成，仍各取各的文件。

运行方式（建议在仓库根目录）::

    python3 -I demo_interleaved.py --base-url http://127.0.0.1:8090
    xvfb-run -a python3 -I demo_interleaved.py            # 无显示器环境

脚本只通过页面 DOM 与浏览器事件驱动，不调用运维接口；产物：
- demo_assets/01..04*.png   关键步骤截图（含两个任务的不同状态）
- demo_assets/interleaved-manifest.json  输入条件/门户任务/创建者/文件/校验清单
- demo_assets/demo-output/  校验通过的两份 CSV（文件名绑定条件与任务）
"""
from __future__ import annotations

import argparse
import json
import sys
import os
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from playwright.sync_api import sync_playwright

from export_automation.models import ExportRequest
from export_automation.portal import PortalSession
from export_automation.runner import PORTAL_OWNER
from export_automation.verify import accept, check_metadata

ASSET_DIR = Path(__file__).resolve().parent / "demo_assets"


def main() -> int:
    parser = argparse.ArgumentParser(description="交错完成导出任务的有头演示")
    parser.add_argument("--base-url", default="http://127.0.0.1:8090")
    parser.add_argument("--headed", action="store_true",
                        help="强制有头（无显示器时请配合 xvfb-run）")
    parser.add_argument("--work-dir", default=str(ASSET_DIR / "demo-work"))
    args = parser.parse_args()

    ASSET_DIR.mkdir(parents=True, exist_ok=True)
    work = Path(args.work_dir)
    req_a = ExportRequest("DEMO-A", "2026-10", "north", "CNY")
    req_b = ExportRequest("DEMO-B", "2026-09", "south", "USD")
    headless = not args.headed

    manifest_rows: list[dict] = []
    with sync_playwright() as pw:
        with PortalSession(
            pw,
            args.base_url,
            user_data_dir=work / "browser-profile",
            download_staging=work / "staging",
            headless=headless,
        ) as session:
            job_a = session.generate_export(req_a)
            job_b = session.generate_export(req_b)
            center, mode = session.ensure_center()
            print(f"[1] 两个任务已生成: {job_a} / {job_b}，中心打开方式={mode}")
            center.screenshot(path=str(ASSET_DIR / "01-both-queued.png"), full_page=True)

            # 交错点 1：只推进 B，A 仍在排队。
            session.refresh_job(job_b)
            print(f"[2] 交错推进：{job_b} 完成，{job_a} 仍排队")
            center.screenshot(path=str(ASSET_DIR / "02-b-done-a-queued.png"), full_page=True)

            # 交错点 2：再推进 A。
            session.refresh_job(job_a)
            print(f"[3] 交错推进：{job_a} 也完成；最新一行是 {job_b}，但下载按任务行绑定")
            center.screenshot(path=str(ASSET_DIR / "03-both-done.png"), full_page=True)

            output_dir = ASSET_DIR / "demo-output"
            output_dir.mkdir(exist_ok=True)
            results = []
            for job_id, req in ((job_a, req_a), (job_b, req_b)):
                # 即便 B 是“最新完成”的一行，A 的下载也来自 A 自己的任务行事件。
                staged = session.download_job(job_id)
                reason = check_metadata(
                    staged, expected_job_id=job_id, expected_digest=req.digest
                )
                assert reason is None, f"演示数据元数据意外不符: {reason}"
                final = accept(staged, output_dir, req.request_id, job_id)
                note = staged.with_suffix(staged.suffix + ".suggested-name.txt")
                if note.exists():
                    note.unlink()
                first_line = json.loads(final.read_text(encoding="utf-8").splitlines()[0])
                results.append((req, job_id, final, first_line))
                manifest_rows.append({
                    "request_id": req.request_id,
                    "input_filter_digest": req.digest,
                    "portal_job_id": job_id,
                    "owner": PORTAL_OWNER,
                    "matched_by": "任务编号+创建者+筛选摘要",
                    "final_file": str(final),
                    "verified_metadata": first_line,
                    "verdict": "通过：job_id/filter_digest/schema_version 全部相符",
                })
            center.screenshot(path=str(ASSET_DIR / "04-center-final.png"), full_page=True)

    (ASSET_DIR / "interleaved-manifest.json").write_text(
        json.dumps({"requests": manifest_rows}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print("[4] 各取各的文件：")
    for req, job_id, final, meta in results:
        print(f"    {req.request_id} 摘要={req.digest} -> 任务={job_id} -> {final.name}")
        print(f"        首行元数据={meta}")
    print(f"截图与清单已写入 {ASSET_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
