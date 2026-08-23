"""MonoX 日志系统配置。

设计：
- 默认输出到 stderr（保持现有行为，不破坏 `run.py` 现有 stdout 协议）。
- 可选：写到文件，按日期分（`logs/monox-YYYY-MM-DD.log`）。
- 控制台和文件用同一 format，方便关联。

启用方法：
  setup_logging(log_dir=Path(".monox/logs"))   # 写到 .monox/logs/monox-2026-08-23.log
  setup_logging()                              # 只输出到 stderr（默认）

为什么单独一个模块：
  `run.py` 和 channel `__main__.py` 各自有 `logging.basicConfig`，加 handler
  时可能撞名（同一 logger 多次 add handler → 重复日志）。集中一个入口避免这坑。
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path

# 单一日志格式：timestamp [monox:logger.name] LEVEL message
# 跟 run.py 现有保持一致，让用户 / grep 习惯不被打断。
LOG_FORMAT = "%(asctime)s [monox:%(name)s] %(levelname)s %(message)s"


def setup_logging(
    log_dir: Path | None = None,
    level: int = logging.INFO,
    also_stderr: bool = True,
) -> Path | None:
    """配置 MonoX 日志。

    Args:
        log_dir: 写入日志的目录。None = 不写文件（只 stderr）。
                 给路径则创建（不存在自动 mkdir），按当天日期写：
                 `{log_dir}/monox-YYYY-MM-DD.log`。
        level: 日志级别，默认 INFO。
        also_stderr: 是否同时输出到 stderr（默认 True；调试时设 False 静默 stderr）。

    Returns:
        写入的日志文件路径（log_dir 为 None 时返回 None）。
    """
    root = logging.getLogger()
    root.setLevel(level)

    # 清掉 run.py / 各 channel basicConfig 已经加的旧 handler，避免重复日志。
    for h in list(root.handlers):
        root.removeHandler(h)

    formatter = logging.Formatter(LOG_FORMAT)

    # 1) stderr（可选）
    if also_stderr:
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(formatter)
        root.addHandler(sh)

    # 2) 文件（按日期分；不存在自动建）
    out_path: Path | None = None
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        today = datetime.now().strftime("%Y-%m-%d")
        out_path = log_dir / f"monox-{today}.log"
        fh = logging.FileHandler(out_path, encoding="utf-8")
        fh.setFormatter(formatter)
        root.addHandler(fh)

    return out_path