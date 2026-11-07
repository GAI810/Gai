"""状态文件的原子读写与跨进程锁

解决：
- 写入被截断（崩溃/并发）导致 JSON 损坏
- 多个进程（监听 / 邮箱 / 周维护）同时读改写丢失更新

用法：
    from lib.state_store import read_json, update_json, atomic_write_json

    data = read_json(path, default={})          # 损坏时自动备份并返回 default
    update_json(path, lambda d: d.update(...))  # 加锁 + 原子写
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable


@contextmanager
def file_lock(path: Path, timeout: float = 10.0):
    """对 path 加跨进程独占锁（用独立的 .lock 文件）"""
    lock_path = Path(str(path) + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "a+")
    deadline = time.time() + timeout
    try:
        while True:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.time() > deadline:
                    raise TimeoutError(f"获取文件锁超时: {lock_path}")
                time.sleep(0.05)
        yield
    finally:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """同目录临时文件 + fsync + os.replace，保证要么旧内容要么新内容"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def atomic_write_json(path: Path, data: Any) -> None:
    atomic_write_bytes(path, json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8"))


def read_json(path: Path, default: Any = None) -> Any:
    """读 JSON；文件损坏时备份为 .corrupt-<ts> 并返回 default"""
    path = Path(path)
    if not path.exists():
        return default if default is not None else {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        backup = path.with_name(f"{path.name}.corrupt-{int(time.time())}")
        try:
            shutil.copy2(path, backup)
        except OSError:
            pass
        return default if default is not None else {}


def update_json(path: Path, mutator: Callable[[dict], None], default: Any = None) -> dict:
    """加锁读-改-写"""
    path = Path(path)
    with file_lock(path):
        data = read_json(path, default if default is not None else {})
        if not isinstance(data, dict):
            data = default if default is not None else {}
        mutator(data)
        atomic_write_json(path, data)
        return data
