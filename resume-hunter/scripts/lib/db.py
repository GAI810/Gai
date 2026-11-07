"""SQLite 候选人状态追踪数据库"""

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

from .config import DB_PATH

# 候选人状态常量
STATUS_SEARCHED = "searched"
STATUS_GREETED = "greeted"
STATUS_REPLIED = "replied"
STATUS_RESUME_REQUESTED = "resume_requested"
STATUS_RESUME_DOWNLOADED = "resume_downloaded"
STATUS_ANALYZED = "analyzed"

# 状态只允许前进，禁止回退（如 resume_downloaded → replied）
STATUS_ORDER = {
    STATUS_SEARCHED: 0,
    STATUS_GREETED: 1,
    STATUS_REPLIED: 2,
    STATUS_RESUME_REQUESTED: 3,
    STATUS_RESUME_DOWNLOADED: 4,
    STATUS_ANALYZED: 5,
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    platform_id TEXT,
    name TEXT,
    title TEXT,
    company TEXT,
    experience TEXT,
    profile_url TEXT,
    search_keyword TEXT,
    status TEXT NOT NULL DEFAULT 'searched',
    greeted_at TIMESTAMP,
    replied_at TIMESTAMP,
    resume_path TEXT,
    analysis_json TEXT,
    match_score REAL,
    tags TEXT,
    notes TEXT,
    job_name TEXT,
    feishu_record_id TEXT,
    feishu_synced_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS search_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    keyword TEXT NOT NULL,
    results_count INTEGER DEFAULT 0,
    greeted_count INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS daily_stats (
    date TEXT PRIMARY KEY,
    boss_searched INTEGER DEFAULT 0,
    boss_greeted INTEGER DEFAULT 0,
    boss_replied INTEGER DEFAULT 0,
    boss_resumes INTEGER DEFAULT 0,
    boss_outbound INTEGER DEFAULT 0,
    maimai_searched INTEGER DEFAULT 0,
    maimai_greeted INTEGER DEFAULT 0,
    maimai_replied INTEGER DEFAULT 0,
    maimai_resumes INTEGER DEFAULT 0,
    maimai_outbound INTEGER DEFAULT 0
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_candidate_platform
    ON candidates(platform, platform_id);
"""


class Database:
    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or DB_PATH
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        # 增量迁移：为旧数据库添加 job_name 列
        try:
            self.conn.execute("ALTER TABLE candidates ADD COLUMN job_name TEXT")
            self.conn.commit()
        except sqlite3.OperationalError:
            pass  # 列已存在

        # 增量迁移：添加飞书同步字段
        try:
            self.conn.execute("ALTER TABLE candidates ADD COLUMN feishu_record_id TEXT")
            self.conn.execute("ALTER TABLE candidates ADD COLUMN feishu_synced_at TIMESTAMP")
            self.conn.commit()
        except sqlite3.OperationalError:
            pass  # 字段已存在

        # 增量迁移：添加飞书招聘人才库导入字段
        try:
            self.conn.execute("ALTER TABLE candidates ADD COLUMN hire_talent_id TEXT")
            self.conn.execute("ALTER TABLE candidates ADD COLUMN hire_imported_at TIMESTAMP")
            self.conn.commit()
        except sqlite3.OperationalError:
            pass  # 字段已存在

        # 增量迁移：日配额 outbound 列
        for col in ("boss_outbound", "maimai_outbound"):
            try:
                self.conn.execute(f"ALTER TABLE daily_stats ADD COLUMN {col} INTEGER DEFAULT 0")
                self.conn.commit()
            except sqlite3.OperationalError:
                pass

        # 增量迁移：求简历幂等标记
        try:
            self.conn.execute("ALTER TABLE candidates ADD COLUMN resume_requested_at TIMESTAMP")
            self.conn.commit()
        except sqlite3.OperationalError:
            pass

        # 增量迁移：对方有消息（独立于我方 replied）
        try:
            self.conn.execute("ALTER TABLE candidates ADD COLUMN friend_messaged_at TIMESTAMP")
            self.conn.commit()
        except sqlite3.OperationalError:
            pass

    def close(self) -> None:
        self.conn.close()

    # ---- 候选人操作 ----

    def add_candidate(self, platform: str, platform_id: str, name: str,
                      title: str = "", company: str = "", experience: str = "",
                      profile_url: str = "", search_keyword: str = "") -> Optional[int]:
        """添加候选人，如果已存在则跳过"""
        try:
            cursor = self.conn.execute(
                """INSERT INTO candidates
                   (platform, platform_id, name, title, company, experience, profile_url, search_keyword)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (platform, platform_id, name, title, company, experience, profile_url, search_keyword),
            )
            self.conn.commit()
            return cursor.lastrowid
        except sqlite3.IntegrityError:
            # 已存在
            return None

    def update_status(self, candidate_id: int, status: str, **kwargs) -> None:
        """更新候选人状态；状态只前进，禁止回退。kwargs 仍会写入。"""
        row = self.conn.execute(
            "SELECT status FROM candidates WHERE id = ?", (candidate_id,)
        ).fetchone()
        current = row["status"] if row else None
        effective_status = status
        if current is not None:
            cur_rank = STATUS_ORDER.get(current, -1)
            new_rank = STATUS_ORDER.get(status, -1)
            if new_rank < cur_rank:
                # 禁止回退：保留更高状态，仍可更新其它字段
                effective_status = current

        sets = ["status = ?", "updated_at = ?"]
        values = [effective_status, datetime.now().isoformat()]

        for key, value in kwargs.items():
            sets.append(f"{key} = ?")
            values.append(value)

        values.append(candidate_id)
        self.conn.execute(
            f"UPDATE candidates SET {', '.join(sets)} WHERE id = ?",
            values,
        )
        self.conn.commit()

    def mark_greeted(self, candidate_id: int) -> None:
        self.update_status(candidate_id, STATUS_GREETED, greeted_at=datetime.now().isoformat())

    def mark_friend_messaged(self, candidate_id: int) -> None:
        """记录对方有消息：只写 friend_messaged_at，绝不写成 replied。

        对方有消息 ≠ 我方已回复。状态保持不变（不前进到 STATUS_REPLIED）；
        更高状态也不回退。mark_replied 仅在我方 send_reply 成功后调用。
        """
        row = self.get_candidate(candidate_id)
        if not row:
            return
        now = datetime.now().isoformat()
        if row.get("friend_messaged_at"):
            self.conn.execute(
                "UPDATE candidates SET updated_at = ? WHERE id = ?",
                (now, candidate_id),
            )
        else:
            self.conn.execute(
                "UPDATE candidates SET friend_messaged_at = ?, updated_at = ? WHERE id = ?",
                (now, now, candidate_id),
            )
        self.conn.commit()

    def has_friend_messaged(self, candidate_id: int) -> bool:
        row = self.get_candidate(candidate_id)
        return bool(row and row.get("friend_messaged_at"))

    def mark_replied(self, candidate_id: int) -> None:
        """标记我方已回复（仅 send_reply 成功后调用）。"""
        self.update_status(candidate_id, STATUS_REPLIED, replied_at=datetime.now().isoformat())

    def mark_resume_requested(self, candidate_id: int) -> bool:
        """求简历幂等：已求过则返回 False，否则标记并返回 True。"""
        row = self.get_candidate(candidate_id)
        if not row:
            return False
        if row.get("resume_requested_at") or row.get("status") in (
            STATUS_RESUME_REQUESTED,
            STATUS_RESUME_DOWNLOADED,
            STATUS_ANALYZED,
        ):
            return False
        self.update_status(
            candidate_id,
            STATUS_RESUME_REQUESTED,
            resume_requested_at=datetime.now().isoformat(),
        )
        return True

    def has_resume_requested(self, candidate_id: int) -> bool:
        row = self.get_candidate(candidate_id)
        if not row:
            return False
        if row.get("resume_requested_at"):
            return True
        return row.get("status") in (
            STATUS_RESUME_REQUESTED,
            STATUS_RESUME_DOWNLOADED,
            STATUS_ANALYZED,
        )

    def mark_resume_downloaded(self, candidate_id: int, resume_path: str) -> None:
        self.update_status(candidate_id, STATUS_RESUME_DOWNLOADED, resume_path=resume_path)

    def mark_analyzed(self, candidate_id: int, analysis: dict, match_score: float, tags: list[str]) -> dict:
        """标记候选人为已分析，返回候选人完整信息"""
        self.update_status(
            candidate_id, STATUS_ANALYZED,
            analysis_json=json.dumps(analysis, ensure_ascii=False),
            match_score=match_score,
            tags=",".join(tags),
        )
        # 返回候选人完整信息供飞书同步使用
        return self.get_candidate(candidate_id) or {}

    def get_candidate(self, candidate_id: int) -> Optional[dict]:
        row = self.conn.execute("SELECT * FROM candidates WHERE id = ?", (candidate_id,)).fetchone()
        return dict(row) if row else None

    def get_candidate_by_platform_id(self, platform: str, platform_id: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM candidates WHERE platform = ? AND platform_id = ?",
            (platform, platform_id),
        ).fetchone()
        return dict(row) if row else None

    def get_candidates_by_status(self, platform: str, status: str, limit: int = 500) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM candidates WHERE platform = ? AND status = ? ORDER BY created_at DESC LIMIT ?",
            (platform, status, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_greeted_candidates(self, platform: str, days: int = 7) -> list[dict]:
        """获取最近 N 天内已打招呼但未回复的候选人"""
        rows = self.conn.execute(
            """SELECT * FROM candidates
               WHERE platform = ? AND status = 'greeted'
               AND greeted_at >= datetime('now', ?)
               ORDER BY greeted_at DESC""",
            (platform, f"-{days} days"),
        ).fetchall()
        return [dict(r) for r in rows]

    def candidate_exists(self, platform: str, platform_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM candidates WHERE platform = ? AND platform_id = ?",
            (platform, platform_id),
        ).fetchone()
        return row is not None

    # ---- 搜索日志 ----

    def log_search(self, platform: str, keyword: str, results_count: int, greeted_count: int) -> None:
        self.conn.execute(
            "INSERT INTO search_logs (platform, keyword, results_count, greeted_count) VALUES (?, ?, ?, ?)",
            (platform, keyword, results_count, greeted_count),
        )
        self.conn.commit()

    # ---- 每日统计 ----

    def get_today_stats(self) -> dict:
        today = datetime.now().strftime("%Y-%m-%d")
        row = self.conn.execute("SELECT * FROM daily_stats WHERE date = ?", (today,)).fetchone()
        if row:
            return dict(row)
        # 从 candidates 表实时计算
        stats = {"date": today}
        for platform in ("boss", "maimai"):
            for status, col_suffix in [
                (STATUS_SEARCHED, "searched"),
                (STATUS_GREETED, "greeted"),
                (STATUS_REPLIED, "replied"),
                (STATUS_RESUME_DOWNLOADED, "resumes"),
            ]:
                count = self.conn.execute(
                    """SELECT COUNT(*) FROM candidates
                       WHERE platform = ? AND status >= ? AND date(created_at) = ?""",
                    (platform, status, today),
                ).fetchone()[0]
                stats[f"{platform}_{col_suffix}"] = count
        return stats

    def increment_daily_stat(self, platform: str, field: str) -> None:
        """幂等友好的日统计自增（同日多次调用会累加；调用方需自行避免重复计次）。"""
        today = datetime.now().strftime("%Y-%m-%d")
        col = f"{platform}_{field}"
        allowed = {
            "boss_searched", "boss_greeted", "boss_replied", "boss_resumes", "boss_outbound",
            "maimai_searched", "maimai_greeted", "maimai_replied", "maimai_resumes", "maimai_outbound",
        }
        if col not in allowed:
            raise ValueError(f"非法日统计字段: {col}")
        self.conn.execute(
            f"""INSERT INTO daily_stats (date, {col}) VALUES (?, 1)
                ON CONFLICT(date) DO UPDATE SET {col} = COALESCE({col}, 0) + 1""",
            (today,),
        )
        self.conn.commit()

    def get_outbound_count(self, platform: str, date: str = None) -> int:
        """读取平台当日 outbound 计数（search/check 共用闸门）。"""
        day = date or datetime.now().strftime("%Y-%m-%d")
        col = f"{platform}_outbound"
        row = self.conn.execute(
            f"SELECT {col} FROM daily_stats WHERE date = ?", (day,)
        ).fetchone()
        if not row or row[0] is None:
            return 0
        return int(row[0])

    def increment_outbound(self, platform: str) -> int:
        """原子自增 outbound，返回自增后的值。"""
        today = datetime.now().strftime("%Y-%m-%d")
        col = f"{platform}_outbound"
        if col not in ("boss_outbound", "maimai_outbound"):
            raise ValueError(f"非法平台: {platform}")
        self.conn.execute(
            f"""INSERT INTO daily_stats (date, {col}) VALUES (?, 1)
                ON CONFLICT(date) DO UPDATE SET {col} = COALESCE({col}, 0) + 1""",
            (today,),
        )
        self.conn.commit()
        return self.get_outbound_count(platform, today)

    # ---- 飞书同步 ----

    def update_feishu_sync_status(self, candidate_id: int, record_id: str) -> None:
        """更新候选人的飞书同步状态"""
        self.conn.execute(
            """UPDATE candidates
               SET feishu_record_id = ?, feishu_synced_at = ?
               WHERE id = ?""",
            (record_id, datetime.now(), candidate_id)
        )
        self.conn.commit()

    def get_unsynced_candidates(self, platform: str = None) -> list:
        """获取未同步到飞书的已分析候选人"""
        query = """SELECT * FROM candidates
                   WHERE status = ? AND feishu_record_id IS NULL"""
        params = [STATUS_ANALYZED]

        if platform:
            query += " AND platform = ?"
            params.append(platform)

        cursor = self.conn.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]

    # ---- 飞书招聘人才库导入 ----

    def get_pending_hire_candidates(self, platform: str = None, min_score: float = None,
                                    limit: int = None) -> list[dict]:
        """获取已分析、有简历文件、尚未导入飞书招聘人才库的候选人"""
        query = """SELECT * FROM candidates
                   WHERE status = ? AND resume_path IS NOT NULL AND hire_talent_id IS NULL"""
        params = [STATUS_ANALYZED]

        if platform:
            query += " AND platform = ?"
            params.append(platform)

        if min_score is not None:
            query += " AND match_score >= ?"
            params.append(min_score)

        query += " ORDER BY match_score DESC, created_at DESC"

        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)

        cursor = self.conn.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]

    def mark_hire_imported(self, candidate_id: int, talent_id: str) -> None:
        """标记候选人已导入飞书招聘人才库"""
        self.conn.execute(
            """UPDATE candidates
               SET hire_talent_id = ?, hire_imported_at = ?
               WHERE id = ?""",
            (talent_id, datetime.now().isoformat(), candidate_id),
        )
        self.conn.commit()
