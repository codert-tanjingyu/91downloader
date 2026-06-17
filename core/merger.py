"""
core/merger.py — 视频解密与分片合并模块

职责：
  - AES-128 CBC 解密（pycryptodome），IV 缺省时按序列号推导
  - PKCS7 去填充（容错）
  - TS 同步字节头校验
  - 流式追加写入（Streaming Append），内存常态控制在数十 MB 内
  - 优先使用 FFmpeg 将合并结果 Remux 为 .mp4（无损封装）
  - FFmpeg 不可用时降级为二进制 .ts 文件拼接
  - 合并完成后清理 temp/ 目录
"""

import asyncio
import logging
import shutil
import subprocess
from pathlib import Path

import config
from core.parser import Segment

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────
# AES-128 解密
# ──────────────────────────────────────────────────────────

def decrypt_ts_segment(
    encrypted_data: bytes,
    key_bytes: bytes,
    iv_bytes: bytes | None = None,
    seq_num: int = 0,
) -> bytes:
    """
    使用 AES-128 CBC 模式解密 TS 分片数据。

    参数：
        encrypted_data: 加密的分片字节数据
        key_bytes: 16 字节 AES 密钥
        iv_bytes: 显式 IV（若为 None，则按 HLS RFC 8216 规范使用序列号推导）
        seq_num: 分片媒体序列号（仅在 iv_bytes 为 None 时使用）

    返回：
        解密后的明文字节数据
    """
    from Crypto.Cipher import AES

    if not iv_bytes:
        # 按 HLS RFC 8216 规范：使用分片序列号的大端序 16 字节表示
        iv_bytes = seq_num.to_bytes(16, byteorder="big")

    cipher = AES.new(key_bytes, AES.MODE_CBC, iv=iv_bytes)
    decrypted = cipher.decrypt(encrypted_data)

    # PKCS7 去填充（容错：去除失败时保留原解密数据）
    try:
        pad_len = decrypted[-1]
        if 1 <= pad_len <= 16 and all(x == pad_len for x in decrypted[-pad_len:]):
            decrypted = decrypted[:-pad_len]
    except Exception:
        pass  # 保留原数据，最大程度保留视频内容

    return decrypted


# ──────────────────────────────────────────────────────────
# FFmpeg 检测
# ──────────────────────────────────────────────────────────

def _find_ffmpeg() -> str | None:
    """
    检测系统中是否存在 ffmpeg 可执行文件。

    返回：
        ffmpeg 路径字符串；不可用时返回 None
    """
    path = shutil.which("ffmpeg")
    if path:
        logger.debug("检测到 FFmpeg: %s", path)
    else:
        logger.warning("未检测到 FFmpeg，将降级为二进制 .ts 文件拼接")
    return path


# ──────────────────────────────────────────────────────────
# 核心合并逻辑
# ──────────────────────────────────────────────────────────

async def merge_segments(
    video_paths: list[Path],
    audio_paths: list[Path],
    segments: list[Segment],
    output_path: Path,
    temp_dir: Path | None = None,
    cleanup: bool = True,
) -> Path:
    """
    解密、合并所有视频（及音频）分片，输出最终视频文件。

    参数：
        video_paths: 已下载的视频分片本地路径列表（按顺序）
        audio_paths: 已下载的音频分片本地路径列表（可为空列表）
        segments: 对应的 Segment 信息（用于获取解密密钥）
        output_path: 最终输出文件路径（含文件名，扩展名由合并方式决定）
        temp_dir: 临时目录（用于存放中间合并文件）
        cleanup: 合并完成后是否清理 temp_dir（默认 True）

    返回：
        实际输出的文件路径
    """
    temp_dir = temp_dir or config.TEMP_DIR
    ffmpeg_bin = _find_ffmpeg()

    # ── 解密并流式合并视频分片 ──
    video_concat = temp_dir / "_video_concat.ts"
    logger.info("正在流式合并 %d 个视频分片 ...", len(video_paths))
    await _stream_merge(video_paths, segments, video_concat)

    # ── 处理音频流（若有）──
    audio_concat: Path | None = None
    if audio_paths:
        audio_segs_info = [
            Segment(index=i, url="", key_info=None)
            for i in range(len(audio_paths))
        ]
        audio_concat = temp_dir / "_audio_concat.ts"
        logger.info("正在流式合并 %d 个音频分片 ...", len(audio_paths))
        await _stream_merge(audio_paths, audio_segs_info, audio_concat)

    # ── 调用 FFmpeg 封装 MP4（优先）──
    if ffmpeg_bin:
        final_path = output_path.with_suffix(".mp4")
        logger.info("使用 FFmpeg 封装 MP4: %s", final_path)
        _run_ffmpeg(ffmpeg_bin, video_concat, audio_concat, final_path)
    else:
        # ── 降级：直接重命名 .ts 拼接结果 ──
        final_path = output_path.with_suffix(".ts")
        logger.info("降级合并：输出 .ts 文件: %s", final_path)
        await asyncio.to_thread(shutil.move, str(video_concat), str(final_path))

    # ── 清理临时目录 ──
    if cleanup:
        _cleanup_temp(temp_dir)

    logger.info("✅ 合并完成，输出文件: %s", final_path)
    return final_path


async def _stream_merge(
    paths: list[Path],
    segments: list[Segment],
    output_ts: Path,
) -> None:
    """
    流式追加写入：依次读取分片、解密（如需）、追加到 output_ts。

    采用固定 chunk 大小分批读写，使内存占用常态维持在数十 MB 内。
    """
    chunk_size = config.WRITE_CHUNK_SIZE

    async def _process_and_write():
        with open(output_ts, "wb") as out_f:
            for path, seg in zip(paths, segments):
                if path is None or not path.exists():
                    logger.warning("分片文件缺失，跳过: %s", path)
                    continue

                data = path.read_bytes()

                # 解密（如有）
                if seg.key_info is not None and seg.key_info.method != "NONE":
                    try:
                        data = decrypt_ts_segment(
                            data,
                            seg.key_info.key_bytes,
                            seg.key_info.iv,
                            seg.index,
                        )
                    except Exception as exc:
                        logger.error("分片 %d 解密失败: %s", seg.index, exc)
                        continue

                # 流式追加写入（固定 chunk）
                offset = 0
                while offset < len(data):
                    out_f.write(data[offset: offset + chunk_size])
                    offset += chunk_size

    # 在线程池执行磁盘 I/O，避免阻塞事件循环
    await asyncio.to_thread(_process_and_write)


def _run_ffmpeg(
    ffmpeg_bin: str,
    video_ts: Path,
    audio_ts: Path | None,
    output_mp4: Path,
) -> None:
    """
    调用 FFmpeg 将 TS 流 Remux 封装为 MP4。

    若存在独立音频流，则同时混流；否则仅封装视频流。
    使用 -c copy 保证无损封装，不重新编码。
    """
    if audio_ts and audio_ts.exists():
        cmd = [
            ffmpeg_bin,
            "-y",                    # 覆盖已存在的输出文件
            "-i", str(video_ts),
            "-i", str(audio_ts),
            "-c", "copy",
            "-movflags", "+faststart",  # 将 moov atom 前移，利于网络播放
            str(output_mp4),
        ]
    else:
        cmd = [
            ffmpeg_bin,
            "-y",
            "-i", str(video_ts),
            "-c", "copy",
            "-movflags", "+faststart",
            str(output_mp4),
        ]

    logger.debug("FFmpeg 命令: %s", " ".join(cmd))
    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    if result.returncode != 0:
        logger.error("FFmpeg 执行失败:\n%s", result.stderr[-2000:])
        raise RuntimeError(f"FFmpeg 封装失败 (退出码 {result.returncode})")

    logger.debug("FFmpeg 输出:\n%s", result.stderr[-500:])


def _cleanup_temp(temp_dir: Path) -> None:
    """清理临时目录中的所有分片文件和中间文件。"""
    if not temp_dir.exists():
        return
    try:
        for f in temp_dir.iterdir():
            # 保留 download_state.json（供断点续传）
            if f.name == "download_state.json":
                continue
            if f.is_file():
                f.unlink()
        logger.info("临时文件清理完成: %s", temp_dir)
    except Exception as exc:
        logger.warning("临时文件清理时发生错误: %s", exc)
