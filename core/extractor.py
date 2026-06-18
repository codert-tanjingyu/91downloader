"""
core/extractor.py — 页面解析与视频 URL 提取模块

职责：
  - 静态路径（默认）：用 curl_cffi 下载页面 HTML，通过正则提取密文和 Key，
    调用 decoder.str_decode 还原真实视频 URL。
  - 动态路径（可选，--use-playwright）：启动 Playwright Chromium，
    用全新 context 两次访问目标页面：第一次让页面 JS 写入 cookie，
    第二次携带 cookie 后 DOM 中 <source src> 即为真实视频地址。

注意：
  Playwright 仅在用户通过 --use-playwright 参数显式开启时才会被导入和使用。
  未安装 playwright 包或未执行 `playwright install chromium` 时，
  静态模式依然正常运行。
"""

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


# ──────────────────────────────────────────────────────────
# 公共入口
# ──────────────────────────────────────────────────────────

async def extract_m3u8_url(
    page_url: str,
    session: AsyncSession,
    use_playwright: bool = False,
    headed: bool = False,
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
        mode = "有头（可见窗口）" if headed else "无头"
        logger.info("使用 Playwright %s 模式提取 M3U8 URL ...", mode)
        return await _extract_via_playwright(page_url, headed=headed)

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
# Playwright 动态提取（可选）
# ──────────────────────────────────────────────────────────

async def _extract_via_playwright(page_url: str, headed: bool = False) -> str:
    """
    使用 Playwright Chromium 加载页面提取视频 URL。

    策略：
      1. 全新 context（零 cookie），第一次 goto 让页面 JS 写入 cookie
      2. 同一 page 再次 goto，这次请求携带 cookie，DOM 中 <source src> 为真实视频地址
      3. 读取 DOM 中第一个有效 <source src> 并返回

    参数：
      headed: True 时弹出可见浏览器窗口（便于调试观察）

    注意：调用前需确保已执行 `playwright install chromium`。
    """
    try:
        from playwright.async_api import async_playwright, Error as PlaywrightError
    except ImportError as exc:
        raise ImportError(
            "Playwright 未安装，请执行：pip install playwright && playwright install chromium"
        ) from exc

    _JS_READ_SOURCE = """() => {
        const el = document.querySelector('source[src]');
        if (!el) return '';
        return el.src || el.getAttribute('src') || '';
    }"""

    try:
        async with async_playwright() as pw:
            # ── 启动浏览器 ──
            try:
                browser = await pw.chromium.launch(
                    headless=not headed,
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

            # ── 创建上下文（全新，零 cookie）──
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

            # ── 第一次访问：让页面 JS 写入 cookie ──
            logger.info("第一次加载页面（建立 cookie）...")
            await page.goto(page_url, wait_until="load", timeout=60_000)
            logger.debug("第一次 load 完成")

            # ── 第二次访问：携带 cookie，DOM 中为真实视频地址 ──
            logger.info("第二次加载页面（携带 cookie，读取真实视频地址）...")
            await page.goto(page_url, wait_until="load", timeout=60_000)
            logger.debug("第二次 load 完成")

            src = await page.evaluate(_JS_READ_SOURCE)
            await browser.close()

            if src and src.startswith("http"):
                logger.info("✅ 从 DOM 读取到真实视频地址: %s", src)
                return src

    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"Playwright 运行时发生异常: {exc}") from exc

    raise ValueError(
        "Playwright 未能从 DOM 中读取到视频地址。\n\n"
        "可能原因：\n"
        "  1. 该视频需要登录账号才能播放\n"
        "  2. 页面结构已更新，<source> 选择器失效\n"
        "  3. 网络超时，未能完成加载 → 检查代理配置\n\n"
        f"页面地址: {page_url}"
    )
