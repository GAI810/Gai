"""人类行为模拟模块 - 随机延迟、鼠标移动、滚动等"""

import logging
import random
import time

logger = logging.getLogger("resume-hunter")


def random_delay(min_s: float = 1.0, max_s: float = 3.0) -> None:
    """随机等待一段时间"""
    delay = random.uniform(min_s, max_s)
    logger.debug(f"等待 {delay:.1f}s")
    time.sleep(delay)


def human_click(page, selector: str) -> None:
    """模拟人类点击 - 带随机偏移"""
    element = page.wait_for_selector(selector, timeout=10000)
    box = element.bounding_box()
    if not box:
        element.click()
        return

    # 点击位置加随机偏移
    x = box["x"] + box["width"] / 2 + random.randint(-3, 3)
    y = box["y"] + box["height"] / 2 + random.randint(-3, 3)

    # 先移动鼠标再点击
    page.mouse.move(x, y)
    random_delay(0.1, 0.3)
    page.mouse.click(x, y)
    random_delay(0.3, 0.8)


def human_type(page, selector: str, text: str) -> None:
    """模拟人类打字 - 逐字符输入"""
    page.click(selector)
    random_delay(0.2, 0.5)
    for char in text:
        page.keyboard.type(char)
        time.sleep(random.uniform(0.05, 0.15))
    random_delay(0.3, 0.6)


def random_scroll(page, times: int = 0) -> None:
    """随机滚动页面"""
    if times == 0:
        times = random.randint(2, 4)
    for _ in range(times):
        distance = random.randint(200, 500)
        page.mouse.wheel(0, distance)
        random_delay(0.5, 1.5)


def take_break() -> None:
    """模拟休息 - 每操作一批人后调用"""
    duration = random.uniform(60, 180)
    logger.info(f"休息 {duration:.0f}s ...")
    time.sleep(duration)


def should_take_break(count: int, break_after: int = 25) -> bool:
    """判断是否该休息了"""
    return count > 0 and count % break_after == 0
