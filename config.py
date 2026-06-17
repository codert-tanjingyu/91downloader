"""
config.py — 全局配置模块

从 .env 文件与环境变量中读取所有可配置项，
提供整个项目统一的配置入口。
"""

import os
import logging
from pathlib import Path
from dotenv import load_dotenv

# 加载项目根目录下的 .env 文件
load_dotenv(dotenv_path=Path(__file__).parent / ".env")

# ──────────────────────────────────────────────
# 目录路径
# ──────────────────────────────────────────────
BASE_DIR: Path = Path(__file__).parent
TEMP_DIR: Path = BASE_DIR / "temp"
OUTPUT_DIR: Path = BASE_DIR / "output"

# 确保目录存在
TEMP_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

# ──────────────────────────────────────────────
# 网络 / 代理
# ──────────────────────────────────────────────
PROXY: str = os.getenv("PROXY", "")           # 留空则不使用代理

# ──────────────────────────────────────────────
# 请求头
# ──────────────────────────────────────────────
DEFAULT_USER_AGENT: str = os.getenv(
    "USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36",
)
REFERER: str = os.getenv("REFERER", "https://www.91porn.com/")
COOKIE: str = os.getenv("COOKIE", "")

# ──────────────────────────────────────────────
# 伪造 IP 头（用于绕过地理限制）
# 从以下国外 IP 段中随机选取填入 X-Forwarded-For / Client-IP
# ──────────────────────────────────────────────
FAKE_IP_RANGES: list[str] = [
    "104.16.",    # Cloudflare anycast
    "172.217.",   # Google
    "13.107.",    # Microsoft
    "151.101.",   # Fastly
    "199.232.",   # GitHub
]

# ──────────────────────────────────────────────
# 下载并发控制
# ──────────────────────────────────────────────
MAX_CONCURRENCY: int = 8          # 默认并发分片数
MIN_CONCURRENCY: int = 2          # 触发限速退避后的最低并发数
CONCURRENCY_RECOVER_STEP: int = 1 # 每次限速恢复时增加的并发数

# ──────────────────────────────────────────────
# 重试策略
# ──────────────────────────────────────────────
MAX_RETRIES: int = 5
RETRY_BASE_DELAY: float = 2.0     # 指数退避基础延迟（秒）
RETRY_MAX_DELAY: float = 60.0     # 最大单次延迟上限
JITTER_RANGE: float = 1.0         # 随机抖动范围（秒）

# ──────────────────────────────────────────────
# 限速触发阈值
# ──────────────────────────────────────────────
RATE_LIMIT_STATUS_CODES: set[int] = {429, 503}  # 触发退避的 HTTP 状态码
RATE_LIMIT_CONSECUTIVE: int = 3   # 连续多少次触发限速时降低并发

# ──────────────────────────────────────────────
# 磁盘空间预检
# ──────────────────────────────────────────────
DISK_SPACE_FACTOR: float = 2.5    # 所需空间 = 估算视频大小 × 此系数

# ──────────────────────────────────────────────
# 文件写入 chunk 大小
# ──────────────────────────────────────────────
WRITE_CHUNK_SIZE: int = 1024 * 512  # 512 KB

# ──────────────────────────────────────────────
# HTTP 超时（秒）
# ──────────────────────────────────────────────
REQUEST_TIMEOUT: int = 30
DOWNLOAD_TIMEOUT: int = 60

# ──────────────────────────────────────────────
# curl_cffi 模拟的浏览器指纹
# ──────────────────────────────────────────────
IMPERSONATE_BROWSER: str = "chrome120"

# ──────────────────────────────────────────────
# 日志
# ──────────────────────────────────────────────
LOG_LEVEL: int = logging.INFO

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
