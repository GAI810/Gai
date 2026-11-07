"""二期风控单测（无浏览器外发；可直接 python -m 或 unittest 跑）"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

# 保证可从 scripts/ 或 lib/ 运行
import sys
from unittest import mock as _pre_mock

SCRIPTS = Path(__file__).resolve().parent.parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

# playwright 为 optional browser 依赖；单测不装也可跑
if "playwright" not in sys.modules:
    _pw = _pre_mock.MagicMock()
    sys.modules["playwright"] = _pw
    sys.modules["playwright.sync_api"] = _pw.sync_api

from lib.browser import BrowserManager, DEFAULT_UA, DEFAULT_VIEWPORT  # noqa: E402
from lib.config import BROWSER_STATE_DIR  # noqa: E402
from lib.db import (  # noqa: E402
    STATUS_ANALYZED,
    STATUS_GREETED,
    STATUS_REPLIED,
    STATUS_RESUME_DOWNLOADED,
    STATUS_RESUME_REQUESTED,
    STATUS_SEARCHED,
    Database,
)
from lib.platforms.boss import BossPlatform  # noqa: E402
from lib.platforms.maimai import MaimaiPlatform  # noqa: E402
from lib.rate_limiter import DailyCounter  # noqa: E402
from lib.safety import (  # noqa: E402
    CircuitBreaker,
    PlatformLockError,
    assert_maimai_outbound_allowed,
    is_reject_message,
    platform_lock,
    resolve_effective_limit,
)


class TmpDBTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "t.sqlite"
        self.db = Database(db_path=self.db_path)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()


class TestDailyQuota(TmpDBTest):
    def test_hydrate_across_processes(self):
        c1 = DailyCounter(daily_limit=50, db=self.db, platform="boss")
        self.assertEqual(c1.count, 0)
        for _ in range(3):
            c1.increment()
        self.assertEqual(c1.count, 3)
        self.assertEqual(self.db.get_outbound_count("boss"), 3)

        # 模拟新进程：新 counter hydrate
        c2 = DailyCounter(daily_limit=50, db=self.db, platform="boss")
        self.assertEqual(c2.count, 3)
        self.assertFalse(DailyCounter(daily_limit=3, db=self.db, platform="boss").can_proceed())

    def test_hard_cap_min_with_limit(self):
        pc = {"daily_limit": 50, "daily_hard_cap": 50}
        self.assertEqual(resolve_effective_limit(20, pc), 20)
        self.assertEqual(resolve_effective_limit(80, pc, i_know_the_risk=True), 50)
        with self.assertRaises(ValueError):
            resolve_effective_limit(80, pc, i_know_the_risk=False)


class TestBossTokens(unittest.TestCase):
    def test_unknown_is_not_enough(self):
        p = BossPlatform(daily_limit=10)
        self.assertIsNone(p._token_balance)
        self.assertFalse(p.has_enough_tokens())
        p._token_balance = -1
        self.assertFalse(p.has_enough_tokens())
        p._token_balance = 0
        self.assertFalse(p.has_enough_tokens())
        p._token_balance = 5
        self.assertTrue(p.has_enough_tokens())
        p._token_balance -= 1
        self.assertEqual(p._token_balance, 4)


class TestStatusForwardOnly(TmpDBTest):
    def test_no_regress_and_reply_only_after_send(self):
        cid = self.db.add_candidate("boss", "p1", "张三")
        self.db.mark_greeted(cid)
        self.db.mark_resume_downloaded(cid, "/tmp/a.pdf")
        # 禁止回退成 replied
        self.db.mark_replied(cid)
        row = self.db.get_candidate(cid)
        self.assertEqual(row["status"], STATUS_RESUME_DOWNLOADED)

        # friend msg 不把更高状态打回，也不改 status
        self.db.mark_friend_messaged(cid)
        row = self.db.get_candidate(cid)
        self.assertEqual(row["status"], STATUS_RESUME_DOWNLOADED)
        self.assertIsNotNone(row.get("friend_messaged_at"))

    def test_friend_messaged_not_we_replied(self):
        """对方有消息 ≠ 我方已回复；send 成功才 mark_replied。"""
        cid = self.db.add_candidate("boss", "p1b", "王五")
        self.db.mark_greeted(cid)
        self.db.mark_friend_messaged(cid)
        row = self.db.get_candidate(cid)
        self.assertEqual(row["status"], STATUS_GREETED)
        self.assertNotEqual(row["status"], STATUS_REPLIED)
        self.assertIsNotNone(row["friend_messaged_at"])
        self.assertTrue(self.db.has_friend_messaged(cid))
        # 不应写入 replied_at
        self.assertIsNone(row.get("replied_at"))

        # 幂等：再 mark 不改 status
        prev = row["friend_messaged_at"]
        self.db.mark_friend_messaged(cid)
        row = self.db.get_candidate(cid)
        self.assertEqual(row["status"], STATUS_GREETED)
        self.assertEqual(row["friend_messaged_at"], prev)

        # 仅 send 成功语义：mark_replied 才升到 replied
        self.db.mark_replied(cid)
        row = self.db.get_candidate(cid)
        self.assertEqual(row["status"], STATUS_REPLIED)
        self.assertIsNotNone(row["replied_at"])

    def test_resume_request_idempotent(self):
        cid = self.db.add_candidate("boss", "p2", "李四")
        self.assertTrue(self.db.mark_resume_requested(cid))
        self.assertTrue(self.db.has_resume_requested(cid))
        self.assertFalse(self.db.mark_resume_requested(cid))
        row = self.db.get_candidate(cid)
        self.assertEqual(row["status"], STATUS_RESUME_REQUESTED)


class TestMaimaiGate(unittest.TestCase):
    def test_init_accepts_daily_limit(self):
        p = MaimaiPlatform(daily_limit=20)
        self.assertEqual(p.daily_counter.daily_limit, 20)

    def test_unverified_blocks_outbound(self):
        cfg = {"enabled": False, "verified": False}
        with self.assertRaises(RuntimeError):
            assert_maimai_outbound_allowed(cfg, scan_only=False, dry_run=False)
        # scan/dry 允许
        assert_maimai_outbound_allowed(cfg, scan_only=True, dry_run=False)
        assert_maimai_outbound_allowed(cfg, scan_only=False, dry_run=True)


class TestSessionPerms(unittest.TestCase):
    def test_save_session_mode_600(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            with mock.patch("lib.browser.BROWSER_STATE_DIR", td_path):
                bm = BrowserManager()
                # 伪造 context.storage_state
                class FakeCtx:
                    def storage_state(self):
                        return {"cookies": [], "origins": []}

                bm._contexts["boss"] = FakeCtx()
                bm.save_session("boss")
                state = td_path / "boss" / "state.json"
                self.assertTrue(state.exists())
                mode = stat.S_IMODE(state.stat().st_mode)
                self.assertEqual(mode, 0o600)
                dir_mode = stat.S_IMODE(state.parent.stat().st_mode)
                self.assertEqual(dir_mode, 0o700)


class TestRejectAndBreaker(unittest.TestCase):
    def test_reject_keywords(self):
        self.assertTrue(is_reject_message("谢谢，暂不考虑这个机会"))
        self.assertTrue(is_reject_message("已找到工作了"))
        self.assertFalse(is_reject_message("您好，方便聊聊吗"))

    def test_circuit_breaker(self):
        b = CircuitBreaker(max_failures=3)
        self.assertFalse(b.record_failure(None, "a"))
        self.assertFalse(b.record_failure(None, "b"))
        self.assertTrue(b.record_failure(None, "c"))
        self.assertTrue(b.tripped)
        b.record_success()
        # success 清零，但 tripped 仍保持（已熔断）——按实现：record_success 只清计数
        self.assertEqual(b.consecutive_failures, 0)


class TestFingerprint(unittest.TestCase):
    def test_fingerprint_stable(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            with mock.patch("lib.browser.BROWSER_STATE_DIR", td_path):
                bm1 = BrowserManager()
                fp1 = bm1.load_or_create_fingerprint("boss")
                bm2 = BrowserManager()
                fp2 = bm2.load_or_create_fingerprint("boss")
                self.assertEqual(fp1["user_agent"], fp2["user_agent"])
                self.assertEqual(fp1["viewport"], fp2["viewport"])
                self.assertEqual(fp1["user_agent"], DEFAULT_UA)
                self.assertEqual(fp1["viewport"], DEFAULT_VIEWPORT)
                mode = stat.S_IMODE((td_path / "boss" / "fingerprint.json").stat().st_mode)
                self.assertEqual(mode, 0o600)


class TestPlatformLock(unittest.TestCase):
    def test_second_process_blocked(self):
        with tempfile.TemporaryDirectory() as td:
            from lib import safety

            with mock.patch.object(safety, "LOCKS_DIR", Path(td)):
                held = threading.Event()
                release = threading.Event()
                err = []

                def holder():
                    with platform_lock("boss"):
                        held.set()
                        release.wait(5)

                t = threading.Thread(target=holder)
                t.start()
                self.assertTrue(held.wait(2))
                try:
                    with platform_lock("boss"):
                        err.append("should not acquire")
                except PlatformLockError:
                    err.append("blocked")
                release.set()
                t.join(timeout=3)
                self.assertEqual(err, ["blocked"])


class TestCheckDryRunSemantics(unittest.TestCase):
    """check --dry-run 在 CLI 层跳过 Database；此处用静态标志保证逻辑存在。"""

    def test_check_source_has_dry_run_early_continue(self):
        src = (SCRIPTS / "check.py").read_text(encoding="utf-8")
        self.assertIn("零写库", src)
        self.assertIn("if args.dry_run:", src)
        self.assertIn("db = None if args.dry_run else Database()", src)
        # 不再默认 all
        self.assertIn('default="boss"', src)
        # 裸 no-limit 需确认
        self.assertIn("已移除裸 --no-limit", src)


class TestBossScanOnlyTokenGate(unittest.TestCase):
    """scan-only 未传 tokens 时，搜索循环不得因 token=None 立即 break。"""

    def test_search_enters_loop_when_token_unknown(self):
        p = BossPlatform(daily_limit=50)
        self.assertIsNone(p._token_balance)
        self.assertFalse(p.has_enough_tokens())  # 外发路径仍拒绝

        calls = []

        def fake_nav(page):
            calls.append("nav")

        def fake_one(page, keyword, filters, limit):
            calls.append(("search", keyword, limit))
            return []

        p._navigate_to_search = fake_nav  # type: ignore
        p._search_one_keyword = fake_one  # type: ignore
        result = p.search_candidates(None, ["x"], max_results=5)
        self.assertEqual(calls[0], "nav")
        self.assertEqual(calls[1], ("search", "x", 5))
        self.assertEqual(result, [])

    def test_greet_path_still_requires_tokens(self):
        """外发/打招呼仍要求 tokens>0（search.py 侧 + has_enough_tokens）。"""
        p = BossPlatform(daily_limit=10)
        p._token_balance = None
        self.assertFalse(p.has_enough_tokens())
        p._token_balance = 0
        self.assertFalse(p.has_enough_tokens())
        src = (SCRIPTS / "search.py").read_text(encoding="utf-8")
        self.assertIn("has_enough_tokens()", src)
        self.assertIn("非 scan-only 拒绝", src)


class TestSearchTokenCli(unittest.TestCase):

    def test_search_defaults(self):
        src = (SCRIPTS / "search.py").read_text(encoding="utf-8")
        self.assertIn("default=20", src)
        self.assertIn("default=None", src)  # tokens
        self.assertIn("未能解析畅聊卡余额", src)
        self.assertIn("--tokens 0", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
