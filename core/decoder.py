"""
core/decoder.py — 密文解码模块

职责：
  - 复现目标网站 JavaScript 中的 strencode/XOR 解码算法
  - 将混淆的 Base64 密文还原为明文 M3U8 URL 或播放信息
"""

import base64
import logging

logger = logging.getLogger(__name__)


def str_decode(encrypted_str: str, key: str) -> str:
    """
    等价于网站 JS 中 strencode(str, key) 的解码逻辑。

    算法：
      1. 对密文进行 Base64 解码，得到字节序列
      2. 与 key 进行循环 XOR 运算（key 按字符索引取模循环）
      3. 将结果字节序列解码为 UTF-8 字符串

    参数：
        encrypted_str: Base64 编码的密文字符串
        key: XOR 解密密钥（不能为空）

    返回：
        解密后的明文字符串

    异常：
        ValueError: key 为空或解码过程失败
    """
    if not key:
        raise ValueError("解密密钥 (key) 不能为空")

    # 兼容不含 padding 的 Base64 字符串
    padding = 4 - len(encrypted_str) % 4
    if padding != 4:
        encrypted_str += "=" * padding

    try:
        decoded_bytes = base64.b64decode(encrypted_str)
    except Exception as exc:
        raise ValueError(f"Base64 解码失败: {exc}") from exc

    key_len = len(key)
    result = bytearray()

    for i, byte_val in enumerate(decoded_bytes):
        k_char = ord(key[i % key_len])
        result.append(byte_val ^ k_char)

    try:
        plaintext = result.decode("utf-8", errors="ignore")
    except Exception as exc:
        raise ValueError(f"UTF-8 解码失败: {exc}") from exc

    logger.debug("str_decode 成功，输出长度: %d 字符", len(plaintext))
    return plaintext


def try_extract_m3u8_from_plaintext(plaintext: str) -> str | None:
    """
    尝试从解密后的明文中提取 M3U8 URL。

    某些站点解密结果直接就是 URL，另一些则嵌套在 JSON 或 JS 代码中。
    本函数先做简单的 URL 扫描，无法确定时原样返回调用方自行处理。

    参数：
        plaintext: str_decode 的输出

    返回：
        M3U8 URL 字符串，或 None（需调用方进一步解析）
    """
    import re

    # 尝试直接匹配 http(s):// 开头含 .m3u8 的 URL
    pattern = r'https?://[^\s\'"<>]+\.m3u8[^\s\'"<>]*'
    match = re.search(pattern, plaintext)
    if match:
        url = match.group(0)
        logger.info("从明文中提取到 M3U8 URL: %s", url)
        return url

    logger.debug("未能从明文中直接提取 M3U8 URL，返回原始明文供上层解析")
    return None
