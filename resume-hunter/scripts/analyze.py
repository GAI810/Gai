"""简历 AI 分析脚本

通过 Claude 对已下载的简历进行结构化分析和打标签。
分析结果写入 DB 并生成 Excel 汇总报表。

设计为在 Claude Code Skill 内调用：
- --list: 列出待分析简历
- (默认): 输出简历文件路径 + 对应 JD，由 Claude 直接读取 PDF 分析
- --save: 将分析结果 JSON 写入数据库
- --export: 生成 Excel 汇总报表
"""

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib.config import DATA_DIR, RESUMES_DIR, load_config
from lib.db import STATUS_ANALYZED, STATUS_RESUME_DOWNLOADED, Database
from lib.resume_parser import parse_resume

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("resume-hunter")

JD_CACHE_FILE = DATA_DIR / "jd_cache.json"
TARGET_COMPANIES_FILE = DATA_DIR / "target_companies.json"


def load_target_companies() -> list[dict]:
    """加载对标公司名单"""
    if TARGET_COMPANIES_FILE.exists():
        with open(TARGET_COMPANIES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def load_jd_cache() -> dict:
    """加载本地 JD 缓存"""
    if JD_CACHE_FILE.exists():
        with open(JD_CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def find_jd_for_candidate(candidate: dict, jd_cache: dict) -> tuple[str, str]:
    """根据候选人的 job_name 查找对应的 JD

    Returns:
        (job_name, jd_text): 职位名称和 JD 内容
    """
    job_name = candidate.get("job_name", "")
    if not job_name:
        return "", ""

    # 精确匹配
    if job_name in jd_cache:
        return job_name, jd_cache[job_name].get("jd_text", "")

    # 模糊匹配：job_name 可能是 "产品经理（增长工具）" 而缓存 key 是带下划线的全名
    for cache_key, cache_val in jd_cache.items():
        # 缓存中的 job_name 字段（去掉下划线后缀的短名）
        cached_short = cache_val.get("job_name", "")
        if cached_short and (job_name in cached_short or cached_short in job_name):
            return cache_key, cache_val.get("jd_text", "")
        # 直接在 key 中查找
        if job_name in cache_key or cache_key in job_name:
            return cache_key, cache_val.get("jd_text", "")

    return job_name, ""


def generate_excel_report(db: Database, output_path: str) -> None:
    """生成 Excel 汇总报表"""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    wb = Workbook()
    ws = wb.active
    ws.title = "简历分析汇总"

    # 表头
    headers = ["姓名", "平台", "沟通职位", "当前职位", "公司", "匹配度", "标签", "亮点", "风险点", "一句话总结", "简历路径"]
    header_fill = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
    header_font = Font(color="FFFFFF", bold=True, size=11)

    for col, header in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=header)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center")

    # 获取已分析的候选人
    analyzed = []
    for platform in ("boss", "maimai"):
        analyzed.extend(db.get_candidates_by_status(platform, STATUS_ANALYZED))

    # 按匹配度降序排列
    analyzed.sort(key=lambda c: c.get("match_score", 0) or 0, reverse=True)

    for row_idx, candidate in enumerate(analyzed, 2):
        analysis = {}
        if candidate.get("analysis_json"):
            try:
                analysis = json.loads(candidate["analysis_json"])
            except json.JSONDecodeError:
                pass

        ws.cell(row=row_idx, column=1, value=candidate.get("name", ""))
        ws.cell(row=row_idx, column=2, value=candidate.get("platform", ""))
        ws.cell(row=row_idx, column=3, value=candidate.get("job_name", ""))
        ws.cell(row=row_idx, column=4, value=analysis.get("current_role", candidate.get("title", "")))
        ws.cell(row=row_idx, column=5, value=candidate.get("company", ""))
        ws.cell(row=row_idx, column=6, value=candidate.get("match_score", 0))
        ws.cell(row=row_idx, column=7, value=", ".join(analysis.get("tags", [])))
        ws.cell(row=row_idx, column=8, value=", ".join(analysis.get("highlights", [])))
        ws.cell(row=row_idx, column=9, value=", ".join(analysis.get("risks", [])))
        ws.cell(row=row_idx, column=10, value=analysis.get("summary", ""))
        ws.cell(row=row_idx, column=11, value=candidate.get("resume_path", ""))

    # 调整列宽
    col_widths = [12, 8, 18, 20, 20, 8, 30, 40, 40, 40, 50]
    for col, width in enumerate(col_widths, 1):
        ws.column_dimensions[chr(64 + col) if col <= 26 else "A" + chr(64 + col - 26)].width = width

    wb.save(output_path)
    logger.info(f"Excel 报表已生成: {output_path}")


def main():
    parser = argparse.ArgumentParser(description="分析已下载的简历")
    parser.add_argument("--jd", "-j", default="",
                        help="JD 文本，用于匹配度评估（会覆盖自动查找的 JD）")
    parser.add_argument("--export", "-e", action="store_true",
                        help="生成 Excel 汇总报表")
    parser.add_argument("--list", "-l", action="store_true",
                        help="仅列出待分析的简历")
    parser.add_argument("--save", "-s", default="",
                        help="保存分析结果 JSON（格式: candidate_id:json_string）")
    args = parser.parse_args()

    db = Database()
    today = datetime.now().strftime("%Y-%m-%d")

    # 保存分析结果
    if args.save:
        try:
            cid_str, json_str = args.save.split(":", 1)
            cid = int(cid_str)
            analysis = json.loads(json_str)
            match_score = analysis.get("match_score", 0)
            tags = analysis.get("tags", [])
            db.mark_analyzed(cid, analysis, match_score, tags)
            print(f"✓ 已保存候选人 {cid} 的分析结果 (匹配度: {match_score})")
        except Exception as e:
            print(f"✗ 保存失败: {e}")
        db.close()
        return

    # 收集待分析的简历
    pending = []
    for platform in ("boss", "maimai"):
        pending.extend(db.get_candidates_by_status(platform, STATUS_RESUME_DOWNLOADED))

    # 加载 JD 缓存
    jd_cache = load_jd_cache()
    if jd_cache:
        print(f"已加载 JD 缓存: {len(jd_cache)} 个职位")

    if args.list:
        print(f"\n待分析简历: {len(pending)} 份\n")
        for c in pending:
            job_name = c.get("job_name", "")
            job_label = f" [职位: {job_name}]" if job_name else ""
            print(f"  [{c['platform']}] {c['name']} - {c['title']} @ {c['company']}{job_label}")
            print(f"    ID: {c['id']}  路径: {c['resume_path']}")
        db.close()
        return

    if not pending and not args.export:
        print("没有待分析的简历")
        db.close()
        return

    # 输出简历信息 + 对应 JD，由 Claude 直接读取 PDF 文件进行分析
    if pending:
        # 加载对标公司名单
        target_companies = load_target_companies()

        print(f"\n待分析简历: {len(pending)} 份")
        print(f"Claude 请依次读取以下 PDF 文件并分析：\n")

        # 输出对标公司名单
        if target_companies:
            print("=== 对标公司名单（有这些公司经历的候选人应提高权重）===")
            for tc in target_companies:
                print(f"  {tc['registered_name']} ({tc['product_name']})")
            print()

        for c in pending:
            resume_path = c.get("resume_path", "")
            if not resume_path or not Path(resume_path).exists():
                logger.warning(f"简历文件不存在: {resume_path}")
                continue

            print(f"--- 候选人 {c['id']} ---")
            print(f"姓名: {c['name']}")
            print(f"平台: {c['platform']}")
            print(f"简历路径: {resume_path}")

            # 查找对应 JD
            if args.jd:
                # 用户指定的 JD 优先
                print(f"JD: (用户指定)")
            else:
                matched_job, jd_text = find_jd_for_candidate(c, jd_cache)
                if jd_text:
                    print(f"沟通职位: {matched_job}")
                    print(f"JD 内容:\n{jd_text}\n")
                elif c.get("job_name"):
                    print(f"沟通职位: {c['job_name']} (JD 未缓存，请先运行 fetch_jd.py)")
                else:
                    print(f"沟通职位: 未知")
            print()

        if args.jd:
            print(f"\n--- 用户指定 JD ---\n{args.jd}\n")

        print("请对每份简历基于对应的 JD 输出 JSON 分析结果，然后用以下命令保存：")
        print("cd ~/.claude/skills/resume-hunter && uv run python scripts/analyze.py --save \"<ID>:<JSON>\"")
        print()

    # 导出 Excel
    if args.export:
        report_path = RESUMES_DIR / f"summary_{today}.xlsx"
        generate_excel_report(db, str(report_path))
        print(f"\n✓ Excel 报表: {report_path}")

    db.close()


if __name__ == "__main__":
    main()
