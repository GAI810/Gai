"""候选人预筛选评分模块

两阶段筛选：
1. 规则过滤（rule_filter）：快速排除明显不匹配的候选人，零成本
2. API 精筛（api_evaluate）：调用 Claude API 对候选人做精细评分

设计原则：宁可放过，不可误杀。规则过滤只排除硬伤（如学历严重不符），
API 精筛负责精细判断匹配度。
"""

import json
import logging
import os
import re

logger = logging.getLogger("resume-hunter")

# 学历等级映射
EDUCATION_LEVELS = {
    "博士": 5,
    "硕士": 4,
    "本科": 3,
    "大专": 2,
    "高中": 1,
    "中专": 1,
}

# 年龄上限（超过则过滤）
MAX_AGE = 35

# 公司黑名单（当前/历史公司包含这些关键词则过滤）。
# 公开快照不写真实公司名；需要时只在本机改这个列表，不要提交。
COMPANY_BLACKLIST: list[str] = []


def _parse_education_level(text: str) -> int:
    """从文本中解析学历等级，返回等级数值，无法解析返回 0"""
    for edu, level in EDUCATION_LEVELS.items():
        if edu in text:
            return level
    return 0


def _parse_experience_years(text: str) -> tuple[float, float]:
    """从文本中解析工作年限范围

    返回 (min_years, max_years)，无法解析返回 (-1, -1)

    示例输入: "3-5年经验", "5年以上", "应届", "1年以内", "在校"
    """
    if not text:
        return (-1, -1)

    if "应届" in text or "在校" in text:
        return (0, 1)

    # "3-5年", "3~5年"
    match = re.search(r"(\d+)\s*[-~～]\s*(\d+)\s*年", text)
    if match:
        return (float(match.group(1)), float(match.group(2)))

    # "5年以上", "10年以上"
    match = re.search(r"(\d+)\s*年以上", text)
    if match:
        return (float(match.group(1)), 99)

    # "1年以内", "3年以下"
    match = re.search(r"(\d+)\s*年以[内下]", text)
    if match:
        return (0, float(match.group(1)))

    # "5年"
    match = re.search(r"(\d+)\s*年", text)
    if match:
        y = float(match.group(1))
        return (y, y)

    return (-1, -1)


class CandidateScorer:
    """候选人评分器

    Args:
        jd_text: JD 全文
        jd_info: JD 结构化信息（来自 jd_cache.json），含 skills/experience/degree 等
        search_keywords: 搜索时使用的关键词列表（用于规则匹配）
    """

    def __init__(self, jd_text: str = "", jd_info: dict = None, search_keywords: list[str] = None):
        self.jd_text = jd_text
        self.jd_info = jd_info or {}
        self.search_keywords = search_keywords or []

        # 从 JD info 中提取要求
        self.required_education = _parse_education_level(self.jd_info.get("degree", ""))
        self.jd_skills = self.jd_info.get("skills", [])

        # 从 JD info 中解析经验要求
        exp_text = self.jd_info.get("experience", "")
        self.required_exp_min, self.required_exp_max = _parse_experience_years(exp_text)

    def rule_filter(self, candidate) -> tuple[bool, str]:
        """规则预筛选

        Returns:
            (是否通过, 原因说明)

        设计：只排除硬伤，无法判断时默认通过
        """
        # 0. 公司黑名单（不触达本公司/关联公司的候选人）
        candidate_bg = f"{candidate.company} {candidate.detail} {candidate.experience}".lower()
        for blocked in COMPANY_BLACKLIST:
            if blocked.lower() in candidate_bg:
                return (False, f"公司黑名单: 背景含'{blocked}'")

        # 1. 年龄检查
        age_match = re.search(r"(\d+)\s*岁", candidate.experience)
        if age_match:
            age = int(age_match.group(1))
            if age > MAX_AGE:
                return (False, f"年龄超限: {age}岁 (上限{MAX_AGE}岁)")

        # 2. 学历检查（如果 JD 有学历要求且候选人信息可解析）
        if self.required_education > 0:
            candidate_edu = _parse_education_level(
                candidate.education + " " + candidate.experience
            )
            # 只有确定低于要求时才过滤（candidate_edu=0 表示无法判断，放行）
            if candidate_edu > 0 and candidate_edu < self.required_education:
                return (False, f"学历不符: 要求{self._edu_name(self.required_education)}及以上")

        # 2. 经验年限检查
        if self.required_exp_min >= 0:
            cand_min, cand_max = _parse_experience_years(candidate.experience)
            if cand_min >= 0 and cand_max >= 0:
                # 候选人的最大年限低于 JD 的最低要求（留 1 年余量避免误杀）
                if cand_max < self.required_exp_min - 1 and self.required_exp_min > 1:
                    return (False, f"经验不足: 要求{self.required_exp_min}年+，候选人约{cand_max}年")

        # 3. 关键词匹配（宽松：title/skills/detail/experience 中任一命中即可）
        if self.jd_skills or self.search_keywords:
            all_keywords = set(self.jd_skills + self.search_keywords)
            candidate_text = " ".join([
                candidate.title,
                candidate.detail,
                candidate.experience,
                candidate.education,
                " ".join(candidate.skills) if candidate.skills else "",
            ]).lower()

            # 双向匹配：关键词在候选人文本中，或候选人关键字段在关键词中
            # 例如 "社区产品经理" 匹配 title="产品经理"（反向包含）
            hit = False
            for kw in all_keywords:
                if not kw:
                    continue
                kw_lower = kw.lower()
                # 正向：关键词在候选人文本中
                if kw_lower in candidate_text:
                    hit = True
                    break
                # 反向：候选人的 title 在关键词中（如 "产品经理" in "社区产品经理"）
                if candidate.title and candidate.title.lower() in kw_lower:
                    hit = True
                    break

            if not hit and all_keywords:
                return (False, "关键词未命中")

        return (True, "规则通过")

    def api_evaluate(self, candidate) -> dict:
        """调用 Claude API 进行精细评分

        Returns:
            {"score": 1-10, "reason": "...", "recommend": True/False}
            出错时返回 {"score": -1, "reason": "API 调用失败", "recommend": True}
        """
        try:
            import anthropic
        except ImportError:
            logger.warning("anthropic 包未安装，跳过 API 评分")
            return {"score": -1, "reason": "anthropic 未安装", "recommend": True}

        # 构建候选人摘要
        skills_str = ", ".join(candidate.skills) if candidate.skills else "无"
        candidate_summary = (
            f"姓名: {candidate.name}\n"
            f"当前职位: {candidate.title}\n"
            f"公司: {candidate.company}\n"
            f"经验标签: {candidate.experience}\n"
            f"学历: {candidate.education or '未知'}\n"
            f"技能: {skills_str}\n"
            f"自我介绍: {candidate.detail or '无'}"
        )

        prompt = (
            "你是一个招聘助手。根据候选人的摘要信息判断其与岗位的匹配度。\n\n"
            f"岗位 JD：\n{self.jd_text}\n\n"
            f"候选人信息：\n{candidate_summary}\n\n"
            "请返回 JSON：{\"score\": 1-10, \"reason\": \"一句话说明\", \"recommend\": true/false}\n"
            "score >= 6 表示建议打招呼。只返回 JSON，不要其他内容。"
        )

        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            logger.warning("未设置 ANTHROPIC_API_KEY，跳过 API 评分")
            return {"score": -1, "reason": "未配置 API key", "recommend": True}

        try:
            client = anthropic.Anthropic(
                api_key=api_key,
                base_url=os.environ.get("ANTHROPIC_BASE_URL") or None,
            )
            response = client.messages.create(
                model=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-20250514"),
                max_tokens=200,
                messages=[{"role": "user", "content": prompt}],
            )

            result_text = response.content[0].text.strip()
            # 提取 JSON（兼容 markdown code block）
            json_match = re.search(r'\{[^{}]+\}', result_text)
            if json_match:
                result = json.loads(json_match.group())
                score = result.get("score", -1)
                reason = result.get("reason", "")
                recommend = result.get("recommend", score >= 6)
                return {"score": score, "reason": reason, "recommend": recommend}
            else:
                logger.warning(f"API 返回格式异常: {result_text[:100]}")
                return {"score": -1, "reason": "返回格式异常", "recommend": True}

        except Exception as e:
            logger.error(f"API 评分失败: {e}")
            return {"score": -1, "reason": f"API 错误: {e}", "recommend": True}

    def _edu_name(self, level: int) -> str:
        """学历等级转名称"""
        for name, lv in EDUCATION_LEVELS.items():
            if lv == level:
                return name
        return "未知"
