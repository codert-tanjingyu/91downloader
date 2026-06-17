"""
main.py — 程序主入口

职责：
  - 解析命令行参数
  - 捕获 SIGINT / Windows CTRL_C_EVENT 信号，确保优雅退出
  - 驱动完整的下载流水线：
      URL输入 → 页面提取 → M3U8解析 → 并发下载 → 解密合并 → 输出视频
  - try...finally 保障任何异常或中断下的资源释放

用法示例：
  python main.py "https://www.91porn.com/view_video.php?viewkey=xxx"
  python main.py <URL> --output my_video --proxy socks5://127.0.0.1:7891
  python main.py <URL> --concurrency 4 --use-playwright
  python main.py <URL> --m3u8  (直接传入 M3U8 链接，跳过页面解析)
"""

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path

import config  # noqa: F401 — 触发日志配置与目录初始化

logger = logging.getLogger("main")

# 全局取消事件（Ctrl+C 时设置）
_cancel_event = asyncio.Event()


def _setup_signal_handlers() -> None:
    """
    注册信号处理器。

    Windows 下 asyncio 不支持 add_signal_handler，
    改用 signal.signal 注册 SIGINT（KeyboardInterrupt）处理。
    """
    def _handler(signum, frame):
        logger.warning("\n⚠️  收到中断信号，正在保存进度并退出 ...")
        _cancel_event.set()

    signal.signal(signal.SIGINT, _handler)
    if hasattr(signal, "SIGBREAK"):          # Windows Ctrl+Break
        signal.signal(signal.SIGBREAK, _handler)


def _build_parser() -> argparse.ArgumentParser:
    """构建命令行参数解析器。"""
    parser = argparse.ArgumentParser(
        prog="91downloader",
        description="91视频 HLS 下载器 — 自动提取 M3U8、并发下载 TS 分片并合并为 MP4",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例：
  # 下载视频页面（自动提取 M3U8）
  python main.py "https://www.91porn.com/view_video.php?viewkey=xxx"

  # 直接传入 M3U8 链接
  python main.py "https://example.com/video/index.m3u8" --m3u8

  # 指定输出名称与代理
  python main.py <URL> --output my_video --proxy socks5://127.0.0.1:7891

  # 使用 Playwright 动态拦截模式（需提前安装：playwright install chromium）
  python main.py <URL> --use-playwright

  # 调整并发数与交互式清晰度选择
  python main.py <URL> --concurrency 4 --interactive
        """,
    )

    parser.add_argument(
        "url",
        help="视频页面 URL 或 M3U8 直链（配合 --m3u8 使用）",
    )
    parser.add_argument(
        "--m3u8",
        action="store_true",
        default=False,
        help="将 url 参数视为直接的 M3U8 链接，跳过页面解析步骤",
    )
    parser.add_argument(
        "--output", "-o",
        default="",
        help='输出文件名（不含扩展名），默认使用页面 URL 末尾的 viewkey 或时间戳',
    )
    parser.add_argument(
        "--proxy",
        default="",
        help='代理地址，覆盖 .env 中的 PROXY 设置。例如：socks5://127.0.0.1:7891',
    )
    parser.add_argument(
        "--cookie",
        default="",
        help='自定义 Cookie 字符串，覆盖 .env 中的 COOKIE 设置',
    )
    parser.add_argument(
        "--concurrency", "-c",
        type=int,
        default=0,
        help=f'并发下载分片数（默认 {config.MAX_CONCURRENCY}）',
    )
    parser.add_argument(
        "--use-playwright",
        action="store_true",
        default=False,
        help="启用 Playwright 无头浏览器动态拦截 M3U8（默认不启用，需安装：playwright install chromium）",
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        default=False,
        help="遇到 Master Playlist 时在终端提供交互式清晰度选择菜单",
    )
    parser.add_argument(
        "--no-cleanup",
        action="store_true",
        default=False,
        help="合并完成后保留 temp/ 目录中的临时分片文件",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        default=False,
        help="开启 DEBUG 级别详细日志输出",
    )

    return parser


def _resolve_output_name(url: str, override: str) -> str:
    """
    根据 URL 推导输出文件名（不含扩展名）。

    优先顺序：
      1. 用户通过 --output 指定的名称
      2. URL 中的 viewkey 参数值
      3. URL 路径最后一段（去除扩展名）
      4. 时间戳兜底
    """
    if override:
        return override

    import re
    from urllib.parse import urlparse, parse_qs

    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    if "viewkey" in qs:
        return f"video_{qs['viewkey'][0]}"

    path_stem = Path(parsed.path).stem
    if path_stem and path_stem != "/":
        return path_stem

    import time
    return f"video_{int(time.time())}"


async def download_pipeline(args: argparse.Namespace) -> None:
    """
    完整的下载流水线协程。

    阶段：
      1. 获取/验证 M3U8 URL
      2. 解析 M3U8（分片列表、加密信息、音频流）
      3. 并发下载所有分片
      4. 解密合并，输出最终视频文件
    """
    from utils.http_client import get_session
    from core.extractor import extract_m3u8_url
    from core.parser import parse_m3u8
    from core.downloader import download_segments
    from core.merger import merge_segments

    # 命令行参数覆盖 config
    if args.proxy:
        config.PROXY = args.proxy
        logger.info("使用命令行代理: %s", args.proxy)
    if args.cookie:
        config.COOKIE = args.cookie

    concurrency = args.concurrency if args.concurrency > 0 else config.MAX_CONCURRENCY
    output_name = _resolve_output_name(args.url, args.output)
    output_path = config.OUTPUT_DIR / output_name

    async with get_session() as session:
        # ── 阶段 1：获取 M3U8 URL ──
        if args.m3u8:
            m3u8_url = args.url
            logger.info("直接使用 M3U8 链接: %s", m3u8_url)
        else:
            logger.info("正在解析视频页面 ...")
            m3u8_url = await extract_m3u8_url(
                args.url,
                session,
                use_playwright=args.use_playwright,
            )
            logger.info("M3U8 URL: %s", m3u8_url)

        if _cancel_event.is_set():
            logger.info("已取消")
            return

        # ── 阶段 2：解析 M3U8 ──
        logger.info("正在解析 M3U8 播放列表 ...")
        parse_result = await parse_m3u8(
            m3u8_url,
            session,
            prefer_highest=not args.interactive,
            interactive=args.interactive,
        )

        if not parse_result.segments:
            raise ValueError("M3U8 解析结果为空，未找到任何分片")

        if _cancel_event.is_set():
            logger.info("已取消")
            return

        # ── 阶段 3：并发下载 ──
        logger.info("开始下载分片 ...")
        video_paths, audio_paths = await download_segments(
            parse_result,
            session,
            concurrency=concurrency,
            cancel_event=_cancel_event,
        )

        if _cancel_event.is_set():
            logger.info("下载已中断，进度已保存，下次运行将自动续传")
            return

        if not video_paths:
            raise RuntimeError("所有视频分片下载失败，无法合并")

        # ── 阶段 4：解密合并 ──
        logger.info("开始合并视频 ...")
        final_file = await merge_segments(
            video_paths,
            audio_paths,
            parse_result.segments,
            output_path,
            cleanup=not args.no_cleanup,
        )

        print(f"\n✅ 下载完成！\n   输出文件: {final_file}")


def main() -> None:
    """程序主入口。"""
    parser = _build_parser()
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
        logger.debug("已开启 DEBUG 模式")

    _setup_signal_handlers()

    logger.info("="*50)
    logger.info(" 91视频 HLS 下载器 启动")
    logger.info("="*50)
    logger.info("目标: %s", args.url)

    try:
        asyncio.run(download_pipeline(args))
    except KeyboardInterrupt:
        logger.info("\n用户中断，进度已保存")
        sys.exit(0)
    except Exception as exc:
        logger.error("下载失败: %s", exc, exc_info=args.verbose)
        sys.exit(1)
    finally:
        logger.debug("程序退出，资源已释放")


if __name__ == "__main__":
    main()
