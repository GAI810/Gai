"""浏览器会话管理 + 反检测模块 + fingerprint 固化"""

from __future__ import annotations

import json
import logging
import os
import random
from pathlib import Path
from typing import Optional, TYPE_CHECKING

from .config import BROWSER_STATE_DIR

if TYPE_CHECKING:
    from playwright.sync_api import Browser, BrowserContext, Page, Playwright

logger = logging.getLogger("resume-hunter")

# 真实 Chromium User-Agent（与 fingerprint 对齐；登录后固化，不随机漂移）
DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
DEFAULT_VIEWPORT = {"width": 1440, "height": 900}

# 反检测 JS 脚本
STEALTH_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => false });
window.chrome = { runtime: {} };
Object.defineProperty(navigator, 'plugins', {
    get: () => [
        { name: 'Chrome PDF Plugin', description: 'Portable Document Format' },
        { name: 'Chrome PDF Viewer', description: '' },
        { name: 'Native Client', description: '' }
    ]
});
Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });
delete navigator.__proto__.webdriver;
"""


class BrowserManager:
    """管理 Playwright 浏览器实例和 session 持久化"""

    def __init__(self, headless: bool = False):
        self.headless = bool(headless)
        self._playwright: Optional["Playwright"] = None
        self._browser: Optional["Browser"] = None
        self._contexts: dict[str, "BrowserContext"] = {}
        self._fingerprints: dict[str, dict] = {}

    def start(self) -> None:
        """启动 Playwright"""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:
            raise RuntimeError(
                "未安装 playwright。请执行: pip install 'resume-hunter[browser]' "
                "或 .venv/bin/pip install playwright && playwright install chromium"
            ) from e
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(
            headless=self.headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-first-run",
                "--no-default-browser-check",
            ],
        )
        logger.info(f"浏览器已启动 (headless={self.headless})")

    def get_context(self, platform: str) -> "BrowserContext":
        """获取或创建平台对应的浏览器上下文（复用 fingerprint）"""
        if platform in self._contexts:
            return self._contexts[platform]

        state_file = self._state_file(platform)
        storage_state = str(state_file) if state_file.exists() else None
        fp = self.load_or_create_fingerprint(platform)
        self._fingerprints[platform] = fp

        context = self._browser.new_context(
            storage_state=storage_state,
            user_agent=fp["user_agent"],
            locale=fp.get("locale", "zh-CN"),
            timezone_id=fp.get("timezone_id", "Asia/Shanghai"),
            viewport=fp["viewport"],
            extra_http_headers={
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
        )
        context.add_init_script(STEALTH_SCRIPT)

        self._contexts[platform] = context
        if storage_state:
            logger.info(f"[{platform}] 已加载保存的登录态")
        else:
            logger.info(f"[{platform}] 无保存的登录态，需要登录")
        logger.info(
            f"[{platform}] fingerprint UA={fp['user_agent'][:40]}... "
            f"viewport={fp['viewport']}"
        )
        return context

    def new_page(self, platform: str) -> "Page":
        context = self.get_context(platform)
        return context.new_page()

    def save_session(self, platform: str) -> None:
        """保存 session：目录 0700、文件 0600、先写临时再 os.replace。"""
        if platform not in self._contexts:
            return
        state = self._contexts[platform].storage_state()
        state_file = self._state_file(platform)
        platform_dir = state_file.parent
        platform_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(platform_dir, 0o700)
        except OSError:
            pass

        tmp = platform_dir / f".state.json.tmp.{os.getpid()}"
        payload = json.dumps(state, indent=2, ensure_ascii=False)
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, state_file)
        try:
            os.chmod(state_file, 0o600)
        except OSError:
            pass
        logger.info(f"[{platform}] session 已保存到 {state_file} (mode 600)")

    def has_session(self, platform: str) -> bool:
        return self._state_file(platform).exists()

    def fingerprint_path(self, platform: str) -> Path:
        return BROWSER_STATE_DIR / platform / "fingerprint.json"

    def load_or_create_fingerprint(self, platform: str) -> dict:
        path = self.fingerprint_path(platform)
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if "user_agent" in data and "viewport" in data:
                    return data
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"[{platform}] fingerprint 损坏，将重建: {e}")
        fp = {
            "user_agent": DEFAULT_UA,
            "viewport": dict(DEFAULT_VIEWPORT),
            "locale": "zh-CN",
            "timezone_id": "Asia/Shanghai",
        }
        self.save_fingerprint(platform, fp)
        return fp

    def save_fingerprint(self, platform: str, fp: Optional[dict] = None) -> Path:
        """固化 fingerprint 到 browser_state/<p>/fingerprint.json（0600）。"""
        data = fp or self._fingerprints.get(platform) or {
            "user_agent": DEFAULT_UA,
            "viewport": dict(DEFAULT_VIEWPORT),
            "locale": "zh-CN",
            "timezone_id": "Asia/Shanghai",
        }
        path = self.fingerprint_path(platform)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(path.parent, 0o700)
        except OSError:
            pass
        tmp = path.parent / f".fingerprint.json.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        self._fingerprints[platform] = data
        logger.info(f"[{platform}] fingerprint 已保存 {path}")
        return path

    def close(self, keep_open: bool = False) -> None:
        if keep_open:
            print("\n浏览器保持打开中，按回车键关闭...")
            try:
                input()
            except (EOFError, KeyboardInterrupt):
                pass
        for platform, ctx in self._contexts.items():
            try:
                ctx.close()
            except Exception:
                pass
        self._contexts.clear()
        if self._browser:
            self._browser.close()
        if self._playwright:
            self._playwright.stop()
        logger.info("浏览器已关闭")

    def _state_file(self, platform: str) -> Path:
        return BROWSER_STATE_DIR / platform / "state.json"

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *args):
        self.close()
