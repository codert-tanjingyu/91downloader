# 视频 HLS 下载器

基于 Python 的 HLS 视频下载工具，支持自动页面解析、绕过 WAF 防爬、AES-128 解密、多线程并发下载，并最终合并为 `.mp4` 文件。

---

## 特性

| 功能 | 说明 |
|---|---|
| **WAF 绕过** | 使用 `curl_cffi` 模拟 Chrome TLS/JA3 指纹 |
| **代理支持** | Socks5 / HTTP 代理，伪造 X-Forwarded-For |
| **页面解析** | 支持 strencode XOR 混淆、JSON 嵌入、video 标签等多策略提取 |
| **Playwright** | 可选动态拦截模式，用 `--use-playwright` 开启 |
| **多级 M3U8** | 自动选最高码率子列表，或 `--interactive` 手动选择 |
| **音视频分离流** | 自动识别并分别下载，FFmpeg 合路 |
| **AES-128 解密** | 支持显式 IV 及序列号推导 IV，PKCS7 容错去填充 |
| **断点续传** | JSON 状态文件 + TS 头校验，重启自动跳过已完成分片 |
| **自适应限速** | 检测 429/503 自动降低并发，恢复后逐步提升 |
| **磁盘预检** | 下载前估算所需空间，不足时提示或中止 |
| **FFmpeg 优先** | 优先 Remux 为 MP4，无 FFmpeg 时降级为 .ts 拼接 |

---

## 安装

### 1. 克隆项目

```bash
git clone <repo_url>
cd 91downloader
```

### 2. 创建虚拟环境（推荐）

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# Linux / macOS
source .venv/bin/activate
```

### 3. 安装依赖

```bash
pip install -r requirements.txt
```

### 4. 安装 FFmpeg（推荐）

- **Windows**：从 [ffmpeg.org](https://ffmpeg.org/download.html) 下载，将 `ffmpeg.exe` 所在目录添加至系统 PATH
- **macOS**：`brew install ffmpeg`
- **Linux**：`sudo apt install ffmpeg`

> 若不安装 FFmpeg，程序将降级输出 `.ts` 文件（仍可正常播放）。

### 5. （可选）安装 Playwright 浏览器内核

> 仅当需要使用 `--use-playwright` 动态拦截模式时才需要：

```bash
playwright install chromium
```

---

## 配置

编辑项目根目录的 `.env` 文件：

```dotenv
# 代理（留空则不使用）
PROXY=socks5://127.0.0.1:7891

# Cookie（将浏览器导出的完整 Cookie 字符串粘贴至此）
COOKIE=

# Referer（默认已填写，通常无需修改）
REFERER=https://www.91porn.com/
```

---

## 使用方法

### 基本用法（自动提取 M3U8）

```bash
python main.py "https://www.91porn.com/view_video.php?viewkey=xxxxxxxxxx"
```

### 直接传入 M3U8 链接

```bash
python main.py "https://example.com/hls/index.m3u8" --m3u8
```

### 常用参数

```bash
# 指定输出文件名
python main.py <URL> --output my_video

# 指定代理（覆盖 .env）
python main.py <URL> --proxy socks5://127.0.0.1:7891

# 调整并发数（默认 8）
python main.py <URL> --concurrency 4

# 交互式选择清晰度（Master Playlist）
python main.py <URL> --interactive

# 使用 Playwright 动态拦截（需先 playwright install chromium）
python main.py <URL> --use-playwright

# 合并后保留临时分片文件
python main.py <URL> --no-cleanup

# 开启详细日志
python main.py <URL> --verbose
```

### 完整参数列表

```
positional arguments:
  url                   视频页面 URL 或 M3U8 直链

options:
  --m3u8                将 url 视为 M3U8 直链，跳过页面解析
  --output, -o          输出文件名（不含扩展名）
  --proxy               代理地址，覆盖 .env 中的 PROXY
  --cookie              Cookie 字符串，覆盖 .env 中的 COOKIE
  --concurrency, -c     并发下载分片数（默认 8）
  --use-playwright      启用 Playwright 动态拦截
  --interactive         交互式清晰度选择
  --no-cleanup          保留 temp/ 临时文件
  --verbose, -v         DEBUG 日志
```

---

## 目录结构

```
91downloader/
├── config.py             # 全局配置
├── main.py               # 程序入口
├── .env                  # 敏感配置（代理、Cookie 等）
├── requirements.txt      # 依赖清单
│
├── core/
│   ├── decoder.py        # strencode XOR 解密
│   ├── extractor.py      # 页面解析与 M3U8 URL 提取
│   ├── parser.py         # M3U8 解析、Key 缓存
│   ├── downloader.py     # 异步并发分片下载
│   └── merger.py         # 解密合并 + FFmpeg 封装
│
├── utils/
│   └── http_client.py    # 统一 HTTP 会话（curl_cffi）
│
├── temp/                 # 临时分片目录（自动创建）
└── output/               # 最终视频输出目录（自动创建）
```

---

## 断点续传

程序会在 `temp/download_state.json` 中记录每个分片的下载状态。
若中途中断（Ctrl+C），重新运行相同命令即可自动跳过已完成的分片，继续下载。

---

## 常见问题

**Q：下载失败，提示 HTTP 403 或 TLS 错误？**
- 检查 `.env` 中的 `PROXY` 是否正确配置
- 尝试在浏览器中访问目标页面，将 Cookie 导出填入 `.env`
- 尝试添加 `--use-playwright` 参数

**Q：提示找不到 M3U8 URL？**
- 目标页面可能更新了加密方式，尝试 `--use-playwright` 模式

**Q：合并后视频无声？**
- 确保 FFmpeg 已正确安装且在系统 PATH 中
- 运行时添加 `--verbose` 查看 FFmpeg 输出日志

**Q：如何加快下载速度？**
- 增大 `--concurrency`（如 `--concurrency 16`）
- 确保代理带宽充足
