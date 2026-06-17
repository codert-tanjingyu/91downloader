"""
core/parser.py — HLS/M3U8 解析模块

职责：
  - 下载并解析 .m3u8 文件
  - 处理多级 M3U8（Master Playlist → 子播放列表选择）
  - 检测独立音频流（#EXT-X-MEDIA:TYPE=AUDIO）
  - 将所有相对路径转换为绝对 URL
  - 对解密 Key 的 HTTP 请求进行缓存，避免重复请求
  - 返回结构化的 ParseResult
"""

import logging
from dataclasses import dataclass, field
from urllib.parse import urljoin

from curl_cffi.requests import AsyncSession

from utils.http_client import fetch_text, fetch_bytes

logger = logging.getLogger(__name__)


@dataclass
class KeyInfo:
    """AES-128 解密密钥信息。"""
    method: str        # 加密方式，如 "AES-128" 或 "NONE"
    key_bytes: bytes   # 密钥字节（已下载）
    iv: bytes | None   # 显式 IV（若 M3U8 中未提供则为 None，需根据序列号推导）


@dataclass
class Segment:
    """单个 TS 分片信息。"""
    index: int         # 媒体序列号（Media Sequence Number）
    url: str           # 分片完整 URL
    key_info: KeyInfo | None = None  # 该分片的解密密钥（None 表示未加密）
    duration: float = 0.0


@dataclass
class ParseResult:
    """M3U8 解析结果。"""
    segments: list[Segment] = field(default_factory=list)
    audio_segments: list[Segment] = field(default_factory=list)
    total_duration: float = 0.0
    estimated_size_bytes: int = 0   # 基于 BANDWIDTH 估算的总字节数


async def parse_m3u8(
    m3u8_url: str,
    session: AsyncSession,
    prefer_highest: bool = True,
    interactive: bool = False,
) -> ParseResult:
    """
    下载并完整解析 M3U8 播放列表。

    流程：
      1. 下载 M3U8 内容
      2. 若为 Master Playlist，选择子播放列表（最高码率或交互选择）
      3. 解析分片列表，处理相对路径、加密信息
      4. 检测并解析独立音频流

    参数：
        m3u8_url: 播放列表 URL（可能是 Master 也可能是 Media Playlist）
        session: 已配置的 HTTP 会话
        prefer_highest: True 则自动选择最高码率子播放列表
        interactive: True 则在终端提供交互选择菜单（覆盖 prefer_highest）

    返回：
        ParseResult 实例
    """
    logger.info("正在解析 M3U8: %s", m3u8_url)
    content = await fetch_text(m3u8_url, session)

    # ── 判断是否为 Master Playlist ──
    if "#EXT-X-STREAM-INF" in content:
        logger.info("检测到 Master Playlist，正在选择子播放列表 ...")
        m3u8_url = await _resolve_master(
            content, m3u8_url, session, prefer_highest, interactive
        )
        content = await fetch_text(m3u8_url, session)
        logger.info("已切换至子播放列表: %s", m3u8_url)

    # ── 解析分片与加密信息 ──
    key_cache: dict[str, bytes] = {}
    segments, audio_m3u8_url = _parse_media_playlist(content, m3u8_url, key_cache)

    # ── 下载 Key（如有）──
    segments = await _fetch_keys(segments, session, key_cache)

    # ── 解析独立音频流（如有）──
    audio_segments: list[Segment] = []
    if audio_m3u8_url:
        logger.info("检测到独立音频流，正在解析: %s", audio_m3u8_url)
        audio_content = await fetch_text(audio_m3u8_url, session)
        audio_key_cache: dict[str, bytes] = {}
        audio_segs, _ = _parse_media_playlist(audio_content, audio_m3u8_url, audio_key_cache)
        audio_segments = await _fetch_keys(audio_segs, session, audio_key_cache)

    total_duration = sum(s.duration for s in segments)
    estimated_size = _estimate_size(content, total_duration)

    logger.info(
        "解析完成：%d 个视频分片，%d 个音频分片，总时长 %.1f 秒，预估大小 %.1f MB",
        len(segments),
        len(audio_segments),
        total_duration,
        estimated_size / 1024 / 1024,
    )

    return ParseResult(
        segments=segments,
        audio_segments=audio_segments,
        total_duration=total_duration,
        estimated_size_bytes=estimated_size,
    )


# ──────────────────────────────────────────────────────────
# 内部工具函数
# ──────────────────────────────────────────────────────────

async def _resolve_master(
    content: str,
    base_url: str,
    session: AsyncSession,
    prefer_highest: bool,
    interactive: bool,
) -> str:
    """从 Master Playlist 中选择子 M3U8 URL。"""
    import re

    # 解析所有 #EXT-X-STREAM-INF 条目
    stream_pattern = re.compile(
        r"#EXT-X-STREAM-INF:([^\n]+)\n([^\n]+)", re.MULTILINE
    )
    streams: list[dict] = []
    for attr_str, uri in stream_pattern.findall(content):
        attrs = _parse_attr_string(attr_str)
        abs_url = urljoin(base_url, uri.strip())
        bandwidth = int(attrs.get("BANDWIDTH", 0))
        resolution = attrs.get("RESOLUTION", "未知")
        streams.append({"url": abs_url, "bandwidth": bandwidth, "resolution": resolution})

    if not streams:
        raise ValueError("Master Playlist 中未找到有效的子播放列表")

    streams.sort(key=lambda s: s["bandwidth"], reverse=True)

    if interactive:
        print("\n可用清晰度列表：")
        for i, s in enumerate(streams):
            bw_mbps = s["bandwidth"] / 1_000_000
            print(f"  [{i}] {s['resolution']}  ({bw_mbps:.1f} Mbps)")
        while True:
            try:
                choice = int(input(f"请输入序号 [0-{len(streams)-1}，默认 0]: ").strip() or "0")
                if 0 <= choice < len(streams):
                    return streams[choice]["url"]
            except (ValueError, EOFError):
                pass
            print("输入无效，请重试")

    # 默认选最高码率（列表已降序排列）
    selected = streams[0]
    logger.info(
        "自动选择最高码率子列表: 分辨率=%s, 码率=%.1f Mbps",
        selected["resolution"],
        selected["bandwidth"] / 1_000_000,
    )
    return selected["url"]


def _parse_media_playlist(
    content: str,
    base_url: str,
    key_cache: dict[str, bytes],
) -> tuple[list[Segment], str | None]:
    """
    解析 Media Playlist，提取分片列表和加密信息。

    返回：
        (segments, audio_m3u8_url)
        audio_m3u8_url 为 None 表示无独立音频流
    """
    import re

    segments: list[Segment] = []
    current_key_url: str | None = None
    current_iv: bytes | None = None
    current_method: str = "NONE"
    media_sequence: int = 0
    audio_m3u8_url: str | None = None
    current_duration: float = 0.0

    lines = content.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()

        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            media_sequence = int(line.split(":", 1)[1])

        elif line.startswith("#EXT-X-KEY:"):
            attrs = _parse_attr_string(line[len("#EXT-X-KEY:"):])
            current_method = attrs.get("METHOD", "NONE").upper()
            if current_method != "NONE":
                uri = attrs.get("URI", "").strip('"')
                current_key_url = urljoin(base_url, uri) if uri else None
                iv_str = attrs.get("IV", "")
                current_iv = bytes.fromhex(iv_str.replace("0x", "").replace("0X", "")) if iv_str else None
            else:
                current_key_url = None
                current_iv = None

        elif line.startswith("#EXTINF:"):
            # 提取分片时长
            try:
                duration_str = line.split(":", 1)[1].rstrip(",").split(",")[0]
                current_duration = float(duration_str)
            except (IndexError, ValueError):
                current_duration = 0.0
            # 下一行是分片 URL
            i += 1
            while i < len(lines) and (not lines[i].strip() or lines[i].strip().startswith("#")):
                i += 1
            if i < len(lines) and lines[i].strip() and not lines[i].strip().startswith("#"):
                seg_url = urljoin(base_url, lines[i].strip())
                key_info = None
                if current_method != "NONE" and current_key_url:
                    key_info = KeyInfo(
                        method=current_method,
                        key_bytes=b"",   # 占位，后续由 _fetch_keys 填充
                        iv=current_iv,
                    )
                    # 将 key_url 存入 key_cache（占位），后续批量下载
                    key_cache[current_key_url] = b""

                seg_index = media_sequence + len(segments)
                segments.append(Segment(
                    index=seg_index,
                    url=seg_url,
                    key_info=KeyInfo(
                        method=current_method,
                        key_bytes=b"",
                        iv=current_iv,
                    ) if current_method != "NONE" and current_key_url else None,
                    duration=current_duration,
                ))
                # 将 key_url 附加到 segment 上，供 _fetch_keys 使用
                if segments[-1].key_info is not None:
                    segments[-1].key_info._key_url = current_key_url  # type: ignore[attr-defined]

        elif line.startswith("#EXT-X-MEDIA:"):
            attrs = _parse_attr_string(line[len("#EXT-X-MEDIA:"):])
            if attrs.get("TYPE", "").upper() == "AUDIO":
                uri = attrs.get("URI", "").strip('"')
                if uri:
                    audio_m3u8_url = urljoin(base_url, uri)
                    logger.debug("发现独立音频流: %s", audio_m3u8_url)

        i += 1

    return segments, audio_m3u8_url


async def _fetch_keys(
    segments: list[Segment],
    session: AsyncSession,
    key_cache: dict[str, bytes],
) -> list[Segment]:
    """
    批量下载所有分片的解密 Key，利用缓存避免重复请求。

    参数：
        segments: 分片列表（key_info.key_bytes 为空时需填充）
        session: HTTP 会话
        key_cache: key_url → key_bytes 映射缓存

    返回：
        填充了 key_bytes 的 segments 列表
    """
    for seg in segments:
        if seg.key_info is None:
            continue
        key_url = getattr(seg.key_info, "_key_url", None)
        if not key_url:
            continue

        if key_url not in key_cache or not key_cache[key_url]:
            logger.debug("下载解密 Key: %s", key_url)
            key_bytes = await fetch_bytes(key_url, session)
            key_cache[key_url] = key_bytes
            logger.debug("Key 下载成功，长度: %d 字节", len(key_bytes))

        seg.key_info.key_bytes = key_cache[key_url]

    return segments


def _parse_attr_string(attr_str: str) -> dict[str, str]:
    """
    解析 HLS 属性字符串，例如：
      'BANDWIDTH=3000000,RESOLUTION=1920x1080,CODECS="avc1.640028,mp4a.40.2"'
    """
    import re
    result: dict[str, str] = {}
    pattern = re.compile(r'([A-Z0-9\-]+)=(?:"([^"]*)"|([\w\-\.@]+))')
    for match in pattern.finditer(attr_str):
        key = match.group(1)
        value = match.group(2) if match.group(2) is not None else match.group(3)
        result[key] = value
    return result


def _estimate_size(m3u8_content: str, total_duration: float) -> int:
    """
    估算视频总大小（字节）。

    策略：
      1. 若 M3U8 含 BANDWIDTH 字段，用最高码率 × 总时长 / 8 估算
      2. 否则每秒按 500 KB 估算（典型 720p HLS 码率）

    返回：
        估算字节数
    """
    import re
    bandwidths = re.findall(r"BANDWIDTH=(\d+)", m3u8_content)
    if bandwidths:
        max_bw = max(int(b) for b in bandwidths)   # bits/s
        return int(max_bw / 8 * total_duration)
    # 降级估算：500 KB/s
    return int(total_duration * 500 * 1024)
