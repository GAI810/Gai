"""环境初始化脚本 - 首次使用时运行"""

import subprocess
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent


def main():
    print("=" * 50)
    print("  Resume Hunter 环境初始化")
    print("=" * 50)

    # 1. 检查虚拟环境
    venv_dir = SKILL_DIR / ".venv"
    if venv_dir.exists():
        print("\n✓ 虚拟环境已存在")
    else:
        print("\n正在创建虚拟环境...")
        subprocess.run(["uv", "sync"], cwd=str(SKILL_DIR), check=True)
        print("✓ 虚拟环境创建完成")

    # 2. 检查 Playwright 浏览器
    print("\n检查 Playwright 浏览器...")
    result = subprocess.run(
        ["uv", "run", "playwright", "install", "chromium"],
        cwd=str(SKILL_DIR),
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        print("✓ Chromium 浏览器就绪")
    else:
        print(f"✗ 浏览器安装失败: {result.stderr}")
        sys.exit(1)

    # 3. 初始化数据目录
    data_dir = SKILL_DIR / "data"
    for d in ["browser_state/boss", "browser_state/maimai", "resumes", "logs"]:
        (data_dir / d).mkdir(parents=True, exist_ok=True)
    print("✓ 数据目录已创建")

    # 4. 初始化数据库
    sys.path.insert(0, str(SKILL_DIR / "scripts"))
    from lib.db import Database
    db = Database()
    db.close()
    print("✓ 数据库已初始化")

    # 5. 初始化配置
    from lib.config import CONFIG_PATH, load_config
    config = load_config()
    print(f"✓ 配置文件: {CONFIG_PATH}")

    print("\n" + "=" * 50)
    print("  初始化完成！")
    print("=" * 50)
    print("\n下一步：")
    print("  1. 编辑配置文件添加你的 JD 和关键词:")
    print(f"     {CONFIG_PATH}")
    print("  2. 登录 Boss直聘:")
    print("     /resume login boss")
    print("  3. 登录 脉脉:")
    print("     /resume login maimai")


if __name__ == "__main__":
    main()
