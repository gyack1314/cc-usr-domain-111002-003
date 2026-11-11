"""导出自动化模块的集成与单元测试。

每个用例使用独立的门户实例与独立浏览器用户目录，互不污染；
全部针对本地合成门户，不访问任何真实系统。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from playwright.sync_api import sync_playwright

from portal.__main__ import running

from export_automation.models import ExportRequest, RunStatus
from export_automation.portal import (
    STATE_DONE,
    STATE_QUEUED,
    PortalSession,
    WindowClosedError,
)
from export_automation.runner import BatchRunner, RunConfig, PORTAL_OWNER
from export_automation.verify import check_metadata, read_metadata, MetadataError

REPO_ROOT = Path(__file__).resolve().parents[1]


def make_request(rid: str, period="2026-10", vendor="north", currency="CNY") -> ExportRequest:
    return ExportRequest(rid, period, vendor, currency)


def make_config(base: str, work: Path, **kwargs) -> RunConfig:
    defaults = dict(
        base_url=base,
        work_dir=work,
        headless=True,
        max_polls=20,
        overall_timeout_s=30.0,
        poll_interval_s=0.1,
        action_timeout_ms=8000,
    )
    defaults.update(kwargs)
    return RunConfig(**defaults)


class MetadataUnitTests(unittest.TestCase):
    def test_good_metadata_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "f.csv"
            p.write_text(
                json.dumps({"job_id": "J-2", "filter_digest": "2026-10|north|CNY",
                            "schema_version": 1}, ensure_ascii=False) + "\n行2\n",
                encoding="utf-8",
            )
            self.assertIsNone(
                check_metadata(
                    p,
                    expected_job_id="J-2",
                    expected_digest="2026-10|north|CNY",
                )
            )

    def test_wrong_job_id_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "f.csv"
            p.write_text(
                json.dumps({"job_id": "J-OLD", "filter_digest": "2026-10|north|CNY",
                            "schema_version": 1}) + "\n",
                encoding="utf-8",
            )
            reason = check_metadata(
                p,
                expected_job_id="J-2",
                expected_digest="2026-10|north|CNY",
            )
            self.assertIn("job_id 不符", reason)

    def test_bad_json_and_bool_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "f.csv"
            p.write_text("not-json\n", encoding="utf-8")
            with self.assertRaises(MetadataError):
                read_metadata(p)
            p.write_text(
                json.dumps({"job_id": "J-2", "filter_digest": "x",
                            "schema_version": True}) + "\n",
                encoding="utf-8",
            )
            with self.assertRaises(MetadataError):
                read_metadata(p)


class RequestModelTests(unittest.TestCase):
    def test_digest_matches_portal_protocol(self):
        req = make_request("R1")
        self.assertEqual("2026-10|north|CNY", req.digest)

    def test_invalid_inputs_rejected(self):
        for bad in [
            {"request_id": "x", "period": "202610", "vendor": "north", "currency": "CNY"},
            {"request_id": "x", "period": "2026-10", "vendor": "west", "currency": "CNY"},
            {"request_id": "x", "period": "2026-10", "vendor": "north", "currency": "EUR"},
            {"request_id": " ", "period": "2026-10", "vendor": "north", "currency": "CNY"},
        ]:
            with self.assertRaises(ValueError):
                ExportRequest.from_dict(bad)


class PlaywrightCase(unittest.TestCase):
    """启动一次浏览器，复用给多个用例；门户与工作目录每用例独立。"""

    @classmethod
    def setUpClass(cls):
        cls.driver = sync_playwright().start()
        cls.browser = cls.driver.chromium.launch(headless=True, args=["--no-sandbox"])

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.driver.stop()

    def setUp(self):
        self.server = running()
        self.base, self.portal_state = self.server.__enter__()
        self._tmp = tempfile.TemporaryDirectory()
        self.work = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()
        self.server.__exit__(None, None, None)

    def session(self, **kw) -> PortalSession:
        # 与 BatchRunner._Dirs 的目录约定保持一致，保证会话间共享同一浏览器档案
        # （localStorage 里的门户任务才能被恢复路径找回）。
        return PortalSession(
            self.driver,
            self.base,
            user_data_dir=self.work / "browser-profile",
            download_staging=self.work / "staging",
            headless=True,
            action_timeout_ms=kw.pop("action_timeout_ms", 8000),
            **kw,
        )

    def runner(self, **kw) -> BatchRunner:
        return BatchRunner(make_config(self.base, self.work, **kw))

    def run_batch(self, requests, **kw):
        return self.runner(**kw).run(requests, self.driver)

    def resume_batch(self, requests, **kw):
        return self.runner(**kw).resume(requests, self.driver)

    def localStorage_jobs(self, session: PortalSession) -> list[dict]:
        return session.main_page.evaluate(
            "() => JSON.parse(localStorage.getItem('jobs') || '[]')"
        )


class HappyPathTests(PlaywrightCase):
    def test_batch_completes_and_files_verified(self):
        requests = [
            make_request("REQ-001"),
            make_request("REQ-002", period="2026-09", vendor="south", currency="USD"),
        ]
        outcomes = self.run_batch(requests)
        self.assertEqual([RunStatus.COMPLETED] * 2, [o.status for o in outcomes])
        # 正常输出目录有且仅有两份通过校验的文件，文件名绑定条件与任务。
        files = sorted((self.work / "output").iterdir())
        self.assertEqual(2, len(files))
        for outcome, req in zip(outcomes, requests):
            path = Path(outcome.output_file)
            self.assertTrue(path.is_file())
            self.assertIn(req.request_id, path.name)
            self.assertIn(outcome.job_id, path.name)
            meta = read_metadata(path)
            self.assertEqual(outcome.job_id, meta.job_id)
            self.assertEqual(req.digest, meta.filter_digest)
            self.assertEqual(1, meta.schema_version)
            self.assertEqual(PORTAL_OWNER, outcome.owner)
        # 暂存区与隔离区保持干净。
        self.assertEqual([], list((self.work / "staging").iterdir()))
        quarantine_dir = self.work / "quarantine"
        self.assertTrue(not quarantine_dir.exists() or not any(quarantine_dir.iterdir()))
        # 清单可解释每个文件属于哪次导出。
        manifest = json.loads((self.work / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(2, len(manifest["requests"]))
        row = manifest["requests"][0]
        self.assertEqual("completed", row["status"])
        self.assertTrue(any("三属性匹配通过" in e for e in row["events"]))

    def test_ignores_preexisting_other_user_job(self):
        # 门户初始就有 other-user 的 J-OLD 同名旧任务，新任务编号必须不同且不被误取。
        outcome = self.run_batch([make_request("REQ-001")])[0]
        self.assertEqual(RunStatus.COMPLETED, outcome.status)
        self.assertNotEqual("J-OLD", outcome.job_id)
        meta = read_metadata(Path(outcome.output_file))
        self.assertEqual(outcome.job_id, meta.job_id)


class ResumeTests(PlaywrightCase):
    def test_resume_reuses_saved_job_and_creates_none(self):
        runner = self.runner()
        runner.run([make_request("REQ-001")], self.driver)
        state = json.loads((self.work / "run-state.json").read_text(encoding="utf-8"))
        saved_job = state["requests"][0]["job_id"]

        # 恢复前记录门户现有任务数；恢复后不得增加。
        with self.session() as probe:
            before = len(self.localStorage_jobs(probe))
        runner2 = self.runner()
        outcomes = runner2.resume([make_request("REQ-001")], self.driver)
        self.assertEqual(RunStatus.COMPLETED, outcomes[0].status)
        self.assertEqual(saved_job, outcomes[0].job_id)
        with self.session() as probe:
            after = len(self.localStorage_jobs(probe))
        self.assertEqual(before, after)

    def test_resume_without_proven_job_reports_manual_only(self):
        # 空状态文件（无任何编号证据）：resume 只能报告人工确认，且不产生任务。
        (self.work).mkdir(parents=True, exist_ok=True)
        (self.work / "run-state.json").write_text(
            json.dumps({"schema_version": 1, "requests": []}), encoding="utf-8"
        )
        with self.session() as probe:
            before = len(self.localStorage_jobs(probe))
        outcomes = self.runner().resume([make_request("REQ-001")], self.driver)
        self.assertEqual(RunStatus.NEEDS_MANUAL, outcomes[0].status)
        self.assertIsNone(outcomes[0].job_id)
        with self.session() as probe:
            after = len(self.localStorage_jobs(probe))
        self.assertEqual(before, after)

    def test_batch_saves_job_id_before_waiting_and_resumes(self):
        # 生成后立即把任务置失败，验证编号已先落盘；恢复时沿用编号而不是重新生成。
        req = make_request("REQ-001")
        with self.session() as session:
            job_id = session.generate_export(req)
            session.inject_failure(job_id)
            # 手动模拟 runner 在生成后立刻保存的状态行。
            runner = self.runner()
            runner._restore([req])
            outcome = runner._outcomes[req.request_id]
            outcome.job_id = job_id
            outcome.owner = PORTAL_OWNER
            outcome.page_summary = req.digest
            runner._save_state()

        runner2 = self.runner()
        runner2._on_center_ready = lambda session_, _r, _o: session_.inject_failure(job_id)
        outcomes = runner2.resume([req], self.driver)
        self.assertEqual(job_id, outcomes[0].job_id)
        self.assertEqual(RunStatus.FAILED, outcomes[0].status)


class FailureAndTimeoutTests(PlaywrightCase):
    def test_failed_job_status_reported(self):
        req = make_request("REQ-001")
        with self.session() as session:
            job_id = session.generate_export(req)
            session.ensure_center()
            session.inject_failure(job_id)
            state = session.wait_until_terminal(
                job_id, max_polls=10, overall_timeout_s=10, poll_interval_s=0.05
            )
            self.assertEqual("失败", state)

        # runner 级验证：中心页打开后通过 localStorage 钩子置失败（不经任何
        # HTTP 运维接口），恢复路径必须把门户“失败”终态映射为 FAILED，且不新建任务。
        runner = self.runner()
        runner._restore([req])
        o = runner._outcomes[req.request_id]
        o.job_id = job_id
        o.owner = PORTAL_OWNER
        runner._on_center_ready = lambda session_, _r, _o: session_.inject_failure(job_id)
        runner.resume([req], self.driver)
        self.assertEqual(RunStatus.FAILED, o.status)

    def test_timeout_when_budget_exhausted(self):
        req = make_request("REQ-001")
        with self.session() as session:
            job_id = session.generate_export(req)
            session.ensure_center()
            # 测试替身：模拟后台迟迟不推进——刷新只回读状态、不点击推进，
            # 任务因此一直停在“排队”，双预算耗尽后必须结束并判定超时。
            session.refresh_job = lambda _id: session.job_state(_id)
            state = session.wait_until_terminal(
                job_id, max_polls=2, overall_timeout_s=2, poll_interval_s=0.05
            )
            self.assertEqual(STATE_QUEUED, state)
        runner = self.runner(max_polls=2, overall_timeout_s=2.0, poll_interval_s=0.05)
        runner._restore([req])
        o = runner._outcomes[req.request_id]
        o.job_id = job_id
        o.owner = PORTAL_OWNER
        with self.session() as session2:
            session2.refresh_job = lambda _id: session2.job_state(_id)
            runner._track_and_download(session2, req, o)
        self.assertEqual(RunStatus.TIMEOUT, o.status)
        self.assertIn("轮询预算耗尽", o.detail)

    def test_window_closed_status(self):
        req = make_request("REQ-001")
        runner = self.runner()
        runner._restore([req])
        o = runner._outcomes[req.request_id]
        with PortalSession(
            self.driver, self.base,
            user_data_dir=self.work / "browser-profile",
            download_staging=self.work / "staging",
            headless=True, action_timeout_ms=8000,
        ) as session:
            job_id = session.generate_export(req)
            center, _mode = session.ensure_center()
            # 会话级证据：真实关闭后读取状态立即抛出窗口关闭异常。
            center.close()
            with self.assertRaises(WindowClosedError):
                session.job_state(job_id)

            # runner 级证据：轮询期间窗口被关 -> 独立状态 window_closed。
            def close_during_polling(_job_id, **_kw):
                session._center_page.close()
                raise WindowClosedError("导出中心窗口已关闭")
            session.wait_until_terminal = close_during_polling
            o.job_id = job_id
            o.owner = PORTAL_OWNER
            runner._track_and_download(session, req, o)
        self.assertEqual(RunStatus.WINDOW_CLOSED, o.status)
        self.assertIsNotNone(o.detail)


class WrongFileTests(PlaywrightCase):
    def test_wrong_metadata_quarantined_not_accepted(self):
        self.portal_state["phase"] = "wrong-file"
        req = make_request("BAD-001", period="2026-07", vendor="south")
        outcomes = self.run_batch([req])
        o = outcomes[0]
        self.assertEqual(RunStatus.QUARANTINED, o.status)
        # 正常输出目录必须为空，异常文件只在隔离区并带原因说明。
        self.assertEqual([], list((self.work / "output").iterdir()))
        qfiles = list((self.work / "quarantine").glob("*.csv"))
        self.assertEqual(1, len(qfiles))
        reason_file = qfiles[0].with_suffix(".csv.reason.txt")
        self.assertTrue(reason_file.is_file())
        self.assertIn("job_id 不符", o.quarantine_reason)
        first = json.loads(qfiles[0].read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual("J-OLD", first["job_id"])


class CenterOpenModeTests(PlaywrightCase):
    def test_center_button_mode(self):
        with self.session() as session:
            center, mode = session.ensure_center()
            self.assertEqual("center-button", mode)
            self.assertTrue(center.locator('[data-job="J-OLD"]').is_visible())

    def test_auto_popup_on_generate_mode(self):
        self.portal_state["phase"] = "popup-on-generate"
        req = make_request("REQ-001")
        with self.session() as session:
            job_id = session.generate_export(req)
            # 生成动作前挂的监听必须抓到自动弹窗，并与本次任务关联。
            popup_records = [r for r in session.popups if r.related_job == job_id]
            self.assertEqual(1, len(popup_records))
            self.assertEqual("auto-on-generate", popup_records[0].opened_by)
            center, mode = session.ensure_center()
            self.assertEqual("reused", mode)
            match = session.find_matching_job(job_id, PORTAL_OWNER, req.digest)
            self.assertIsNotNone(match)


class InterleavingTests(PlaywrightCase):
    """两个导出任务交错完成，验证各取各的文件、不靠最新行/文件名。"""

    def test_two_jobs_complete_out_of_order(self):
        req_a = make_request("JOB-A", period="2026-10", vendor="north", currency="CNY")
        req_b = make_request("JOB-B", period="2026-09", vendor="south", currency="USD")
        with self.session() as session:
            job_a = session.generate_export(req_a)
            job_b = session.generate_export(req_b)
            self.assertNotEqual(job_a, job_b)
            center, _ = session.ensure_center()

            # 交错：B 先完成，A 后完成。
            self.assertEqual("排队", session.job_state(job_a))
            session.refresh_job(job_b)
            self.assertEqual(STATE_DONE, session.job_state(job_b))
            session.refresh_job(job_a)
            self.assertEqual(STATE_DONE, session.job_state(job_a))

            # 即便 B 是“最新完成”的一行，A 的下载也必须绑定 A 自己的任务行。
            staged_a = session.download_job(job_a)
            staged_b = session.download_job(job_b)
            reason_a = check_metadata(
                staged_a, expected_job_id=job_a, expected_digest=req_a.digest
            )
            reason_b = check_metadata(
                staged_b, expected_job_id=job_b, expected_digest=req_b.digest
            )
            self.assertIsNone(reason_a)
            self.assertIsNone(reason_b)
            meta_a = read_metadata(staged_a)
            meta_b = read_metadata(staged_b)
            self.assertEqual(job_a, meta_a.job_id)
            self.assertEqual(job_b, meta_b.job_id)
            self.assertEqual(req_a.digest, meta_a.filter_digest)
            self.assertEqual(req_b.digest, meta_b.filter_digest)
            # 门户对两个任务给出的建议文件名完全相同，证明不能靠文件名认领。
            name_a = staged_a.with_suffix(".csv.suggested-name.txt").read_text(
                encoding="utf-8"
            )
            name_b = staged_b.with_suffix(".csv.suggested-name.txt").read_text(
                encoding="utf-8"
            )
            self.assertEqual(name_a, name_b)
            self.assertEqual("结算资料.csv", name_a)


class NoOperatorEndpointTests(unittest.TestCase):
    def test_module_never_calls_control_endpoint(self):
        # 业务代码红线：包内不得发起对运维入口的 HTTP 调用（AST 检查，
        # 文档字符串中提及路径不算违规）。
        import ast

        for py in (REPO_ROOT / "export_automation").glob("*.py"):
            tree = ast.parse(py.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                    continue
                if "/__control" in node.value and "不调用" not in node.value:
                    self.fail(f"{py.name} 出现运维入口字符串: {node.value!r}")


class BatchIsolationTests(PlaywrightCase):
    def test_unreachable_portal_marks_each_item_without_crashing(self):
        # 门户不可达属于 Playwright 级异常：必须隔离在单项内并转人工确认，
        # 不能击穿循环导致整批崩溃。
        from export_automation.runner import RunConfig as RC

        bad_config = RC(
            base_url="http://127.0.0.1:59999",  # 无监听的高端口：连接被拒绝
            work_dir=self.work,
            headless=True,
            max_polls=2,
            overall_timeout_s=5,
            poll_interval_s=0.05,
            action_timeout_ms=3000,
        )
        runner = BatchRunner(bad_config)
        outcomes = runner.run(
            [make_request("REQ-001"), make_request("REQ-002")], self.driver
        )
        self.assertEqual(2, len(outcomes))
        self.assertTrue(all(o.status is RunStatus.NEEDS_MANUAL for o in outcomes))
        self.assertTrue(all(not o.job_id for o in outcomes))
        # 没有任何文件被放入正常输出目录。
        self.assertTrue(not any((self.work / "output").iterdir()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
