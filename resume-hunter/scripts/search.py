"""搜索 + 预筛选 + 打招呼脚本（二期风控：配额/token/平台锁/脉脉闸）"""

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib.browser import BrowserManager
from lib.candidate_scorer import CandidateScorer
from lib.config import JD_CACHE_FILE, load_config
from lib.db import Database
from lib.human_behavior import should_take_break, take_break
from lib.platforms.base import SearchFilters
from lib.platforms.boss import BossPlatform
from lib.platforms.maimai import MaimaiPlatform
from lib.safety import (
    CircuitBreaker,
    PlatformLockError,
    assert_headed_for_outbound,
    assert_maimai_outbound_allowed,
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


def load_jd_cache() -> dict:
    if JD_CACHE_FILE.exists():
        with open(JD_CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def find_jd_for_job(job_name: str, jd_cache: dict) -> tuple[str, dict]:
    if not job_name or not jd_cache:
        return ("", {})
    for key, info in jd_cache.items():
        if info.get("job_name", "") == job_name:
            return (info.get("jd_text", ""), info)
    for key, info in jd_cache.items():
        cached_name = info.get("job_name", key)
        if job_name in cached_name or cached_name in job_name:
            return (info.get("jd_text", ""), info)
    for key, info in jd_cache.items():
        cached_name = info.get("job_name", key)
        if any(word in cached_name for word in job_name.split() if len(word) >= 2):
            return (info.get("jd_text", ""), info)
    return ("", {})


def main():
    parser = argparse.ArgumentParser(description="搜索候选人并自动打招呼（带预筛选）")
    parser.add_argument("--platform", "-p", required=True, choices=["boss", "maimai"],
                        help="目标平台")
    parser.add_argument("--keywords", "-k", required=True,
                        help="搜索关键词，逗号分隔，如 '产品经理,B端,SaaS'")
    parser.add_argument("--limit", "-l", type=int, default=20,
                        help="最大触达人数 (默认 20，受 daily_hard_cap 约束)")
    parser.add_argument("--city", "-c", default="", help="城市筛选")
    parser.add_argument("--experience", "-e", default="", help="经验筛选，如 '3-5年'")
    parser.add_argument(
        "--tokens", "-t", type=int, default=None,
        help="畅聊卡 token 数量（Boss 专用；未指定则尝试读余额，读不到则非 scan-only 拒绝）",
    )
    parser.add_argument("--job", "-j", default="", help="指定岗位名")
    parser.add_argument("--threshold", "-th", type=float, default=6.0, help="最低评分阈值")
    parser.add_argument("--no-api", action="store_true", help="只用规则过滤")
    parser.add_argument("--scan-only", action="store_true", help="只扫描评分，不打招呼")
    parser.add_argument("--dry-run", action="store_true", help="同 --scan-only")
    parser.add_argument("--keep-open", action="store_true", help="结束后保持浏览器打开")
    parser.add_argument(
        "--i-know-the-risk",
        action="store_true",
        help="确认突破建议上限（仍受 daily_hard_cap 硬顶）",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="无头模式（禁止非 scan-only 外发）",
    )
    args = parser.parse_args()

    scan_only = args.scan_only or args.dry_run
    config = load_config()
    platform_name = args.platform
    keywords = [k.strip() for k in args.keywords.split(",") if k.strip()]
    filters = SearchFilters(city=args.city, experience=args.experience)

    platform_config = config["platforms"].get(platform_name, {})
    if not platform_config.get("enabled", True):
        print(f"平台 {platform_name} 未启用，请在配置中开启")
        sys.exit(1)

    try:
        assert_headed_for_outbound(headless=args.headless, scan_only=scan_only)
        if platform_name == "maimai":
            assert_maimai_outbound_allowed(
                platform_config, scan_only=scan_only, dry_run=args.dry_run
            )
        daily_limit = resolve_effective_limit(
            args.limit,
            platform_config,
            i_know_the_risk=args.i_know_the_risk,
            config=config,
        )
    except (ValueError, RuntimeError) as e:
        print(f"✗ {e}")
        sys.exit(1)

    # Boss token 闸门：默认未指定；0 退出；负数报错；未知余额且非 scan-only 拒绝
    token_limit = args.tokens
    if platform_name == "boss":
        if token_limit is not None and token_limit < 0:
            print("✗ --tokens 不能为负数")
            sys.exit(1)
        if token_limit == 0:
            print("✗ --tokens 0，退出（无可用 token）")
            sys.exit(0)

    behavior_config = config.get("behavior", {})
    break_after = behavior_config.get("break_after", 25)
    circuit_max = int(behavior_config.get("circuit_max_failures", 3))

    db = Database()
    platform = PLATFORMS[platform_name](daily_limit=daily_limit, db=db)

    if not platform.daily_counter.can_proceed() and not scan_only:
        print(
            f"✗ 今日外发已达硬顶 {platform.daily_counter.count}/{daily_limit}，"
            f"直接退出不外发"
        )
        db.close()
        sys.exit(0)

    if platform_name == "boss" and token_limit is not None and token_limit > 0:
        platform._token_balance = token_limit
        daily_limit = min(daily_limit, token_limit)
        platform.daily_counter.daily_limit = daily_limit

    jd_cache = load_jd_cache()
    jd_text, jd_info = find_jd_for_job(args.job, jd_cache)
    has_jd = bool(jd_text)
    use_api = not args.no_api and has_jd

    extra_keywords = []
    if args.job and has_jd and sys.stdin.isatty():
        print(f"\n岗位: {jd_info.get('job_name', args.job)}")
        print(f"JD 技能要求: {', '.join(jd_info.get('skills', [])) or '无'}")
        print(f"搜索关键词: {', '.join(keywords)}")
        extra_input = input("\n是否有额外筛选关键词？（逗号分隔，直接回车跳过）: ").strip()
        if extra_input:
            extra_keywords = [k.strip() for k in extra_input.split(",") if k.strip()]
            print(f"  已添加: {', '.join(extra_keywords)}")

    all_keywords = keywords + extra_keywords
    scorer = CandidateScorer(
        jd_text=jd_text,
        jd_info=jd_info,
        search_keywords=all_keywords,
    )

    print(f"\n{'=' * 50}")
    print(f"  Resume Hunter - 智能搜索模式")
    print(f"  平台: {platform_name}")
    print(f"  关键词: {', '.join(keywords)}")
    print(f"  上限: {daily_limit} 人（含持久日配额）")
    print(f"  已用 outbound: {platform.daily_counter.count}")
    if args.job:
        if has_jd:
            print(f"  匹配岗位: {jd_info.get('job_name', args.job)} (JD {len(jd_text)} 字)")
        else:
            print(f"  匹配岗位: {args.job} (未找到 JD，仅用规则过滤)")
    print(f"  评分阈值: {args.threshold}")
    print(f"  评分模式: {'规则 + API' if use_api else '仅规则'}")
    if platform_name == "boss":
        if token_limit is not None:
            print(f"  畅聊卡 token: {token_limit}")
        else:
            print(f"  畅聊卡 token: 未指定（将尝试读余额）")
    if scan_only:
        print(f"  模式: 扫描模式（不打招呼）")
    print(f"{'=' * 50}\n")

    try:
        lock_cm = platform_lock(platform_name)
        lock_cm.__enter__()
    except PlatformLockError as e:
        print(f"✗ {e}")
        db.close()
        sys.exit(1)

    bm = BrowserManager(headless=args.headless)
    bm.start()
    breaker = CircuitBreaker(max_failures=circuit_max)

    stats = {
        "total": 0,
        "rule_passed": 0,
        "rule_filtered": 0,
        "api_passed": 0,
        "api_filtered": 0,
        "greeted": 0,
        "skipped_existing": 0,
    }

    try:
        page = bm.new_page(platform_name)

        if not platform.is_logged_in(page):
            print("✗ 未登录，请先运行: python scripts/login.py --platform " + platform_name)
            sys.exit(1)
        print("✓ 已登录\n")

        # Boss：非 scan-only 必须有明确 token（CLI 或页面解析）
        if platform_name == "boss" and not scan_only:
            if token_limit is None:
                bal = platform.check_token_balance(page)
                if bal is None or bal < 0:
                    print("✗ 未能解析畅聊卡余额，且未传 --tokens N(N>0)；非 scan-only 拒绝运行")
                    sys.exit(1)
                if bal == 0:
                    print("✗ 畅聊卡余额为 0，退出")
                    sys.exit(0)
                platform._token_balance = bal
                daily_limit = min(daily_limit, bal)
                platform.daily_counter.daily_limit = daily_limit
                print(f"✓ 解析到畅聊卡余额: {bal}，有效上限 {daily_limit}")
            else:
                platform._token_balance = token_limit

        candidates = platform.search_candidates(page, keywords, filters, max_results=daily_limit)
        print(f"\n找到 {len(candidates)} 位候选人，开始预筛选...\n")
        stats["total"] = len(candidates)

        for i, candidate in enumerate(candidates):
            if not scan_only and not platform.daily_counter.can_proceed():
                logger.warning("日配额已满，停止外发")
                break
            if breaker.tripped:
                break

            if db.candidate_exists(platform_name, candidate.platform_id):
                logger.info(f"  跳过已处理: {candidate.name}")
                stats["skipped_existing"] += 1
                continue

            rule_passed, rule_reason = scorer.rule_filter(candidate)
            if not rule_passed:
                logger.info(f"  ✗ [规则] {candidate.name} ({candidate.title}) - {rule_reason}")
                stats["rule_filtered"] += 1
                db.add_candidate(
                    platform=platform_name,
                    platform_id=candidate.platform_id,
                    name=candidate.name,
                    title=candidate.title,
                    company=candidate.company,
                    experience=candidate.experience,
                    profile_url=candidate.profile_url,
                    search_keyword=candidate.search_keyword,
                )
                continue

            stats["rule_passed"] += 1

            api_score = -1
            api_reason = ""
            if use_api:
                result = scorer.api_evaluate(candidate)
                api_score = result["score"]
                api_reason = result["reason"]
                if api_score >= 0 and api_score < args.threshold:
                    logger.info(
                        f"  ✗ [API {api_score:.0f}分] {candidate.name} ({candidate.title}) - {api_reason}"
                    )
                    stats["api_filtered"] += 1
                    db.add_candidate(
                        platform=platform_name,
                        platform_id=candidate.platform_id,
                        name=candidate.name,
                        title=candidate.title,
                        company=candidate.company,
                        experience=candidate.experience,
                        profile_url=candidate.profile_url,
                        search_keyword=candidate.search_keyword,
                    )
                    continue
                if api_score >= 0:
                    stats["api_passed"] += 1
                    logger.info(
                        f"  ✓ [API {api_score:.0f}分] {candidate.name} ({candidate.title}) - {api_reason}"
                    )

            score_str = f" (API: {api_score:.0f}分)" if api_score >= 0 else " (规则通过)"
            if not use_api:
                score_str = " (规则通过)"

            cid = db.add_candidate(
                platform=platform_name,
                platform_id=candidate.platform_id,
                name=candidate.name,
                title=candidate.title,
                company=candidate.company,
                experience=candidate.experience,
                profile_url=candidate.profile_url,
                search_keyword=candidate.search_keyword,
            )
            if cid is None:
                continue

            if api_score >= 0 and cid:
                db.update_status(cid, "searched", match_score=api_score)

            if scan_only:
                logger.info(
                    f"  [扫描]{score_str} {candidate.name} - {candidate.title} @ {candidate.company}"
                )
                continue

            # 外发前配额（限速在平台 send_greeting 内）
            if not platform.daily_counter.can_proceed():
                break

            ok = platform.send_greeting(page, candidate)
            if ok:
                db.mark_greeted(cid)
                db.increment_daily_stat(platform_name, "greeted")
                # outbound 由 DailyCounter.increment 在平台 send_greeting 内完成；
                # 若平台未调 increment（异常路径），这里兜底不重复：Boss/MaiMai 已 increment
                stats["greeted"] += 1
                breaker.record_success()

                if platform.detect_captcha(page):
                    if not platform.wait_for_captcha(page):
                        logger.error("验证码未通过，停止操作")
                        break

                if platform_name == "boss" and not platform.has_enough_tokens():
                    logger.warning("畅聊卡 token 已用完，停止")
                    break
                # 定期校准余额
                if platform_name == "boss" and stats["greeted"] % 10 == 0:
                    platform.check_token_balance(page)
            else:
                if breaker.record_failure(page, reason=f"greet {candidate.name}"):
                    break

            if should_take_break(stats["greeted"], break_after):
                take_break()

        for keyword in keywords:
            db.log_search(platform_name, keyword, stats["total"], stats["greeted"])

        bm.save_session(platform_name)

        print(f"\n{'=' * 50}")
        print(f"  搜索完成")
        print(f"  扫描候选人: {stats['total']} 人")
        print(f"  已处理跳过: {stats['skipped_existing']} 人")
        print(f"  规则过滤: {stats['rule_filtered']} 人")
        print(f"  规则通过: {stats['rule_passed']} 人")
        if use_api:
            print(f"  API 过滤: {stats['api_filtered']} 人")
            print(f"  API 通过: {stats['api_passed']} 人")
        if not scan_only:
            print(f"  打招呼: {stats['greeted']} 人")
            print(f"  剩余配额: {platform.daily_counter.remaining}")
            if platform_name == "boss" and platform._token_balance is not None:
                print(f"  剩余 token: {platform._token_balance}")
            if breaker.tripped:
                print(f"  熔断截图: {breaker.last_screenshot}")
        print(f"{'=' * 50}\n")

    except KeyboardInterrupt:
        print("\n\n操作已中断")
        bm.save_session(platform_name)
    except Exception as e:
        logger.error(f"搜索异常: {e}", exc_info=True)
        bm.save_session(platform_name)
    finally:
        try:
            lock_cm.__exit__(None, None, None)
        except Exception:
            pass
        db.close()
        bm.close(keep_open=args.keep_open)


if __name__ == "__main__":
    main()
