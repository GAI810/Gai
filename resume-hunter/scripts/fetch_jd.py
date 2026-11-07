"""一次性抓取所有岗位 JD 并保存到本地

通过 Boss直聘内部 API 获取职位列表和 JD 内容：
1. /wapi/zpjob/job/chatted/jobList - 获取有沟通的职位列表（含加密 ID）
2. /wapi/zpjob/job/edit?encJobId=XXX - 获取职位编辑数据（含完整 JD: job.postDescription）

这种方式不需要访问公开详情页，风控风险极低。

用法：
  cd ~/.claude/skills/resume-hunter && uv run python scripts/fetch_jd.py
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib.browser import BrowserManager
from lib.config import DATA_DIR
from lib.human_behavior import random_delay

JD_FILE = DATA_DIR / "jd_cache.json"


def main():
    # 如果已有缓存，先加载
    existing_cache = {}
    if JD_FILE.exists():
        with open(JD_FILE, "r", encoding="utf-8") as f:
            existing_cache = json.load(f)
        print(f"已有缓存: {len(existing_cache)} 个职位")

    bm = BrowserManager()
    bm.start()

    try:
        page = bm.new_page("boss")
        page.goto("https://www.zhipin.com/web/geek/chat", wait_until="domcontentloaded", timeout=20000)
        page.wait_for_selector("dl.menu-chat", timeout=10000)
        time.sleep(3)
        print("✓ 已登录\n")

        # 1. 获取有沟通的职位列表
        print("===== 获取职位列表 =====")
        resp = page.request.get("https://www.zhipin.com/wapi/zpjob/job/chatted/jobList")
        if not resp.ok:
            print(f"API 请求失败: HTTP {resp.status}")
            sys.exit(1)

        api_data = resp.json()
        if api_data.get("code") != 0:
            print(f"API 返回错误: {api_data.get('message')}")
            sys.exit(1)

        jobs = api_data.get("zpData", [])
        print(f"找到 {len(jobs)} 个有沟通的职位:")
        for j in jobs:
            print(f"  {j['jobName']}  (encId: {j['encryptJobId']})")

        if not jobs:
            print("没有找到职位数据")
            sys.exit(1)

        # 2. 逐个通过编辑 API 获取 JD
        jd_cache = {}

        for job in jobs:
            encrypt_id = job["encryptJobId"]
            job_name = job["jobName"]
            job_id = str(job["jobId"])
            salary = job.get("salaryDesc", "")
            address = job.get("address", "")
            cache_key = job.get("description", f"{job_name} _ {address} {salary}")

            print(f"\n--- {job_name} ---")

            # 调用编辑 API 获取完整职位数据
            edit_url = f"https://www.zhipin.com/wapi/zpjob/job/edit?encJobId={encrypt_id}"
            try:
                resp = page.request.get(edit_url)
                if not resp.ok:
                    print(f"  HTTP {resp.status}")
                    continue

                edit_data = resp.json()
                if edit_data.get("code") != 0:
                    print(f"  API 错误: {edit_data.get('message')}")
                    continue

                zp_data = edit_data["zpData"]
                job_info = zp_data.get("job", {})
                jd_text = job_info.get("postDescription", "")

                if jd_text:
                    print(f"  ✓ JD 长度: {len(jd_text)} 字")
                    print(f"  预览: {jd_text[:120]}...")

                    # 提取额外信息
                    skill_list = zp_data.get("skillList", [])
                    skills = [s.get("name", "") for s in skill_list if isinstance(s, dict)] if isinstance(skill_list, list) else []

                    jd_cache[cache_key] = {
                        "job_id": job_id,
                        "encrypt_id": encrypt_id,
                        "job_name": job_name,
                        "jd_text": jd_text,
                        "salary": salary,
                        "address": address,
                        "experience": job_info.get("experienceName", ""),
                        "degree": job_info.get("degreeName", ""),
                        "skills": skills,
                        "online_status": job.get("jobOnlineStatus", -1),
                    }
                else:
                    print(f"  ✗ JD 为空")
                    jd_cache[cache_key] = {
                        "job_id": job_id,
                        "encrypt_id": encrypt_id,
                        "job_name": job_name,
                        "jd_text": "",
                    }

            except Exception as e:
                print(f"  错误: {e}")

            random_delay(1, 2)

        # 3. 保存到本地
        with open(JD_FILE, "w", encoding="utf-8") as f:
            json.dump(jd_cache, f, ensure_ascii=False, indent=2)

        print(f"\n===== 结果汇总 =====")
        print(f"✓ JD 已保存到: {JD_FILE}")
        print(f"  共 {len(jd_cache)} 个职位:")
        for name, info in jd_cache.items():
            jd_len = len(info.get("jd_text", ""))
            status = "✓" if jd_len > 50 else "✗"
            print(f"  {status} {info.get('job_name', name)}: {jd_len} 字")

    except Exception as e:
        print(f"错误: {e}")
        import traceback
        traceback.print_exc()
    finally:
        bm.save_session("boss")
        bm.close()


if __name__ == "__main__":
    main()
