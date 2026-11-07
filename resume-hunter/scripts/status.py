"""状态查看脚本 - 展示今日统计和候选人概览"""

import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib.db import (
    STATUS_ANALYZED,
    STATUS_GREETED,
    STATUS_REPLIED,
    STATUS_RESUME_DOWNLOADED,
    STATUS_SEARCHED,
    Database,
)


def main():
    parser = argparse.ArgumentParser(description="查看运行状态和统计")
    parser.add_argument("--all", "-a", action="store_true",
                        help="显示所有候选人（不只是今天）")
    parser.add_argument("--platform", "-p", choices=["boss", "maimai"],
                        help="只显示特定平台")
    args = parser.parse_args()

    db = Database()
    today = datetime.now().strftime("%Y-%m-%d")

    print(f"\n{'=' * 55}")
    print(f"  Resume Hunter 状态面板  |  {today}")
    print(f"{'=' * 55}")

    # 各平台统计
    platforms = [args.platform] if args.platform else ["boss", "maimai"]

    for platform in platforms:
        print(f"\n  [{platform.upper()}]")

        for status, label in [
            (STATUS_SEARCHED, "已搜索"),
            (STATUS_GREETED, "已打招呼"),
            (STATUS_REPLIED, "已回复"),
            (STATUS_RESUME_DOWNLOADED, "已获取简历"),
            (STATUS_ANALYZED, "已分析"),
        ]:
            candidates = db.get_candidates_by_status(platform, status)
            count = len(candidates)
            print(f"    {label}: {count}")

    # 今日汇总
    stats = db.get_today_stats()
    print(f"\n  [今日汇总]")
    print(f"    Boss  - 打招呼: {stats.get('boss_greeted', 0)}  回复: {stats.get('boss_replied', 0)}  简历: {stats.get('boss_resumes', 0)}")
    print(f"    脉脉  - 打招呼: {stats.get('maimai_greeted', 0)}  回复: {stats.get('maimai_replied', 0)}  简历: {stats.get('maimai_resumes', 0)}")

    # 最近操作的候选人
    print(f"\n  [最近打招呼]")
    for platform in platforms:
        recent = db.get_candidates_by_status(platform, STATUS_GREETED)[:5]
        for c in recent:
            print(f"    [{platform}] {c['name']} - {c['title']} @ {c['company']}  ({c.get('greeted_at', '')[:16]})")

    print(f"\n  [等待回复]")
    for platform in platforms:
        waiting = db.get_greeted_candidates(platform, days=7)
        if waiting:
            print(f"    [{platform}] {len(waiting)} 人等待回复")

    print(f"\n{'=' * 55}\n")
    db.close()


if __name__ == "__main__":
    main()
