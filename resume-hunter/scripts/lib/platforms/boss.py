"""Boss直聘自动化模块

Boss直聘招聘者端工作流（基于真实 DOM 验证）：
1. 招聘者后台入口：https://www.zhipin.com/web/geek/chat
2. 左侧菜单「搜索」→ /web/chat/search → iframe /web/frame/search/
3. 搜索结果以 .card-container 卡片展示，点击卡片打招呼（消耗畅聊卡 token）
4. 「沟通」页面查看会话列表，检测候选人回复
5. 简历获取流程：双方互发消息 → 点击"求简历" → 候选人同意 → 附件简历可下载

页面结构关键点：
- 搜索页内容在 iframe[name=searchFrame] 内，需要用 frame_locator 访问
- 聊天列表在主页面，不在 iframe 内
- 打招呼会消耗畅聊卡 token，需要做 token 余量检查
- 对话中 .item-friend = 候选人消息，.item-myself = 我方消息
- "求简历"按钮需双方互发消息后才启用（.operate-btn 不含 disabled）
- "附件简历"按钮在候选人同意求简历后才启用（.resume-btn-file 不含 disabled）
"""

import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from ..human_behavior import human_click, random_delay, random_scroll, should_take_break, take_break
from ..rate_limiter import DailyCounter, RateLimiter
from .base import BasePlatform, Candidate, SearchFilters

logger = logging.getLogger("resume-hunter")


class BossPlatform(BasePlatform):
    """Boss直聘自动化实现"""

    # ===== 页面 URL =====
    CHAT_URL = "https://www.zhipin.com/web/geek/chat"
    SEARCH_URL = "https://www.zhipin.com/web/chat/search"
    HOME_URL = "https://www.zhipin.com"
    SEARCH_IFRAME_URL = "/web/frame/search/"
    PROPS_URL = "https://www.zhipin.com/web/chat/business/mall"

    # ===== 主页面选择器（已验证） =====
    # 左侧菜单
    SEL_MENU_SEARCH = "dl.menu-geeksearch"
    SEL_MENU_CHAT = "dl.menu-chat"
    SEL_MENU_RECOMMEND = "dl.menu-recommend"
    SEL_MENU_PROPS = "dl.menu-prop"

    # 聊天列表
    SEL_CHAT_ITEM = ".geek-item-wrap"
    SEL_CHAT_ITEM_INNER = ".geek-item"
    SEL_CHAT_NAME = ".geek-name"
    SEL_CHAT_LABEL_LIST = ".label-list"
    SEL_CHAT_LABEL_ITEM = ".chat-label-item"
    SEL_CHAT_JOB_SELECT = ".chat-select-job"
    SEL_USER_LIST = ".user-list"

    # 聊天详情/对话
    SEL_CONVERSATION = ".chat-conversation"
    SEL_CONVERSATION_MSG = ".conversation-message"
    SEL_CHAT_MSG_LIST = ".chat-message-list"
    SEL_EDITOR_INPUT = ".boss-chat-editor-input"
    SEL_SUBMIT_BTN = ".submit-content"

    # 对话消息类型（已验证）
    SEL_MSG_FRIEND = ".item-friend"  # 候选人发的消息
    SEL_MSG_MYSELF = ".item-myself"  # 我方发的消息
    SEL_MSG_SYSTEM = ".item-system"  # 系统消息

    # 简历按钮（在对话区域，已验证）
    SEL_RESUME_ONLINE = "a.resume-btn-online"  # 在线简历（只查看经历页，非 PDF）
    SEL_RESUME_FILE = ".resume-btn-file"  # 附件简历（候选人同意后才可下载 PDF）
    SEL_RESUME_CONTENT = ".resume-btn-content"
    SEL_ITEM_RESUME = ".item-resume"

    # 操作按钮（在对话右侧工具栏）
    SEL_OPERATE_BTNS = ".operate-btn"  # 所有操作按钮

    # ===== 搜索 iframe 内选择器（已验证） =====
    SEL_IFRAME = 'iframe[name=searchFrame]'
    SEL_SEARCH_INPUT = "input.search-input"
    SEL_CANDIDATE_CARD = ".card-container"
    SEL_CARD_INNER = ".card-inner"
    SEL_CANDIDATE_NAME = ".name-label"
    SEL_CANDIDATE_INFO_LABELS = ".info-labels .label-text"
    SEL_CANDIDATE_DETAIL = ".info-detail"
    SEL_CANDIDATE_EXPECT = ".expect-exp-box"
    SEL_CANDIDATE_WORK = ".work-exp-box"
    SEL_CANDIDATE_EDU = ".edu-exp-box"
    SEL_KEYWORD_TAG = ".keyword-tag"
    SEL_AI_SEARCH_BTN = ".btn-ai-common"
    SEL_CHANGE_BTN = ".change-btn"  # 换一换

    # 筛选器
    SEL_FILTER_DEGREE = ".degree-select-C"
    SEL_FILTER_RESET = ".reset-btn"  # 清空筛选
    SEL_FILTER_CHECKBOX = ".filter-checkbox, .check-item, .el-checkbox"
    SEL_COLLEAGUE_EXCHANGE = ".colleague-exchange-filter"  # 同事交换简历筛选

    @property
    def name(self) -> str:
        return "boss"

    def __init__(self, daily_limit: int = 50, db=None):
        self.rate_limiter = RateLimiter(max_per_hour=60, min_interval=30, max_interval=90)
        self.daily_counter = DailyCounter(daily_limit=daily_limit, db=db, platform="boss")
        self._token_balance: Optional[int] = None

    def is_logged_in(self, page) -> bool:
        """检测是否已登录 - 通过访问聊天页判断"""
        try:
            page.goto(self.CHAT_URL, wait_until="domcontentloaded", timeout=20000)
            # 等待左侧菜单出现，最多 10 秒
            try:
                page.wait_for_selector(self.SEL_MENU_CHAT, timeout=10000)
                return True
            except Exception:
                return False
        except Exception as e:
            logger.error(f"检测登录状态失败: {e}")
            return False

    # ===== Token 管理 =====

    def check_token_balance(self, page) -> Optional[int]:
        """检查畅聊卡 token 余额；未知时返回 None（不明就停）。"""
        try:
            props_el = page.query_selector(self.SEL_MENU_PROPS)
            if props_el:
                text = props_el.text_content()
                match = re.search(r"(\d+)", text)
                if match:
                    self._token_balance = int(match.group(1))
                    logger.info(f"畅聊卡余额: {self._token_balance}")
                    return self._token_balance
        except Exception as e:
            logger.debug(f"检查 token 余额失败: {e}")

        # 未知：保持 None，禁止把 -1 当成「不限」
        self._token_balance = None
        return None

    def has_enough_tokens(self) -> bool:
        """是否还有足够的 token；未知（None/<0）视为不足。"""
        if self._token_balance is None or self._token_balance < 0:
            return False
        return self._token_balance > 0

    # ===== 搜索候选人 =====

    def search_candidates(self, page, keywords: list[str],
                          filters: Optional[SearchFilters] = None,
                          max_results: int = 100) -> list[Candidate]:
        """搜索候选人"""
        candidates = []

        # 先进入搜索页
        self._navigate_to_search(page)

        for keyword in keywords:
            if not self.daily_counter.can_proceed():
                logger.warning(f"已达每日上限 {self.daily_counter.count}，停止搜索")
                break
            # 搜索/浏览不消耗畅聊卡；token 闸只用于打招呼/外发路径（has_enough_tokens）
            # scan-only 未设 _token_balance 时不得在此 break

            logger.info(f"搜索关键词: {keyword}")
            page_candidates = self._search_one_keyword(page, keyword, filters, max_results - len(candidates))
            candidates.extend(page_candidates)

            if len(candidates) >= max_results:
                break

        return candidates

    def _navigate_to_search(self, page) -> None:
        """导航到搜索页面"""
        page.goto(self.CHAT_URL, wait_until="domcontentloaded", timeout=20000)
        random_delay(2, 4)
        # 检查 token
        self.check_token_balance(page)
        # 点击搜索菜单
        page.click(self.SEL_MENU_SEARCH, timeout=5000)
        random_delay(5, 8)

    def _get_search_frame(self, page):
        """获取搜索 iframe 的 frame_locator"""
        return page.frame_locator(self.SEL_IFRAME)

    def _search_one_keyword(self, page, keyword: str,
                            filters: Optional[SearchFilters] = None,
                            limit: int = 100) -> list[Candidate]:
        """在搜索 iframe 内搜索单个关键词"""
        candidates = []
        fl = self._get_search_frame(page)

        # 清空并输入关键词
        search_input = fl.locator(self.SEL_SEARCH_INPUT)
        try:
            search_input.wait_for(timeout=10000)
            search_input.fill("")
            random_delay(0.3, 0.8)
            search_input.fill(keyword)
            random_delay(0.5, 1)
            search_input.press("Enter")
            random_delay(4, 6)
        except Exception as e:
            logger.error(f"搜索框操作失败: {e}")
            return candidates

        # 勾选"仅30天未和同事交换简历"筛选框
        self._apply_colleague_filter(page)

        page_num = 1
        while len(candidates) < limit and self.daily_counter.can_proceed():
            logger.info(f"  第 {page_num} 页")

            cards = fl.locator(self.SEL_CANDIDATE_CARD)
            count = cards.count()
            if count == 0:
                logger.info("  未找到候选人卡片，搜索结束")
                break

            logger.info(f"  找到 {count} 张候选人卡片")

            for i in range(count):
                if len(candidates) >= limit or not self.daily_counter.can_proceed():
                    break
                # 浏览卡片不消耗 token；不足时由外发/打招呼路径拦截

                try:
                    card = cards.nth(i)
                    candidate = self._extract_candidate_from_card(card, keyword)
                    if candidate:
                        candidates.append(candidate)
                except Exception as e:
                    logger.debug(f"  解析候选人卡片失败: {e}")
                    continue

            # 滚动页面看看有没有更多内容
            try:
                fl.locator("body").evaluate("el => window.scrollBy(0, window.innerHeight)")
                random_delay(2, 3)
            except Exception:
                pass

            page_num += 1
            if page_num > 10:  # 最多翻 10 页
                break

        return candidates

    def _extract_candidate_from_card(self, card, keyword: str) -> Optional[Candidate]:
        """从 iframe 内的候选人卡片提取信息"""
        try:
            name_el = card.locator(self.SEL_CANDIDATE_NAME).first
            name = name_el.text_content(timeout=3000).strip() if name_el.count() > 0 else "未知"

            # 提取基本信息标签 (年龄/经验/学历/状态/薪资)
            labels = card.locator(self.SEL_CANDIDATE_INFO_LABELS)
            label_texts = []
            for j in range(labels.count()):
                label_texts.append(labels.nth(j).text_content(timeout=2000).strip())
            experience = " | ".join(label_texts)

            # 详细介绍
            detail_el = card.locator(self.SEL_CANDIDATE_DETAIL).first
            detail = detail_el.text_content(timeout=2000).strip() if detail_el.count() > 0 else ""

            # 工作经历
            work_el = card.locator(self.SEL_CANDIDATE_WORK).first
            title = ""
            company = ""
            if work_el.count() > 0:
                work_text = work_el.text_content(timeout=2000).strip()
                # 格式: "职位 公司名 职位名"
                parts = [p.strip() for p in work_text.replace("职位", "").split() if p.strip()]
                if len(parts) >= 2:
                    company = parts[0]
                    title = parts[1]
                elif parts:
                    title = parts[0]

            # 学历信息
            education = ""
            edu_el = card.locator(self.SEL_CANDIDATE_EDU).first
            if edu_el.count() > 0:
                education = edu_el.text_content(timeout=2000).strip()
                # 去掉前缀 "教育" 文本
                education = education.replace("教育", "").strip()

            # 技能标签
            skills = []
            skill_els = card.locator(self.SEL_KEYWORD_TAG)
            for j in range(skill_els.count()):
                tag = skill_els.nth(j).text_content(timeout=2000).strip()
                if tag:
                    skills.append(tag)

            # 生成唯一 ID
            platform_id = f"boss_{hash(name + title + company) % 100000:05d}"

            return Candidate(
                platform="boss",
                platform_id=platform_id,
                name=name,
                title=title,
                company=company,
                experience=experience,
                profile_url="",
                search_keyword=keyword,
                education=education,
                skills=skills,
                detail=detail,
            )

        except Exception as e:
            logger.debug(f"  提取候选人信息失败: {e}")
            return None

    # ===== 打招呼 =====

    def send_greeting(self, page, candidate: Candidate) -> bool:
        """点击候选人卡片打招呼（在搜索 iframe 中直接操作）

        流程：
        1. 点击卡片打开候选人详情
        2. 检查右侧是否有同事近 1.5 年内打过招呼的记录
        3. 如果有同事记录 → 关闭弹窗，跳过
        4. 如果没有 → 确认畅聊卡弹窗，完成打招呼
        """
        try:
            self.rate_limiter.wait()

            # 先关闭可能残留的弹窗
            self.close_dialog(page)
            random_delay(0.3, 0.5)

            fl = self._get_search_frame(page)
            cards = fl.locator(self.SEL_CANDIDATE_CARD)
            count = cards.count()

            # 找到匹配的候选人卡片并点击
            for i in range(count):
                card = cards.nth(i)
                card_name = card.locator(self.SEL_CANDIDATE_NAME).first
                if card_name.count() > 0:
                    n = card_name.text_content(timeout=2000).strip()
                    if n == candidate.name:
                        card.click(timeout=5000)
                        random_delay(1, 2)

                        # 检查同事打招呼记录
                        colleague_info = self._check_colleague_record(page)
                        if colleague_info:
                            logger.info(f"  ✗ 同事已触达: {candidate.name} - {colleague_info}")
                            self.close_dialog(page)
                            random_delay(0.5, 1)
                            return False

                        # 自动确认畅聊卡消耗弹窗
                        self._confirm_token_dialog(page)
                        random_delay(1, 2)

                        # token 扣减
                        if self._token_balance is not None and self._token_balance > 0:
                            self._token_balance -= 1

                        self.daily_counter.increment()
                        logger.info(f"  ✓ 已打招呼: {candidate.name} ({candidate.title} @ {candidate.company})")

                        # 关闭打招呼后弹出的简历预览弹窗
                        self.close_dialog(page)
                        random_delay(0.5, 1)

                        return True

            logger.debug(f"  未找到匹配的候选人卡片: {candidate.name}")
            return False

        except Exception as e:
            logger.error(f"  ✗ 打招呼失败 {candidate.name}: {e}")
            return False

    # ===== 未读消息检测（基于左侧菜单红色气泡） =====

    def get_unread_badge_count(self, page) -> dict:
        """检测左侧菜单各项目的未读红色气泡数量

        Returns:
            dict: {
                "chat": int,      # 沟通/聊天未读数
                "recommend": int, # 推荐未读数
                "search": int,    # 搜索未读数
                "total": int      # 总未读数
            }
        """
        try:
            result = page.evaluate("""() => {
                const badges = {};
                
                // 查找所有菜单项中的红色气泡
                // Boss直聘的未读气泡通常是 .badge, .unread-count, [class*="badge"] 等
                const menuItems = document.querySelectorAll('dl.menu-chat, dl.menu-recommend, dl.menu-geeksearch, dl.menu-prop');
                
                menuItems.forEach(item => {
                    // 获取菜单类型
                    let type = '';
                    if (item.classList.contains('menu-chat')) type = 'chat';
                    else if (item.classList.contains('menu-recommend')) type = 'recommend';
                    else if (item.classList.contains('menu-geeksearch')) type = 'search';
                    else if (item.classList.contains('menu-prop')) type = 'props';
                    
                    // 查找红色气泡
                    const badge = item.querySelector('.badge, .unread-count, .unread-badge, [class*="badge"], [class*="unread"]');
                    if (badge) {
                        const text = badge.textContent.trim();
                        const num = parseInt(text) || (text ? 1 : 0);
                        badges[type] = num;
                    } else {
                        badges[type] = 0;
                    }
                });
                
                // 计算总数
                badges.total = Object.values(badges).reduce((a, b) => a + b, 0);
                
                return badges;
            }""")
            return result or {"chat": 0, "recommend": 0, "search": 0, "props": 0, "total": 0}
        except Exception as e:
            logger.debug(f"检测未读气泡失败: {e}")
            return {"chat": 0, "recommend": 0, "search": 0, "props": 0, "total": 0}

    # ===== 巡检回复 =====

    def get_chat_badge_count(self, page) -> int:
        """获取左侧「沟通」菜单的红色气泡未读数

        这是未读消息数的权威来源，用于校准巡检结果。
        红色气泡是 dl.menu-chat 内一个显示数字的小元素。
        """
        try:
            count = page.evaluate("""() => {
                const chatMenu = document.querySelector('dl.menu-chat');
                if (!chatMenu) return 0;

                // 在菜单内查找包含纯数字的小元素（红色气泡）
                const allEls = chatMenu.querySelectorAll('*');
                for (const el of allEls) {
                    const text = el.textContent.trim();
                    if (!text || text.length > 4) continue;
                    // 跳过包含子元素文本的父节点（只看叶子节点）
                    if (el.children.length > 0) {
                        // 检查是否有子元素贡献了这段文本
                        let hasChildWithSameText = false;
                        for (const child of el.children) {
                            if (child.textContent.trim() === text) {
                                hasChildWithSameText = true;
                                break;
                            }
                        }
                        if (hasChildWithSameText) continue;
                    }
                    const num = parseInt(text);
                    if (num > 0 && String(num) === text) {
                        return num;
                    }
                }
                return 0;
            }""")
            return count if count else 999
        except Exception as e:
            logger.debug(f"获取气泡未读数失败: {e}")
            return 0

    def check_replies(self, page, unread_only: bool = True) -> list[Candidate]:
        """检查聊天列表，返回有未读消息的候选人名单

        基于左侧红色气泡数校准，确保不遗漏未读消息。
        流程：
        1. 读取气泡数作为基准
        2. 用「未读」筛选标签过滤出未读会话
        3. 滚动加载全部并与气泡数对比校准
        4. 收集候选人名单后切回全部视图
        """
        candidates = []

        page.goto(self.CHAT_URL, wait_until="domcontentloaded", timeout=20000)
        page.wait_for_selector(self.SEL_MENU_CHAT, timeout=10000)
        random_delay(3, 5)

        # Step 1: 读取左侧菜单红色气泡数作为基准
        initial_badge = self.get_chat_badge_count(page)
        logger.info(f"左侧气泡未读数: {initial_badge}")

        if unread_only and initial_badge == 0:
            logger.info("气泡显示无未读消息，跳过巡检")
            return []

        # Step 2: 点击「未读」筛选标签，只显示未读会话
        self.filter_by_label(page, "未读")
        random_delay(2, 3)

        # Step 3: 滚动加载所有未读会话
        chat_items = self._load_all_chat_items(page)
        loaded_count = len(chat_items)
        logger.info(f"「未读」筛选后加载会话: {loaded_count} 条 (气泡基准: {initial_badge})")

        # Step 4: 如果加载数量少于气泡数，尝试更多滚动
        if loaded_count < initial_badge:
            logger.warning(f"加载数 ({loaded_count}) < 气泡数 ({initial_badge})，深度加载...")
            chat_items = self._load_all_chat_items(page, max_scroll=40)
            loaded_count = len(chat_items)
            logger.info(f"深度加载后: {loaded_count} 条")

        # Step 5: 提取候选人信息
        for item in chat_items:
            try:
                inner = item.query_selector(self.SEL_CHAT_ITEM_INNER)
                if not inner:
                    continue
                name_el = inner.query_selector(self.SEL_CHAT_NAME)
                if not name_el:
                    continue
                name = name_el.text_content().strip()
                candidates.append(Candidate(
                    platform="boss",
                    platform_id=f"boss_chat_{name}",
                    name=name,
                ))
            except Exception:
                continue

        # 校准日志
        if len(candidates) < initial_badge:
            logger.warning(f"校准偏差: 找到 {len(candidates)} 条会话，气泡显示 {initial_badge}")
        else:
            logger.info(f"校准通过: {len(candidates)} 条会话 (气泡: {initial_badge})")

        # 不切回全部视图——保持「未读」筛选状态，
        # 让 enter_conversation 直接在当前筛选列表中找到并点击候选人

        return candidates

    def _clear_filter(self, page) -> None:
        """清除聊天列表筛选，回到全部视图"""
        try:
            labels = page.query_selector_all(self.SEL_CHAT_LABEL_ITEM)
            if labels:
                # 点击第一个标签（通常是「全部」）
                labels[0].click()
                random_delay(1, 2)
        except Exception:
            pass

    def _load_all_chat_items(self, page, max_scroll: int = 80) -> list:
        """滚动聊天列表加载所有会话项

        Boss直聘会话列表默认只渲染可视区域内的项目，
        需要滚动才能加载更多。反复滚动直到数量不再增加。
        """
        prev_count = 0
        for _ in range(max_scroll):
            items = page.query_selector_all(self.SEL_CHAT_ITEM)
            current_count = len(items)
            if current_count == prev_count:
                break
            prev_count = current_count
            # 滚动聊天列表容器
            try:
                page.evaluate("""() => {
                    const list = document.querySelector('.user-list, .chat-user-list, [class*="user-list"]');
                    if (list) list.scrollTop = list.scrollHeight;
                }""")
                random_delay(1, 2)
            except Exception:
                break
        return page.query_selector_all(self.SEL_CHAT_ITEM)

    def enter_conversation(self, page, candidate: Candidate) -> bool:
        """点击进入与候选人的对话"""
        try:
            # 先关闭可能存在的弹窗（如 PDF 预览）
            self.close_dialog(page)

            chat_items = page.query_selector_all(self.SEL_CHAT_ITEM)
            for item in chat_items:
                try:
                    name_el = item.query_selector(self.SEL_CHAT_NAME)
                    if name_el and candidate.name in name_el.text_content():
                        item.click()
                        random_delay(2, 3)
                        return True
                except Exception:
                    continue

            logger.debug(f"  未找到候选人的聊天: {candidate.name}")
            return False
        except Exception as e:
            logger.error(f"  进入对话失败 {candidate.name}: {e}")
            return False

    def analyze_conversation(self, page) -> dict:
        """分析当前对话的状态

        Returns:
            dict:
                has_friend_msg: 候选人是否发过消息
                has_my_msg: 我方是否发过消息（排除系统自动招呼）
                resume_request_enabled: "求简历"按钮是否可点击
                resume_file_enabled: "附件简历"按钮是否可下载
                job_name: 沟通的职位名称
        """
        state = {
            "has_friend_msg": False,
            "has_my_msg": False,
            "resume_request_enabled": False,
            "resume_file_enabled": False,
            "job_name": "",
            "last_friend_msg": "",
            "friend_messages": [],
        }

        try:
            # 检查候选人消息（并抽出文本供拒信过滤）
            friend_msgs = page.query_selector_all(self.SEL_MSG_FRIEND)
            state["has_friend_msg"] = len(friend_msgs) > 0
            texts = []
            for el in friend_msgs:
                try:
                    txt = (el.text_content() or "").strip()
                except Exception:
                    txt = ""
                if txt:
                    texts.append(txt)
            state["friend_messages"] = texts
            state["last_friend_msg"] = texts[-1] if texts else ""

            # 检查我方消息
            my_msgs = page.query_selector_all(self.SEL_MSG_MYSELF)
            state["has_my_msg"] = len(my_msgs) > 0

            # 检查"求简历"按钮状态
            # 遍历所有 .operate-btn，找到含"求简历"文本的那个
            operate_btns = page.query_selector_all(self.SEL_OPERATE_BTNS)
            for btn in operate_btns:
                text = btn.text_content().strip()
                if "求简历" in text:
                    btn_class = btn.get_attribute("class") or ""
                    state["resume_request_enabled"] = "disabled" not in btn_class
                    break

            # 检查"附件简历"按钮状态
            file_btn = page.query_selector(self.SEL_RESUME_FILE)
            if file_btn:
                btn_class = file_btn.get_attribute("class") or ""
                state["resume_file_enabled"] = "disabled" not in btn_class

            # 提取"沟通的职位"名称
            state["job_name"] = self._extract_job_name(page)

        except Exception as e:
            logger.debug(f"  分析对话状态失败: {e}")

        return state

    def _extract_job_name(self, page) -> str:
        """从当前对话中提取"沟通的职位"名称

        Boss直聘对话中会显示"沟通的职位-XXX"的系统消息卡片。
        """
        try:
            job_name = page.evaluate("""() => {
                // 方法1: 从系统消息/卡片中查找 "沟通的职位" 文本
                const items = document.querySelectorAll(
                    '.item-system, .item-resume, .message-card, [class*="message-card"]'
                );
                for (const item of items) {
                    const text = item.textContent || '';
                    const match = text.match(/沟通的职位[：:：-]\\s*(.+?)(?:\\s*$)/);
                    if (match) return match[1].trim();
                }
                // 方法2: 从卡片标题查找
                const titles = document.querySelectorAll(
                    '.message-card-top-title, [class*="card-top-title"]'
                );
                for (const el of titles) {
                    const text = el.textContent || '';
                    const match = text.match(/沟通的职位[：:：-]\\s*(.+)/);
                    if (match) return match[1].trim();
                    if (text.includes('沟通的职位')) return text.replace(/沟通的职位[：:：-]?\\s*/, '').trim();
                }
                // 方法3: 从右侧面板的职位信息提取
                const jobEl = document.querySelector('.job-detail-op .name, .job-info .name');
                if (jobEl) return jobEl.textContent.trim();
                return '';
            }""")
            return job_name or ""
        except Exception:
            return ""

    def send_reply(self, page, message: str = "您好，感谢您对岗位的关注，方便进一步沟通一下吗？") -> bool:
        """在当前对话中发送回复消息

        Args:
            page: Playwright page
            message: 回复消息内容
        """
        try:
            editor = page.query_selector(self.SEL_EDITOR_INPUT)
            if not editor:
                logger.error("  未找到消息输入框")
                return False

            # 点击输入框获取焦点
            editor.click()
            random_delay(0.3, 0.5)

            # 输入消息
            editor.fill(message)
            random_delay(0.5, 1)

            # 点击发送
            submit = page.query_selector(f"{self.SEL_SUBMIT_BTN} .submit")
            if submit:
                submit.click()
                random_delay(1, 2)
                logger.info(f"  ✓ 已发送回复: {message[:30]}...")
                return True
            else:
                # 也可以用 Enter 发送
                editor.press("Enter")
                random_delay(1, 2)
                logger.info(f"  ✓ 已发送回复(Enter): {message[:30]}...")
                return True

        except Exception as e:
            logger.error(f"  ✗ 发送回复失败: {e}")
            return False

    def request_resume(self, page, candidate: Candidate) -> bool:
        """点击"求简历"按钮请求候选人发送简历

        点击后会自动发送消息"方便发一份你的简历过来吗？"给候选人。
        候选人同意后附件简历才可下载。
        """
        try:
            # 找到"求简历"按钮
            operate_btns = page.query_selector_all(self.SEL_OPERATE_BTNS)
            resume_btn = None
            for btn in operate_btns:
                text = btn.text_content().strip()
                if "求简历" in text:
                    btn_class = btn.get_attribute("class") or ""
                    if "disabled" not in btn_class:
                        resume_btn = btn
                    break

            if not resume_btn:
                logger.debug(f"  求简历按钮不可用: {candidate.name}")
                return False

            # 点击求简历
            resume_btn.click()
            random_delay(1, 2)

            # 弹出确认框: "确定向牛人请求简历，并回复内容：'方便发一份你的简历过来吗？'"
            # 确认按钮: <span class="boss-btn-primary boss-btn">确定</span>
            confirm_btn = page.query_selector(".exchange-tooltip .boss-btn-primary")
            if confirm_btn:
                confirm_btn.click()
                random_delay(1, 2)

            logger.info(f"  ✓ 已发送求简历请求: {candidate.name}")
            return True

        except Exception as e:
            logger.error(f"  ✗ 求简历失败 {candidate.name}: {e}")
            return False

    def download_resume(self, page, candidate: Candidate, save_dir: str) -> Optional[str]:
        """下载附件简历文件

        Boss直聘的"附件简历"按钮点击后会打开 PDF 预览弹窗（dialog-wrap），
        其中包含一个 iframe 加载 PDF.js viewer，src 中含有 PDF 文件的 URL。
        我们从 iframe src 中提取 PDF URL，通过浏览器 API 直接下载。
        """
        try:
            save_path = Path(save_dir)
            save_path.mkdir(parents=True, exist_ok=True)

            # 检查附件简历按钮是否可用
            file_btn = page.query_selector(self.SEL_RESUME_FILE)
            if not file_btn or "disabled" in (file_btn.get_attribute("class") or ""):
                logger.debug(f"  附件简历不可用（候选人尚未同意）: {candidate.name}")
                return None

            # 点击打开 PDF 预览弹窗
            file_btn.click()
            random_delay(2, 3)

            # 从预览弹窗的 iframe src 中提取 PDF URL
            # iframe src 格式: /web/common/pdfjs/web/viewer.html?file=/wflow/zpgeek/download/preview4boss/...
            pdf_url = None
            dialog = page.query_selector('.dialog-wrap.active')
            if dialog:
                iframe = dialog.query_selector('iframe.attachment-iframe, iframe[class*="attachment"]')
                if iframe:
                    src = iframe.get_attribute("src") or ""
                    # 从 src 中提取 file= 参数
                    match = re.search(r'file=([^&]+)', src)
                    if match:
                        from urllib.parse import unquote
                        pdf_path = unquote(match.group(1))
                        pdf_url = f"https://www.zhipin.com{pdf_path}"

            if not pdf_url:
                logger.debug(f"  未找到 PDF URL: {candidate.name}")
                self.close_dialog(page)
                return None

            # 通过浏览器下载 PDF
            safe_name = re.sub(r'[/\\:*?"<>|]', '_', f"{candidate.name}_{candidate.title}")
            filename = f"{safe_name}.pdf"
            filepath = save_path / filename

            # 用 page.request 下载（会自动带上 cookie）
            response = page.request.get(pdf_url)
            if response.ok:
                filepath.write_bytes(response.body())
                logger.info(f"  ✓ 附件简历已下载: {filepath}")
                self.close_dialog(page)
                return str(filepath)
            else:
                logger.error(f"  下载失败 HTTP {response.status}: {candidate.name}")
                self.close_dialog(page)
                return None

        except Exception as e:
            logger.error(f"  ✗ 下载附件简历失败 {candidate.name}: {e}")
            self.close_dialog(page)
            return None

    def _confirm_token_dialog(self, page) -> None:
        """自动确认畅聊卡消耗弹窗

        点击卡片打招呼时，如果 token 不足可能弹出畅聊卡确认框，
        直接点确认继续。
        """
        try:
            # 常见确认按钮选择器
            confirm_selectors = [
                ".dialog-wrap.active .boss-btn-primary",
                ".dialog-wrap.active .btn-sure",
                ".dialog-wrap.active .btn-confirm",
                ".boss-popup .boss-btn-primary",
            ]
            for sel in confirm_selectors:
                btn = page.query_selector(sel)
                if btn and btn.is_visible():
                    btn.click()
                    logger.info("  ✓ 已确认畅聊卡消耗")
                    random_delay(0.5, 1)
                    return
        except Exception:
            pass

    # ===== 同事重复触达检测 =====

    def _apply_colleague_filter(self, page) -> None:
        """在搜索 iframe 中勾选"仅30天未和同事交换简历"筛选框

        搜索结果页有各种筛选条件（学历、经验等），其中包含一个
        "仅30天未和同事交换简历"的 checkbox，勾选后可过滤掉近期
        已被同事联系过的候选人。
        """
        try:
            fl = self._get_search_frame(page)
            random_delay(1, 2)

            # 策略1: 通过文本内容精确查找 checkbox
            checkbox = fl.locator("text=仅30天未和同事交换简历").first
            if checkbox.count() > 0:
                # 检查是否已勾选（父元素或自身是否有 checked/is-checked 类）
                is_checked = checkbox.evaluate("""el => {
                    // 检查自身或父级 checkbox 组件是否已选中
                    const parent = el.closest('.el-checkbox, .check-item, .filter-checkbox, label');
                    if (parent) {
                        return parent.classList.contains('is-checked') ||
                               parent.classList.contains('checked') ||
                               parent.querySelector('input[type=checkbox]:checked') !== null;
                    }
                    return false;
                }""")
                if not is_checked:
                    checkbox.click(timeout=3000)
                    random_delay(2, 3)
                    logger.info("  ✓ 已勾选「仅30天未和同事交换简历」")
                else:
                    logger.info("  「仅30天未和同事交换简历」已勾选")
                return

            # 策略2: 遍历所有 checkbox 类元素，按文本匹配
            selectors = [
                ".filter-checkbox",
                ".check-item",
                ".el-checkbox",
                "label:has(input[type=checkbox])",
            ]
            for sel in selectors:
                items = fl.locator(sel)
                for i in range(items.count()):
                    item = items.nth(i)
                    text = item.text_content(timeout=2000).strip()
                    if "同事" in text and "简历" in text:
                        is_checked = item.evaluate("""el => {
                            return el.classList.contains('is-checked') ||
                                   el.classList.contains('checked') ||
                                   el.querySelector('input[type=checkbox]:checked') !== null;
                        }""")
                        if not is_checked:
                            item.click(timeout=3000)
                            random_delay(2, 3)
                            logger.info(f"  ✓ 已勾选筛选框: {text}")
                        else:
                            logger.info(f"  筛选框已勾选: {text}")
                        return

            logger.debug("  未找到「同事交换简历」筛选框")
        except Exception as e:
            logger.debug(f"  勾选同事筛选框失败: {e}")

    def _check_colleague_record(self, page) -> str:
        """检查点击候选人卡片后，右侧面板是否有同事近 1.5 年内打过招呼的记录

        Boss直聘点击候选人卡片后，右侧会展示候选人详情面板，其中可能包含
        "同事沟通记录"或"公司同事已联系"等信息。

        Returns:
            同事记录描述字符串（如 "张三 2025-01-15 已沟通"），
            如果无记录或记录超过 1.5 年则返回空字符串
        """
        try:
            # 等待右侧面板加载
            random_delay(0.5, 1)

            # 使用 JS 在主页面和所有可能的面板中查找同事记录
            result = page.evaluate(r"""() => {
                const now = Date.now();
                const EIGHTEEN_MONTHS_MS = 18 * 30 * 24 * 60 * 60 * 1000;  // ~1.5年

                // 查找所有可能包含同事记录的容器
                const selectors = [
                    '.colleague-record',
                    '.colleague-info',
                    '.company-colleague',
                    '[class*="colleague"]',
                    '.contact-record',
                    '.team-record',
                    '.same-company',
                    '[class*="same-company"]',
                ];

                for (const sel of selectors) {
                    const els = document.querySelectorAll(sel);
                    for (const el of els) {
                        if (!el.textContent.trim()) continue;
                        const text = el.textContent.trim();

                        // 提取日期（格式: YYYY-MM-DD 或 YYYY.MM.DD 或 MM-DD 或 X天前/X月前）
                        const datePatterns = [
                            /(\d{4})[-./](\d{1,2})[-./](\d{1,2})/,
                            /(\d{1,2})[-./](\d{1,2})/,
                        ];

                        for (const pattern of datePatterns) {
                            const match = text.match(pattern);
                            if (match) {
                                let dateStr;
                                if (match.length >= 4) {
                                    dateStr = `${match[1]}-${match[2].padStart(2,'0')}-${match[3].padStart(2,'0')}`;
                                } else {
                                    const year = new Date().getFullYear();
                                    dateStr = `${year}-${match[1].padStart(2,'0')}-${match[2].padStart(2,'0')}`;
                                }
                                const recordDate = new Date(dateStr).getTime();
                                if (now - recordDate <= EIGHTEEN_MONTHS_MS) {
                                    return text.substring(0, 100);
                                }
                            }
                        }

                        // 匹配相对时间: "X天前", "X月前", "X周前"
                        const relMatch = text.match(/(\d+)\s*(天|月|周|小时|分钟)前/);
                        if (relMatch) {
                            const num = parseInt(relMatch[1]);
                            const unit = relMatch[2];
                            let daysAgo = 0;
                            if (unit === '天') daysAgo = num;
                            else if (unit === '周') daysAgo = num * 7;
                            else if (unit === '月') daysAgo = num * 30;
                            else if (unit === '小时' || unit === '分钟') daysAgo = 0;

                            if (daysAgo <= 548) {  // ~1.5年
                                return text.substring(0, 100);
                            }
                        }

                        // 如果有同事相关文本但无法解析日期，保守返回
                        if ((text.includes('同事') && (text.includes('沟通') || text.includes('联系') || text.includes('打招呼')))
                            || text.includes('已被同事联系')) {
                            return text.substring(0, 100);
                        }
                    }
                }

                // 额外检查：在整个右侧面板中搜索同事相关文本
                const rightPanels = document.querySelectorAll(
                    '.detail-figure, .resume-detail, .geek-detail, .right-panel, ' +
                    '.detail-content, [class*="detail-panel"], [class*="geek-card"]'
                );
                for (const panel of rightPanels) {
                    const text = panel.textContent || '';
                    // 查找 "XX(同事) 于 XXXX-XX-XX 沟通过" 类似文本
                    const colleagueMatch = text.match(
                        /(?:同事|同公司).{0,20}(?:沟通|联系|打招呼|触达|交换).{0,30}(?:\d{4}[-./]\d{1,2}[-./]\d{1,2}|\d+\s*(?:天|月|周)前)/
                    );
                    if (colleagueMatch) {
                        const snippet = colleagueMatch[0].substring(0, 100);
                        // 提取其中的日期判断是否在1.5年内
                        const dm = snippet.match(/(\d{4})[-./](\d{1,2})[-./](\d{1,2})/);
                        if (dm) {
                            const d = new Date(`${dm[1]}-${dm[2].padStart(2,'0')}-${dm[3].padStart(2,'0')}`).getTime();
                            if (now - d <= EIGHTEEN_MONTHS_MS) return snippet;
                        } else {
                            return snippet;  // 有匹配但无日期，保守返回
                        }
                    }
                }

                return '';
            }""")

            return result or ""

        except Exception as e:
            logger.debug(f"  检查同事记录失败: {e}")
            return ""

    # ===== 辅助方法 =====

    def close_dialog(self, page) -> None:
        """关闭可能存在的弹窗（如 PDF 预览弹窗）

        Boss直聘的弹窗结构: <div class="dialog-wrap active"> 内含关闭按钮
        如果没有关闭按钮，用 Escape 键关闭。
        """
        try:
            dialog = page.query_selector('.dialog-wrap.active')
            if not dialog:
                return

            # 尝试点击关闭按钮
            close_btn = dialog.query_selector('.close, .dialog-close, [class*="close"]')
            if close_btn:
                close_btn.click()
                random_delay(0.5, 1)
                return

            # 没有关闭按钮，按 Escape
            page.keyboard.press("Escape")
            random_delay(0.5, 1)
        except Exception:
            # 静默处理，不影响主流程
            try:
                page.keyboard.press("Escape")
                random_delay(0.3, 0.5)
            except Exception:
                pass

    def filter_by_label(self, page, label: str) -> None:
        """切换聊天列表筛选标签（全部/新招呼/沟通中/已获取简历等）"""
        labels = page.query_selector_all(self.SEL_CHAT_LABEL_ITEM)
        for l in labels:
            if label in l.text_content():
                l.click()
                random_delay(1, 2)
                return
        logger.debug(f"未找到标签: {label}")

    def detect_captcha(self, page) -> bool:
        """检测是否出现验证码"""
        captcha_selectors = [
            ".geetest_widget",
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
