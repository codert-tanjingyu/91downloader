"""
utils/http_client.py — 统一 HTTP 会话管理

职责：
  - 使用 curl_cffi 构建模拟浏览器 TLS/JA3 指纹的异步 HTTP 客户端
  - 注入代理、统一请求头、随机伪造 X-Forwarded-For / Client-IP
  - 提供全局单例 Session 的异步上下文管理器
"""

import logging
import random
import contextlib
from typing import AsyncGenerator

from curl_cffi.requests import AsyncSession

import config

logger = logging.getLogger(__name__)


def _random_fake_ip() -> str:
    """从预设的国外 IP 段中随机生成一个伪造的公网 IP。"""
    prefix = random.choice(config.FAKE_IP_RANGES)
    suffix = f"{random.randint(1, 254)}.{random.randint(1, 254)}"
    return prefix + suffix


def _build_headers(extra: dict | None = None) -> dict:
    """构造基础请求头，可通过 extra 参数追加/覆盖特定字段。"""
    fake_ip = _random_fake_ip()
    headers = {
        "User-Agent": config.DEFAULT_USER_AGENT,
        "Referer": config.REFERER,
        "X-Forwarded-For": fake_ip,
        "Client-IP": fake_ip,
        "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
        "Accept": "*/*",
        "Connection": "keep-alive",
    }
    if config.COOKIE:
        headers["Cookie"] = config.COOKIE
    if extra:
        headers.update(extra)
    return headers


@contextlib.asynccontextmanager
async def get_session(
    extra_headers: dict | None = None,
    proxy: str | None = None,
) -> AsyncGenerator[AsyncSession, None]:
    """
    异步上下文管理器，提供配置好的 curl_cffi AsyncSession。

    所有模块应通过此接口获取 Session，以保持：
      - TLS/JA3 指纹一致（模拟 Chrome）
      - Cookie 状态统一
      - 代理配置统一

    用法：
        async with get_session() as session:
            resp = await session.get(url)

    参数：
        extra_headers: 追加或覆盖的请求头字典
        proxy: 覆盖 config.PROXY 的代理地址（可选）
    """
    effective_proxy = proxy or config.PROXY or None
    headers = _build_headers(extra_headers)

    proxies: dict | None = None
    if effective_proxy:
        proxies = {"http": effective_proxy, "https": effective_proxy}
        logger.debug("使用代理: %s", effective_proxy)

    session = AsyncSession(
        impersonate=config.IMPERSONATE_BROWSER,
        headers=headers,
        proxies=proxies,
        timeout=config.REQUEST_TIMEOUT,
        verify=False,  # 忽略 SSL 证书错误（部分 CDN 证书异常）
    )
    try:
        yield session
    finally:
        await session.close()
        logger.debug("HTTP 会话已关闭")


async def fetch_text(
    url: str,
    session: AsyncSession,
    extra_headers: dict | None = None,
) -> str:
    """
    GET 请求并返回文本内容。

    参数：
        url: 目标 URL
        session: 已初始化的 AsyncSession
        extra_headers: 附加请求头

    返回：
        响应正文文本

    异常：
        RuntimeError: 非 2xx 状态码
    """
    resp = await session.get(
        url,
        headers=extra_headers or {},
        timeout=config.REQUEST_TIMEOUT,
    )
    if resp.status_code != 200:
        raise RuntimeError(
            f"HTTP {resp.status_code} — 请求失败: {url}"
        )
    return resp.text


async def fetch_bytes(
    url: str,
    session: AsyncSession,
    extra_headers: dict | None = None,
) -> bytes:
    """
    GET 请求并返回二进制内容（用于下载 TS 分片和 Key）。

    参数：
        url: 目标 URL
        session: 已初始化的 AsyncSession
        extra_headers: 附加请求头

    返回：
        响应正文字节数据

    异常：
        RuntimeError: 非 2xx 状态码
    """
    resp = await session.get(
        url,
        headers=extra_headers or {},
        timeout=config.DOWNLOAD_TIMEOUT,
    )
    if resp.status_code != 200:
        raise RuntimeError(
            f"HTTP {resp.status_code} — 下载失败: {url}"
        )
    return resp.content
