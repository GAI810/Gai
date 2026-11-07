"""对照实验：简历匹配预测 vs 招聘系统里实际投递的岗位

纯只读：不创建人才、不创建投递。输出表格 + 命中率。

用法：
  .venv/bin/python scripts/evaluate_matching.py [简历目录]
"""

import json
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import hire_import as h  # noqa: E402
from lib.resume_parser import parse_resume  # noqa: E402


def load_log_map() -> dict:
    """文件名 -> talent_id（来自历史入库日志）"""
    mapping = {}
    if not h.IMPORT_LOG_FILE.exists():
        return mapping
    for line in h.IMPORT_LOG_FILE.read_text(encoding="utf-8").splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("file") and e.get("talent_id"):
            mapping[Path(e["file"]).name] = e["talent_id"]
    return mapping


def load_auto_app_ids() -> set:
    """我们自己创建的投递 ID（评估时排除，避免循环验证）"""
    ids = set()
    if not h.IMPORT_LOG_FILE.exists():
        return ids
    for line in h.IMPORT_LOG_FILE.read_text(encoding="utf-8").splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("app_id"):
            ids.add(str(e["app_id"]))
    return ids


FAMILIES = [
    ("产品经理", ("产品经理",)),
    ("算法", ("算法", "AI Infra", "机器学习")),
    ("设计", ("设计",)),
    ("运营", ("运营", "社群")),
    ("研发", ("开发", "工程师", "全栈", "前端", "后端", "服务端", "测试")),
    ("HR", ("HR", "招聘", "人事")),
    ("合规", ("合规", "法务")),
    ("资料库", ("资料库", "资料")),
    ("数据", ("数据",)),
    ("客服", ("客服",)),
]


def job_family(title: str) -> str:
    """岗位大类：产品经理 / 运营 / 设计 / 研发 / 算法 / HR ..."""
    t = title or ""
    for fam, kws in FAMILIES:
        if any(kw.lower() in t.lower() for kw in kws):
            return fam
    return "其他"


_TITLE_CACHE: dict[str, str] = {}


def resolve_title(job_id: str, title_map: dict) -> str:
    """把岗位 id 解析成标题（含已关闭岗位）"""
    if job_id in title_map:
        return title_map[job_id]
    if job_id in _TITLE_CACHE:
        return _TITLE_CACHE[job_id]
    r = h.lark_json(["api", "GET", f"/open-apis/hire/v1/jobs/{job_id}", "--as", "bot"])
    j = (r.get("data") or {}).get("job") or {}
    title = j.get("title") or job_id
    _TITLE_CACHE[job_id] = title
    return title


def actual_jobs(talent_id: str, title_map: dict) -> list[str]:
    """人才在飞书里的实际投递岗位（标题）"""
    ids = []
    r = h.lark_json(["api", "GET", "/open-apis/hire/v1/applications", "--as", "bot",
                     "--params", json.dumps({"talent_id": talent_id, "page_size": 20})])
    auto = load_auto_app_ids()
    for aid in ((r.get("data") or {}).get("items") or []):
        if str(aid) in auto:
            continue
        r2 = h.lark_json(["api", "GET", f"/open-apis/hire/v1/applications/{aid}", "--as", "bot"])
        job = ((r2.get("data") or {}).get("application") or {}).get("job_id")
        if job:
            ids.append(str(job))
    return [resolve_title(j, title_map) for j in ids]


def predict(path: Path, all_jobs: list[dict]):
    """与生产完全一致的决策（复用 hire_import.decide_job）"""
    text = unicodedata.normalize("NFKC", parse_resume(path) or "")
    name = h.name_from_filename(path.name) or h.guess_name(text, path.stem)
    applied = h.extract_boss_job(path.name)
    years = h.experience_years(text)

    decision = h.decide_job(text, name, applied_job=applied, llm_match=h.llm_enabled())
    pred = None
    if decision.get("job_id"):
        pred = {"id": decision["job_id"], "title": decision.get("title", "")}
    return {"name": name, "applied": applied, "years": years, "pred": pred,
            "via": decision.get("via", "none"), "conf": decision.get("confidence"),
            "pool": 0}


def main() -> int:
    folder = Path(sys.argv[1]) if len(sys.argv) > 1 else \
        Path.home() / "Downloads/招聘简历/待整理下载/已入库"
    files = sorted(p for p in folder.glob("*.pdf"))
    if not files:
        print(f"（{folder} 里没有 PDF）")
        return 1

    all_jobs = h.all_jobs_cached()
    title_map = {j["id"]: j["title"] for j in all_jobs}
    log_map = load_log_map()

    rows = []
    for f in files:
        r = predict(f, all_jobs)
        tid = log_map.get(f.name)
        actual = actual_jobs(tid, title_map) if tid else []
        pred_title = r["pred"]["title"] if r["pred"] else "（无匹配）"
        hit = bool(r["pred"] and any(pred_title == a for a in actual))
        fam = job_family(pred_title) if r["pred"] else ""
        fam_hit = bool(r["pred"] and any(job_family(a) == fam for a in actual))
        rows.append({"file": f.name, **r, "actual": actual, "hit": hit,
                     "fam": fam, "fam_hit": fam_hit})
        print(f"处理: {f.name[:36]:<38} -> {pred_title[:18]:<20} "
              f"[{r['via']}] 实际: {actual}")

    print("\n| # | 简历 | 姓名 | Boss投递 | 预测岗位 | 路径 | 置信度 | 实际(飞书) | 命中 |")
    print("|---|---|---|---|---|---|---|---|---|")
    for i, r in enumerate(rows, 1):
        pred = r["pred"]["title"] if r["pred"] else "（无匹配）"
        conf = f"{r['conf']:.2f}" if r["conf"] is not None else "-"
        actual = "、".join(r["actual"]) if r["actual"] else "（无记录）"
        mark = "✅" if r["hit"] else ("≈" if r["fam_hit"] else "—")
        print(f"| {i} | {r['file'][:24]} | {r['name']} | {r['applied'] or '-'} | "
              f"{pred} | {r['via']} | {conf} | {actual} | {mark} |")

    judged = [r for r in rows if r["actual"]]
    hits = [r for r in judged if r["hit"]]
    fam_hits = [r for r in judged if r["fam_hit"]]
    print(f"\n可对照样本: {len(judged)} / {len(rows)}")
    if judged:
        print(f"精确命中: {len(hits)} → {len(hits)/len(judged)*100:.0f}%")
        print(f"大类命中: {len(fam_hits)} → {len(fam_hits)/len(judged)*100:.0f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
