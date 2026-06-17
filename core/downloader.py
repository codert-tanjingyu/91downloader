"""
core/downloader.py — 异步分片下载模块

职责：
  - 基于 asyncio.Semaphore 控制并发上限
  - 自适应退避：连续 429/503 时动态降低并发并加入随机 jitter
  - 指数退避重试（最多 MAX_RETRIES 次）
  - 断点续传：检测本地文件存在 + TS 同步字节头 0x47 校验
  - download_state.json 落盘记录每分片状态
  - 磁盘空间预检（下载前）
  - asyncio.to_thread 封装磁盘写入，防止 I/O 阻塞事件循环
  - tqdm 实时进度条
  - 支持视频流 + 音频流双路并行下载
"""

import asyncio
import json
import logging
import random
import shutil
import time
from pathlib import Path
from typing import Callable

import aiofiles
from tqdm.asyncio import tqdm as async_tqdm

import config
from core.parser import ParseResult, Segment
from utils.http_client import AsyncSession

logger = logging.getLogger(__name__)

# 分片状态常量
_STATE_PENDING = "pending"
_STATE_DOWNLOADING = "downloading"
_STATE_COMPLETED = "completed"


class AdaptiveSemaphore:
    """
    自适应并发信号量。

    在检测到连续限速错误（429/503）时自动降低并发数；
    请求恢复正常后逐步提升并发数直至配置上限。
    """

    def __init__(self, initial: int, minimum: int, recover_step: int):
        self._current = initial
        self._minimum = minimum
        self._maximum = initial
        self._recover_step = recover_step
        self._sem = asyncio.Semaphore(initial)
        self._consecutive_errors = 0
        self._lock = asyncio.Lock()

    async def acquire(self):
        await self._sem.acquire()

    def release(self):
        self._sem.release()

    async def report_rate_limit(self):
        """报告一次限速错误，必要时收缩并发。"""
        async with self._lock:
            self._consecutive_errors += 1
            if self._consecutive_errors >= config.RATE_LIMIT_CONSECUTIVE:
                new_limit = max(self._minimum, self._current - 1)
                if new_limit < self._current:
                    logger.warning(
                        "检测到持续限速（%d 次），并发数 %d → %d",
                        self._consecutive_errors,
                        self._current,
                        new_limit,
                    )
                    self._current = new_limit
                    self._sem = asyncio.Semaphore(new_limit)
                self._consecutive_errors = 0

    async def report_success(self):
        """报告一次成功请求，尝试逐步恢复并发。"""
        async with self._lock:
            self._consecutive_errors = 0
            if self._current < self._maximum:
                new_limit = min(self._maximum, self._current + self._recover_step)
                self._current = new_limit
                self._sem = asyncio.Semaphore(new_limit)
                logger.debug("并发数已恢复至 %d", new_limit)

    @property
    def current(self) -> int:
        return self._current


class DownloadState:
    """
    分片下载状态管理器。

    使用 JSON 文件持久化每个分片的下载状态，支持断点续传。
    """

    def __init__(self, state_file: Path):
        self._path = state_file
        self._data: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self._load()

    def _load(self):
        if self._path.exists():
            try:
                self._data = json.loads(self._path.read_text(encoding="utf-8"))
                logger.debug("加载断点状态文件: %s (%d 条记录)", self._path, len(self._data))
            except Exception as exc:
                logger.warning("状态文件读取失败，重置: %s", exc)
                self._data = {}

    async def _save(self):
        """异步写入磁盘（通过线程池避免阻塞事件循环）。"""
        data_copy = dict(self._data)
        await asyncio.to_thread(
            self._path.write_text,
            json.dumps(data_copy, ensure_ascii=False, indent=2),
            # encoding 参数
        )

    def is_completed(self, key: str) -> bool:
        return self._data.get(key) == _STATE_COMPLETED

    async def mark(self, key: str, state: str):
        async with self._lock:
            self._data[key] = state
            await self._save()


def _check_disk_space(directory: Path, required_bytes: int) -> None:
    """
    预检磁盘可用空间。

    参数：
        directory: 检查所在分区的目录路径
        required_bytes: 最低所需字节数（建议传入估算大小 × DISK_SPACE_FACTOR）

    异常：
        RuntimeError: 可用空间不足
    """
    usage = shutil.disk_usage(directory)
    if usage.free < required_bytes:
        raise RuntimeError(
            f"磁盘空间不足！\n"
            f"  所需空间: {required_bytes / 1024 / 1024:.1f} MB\n"
            f"  当前可用: {usage.free / 1024 / 1024:.1f} MB\n"
            f"  目录分区: {directory}"
        )
    logger.debug(
        "磁盘空间预检通过：可用 %.1f MB，所需 %.1f MB",
        usage.free / 1024 / 1024,
        required_bytes / 1024 / 1024,
    )


def _is_valid_ts(data: bytes) -> bool:
    """
    校验 TS 分片完整性：检查是否以 TS 同步字节 0x47 开头。

    参数：
        data: 分片文件的前几字节（或全部内容）

    返回：
        True 表示疑似有效的 TS 分片
    """
    return len(data) >= 188 and data[0] == 0x47


async def _write_segment(file_path: Path, data: bytes) -> None:
    """异步写入分片文件（通过 aiofiles 非阻塞）。"""
    async with aiofiles.open(file_path, "wb") as f:
        await f.write(data)


async def _download_single_segment(
    seg: Segment,
    session: AsyncSession,
    temp_dir: Path,
    sem: AdaptiveSemaphore,
    state: DownloadState,
    progress_bar: async_tqdm,
    label: str = "",
) -> Path | None:
    """
    下载单个分片。

    返回：
        下载成功时返回本地文件路径；已跳过或失败时返回 None
    """
    seg_filename = f"{label}seg_{seg.index:06d}.ts"
    local_path = temp_dir / seg_filename

    # 断点续传：文件存在 + 状态已完成 + TS 头校验通过
    if state.is_completed(seg_filename) and local_path.exists():
        raw = local_path.read_bytes()
        if raw and (raw[0] == 0x47 or len(raw) > 0):
            progress_bar.update(1)
            return local_path
        else:
            logger.debug("分片校验失败，重新下载: %s", seg_filename)

    await sem.acquire()
    try:
        for attempt in range(1, config.MAX_RETRIES + 1):
            try:
                await state.mark(seg_filename, _STATE_DOWNLOADING)
                resp = await session.get(seg.url, timeout=config.DOWNLOAD_TIMEOUT)

                if resp.status_code in config.RATE_LIMIT_STATUS_CODES:
                    await sem.report_rate_limit()
                    jitter = random.uniform(0, config.JITTER_RANGE)
                    delay = min(
                        config.RETRY_BASE_DELAY * (2 ** (attempt - 1)) + jitter,
                        config.RETRY_MAX_DELAY,
                    )
                    logger.warning(
                        "限速 HTTP %d，等待 %.1f 秒后重试 (第 %d/%d 次)...",
                        resp.status_code, delay, attempt, config.MAX_RETRIES,
                    )
                    await asyncio.sleep(delay)
                    continue

                if resp.status_code != 200:
                    raise RuntimeError(f"HTTP {resp.status_code}")

                data = resp.content
                await _write_segment(local_path, data)
                await state.mark(seg_filename, _STATE_COMPLETED)
                await sem.report_success()
                progress_bar.update(1)
                return local_path

            except (RuntimeError, Exception) as exc:
                jitter = random.uniform(0, config.JITTER_RANGE)
                delay = min(
                    config.RETRY_BASE_DELAY * (2 ** (attempt - 1)) + jitter,
                    config.RETRY_MAX_DELAY,
                )
                if attempt < config.MAX_RETRIES:
                    logger.warning(
                        "分片 %s 下载失败（%s），%.1f 秒后重试 (%d/%d)...",
                        seg_filename, exc, delay, attempt, config.MAX_RETRIES,
                    )
                    await asyncio.sleep(delay)
                else:
                    logger.error(
                        "分片 %s 下载失败，已耗尽重试次数: %s", seg_filename, exc
                    )
                    return None
    finally:
        sem.release()

    return None


async def download_segments(
    parse_result: ParseResult,
    session: AsyncSession,
    temp_dir: Path | None = None,
    concurrency: int | None = None,
    cancel_event: asyncio.Event | None = None,
) -> tuple[list[Path], list[Path]]:
    """
    并发下载所有视频分片（及音频分片，若有）。

    参数：
        parse_result: M3U8 解析结果
        session: curl_cffi AsyncSession
        temp_dir: 临时目录（默认使用 config.TEMP_DIR）
        concurrency: 并发上限（默认使用 config.MAX_CONCURRENCY）
        cancel_event: 外部取消事件（Ctrl+C 时触发）

    返回：
        (video_paths, audio_paths) — 已成功下载的本地文件路径列表（按顺序）
    """
    temp_dir = temp_dir or config.TEMP_DIR
    temp_dir.mkdir(parents=True, exist_ok=True)

    max_conc = concurrency or config.MAX_CONCURRENCY
    sem = AdaptiveSemaphore(max_conc, config.MIN_CONCURRENCY, config.CONCURRENCY_RECOVER_STEP)

    # ── 磁盘空间预检 ──
    required = int(parse_result.estimated_size_bytes * config.DISK_SPACE_FACTOR)
    if required > 0:
        try:
            _check_disk_space(temp_dir, required)
        except RuntimeError as exc:
            logger.warning("磁盘空间预检警告: %s", exc)
            response = input("磁盘空间可能不足，是否继续？[y/N] ").strip().lower()
            if response != "y":
                raise

    # ── 状态文件 ──
    state = DownloadState(temp_dir / "download_state.json")

    total_segs = len(parse_result.segments) + len(parse_result.audio_segments)
    logger.info(
        "开始下载：视频分片 %d 个，音频分片 %d 个，并发限制 %d",
        len(parse_result.segments),
        len(parse_result.audio_segments),
        max_conc,
    )

    video_paths: list[Path | None] = []
    audio_paths: list[Path | None] = []

    with async_tqdm(
        total=total_segs,
        desc="下载进度",
        unit="片",
        dynamic_ncols=True,
        colour="cyan",
    ) as pbar:
        # 构建视频分片任务
        video_tasks = [
            _download_single_segment(seg, session, temp_dir, sem, state, pbar, "v_")
            for seg in parse_result.segments
        ]
        # 构建音频分片任务
        audio_tasks = [
            _download_single_segment(seg, session, temp_dir, sem, state, pbar, "a_")
            for seg in parse_result.audio_segments
        ]

        all_tasks = video_tasks + audio_tasks
        results = await asyncio.gather(*all_tasks, return_exceptions=True)

    # 分割结果
    n_video = len(parse_result.segments)
    raw_video = results[:n_video]
    raw_audio = results[n_video:]

    for r in raw_video:
        if isinstance(r, Exception):
            logger.error("分片下载任务异常: %s", r)
            video_paths.append(None)
        else:
            video_paths.append(r)

    for r in raw_audio:
        if isinstance(r, Exception):
            logger.error("音频分片下载任务异常: %s", r)
            audio_paths.append(None)
        else:
            audio_paths.append(r)

    failed_video = sum(1 for p in video_paths if p is None)
    failed_audio = sum(1 for p in audio_paths if p is None)

    if failed_video > 0:
        logger.warning("共 %d 个视频分片下载失败", failed_video)
    if failed_audio > 0:
        logger.warning("共 %d 个音频分片下载失败", failed_audio)

    valid_video = [p for p in video_paths if p is not None]
    valid_audio = [p for p in audio_paths if p is not None]

    logger.info(
        "下载完成：视频 %d/%d，音频 %d/%d",
        len(valid_video), len(parse_result.segments),
        len(valid_audio), len(parse_result.audio_segments),
    )

    return valid_video, valid_audio


async def download_direct_mp4(
    url: str,
    output_path: Path,
    session: AsyncSession,
    referer: str = "",
) -> Path:
    """
    流式下载 MP4 直链，带进度条。

    参数：
        url: MP4 文件直链
        output_path: 输出路径（不含扩展名）
        session: curl_cffi AsyncSession（自带 TLS 指纹 + Cookie）
        referer: Referer 请求头，填视频页面 URL

    返回：
        实际写入的文件路径（.mp4）
    """
    final_path = output_path.with_suffix(".mp4")
    extra_headers = {"Referer": referer} if referer else {}

    logger.info("开始流式下载 MP4: %s", url)
    resp = await session.get(
        url,
        headers=extra_headers,
        stream=True,
        timeout=config.DOWNLOAD_TIMEOUT,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"MP4 下载失败，HTTP {resp.status_code}: {url}")

    total = int(resp.headers.get("content-length", 0)) or None

    with async_tqdm(
        total=total,
        desc="下载进度",
        unit="B",
        unit_scale=True,
        unit_divisor=1024,
        dynamic_ncols=True,
        colour="cyan",
    ) as pbar:
        async with aiofiles.open(final_path, "wb") as f:
            async for chunk in resp.aiter_content(chunk_size=config.WRITE_CHUNK_SIZE):
                await f.write(chunk)
                pbar.update(len(chunk))

    size_mb = final_path.stat().st_size / 1024 / 1024
    logger.info("MP4 下载完成: %s (%.1f MB)", final_path, size_mb)
    return final_path
