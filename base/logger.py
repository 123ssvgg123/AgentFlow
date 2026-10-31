"""
    日志管理模块：base/logger.py

    职责：提供统一格式的 logger，同时输出到控制台与文件（logs/app.log）。
        与 FoodSafetySystem 的日志约定保持一致（同样的格式与落地路径），
        便于两个系统在同一台机器上联合排查。

    用法：
        from base.logger import logger
        logger.info("xxx")
"""
# 导入日志库
import logging
# 导入路径操作库
import os
# 导入配置对象（日志目录由配置统一给出，避免此处硬编码路径）
from base.config import config

# ============ 1. 确保日志目录存在 ============
os.makedirs(config.LOG_DIR, exist_ok=True)

# ============ 2. 创建 logger 并设定级别 ============
logger = logging.getLogger("FoodAgent")
logger.setLevel(logging.INFO)
# 避免被上游（如 uvicorn）重复添加 handler 造成日志重复输出
logger.propagate = False

# ============ 3. 日志格式 ============
formatter = logging.Formatter(
    fmt="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# ============ 4. 控制台 handler ============
console_handler = logging.StreamHandler()
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)

# ============ 5. 文件 handler（追加写，UTF-8） ============
file_handler = logging.FileHandler(config.LOG_FILE, mode="a", encoding="utf-8")
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)


if __name__ == "__main__":
    logger.info("日志模块自检：这是一条 info 日志")
    logger.warning("日志模块自检：这是一条 warning 日志")
    print("日志文件路径：", config.LOG_FILE)
