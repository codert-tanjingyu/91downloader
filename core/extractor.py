"""
core/extractor.py — 页面解析与 M3U8 URL 提取模块

职责：
  - 静态路径（默认）：用 curl_cffi 下载页面 HTML，通过正则提取密文和 Key，
    调用 decoder.str_decode 还原真实 M3U8 URL。
  - 动态路径（可选，--use-playwright）：启动 Playwright 无头 Chromium，
    监听网络响应（含 Content-Type 检测），自动点击播放按钮触发 HLS 请求，
    直接拦截加载后的真实 M3U8 URL，无需破解混淆逻辑。

注意：
  Playwright 仅在用户通过 --use-playwright 参数显式开启时才会被导入和使用。
  未安装 playwright 包或未执行 `playwright install chromium` 时，
  静态模式依然正常运行。
"""

import asyncio
import json
import re
import logging
from urllib.parse import urljoin

from curl_cffi.requests import AsyncSession
from bs4 import BeautifulSoup

from core.decoder import str_decode, try_extract_m3u8_from_plaintext
from utils.http_client import fetch_text

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────
# HLS 响应 Content-Type 集合
# ──────────────────────────────────────────────────────────
_HLS_CONTENT_TYPES: set[str] = {
    "application/x-mpegurl",
    "application/vnd.apple.mpegurl",
    "audio/mpegurl",
    "audio/x-mpegurl",
}

# ──────────────────────────────────────────────────────────
# 正则模式库
# ──────────────────────────────────────────────────────────

# 匹配 strencode("BASE64", "KEY") 调用
_RE_STRENCODE = re.compile(
    r'strencode\s*\(\s*["\']([A-Za-z0-9+/=]+)["\']\s*,\s*["\']([^"\']+)["\']\s*\)',
    re.IGNORECASE,
)

# 匹配 var player_data={...} JSON 赋值（部分站点明文嵌入）
_RE_PLAYER_DATA_JSON = re.compile(
    r'var\s+player_data\s*=\s*(\{.*?\})\s*[;\n]',
    re.DOTALL,
)

# 91porn 特有：var vurl = "..." / var url = "..." / var src = "..."
_RE_VAR_URL = re.compile(
    r'var\s+(?:vurl|hlsurl|hls_url|m3u8url|url|src)\s*=\s*["\']([^"\']+\.m3u8[^"\']*)["\']',
    re.IGNORECASE,
)

# 91porn 特有：src:"..." 或 source:"..." 或 file:"..." 属性赋值
_RE_PLAYER_ATTR = re.compile(
    r'(?:src|source|file|hls)\s*:\s*["\']([^"\']+\.m3u8[^"\']*)["\']',
    re.IGNORECASE,
)

# HTML5 video/source 标签
_RE_VIDEO_SRC = re.compile(
    r'<(?:video|source)[^>]+src=["\']([^"\']+\.m3u8[^"\']*)["\']',
    re.IGNORECASE,
)

# 全局脚本内 .m3u8 URL 扫描
_RE_DIRECT_M3U8 = re.compile(
    r'["\']?(https?://[^\s\'"<>]+\.m3u8[^\s\'"<>]*)["\']?',
    re.IGNORECASE,
)

# 播放按钮选择器列表（按优先级依次尝试）
_PLAY_BUTTON_SELECTORS = [
    "button.vjs-big-play-button",          # Video.js
    ".play-button",
    ".btn-play",
    "#player .play",
    "div.play_btn",
    "div[class*='play']",
    ".dplayer-play-icon",                  # DPlayer
    ".jw-icon-display",                    # JW Player
    "video",                               # 直接点击 video 元素
]


# ──────────────────────────────────────────────────────────
# 公共入口
# ──────────────────────────────────────────────────────────

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
# 静态解析
# ──────────────────────────────────────────────────────────

async def _extract_static(page_url: str, session: AsyncSession) -> str:
    """静态 HTTP 页面解析，依次尝试多种提取策略。"""
    html = await fetch_text(page_url, session)
    logger.debug("页面 HTML 获取成功，长度: %d", len(html))

    strategies = [
        ("strencode XOR 混淆",    lambda: _try_strencode(html, page_url)),
        ("player_data JSON",       lambda: _try_player_data_json(html)),
        ("var url/vurl 赋值",      lambda: _try_var_url(html)),
        ("播放器属性 src/file",    lambda: _try_player_attr(html)),
        ("HTML5 video/source 标签",lambda: _try_video_tag(html)),
        ("脚本全局扫描",           lambda: _try_direct_scan(html)),
    ]

    for name, fn in strategies:
        try:
            result = fn()
        except Exception as exc:
            logger.debug("策略「%s」执行异常（跳过）: %s", name, exc)
            continue
        if result:
            logger.info("策略「%s」提取成功: %s", name, result)
            return result
        logger.debug("策略「%s」未命中", name)

    raise ValueError(
        "静态解析失败：无法从页面提取到 M3U8 URL。\n"
        "建议：\n"
        "  1. 确认 .env 中已配置有效的 COOKIE（从浏览器导出）\n"
        "  2. 确认代理配置正确（访问该站需要代理时）\n"
        "  3. 尝试添加 --use-playwright 参数改用动态拦截模式\n"
        f"  页面地址: {page_url}"
    )


def _try_strencode(html: str, base_url: str) -> str | None:
    """尝试通过 strencode(密文, Key) 模式解密还原 M3U8 URL。"""
    matches = _RE_STRENCODE.findall(html)
    if not matches:
        return None

    for encrypted, key in matches:
        try:
            plaintext = str_decode(encrypted, key)
            logger.debug("strencode 解密成功，明文片段: %s", plaintext[:120])
            url = try_extract_m3u8_from_plaintext(plaintext)
            if url:
                return _ensure_absolute(url, base_url)
            # 明文可能是 JSON，继续尝试从中提取
            url = _extract_m3u8_from_json_str(plaintext)
            if url:
                return _ensure_absolute(url, base_url)
        except ValueError as exc:
            logger.warning("strencode 解密失败（跳过）: %s", exc)

    return None


def _try_player_data_json(html: str) -> str | None:
    """尝试从 var player_data={...} 中提取 src/url/hls_url 字段。"""
    match = _RE_PLAYER_DATA_JSON.search(html)
    if not match:
        return None
    try:
        data = json.loads(match.group(1))
        for key in ("src", "url", "hls_url", "hlsUrl", "m3u8", "file"):
            val = data.get(key, "")
            if val and ".m3u8" in val:
                return val
    except json.JSONDecodeError:
        pass
    return None


def _try_var_url(html: str) -> str | None:
    """匹配 var vurl/url/src = "...m3u8..." 赋值语句。"""
    match = _RE_VAR_URL.search(html)
    return match.group(1) if match else None


def _try_player_attr(html: str) -> str | None:
    """匹配播放器配置对象中的 src/file/hls 属性值。"""
    match = _RE_PLAYER_ATTR.search(html)
    return match.group(1) if match else None


def _try_video_tag(html: str) -> str | None:
    """尝试从 HTML5 <video> / <source> 标签提取 .m3u8 src。"""
    match = _RE_VIDEO_SRC.search(html)
    return match.group(1) if match else None


def _try_direct_scan(html: str) -> str | None:
    """在所有 <script> 标签内容中全局扫描 .m3u8 URL。"""
    soup = BeautifulSoup(html, "lxml")
    for script in soup.find_all("script"):
        text = script.get_text() or ""
        match = _RE_DIRECT_M3U8.search(text)
        if match:
            return match.group(1)
    return None


def _extract_m3u8_from_json_str(text: str) -> str | None:
    """尝试将文本解析为 JSON，从中提取 m3u8 URL。"""
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            for key in ("src", "url", "hls", "hls_url", "hlsUrl", "m3u8", "file", "source"):
                val = data.get(key, "")
                if isinstance(val, str) and ".m3u8" in val:
                    return val
    except (json.JSONDecodeError, TypeError):
        pass
    return None


def _ensure_absolute(url: str, base_url: str) -> str:
    """若 url 为相对路径，则基于 base_url 补全为绝对路径。"""
    if url.startswith("http"):
        return url
    return urljoin(base_url, url)


# ──────────────────────────────────────────────────────────
# Playwright 动态拦截（可选）
# ──────────────────────────────────────────────────────────

def _is_hls_response(url: str, content_type: str) -> bool:
    """
    判断一个网络响应是否为 HLS 播放列表。

    检测两个维度：
      1. URL 中包含 .m3u8 字符串
      2. Content-Type 为标准 HLS MIME 类型

    两个条件满足其一即判定为 HLS。
    """
    if ".m3u8" in url:
        return True
    ct = content_type.lower().split(";")[0].strip()
    return ct in _HLS_CONTENT_TYPES


async def _extract_via_playwright(page_url: str) -> str:
    """
    使用 Playwright 无头 Chromium 加载页面提取 M3U8 URL。

    策略（按优先级）：
      1. 页面 load 后直接读 DOM 中 <source> / <video> 的 src 属性
         —— 91porn 的 strencode JS 在内联脚本执行阶段就已写入 DOM，
            无需等待网络请求，这是最快最可靠的方式。
      2. 若 DOM 未命中，点击播放按钮后再次轮询 DOM，
         同时保留网络响应拦截作为兜底。

    注意：调用前需确保已执行 `playwright install chromium`。
    """
    try:
        from playwright.async_api import async_playwright, Error as PlaywrightError
    except ImportError as exc:
        raise ImportError(
            "Playwright 未安装，请执行：pip install playwright && playwright install chromium"
        ) from exc

    # 从 DOM 中读取播放地址的 JS 片段（m3u8 优先，mp4 兜底）
    _JS_READ_DOM = """() => {
        const selectors = [
            'source[src*=".m3u8"]',
            'video[src*=".m3u8"]',
            'source[src*=".mp4"]',
            'video[src*=".mp4"]',
            'video source[src]',
            'source[src]',
        ];
        for (const sel of selectors) {
            const el = document.querySelector(sel);
            if (el) {
                const src = el.src || el.getAttribute('src') || '';
                if (src && src.startsWith('http')) return src;
            }
        }
        return '';
    }"""

    intercepted_url: list[str] = []

    try:
        async with async_playwright() as pw:
            # ── 启动浏览器 ──
            try:
                browser = await pw.chromium.launch(
                    headless=True,
                    args=[
                        "--no-sandbox",
                        "--disable-setuid-sandbox",
                        "--disable-blink-features=AutomationControlled",
                    ],
                )
            except PlaywrightError as exc:
                err_msg = str(exc)
                if "Executable doesn't exist" in err_msg or "executable" in err_msg.lower():
                    raise RuntimeError(
                        "Playwright Chromium 浏览器内核未安装！\n\n"
                        "  请在当前 Python 环境中执行：\n"
                        "    playwright install chromium\n\n"
                        "  如不需要 Playwright，去掉 --use-playwright 参数即可。"
                    ) from exc
                raise

            # ── 创建上下文（模拟真实浏览器） ──
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/125.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 720},
                java_script_enabled=True,
            )
            page = await context.new_page()

            # ── 网络响应拦截（兜底备用）──
            async def on_response(response):
                if intercepted_url:
                    return
                url = response.url
                try:
                    ct = response.headers.get("content-type", "")
                except Exception:
                    ct = ""
                if _is_hls_response(url, ct):
                    intercepted_url.append(url)
                    logger.info("✅ 网络拦截到 HLS 播放列表: %s", url)

            page.on("response", on_response)

            # ── 加载页面，等待内联脚本执行完毕 ──
            logger.info("正在加载页面 ...")
            await page.goto(page_url, wait_until="load", timeout=60_000)
            logger.debug("页面 load 事件已触发")

            # ── 优先：直接从 DOM 读取（strencode 已在内联阶段写入）──
            logger.info("尝试从 DOM 读取播放地址 ...")
            dom_url = await page.evaluate(_JS_READ_DOM)
            if dom_url and dom_url.startswith("http"):
                logger.info("✅ 从 DOM 读取到播放地址: %s", dom_url)
                await browser.close()
                return dom_url

            # ── DOM 未命中：尝试点击播放按钮，轮询 DOM + 网络拦截 ──
            logger.info("DOM 未命中，尝试点击播放按钮 ...")
            for selector in _PLAY_BUTTON_SELECTORS:
                try:
                    element = page.locator(selector).first
                    if await element.count() > 0:
                        await element.click(timeout=3_000)
                        logger.debug("已点击播放按钮: %s", selector)
                        break
                except Exception as e:
                    logger.debug("选择器 %s 点击失败: %s", selector, e)

            # 点击后轮询（最多 10 秒），DOM 和网络拦截都算成功
            for _ in range(20):
                dom_url = await page.evaluate(_JS_READ_DOM)
                if dom_url and dom_url.startswith("http"):
                    logger.info("✅ 点击后从 DOM 读取到播放地址: %s", dom_url)
                    await browser.close()
                    return dom_url
                if intercepted_url:
                    logger.info("✅ 网络拦截兜底成功")
                    await browser.close()
                    return intercepted_url[0]
                await asyncio.sleep(0.5)

            await browser.close()

    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"Playwright 运行时发生异常: {exc}") from exc

    raise ValueError(
        "Playwright 未能获取到 HLS 播放列表 URL。\n\n"
        "可能原因：\n"
        "  1. 该视频需要登录/Cookie 才能播放 → 在 .env 中配置 COOKIE\n"
        "  2. 视频播放器使用了非标准 HLS 分发方式\n"
        "  3. 网络超时，未能完成加载 → 检查代理配置\n\n"
        f"页面地址: {page_url}"
    )
