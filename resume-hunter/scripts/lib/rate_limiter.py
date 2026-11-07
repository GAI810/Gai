"""速率控制模块 — 滑动窗口 + 持久化日配额"""

from __future__ import annotations

import logging
import time
from collections import deque
from datetime import datetime
from typing import Optional, Protocol

logger = logging.getLogger("resume-hunter")


class _DailyStatsDB(Protocol):
    def get_outbound_count(self, platform: str, date: Optional[str] = None) -> int: ...
    def increment_outbound(self, platform: str) -> int: ...


class RateLimiter:
    """基于滑动窗口的速率限制器"""

    def __init__(self, max_per_hour: int = 60, min_interval: float = 30.0, max_interval: float = 90.0):
        self.max_per_hour = max_per_hour
        self.min_interval = min_interval
        self.max_interval = max_interval
        self.timestamps: deque[float] = deque()

    def wait(self) -> None:
        """等待直到可以执行下一个操作"""
        now = time.time()

        while self.timestamps and now - self.timestamps[0] > 3600:
            self.timestamps.popleft()

        if len(self.timestamps) >= self.max_per_hour:
            wait_until = self.timestamps[0] + 3600
            wait_time = wait_until - now
            if wait_time > 0:
                logger.warning(f"小时速率上限，等待 {wait_time:.0f}s")
                time.sleep(wait_time)

        if self.timestamps:
            elapsed = time.time() - self.timestamps[-1]
            import random
            required = random.uniform(self.min_interval, self.max_interval)
            if elapsed < required:
                wait_time = required - elapsed
                logger.debug(f"操作间隔等待 {wait_time:.1f}s")
                time.sleep(wait_time)

        self.timestamps.append(time.time())

    @property
    def count_this_hour(self) -> int:
        now = time.time()
        while self.timestamps and now - self.timestamps[0] > 3600:
            self.timestamps.popleft()
        return len(self.timestamps)


class DailyCounter:
    """每日外发计数器（可从 SQLite daily_stats  hydrate，跨进程生效）"""

    def __init__(
        self,
        daily_limit: int = 50,
        db: Optional[_DailyStatsDB] = None,
        platform: str = "",
    ):
        self.daily_limit = max(0, int(daily_limit))
        self._db = db
        self._platform = platform
        self._date: str = ""
        self._count: int = 0
        self._hydrate()

    def _hydrate(self) -> None:
        today = datetime.now().strftime("%Y-%m-%d")
        self._date = today
        if self._db is not None and self._platform:
            try:
                self._count = int(self._db.get_outbound_count(self._platform, today))
                logger.info(
                    f"[{self._platform}] 日配额 hydrate: {self._count}/{self.daily_limit}"
                )
                return
            except Exception as e:
                logger.warning(f"日配额 hydrate 失败，回退内存计数: {e}")
        self._count = 0

    def _ensure_today(self) -> None:
        today = datetime.now().strftime("%Y-%m-%d")
        if self._date != today:
            self._hydrate()

    def increment(self) -> None:
        """动作成功后调用：内存 +1，并原子写入 DB outbound。"""
        self._ensure_today()
        if self._db is not None and self._platform:
            try:
                self._count = int(self._db.increment_outbound(self._platform))
                return
            except Exception as e:
                logger.warning(f"日配额 DB 自增失败，仅更新内存: {e}")
        self._count += 1

    def can_proceed(self) -> bool:
        self._ensure_today()
        return self._count < self.daily_limit

    @property
    def remaining(self) -> int:
        self._ensure_today()
        return max(0, self.daily_limit - self._count)

    @property
    def count(self) -> int:
        self._ensure_today()
        return self._count
