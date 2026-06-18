"""
core/extractor.py — 视频 URL 提取模块

使用 Playwright Chromium 从页面提取可播放视频地址。

策略（按优先级）：
  1. 全新 context 两次访问页面：第一次建立 cookie，第二次携带 cookie
     后读取 DOM 中 <source src>，即为可直接下载的 MP4 地址。
  2. 若 DOM 无结果，点击播放按钮并监听网络请求，
     等待 M3U8 播放列表地址出现（最多 120 秒）。

注意：需提前执行 `playwright install chromium`。
"""

import asyncio
import logging

logger = logging.getLogger(__name__)

_HLS_CONTENT_TYPES: set[str] = {
    "application/x-mpegurl",
    "application/vnd.apple.mpegurl",
    "audio/mpegurl",
    "audio/x-mpegurl",
}

_PLAY_BUTTON_SELECTORS = [
    "button.vjs-big-play-button",   # Video.js
    ".play-button",
    ".btn-play",
    "#player .play",
    "div.play_btn",
    "div[class*='play']",
    ".dplayer-play-icon",           # DPlayer
    ".jw-icon-display",             # JW Player
    "video",
]


async def extract_video_url(page_url: str, headed: bool = False) -> str:
    """
    从页面提取视频 URL（MP4 直链或 M3U8），使用 Playwright。

    异常：
        RuntimeError / ValueError: 无法获取到有效视频地址
    """
    return await _extract_via_playwright(page_url, headed=headed)


def _is_hls_response(url: str, content_type: str) -> bool:
    if ".m3u8" in url:
        return True
    ct = content_type.lower().split(";")[0].strip()
    return ct in _HLS_CONTENT_TYPES


async def _extract_via_playwright(page_url: str, headed: bool = False) -> str:
    """
    Playwright 提取流程：
      1. 两次 goto：第一次建 cookie，第二次带 cookie 读 DOM → MP4
      2. DOM 无结果：点播放按钮 + 网络拦截等待 M3U8（最多 120 秒）
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

    intercepted_url: list[str] = []

    try:
        async with async_playwright() as pw:
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
                        "Playwright Chromium 未安装，请执行：playwright install chromium"
                    ) from exc
                raise

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

            # 网络拦截（M3U8 降级路径用）
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
                    logger.info("✅ 网络拦截到 M3U8: %s", url)

            page.on("response", on_response)

            # ── 路径 1：两次访问读 DOM → MP4 ──
            logger.info("第一次加载页面（建立 cookie）...")
            await page.goto(page_url, wait_until="load", timeout=60_000)

            logger.info("第二次加载页面（携带 cookie，读取视频地址）...")
            await page.goto(page_url, wait_until="load", timeout=60_000)

            src = await page.evaluate(_JS_READ_SOURCE)
            if src and src.startswith("http"):
                logger.info("✅ 从 DOM 读取到 MP4 直链: %s", src)
                await browser.close()
                return src

            # ── 路径 2：点击播放，等待 M3U8 网络请求（最多 120 秒）──
            logger.info("DOM 无 MP4，点击播放按钮等待 M3U8 ...")
            for selector in _PLAY_BUTTON_SELECTORS:
                try:
                    el = page.locator(selector).first
                    if await el.count() > 0:
                        await el.click(timeout=3_000)
                        logger.debug("已点击播放按钮: %s", selector)
                        break
                except Exception as e:
                    logger.debug("选择器 %s 点击失败: %s", selector, e)

            _MAX_WAIT_SEC = 120.0
            _POLL_SEC = 0.5
            elapsed = 0.0
            while elapsed < _MAX_WAIT_SEC:
                if intercepted_url:
                    logger.info("✅ 拦截到 M3U8（等待 %.1f 秒后）: %s", elapsed, intercepted_url[0])
                    await browser.close()
                    return intercepted_url[0]
                await asyncio.sleep(_POLL_SEC)
                elapsed += _POLL_SEC

            await browser.close()

    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"Playwright 运行时发生异常: {exc}") from exc

    raise ValueError(
        "未能获取到视频地址。\n\n"
        "可能原因：\n"
        "  1. 该视频需要登录账号才能播放\n"
        "  2. 广告时长超出等待上限（120 秒）\n"
        "  3. 网络超时，未能完成加载 → 检查代理配置\n\n"
        f"页面地址: {page_url}"
    )
