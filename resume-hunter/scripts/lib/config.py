"""配置管理模块"""

import os
from pathlib import Path

import yaml

# 项目根目录
SKILL_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = SKILL_DIR / "data"
BROWSER_STATE_DIR = DATA_DIR / "browser_state"
RESUMES_DIR = DATA_DIR / "resumes"
LOGS_DIR = DATA_DIR / "logs"
LOCKS_DIR = DATA_DIR / "locks"
DB_PATH = DATA_DIR / "db.sqlite"
CONFIG_PATH = DATA_DIR / "config.yaml"
JD_CACHE_FILE = DATA_DIR / "jd_cache.json"

# 确保目录存在
for d in [DATA_DIR, BROWSER_STATE_DIR, RESUMES_DIR, LOGS_DIR, LOCKS_DIR,
          BROWSER_STATE_DIR / "boss", BROWSER_STATE_DIR / "maimai"]:
    d.mkdir(parents=True, exist_ok=True)

# 默认配置（二期风控：硬顶 50；脉脉默认关闭且未验证）
DEFAULT_CONFIG = {
    "platforms": {
        "boss": {
            "enabled": True,
            "daily_limit": 50,
            "daily_hard_cap": 50,
            "base_url": "https://www.zhipin.com",
        },
        "maimai": {
            "enabled": False,
            "verified": False,
            "daily_limit": 50,
            "daily_hard_cap": 50,
            "base_url": "https://maimai.cn",
        },
    },
    "daily_hard_cap": 50,
    "schedule": {
        "morning": {"start": "10:00", "end": "12:00"},
        "afternoon": {"start": "17:00", "end": "19:00"},
    },
    "behavior": {
        "min_delay": 30,
        "max_delay": 90,
        "break_after": 25,
        "break_duration_min": 60,
        "break_duration_max": 180,
        "circuit_max_failures": 3,
    },
    "messages": {
        "reply_template": "您好，感谢您对岗位的关注，方便进一步沟通一下吗？",
        "reject_keywords": [
            "不感兴趣",
            "先不考虑",
            "不考虑",
            "已找到",
            "找到工作",
            "已经找到",
            "有工作了",
            "勿扰",
            "别再联系",
            "请勿打扰",
            "谢谢不了",
            "不用了",
            "暂不考虑",
            "机会不合适",
            "不合适",
            "已入职",
            "已有 offer",
            "已有offer",
        ],
    },
    "jobs": [],
    "resume_output": {
        "base_dir": str(RESUMES_DIR),
        "excel_summary": True,
    },
}


def load_config() -> dict:
    """加载配置文件，不存在则创建默认配置"""
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            user_config = yaml.safe_load(f) or {}
        config = _deep_merge(DEFAULT_CONFIG, user_config)
    else:
        config = _deep_merge({}, DEFAULT_CONFIG)
        save_config(config)
    return config


def save_config(config: dict) -> None:
    """保存配置到文件"""
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.dump(config, f, default_flow_style=False, allow_unicode=True)


def _deep_merge(base: dict, override: dict) -> dict:
    """深度合并两个字典"""
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result
