#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fleet_core -- MCP Fleet v5.2 安全与可靠性底座（纯 Python 标准库，3.6+）。

本模块是 v5.2 协议的安全核心，只依赖标准库、不 import 项目内其他模块，
供 `mcp_agent.py` / `fleet_proto.py` / `fleet_install.py` 等集成方按需注入使用：

  - 身份        Ed25519（RFC 8032）自实现 keygen/sign/verify
  - 密钥交换    X25519（RFC 7748）自实现
  - 对称加密    HKDF-SHA256 + SHA-256 CTR 流 + HMAC-SHA256（encrypt-then-MAC）
  - 传输鉴权    请求签名 sign_request / verify_request + ReplayGuard 防重放
  - 配对邀请    Invite（fleet://join/...）+ pair_proof（SAS 短认证串 + 配对密钥）
  - 执行策略    ExecPolicy（危险命令拒绝 / 审批清单 / 路径 jail / 超时上限）
  - 脱敏        redact()
  - 审计        AuditChain 哈希链 JSONL（可定位篡改行号）
  - 可观测性    Metrics（counter/gauge/histogram + Prometheus 文本）
  - TLS 自签    ensure_self_signed_cert / cert_fingerprint（无 openssl 时降级）
  - 持久化      atomic_write / append_jsonl / read_jsonl（0600 + fsync + 并发安全）

设计约束（详见 references/security-model.md）：
  * 不用任何第三方依赖；不得反向依赖节点/协议/CLI 模块（保持单向依赖）。
  * 落盘敏感文件权限 0600、目录 0700；写文件一律原子写。
  * 纯 Python 大整数运算**不是常数时间**，只作为「无第三方依赖」场景下的
    可用实现；高价值场景应替换为 libsodium / cryptography 提供的原生后端。
  * 单向静态 X25519 交换不具备完美前向保密（PFS），需要 PFS 时应每会话
    重新生成临时密钥并丢弃旧密钥。

版本：5.2.2
"""

import base64
import binascii
import collections
import contextlib
import errno
import hashlib
import hmac
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time

try:                     # POSIX 才有的跨进程文件锁；Windows 下降级为进程内锁
    import fcntl
except ImportError:      # pragma: no cover - 仅在 Windows 上走这里
    fcntl = None

VERSION = "5.2.10"

#: 落盘文件默认权限（仅属主可读写）
FILE_MODE = 0o600
#: 目录默认权限（仅属主可进入）
DIR_MODE = 0o700

#: 密封数据版本前缀：v5.2 第 1 版格式
SEAL_PREFIX = "enc2$"
#: 密封数据 nonce 长度（字节）
SEAL_NONCE_LEN = 16
#: 密封数据 HMAC 标签长度（字节）
SEAL_TAG_LEN = 32

#: 邀请 URI 前缀
INVITE_PREFIX = "fleet://join/"

#: 请求签名版本前缀
REQUEST_SIG_PREFIX = "v5.2"

#: 各类签名/转录的域分隔标签（防止跨用途签名混淆）
_DOMAIN_INVITE = b"fleet1|invite|"
_DOMAIN_PAIR = b"fleet1|pair-transcript|"
_DOMAIN_PROOF = b"fleet1|pair-proof|"

__all__ = [
    "VERSION", "FILE_MODE", "DIR_MODE", "SEAL_PREFIX", "INVITE_PREFIX",
    "Identity", "verify_signature", "node_id_from_public_key",
    "x25519", "x25519_keypair",
    "hkdf_sha256", "seal", "open_sealed", "derive_pair_key",
    "ReplayGuard", "sign_request", "verify_request",
    "Invite", "pair_proof",
    "ExecPolicy", "redact",
    "AuditChain",
    "Metrics",
    "ensure_self_signed_cert", "cert_fingerprint",
    "atomic_write", "append_jsonl", "read_jsonl",
    "now_ms", "constant_time_eq", "check_clock_skew",
]


# ===========================================================================
# 0. 时间 / 编码 / 常量时间比较
# ===========================================================================

def _now():
    """当前 Unix 时间戳（秒，float）。

    单独抽成函数是为了让测试可以注入时间（monkeypatch ``fleet_core._now``），
    从而验证「时间窗过期」等行为而不必真的 sleep。
    """
    return time.time()


def now_ms():
    """当前 Unix 时间戳（毫秒，int）。用于审计记录/邀请时间戳等。"""
    return int(_now() * 1000)


def constant_time_eq(a, b):
    """常数时间比较两个字符串或字节串。

    :param a: str 或 bytes
    :param b: str 或 bytes
    :return: 相等返回 True；类型不一致/None 返回 False（不抛异常）
    """
    if isinstance(a, str) and isinstance(b, str):
        a = a.encode("utf-8")
        b = b.encode("utf-8")
    if not isinstance(a, (bytes, bytearray)) or not isinstance(b, (bytes, bytearray)):
        return False
    return hmac.compare_digest(bytes(a), bytes(b))


def check_clock_skew(peer_ts, max_skew_s=300):
    """检查对端时间戳与本地时间的偏差是否在允许窗口内。

    :param peer_ts: 对端时间戳（秒，可为 int/float/数字字符串）
    :param max_skew_s: 允许的最大偏差秒数
    :return: ``(ok: bool, skew_s: int)``，skew_s = 本地时间 - 对端时间（四舍五入取整）
    :raises ValueError: peer_ts 不是数字
    """
    try:
        peer = float(peer_ts)
    except (TypeError, ValueError):
        raise ValueError("peer_ts 必须是数字，收到 %r" % (peer_ts,))
    skew = _now() - peer
    return (abs(skew) <= float(max_skew_s), int(round(skew)))


def _as_bytes(value, what="数据"):
    """把 str/bytes/bytearray 统一转成 bytes；其他类型抛 TypeError。"""
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    if isinstance(value, str):
        return value.encode("utf-8")
    raise TypeError("%s 必须是 str 或 bytes，收到 %s" % (what, type(value).__name__))


def _b64e(raw):
    """标准 base64 编码（bytes -> str）。"""
    return base64.b64encode(raw).decode("ascii")


def _b64d(text):
    """标准 base64 解码（str -> bytes）；非法输入抛 ValueError。"""
    if not isinstance(text, str):
        raise ValueError("base64 输入必须是 str")
    try:
        return base64.b64decode(text.encode("ascii"), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("base64 解码失败: %s" % exc)


def _b64u_e(raw):
    """URL-safe base64 编码并去掉 '=' 填充（用于 URI 承载 JSON）。"""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64u_d(text):
    """URL-safe base64 解码，自动补齐 padding；非法输入抛 ValueError。"""
    if not isinstance(text, str):
        raise ValueError("base64url 输入必须是 str")
    pad = "=" * (-len(text) % 4)
    try:
        return base64.urlsafe_b64decode((text + pad).encode("ascii"))
    except (binascii.Error, ValueError) as exc:
        raise ValueError("base64url 解码失败: %s" % exc)


def _canon_json(obj):
    """规范化 JSON 编码：键排序、无多余空白、UTF-8 bytes。

    签名基串、哈希链、邀请载荷都使用这一编码，保证跨进程/跨平台字节一致。
    不可 JSON 序列化的值用 ``str()`` 兜底（确定性、不会静默丢字段）。
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str).encode("utf-8")


def _new_nonce(nbytes=16):
    """生成随机 nonce（十六进制字符串，默认 128 bit）。"""
    return os.urandom(nbytes).hex()


# ===========================================================================
# 1. Ed25519（RFC 8032）-- 纯 Python 实现
#
# 曲线：twisted Edwards -x^2 + y^2 = 1 + d*x^2*y^2  over GF(2^255-19)
# 点用扩展坐标 (X, Y, Z, T)，x = X/Z, y = Y/Z, x*y = T/Z。
# 说明：这是「可读优先」的实现；大整数运算不是常数时间（见文档已知限制）。
# ===========================================================================

_ED_P = 2 ** 255 - 19
_ED_L = 2 ** 252 + 27742317777372353535851937790883648493
_ED_D = -121665 * pow(121666, _ED_P - 2, _ED_P) % _ED_P
_ED_I = pow(2, (_ED_P - 1) // 4, _ED_P)
_ED_BY = 4 * pow(5, _ED_P - 2, _ED_P) % _ED_P


def _ed_xrecover(y):
    """由 y 恢复曲线点 x（返回偶数 x；无解返回 None）。"""
    xx = (y * y - 1) * pow(_ED_D * y * y + 1, _ED_P - 2, _ED_P) % _ED_P
    x = pow(xx, (_ED_P + 3) // 8, _ED_P)
    if (x * x - xx) % _ED_P != 0:
        x = x * _ED_I % _ED_P
    if (x * x - xx) % _ED_P != 0:
        return None
    if x % 2 != 0:
        x = _ED_P - x
    return x


_ED_BX = _ed_xrecover(_ED_BY)
_ED_BASE = (_ED_BX, _ED_BY, 1, _ED_BX * _ED_BY % _ED_P)
_ED_IDENTITY = (0, 1, 1, 0)


def _ed_add(p1, p2):
    """扩展坐标点加法（a=-1 的 unified add-2008-hwcd-3）。"""
    x1, y1, z1, t1 = p1
    x2, y2, z2, t2 = p2
    a = (y1 - x1) * (y2 - x2) % _ED_P
    b = (y1 + x1) * (y2 + x2) % _ED_P
    c = t1 * 2 * _ED_D * t2 % _ED_P
    d = z1 * 2 * z2 % _ED_P
    e = b - a
    f = d - c
    g = d + c
    h = b + a
    return (e * f % _ED_P, g * h % _ED_P, f * g % _ED_P, e * h % _ED_P)


def _ed_dbl(p1):
    """扩展坐标点倍加（dbl-2008-hwcd，a=-1）。"""
    x1, y1, z1, _t1 = p1
    a = x1 * x1 % _ED_P
    b = y1 * y1 % _ED_P
    c = 2 * z1 * z1 % _ED_P
    e = 2 * x1 * y1 % _ED_P
    g = (b - a) % _ED_P
    f = (g - c) % _ED_P
    h = (-(a + b)) % _ED_P
    return (e * f % _ED_P, g * h % _ED_P, f * g % _ED_P, e * h % _ED_P)


def _ed_mul(scalar, point):
    """标量乘 ``scalar * point``（256 位 double-and-add）。"""
    result = _ED_IDENTITY
    for bit in range(255, -1, -1):
        result = _ed_dbl(result)
        if (scalar >> bit) & 1:
            result = _ed_add(result, point)
    return result


def _ed_encode(point):
    """点编码为 32 字节：小端 y | (x & 1) << 255。"""
    x, y, z, _t = point
    zi = pow(z, _ED_P - 2, _ED_P)
    x = x * zi % _ED_P
    y = y * zi % _ED_P
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def _ed_decode(raw):
    """32 字节 -> 点；非法编码返回 None（不抛异常）。"""
    if not isinstance(raw, (bytes, bytearray)) or len(raw) != 32:
        return None
    value = int.from_bytes(bytes(raw), "little")
    sign = (value >> 255) & 1
    y = value & ((1 << 255) - 1)
    if y >= _ED_P:
        return None
    x = _ed_xrecover(y)
    if x is None:
        return None
    if x == 0 and sign:
        return None
    if (x & 1) != sign:
        x = _ED_P - x
    if (-x * x + y * y - 1 - _ED_D * x * x * y * y) % _ED_P != 0:
        return None
    return (x, y, 1, x * y % _ED_P)


def _ed_clamp(digest32):
    """Ed25519 私钥标量裁剪（清低 3 位、清 bit255、置 bit254）。"""
    a = int.from_bytes(digest32, "little")
    a &= ~7
    a &= (1 << 255) - 1
    a |= 1 << 254
    return a


def _ed_expand(seed):
    """由 32 字节种子扩展出 (标量 a, prefix32, 公钥点 A, 公钥编码)。"""
    if not isinstance(seed, (bytes, bytearray)) or len(seed) != 32:
        raise ValueError("Ed25519 种子必须是 32 字节")
    digest = hashlib.sha512(bytes(seed)).digest()
    a = _ed_clamp(digest[:32])
    point = _ed_mul(a, _ED_BASE)
    return a, digest[32:], point, _ed_encode(point)


def _ed_public_key(seed):
    """由 32 字节种子派生 32 字节 Ed25519 公钥。"""
    return _ed_expand(seed)[3]


def _ed_sign_raw(seed, msg):
    """对 bytes 消息做 Ed25519 签名，返回 64 字节签名。"""
    a, prefix, _point, pub = _ed_expand(seed)
    r = int.from_bytes(hashlib.sha512(prefix + msg).digest(), "little") % _ED_L
    r_encoded = _ed_encode(_ed_mul(r, _ED_BASE))
    k = int.from_bytes(hashlib.sha512(r_encoded + pub + msg).digest(), "little") % _ED_L
    s = (r + k * a) % _ED_L
    return r_encoded + s.to_bytes(32, "little")


def _ed_verify_raw(pub, msg, sig):
    """校验 32 字节公钥 + 64 字节签名；任何非法输入返回 False。"""
    if not isinstance(pub, (bytes, bytearray)) or len(pub) != 32:
        return False
    if not isinstance(sig, (bytes, bytearray)) or len(sig) != 64:
        return False
    sig = bytes(sig)
    r_encoded = sig[:32]
    s = int.from_bytes(sig[32:], "little")
    if s >= _ED_L:                     # 拒绝可延展签名（malleability）
        return False
    a_point = _ed_decode(pub)
    if a_point is None:
        return False
    k = int.from_bytes(hashlib.sha512(r_encoded + bytes(pub) + msg).digest(),
                       "little") % _ED_L
    neg_a = (_ED_P - a_point[0], a_point[1], a_point[2], _ED_P - a_point[3])
    check = _ed_add(_ed_mul(s, _ED_BASE), _ed_mul(k, neg_a))
    return hmac.compare_digest(_ed_encode(check), r_encoded)


# ===========================================================================
# 2. X25519（RFC 7748）-- 纯 Python Montgomery ladder
# ===========================================================================

_X25519_BASE = (b"\x09" + b"\x00" * 31)


def x25519(private_key, public_key):
    """RFC 7748 X25519 标量乘：返回 32 字节共享点（little-endian u 坐标）。

    :param private_key: 32 字节私钥（函数内部按 RFC 裁剪）
    :param public_key: 32 字节对端 u 坐标（函数内部忽略最高位）
    :return: 32 字节共享秘密
    :raises ValueError: 任一参数不是 32 字节

    注意：低阶公钥会得到全零输出；本函数按 RFC 原样返回，调用方（如
    :func:`pair_proof`）必须自行拒绝全零共享秘密。
    """
    for name, value in (("private_key", private_key), ("public_key", public_key)):
        if not isinstance(value, (bytes, bytearray)) or len(value) != 32:
            raise ValueError("X25519 %s 必须是 32 字节" % name)
    k = bytearray(private_key)
    k[0] &= 248
    k[31] &= 127
    k[31] |= 64
    scalar = int.from_bytes(bytes(k), "little")
    p = _ED_P
    x1 = int.from_bytes(bytes(public_key), "little") & ((1 << 255) - 1)
    x2, z2, x3, z3 = 1, 0, x1, 1
    swap = 0
    for t in range(254, -1, -1):
        kt = (scalar >> t) & 1
        swap ^= kt
        if swap:
            x2, x3 = x3, x2
            z2, z3 = z3, z2
        swap = kt
        a = (x2 + z2) % p
        aa = a * a % p
        b = (x2 - z2) % p
        bb = b * b % p
        e = (aa - bb) % p
        c = (x3 + z3) % p
        d = (x3 - z3) % p
        da = d * a % p
        cb = c * b % p
        x3 = (da + cb) % p
        x3 = x3 * x3 % p
        z3 = (da - cb) % p
        z3 = x1 * z3 % p * z3 % p
        x2 = aa * bb % p
        z2 = e * (aa + 121665 * e) % p
    if swap:
        x2, x3 = x3, x2
        z2, z3 = z3, z2
    return (x2 * pow(z2, p - 2, p) % p).to_bytes(32, "little")


def x25519_keypair():
    """生成 X25519 临时密钥对。

    :return: ``(priv32, pub32)`` 两个 bytes
    """
    priv = os.urandom(32)
    return (priv, x25519(priv, _X25519_BASE))


# ===========================================================================
# 3. HKDF / 密封加密（SHA-256 CTR 流 + HMAC-SHA256，encrypt-then-MAC）
#
# 令牌格式：``enc2$<base64(nonce[16] | ciphertext | tag[32])>``
#   enc_key = HKDF-SHA256(key, salt=nonce, info="fleet/v5.2/seal/enc")
#   mac_key = HKDF-SHA256(key, salt=nonce, info="fleet/v5.2/seal/mac")
#   keystream_i = HMAC-SHA256(enc_key, "ctr|" + nonce + i.to_bytes(8,"big"))
#   tag = HMAC-SHA256(mac_key, "enc2$tag|" + nonce + len(aad) + aad + ct)
# ===========================================================================

def hkdf_sha256(ikm, salt, info, length=32):
    """RFC 5869 HKDF-SHA256（extract + expand）。

    :param ikm: 输入密钥材料（bytes）
    :param salt: 盐（bytes；为空时按 RFC 使用 32 字节零盐）
    :param info: 上下文信息（bytes）
    :param length: 输出长度（1 .. 255*32）
    :return: ``length`` 字节伪随机密钥
    :raises ValueError: 长度越界
    """
    ikm = _as_bytes(ikm, "ikm")
    salt = _as_bytes(salt, "salt")
    info = _as_bytes(info, "info")
    try:
        length = int(length)
    except (TypeError, ValueError):
        raise ValueError("HKDF length 必须是整数")
    if length <= 0 or length > 255 * 32:
        raise ValueError("HKDF length 必须在 1..%d 之间" % (255 * 32))
    if not salt:
        salt = b"\x00" * 32
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    out = bytearray()
    block = b""
    counter = 1
    while len(out) < length:
        block = hmac.new(prk, block + info + bytes([counter]),
                         hashlib.sha256).digest()
        out += block
        counter += 1
    return bytes(out[:length])


def _seal_keystream(key, nonce, length):
    """SHA-256/HMAC 构造的 CTR 型密钥流。"""
    out = bytearray()
    counter = 0
    limit = (length // 32) + 2
    while len(out) < length:
        if counter > limit + 2:                      # 防御性上限
            raise ValueError("密钥流长度异常")
        out += hmac.new(key, b"ctr|" + nonce + counter.to_bytes(8, "big"),
                        hashlib.sha256).digest()
        counter += 1
    return bytes(out[:length])


def _xor_bytes(data, stream):
    """等长字节串 XOR。"""
    if len(data) != len(stream):
        raise ValueError("XOR 长度不一致")
    return bytes(bytearray(a ^ b for a, b in zip(data, stream)))


def _seal_tag(mac_key, nonce, aad, ciphertext):
    """计算加密后认证标签（覆盖版本前缀、nonce、AAD 长度与密文）。"""
    msg = (SEAL_PREFIX.encode("ascii") + b"tag|" + nonce
           + len(aad).to_bytes(4, "big") + aad + ciphertext)
    return hmac.new(mac_key, msg, hashlib.sha256).digest()


def seal(key, plaintext, aad=b""):
    """对称密封：返回 ``"enc2$<base64(nonce+ct+tag)>"`` 字符串。

    :param key: 对称密钥（>= 16 字节；推荐 HKDF 派生的 32 字节）
    :param plaintext: 明文（str/bytes）
    :param aad: 附加认证数据（str/bytes，可为空）；解密时必须提供相同值
    :return: 带版本前缀的 base64 令牌
    :raises ValueError: 密钥过短
    :raises TypeError: 参数类型非法
    """
    key = _as_bytes(key, "key")
    if len(key) < 16:
        raise ValueError("密封密钥至少 16 字节，收到 %d 字节" % len(key))
    plaintext = _as_bytes(plaintext, "plaintext")
    aad = _as_bytes(aad, "aad")
    nonce = os.urandom(SEAL_NONCE_LEN)
    enc_key = hkdf_sha256(key, nonce, b"fleet/v5.2/seal/enc", 32)
    mac_key = hkdf_sha256(key, nonce, b"fleet/v5.2/seal/mac", 32)
    ciphertext = _xor_bytes(plaintext, _seal_keystream(enc_key, nonce, len(plaintext)))
    tag = _seal_tag(mac_key, nonce, aad, ciphertext)
    return SEAL_PREFIX + _b64e(nonce + ciphertext + tag)


def open_sealed(key, token, aad=b""):
    """解开 :func:`seal` 产生的令牌，返回明文字节。

    :param key: 与加密时相同的对称密钥
    :param token: ``enc2$...`` 令牌
    :param aad: 与加密时相同的附加认证数据
    :return: 明文 bytes
    :raises ValueError: 前缀/长度/密钥/AAD 任一不匹配，或数据被篡改
    """
    key = _as_bytes(key, "key")
    if len(key) < 16:
        raise ValueError("密封密钥至少 16 字节")
    aad = _as_bytes(aad, "aad")
    if not isinstance(token, str) or not token.startswith(SEAL_PREFIX):
        raise ValueError("不是 %s 密封数据（缺少版本前缀）" % SEAL_PREFIX)
    try:
        raw = _b64d(token[len(SEAL_PREFIX):])
    except ValueError as exc:
        raise ValueError("密封数据 base64 非法: %s" % exc)
    if len(raw) < SEAL_NONCE_LEN + SEAL_TAG_LEN:
        raise ValueError("密封数据过短（至少 %d 字节）"
                         % (SEAL_NONCE_LEN + SEAL_TAG_LEN))
    nonce = raw[:SEAL_NONCE_LEN]
    ciphertext = raw[SEAL_NONCE_LEN:-SEAL_TAG_LEN]
    tag = raw[-SEAL_TAG_LEN:]
    mac_key = hkdf_sha256(key, nonce, b"fleet/v5.2/seal/mac", 32)
    expect = _seal_tag(mac_key, nonce, aad, ciphertext)
    if not hmac.compare_digest(tag, expect):
        raise ValueError("密封数据完整性校验失败（密钥/AAD 不匹配或数据被篡改）")
    enc_key = hkdf_sha256(key, nonce, b"fleet/v5.2/seal/enc", 32)
    return _xor_bytes(ciphertext, _seal_keystream(enc_key, nonce, len(ciphertext)))


def derive_pair_key(shared_secret, transcript):
    """由 X25519 共享秘密 + 配对转录派生 32 字节配对密钥。

    :param shared_secret: 32 字节 X25519 共享秘密
    :param transcript: 双方一致的配对转录字节串（含 node id / 公钥 / 临时公钥 / nonce）
    :return: 32 字节对称密钥
    """
    shared_secret = _as_bytes(shared_secret, "shared_secret")
    if len(shared_secret) != 32:
        raise ValueError("共享秘密必须是 32 字节")
    transcript = _as_bytes(transcript, "transcript")
    salt = hashlib.sha256(transcript).digest()
    return hkdf_sha256(shared_secret, salt, b"fleet/v5.2/pair-key", 32)


# ===========================================================================
# 4. 请求签名 / 防重放
#
# 签名基串（ASCII/UTF-8）：
#   "fleet1|METHOD|PATH|ts|nonce|sha256(body) hex"
# 请求头：
#   X-Fleet-Node  node_id（base32 前 16 位）
#   X-Fleet-Ts    秒级时间戳，格式化 "%.3f"
#   X-Fleet-Nonce 16 字节随机十六进制
#   X-Fleet-Sig   base64(Ed25519 签名)
# ===========================================================================

class ReplayGuard:
    """请求防重放守卫：时间窗 + (scope, nonce) 去重 + 容量上限。

    设计取舍：
      * 时间窗拒绝明显过期/未来的请求（默认 ±300s）。
      * nonce 去重保证同一 (scope, nonce) 只能成功一次。
      * 容量上限防止 nonce 表被无限撑大：超过 ``max_entries`` 时淘汰最久
        登记项（LRU 近似）。生产上窗口 300s 内请求量远小于 2 万，正常不会淘汰。
    """

    def __init__(self, window_s=300, max_entries=20000):
        """初始化守卫。

        :param window_s: 允许的时间偏差窗口（秒）
        :param max_entries: nonce 表容量上限
        :raises ValueError: 参数非正
        """
        window_s = int(window_s)
        max_entries = int(max_entries)
        if window_s <= 0:
            raise ValueError("window_s 必须为正整数")
        if max_entries <= 0:
            raise ValueError("max_entries 必须为正整数")
        self.window_s = window_s
        self.max_entries = max_entries
        self._lock = threading.Lock()
        self._seen = collections.OrderedDict()   # (scope, nonce) -> 登记时刻 ts

    def check(self, scope, nonce, ts):
        """登记并检查一次请求。

        :param scope: 作用域（通常为对端 node_id）
        :param nonce: 请求随机数（非空字符串）
        :param ts: 请求时间戳（秒）
        :return: 首次出现且时间窗内为 True；重放/过期/参数非法为 False
        """
        if not isinstance(scope, str) or not scope:
            return False
        if not isinstance(nonce, str) or not nonce:
            return False
        try:
            ts = float(ts)
        except (TypeError, ValueError):
            return False
        now = _now()
        if abs(now - ts) > self.window_s:
            return False
        key = (scope, nonce)
        with self._lock:
            if key in self._seen:
                return False
            cutoff = now - 2 * self.window_s
            while self._seen:
                oldest_key = next(iter(self._seen))
                if self._seen[oldest_key] < cutoff:
                    self._seen.popitem(last=False)
                else:
                    break
            while len(self._seen) >= self.max_entries:
                self._seen.popitem(last=False)
            self._seen[key] = now
            return True


def _request_basis(method, path, ts_text, nonce, body):
    """构造请求签名基串（bytes）。"""
    body_hash = hashlib.sha256(body).hexdigest()
    return ("%s|%s|%s|%s|%s|%s" % (REQUEST_SIG_PREFIX,
                                   str(method).upper(), path,
                                   ts_text, nonce, body_hash)).encode("utf-8")


def sign_request(identity, method, path, body, ts=None, nonce=None):
    """为出站 HTTP 请求生成 v5.2 签名头。

    :param identity: :class:`Identity` 实例
    :param method: HTTP 方法（大小写不敏感，签名内统一大写）
    :param path: 请求路径（含 query 时调用方自行决定是否纳入；本函数原样使用）
    :param body: 请求体（str/bytes，可为空）
    :param ts: 时间戳（秒），默认当前时间
    :param nonce: 随机数，默认安全随机
    :return: 含 4 个 ``X-Fleet-*`` 头的 dict
    :raises TypeError: identity/参数类型非法
    """
    if not isinstance(identity, Identity):
        raise TypeError("identity 必须是 Identity 实例")
    if not isinstance(path, str) or not path:
        raise ValueError("path 必须是非空字符串")
    body = _as_bytes(body if body is not None else b"", "body")
    ts_value = _now() if ts is None else float(ts)
    ts_text = "%.3f" % ts_value
    nonce = nonce if nonce else _new_nonce()
    if not isinstance(nonce, str):
        raise TypeError("nonce 必须是字符串")
    basis = _request_basis(method, path, ts_text, nonce, body)
    return {
        "X-Fleet-Node": identity.node_id,
        "X-Fleet-Ts": ts_text,
        "X-Fleet-Nonce": nonce,
        "X-Fleet-Sig": identity.sign(basis),
    }


_NONCE_RE = re.compile(r"^[A-Za-z0-9_\-]{8,128}$")


def _header_get(headers, name):
    """大小写不敏感地从 headers dict 取头（缺失返回 None）。"""
    if not isinstance(headers, dict):
        return None
    target = name.lower()
    for key in headers:
        if isinstance(key, str) and key.lower() == target:
            return headers[key]
    return None


def verify_request(public_key_b64, method, path, body, headers, guard=None,
                   max_skew_s=300):
    """校验入站请求的 v5.2 签名、时间窗与 nonce 唯一性。

    :param public_key_b64: 对端 Ed25519 公钥（base64）
    :param method: HTTP 方法
    :param path: 请求路径
    :param body: 请求体（str/bytes）
    :param headers: 请求头 dict（键大小写不敏感）
    :param guard: 可选 :class:`ReplayGuard`；提供时执行 nonce 去重
    :param max_skew_s: 允许的时间偏差（秒）
    :return: ``(ok: bool, reason: str)``，成功时 reason 为 ``""``
    """
    if not isinstance(path, str) or not path:
        return (False, "path 非法")
    try:
        body = _as_bytes(body if body is not None else b"", "body")
    except TypeError as exc:
        return (False, str(exc))
    node = _header_get(headers, "X-Fleet-Node")
    ts_text = _header_get(headers, "X-Fleet-Ts")
    nonce = _header_get(headers, "X-Fleet-Nonce")
    sig = _header_get(headers, "X-Fleet-Sig")
    if not (isinstance(node, str) and isinstance(ts_text, str)
            and isinstance(nonce, str) and isinstance(sig, str)):
        return (False, "缺少或非法的 X-Fleet-* 签名头")
    try:
        ts_value = float(ts_text)
    except (TypeError, ValueError):
        return (False, "X-Fleet-Ts 不是合法数字")
    if not _NONCE_RE.match(nonce):
        return (False, "X-Fleet-Nonce 格式非法")
    expected_node = node_id_from_public_key(public_key_b64)
    if not expected_node:
        return (False, "公钥非法")
    if not constant_time_eq(node, expected_node):
        return (False, "X-Fleet-Node 与公钥不匹配")
    ok, skew = check_clock_skew(ts_value, max_skew_s)
    if not ok:
        return (False, "时间戳超出允许窗口（skew=%ds）" % skew)
    basis = _request_basis(method, path, ts_text, nonce, body)
    if not verify_signature(public_key_b64, basis, sig):
        return (False, "签名校验失败")
    if guard is not None:
        if not isinstance(guard, ReplayGuard):
            raise TypeError("guard 必须是 ReplayGuard 实例或 None")
        if not guard.check(node, nonce, ts_value):
            return (False, "重放请求（nonce 重复或时间窗过期）")
    return (True, "")


# ===========================================================================
# 5. 身份（Ed25519 密钥文件）
# ===========================================================================

def _node_id_from_pubkey(pub_raw):
    """32 字节公钥 -> node_id（base32(sha256(pub))[:16]，小写）。"""
    digest = hashlib.sha256(pub_raw).digest()
    return base64.b32encode(digest).decode("ascii").lower()[:16]


def _fingerprint_from_pubkey(pub_raw):
    """32 字节公钥 -> 人类可读指纹 "ABCD-EFGH-IJKL-MNOP"。"""
    hexdigest = hashlib.sha256(pub_raw).hexdigest().upper()[:16]
    return "-".join(hexdigest[i:i + 4] for i in range(0, 16, 4))


def node_id_from_public_key(public_key_b64):
    """由 base64 公钥计算 node_id；公钥非法返回 ``""``。"""
    try:
        raw = _b64d(public_key_b64)
    except (ValueError, TypeError):
        return ""
    if len(raw) != 32:
        return ""
    return _node_id_from_pubkey(raw)


class Identity:
    """Ed25519 节点身份。

    公开属性（契约固定）：

    - ``node_id``        base32(sha256(pubkey))[:16]，小写，用于协议内标识节点
    - ``fingerprint``    人类可读指纹 "ABCD-EFGH-IJKL-MNOP"，用于电话/扫码核对
    - ``public_key_b64`` base64(32 字节公钥)

    私钥只在内存中以 32 字节种子形式存在，通过 ``private_key_b64`` 属性按需导出；
    ``repr()`` 不会泄露私钥。
    """

    __slots__ = ("_seed", "_pub", "node_id", "fingerprint", "public_key_b64")

    def __init__(self, seed, public_key=None):
        """从 32 字节种子构造身份（不落盘）。

        :param seed: 32 字节 Ed25519 种子
        :param public_key: 可选 32 字节公钥；提供时用于校验一致性
        :raises ValueError: 种子长度错误或与公钥不一致
        """
        if not isinstance(seed, (bytes, bytearray)) or len(seed) != 32:
            raise ValueError("Ed25519 种子必须是 32 字节")
        self._seed = bytes(seed)
        derived = _ed_public_key(self._seed)
        if public_key is not None:
            if not isinstance(public_key, (bytes, bytearray)) or len(public_key) != 32:
                raise ValueError("公钥必须是 32 字节")
            if not hmac.compare_digest(derived, bytes(public_key)):
                raise ValueError("私钥与公钥不匹配（身份文件可能被篡改）")
        self._pub = derived
        self.public_key_b64 = _b64e(derived)
        self.node_id = _node_id_from_pubkey(derived)
        self.fingerprint = _fingerprint_from_pubkey(derived)

    @property
    def private_key_b64(self):
        """base64(32 字节种子)。仅应在落盘/备份时使用，切勿写日志。"""
        return _b64e(self._seed)

    @property
    def public_key(self):
        """32 字节公钥原始 bytes。"""
        return self._pub

    def sign(self, msg):
        """对消息做 Ed25519 签名。

        :param msg: str（按 UTF-8 编码）或 bytes
        :return: base64(64 字节签名)
        """
        return _b64e(_ed_sign_raw(self._seed, _as_bytes(msg, "msg")))

    def verify(self, msg, sig_b64):
        """用本身份公钥校验签名（便捷方法）。

        :return: 合法 True / 非法 False
        """
        return verify_signature(self.public_key_b64, msg, sig_b64)

    @classmethod
    def from_private_b64(cls, s):
        """从 base64(32 字节种子) 恢复身份。

        :raises ValueError: base64 非法或长度不是 32 字节
        """
        seed = _b64d(s)
        if len(seed) != 32:
            raise ValueError("Ed25519 私钥必须是 32 字节种子（base64 解码后 %d 字节）"
                             % len(seed))
        return cls(seed)

    @classmethod
    def load_or_create(cls, path):
        """从 ``path`` 加载身份；不存在则生成并以 0600 原子落盘。

        文件为 JSON（含私钥），权限强制 0600、父目录 0700。已存在但内容
        损坏/私钥与公钥不一致时**抛异常而不是悄悄重建**——重建会让已配对
        节点信任断裂，必须由人介入。

        :param path: 身份文件路径
        :return: :class:`Identity`
        :raises ValueError: 文件存在但损坏/字段缺失/公私钥不匹配
        :raises OSError: 读写失败
        """
        if not isinstance(path, str) or not path:
            raise ValueError("path 必须是非空字符串")
        path = os.path.abspath(path)
        if os.path.exists(path):
            try:
                with open(path, "rb") as handle:
                    raw = handle.read()
            except OSError as exc:
                raise OSError("读取身份文件失败: %s" % exc)
            if not raw.strip():
                raise ValueError("身份文件为空: %s" % path)
            try:
                doc = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as exc:
                raise ValueError("身份文件不是合法 JSON: %s（%s）" % (path, exc))
            if not isinstance(doc, dict):
                raise ValueError("身份文件格式非法（应为 JSON 对象）: %s" % path)
            for field in ("private_key_b64", "public_key_b64", "node_id"):
                if not isinstance(doc.get(field), str) or not doc.get(field):
                    raise ValueError("身份文件缺少字段 %s: %s" % (field, path))
            identity = cls.from_private_b64(doc["private_key_b64"])
            if doc["public_key_b64"] != identity.public_key_b64:
                raise ValueError("身份文件公钥与私钥不匹配（可能被篡改）: %s" % path)
            if doc["node_id"] != identity.node_id:
                raise ValueError("身份文件 node_id 与私钥不匹配（可能被篡改）: %s" % path)
            _best_effort_chmod(path, FILE_MODE)
            return identity
        identity = cls(os.urandom(32))
        doc = {
            "v": 1,
            "core_version": VERSION,
            "created_ms": now_ms(),
            "node_id": identity.node_id,
            "fingerprint": identity.fingerprint,
            "public_key_b64": identity.public_key_b64,
            "private_key_b64": identity.private_key_b64,
        }
        payload = json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        atomic_write(path, payload, FILE_MODE)
        return identity

    def __repr__(self):
        """调试表示：只含 node_id，不含私钥。"""
        return "<Identity node_id=%s fingerprint=%s>" % (self.node_id, self.fingerprint)


def verify_signature(public_key_b64, msg, sig_b64):
    """校验任意 msg 的 Ed25519 签名。

    任何输入错误（base64 非法、长度不对、点非法、签名可延展）都返回 False，
    不抛异常——这是网络边界上反复调用的函数，异常会拖垮调用方。

    :return: 合法 True / 非法 False
    """
    try:
        pub = _b64d(public_key_b64)
        sig = _b64d(sig_b64)
    except (ValueError, TypeError):
        return False
    try:
        msg_bytes = _as_bytes(msg, "msg")
    except TypeError:
        return False
    return _ed_verify_raw(pub, msg_bytes, sig)


# ===========================================================================
# 6. 邀请 / 配对（SAS 短认证串）
# ===========================================================================

class Invite:
    """配对邀请。

    字段（契约固定）：``v, name, node_id, pubkey, host, port, ts, ttl, nonce,
    caps, sig``。URI 形态：``fleet://join/<b64url(json)>``。

    安全属性：
      * 邀请由创建方 Ed25519 私钥签名，解析方用邀请内公钥验签；
      * 解析时校验 ``node_id`` 与 ``pubkey`` 一致（防止拼接伪造）；
      * ``ts + ttl`` 过期即拒绝；
      * 邀请只携带公钥，泄露邀请不会泄露私钥；但仍建议通过可信渠道传递，
        并在配对后用 SAS 短认证串人工核对。
    """

    FIELDS = ("v", "name", "node_id", "pubkey", "host", "port", "ts", "ttl",
              "nonce", "caps", "sig")

    def __init__(self, v=1, name="", node_id="", pubkey="", host="", port=0,
                 ts=0, ttl=3600, nonce="", caps=None, sig=""):
        """直接构造邀请对象（一般用 :meth:`create` / :meth:`parse`）。"""
        self.v = int(v)
        self.name = str(name)
        self.node_id = str(node_id)
        self.pubkey = str(pubkey)
        self.host = str(host)
        self.port = int(port)
        self.ts = int(ts)
        self.ttl = int(ttl)
        self.nonce = str(nonce)
        self.caps = dict(caps) if isinstance(caps, dict) else {}
        self.sig = str(sig)

    def _payload(self, include_sig=True):
        """返回用于序列化/签名的字段 dict。"""
        doc = {
            "v": self.v,
            "name": self.name,
            "node_id": self.node_id,
            "pubkey": self.pubkey,
            "host": self.host,
            "port": self.port,
            "ts": self.ts,
            "ttl": self.ttl,
            "nonce": self.nonce,
            "caps": self.caps,
        }
        if include_sig:
            doc["sig"] = self.sig
        return doc

    def _sign_basis(self):
        """邀请签名基串（域分隔 + 规范化 JSON，不含 sig 本身）。"""
        return _DOMAIN_INVITE + _canon_json(self._payload(include_sig=False))

    def to_uri(self):
        """序列化为 ``fleet://join/<b64url>`` 邀请 URI。

        :raises ValueError: 尚未签名（sig 为空）
        """
        if not self.sig:
            raise ValueError("邀请尚未签名，无法序列化")
        return INVITE_PREFIX + _b64u_e(_canon_json(self._payload(True)))

    def is_expired(self, now=None):
        """是否已过期（``ts + ttl <= now``）。"""
        current = _now() if now is None else float(now)
        return (self.ts + self.ttl) <= current

    def remaining_s(self, now=None):
        """剩余有效秒数（已过期返回 0 或负数原因由 is_expired 判断）。"""
        current = _now() if now is None else float(now)
        return (self.ts + self.ttl) - current

    @classmethod
    def create(cls, identity, name, host, port, ttl=3600, caps=None):
        """用本机身份创建邀请。

        :param identity: :class:`Identity`
        :param name: 节点名（非空字符串）
        :param host: 可达主机名/IP（可为空字符串，表示由调用方补充）
        :param port: HTTP 端口（1..65535）
        :param ttl: 有效期秒数（> 0）
        :param caps: 能力声明 dict（可选）
        :return: 已签名的 :class:`Invite`
        :raises ValueError: 参数非法
        """
        if not isinstance(identity, Identity):
            raise TypeError("identity 必须是 Identity 实例")
        if not isinstance(name, str) or not name:
            raise ValueError("name 必须是非空字符串")
        if not isinstance(host, str):
            raise ValueError("host 必须是字符串")
        try:
            port = int(port)
            ttl = int(ttl)
        except (TypeError, ValueError):
            raise ValueError("port/ttl 必须是整数")
        if port < 1 or port > 65535:
            raise ValueError("port 必须在 1..65535 之间")
        if ttl <= 0:
            raise ValueError("ttl 必须为正整数")
        if caps is not None and not isinstance(caps, dict):
            raise ValueError("caps 必须是 dict 或 None")
        invite = cls(
            v=1, name=name, node_id=identity.node_id,
            pubkey=identity.public_key_b64, host=host, port=port,
            ts=int(_now()), ttl=ttl, nonce=_new_nonce(8),
            caps=caps or {}, sig="",
        )
        invite.sig = identity.sign(invite._sign_basis())
        return invite

    @classmethod
    def parse(cls, uri):
        """解析并校验邀请 URI。

        校验顺序：前缀 -> base64url/JSON -> 字段类型 -> 版本 -> node_id 与公钥
        一致性 -> 签名 -> 过期。任何失败抛 ValueError（带明确原因）。

        :param uri: ``fleet://join/...``
        :return: 校验通过的 :class:`Invite`
        :raises ValueError: URI 非法/字段缺失/伪造/过期
        """
        if not isinstance(uri, str):
            raise ValueError("邀请必须是字符串")
        uri = uri.strip()
        if not uri.startswith(INVITE_PREFIX):
            raise ValueError("不是 fleet 邀请 URI（应以 %s 开头）" % INVITE_PREFIX)
        try:
            raw = _b64u_d(uri[len(INVITE_PREFIX):])
        except ValueError as exc:
            raise ValueError("邀请载荷 base64url 非法: %s" % exc)
        try:
            doc = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError("邀请载荷不是合法 JSON: %s" % exc)
        if not isinstance(doc, dict):
            raise ValueError("邀请载荷必须是 JSON 对象")
        for field in ("v", "name", "node_id", "pubkey", "host", "port", "ts",
                      "ttl", "nonce", "sig"):
            if field not in doc:
                raise ValueError("邀请缺少字段: %s" % field)
        try:
            invite = cls(
                v=doc["v"], name=doc["name"], node_id=doc["node_id"],
                pubkey=doc["pubkey"], host=doc["host"], port=doc["port"],
                ts=doc["ts"], ttl=doc["ttl"], nonce=doc["nonce"],
                caps=doc.get("caps"), sig=doc["sig"],
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("邀请字段类型非法: %s" % exc)
        if invite.v != 1:
            raise ValueError("不支持的邀请版本: %s" % invite.v)
        derived_node = node_id_from_public_key(invite.pubkey)
        if not derived_node:
            raise ValueError("邀请公钥非法")
        if not constant_time_eq(derived_node, invite.node_id):
            raise ValueError("邀请 node_id 与公钥不匹配（疑似伪造）")
        if invite.port < 1 or invite.port > 65535:
            raise ValueError("邀请端口非法: %d" % invite.port)
        if invite.ttl <= 0:
            raise ValueError("邀请 ttl 非法: %d" % invite.ttl)
        if not invite.nonce:
            raise ValueError("邀请 nonce 为空")
        if not verify_signature(invite.pubkey, invite._sign_basis(), invite.sig):
            raise ValueError("邀请签名校验失败（可能被篡改或伪造）")
        if invite.is_expired():
            raise ValueError("邀请已过期（ts=%d ttl=%d）" % (invite.ts, invite.ttl))
        return invite


def _pair_transcript(invite, eph_a, eph_b):
    """构造双方一致的配对转录。

    **重要**：配对双方必须使用**同一份** Invite（加入方从 join URI 解析、
    发起方保留自己创建的那一份）。转录只包含邀请内容（邀请方身份、nonce、
    签名）与两个临时公钥，两侧得到完全相同字节流，因此 SAS 与配对密钥一致。

    转录刻意不包含「调用方自己的 identity」：单向邀请（最常见的
    ``--join fleet://join/...`` 流程）里加入方没有自己的邀请，若把调用方身份
    纳入转录，两侧将算出不同 SAS。加入方身份由它用自己的 Ed25519 私钥对
    转录签名产生的 proof 绑定（调用方用 join 请求携带的公钥验签）。
    """
    eph_hex = sorted([bytes(eph_a).hex(), bytes(eph_b).hex()])
    parts = [invite.node_id, invite.pubkey, invite.nonce, invite.sig] + eph_hex
    return _DOMAIN_PAIR + "|".join(parts).encode("utf-8")


def pair_proof(identity, invite, my_eph_priv, my_eph_pub, peer_eph_pub):
    """生成配对证明、SAS 短认证串与配对密钥。

    流程：X25519(my_eph_priv, peer_eph_pub) -> shared；用双方一致的配对转录
    （同一份 Invite + 两个临时公钥）派生 key；SAS = sha256(transcript) 前 4
    字节 % 1_000_000（6 位数字）。

    **配对双方必须传入同一份 Invite**（发起方用自己创建的，加入方用从 join
    URI 解析出的）。满足该前提时，双方调用本函数（临时密钥对调）会得到
    **相同的 SAS 和 key**，这是防中间人攻击的人工校验点。

    :param identity: 本方 :class:`Identity`（用于对转录签名，绑定本方身份）
    :param invite: 双方共用的邀请（提供邀请方 node_id/pubkey/nonce/sig）
    :param my_eph_priv: 本方 X25519 临时私钥（32 字节）
    :param my_eph_pub: 本方 X25519 临时公钥（32 字节）
    :param peer_eph_pub: 对端 X25519 临时公钥（32 字节）
    :return: ``(proof_sig_b64, sas: str, key: bytes)``
    :raises ValueError: 参数长度非法、对端临时公钥为低阶点（共享秘密全零）
    :raises TypeError: identity/invite 类型非法
    """
    if not isinstance(identity, Identity):
        raise TypeError("identity 必须是 Identity 实例")
    if not isinstance(invite, Invite):
        raise TypeError("invite 必须是 Invite 实例")
    for name, value in (("my_eph_priv", my_eph_priv), ("my_eph_pub", my_eph_pub),
                        ("peer_eph_pub", peer_eph_pub)):
        if not isinstance(value, (bytes, bytearray)) or len(value) != 32:
            raise ValueError("%s 必须是 32 字节" % name)
    shared = x25519(my_eph_priv, peer_eph_pub)
    if shared == b"\x00" * 32:
        raise ValueError("对端临时公钥为低阶点（共享秘密全零），拒绝配对")
    transcript = _pair_transcript(invite, my_eph_pub, peer_eph_pub)
    digest = hashlib.sha256(transcript).digest()
    sas = "%06d" % (int.from_bytes(digest[:4], "big") % 1000000)
    key = derive_pair_key(shared, transcript)
    proof = identity.sign(_DOMAIN_PROOF + transcript)
    return (proof, sas, key)


# ===========================================================================
# 7. 执行策略（ExecPolicy）与脱敏（redact）
# ===========================================================================

#: 默认拒绝规则：(rule_id, 正则, 人类可读原因)
#: 这些操作一旦执行基本不可恢复，必须硬拒绝（不是审批）。
DEFAULT_DENY_RULES = [
    ("rm_root",
     r"\brm\s+(?:-[^\s]+\s+){1,6}(?:--no-preserve-root\s+)?/(?:\s|$|\*)",
     "递归删除根目录（rm -rf /）"),
    ("no_preserve_root", r"--no-preserve-root", "使用 --no-preserve-root 删除根目录"),
    ("mkfs", r"\bmkfs(?:\.[A-Za-z0-9]+)?\b", "格式化文件系统（mkfs）"),
    ("dd_device", r"\bdd\b[^\n;|&]*\bof\s*=\s*/dev/", "dd 直接写块设备"),
    ("write_block_device",
     r">\s*/dev/(?:sd|nvme|hd|vd|disk|mmcblk)", "重定向写入块设备"),
    ("fork_bomb",
     r":\s*\(\s*\)\s*\{[^}]*:\s*\|\s*:", "fork bomb（拒绝服务攻击）"),
    ("fork_bomb_words", r"\bfork\s*bomb\b", "fork bomb（拒绝服务攻击）"),
    ("chmod_root", r"\bchmod\s+(?:-[A-Za-z]+\s+)*777\s+/(?:\s|$)", "chmod 777 /"),
    ("wipefs", r"\bwipefs\b", "擦除文件系统签名（wipefs）"),
    ("shred_device", r"\bshred\b[^\n;|&]*/dev/", "shred 块设备"),
]

#: 默认审批规则：(rule_id, 正则, 人类可读原因)
#: 这些操作可能合法但影响面大，默认交给人审批。
DEFAULT_ASK_RULES = [
    ("power_state", r"\b(?:shutdown|reboot|poweroff|halt)\b", "关机/重启"),
    ("init_level", r"\binit\s+[06]\b", "切换运行级别"),
    ("systemctl_power",
     r"\bsystemctl\s+(?:reboot|poweroff|halt|suspend|rescue|emergency)\b",
     "systemd 电源/救援操作"),
    ("rm_recursive", r"\brm\s+-[A-Za-z]*[rR][A-Za-z]*", "递归删除"),
    ("pipe_to_shell",
     r"\b(?:curl|wget)\b[^\n;|&]*\|\s*(?:ba|z|da|k)?sh\b", "网络脚本直接执行"),
    ("sudo", r"\bsudo\b", "提权执行"),
    ("package_manager",
     r"\b(?:apt|apt-get|yum|dnf|apk|pacman|zypper|brew)\s+"
     r"(?:install|remove|purge|upgrade|dist-upgrade|update)\b",
     "包管理器变更系统"),
    ("pip_install", r"\bpip[0-9]?\s+install\b", "安装 Python 包"),
    ("npm_install_global", r"\bnpm\s+(?:install|i)\s+-g\b", "全局安装 npm 包"),
    ("docker_mutate",
     r"\bdocker\s+(?:run|rm|rmi|stop|kill|system|prune|volume)\b",
     "Docker 容器/镜像变更"),
    ("git_destructive",
     r"\bgit\s+(?:push|reset\s+--hard|clean\s+-[A-Za-z]*f)\b",
     "Git 破坏性操作"),
    ("netcat_exec", r"\b(?:nc|ncat|netcat)\b[^\n]*-[A-Za-z]*e\b",
     "netcat 反弹 shell"),
    ("account_change",
     r"\b(?:useradd|userdel|usermod|passwd|chpasswd|visudo)\b", "账号/密码变更"),
    ("crontab", r"\bcrontab\b", "修改计划任务"),
    ("etc_write", r">\s*/etc/", "写入系统配置目录"),
    ("firewall",
     r"\b(?:iptables|ip6tables|ufw|firewall-cmd|nft)\b", "防火墙变更"),
    ("kill_init", r"\bkill\s+-9\s+1\b", "强杀 init 进程"),
    ("windows_destructive",
     r"\b(?:del\s+/[sfq]|rmdir\s+/s|format\s+[A-Za-z]:|reg\s+(?:add|delete))\b",
     "Windows 破坏性操作"),
]

#: 参与路径 jail 检查的参数名（小写）
_PATH_ARG_KEYS = frozenset([
    "path", "paths", "file", "files", "filename", "filepath", "dir",
    "directory", "cwd", "workdir", "working_dir", "target", "dest",
    "destination", "src", "source", "out", "output", "root",
])

#: 承载「文件正文 / 二进制分块」的参数名（小写）——是数据，不是命令。
#: 覆盖 write_file/upload 的 content、edit 的 oldText/newText、
#: shell_write/tunnel_write 的 data_b64 等。
_BULK_ARG_KEYS = frozenset([
    "content", "data", "data_b64", "b64", "base64", "text", "body",
    "payload", "blob", "chunk", "stdin",
    "oldtext", "newtext", "oldtext_b64", "newtext_b64",
    "old_string", "new_string", "old_str", "new_str",
    "patch", "diff",
])

#: 数据载荷参与命令匹配的体积上限（字节）。超过即视为大块数据跳过扫描。
_BULK_SCAN_LIMIT = 4096

#: 参数会被真正交给 shell/进程执行的工具。
_EXEC_TOOLS = frozenset([
    "exec", "shell", "shell_exec", "sh", "bash", "run", "run_command",
])

#: 会把 stdin 当**命令**读的解释器。
#:
#: 只有这几种情况下 stdin 才可能变成"被执行的命令"，必须全量扫描；其余
#: （`base64 -d > f`、`tar xz`、`cat > f`）stdin 只是数据，全量扫描等于让
#: 随机 base64 里的 `/nft/` 把上传拦下来——实测 512KB 块有 5% 的命中率。
_INTERPRETER_RE = re.compile(
    r"(?:^|[|;&(]\s*|\bsudo\s+)(?:ba|z|da|k)?sh\b"
    r"|\bpython[0-9.]*\b|\bperl\b|\bruby\b|\bnode\b|\bphp\b|\bosascript\b",
    re.IGNORECASE)


def _compile_rules(items, prefix):
    """把配置中的规则列表编译为 ``(rule_id, regex, reason)``。

    允许两种写法：
      * ``"^rm\\b"``                              -> id = ``<prefix>:<idx>``
      * ``{"id": "x", "pattern": "...", "reason": "..."}``
    """
    compiled = []
    if items is None:
        return compiled
    if not isinstance(items, (list, tuple)):
        raise ValueError("%s 必须是列表" % prefix)
    for index, item in enumerate(items):
        if isinstance(item, str):
            pattern = item
            rule_id = "%s:%d" % (prefix, index)
            reason = "命中规则 %s" % rule_id
        elif isinstance(item, dict):
            pattern = item.get("pattern")
            if not isinstance(pattern, str) or not pattern:
                raise ValueError("%s[%d] 缺少 pattern" % (prefix, index))
            rule_id = str(item.get("id") or ("%s:%d" % (prefix, index)))
            reason = str(item.get("reason") or ("命中规则 %s" % rule_id))
        else:
            raise ValueError("%s[%d] 必须是字符串或 dict" % (prefix, index))
        try:
            regex = re.compile(pattern, re.IGNORECASE | re.MULTILINE)
        except re.error as exc:
            raise ValueError("%s[%d] 正则非法: %s" % (prefix, index, exc))
        compiled.append((rule_id, regex, reason))
    return compiled


def _collect_strings(value, out, depth=0):
    """递归收集值里的所有字符串（用于数据载荷提取）。"""
    if depth > 6:
        return
    if isinstance(value, str):
        if value:
            out.append(value)
    elif isinstance(value, dict):
        for key in value:
            _collect_strings(value[key], out, depth + 1)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            _collect_strings(item, out, depth + 1)


def _split_args(value, cmds, payloads, depth=0):
    """把 args 拆成「命令文本」与「数据载荷」两类。

    - ``cmds``：会被当作命令解析的文本（含非典型字段名，防止藏命令绕过策略）
    - ``payloads``：文件正文 / 二进制分块，属于数据而非命令

    必须分流：``upload`` 的 ``content`` 是 base64 文件内容，若参与危险命令正则，
    载荷里随机出现的 ``ufw``（仅 3 个字符）几乎必然命中，导致**每个大文件上传
    都被误判为「防火墙变更」**。
    """
    if depth > 6:
        return
    if isinstance(value, str):
        cmds.append(value)
    elif isinstance(value, bool):
        cmds.append("true" if value else "false")
    elif isinstance(value, (int, float)):
        cmds.append(str(value))
    elif isinstance(value, dict):
        for key in value:
            child = value[key]
            if str(key).lower() in _BULK_ARG_KEYS:
                _collect_strings(child, payloads, depth + 1)
            else:
                cmds.append(str(key))
                _split_args(child, cmds, payloads, depth + 1)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            _split_args(item, cmds, payloads, depth + 1)


def _payload_texts(tool, payloads, cmd_text):
    """筛出仍需参与命令匹配的数据载荷。

    小体量文本载荷照样扫描——``write_file`` 写一段含 ``rm -rf /`` 的脚本值得
    拦一下；但超过 :data:`_BULK_SCAN_LIMIT` 的大块数据（大文件正文、base64
    分块）一律跳过：它们不是命令行，全量匹配既慢（O(载荷 × 规则数)，1MB stdin
    实测 150ms）又必然误判。

    唯一例外是「stdin 会被当命令读」的场景：``exec`` 的 command 是
    ``bash`` / ``python3`` 这类解释器时，stdin 就是命令本身，必须全量扫描。
    判断依据是**命令文本**而不是工具名——``exec {"command": "base64 -d > f"}``
    的 stdin 是数据，不该受连累。
    """
    if (str(tool).lower() in _EXEC_TOOLS
            and _INTERPRETER_RE.search(cmd_text or "")):
        return list(payloads)
    return [p for p in payloads if len(p) <= _BULK_SCAN_LIMIT]


def _command_text(tool, args):
    """把 (tool, args) 归一化为可匹配的命令文本。

    优先使用常见的命令字段（cmd/command/script/argv），同时保留其他参数文本，
    避免攻击者把危险命令藏在非典型字段里绕过策略；但**文件正文类字段**
    （见 :data:`_BULK_ARG_KEYS`）按数据对待，只在小体量时参与匹配。
    """
    parts = [str(tool)]
    cmd_fields = []
    for key in ("cmd", "command", "script", "argv", "shell", "exec"):
        value = args.get(key)
        if isinstance(value, str):
            parts.append(value)
            cmd_fields.append(value)
    cmds = []
    payloads = []
    _split_args(args, cmds, payloads)
    parts.extend(cmds)
    # 判断「stdin 会不会被当命令读」只看命令字段本身，不看键名/路径
    parts.extend(_payload_texts(tool, payloads, " ; ".join(cmd_fields)))
    return " ".join(parts)


class ExecPolicy:
    """执行策略：对 ``call_tool(tool, args)`` 给出 allow / ask / deny 决策。

    配置字段（均可选）：

    - ``deny_patterns``      危险命令正则列表（命中即 deny）
    - ``allow_patterns``     允许清单；非空时未命中者一律 ask（白名单模式）
    - ``require_approval``   需人工审批的正则列表（命中即 ask）
    - ``path_jail``          允许访问的路径根列表（越界即 deny）
    - ``max_timeout``        单次执行允许的最大超时秒数（超过即 ask）

    决策顺序（安全优先）：deny > path_jail > timeout > require_approval >
    allow_patterns > default_allow。即「拒绝规则永远最高优先级」，无法被
    允许清单或审批规则放行。
    """

    def __init__(self, cfg=None):
        """从配置 dict 构造策略；未提供的字段使用安全默认值。

        :param cfg: 配置 dict，见类文档
        :raises ValueError: 配置类型/正则非法
        """
        if cfg is None:
            cfg = {}
        if not isinstance(cfg, dict):
            raise ValueError("ExecPolicy 配置必须是 dict")
        self.deny_patterns = cfg.get("deny_patterns")
        self.allow_patterns = cfg.get("allow_patterns")
        self.require_approval = cfg.get("require_approval")
        self.path_jail = cfg.get("path_jail") or []
        if not isinstance(self.path_jail, (list, tuple)):
            raise ValueError("path_jail 必须是路径列表")
        try:
            self.max_timeout = int(cfg.get("max_timeout", 60))
        except (TypeError, ValueError):
            raise ValueError("max_timeout 必须是整数")
        if self.max_timeout <= 0:
            raise ValueError("max_timeout 必须为正整数")
        self._deny = _compile_rules(self.deny_patterns, "deny")
        self._allow = _compile_rules(self.allow_patterns, "allow")
        self._ask = _compile_rules(self.require_approval, "ask")

    @classmethod
    def default(cls):
        """返回内置默认策略（危险命令 deny、破坏性操作 ask）。"""
        return cls({
            "deny_patterns": [{"id": rid, "pattern": pat, "reason": reason}
                              for rid, pat, reason in DEFAULT_DENY_RULES],
            "require_approval": [{"id": rid, "pattern": pat, "reason": reason}
                                 for rid, pat, reason in DEFAULT_ASK_RULES],
            "max_timeout": 60,
        })

    @classmethod
    def with_defaults(cls, cfg=None):
        """在**保留内置安全规则**的前提下叠加用户配置。

        这是节点运行时的推荐入口：用户配置只能追加规则/收紧限制，
        不能因为没写 ``deny_patterns`` 就把默认的危险命令拦截关掉。
        """
        cfg = dict(cfg or {})
        defaults = cls.default()
        deny = [{"id": r[0], "pattern": r[1], "reason": r[2]}
                for r in DEFAULT_DENY_RULES] + list(cfg.get("deny_patterns") or [])
        ask = [{"id": r[0], "pattern": r[1], "reason": r[2]}
               for r in DEFAULT_ASK_RULES] + list(cfg.get("require_approval") or [])
        cfg["deny_patterns"] = deny
        cfg["require_approval"] = ask
        cfg.setdefault("max_timeout", defaults.max_timeout)
        return cls(cfg)

    @property
    def is_allowlist_mode(self):
        """是否处于白名单模式（配置了非空 allow_patterns）。"""
        return bool(self._allow)

    def _check_paths(self, args):
        """路径 jail 检查；返回 (decision, reason, rule_id) 或 None。"""
        if not self.path_jail:
            return None
        roots = []
        for root in self.path_jail:
            try:
                roots.append(os.path.realpath(os.path.expanduser(str(root))))
            except OSError:
                raise ValueError("path_jail 根路径非法: %r" % (root,))
        if not roots:
            return None
        path_values = []

        def walk(value, depth=0):
            if depth > 6:
                return
            if isinstance(value, dict):
                for key in value:
                    child = value[key]
                    if str(key).lower() in _PATH_ARG_KEYS:
                        collect_strings(child, depth + 1)
                    walk(child, depth + 1)
            elif isinstance(value, (list, tuple)):
                for item in value:
                    walk(item, depth + 1)

        def collect_strings(value, depth=0):
            if depth > 6:
                return
            if isinstance(value, str):
                if value:
                    path_values.append(value)
            elif isinstance(value, (list, tuple)):
                for item in value:
                    collect_strings(item, depth + 1)

        if isinstance(args, dict):
            walk(args)
        for raw_path in path_values:
            if "://" in raw_path:                  # URL 不是文件路径，跳过 jail
                continue
            expanded = os.path.expanduser(raw_path)
            if os.path.isabs(expanded):
                resolved = os.path.realpath(expanded)
            else:
                resolved = os.path.realpath(os.path.join(roots[0], expanded))
            inside = False
            for root in roots:
                if resolved == root or resolved.startswith(root + os.sep):
                    inside = True
                    break
            if not inside:
                return ("deny", "路径越界（不在 path_jail 内）: %s" % raw_path,
                        "path_jail")
        return None

    def check(self, tool, args):
        """对一次工具调用做策略判定。

        :param tool: 工具名（如 ``exec`` / ``shell`` / ``read_file``）
        :param args: 参数字典
        :return: ``(decision, reason, rule_id)``
                 decision ∈ ``"allow" | "ask" | "deny"``
        :raises ValueError: tool 为空
        :raises TypeError: args 不是 dict/None
        """
        if not isinstance(tool, str) or not tool:
            raise ValueError("tool 必须是非空字符串")
        if args is None:
            args = {}
        if not isinstance(args, dict):
            raise TypeError("args 必须是 dict 或 None")
        text = _command_text(tool, args)
        for rule_id, regex, reason in self._deny:
            if regex.search(text):
                return ("deny", reason, rule_id)
        path_decision = self._check_paths(args)
        if path_decision is not None:
            return path_decision
        timeout = args.get("timeout")
        if timeout is not None:
            try:
                timeout_value = float(timeout)
            except (TypeError, ValueError):
                return ("deny", "timeout 不是数字: %r" % (timeout,), "bad_timeout")
            if timeout_value <= 0:
                return ("deny", "timeout 必须为正数", "bad_timeout")
            if timeout_value > self.max_timeout:
                return ("ask",
                        "请求超时 %.1fs 超过上限 %ds" % (timeout_value, self.max_timeout),
                        "timeout_exceeds")
        for rule_id, regex, reason in self._ask:
            if regex.search(text):
                return ("ask", reason, rule_id)
        for index, (_rule_id, regex, _reason) in enumerate(self._allow):
            if regex.search(text):
                return ("allow", "命中允许清单", "allow_pattern:%d" % index)
        if self._allow:
            return ("ask", "不在允许清单内，需要人工确认", "not_in_allowlist")
        return ("allow", "未命中拒绝/审批规则，按默认放行", "default_allow")


#: 脱敏正则（顺序敏感：先处理长令牌，再处理键值对）
_REDACT_RULES = [
    # PEM 私钥块（可跨行）
    (re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?"
                r"-----END [A-Z0-9 ]*PRIVATE KEY-----", re.DOTALL),
     "[REDACTED PRIVATE KEY]"),
    # 邀请 URI
    (re.compile(r"fleet://join/[A-Za-z0-9_\-=]+"), "fleet://join/[REDACTED]"),
    # 本模块密封令牌
    (re.compile(r"enc2\$[A-Za-z0-9+/=]+"), "enc2$[REDACTED]"),
    # 旧版邮箱加密令牌
    (re.compile(r"enc1\$[A-Za-z0-9+/=]+"), "enc1$[REDACTED]"),
    # Authorization: Bearer xxx
    (re.compile(r"(?i)\b(authorization\s*:\s*bearer\s+)[A-Za-z0-9._~+/=\-]+"),
     r"\1***"),
    # Bearer xxx（无 header 名）
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=\-]{12,}"), "Bearer ***"),
    # key=value / key: value 形态
    (re.compile(r"(?i)\b(token|password|passwd|pwd|secret|api[_-]?key|"
                r"access[_-]?key|private[_-]?key(?:_b64)?|auth[_-]?token|"
                r"client[_-]?secret)\b(\s*[:=]\s*)"
                r"(\"[^\"]*\"|'[^']*'|[^\s,;&}]+)"),
     r"\1\2***"),
    # 常见云厂商/平台令牌
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b"), "sk-[REDACTED]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AKIA[REDACTED]"),
    (re.compile(r"\b(?:ghp|gho|ghs|ghu)_[A-Za-z0-9]{20,}\b"), "[REDACTED]"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b"), "[REDACTED]"),
]


def redact(text):
    """对日志/审计文本脱敏：token、密码、私钥、邀请码、密封令牌。

    :param text: 待脱敏字符串
    :return: 脱敏后的字符串（原文本不被修改）
    :raises TypeError: text 不是 str
    """
    if not isinstance(text, str):
        raise TypeError("redact 只接受 str，收到 %s" % type(text).__name__)
    result = text
    for regex, replacement in _REDACT_RULES:
        result = regex.sub(replacement, result)
    return result


# ===========================================================================
# 8. 审计哈希链
#
# 每行：{"idx":n, "ts":毫秒, "prev":前一条hash, "event":{...}, "hash":本条hash}
# hash = sha256("idx|ts|prev|" + canonical_json(event))
# 任何一行被改写/删除/重排都会导致后续 prev 不符，verify() 返回首个坏行号。
# ===========================================================================

def _audit_record_hash(index, ts, prev, event):
    """计算一条审计记录的哈希（hex，64 字符）。"""
    digest = hashlib.sha256()
    digest.update(("%d|%d|%s|" % (int(index), int(ts), prev)).encode("utf-8"))
    digest.update(_canon_json(event))
    return digest.hexdigest()


class AuditChain:
    """防篡改审计日志（哈希链 JSONL）。

    用途：记录工具调用、配对、密钥轮换等安全事件。append 带 fsync 与
    进程内/跨进程锁；verify 能定位第一条被篡改（或链断裂）的行号，
    便于取证与告警。
    """

    GENESIS = "0" * 64

    def __init__(self, path):
        """绑定审计文件路径（不立即创建文件）。

        :param path: JSONL 文件路径
        :raises ValueError: path 为空或不是字符串
        """
        if not isinstance(path, str) or not path:
            raise ValueError("审计文件路径必须是非空字符串")
        self.path = os.path.abspath(path)
        self._lock = threading.Lock()
        _ensure_dir(os.path.dirname(self.path))

    def _read_tail_state(self):
        """读取链尾状态 (prev_hash, next_index)；空文件返回创世值。"""
        if not os.path.exists(self.path):
            return (self.GENESIS, 0)
        line = _read_last_line(self.path)
        if not line:
            return (self.GENESIS, 0)
        try:
            record = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError("审计链末尾记录损坏，拒绝追加: %s" % exc)
        if not isinstance(record, dict) or "hash" not in record or "idx" not in record:
            raise ValueError("审计链末尾记录缺字段，拒绝追加")
        try:
            return (str(record["hash"]), int(record["idx"]) + 1)
        except (TypeError, ValueError) as exc:
            raise ValueError("审计链末尾记录 idx 非法: %s" % exc)

    def append(self, event):
        """追加一条审计事件，返回本条记录哈希。

        :param event: 事件 dict（任意 JSON 可序列化内容）
        :return: 本条记录 hash（hex 字符串）
        :raises TypeError: event 不是 dict
        :raises ValueError: 链尾损坏（需人工修复，不会破坏链继续写）
        """
        if not isinstance(event, dict):
            raise TypeError("审计事件必须是 dict")
        with self._lock:
            _ensure_dir(os.path.dirname(self.path))
            fd = os.open(self.path,
                         os.O_WRONLY | os.O_CREAT | os.O_APPEND, FILE_MODE)
            try:
                if fcntl is not None:
                    fcntl.flock(fd, fcntl.LOCK_EX)
                prev, index = self._read_tail_state()
                ts = now_ms()
                record = {
                    "idx": index,
                    "ts": ts,
                    "prev": prev,
                    "event": event,
                    "hash": _audit_record_hash(index, ts, prev, event),
                }
                line = _canon_json(record) + b"\n"
                written = 0
                while written < len(line):
                    written += os.write(fd, line[written:])
                os.fsync(fd)
            finally:
                if fcntl is not None:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                    except OSError:
                        pass
                os.close(fd)
            return record["hash"]

    def verify(self):
        """校验整条哈希链。

        :return: ``(ok: bool, bad_index: int)``；ok 时 bad_index 为 -1，
                否则为第一条异常记录的 0 基行号（含 JSON 损坏/空行）
        """
        if not os.path.exists(self.path):
            return (True, -1)
        prev = self.GENESIS
        expected = 0
        with open(self.path, "rb") as handle:
            for lineno, raw in enumerate(handle):
                line = raw.strip()
                if not line:
                    return (False, lineno)
                try:
                    record = json.loads(line.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    return (False, lineno)
                if not isinstance(record, dict):
                    return (False, lineno)
                if record.get("idx") != expected:
                    return (False, lineno)
                if not constant_time_eq(str(record.get("prev")), prev):
                    return (False, lineno)
                try:
                    computed = _audit_record_hash(record.get("idx"),
                                                  record.get("ts"),
                                                  record.get("prev"),
                                                  record.get("event"))
                except (TypeError, ValueError):
                    return (False, lineno)
                if not constant_time_eq(computed, str(record.get("hash"))):
                    return (False, lineno)
                prev = str(record["hash"])
                expected += 1
        return (True, -1)

    def tail(self, n=20):
        """返回最近 ``n`` 条记录（文件不存在返回空列表）。"""
        try:
            n = int(n)
        except (TypeError, ValueError):
            raise ValueError("n 必须是整数")
        if n <= 0:
            return []
        records = read_jsonl(self.path)
        return records[-n:]


# ===========================================================================
# 9. 指标（Metrics）
# ===========================================================================

def _fmt_labels(labels):
    """把 label dict 渲染为 Prometheus label 串（含转义、按键排序）。"""
    if not labels:
        return ""
    parts = []
    for key in sorted(labels):
        value = str(labels[key])
        value = (value.replace("\\", "\\\\")
                      .replace("\"", "\\\"")
                      .replace("\n", "\\n"))
        parts.append("%s=\"%s\"" % (key, value))
    return "{%s}" % ",".join(parts)


def _metric_name(name):
    """指标名转义：非 ``[A-Za-z0-9_:]`` 字符替换为 ``_``。"""
    return re.sub(r"[^A-Za-z0-9_:]", "_", str(name))


def _fmt_number(value):
    """数值渲染：整数按整数输出，浮点用 repr（保留精度）。"""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError("指标值必须是数字，收到 %r" % (value,))
    return repr(number)


class Metrics:
    """线程安全的进程内指标注册表（counter / gauge / histogram）。

    用法::

        m = Metrics()
        m.inc("requests", method="GET")
        m.set_gauge("queue_depth", 3)
        m.observe("latency_s", 0.42, node="nas")
        m.snapshot()            # JSON 友好 dict
        m.render_prometheus()   # Prometheus 文本格式
    """

    #: 默认直方图桶（秒级，覆盖局域网 RPC 到长任务）
    DEFAULT_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5,
                       1.0, 2.5, 5.0, 10.0)

    def __init__(self, prefix=""):
        """初始化指标表。

        :param prefix: 指标名前缀（如 ``"fleet_"``），默认无前缀
        """
        self._prefix = str(prefix or "")
        self._lock = threading.Lock()
        self._counters = {}
        self._gauges = {}
        self._hists = {}

    def _key(self, name, labels):
        """返回 (完整指标名, 排序后的 label 元组)。"""
        if not isinstance(name, str) or not name:
            raise ValueError("指标名必须是非空字符串")
        pairs = tuple(sorted((str(k), str(v)) for k, v in labels.items()))
        return (self._prefix + name, pairs)

    def inc(self, name, value=1, **labels):
        """计数器累加（可为负，但语义上建议非负）。

        :raises ValueError: name 非法 / value 非整数
        """
        try:
            delta = int(value)
        except (TypeError, ValueError):
            raise ValueError("counter 增量必须是整数")
        key = self._key(name, labels)
        with self._lock:
            self._counters[key] = self._counters.get(key, 0) + delta

    def set_gauge(self, name, value, **labels):
        """设置瞬时值（gauge），同一 key 覆盖写。

        :raises ValueError: value 不是数字
        """
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("gauge 值必须是数字")
        key = self._key(name, labels)
        with self._lock:
            self._gauges[key] = value

    def observe(self, name, value, **labels):
        """记录一次观测值（histogram：只保留 count/sum/min/max/桶计数）。"""
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ValueError("histogram 观测值必须是数字")
        key = self._key(name, labels)
        with self._lock:
            hist = self._hists.get(key)
            if hist is None:
                hist = {
                    "count": 0,
                    "sum": 0.0,
                    "min": None,
                    "max": None,
                    "buckets": [0] * len(self.DEFAULT_BUCKETS),
                }
                self._hists[key] = hist
            hist["count"] += 1
            hist["sum"] += number
            if hist["min"] is None or number < hist["min"]:
                hist["min"] = number
            if hist["max"] is None or number > hist["max"]:
                hist["max"] = number
            # 桶计数按 Prometheus 语义存储为「累计值」：观测值落入第一个
            # 满足 number <= bound 的桶后，该桶及其后续所有桶都 +1。
            for index, bound in enumerate(self.DEFAULT_BUCKETS):
                if number <= bound:
                    for later in range(index, len(self.DEFAULT_BUCKETS)):
                        hist["buckets"][later] += 1
                    break

    def snapshot(self):
        """返回可 JSON 序列化的指标快照。

        :return: ``{"counters": {...}, "gauges": {...}, "histograms": {...}}``
                 key 为带 label 的渲染名，顺序确定（按 key 排序）
        """
        with self._lock:
            counters = dict(self._counters)
            gauges = dict(self._gauges)
            hists = {}
            for key, hist in self._hists.items():
                hists[key] = {
                    "count": hist["count"],
                    "sum": hist["sum"],
                    "min": hist["min"],
                    "max": hist["max"],
                    "buckets": dict(
                        (("%g" % bound), hist["buckets"][i])
                        for i, bound in enumerate(self.DEFAULT_BUCKETS)
                    ),
                }
        def render(key):
            name, labels = key
            return name + _fmt_labels(dict(labels))

        return {
            "counters": dict((render(k), v)
                             for k, v in sorted(counters.items())),
            "gauges": dict((render(k), v)
                           for k, v in sorted(gauges.items())),
            "histograms": dict((render(k), v)
                               for k, v in sorted(hists.items())),
        }

    def render_prometheus(self):
        """渲染为 Prometheus 文本格式（无 HELP，含 TYPE）。

        直方图输出 ``_bucket{le="..."}``、``_sum``、``_count``；桶计数为累计值。
        """
        with self._lock:
            counters = dict(self._counters)
            gauges = dict(self._gauges)
            hists = {}
            for key, hist in self._hists.items():
                hists[key] = {
                    "count": hist["count"],
                    "sum": hist["sum"],
                    "buckets": list(hist["buckets"]),
                }
        lines = []
        counter_names = sorted(set(k[0] for k in counters))
        for name in counter_names:
            lines.append("# TYPE %s counter" % _metric_name(name))
            for key in sorted(counters):
                if key[0] != name:
                    continue
                lines.append("%s%s %d" % (_metric_name(name),
                                          _fmt_labels(dict(key[1])),
                                          counters[key]))
        gauge_names = sorted(set(k[0] for k in gauges))
        for name in gauge_names:
            lines.append("# TYPE %s gauge" % _metric_name(name))
            for key in sorted(gauges):
                if key[0] != name:
                    continue
                lines.append("%s%s %s" % (_metric_name(name),
                                          _fmt_labels(dict(key[1])),
                                          _fmt_number(gauges[key])))
        hist_names = sorted(set(k[0] for k in hists))
        for name in hist_names:
            lines.append("# TYPE %s histogram" % _metric_name(name))
            for key in sorted(hists):
                if key[0] != name:
                    continue
                labels = dict(key[1])
                hist = hists[key]
                for index, bound in enumerate(self.DEFAULT_BUCKETS):
                    bucket_labels = dict(labels)
                    bucket_labels["le"] = "%g" % bound
                    lines.append("%s_bucket%s %d"
                                 % (_metric_name(name),
                                    _fmt_labels(bucket_labels),
                                    hist["buckets"][index]))
                total_labels = dict(labels)
                total_labels["le"] = "+Inf"
                lines.append("%s_bucket%s %d"
                             % (_metric_name(name),
                                _fmt_labels(total_labels), hist["count"]))
                lines.append("%s_sum%s %s"
                             % (_metric_name(name), _fmt_labels(labels),
                                _fmt_number(hist["sum"])))
                lines.append("%s_count%s %d"
                             % (_metric_name(name), _fmt_labels(labels),
                                hist["count"]))
        return "\n".join(lines) + ("\n" if lines else "")


# ===========================================================================
# 10. TLS 自签证书（可选依赖 openssl；不可用时降级返回 None）
# ===========================================================================

def ensure_self_signed_cert(out_dir, common_name=None):
    """确保 ``out_dir`` 下存在自签 TLS 证书，返回 ``(cert, key)`` 路径。

    已有 ``cert.pem``/``key.pem`` 时直接复用，不重新生成（避免每次启动
    指纹变化导致客户端校验失败）。依赖系统 ``openssl``；缺失或执行失败
    返回 ``None``（集成方应降级为纯 HTTP 或提示用户手动配置）。

    :param out_dir: 证书输出目录（不存在则创建，0700）
    :param common_name: 证书 CN，默认取主机名
    :return: ``(cert_path, key_path)`` 或 ``None``
    """
    openssl = shutil.which("openssl")
    if not openssl:
        return None
    try:
        _ensure_dir(os.path.abspath(out_dir))
    except OSError:
        return None
    out_dir = os.path.abspath(out_dir)
    cert_path = os.path.join(out_dir, "cert.pem")
    key_path = os.path.join(out_dir, "key.pem")
    if (os.path.exists(cert_path) and os.path.getsize(cert_path) > 0
            and os.path.exists(key_path) and os.path.getsize(key_path) > 0):
        _best_effort_chmod(key_path, FILE_MODE)
        return (cert_path, key_path)
    cn = common_name or socket.gethostname() or "localhost"
    if not re.match(r"^[A-Za-z0-9._\-]{1,64}$", cn):
        cn = "localhost"
    cmd = [
        openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
        "-keyout", key_path, "-out", cert_path, "-days", "3650",
        "-subj", "/CN=%s" % cn,
    ]
    try:
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=30, check=True)
    except (OSError, subprocess.SubprocessError):
        for leftover in (cert_path, key_path):
            try:
                if os.path.exists(leftover):
                    os.unlink(leftover)
            except OSError:
                pass
        return None
    if not (os.path.exists(cert_path) and os.path.exists(key_path)):
        return None
    _best_effort_chmod(key_path, FILE_MODE)
    _best_effort_chmod(cert_path, 0o644)
    return (cert_path, key_path)


def cert_fingerprint(cert_path):
    """计算证书 SHA-256 指纹，格式 ``"sha256:AB:CD:..."``。

    :param cert_path: PEM 证书路径
    :return: 指纹字符串；openssl 不可用或解析失败返回 ``""``
    :raises ValueError: 证书文件不存在
    """
    if not isinstance(cert_path, str) or not cert_path:
        raise ValueError("cert_path 必须是非空字符串")
    if not os.path.exists(cert_path):
        raise ValueError("证书文件不存在: %s" % cert_path)
    openssl = shutil.which("openssl")
    if not openssl:
        return ""
    try:
        output = subprocess.check_output(
            [openssl, "x509", "-in", cert_path, "-noout", "-fingerprint", "-sha256"],
            stderr=subprocess.DEVNULL, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return ""
    text = output.decode("utf-8", "replace")
    value = ""
    for line in text.splitlines():
        if "=" in line and "fingerprint" in line.lower():
            value = line.split("=", 1)[1].strip()
            break
    if not value:
        return ""
    return "sha256:" + value.upper()


# ===========================================================================
# 11. 持久化 / 杂项（原子写、JSONL、目录与权限）
# ===========================================================================

_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS = {}


def _path_lock(path):
    """取得某个路径对应的进程内互斥锁（懒创建，按路径复用）。"""
    with _LOCKS_GUARD:
        lock = _PATH_LOCKS.get(path)
        if lock is None:
            lock = threading.Lock()
            _PATH_LOCKS[path] = lock
        return lock


def _best_effort_chmod(path, mode):
    """尽力设置文件权限；不支持 POSIX 权限的文件系统上静默忽略。

    这是唯一「静默」的地方：某些平台（Windows/FAT/部分网络盘）没有 POSIX
    权限位，调用方无法补救，因此不抛异常。安全性由调用方在上层文档中说明。
    """
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def _ensure_dir(dir_path):
    """递归创建目录（0700），已存在的目录不改动其权限。"""
    if not dir_path:
        return
    dir_path = os.path.abspath(dir_path)
    if os.path.isdir(dir_path):
        return
    parent = os.path.dirname(dir_path)
    if parent and parent != dir_path:
        _ensure_dir(parent)
    try:
        os.mkdir(dir_path, DIR_MODE)
    except OSError as exc:
        if exc.errno != errno.EEXIST:
            raise
        return
    _best_effort_chmod(dir_path, DIR_MODE)


def _fsync_dir(dir_path):
    """对目录 fsync，确保 rename 落盘（Windows/不支持时忽略）。"""
    if not dir_path:
        return
    try:
        fd = os.open(dir_path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write(path, data, mode=FILE_MODE):
    """原子写文件：同目录临时文件 -> fsync -> ``os.replace`` -> fsync 目录。

    :param path: 目标路径（父目录不存在时自动创建，0700）
    :param data: str（UTF-8 编码）或 bytes
    :param mode: 文件权限，默认 0600
    :raises TypeError: data 类型非法
    :raises OSError: 写入失败（临时文件会被清理）
    """
    if not isinstance(path, str) or not path:
        raise ValueError("path 必须是非空字符串")
    if isinstance(data, str):
        raw = data.encode("utf-8")
    elif isinstance(data, (bytes, bytearray)):
        raw = bytes(data)
    else:
        raise TypeError("data 必须是 str 或 bytes，收到 %s" % type(data).__name__)
    target = os.path.abspath(path)
    parent = os.path.dirname(target) or os.sep
    _ensure_dir(parent)
    tmp_path = os.path.join(
        parent, ".%s.tmp.%s" % (os.path.basename(target), os.urandom(6).hex()))
    fd = None
    try:
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        written = 0
        while written < len(raw):
            written += os.write(fd, raw[written:])
        os.fsync(fd)
        os.close(fd)
        fd = None
        _best_effort_chmod(tmp_path, mode)
        os.replace(tmp_path, target)
        _fsync_dir(parent)
    except BaseException:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        except OSError:
            pass
        raise


def append_jsonl(path, obj):
    """追加一行 JSON 到文件并 fsync（进程内 + 跨进程锁，防止交错/丢行）。

    :param path: JSONL 文件路径
    :param obj: 任意 JSON 可序列化对象（审计/邮箱等场景通常是 dict）
    :raises TypeError: 无法 JSON 序列化
    :raises OSError: 写入失败
    """
    if not isinstance(path, str) or not path:
        raise ValueError("path 必须是非空字符串")
    target = os.path.abspath(path)
    try:
        line = _canon_json(obj) + b"\n"
    except (TypeError, ValueError) as exc:
        raise TypeError("obj 无法 JSON 序列化: %s" % exc)
    _ensure_dir(os.path.dirname(target))
    with _path_lock(target):
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, FILE_MODE)
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_EX)
            written = 0
            while written < len(line):
                written += os.write(fd, line[written:])
            os.fsync(fd)
        finally:
            if fcntl is not None:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    pass
            os.close(fd)


def read_jsonl(path, limit=0):
    """读取 JSONL 文件。

    :param path: 文件路径（不存在返回空列表）
    :param limit: 0 表示读取全部；> 0 表示只返回**最后** limit 条（适合审计尾部）
    :return: 解析后的对象列表
    :raises ValueError: 某行不是合法 JSON（含行号），避免静默容忍损坏日志
    """
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        raise ValueError("limit 必须是整数")
    if limit < 0:
        raise ValueError("limit 不能为负数")
    if not os.path.exists(path):
        return []
    records = []
    with open(path, "rb") as handle:
        for lineno, raw in enumerate(handle):
            line = raw.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line.decode("utf-8")))
            except (ValueError, UnicodeDecodeError) as exc:
                raise ValueError("第 %d 行不是合法 JSON: %s" % (lineno + 1, exc))
    if limit and len(records) > limit:
        return records[-limit:]
    return records


def _read_last_line(path):
    """读取文件的最后一行（不含行尾换行；空文件返回 b""）。

    从文件尾部按 4KB 反向扫描，避免把大审计日志整个读进内存。
    """
    try:
        handle = open(path, "rb")
    except OSError:
        return b""
    with handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        if size == 0:
            return b""
        position = size
        buffer = b""
        while position > 0:
            step = min(4096, position)
            position -= step
            handle.seek(position)
            buffer = handle.read(step) + buffer
            stripped = buffer.rstrip(b"\n")
            newline = stripped.rfind(b"\n")
            if newline != -1:
                return stripped[newline + 1:]
            if position == 0:
                return stripped
    return b""
