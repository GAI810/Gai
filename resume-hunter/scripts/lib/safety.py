"""二期风控公共能力：平台锁、限额、拒信词、熔断。"""

from __future__ import annotations

import fcntl
import logging
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from .config import DATA_DIR, load_config

logger = logging.getLogger("resume-hunter")

LOCKS_DIR = DATA_DIR / "locks"
DEFAULT_HARD_CAP = 50

# 拒信/明确婉拒关键词（命中则跳过自动回复与求简历）
DEFAULT_REJECT_KEYWORDS = [
    "不感兴趣",
    "先不考虑",
    "不考虑",
    "已找到",
    "找到工作",
    "已经找到",
    "有工作了",
    "勿扰",
    "别再联系",
    "请勿打扰",
    "谢谢不了",
    "不用了",
    "暂不考虑",
    "机会不合适",
    "不合适",
    "已入职",
    "已入职其他",
    "已有 offer",
    "已有offer",
]

# 页面风控信号
RISK_PAGE_KEYWORDS = [
    "操作频繁",
    "访问过于频繁",
    "请稍后再试",
    "账号存在异常",
    "安全验证",
    "滑动验证",
    "请完成验证",
    "verify",
    "人机验证",
    "限制",
]


class PlatformLockError(RuntimeError):
    """同平台已有进程占用锁"""


@contextmanager
def platform_lock(platform: str, timeout: float = 0.0) -> Iterator[None]:
    """data/locks/<platform>.lock 独占锁；拿不到则抛 PlatformLockError。

    timeout=0 表示非阻塞立即失败（search/check 默认）。
    """
    LOCKS_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = LOCKS_DIR / f"{platform}.lock"
    fh = open(lock_path, "a+")
    try:
        if timeout <= 0:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as e:
                raise PlatformLockError(
                    f"平台 {platform} 已有 search/check 进程在跑（锁: {lock_path}），请稍后再试"
                ) from e
        else:
            import time

            deadline = time.time() + timeout
            while True:
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.time() > deadline:
                        raise PlatformLockError(
                            f"平台 {platform} 锁等待超时（锁: {lock_path}）"
                        )
                    time.sleep(0.05)
        yield
    finally:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()


def get_hard_cap(platform_config: Optional[dict] = None, config: Optional[dict] = None) -> int:
    cfg = config or load_config()
    pc = platform_config or {}
    hard = pc.get("daily_hard_cap", cfg.get("daily_hard_cap", DEFAULT_HARD_CAP))
    try:
        hard = int(hard)
    except (TypeError, ValueError):
        hard = DEFAULT_HARD_CAP
    return max(1, hard)


def resolve_effective_limit(
    cli_limit: int,
    platform_config: dict,
    *,
    i_know_the_risk: bool = False,
    config: Optional[dict] = None,
) -> int:
    """CLI limit 与 daily_limit / daily_hard_cap 取 min；超硬顶需确认。"""
    hard = get_hard_cap(platform_config, config)
    daily = int(platform_config.get("daily_limit", hard) or hard)
    daily = min(daily, hard)
    requested = int(cli_limit)
    if requested > hard:
        if not i_know_the_risk:
            raise ValueError(
                f"--limit {requested} 超过 daily_hard_cap={hard}。"
                f" 如确需更高请加 --i-know-the-risk（仍会被硬顶压到 {hard}）"
            )
        logger.warning(f"--limit {requested} > hard_cap {hard}，已压到硬顶")
        requested = hard
    return max(0, min(requested, daily, hard))


def is_reject_message(text: str, keywords: Optional[list[str]] = None) -> bool:
    if not text:
        return False
    kws = keywords if keywords is not None else DEFAULT_REJECT_KEYWORDS
    lowered = text.lower()
    for kw in kws:
        if kw.lower() in lowered or kw in text:
            return True
    return False


def page_shows_risk(page) -> bool:
    """粗检页面是否出现风控/验证文案。"""
    try:
        body = page.content() if hasattr(page, "content") else ""
    except Exception:
        try:
            body = page.inner_text("body")
        except Exception:
            return False
    if not body:
        return False
    lower = body.lower()
    for kw in RISK_PAGE_KEYWORDS:
        if kw.lower() in lower or kw in body:
            return True
    return False


class CircuitBreaker:
    """连续外发失败熔断。"""

    def __init__(self, max_failures: int = 3, screenshot_dir: Optional[Path] = None):
        self.max_failures = max_failures
        self.consecutive_failures = 0
        self.tripped = False
        self.last_screenshot: Optional[str] = None
        self.screenshot_dir = screenshot_dir or (DATA_DIR / "logs" / "circuit")

    def record_success(self) -> None:
        self.consecutive_failures = 0

    def record_failure(self, page=None, reason: str = "") -> bool:
        """记录失败；若已熔断返回 True。"""
        self.consecutive_failures += 1
        logger.warning(
            f"外发失败 ({self.consecutive_failures}/{self.max_failures})"
            + (f": {reason}" if reason else "")
        )
        if self.consecutive_failures >= self.max_failures or (page and page_shows_risk(page)):
            self.tripped = True
            if page is not None:
                self.last_screenshot = self._save_screenshot(page)
            logger.error(
                f"风控熔断：连续失败或页面出现限制/验证。"
                f" screenshot={self.last_screenshot or 'N/A'}"
            )
            return True
        return False

    def _save_screenshot(self, page) -> Optional[str]:
        try:
            self.screenshot_dir.mkdir(parents=True, exist_ok=True)
            from datetime import datetime

            path = self.screenshot_dir / f"circuit_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
            page.screenshot(path=str(path), full_page=True)
            os.chmod(path, 0o600)
            return str(path)
        except Exception as e:
            logger.debug(f"截图失败: {e}")
            return None


def assert_maimai_outbound_allowed(platform_config: dict, *, scan_only: bool = False, dry_run: bool = False) -> None:
    """脉脉未 verified 时禁止外发；scan-only/dry-run 可读。"""
    if platform_config.get("verified", False):
        return
    if scan_only or dry_run:
        logger.warning("脉脉尚未 verified，仅允许 scan-only/dry-run，禁止外发")
        return
    raise RuntimeError(
        "脉脉平台未验证（platforms.maimai.verified=false），禁止外发。"
        " 请先人工验证选择器与流程后，在 data/config.yaml 将 verified 设为 true；"
        " 或使用 --scan-only / --dry-run。"
    )


def assert_headed_for_outbound(*, headless: bool, scan_only: bool) -> None:
    if headless and not scan_only:
        raise RuntimeError("无头模式禁止非 scan-only 外发；请使用 headed 浏览器或加 --scan-only")
