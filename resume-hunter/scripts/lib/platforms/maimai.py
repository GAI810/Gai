"""脉脉自动化模块

脉脉招聘者端工作流：
1. 人才搜索/人才银行：https://maimai.cn/ent/talents/discover/search
2. 搜索候选人 → 浏览列表
3. 点击候选人 → 查看经历介绍
4. 点击"立即沟通" → 发送招呼语
5. 消息页面：检查候选人回复
6. 候选人回复后 → 索要简历

注意：
- 脉脉页面结构可能随时更新，选择器需要根据实际页面调整
- 首次使用前需要运行 login.py 手动登录保存 session
- 所有选择器标记了 TODO，需要在真实页面上验证和调整
"""

import logging
import re
import time
from pathlib import Path
from typing import Optional

from ..human_behavior import human_click, random_delay, random_scroll
from ..rate_limiter import DailyCounter, RateLimiter
from .base import BasePlatform, Candidate, SearchFilters

logger = logging.getLogger("resume-hunter")


class MaimaiPlatform(BasePlatform):
    """脉脉自动化实现"""

    # ===== 页面 URL =====
    SEARCH_URL = "https://maimai.cn/ent/talents/discover/search"
    CHAT_URL = "https://maimai.cn/ent/im"
    HOME_URL = "https://maimai.cn"

    # ===== 选择器（TODO: 需要在真实页面验证） =====
    # 搜索页
    SEL_SEARCH_INPUT = 'input[class*="search"], input[placeholder*="搜索"], .search-input input'
    SEL_SEARCH_BTN = 'button[class*="search"], .search-btn, .btn-search'
    SEL_CANDIDATE_CARD = '.talent-card, .candidate-card, [class*="talent-item"]'
    SEL_CANDIDATE_NAME = '.name, .talent-name, [class*="name"]'
    SEL_CANDIDATE_TITLE = '.position, .talent-title, [class*="position"]'
    SEL_CANDIDATE_COMPANY = '.company, .talent-company, [class*="company"]'
    SEL_CANDIDATE_EXP = '.experience, .talent-exp, [class*="desc"]'

    # 沟通按钮
    SEL_GREET_BTN = '.btn-chat, .btn-contact, [class*="contact"], [class*="communicate"]'

    # 对话页面
    SEL_CHAT_LIST_ITEM = '.chat-item, .im-item, [class*="conversation"]'
    SEL_CHAT_NAME = '.name, .chat-name, [class*="name"]'
    SEL_CHAT_LAST_MSG = '.last-msg, .msg-preview, [class*="last-msg"]'
    SEL_CHAT_UNREAD = '.unread, .badge, [class*="unread"]'

    # 简历相关
    SEL_REQUEST_RESUME = '[class*="resume"], .btn-resume, [class*="request-resume"]'
    SEL_RESUME_DOWNLOAD = 'a[download], .resume-download, [class*="download"]'

    @property
    def name(self) -> str:
        return "maimai"

    def __init__(self, daily_limit: int = 50, db=None):
        self.rate_limiter = RateLimiter(max_per_hour=60, min_interval=30, max_interval=90)
        self.daily_counter = DailyCounter(daily_limit=daily_limit, db=db, platform="maimai")

    def is_logged_in(self, page) -> bool:
        """检测是否已登录"""
        try:
            page.goto(self.HOME_URL, wait_until="domcontentloaded", timeout=15000)
            random_delay(2, 4)
            logged_in = page.query_selector('.avatar, .user-info, [class*="avatar"]') is not None
            if not logged_in:
                has_login = page.query_selector('.login-btn, a[href*="login"]') is not None
                logged_in = not has_login
            return logged_in
        except Exception as e:
            logger.error(f"检测登录状态失败: {e}")
            return False

    def search_candidates(self, page, keywords: list[str],
                          filters: Optional[SearchFilters] = None,
                          max_results: int = 100) -> list[Candidate]:
        """搜索候选人"""
        candidates = []

        for keyword in keywords:
            if not self.daily_counter.can_proceed():
                logger.warning(f"已达每日上限 {self.daily_counter.count}，停止搜索")
                break

            logger.info(f"搜索关键词: {keyword}")
            page_candidates = self._search_one_keyword(page, keyword, filters, max_results - len(candidates))
            candidates.extend(page_candidates)

            if len(candidates) >= max_results:
                break

        return candidates

    def _search_one_keyword(self, page, keyword: str,
                            filters: Optional[SearchFilters] = None,
                            limit: int = 100) -> list[Candidate]:
        """搜索单个关键词"""
        candidates = []

        # 导航到搜索页
        page.goto(self.SEARCH_URL, wait_until="domcontentloaded", timeout=15000)
        random_delay(3, 5)

        # 输入搜索关键词
        search_input = page.query_selector(self.SEL_SEARCH_INPUT)
        if search_input:
            search_input.fill("")
            random_delay(0.5, 1)
            search_input.fill(keyword)
            random_delay(0.5, 1)
            page.keyboard.press("Enter")
            random_delay(3, 5)

        page_num = 1
        while len(candidates) < limit and self.daily_counter.can_proceed():
            logger.info(f"  第 {page_num} 页")

            cards = page.query_selector_all(self.SEL_CANDIDATE_CARD)
            if not cards:
                logger.info("  未找到候选人卡片，搜索结束")
                break

            for card in cards:
                if len(candidates) >= limit or not self.daily_counter.can_proceed():
                    break

                try:
                    candidate = self._extract_candidate_from_card(card, keyword)
                    if candidate:
                        candidates.append(candidate)
                except Exception as e:
                    logger.debug(f"  解析候选人卡片失败: {e}")
                    continue

            # 翻页
            if not self._go_next_page(page):
                break
            page_num += 1
            random_delay(2, 4)

        return candidates

    def _extract_candidate_from_card(self, card, keyword: str) -> Optional[Candidate]:
        """从候选人卡片提取信息"""
        name_el = card.query_selector(self.SEL_CANDIDATE_NAME)
        title_el = card.query_selector(self.SEL_CANDIDATE_TITLE)
        company_el = card.query_selector(self.SEL_CANDIDATE_COMPANY)
        exp_el = card.query_selector(self.SEL_CANDIDATE_EXP)

        name = name_el.text_content().strip() if name_el else "未知"
        title = title_el.text_content().strip() if title_el else ""
        company = company_el.text_content().strip() if company_el else ""
        experience = exp_el.text_content().strip() if exp_el else ""

        link = card.query_selector("a[href]")
        profile_url = link.get_attribute("href") if link else ""
        platform_id = self._extract_id_from_url(profile_url) or f"maimai_{name}_{hash(name + title) % 10000}"

        return Candidate(
            platform="maimai",
            platform_id=platform_id,
            name=name,
            title=title,
            company=company,
            experience=experience,
            profile_url=profile_url,
            search_keyword=keyword,
        )

    def send_greeting(self, page, candidate: Candidate) -> bool:
        """向候选人发送打招呼"""
        try:
            self.rate_limiter.wait()

            if candidate.profile_url:
                url = candidate.profile_url
                if not url.startswith("http"):
                    url = f"{self.HOME_URL}{url}"
                page.goto(url, wait_until="domcontentloaded", timeout=15000)
                random_delay(2, 4)

            greet_btn = page.query_selector(self.SEL_GREET_BTN)
            if not greet_btn:
                logger.debug(f"  未找到沟通按钮: {candidate.name}")
                return False

            human_click(page, self.SEL_GREET_BTN)
            random_delay(2, 3)

            self.daily_counter.increment()
            logger.info(f"  ✓ 已打招呼: {candidate.name} ({candidate.title})")
            return True

        except Exception as e:
            logger.error(f"  ✗ 打招呼失败 {candidate.name}: {e}")
            return False

    def check_replies(self, page) -> list[Candidate]:
        """检查消息列表，找出已回复的候选人"""
        replied = []

        page.goto(self.CHAT_URL, wait_until="domcontentloaded", timeout=15000)
        random_delay(3, 5)

        chat_items = page.query_selector_all(self.SEL_CHAT_LIST_ITEM)
        logger.info(f"会话列表共 {len(chat_items)} 条")

        for item in chat_items:
            try:
                unread = item.query_selector(self.SEL_CHAT_UNREAD)
                name_el = item.query_selector(self.SEL_CHAT_NAME)

                if not name_el:
                    continue

                name = name_el.text_content().strip()
                if unread:
                    candidate = Candidate(
                        platform="maimai",
                        platform_id=f"maimai_chat_{name}",
                        name=name,
                    )
                    replied.append(candidate)
                    logger.info(f"  发现回复: {name}")

            except Exception as e:
                logger.debug(f"  检查会话失败: {e}")
                continue

        return replied

    def request_resume(self, page, candidate: Candidate) -> bool:
        """在对话中索要简历"""
        try:
            random_delay(1, 2)
            resume_btn = page.query_selector(self.SEL_REQUEST_RESUME)
            if resume_btn:
                human_click(page, self.SEL_REQUEST_RESUME)
                random_delay(2, 3)
                logger.info(f"  ✓ 已索要简历: {candidate.name}")
                return True
            else:
                logger.debug(f"  未找到索要简历按钮: {candidate.name}")
                return False

        except Exception as e:
            logger.error(f"  ✗ 索要简历失败 {candidate.name}: {e}")
            return False

    def download_resume(self, page, candidate: Candidate, save_dir: str) -> Optional[str]:
        """下载简历文件"""
        try:
            save_path = Path(save_dir)
            save_path.mkdir(parents=True, exist_ok=True)

            with page.expect_download(timeout=10000) as download_info:
                download_btn = page.query_selector(self.SEL_RESUME_DOWNLOAD)
                if download_btn:
                    human_click(page, self.SEL_RESUME_DOWNLOAD)
                else:
                    logger.debug(f"  未找到下载按钮: {candidate.name}")
                    return None

            download = download_info.value
            original_name = download.suggested_filename
            ext = Path(original_name).suffix or ".pdf"
            filename = f"{candidate.name}_{candidate.title}{ext}".replace("/", "_")
            filepath = save_path / filename

            download.save_as(str(filepath))
            logger.info(f"  ✓ 简历已下载: {filepath}")
            return str(filepath)

        except Exception as e:
            logger.error(f"  ✗ 下载简历失败 {candidate.name}: {e}")
            return None

    # ===== 辅助方法 =====

    def _go_next_page(self, page) -> bool:
        """翻到下一页"""
        try:
            next_btn = page.query_selector('.next, [class*="next"], .pagination-next')
            if next_btn and next_btn.is_enabled():
                human_click(page, '.next, [class*="next"], .pagination-next')
                page.wait_for_load_state("domcontentloaded", timeout=10000)
                random_delay(2, 4)
                return True
        except Exception:
            pass
        return False

    def _extract_id_from_url(self, url: str) -> str:
        """从 URL 中提取候选人 ID"""
        if not url:
            return ""
        match = re.search(r"/profile/(\d+)", url)
        if match:
            return match.group(1)
        return ""

    def detect_captcha(self, page) -> bool:
        """检测是否出现验证码"""
        captcha_selectors = [
            '[class*="captcha"]',
            '[class*="verify"]',
            ".slide-verify",
        ]
        for sel in captcha_selectors:
            if page.query_selector(sel):
                logger.warning("⚠ 检测到验证码！")
                return True
        return False

    def wait_for_captcha(self, page, timeout: int = 300) -> bool:
        """等待用户完成验证码"""
        if not self.detect_captcha(page):
            return True

        print("\n" + "=" * 50)
        print("⚠ 检测到验证码，请在浏览器中手动完成验证")
        print(f"  超时时间: {timeout} 秒")
        print("=" * 50 + "\n")

        start = time.time()
        while time.time() - start < timeout:
            time.sleep(2)
            if not self.detect_captcha(page):
                logger.info("验证码已通过")
                return True

        logger.error("验证码处理超时")
        return False
