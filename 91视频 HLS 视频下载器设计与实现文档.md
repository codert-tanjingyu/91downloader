# 91视频 HLS 视频下载器设计与实现文档

## 1. 项目概述
本项目旨在实现一个基于 Python 的视频下载工具。输入特定网页链接后，系统自动解析页面、提取底层的 `.m3u8` 播放列表、多线程/异步下载并解密其对应的 `.ts` 视频切片，最终将分片合并为完整的 `.mp4` 或 `.ts` 视频文件。

---

## 2. 核心技术挑战与解决方案

### 2.1 规避地理限制与防爬机制 (WAF 绕过)
* **挑战**：目标网站通常会对特定区域 IP 进行限制，且广泛使用 Cloudflare 等 WAF（Web 应用程序防火墙）进行防爬保护。普通的 HTTP 请求库（如标准 `httpx`/`requests`）在 TLS 握手特征（JA3 指纹）上与浏览器不符，极易被直接返回 `403`。
* **方案**：
  1. **TLS/JA3 指纹防伪装**：采用 `curl_cffi` 库代替标准 `httpx`，以模拟 Chrome 浏览器的 TLS 和 HTTP/2 特征。
  2. **代理与请求头伪造**：支持配置 Socks5/HTTP 代理，伪造 `X-Forwarded-For` 和 `Client-IP` 随机国外 IP 段，携带正确的 `Referer` 与 `User-Agent`。
  3. **Cookie 提取导入**：支持手动配置 Cookie 传入，或者集成 `browser-cookie3` 自动从本地主流浏览器中提取已通过人机验证的 Cookie 状态。

### 2.2 网页混淆与视频源提取
* **挑战**：视频的播放地址（M3U8 链接）通过动态混淆 of JavaScript 进行加密加载，且加密逻辑可能会发生变动。
* **方案**：
  1. **静态提取与逆向**：通过正则表达式（`re`）提取混淆代码段中的密文与 Key，并在 Python 中复写其 XOR 解码算法。
  2. **动态渲染兜底 (Playwright)**：作为备用机制，如前端加密频繁升级，支持可选启动无头浏览器（Playwright）进行网络请求侦听，直接拦截获取加载后的真实 M3U8 地址，保障解析的长期稳定性。

### 2.3 高速切片下载与自适应流控
* **挑战**：分片数量多，高并发下载易触发服务器限速（HTTP 429）或封禁；网络抖动时易导致部分分片损坏或丢失。
* **方案**：
  1. **异步并发限制 (Semaphore)**：基于 `asyncio` + `aiohttp`/`curl_cffi` 异步并发下载，通过信号量机制（例如 `asyncio.Semaphore(8)`）限制并发上限。
  2. **自适应速率退避**：当检测到连续请求返回 `429 Too Many Requests` 或 `503 Service Unavailable` 时，临时收缩信号量限制（如将并发上限降低）并增加随机延迟（Jitter），待请求恢复正常后再逐步恢复。
  3. **指数退避重试**：失败分片进入重试队列，采用指数退避机制（如每次间隔 2s, 4s, 8s）并加入随机抖动。

### 2.4 HLS 媒体解密（AES-128）与地址解析
* **挑战**：部分 `.m3u8` 文件内的 TS 分片采用 AES-128 加密，且其链接和 Key 链接可能是复杂的相对路径，IV 缺省时需正确推导。
* **方案**：
  1. **相对路径解析**：解析 `.m3u8` 节点时，使用 `urllib.parse.urljoin(m3u8_url, path)` 自动拼接得到完整的绝对 URL，防止相对地址请求失败。
  2. **标准 IV 推导与解密**：解析 `#EXT-X-KEY`。若缺省 `IV`，则严格按照 HLS 标准（RFC 8216）使用分片的媒体序列号（Sequence Number）的大端序 16 字节表示作为解密 IV，使用 `pycryptodome` 进行 AES-128 CBC 解密。
  3. **密钥缓存 (Key Caching)**：对解密密钥（Key）的请求进行缓存，避免针对每个分片重复请求相同的 Key 链接。

### 2.5 多级 M3U8（Master Playlist）处理机制
* **挑战**：标准的 HLS 流通常包含一个“主播放列表”（Master Playlist），其内容并不是直接的 `.ts` 分片，而是嵌套了不同码率/分辨率的子播放列表（标志有 `#EXT-X-STREAM-INF`），若不支持解析主列表将导致读取分片失败。
* **方案**：在解析模块中增加对多级 M3U8 的检测逻辑。如果解析内容包含 `#EXT-X-STREAM-INF` 标签，则默认自动选择分辨率/带宽最高的子 `.m3u8` 链接进行二级解析，或者向终端提供可交互的选择菜单。

### 2.6 统一的 HTTP 会话管理 (Session Coherence)
* **挑战**：在防爬较为严格的场景下，获取网页、请求 M3U8、请求解密 Key 和下载 TS 分片等步骤，必须在同一个网络会话（Session）中进行，否则可能因 TLS 指纹不一致或 Cookie 校验失败导致 Key/分片获取受阻。
* **方案**：在 `utils/http_client.py` 中设计统一的上下文管理器，确保全局在生命周期内复用同一个 HTTP 客户端连接池，保持 SSL/JA3 握手状态、Cookies 和自定义 headers 的高度一致性。

### 2.7 TS 分片完整性校验 (Data Integrity)
* **挑战**：单纯依靠“本地文件存在且大小大于零”进行断点续传并不绝对安全，网络抖动或连接半中断可能导致切片文件损坏，合并后引发卡顿或音画不同步。
* **方案**：
  1. **结构特征校验**：由于标准 TS 帧为固定 188 字节且以同步字节 `0x47` 包头开头，解密后对分片进行头部特征包结构检验。
  2. **下载状态落盘**：引入轻量级状态记录文件（如 `download_state.json`）记录所有分片的状态（`pending`, `downloading`, `completed`），只有在完全写入磁盘并成功 close 后才将其标记为 `completed` 作为续传依据。

### 2.8 资源释放与异常退出清理 (Graceful Shutdown)
* **挑战**：在异步高并发下载中，如果用户强行中断（Ctrl+C）或者程序抛出未捕获异常，容易引发未完成的临时文件大量残留、连接池和事件循环泄漏等问题。
* **方案**：引入信号监听机制（使用 `signal` 库捕获 `SIGINT`），并在下载/合并的主入口采用 `try...finally` 结构，确保在任何异常退出场景下都会释放 HTTP 资源、保存进度并妥善清理半成品临时文件。

### 2.9 音视频分离流 (Demuxed Stream) 的适配
* **挑战**：现代 HLS 流（尤其是多音轨或超高清资源）常采用音视频分离（Demuxed）的存储结构。主播放列表关联了独立的视频流和音频流 M3U8。如果仅下载分辨率最高的视频流，将导致最终合并出的视频无声。
* **方案**：在解析模块中增加对 `#EXT-X-MEDIA:TYPE=AUDIO` 标签的匹配。若存在独立音频轨，需分别下载对应的视频分片和音频分片，并在合并阶段通过 FFmpeg 进行合路拼接：`ffmpeg -i video.ts -i audio.ts -c copy output.mp4`。

### 2.10 避免异步事件循环中的磁盘 I/O 阻塞
* **挑战**：`asyncio` 事件循环是单线程的。在高速并发写入分片时，Python 原生的 `open().write()` 阻塞磁盘 I/O 操作极易导致事件循环卡死，引发网络请求超时和整体吞吐率下降。
* **方案**：使用 Python 内置的线程池执行磁盘文件落盘操作，利用 `asyncio.to_thread`（或 `run_in_executor`）将写入封装为非阻塞协程，保障异步循环的网络响应速度。

### 2.11 磁盘空间预检机制 (Disk Space Check)
* **挑战**：在进行视频 Remux 合并（特别是 TS 合并为 MP4）时，磁盘会瞬时同时存在所有的临时 `.ts` 分片与最终合成文件，需要至少两倍以上的存储空间。若空间不足中途写入失败，将导致前功尽弃且损坏数据。
* **方案**：解析 M3U8 列表后，根据码率（`BANDWIDTH`）或分片平均大小，估算视频最终总体积。在下载启动前使用 `shutil.disk_usage()` 对临时目录和输出目录的分区进行空间可用性校验，不足估算体积的 2.5 倍时抛出警告或中止任务。

### 2.12 内存友好的“解密+合并”流式处理
* **挑战**：如果将几百个分片数据全部读入内存一次性解密并组合为一个巨大的 `bytes` 对象进行合并，下载大体积或高清长视频时会导致 OOM（内存溢出）甚至引发系统崩溃。
* **方案**：在合并阶段，采用流式读写追加机制（Streaming / Append 模式），依次读取分片、解密，然后以固定 chunk 大小流式追加写入本地临时合并文件，或者利用 Python 的管道（`subprocess.PIPE`）将解密后的分片流式推送给 `ffmpeg` 的 `stdin` 进行混流封装，使内存占用常态化控制在数十兆（MB）内。

---

## 3. 系统架构与模块设计

系统划分为五个核心模块，其数据流向如下：
`用户输入URL` ➔ `页面解析模块` ➔ `M3U8解析模块` ➔ `并发下载模块` ➔ `解密合并模块` ➔ `输出视频`

### 3.1 页面解析模块（Extractor）
* **职责**：负责获取网页 HTML 并提取混淆播放列表；支持静态 HTTP 提取与 Playwright 动态拦截双重引擎。
* **主要依赖**：`curl_cffi`，`BeautifulSoup`，`re`，（可选）`playwright`。

### 3.2 密文解码模块（Decoder）
* **职责**：执行与网页 JS 等价的解密逻辑，还原出真实的 M3U8 链接，增加空密钥等防御性检查。
* **核心算法**：Base64 还原与防越界的 XOR 运算。

### 3.3 HLS/M3U8 解析模块（M3U8 Parser）
* **职责**：下载并解析 `.m3u8`，使用 `urljoin` 处理相对路径，提取分片绝对地址、解密密钥（带缓存）和 IV。
* **主要依赖**：`m3u8` 库，`urllib.parse`。

### 3.4 异步下载模块（Downloader）
* **职责**：以并发限制（Semaphore）和断点续传（通过本地文件名和大小校验）的形式，稳定下载分片到临时目录。
* **主要依赖**：`aiohttp` / `curl_cffi`，`tqdm`（进度条），`asyncio`。

### 3.5 视频处理与合并模块（Processor）
* **职责**：解密分片，对于独立音频流进行双路并行下载与合路封装；在合并阶段使用追加或管道推送机制流式合并，并调用 `ffmpeg` 完成画质无损的重封装合并（当 FFmpeg 缺失时降级为二进制拼接）。
* **主要依赖**：`pycryptodome`（AES 解密），`subprocess`（流式管道传输与 FFmpeg 调用），`shutil`（磁盘预检）。

---

## 4. 关键代码逻辑设计（伪代码/逻辑参考）

### 4.1 网页解密算法实现 (以常见 XOR 为例)
根据该站历史上的 `strencode` 规律，其常用解码还原算法的 Python 等价实现参考如下：

```python
import base64

def str_decode(encrypted_str: str, key: str) -> str:
    """
    等价于 JS 中 document.write(strencode(str, key)) 的逻辑
    """
    if not key:
        raise ValueError("解密密钥(Key)不能为空")
    try:
        # 1. Base64 解码
        decoded_bytes = base64.b64decode(encrypted_str)
        key_len = len(key)
        res = bytearray()
        
        # 2. 与 Key 进行循环 XOR 运算
        for i, byte_val in enumerate(decoded_bytes):
            k_char = ord(key[i % key_len])
            res.append(byte_val ^ k_char)
            
        return res.decode('utf-8', errors='ignore')
    except Exception as e:
        raise ValueError(f"解码失败: {e}")
```

### 4.2 AES-128 切片解密逻辑
若 M3U8 包含 `#EXT-X-KEY`，需要使用密钥进行解密：

```python
from Crypto.Cipher import AES

def decrypt_ts_segment(encrypted_data: bytes, key_bytes: bytes, iv_bytes: bytes = None, seq_num: int = 0) -> bytes:
    """
    使用 AES-128 CBC 模式解密 ts 数据片
    """
    # 根据 HLS 标准 RFC 8216，如无显式 IV 属性，须使用分片序列号(Media Sequence Number)的大端序 16 字节表示作为 IV
    if not iv_bytes:
        iv_bytes = seq_num.to_bytes(16, byteorder='big')
        
    cipher = AES.new(key_bytes, AES.MODE_CBC, iv=iv_bytes)
    decrypted_data = cipher.decrypt(encrypted_data)
    
    # 去除 PKCS7 填充（AES 块大小为 16 字节，填充长度有效范围为 1 至 16）
    # 增加 try...except 容错保护，防止由于上游切片工具的填充异常导致整个分片解密过程崩溃
    try:
        pad_len = decrypted_data[-1]
        if 1 <= pad_len <= 16 and all(x == pad_len for x in decrypted_data[-pad_len:]):
            decrypted_data = decrypted_data[:-pad_len]
    except Exception:
        # 填充去除失败时保留原解密数据，最大程度保留视频内容，避免任务中断
        pass
        
    return decrypted_data
```

---

## 5. 工程目录结构设计

为了保证代码的清晰度与可测试性，建议采用以下目录布局：

```text
91downloader/
│
├── config.py                 # 全局配置（代理设置、请求头、重试次数、临时目录等）
├── main.py                   # 程序入口（解析命令行参数并启动流程）
│
├── core/                     # 核心逻辑目录
│   ├── __init__.py
│   ├── extractor.py          # 负责请求页面并提取加密数据
│   ├── decoder.py            # 存放解密算法（strencode）
│   ├── parser.py             # 负责下载并解析 m3u8
│   ├── downloader.py         # 异步多线程/多协程分片下载器
│   └── merger.py             # 视频解密与分片合并
│
├── utils/                    # 辅助工具包
│   ├── __init__.py
│   └── http_client.py        # 封装请求、处理代理和伪造 IP 逻辑
│
├── temp/                     # 临时目录（用于存放下载的 .ts 分片，下载完合并后删除）
├── output/                   # 存放最终合并完成的视频
├── requirements.txt          # 项目依赖项清单
└── README.md                 # 使用说明文档
```

---

## 6. 开发与测试计划

### 6.1 开发阶段
1. **第一阶段：环境准备与依赖安装**
   * 创建 Python 虚拟环境。
   * 编写 `requirements.txt`，锁定核心依赖的大版本以确保最佳兼容性：
     ```text
     curl_cffi>=0.5.10       # 提供 TLS/JA3 指纹绕过与异步支持
     beautifulsoup4>=4.12.0  # HTML 结构解析
     m3u8>=3.0.0             # 标准 M3U8 解析
     pycryptodome>=3.19.0    # 跨平台的高性能 AES 加密支持
     tqdm>=4.66.0            # 终端进度条
     playwright>=1.40.0      # 动态渲染引擎（备用）
     ```
2. **第二阶段：工具类与解析逻辑开发**
   * 编写 `http_client.py`，配置指纹客户端（`curl_cffi`）、代理和请求头策略。
   * 编写 `extractor.py`，支持静态规则匹配与可选的 Playwright 运行时网络抓包。
3. **第三阶段：HLS 逻辑实现**
   * 完成 `.m3u8` 相对路径转换与 Key 缓存请求解析。
   * 开发带信号量并发限制（`Semaphore`）和本地校验断点续传的 `downloader.py`。
4. **第四阶段：合并与清理**
   * 编写 `merger.py`，在 AES 解密后，检测系统内是否存在 `ffmpeg`。若存在则执行 Remux 封装为标准 MP4，若不存在则退避为二进制拼接，并清理 `temp/`。

### 6.2 模块化测试建议
* **测试用例 1**：WAF 绕过及页面解析测试。测试使用 `curl_cffi` 模拟的客户端在含有防爬防护的页面下，能否正常拉取 HTML 并解出正确的 M3U8 地址。
* **测试用例 2**：高并发与断点续传测试。在不断电或人工终止下载的场景下，验证重启脚本后已完成的分片是否能被正确跳过，以及限制并发后的网络请求稳定性。

---
