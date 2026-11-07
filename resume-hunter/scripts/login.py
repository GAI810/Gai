"""登录脚本 - 打开浏览器让用户手动登录，保存 session + fingerprint"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib.browser import BrowserManager
from lib.platforms.boss import BossPlatform
from lib.platforms.maimai import MaimaiPlatform

PLATFORMS = {
    "boss": BossPlatform,
    "maimai": MaimaiPlatform,
}

LOGIN_URLS = {
    "boss": "https://www.zhipin.com/web/user/?ka=header-login",
    "maimai": "https://maimai.cn/platform_login",
}


def main():
    parser = argparse.ArgumentParser(description="登录招聘平台并保存 session")
    parser.add_argument("--platform", "-p", required=True, choices=["boss", "maimai"],
                        help="目标平台: boss 或 maimai")
    args = parser.parse_args()

    platform_name = args.platform
    platform = PLATFORMS[platform_name]()
    login_url = LOGIN_URLS[platform_name]

    print(f"\n正在打开 {platform_name} 登录页面...")
    print("请在浏览器中完成登录操作")
    print("登录成功后，脚本会自动保存你的登录态与 fingerprint\n")

    bm = BrowserManager(headless=False)
    bm.start()

    try:
        # 先固化 / 加载 fingerprint，再开页
        fp = bm.load_or_create_fingerprint(platform_name)
        print(f"fingerprint: UA={fp['user_agent'][:50]}... viewport={fp['viewport']}")

        page = bm.new_page(platform_name)
        page.goto(login_url, wait_until="domcontentloaded", timeout=30000)

        print("等待登录中... (超时: 5 分钟)")
        print("提示: 登录完成后页面会自动跳转\n")

        start_time = time.time()
        timeout = 300

        while time.time() - start_time < timeout:
            time.sleep(3)
            if platform.is_logged_in(page):
                print("\n✓ 登录成功！")
                bm.save_session(platform_name)
                bm.save_fingerprint(platform_name)
                print("✓ Session + fingerprint 已保存，下次复用同一 UA/viewport")
                return

        print("\n✗ 登录超时，请重试")
        sys.exit(1)

    except KeyboardInterrupt:
        print("\n操作已取消")
    finally:
        bm.close()


if __name__ == "__main__":
    main()
