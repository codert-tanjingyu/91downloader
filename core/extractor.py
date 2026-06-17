"""
core/extractor.py — 页面解析与 M3U8 URL 提取模块

职责：
  - 静态路径（默认）：用 curl_cffi 下载页面 HTML，通过正则提取密文和 Key，
    调用 decoder.str_decode 还原真实 M3U8 URL。
  - 动态路径（可选，--use-playwright）：启动 Playwright 无头 Chromium，
    监听网络响应，直接拦截加载后的 M3U8 URL，无需破解混淆逻辑。

注意：
  Playwright 仅在用户通过 --use-playwright 参数显式开启时才会被导入和使用。
  未安装 playwright 包或未执行 `playwright install chromium` 时，
  静态模式依然正常运行。
"""

import re
import logging
from urllib.parse import urlparse, urljoin

from curl_cffi.requests import AsyncSession
from bs4 import BeautifulSoup

from core.decoder import str_decode, try_extract_m3u8_from_plaintext
from utils.http_client import fetch_text

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────
# 正则模式库（覆盖常见混淆变体）
# ──────────────────────────────────────────────────────────

# 匹配形如：strencode("BASE64_DATA", "KEY") 的调用
_RE_STRENCODE = re.compile(
    r'strencode\s*\(\s*["\']([A-Za-z0-9+/=]+)["\']\s*,\s*["\']([^"\']+)["\']\s*\)',
    re.IGNORECASE,
)

# 匹配形如：var player_data={"src":"URL"} 的 JSON 赋值（部分站点直接明文输出）
_RE_PLAYER_DATA_JSON = re.compile(
    r'var\s+player_data\s*=\s*(\{.*?\})\s*;',
    re.DOTALL,
)

# 匹配 HTML5 video src 或 source src（最简单的情况）
_RE_VIDEO_SRC = re.compile(
    r'<(?:video|source)[^>]+src=["\']([^"\']+\.m3u8[^"\']*)["\']',
    re.IGNORECASE,
)

# 直接包含 .m3u8 的脚本变量赋值
_RE_DIRECT_M3U8 = re.compile(
    r'["\']?(https?://[^\s\'"<>]+\.m3u8[^\s\'"<>]*)["\']?',
    re.IGNORECASE,
)


async def extract_m3u8_url(
    page_url: str,
    session: AsyncSession,
    use_playwright: bool = False,
) -> str:
    """
    提取目标视频页面中的 M3U8 播放列表 URL。

    参数：
        page_url: 视频页面完整 URL
        session: 已配置的 curl_cffi AsyncSession（静态模式使用）
        use_playwright: 是否启用 Playwright 动态拦截（默认 False）

    返回：
        M3U8 URL 字符串

    异常：
        ValueError: 无法从页面提取到有效的 M3U8 URL
    """
    if use_playwright:
        logger.info("使用 Playwright 动态模式提取 M3U8 URL ...")
        return await _extract_via_playwright(page_url)

    logger.info("使用静态模式提取 M3U8 URL: %s", page_url)
    return await _extract_static(page_url, session)


# ──────────────────────────────────────────────────────────
# 内部实现：静态解析
# ──────────────────────────────────────────────────────────

async def _extract_static(page_url: str, session: AsyncSession) -> str:
    """静态 HTTP 页面解析，依次尝试多种提取策略。"""
    html = await fetch_text(page_url, session)
    logger.debug("页面 HTML 获取成功，长度: %d", len(html))

    # 策略 1：strencode 混淆模式
    m3u8_url = _try_strencode(html, page_url)
    if m3u8_url:
        return m3u8_url

    # 策略 2：player_data JSON 明文嵌入
    m3u8_url = _try_player_data_json(html)
    if m3u8_url:
        return m3u8_url

    # 策略 3：HTML5 video/source 标签
    m3u8_url = _try_video_tag(html)
    if m3u8_url:
        return m3u8_url

    # 策略 4：全局脚本正则扫描
    m3u8_url = _try_direct_scan(html)
    if m3u8_url:
        return m3u8_url

    raise ValueError(
        f"无法从页面提取 M3U8 URL，请检查页面结构或尝试 --use-playwright 模式。\n"
        f"页面地址: {page_url}"
    )


def _try_strencode(html: str, base_url: str) -> str | None:
    """尝试通过 strencode(密文, Key) 模式解密还原 M3U8 URL。"""
    matches = _RE_STRENCODE.findall(html)
    if not matches:
        logger.debug("未找到 strencode 调用")
        return None

    for encrypted, key in matches:
        try:
            plaintext = str_decode(encrypted, key)
            logger.debug("strencode 解密成功，明文: %s", plaintext[:80])
            url = try_extract_m3u8_from_plaintext(plaintext)
            if url:
                return _ensure_absolute(url, base_url)
        except ValueError as exc:
            logger.warning("strencode 解密失败（跳过）: %s", exc)

    return None


def _try_player_data_json(html: str) -> str | None:
    """尝试从 var player_data={...} 中提取 src 字段。"""
    import json

    match = _RE_PLAYER_DATA_JSON.search(html)
    if not match:
        return None

    try:
        data = json.loads(match.group(1))
        src = data.get("src") or data.get("url") or data.get("hls_url")
        if src and ".m3u8" in src:
            logger.info("从 player_data JSON 提取到 M3U8: %s", src)
            return src
    except json.JSONDecodeError:
        logger.debug("player_data JSON 解析失败")

    return None


def _try_video_tag(html: str) -> str | None:
    """尝试从 HTML5 <video> / <source> 标签提取 .m3u8 src。"""
    match = _RE_VIDEO_SRC.search(html)
    if match:
        url = match.group(1)
        logger.info("从 video/source 标签提取到 M3U8: %s", url)
        return url
    return None


def _try_direct_scan(html: str) -> str | None:
    """在所有 <script> 标签内容中全局扫描 .m3u8 URL。"""
    soup = BeautifulSoup(html, "lxml")
    for script in soup.find_all("script"):
        text = script.get_text() or ""
        match = _RE_DIRECT_M3U8.search(text)
        if match:
            url = match.group(1)
            logger.info("从脚本全局扫描提取到 M3U8: %s", url)
            return url
    return None


def _ensure_absolute(url: str, base_url: str) -> str:
    """若 url 为相对路径，则基于 base_url 补全为绝对路径。"""
    if url.startswith("http"):
        return url
    return urljoin(base_url, url)


# ──────────────────────────────────────────────────────────
# 内部实现：Playwright 动态拦截（可选）
# ──────────────────────────────────────────────────────────

async def _extract_via_playwright(page_url: str) -> str:
    """
    使用 Playwright 无头 Chromium 加载页面，
    监听网络响应并拦截第一个 .m3u8 请求 URL。

    注意：
      调用前需确保已执行 `playwright install chromium`。
      若 playwright 包未安装，本函数会抛出清晰的 ImportError 提示。
    """
    try:
        from playwright.async_api import async_playwright, Error as PlaywrightError
    except ImportError as exc:
        raise ImportError(
            "Playwright 未安装，请执行：pip install playwright && playwright install chromium"
        ) from exc

    intercepted_url: list[str] = []

    try:
        async with async_playwright() as pw:
            try:
                browser = await pw.chromium.launch(headless=True)
            except PlaywrightError as exc:
                err_msg = str(exc)
                if "Executable doesn't exist" in err_msg or "executable" in err_msg.lower():
                    raise RuntimeError(
                        "Playwright Chromium 浏览器内核未安装！\n\n"
                        "  请在当前 Python 环境中执行以下命令安装：\n"
                        "    playwright install chromium\n\n"
                        "  如果使用的是 conda 环境，请先激活环境再执行上述命令。\n\n"
                        "  如果不需要 Playwright，请去掉命令行中的 --use-playwright 参数，\n"
                        "  程序会自动使用静态解析模式（无需安装浏览器）。"
                    ) from exc
                raise

            context = await browser.new_context()
            page = await context.new_page()

            async def on_response(response):
                if ".m3u8" in response.url and not intercepted_url:
                    intercepted_url.append(response.url)
                    logger.info("Playwright 拦截到 M3U8 URL: %s", response.url)

            page.on("response", on_response)

            logger.info("Playwright 正在加载页面: %s", page_url)
            await page.goto(page_url, wait_until="networkidle", timeout=60_000)

            await browser.close()

    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"Playwright 运行时发生异常: {exc}") from exc

    if not intercepted_url:
        raise ValueError(
            f"Playwright 未能拦截到 M3U8 URL，页面可能使用了其他加密方式。\n"
            f"页面地址: {page_url}"
        )

    return intercepted_url[0]
