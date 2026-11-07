"""巡检回复 + 获取简历脚本（二期风控：dry-run 零写库 / 限速配额 / 拒信 / 熔断 / 平台锁）"""

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib.browser import BrowserManager
from lib.config import RESUMES_DIR, load_config
from lib.db import Database
from lib.human_behavior import random_delay
from lib.platforms.boss import BossPlatform
from lib.platforms.maimai import MaimaiPlatform
from lib.safety import (
    CircuitBreaker,
    PlatformLockError,
    assert_headed_for_outbound,
    assert_maimai_outbound_allowed,
    is_reject_message,
    platform_lock,
    resolve_effective_limit,
)

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("resume-hunter")

PLATFORMS = {
    "boss": BossPlatform,
    "maimai": MaimaiPlatform,
}


def _extract_friend_text(state: dict) -> str:
    """尽量取出对方最近消息文本，供拒信过滤。"""
    for key in ("last_friend_msg", "friend_msg", "last_message", "preview"):
        val = state.get(key)
        if isinstance(val, str) and val.strip():
            return val
    msgs = state.get("friend_messages") or state.get("messages") or []
    if isinstance(msgs, list):
        parts = []
        for m in msgs:
            if isinstance(m, str):
                parts.append(m)
            elif isinstance(m, dict):
                parts.append(str(m.get("text") or m.get("content") or ""))
        return "\n".join(parts)
    return ""


def main():
    parser = argparse.ArgumentParser(description="巡检回复并获取简历")
    parser.add_argument(
        "--platform", "-p",
        choices=["boss", "maimai"],
        default="boss",
        help="目标平台 (默认: boss；不再默认 all)",
    )
    parser.add_argument("--days", "-d", type=int, default=7, help="检查最近 N 天的会话")
    parser.add_argument("--reply-msg", "-m", default="", help="自动回复（留空用 config 模板）")
    parser.add_argument("--limit", "-l", type=int, default=20, help="最多处理会话数 (默认 20)")
    parser.add_argument("--dry-run", action="store_true", help="试运行：只分析，零 DB 写入、零外发")
    parser.add_argument("--keep-open", action="store_true", help="结束后保持浏览器打开")
    parser.add_argument(
        "--i-know-the-risk",
        action="store_true",
        help="确认高上限（仍受 daily_hard_cap≤50；取代旧 --no-limit）",
    )
    parser.add_argument(
        "--no-limit",
        action="store_true",
        help=argparse.SUPPRESS,  # 兼容旧旗标：转为需确认 + 硬顶
    )
    parser.add_argument("--headless", action="store_true", help="无头（禁止非 dry-run 外发）")
    args = parser.parse_args()

    config = load_config()
    today = datetime.now().strftime("%Y-%m-%d")
    platform_name = args.platform
    platform_config = config["platforms"].get(platform_name, {})

    if not platform_config.get("enabled", True):
        print(f"平台 {platform_name} 未启用")
        sys.exit(1)

    # 旧 --no-limit：必须同时有 --i-know-the-risk，且硬顶 ≤50
    if args.no_limit:
        if not args.i_know_the_risk:
            print("✗ 已移除裸 --no-limit。如确需提高上限请加 --i-know-the-risk（硬顶仍 ≤ daily_hard_cap）")
            sys.exit(1)
        args.limit = max(args.limit, 50)
        args.days = max(args.days, 30)
        print("⚠ --no-limit 已降级为确认模式，硬顶仍受 daily_hard_cap 约束")

    try:
        assert_headed_for_outbound(headless=args.headless, scan_only=args.dry_run)
        if platform_name == "maimai":
            assert_maimai_outbound_allowed(
                platform_config, scan_only=False, dry_run=args.dry_run
            )
        effective_limit = resolve_effective_limit(
            args.limit,
            platform_config,
            i_know_the_risk=args.i_know_the_risk,
            config=config,
        )
    except (ValueError, RuntimeError) as e:
        print(f"✗ {e}")
        sys.exit(1)

    msg_cfg = config.get("messages", {})
    reply_template = args.reply_msg or msg_cfg.get(
        "reply_template",
        "您好，感谢您对岗位的关注，方便进一步沟通一下吗？",
    )
    reject_keywords = msg_cfg.get("reject_keywords") or None
    circuit_max = int(config.get("behavior", {}).get("circuit_max_failures", 3))

    print(f"\n{'=' * 50}")
    print(f"  Resume Hunter - 巡检模式")
    print(f"  平台: {platform_name}")
    print(f"  检查范围: 最近 {args.days} 天")
    print(f"  上限: {effective_limit}")
    print(f"  回复模板: {reply_template}")
    if args.dry_run:
        print(f"  模式: 试运行（零写库 / 零外发）")
    print(f"{'=' * 50}\n")

    # dry-run：不打开可写 Database 路径上的业务写入；仍可只读分析。
    # 为验收「零 DB 写入」，dry-run 使用独立内存库或不创建业务写入连接。
    db = None if args.dry_run else Database()

    try:
        lock_cm = platform_lock(platform_name)
        lock_cm.__enter__()
    except PlatformLockError as e:
        print(f"✗ {e}")
        if db:
            db.close()
        sys.exit(1)

    bm = BrowserManager(headless=args.headless)
    bm.start()

    total_replied = 0
    total_resume_requested = 0
    total_resumes = 0
    total_rejected_skip = 0
    total_friend_msg = 0

    try:
        print(f"\n--- {platform_name} ---")
        # dry-run 也构造 counter 便于展示；外发路径才 hydrate 持久配额
        platform = PLATFORMS[platform_name](
            daily_limit=effective_limit,
            db=None if args.dry_run else db,
        )
        if not args.dry_run and not platform.daily_counter.can_proceed():
            print(
                f"✗ 今日外发已达硬顶 {platform.daily_counter.count}/{effective_limit}，退出"
            )
            sys.exit(0)

        page = bm.new_page(platform_name)
        breaker = CircuitBreaker(max_failures=circuit_max)

        if not platform.is_logged_in(page):
            print(f"✗ {platform_name} 未登录，跳过")
            page.close()
            sys.exit(1)

        print(f"✓ {platform_name} 已登录")
        if not args.dry_run:
            print(f"  日配额剩余: {platform.daily_counter.remaining}")

        MAX_ROUNDS = max(3, (effective_limit // 30) + 2)
        processed = 0
        processed_names = set()

        for round_num in range(1, MAX_ROUNDS + 1):
            if processed >= effective_limit:
                break
            if breaker.tripped:
                break

            if round_num > 1:
                print(f"\n  --- 校准第 {round_num} 轮 ---")

            all_candidates = platform.check_replies(page)
            new_candidates = [c for c in all_candidates if c.name not in processed_names]

            if not new_candidates:
                if round_num == 1:
                    print(f"  无未读会话\n")
                else:
                    logger.info(f"  第 {round_num} 轮无新增未读")
                break

            print(f"  {'会话列表' if round_num == 1 else '补充发现'}共 {len(new_candidates)} 条\n")

            for candidate in new_candidates:
                if processed >= effective_limit:
                    logger.info(f"  已达处理上限 {effective_limit}，停止")
                    break
                if breaker.tripped:
                    break

                processed_names.add(candidate.name)

                if not platform.enter_conversation(page, candidate):
                    continue

                state = platform.analyze_conversation(page)

                status_parts = []
                if state.get("has_friend_msg"):
                    status_parts.append("候选人有消息")
                if state.get("has_my_msg"):
                    status_parts.append("我已回复")
                if state.get("resume_request_enabled"):
                    status_parts.append("求简历可用")
                if state.get("resume_file_enabled"):
                    status_parts.append("附件简历可下载")
                if state.get("job_name"):
                    status_parts.append(f"职位: {state['job_name']}")
                status_str = " | ".join(status_parts) if status_parts else "无消息"
                logger.info(f"  [{candidate.name}] {status_str}")

                if not state.get("has_friend_msg"):
                    continue

                total_friend_msg += 1
                friend_text = _extract_friend_text(state)

                # ---- dry-run：分析后立即 continue，零写库零外发 ----
                if args.dry_run:
                    if is_reject_message(friend_text, reject_keywords):
                        logger.info(f"  [dry-run] 拒信跳过: {candidate.name}")
                        total_rejected_skip += 1
                    processed += 1
                    continue

                # ---- 以下才允许 DB / 外发 ----
                if not platform.daily_counter.can_proceed():
                    logger.warning("日配额已满，停止外发")
                    break

                db_candidate = db.get_candidate_by_platform_id(platform_name, candidate.platform_id)
                if db_candidate:
                    cid = db_candidate["id"]
                    job_name = state.get("job_name", "")
                    if job_name and not db_candidate.get("job_name"):
                        # 只更新字段，不强制改状态
                        db.update_status(cid, db_candidate["status"], job_name=job_name)
                else:
                    cid = db.add_candidate(
                        platform=platform_name,
                        platform_id=candidate.platform_id,
                        name=candidate.name,
                    )
                    job_name = state.get("job_name", "")
                    if cid and job_name:
                        db.update_status(cid, "searched", job_name=job_name)

                # 对方有消息 ≠ 我方已回复：只记 friend 侧，不 mark_replied
                if cid:
                    db.mark_friend_messaged(cid)

                if is_reject_message(friend_text, reject_keywords):
                    logger.info(f"  拒信关键词命中，跳过自动回复/求简历: {candidate.name}")
                    total_rejected_skip += 1
                    processed += 1
                    continue

                # Step 1: 附件简历可下载
                if state.get("resume_file_enabled"):
                    save_dir = RESUMES_DIR / today / platform_name
                    filepath = platform.download_resume(page, candidate, str(save_dir))
                    if filepath and cid:
                        db.mark_resume_downloaded(cid, filepath)
                        db.increment_daily_stat(platform_name, "resumes")
                        total_resumes += 1
                    processed += 1
                    random_delay(2, 4)
                    continue

                # Step 2: 我方未回复 → 限速后发送；成功才 mark_replied
                if not state.get("has_my_msg"):
                    if not platform.daily_counter.can_proceed():
                        break
                    platform.rate_limiter.wait()
                    if platform.send_reply(page, reply_template):
                        if cid:
                            db.mark_replied(cid)
                            db.increment_daily_stat(platform_name, "replied")
                        platform.daily_counter.increment()
                        total_replied += 1
                        breaker.record_success()
                        random_delay(2, 3)
                        state = platform.analyze_conversation(page)
                    else:
                        if breaker.record_failure(page, reason=f"reply {candidate.name}"):
                            break

                # Step 3: 求简历（DB 幂等：只求一次）
                if state.get("resume_request_enabled"):
                    already = cid and db.has_resume_requested(cid)
                    if already:
                        logger.info(f"  已求过简历，跳过: {candidate.name}")
                    else:
                        if not platform.daily_counter.can_proceed():
                            break
                        platform.rate_limiter.wait()
                        if platform.request_resume(page, candidate):
                            if cid:
                                db.mark_resume_requested(cid)
                            platform.daily_counter.increment()
                            total_resume_requested += 1
                            breaker.record_success()
                            random_delay(1, 2)
                            state = platform.analyze_conversation(page)
                        else:
                            if breaker.record_failure(page, reason=f"resume {candidate.name}"):
                                break

                # Step 4: 下载
                if state.get("resume_file_enabled"):
                    save_dir = RESUMES_DIR / today / platform_name
                    filepath = platform.download_resume(page, candidate, str(save_dir))
                    if filepath and cid:
                        db.mark_resume_downloaded(cid, filepath)
                        db.increment_daily_stat(platform_name, "resumes")
                        total_resumes += 1

                if platform.detect_captcha(page):
                    if not platform.wait_for_captcha(page):
                        logger.error("验证码未通过，停止操作")
                        break

                processed += 1
                random_delay(2, 4)

            remaining = platform.get_chat_badge_count(page) if hasattr(platform, "get_chat_badge_count") else 0
            if remaining > 0:
                logger.warning(f"  校准: 处理后气泡仍有 {remaining} 条未读，将进行补充扫描")
            else:
                logger.info(f"  校准: 气泡为 0，未读全部处理完毕")
                break

        bm.save_session(platform_name)
        page.close()

        print(f"\n{'=' * 50}")
        print(f"  巡检完成")
        print(f"  对方有消息: {total_friend_msg}")
        print(f"  拒信跳过: {total_rejected_skip}")
        print(f"  我方成功回复: {total_replied}")
        print(f"  发送求简历请求: {total_resume_requested}")
        print(f"  下载简历: {total_resumes} 份")
        if breaker.tripped:
            print(f"  熔断截图: {breaker.last_screenshot}")
        print(f"{'=' * 50}\n")

    except KeyboardInterrupt:
        print("\n\n操作已中断")
        bm.save_session(platform_name)
    except Exception as e:
        logger.error(f"巡检异常: {e}", exc_info=True)
    finally:
        try:
            lock_cm.__exit__(None, None, None)
        except Exception:
            pass
        if db:
            db.close()
        bm.close(keep_open=args.keep_open)


if __name__ == "__main__":
    main()
