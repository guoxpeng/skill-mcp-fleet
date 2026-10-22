# -*- coding: utf-8 -*-
"""v5.2 安全核心单元测试（``scripts/fleet_core.py``）。

覆盖：
  * RFC 8032 Ed25519 官方向量（含 1023 字节长消息）、签名篡改/可延展签名
  * RFC 7748 X25519 官方向量、DH 一致性、低阶点
  * RFC 5869 HKDF 向量、seal/open_sealed 篡改与 AAD 不匹配
  * 请求签名篡改、时间窗、nonce 重放、容量上限
  * 邀请伪造/过期/node_id 不一致
  * 配对 SAS 双方一致、proof 可验、低阶临时公钥拒绝
  * 执行策略命中矩阵（deny/ask/allow）、路径 jail、超时、允许清单
  * 审计哈希链篡改定位、并发 append 不丢
  * 原子写 0600、父目录 0700、无临时文件残留
  * 指标计数/直方图/Prometheus 渲染/线程安全
  * TLS 自签（openssl 不可用时 skip）
  * Python 3.6 语法兼容（ast.parse feature_version）

运行：``.venv/bin/python -m pytest tests/test_core_51.py -q``
"""

import ast
import base64
import json
import os
import re
import stat
import threading
import time

import pytest

import fleet_core as core


# ---------------------------------------------------------------------------
# 常量与工具
# ---------------------------------------------------------------------------

# RFC 8032 §7.1 测试向量：(seed_hex, public_hex, message_hex, signature_hex)
ED25519_VECTORS = [
    (
        "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
        "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a",
        "",
        "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155"
        "5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b",
    ),
    (
        "4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
        "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
        "72",
        "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da"
        "085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00",
    ),
    (
        "c5aa8df43f9f837bedb7442f31dcb7b166d38535076f094b85ce3a2e0b4458f7",
        "fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025",
        "af82",
        "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac"
        "18ff9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a",
    ),
    (
        # §7.1 TEST 1023 字节长消息（字符串 "abc" 重复 341 次）
        "833fe62409237b9d62ec77587520911e9a759cec1d19755b7da901b96dca3d42",
        "ec172b93ad5e563bf4932c70e1245034c35467ef2efd4d64ebf819683467e2bf",
        "ddaf35a193617abacc417349ae20413112e6fa4e89a97ea20a9eeee64b55d39a"
        "2192992a274fc1a836ba3c23a3feebbd454d4423643ce80e2a9ac94fa54ca49f",
        "dc2a4459e7369633a52b1bf277839a00201009a3efbf3ecb69bea2186c26b589"
        "09351fc9ac90b3ecfdfbc7c66431e0303dca179c138ac17ad9bef1177331a704",
    ),
]

# RFC 7748 §5.2 标量乘向量
X25519_SCALAR_VECTOR = (
    "a546e36bf0527c9d3b16154b82465edd62144c0ac1fc5a18506a2244ba449ac4",
    "e6db6867583030db3594c1a424b15f7c726624ec26b3353b10a903a6d0ab1c4c",
    "c3da55379de9c6908e94ea4df28d084f32eccf03491c71f754b4075577a28552",
)


def hx(text):
    """十六进制字符串 -> bytes。"""
    return bytes.fromhex(text)


def patch_now(monkeypatch, value):
    """把模块内 ``_now()`` 固定为指定时间戳。"""
    monkeypatch.setattr(core, "_now", lambda: float(value))


def b64url_encode(raw):
    """测试内使用的无 padding base64url 编码。"""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def b64url_decode(text):
    """测试内使用的 base64url 解码。"""
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode((text + padding).encode("ascii"))


def invite_payload(uri):
    """从邀请 URI 中取出 JSON payload（测试伪造用）。"""
    assert uri.startswith(core.INVITE_PREFIX)
    return json.loads(b64url_decode(uri[len(core.INVITE_PREFIX):]).decode("utf-8"))


def rebuild_invite_uri(doc):
    """把修改后的 payload 重新编码为邀请 URI（测试伪造用）。"""
    raw = json.dumps(doc, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=False).encode("utf-8")
    return core.INVITE_PREFIX + b64url_encode(raw)


# ---------------------------------------------------------------------------
# 1. Ed25519（RFC 8032）
# ---------------------------------------------------------------------------

class TestEd25519:
    """Ed25519 官方向量、签名篡改与身份标识格式。"""

    @pytest.mark.parametrize("index", range(len(ED25519_VECTORS)))
    def test_rfc8032_keygen_and_sign(self, index):
        seed_hex, pub_hex, msg_hex, sig_hex = ED25519_VECTORS[index]
        identity = core.Identity(hx(seed_hex))
        assert identity.public_key == hx(pub_hex)
        assert core._b64d(identity.sign(hx(msg_hex))) == hx(sig_hex)

    @pytest.mark.parametrize("index", range(len(ED25519_VECTORS)))
    def test_rfc8032_verify(self, index):
        seed_hex, pub_hex, msg_hex, sig_hex = ED25519_VECTORS[index]
        pub_b64 = core._b64e(hx(pub_hex))
        sig_b64 = core._b64e(hx(sig_hex))
        assert core.verify_signature(pub_b64, hx(msg_hex), sig_b64) is True

    def test_long_message_1023_bytes(self):
        """长消息（≥ 64 字节，覆盖 SHA-512 分块边界）签名往返。"""
        _seed, pub_hex, msg_hex, sig_hex = ED25519_VECTORS[3]
        assert len(hx(msg_hex)) == 64
        assert core.verify_signature(core._b64e(hx(pub_hex)), hx(msg_hex),
                                     core._b64e(hx(sig_hex)))

    def test_tampered_message_rejected(self):
        seed_hex, pub_hex, msg_hex, sig_hex = ED25519_VECTORS[1]
        msg = bytearray(hx(msg_hex))
        msg[0] ^= 0x01
        assert not core.verify_signature(core._b64e(hx(pub_hex)), bytes(msg),
                                         core._b64e(hx(sig_hex)))

    def test_tampered_signature_rejected(self):
        _seed, pub_hex, msg_hex, sig_hex = ED25519_VECTORS[1]
        sig = bytearray(hx(sig_hex))
        sig[3] ^= 0x80
        assert not core.verify_signature(core._b64e(hx(pub_hex)), hx(msg_hex),
                                         core._b64e(bytes(sig)))

    def test_tampered_public_key_rejected(self):
        _seed, pub_hex, msg_hex, sig_hex = ED25519_VECTORS[1]
        pub = bytearray(hx(pub_hex))
        pub[5] ^= 0x01
        assert not core.verify_signature(core._b64e(bytes(pub)), hx(msg_hex),
                                         core._b64e(hx(sig_hex)))

    def test_malleable_scalar_rejected(self):
        """S + L 仍能通过朴素曲线方程，必须被 S < L 检查拒绝。"""
        _seed, pub_hex, msg_hex, sig_hex = ED25519_VECTORS[0]
        sig = bytearray(hx(sig_hex))
        s = int.from_bytes(bytes(sig[32:]), "little")
        malleable = s + core._ED_L
        if malleable >= 2 ** 256:
            pytest.skip("该向量 S+L 溢出 256 位，无法构造")
        sig[32:] = malleable.to_bytes(32, "little")
        assert not core.verify_signature(core._b64e(hx(pub_hex)), hx(msg_hex),
                                         core._b64e(bytes(sig)))

    def test_verify_signature_malformed_returns_false(self):
        seed_hex, pub_hex, msg_hex, sig_hex = ED25519_VECTORS[0]
        good_pub = core._b64e(hx(pub_hex))
        good_sig = core._b64e(hx(sig_hex))
        assert core.verify_signature("not-base64!!!", hx(msg_hex), good_sig) is False
        assert core.verify_signature(good_pub, hx(msg_hex), "AAAA") is False
        assert core.verify_signature("", b"", "") is False
        assert core.verify_signature(good_pub, None, good_sig) is False
        # 32 字节全 0xff 不是合法曲线点
        assert core.verify_signature(core._b64e(b"\xff" * 32), b"x", good_sig) is False

    def test_identity_derived_ids(self):
        identity = core.Identity(hx(ED25519_VECTORS[0][0]))
        assert re.match(r"^[a-z2-7]{16}$", identity.node_id)
        assert re.match(r"^[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}$",
                        identity.fingerprint)
        assert core.node_id_from_public_key(identity.public_key_b64) == identity.node_id
        assert core.node_id_from_public_key("bad!") == ""

    def test_identity_sign_accepts_str_and_bytes(self):
        identity = core.Identity(hx(ED25519_VECTORS[2][0]))
        assert identity.sign("你好") == identity.sign("你好".encode("utf-8"))
        assert identity.verify("你好", identity.sign("你好")) is True

    def test_identity_repr_hides_private_key(self):
        identity = core.Identity(hx(ED25519_VECTORS[0][0]))
        text = repr(identity)
        assert identity.node_id in text
        assert identity.private_key_b64 not in text
        assert identity.private_key_b64[:12] not in text

    def test_from_private_b64_and_seed_validation(self):
        identity = core.Identity.from_private_b64(
            core._b64e(hx(ED25519_VECTORS[0][0])))
        assert identity.public_key == hx(ED25519_VECTORS[0][1])
        with pytest.raises(ValueError):
            core.Identity.from_private_b64(core._b64e(b"\x00" * 31))
        with pytest.raises(ValueError):
            core.Identity(b"\x00" * 31)
        with pytest.raises(ValueError):
            core.Identity(b"\x00" * 32, public_key=b"\x01" * 32)


# ---------------------------------------------------------------------------
# 2. X25519（RFC 7748）
# ---------------------------------------------------------------------------

class TestX25519:
    """X25519 官方标量向量、DH 一致性、边界输入。"""

    def test_rfc7748_scalar_vector(self):
        scalar_hex, u_hex, expect_hex = X25519_SCALAR_VECTOR
        assert core.x25519(hx(scalar_hex), hx(u_hex)) == hx(expect_hex)

    def test_rfc7748_diffie_hellman(self):
        alice_priv = hx("77076d0a7318a57d3c16c17251b26645df4c2f87ebc0992ab177fba51db92c2a")
        alice_pub = hx("8520f0098930a754748b7ddcb43ef75a0dbf3a0d26381af4eba4a98eaa9b4e6a")
        bob_priv = hx("5dab087e624a8a4b79e17f8b83800ee66f3bb1292618b6fd1c2f8b27ff88e0eb")
        bob_pub = hx("de9edb7d7b7dc1b4d35b61c2ece435373f8343c85b78674dadfc7e146f882b4f")
        shared = hx("4a5d9d5ba4ce2de1728e3bf480350f25e07e21c947d19e3376f09b3c1e161742")
        base = b"\x09" + b"\x00" * 31
        assert core.x25519(alice_priv, base) == alice_pub
        assert core.x25519(bob_priv, base) == bob_pub
        assert core.x25519(alice_priv, bob_pub) == shared
        assert core.x25519(bob_priv, alice_pub) == shared

    def test_keypair_consistency(self):
        priv, pub = core.x25519_keypair()
        assert len(priv) == 32 and len(pub) == 32
        assert core.x25519(priv, b"\x09" + b"\x00" * 31) == pub
        other_priv, other_pub = core.x25519_keypair()
        assert core.x25519(priv, other_pub) == core.x25519(other_priv, pub)

    def test_bad_lengths_raise(self):
        for bad in (b"\x00" * 31, b"\x00" * 33, "x" * 32):
            with pytest.raises(ValueError):
                core.x25519(bad, b"\x09" + b"\x00" * 31)
            with pytest.raises(ValueError):
                core.x25519(b"\x00" * 32, bad)

    def test_low_order_point_yields_zero(self):
        priv, _pub = core.x25519_keypair()
        assert core.x25519(priv, b"\x00" * 32) == b"\x00" * 32


# ---------------------------------------------------------------------------
# 3. HKDF / 密封加密
# ---------------------------------------------------------------------------

class TestHkdfAndSeal:
    """HKDF 官方向量与 seal/open_sealed 的完整性/机密性。"""

    def test_rfc5869_hkdf_sha256_case1(self):
        ikm = hx("0b" * 22)
        salt = hx("000102030405060708090a0b0c")
        info = hx("f0f1f2f3f4f5f6f7f8f9")
        expect = hx("3cb25f25faacd57a90434f64d0362f2a2d2d0a90cf1a5a4c5db02d56ecc4c5bf"
                    "34007208d5b887185865")
        assert core.hkdf_sha256(ikm, salt, info, 42) == expect

    def test_hkdf_validation(self):
        with pytest.raises(ValueError):
            core.hkdf_sha256(b"a", b"b", b"c", 0)
        with pytest.raises(ValueError):
            core.hkdf_sha256(b"a", b"b", b"c", 255 * 32 + 1)
        assert len(core.hkdf_sha256(b"k", b"", b"info", 64)) == 64

    def test_seal_roundtrip_and_version_prefix(self):
        key = b"k" * 32
        token = core.seal(key, "机密数据 🛰")
        assert token.startswith(core.SEAL_PREFIX)
        assert core.open_sealed(key, token) == "机密数据 🛰".encode("utf-8")
        assert core.open_sealed(key, core.seal(key, b"")) == b""

    def test_seal_nonce_randomized(self):
        key = b"k" * 32
        first = core.seal(key, b"same")
        second = core.seal(key, b"same")
        assert first != second
        assert core.open_sealed(key, first) == core.open_sealed(key, second)

    def test_seal_large_binary_payload(self):
        key = os.urandom(32)
        payload = bytes(bytearray((i * 37) % 256 for i in range(5000)))
        assert core.open_sealed(key, core.seal(key, payload)) == payload

    def test_ciphertext_tamper_detected(self):
        key = b"k" * 32
        token = core.seal(key, b"payload", aad=b"ctx")
        raw = bytearray(core._b64d(token[len(core.SEAL_PREFIX):]))
        raw[20] ^= 0x01                       # 翻转密文第 4 字节
        tampered = core.SEAL_PREFIX + core._b64e(bytes(raw))
        with pytest.raises(ValueError, match="完整性"):
            core.open_sealed(key, tampered, aad=b"ctx")

    def test_nonce_and_tag_tamper_detected(self):
        key = b"k" * 32
        token = core.seal(key, b"payload")
        raw = bytearray(core._b64d(token[len(core.SEAL_PREFIX):]))
        raw[0] ^= 0x01                        # nonce
        with pytest.raises(ValueError):
            core.open_sealed(key, core.SEAL_PREFIX + core._b64e(bytes(raw)))
        raw2 = bytearray(core._b64d(token[len(core.SEAL_PREFIX):]))
        raw2[-1] ^= 0x01                      # tag
        with pytest.raises(ValueError):
            core.open_sealed(key, core.SEAL_PREFIX + core._b64e(bytes(raw2)))

    def test_aad_mismatch_rejected(self):
        key = b"k" * 32
        token = core.seal(key, b"payload", aad=b"node-a")
        with pytest.raises(ValueError, match="完整性"):
            core.open_sealed(key, token, aad=b"node-b")
        with pytest.raises(ValueError):
            core.open_sealed(key, token)

    def test_wrong_key_rejected(self):
        token = core.seal(b"a" * 32, b"payload")
        with pytest.raises(ValueError):
            core.open_sealed(b"b" * 32, token)

    def test_format_errors_raise(self):
        key = b"k" * 32
        for bad in ("plain", "v5.2$2$AAAA", "enc2$@@@", "enc2$" + core._b64e(b"short")):
            with pytest.raises(ValueError):
                core.open_sealed(key, bad)

    def test_short_key_rejected(self):
        with pytest.raises(ValueError):
            core.seal(b"short", b"x")
        with pytest.raises(ValueError):
            core.open_sealed(b"short", core.SEAL_PREFIX + "AAAA")

    def test_derive_pair_key(self):
        shared = b"s" * 32
        first = core.derive_pair_key(shared, b"transcript")
        second = core.derive_pair_key(shared, b"transcript")
        assert first == second and len(first) == 32
        assert core.derive_pair_key(shared, b"other") != first
        with pytest.raises(ValueError):
            core.derive_pair_key(b"short", b"t")


# ---------------------------------------------------------------------------
# 4. 请求签名 / 防重放
# ---------------------------------------------------------------------------

class TestRequestSignature:
    """请求签名基串、篡改检测、时间窗与重放。"""

    @pytest.fixture()
    def identity(self):
        return core.Identity(b"\x11" * 32)

    def test_sign_and_verify_ok(self, identity):
        headers = core.sign_request(identity, "POST", "/fleet/mail", b'{"x":1}')
        assert set(headers) == {"X-Fleet-Node", "X-Fleet-Ts",
                                "X-Fleet-Nonce", "X-Fleet-Sig"}
        ok, reason = core.verify_request(identity.public_key_b64, "POST",
                                         "/fleet/mail", b'{"x":1}', headers)
        assert ok is True and reason == ""
        # 方法大小写不敏感
        ok2, _ = core.verify_request(identity.public_key_b64, "post",
                                     "/fleet/mail", b'{"x":1}', headers)
        assert ok2 is True

    def test_header_lookup_case_insensitive(self, identity):
        headers = core.sign_request(identity, "GET", "/fleet/caps", b"")
        upper = dict((k.upper(), v) for k, v in headers.items())
        ok, _ = core.verify_request(identity.public_key_b64, "GET",
                                    "/fleet/caps", b"", upper)
        assert ok is True

    def test_body_tamper_rejected(self, identity):
        headers = core.sign_request(identity, "POST", "/fleet/mail", b'{"x":1}')
        ok, reason = core.verify_request(identity.public_key_b64, "POST",
                                         "/fleet/mail", b'{"x":2}', headers)
        assert ok is False and "签名" in reason

    def test_path_and_method_tamper_rejected(self, identity):
        headers = core.sign_request(identity, "POST", "/fleet/mail", b"body")
        ok, _ = core.verify_request(identity.public_key_b64, "POST",
                                    "/fleet/exec", b"body", headers)
        assert ok is False
        ok2, _ = core.verify_request(identity.public_key_b64, "DELETE",
                                     "/fleet/mail", b"body", headers)
        assert ok2 is False

    def test_timestamp_tamper_rejected(self, identity):
        headers = core.sign_request(identity, "GET", "/fleet/health", b"")
        headers = dict(headers)
        headers["X-Fleet-Ts"] = "%.3f" % (float(headers["X-Fleet-Ts"]) + 1.0)
        ok, reason = core.verify_request(identity.public_key_b64, "GET",
                                         "/fleet/health", b"", headers)
        assert ok is False and "签名" in reason

    def test_missing_headers_rejected(self, identity):
        for headers in ({}, {"X-Fleet-Node": identity.node_id},
                        {"X-Fleet-Ts": "1.0", "X-Fleet-Nonce": "a" * 16,
                         "X-Fleet-Sig": "x"}):
            ok, reason = core.verify_request(identity.public_key_b64, "GET",
                                             "/x", b"", headers)
            assert ok is False and reason

    def test_wrong_identity_rejected(self, identity):
        other = core.Identity(b"\x22" * 32)
        headers = core.sign_request(identity, "GET", "/x", b"")
        ok, reason = core.verify_request(other.public_key_b64, "GET", "/x",
                                         b"", headers)
        assert ok is False and "不匹配" in reason

    def test_clock_skew_rejected(self, identity):
        headers = core.sign_request(identity, "GET", "/x", b"",
                                    ts=time.time() - 10000)
        ok, reason = core.verify_request(identity.public_key_b64, "GET", "/x",
                                         b"", headers)
        assert ok is False and "时间戳" in reason

    def test_replay_with_guard_rejected(self, identity):
        guard = core.ReplayGuard(window_s=300)
        headers = core.sign_request(identity, "POST", "/fleet/mail", b"body")
        first, _ = core.verify_request(identity.public_key_b64, "POST",
                                       "/fleet/mail", b"body", headers,
                                       guard=guard)
        second, reason = core.verify_request(identity.public_key_b64, "POST",
                                             "/fleet/mail", b"body", headers,
                                             guard=guard)
        assert first is True
        assert second is False and "重放" in reason

    def test_replay_guard_scope_isolation(self, monkeypatch):
        patch_now(monkeypatch, 1000.0)
        guard = core.ReplayGuard(window_s=300)
        assert guard.check("node-a", "nonce-1", 1000.0) is True
        assert guard.check("node-b", "nonce-1", 1000.0) is True
        assert guard.check("node-a", "nonce-1", 1000.0) is False

    def test_replay_guard_window_expiry(self, monkeypatch):
        guard = core.ReplayGuard(window_s=300)
        patch_now(monkeypatch, 1000.0)
        assert guard.check("scope", "nonce-old", 1000.0) is True
        # 请求时间戳太旧（超出窗口）
        patch_now(monkeypatch, 1400.0)
        assert guard.check("scope", "nonce-new", 1000.0) is False
        # 未来时间戳同样超出窗口
        assert guard.check("scope", "nonce-future", 1800.0) is False
        # 窗口内新 nonce 仍可放行
        assert guard.check("scope", "nonce-ok", 1350.0) is True

    def test_replay_guard_capacity_bound(self, monkeypatch):
        patch_now(monkeypatch, 5000.0)
        guard = core.ReplayGuard(window_s=300, max_entries=2)
        for index in range(5):
            assert guard.check("scope", "n-%d" % index, 5000.0) is True
        assert len(guard._seen) <= 2
        with pytest.raises(ValueError):
            core.ReplayGuard(window_s=0)
        with pytest.raises(ValueError):
            core.ReplayGuard(max_entries=0)


# ---------------------------------------------------------------------------
# 5. 邀请 / 配对
# ---------------------------------------------------------------------------

class TestInvite:
    """邀请创建/解析、伪造、过期与字段校验。"""

    @pytest.fixture()
    def identity(self):
        return core.Identity(b"\x33" * 32)

    def test_roundtrip(self, identity):
        invite = core.Invite.create(identity, "nas-01", "192.0.2.10", 3100,
                                    ttl=3600, caps={"exec": True, "gpu": False})
        uri = invite.to_uri()
        assert uri.startswith(core.INVITE_PREFIX)
        parsed = core.Invite.parse(uri)
        assert parsed.name == "nas-01"
        assert parsed.node_id == identity.node_id
        assert parsed.pubkey == identity.public_key_b64
        assert parsed.host == "192.0.2.10"
        assert parsed.port == 3100
        assert parsed.ttl == 3600
        assert parsed.caps == {"exec": True, "gpu": False}
        assert core.Invite.parse(uri).nonce == parsed.nonce

    def test_to_uri_requires_signature(self, identity):
        unsigned = core.Invite(v=1, name="x", node_id=identity.node_id,
                               pubkey=identity.public_key_b64, host="h",
                               port=1, ts=int(time.time()), ttl=60,
                               nonce="abcd", caps={}, sig="")
        with pytest.raises(ValueError, match="签名"):
            unsigned.to_uri()

    def test_wrong_prefix_rejected(self):
        with pytest.raises(ValueError, match="邀请 URI"):
            core.Invite.parse("https://example.com/join/abc")
        with pytest.raises(ValueError):
            core.Invite.parse(12345)

    def test_malformed_payload_rejected(self):
        with pytest.raises(ValueError):
            core.Invite.parse(core.INVITE_PREFIX + "!!!not-base64!!!")
        with pytest.raises(ValueError):
            core.Invite.parse(core.INVITE_PREFIX + b64url_encode(b"not json"))

    def test_forged_signature_rejected(self, identity):
        uri = core.Invite.create(identity, "nas", "127.0.0.1", 3100).to_uri()
        doc = invite_payload(uri)
        other = core.Identity(b"\x44" * 32)
        doc["sig"] = other.sign(b"anything")
        with pytest.raises(ValueError, match="签名"):
            core.Invite.parse(rebuild_invite_uri(doc))

    def test_tampered_name_rejected(self, identity):
        uri = core.Invite.create(identity, "nas", "127.0.0.1", 3100).to_uri()
        doc = invite_payload(uri)
        doc["name"] = "evil"
        with pytest.raises(ValueError, match="签名"):
            core.Invite.parse(rebuild_invite_uri(doc))

    def test_node_id_mismatch_rejected(self, identity):
        uri = core.Invite.create(identity, "nas", "127.0.0.1", 3100).to_uri()
        doc = invite_payload(uri)
        doc["node_id"] = core.Identity(b"\x55" * 32).node_id
        with pytest.raises(ValueError, match="不匹配"):
            core.Invite.parse(rebuild_invite_uri(doc))

    def test_expired_invite_rejected(self, identity, monkeypatch):
        invite = core.Invite.create(identity, "nas", "127.0.0.1", 3100, ttl=60)
        uri = invite.to_uri()
        patch_now(monkeypatch, invite.ts + 59)
        assert core.Invite.parse(uri).name == "nas"
        patch_now(monkeypatch, invite.ts + 61)
        with pytest.raises(ValueError, match="过期"):
            core.Invite.parse(uri)

    def test_create_validates_fields(self, identity):
        with pytest.raises(ValueError):
            core.Invite.create(identity, "", "127.0.0.1", 3100)
        with pytest.raises(ValueError):
            core.Invite.create(identity, "n", "127.0.0.1", 0)
        with pytest.raises(ValueError):
            core.Invite.create(identity, "n", "127.0.0.1", 70000)
        with pytest.raises(ValueError):
            core.Invite.create(identity, "n", "127.0.0.1", 3100, ttl=0)
        with pytest.raises(ValueError):
            core.Invite.create(identity, "n", "127.0.0.1", 3100, caps="bad")

    def test_engine_signed_invite_is_verifiable(self, identity):
        uri = core.Invite.create(identity, "nas", "127.0.0.1", 3100).to_uri()
        doc = invite_payload(uri)
        assert core.verify_signature(doc["pubkey"],
                                     core._DOMAIN_INVITE + core._canon_json(
                                         dict((k, v) for k, v in doc.items()
                                              if k != "sig")),
                                     doc["sig"])


class TestPairProof:
    """配对 SAS/密钥一致性、proof 验签、低阶临时公钥拒绝。"""

    @pytest.fixture()
    def peers(self):
        host = core.Identity(b"\x66" * 32)
        joiner = core.Identity(b"\x77" * 32)
        invite = core.Invite.create(host, "host", "127.0.0.1", 3100)
        return host, joiner, invite

    def test_sas_and_key_agree_on_both_sides(self, peers):
        host, joiner, invite = peers
        joiner_view = core.Invite.parse(invite.to_uri())   # 加入方解析同一份邀请
        host_priv, host_pub = core.x25519_keypair()
        join_priv, join_pub = core.x25519_keypair()
        proof_h, sas_h, key_h = core.pair_proof(
            host, invite, host_priv, host_pub, join_pub)
        proof_j, sas_j, key_j = core.pair_proof(
            joiner, joiner_view, join_priv, join_pub, host_pub)
        assert sas_h == sas_j
        assert key_h == key_j and len(key_h) == 32
        assert re.match(r"^\d{6}$", sas_h)
        assert proof_h != proof_j

    def test_proofs_verify_with_own_public_keys(self, peers):
        host, joiner, invite = peers
        host_priv, host_pub = core.x25519_keypair()
        join_priv, join_pub = core.x25519_keypair()
        proof_h, _sas, _key = core.pair_proof(host, invite, host_priv, host_pub, join_pub)
        proof_j, _sas, _key = core.pair_proof(joiner, invite, join_priv, join_pub, host_pub)
        transcript = core._pair_transcript(invite, host_pub, join_pub)
        assert core.verify_signature(host.public_key_b64,
                                     core._DOMAIN_PROOF + transcript, proof_h)
        assert core.verify_signature(joiner.public_key_b64,
                                     core._DOMAIN_PROOF + transcript, proof_j)

    def test_sas_differs_for_different_ephemerals(self, peers):
        host, _joiner, invite = peers
        priv1, pub1 = core.x25519_keypair()
        priv2, pub2 = core.x25519_keypair()
        _p, sas1, key1 = core.pair_proof(host, invite, priv1, pub1, pub2)
        _p, sas2, key2 = core.pair_proof(host, invite, priv2, pub2, pub1)
        # 传入顺序对调但密钥对也交换：转录相同，因此 SAS/key 相同
        assert sas1 == sas2 and key1 == key2
        _priv3, pub3 = core.x25519_keypair()
        _p, sas3, key3 = core.pair_proof(host, invite, priv1, pub1, pub3)
        assert sas3 != sas1
        assert key3 != key1

    def test_low_order_peer_key_rejected(self, peers):
        host, _joiner, invite = peers
        priv, pub = core.x25519_keypair()
        with pytest.raises(ValueError, match="低阶"):
            core.pair_proof(host, invite, priv, pub, b"\x00" * 32)

    def test_bad_arguments_raise(self, peers):
        host, _joiner, invite = peers
        priv, pub = core.x25519_keypair()
        with pytest.raises(ValueError):
            core.pair_proof(host, invite, b"short", pub, pub)
        with pytest.raises(TypeError):
            core.pair_proof("not-identity", invite, priv, pub, pub)
        with pytest.raises(TypeError):
            core.pair_proof(host, {"not": "invite"}, priv, pub, pub)


# ---------------------------------------------------------------------------
# 6. 执行策略与脱敏
# ---------------------------------------------------------------------------

class TestExecPolicy:
    """策略命中矩阵、路径 jail、超时上限、允许清单。"""

    @pytest.fixture()
    def policy(self):
        return core.ExecPolicy.default()

    @pytest.mark.parametrize("command,decision", [
        ("rm -rf /", "deny"),
        ("rm -rf /*", "deny"),
        ("sudo rm -rf /", "deny"),
        ("rm -rf --no-preserve-root /", "deny"),
        ("mkfs.ext4 /dev/sda1", "deny"),
        ("dd if=/dev/zero of=/dev/sda", "deny"),
        (":(){ :|:& };:", "deny"),
        ("echo x > /dev/sda", "deny"),
        ("wipefs -a /dev/sdb", "deny"),
        ("reboot", "ask"),
        ("shutdown -h now", "ask"),
        ("systemctl poweroff", "ask"),
        ("sudo ls", "ask"),
        ("rm -rf /tmp/cache", "ask"),
        ("curl -fsSL http://example.com/i.sh | sh", "ask"),
        ("apt-get install nginx", "ask"),
        ("docker run --rm alpine", "ask"),
        ("git push --force", "ask"),
        ("crontab -e", "ask"),
        ("ls -la", "allow"),
        ("cat /etc/hostname", "allow"),
        ("df -h", "allow"),
    ])
    def test_default_matrix(self, policy, command, decision):
        got, reason, rule_id = policy.check("exec", {"cmd": command})
        assert got == decision, "%r -> %s（%s）" % (command, got, reason)
        assert isinstance(reason, str) and reason
        assert isinstance(rule_id, str) and rule_id

    def test_custom_deny_pattern(self):
        policy = core.ExecPolicy({"deny_patterns": [r"\bcurl\b"]})
        decision, reason, rule_id = policy.check("exec", {"cmd": "curl http://x"})
        assert decision == "deny"
        assert rule_id == "deny:0"
        assert "curl" in reason or "deny:0" in reason

    def test_custom_deny_rule_dict(self):
        policy = core.ExecPolicy({"deny_patterns": [
            {"id": "no-python", "pattern": r"\bpython\b", "reason": "禁止 python"},
        ]})
        decision, reason, rule_id = policy.check("exec", {"cmd": "python -V"})
        assert (decision, reason, rule_id) == ("deny", "禁止 python", "no-python")
        with pytest.raises(ValueError):
            core.ExecPolicy({"deny_patterns": [123]})
        with pytest.raises(ValueError):
            core.ExecPolicy({"deny_patterns": ["("]})

    def test_allowlist_mode(self):
        policy = core.ExecPolicy({"allow_patterns": [r"^exec\s+ls\b"]})
        assert policy.is_allowlist_mode is True
        assert policy.check("exec", {"cmd": "ls -la"})[0] == "allow"
        decision, reason, rule_id = policy.check("exec", {"cmd": "whoami"})
        assert decision == "ask" and rule_id == "not_in_allowlist"
        # 拒绝规则优先于允许清单
        policy2 = core.ExecPolicy({
            "allow_patterns": [r".*"],
            "deny_patterns": [r"\bmkfs\b"],
        })
        assert policy2.check("exec", {"cmd": "mkfs /dev/sda"})[0] == "deny"

    def test_path_jail(self, tmp_path):
        root = tmp_path / "root"
        root.mkdir()
        policy = core.ExecPolicy({"path_jail": [str(root)]})
        inside = root / "a.txt"
        assert policy.check("read_file", {"path": str(inside)})[0] == "allow"
        decision, reason, rule_id = policy.check("read_file",
                                                 {"path": "/etc/passwd"})
        assert (decision, rule_id) == ("deny", "path_jail")
        # ../ 穿越必须被规范化后识破
        escape = os.path.join(str(root), "..", "etc", "passwd")
        assert policy.check("read_file", {"path": escape})[0] == "deny"
        # 相对路径按第一个 jail 根解析
        assert policy.check("read_file", {"path": "sub/a.txt"})[0] == "allow"
        # URL 不当作文件路径
        assert policy.check("http_get", {"path": "https://example.com/x"})[0] == "allow"

    def test_max_timeout(self):
        policy = core.ExecPolicy({"max_timeout": 30})
        decision, _reason, rule_id = policy.check(
            "exec", {"cmd": "sleep 1", "timeout": 31})
        assert (decision, rule_id) == ("ask", "timeout_exceeds")
        assert policy.check("exec", {"cmd": "sleep 1", "timeout": 5})[0] == "allow"
        assert policy.check("exec", {"cmd": "x", "timeout": "abc"})[0] == "deny"
        assert policy.check("exec", {"cmd": "x", "timeout": -1})[0] == "deny"
        with pytest.raises(ValueError):
            core.ExecPolicy({"max_timeout": 0})

    def test_custom_approval_rules(self):
        policy = core.ExecPolicy({"require_approval": [
            {"id": "deploy", "pattern": r"\bdeploy\b", "reason": "上线需审批"},
        ]})
        assert policy.check("exec", {"cmd": "bash deploy.sh"}) == (
            "ask", "上线需审批", "deploy")
        assert policy.check("exec", {"cmd": "echo hi"})[0] == "allow"

    def test_invalid_arguments(self, policy):
        with pytest.raises(ValueError):
            policy.check("", {"cmd": "ls"})
        with pytest.raises(TypeError):
            policy.check("exec", "not-a-dict")
        assert policy.check("exec", None)[0] == "allow"
        with pytest.raises(ValueError):
            core.ExecPolicy("not-a-dict")


class TestRedact:
    """脱敏：token/密码/私钥/邀请码/密封令牌。"""

    def test_key_value_secrets(self):
        text = ('token=abc123 password: "hunter2" api_key=sk-xyz '
                'private_key_b64: QUJDREVG')
        result = core.redact(text)
        for secret in ("abc123", "hunter2", "sk-xyz", "QUJDREVG"):
            assert secret not in result
        assert "***" in result

    def test_authorization_bearer(self):
        result = core.redact("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig")
        assert "eyJhbGciOiJIUzI1NiJ9" not in result
        assert "Bearer ***" in result

    def test_private_key_block(self):
        text = ("before\n-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n"
                "-----END RSA PRIVATE KEY-----\nafter")
        result = core.redact(text)
        assert "MIIEow" not in result
        assert "[REDACTED PRIVATE KEY]" in result
        assert result.startswith("before") and result.endswith("after")

    def test_invite_and_sealed_tokens(self):
        text = ("join fleet://join/eyJ2IjoxfQ token enc2$QUJD "
                "old enc1$QUJD")
        result = core.redact(text)
        assert "eyJ2IjoxfQ" not in result
        assert "enc2$QUJD" not in result
        assert "enc1$QUJD" not in result
        assert "fleet://join/[REDACTED]" in result

    def test_cloud_tokens(self):
        result = core.redact("AKIAIOSFODNN7EXAMPLE ghp_abcdefghijklmnopqrstuvwxyz012345")
        assert "AKIAIOSFODNN7EXAMPLE" not in result
        assert "ghp_abcdefghijklmnopqrstuvwxyz012345" not in result

    def test_redact_type_error(self):
        with pytest.raises(TypeError):
            core.redact(b"bytes")


# ---------------------------------------------------------------------------
# 7. 审计哈希链
# ---------------------------------------------------------------------------

def read_audit_lines(path):
    """把审计文件读成行列表（用于篡改测试）。"""
    with open(path, "rb") as handle:
        return handle.read().decode("utf-8").splitlines()


def write_audit_lines(path, lines):
    """重写审计文件（模拟攻击者篡改）。"""
    with open(path, "wb") as handle:
        handle.write(("\n".join(lines) + "\n").encode("utf-8"))


class TestAuditChain:
    """哈希链追加/校验/篡改定位/并发。"""

    def test_append_verify_tail(self, tmp_path):
        chain = core.AuditChain(str(tmp_path / "audit.jsonl"))
        hashes = [chain.append({"event": "tool", "n": i}) for i in range(5)]
        assert len(set(hashes)) == 5
        assert chain.verify() == (True, -1)
        tail = chain.tail(2)
        assert len(tail) == 2
        assert tail[-1]["event"]["n"] == 4
        assert tail[-1]["idx"] == 4
        # 链式 prev 指向
        records = core.read_jsonl(str(tmp_path / "audit.jsonl"))
        assert records[0]["prev"] == core.AuditChain.GENESIS
        assert records[1]["prev"] == records[0]["hash"]

    def test_empty_chain_verifies(self, tmp_path):
        chain = core.AuditChain(str(tmp_path / "missing.jsonl"))
        assert chain.verify() == (True, -1)
        assert chain.tail(10) == []

    def test_tampered_event_locates_line(self, tmp_path):
        path = str(tmp_path / "audit.jsonl")
        chain = core.AuditChain(path)
        for i in range(4):
            chain.append({"event": "exec", "n": i})
        lines = read_audit_lines(path)
        record = json.loads(lines[2])
        record["event"]["n"] = 999
        lines[2] = json.dumps(record, sort_keys=True, separators=(",", ":"))
        write_audit_lines(path, lines)
        assert chain.verify() == (False, 2)

    def test_tampered_hash_locates_line(self, tmp_path):
        path = str(tmp_path / "audit.jsonl")
        chain = core.AuditChain(path)
        for i in range(3):
            chain.append({"event": "x", "n": i})
        lines = read_audit_lines(path)
        record = json.loads(lines[1])
        record["hash"] = "f" * 64
        lines[1] = json.dumps(record, sort_keys=True, separators=(",", ":"))
        write_audit_lines(path, lines)
        assert chain.verify() == (False, 1)

    def test_deleted_line_located(self, tmp_path):
        path = str(tmp_path / "audit.jsonl")
        chain = core.AuditChain(path)
        for i in range(3):
            chain.append({"event": "x", "n": i})
        lines = read_audit_lines(path)
        del lines[1]
        write_audit_lines(path, lines)
        assert chain.verify() == (False, 1)

    def test_reordered_lines_located(self, tmp_path):
        path = str(tmp_path / "audit.jsonl")
        chain = core.AuditChain(path)
        for i in range(3):
            chain.append({"event": "x", "n": i})
        lines = read_audit_lines(path)
        lines[1], lines[2] = lines[2], lines[1]
        write_audit_lines(path, lines)
        assert chain.verify() == (False, 1)

    def test_corrupt_json_located(self, tmp_path):
        path = str(tmp_path / "audit.jsonl")
        chain = core.AuditChain(path)
        chain.append({"event": "x"})
        chain.append({"event": "y"})
        lines = read_audit_lines(path)
        lines[1] = "{not json"
        write_audit_lines(path, lines)
        assert chain.verify() == (False, 1)

    def test_append_rejects_non_dict(self, tmp_path):
        chain = core.AuditChain(str(tmp_path / "audit.jsonl"))
        with pytest.raises(TypeError):
            chain.append(["not", "dict"])

    def test_concurrent_append_no_loss(self, tmp_path):
        chain = core.AuditChain(str(tmp_path / "audit.jsonl"))
        total = 8 * 15
        errors = []

        def worker(worker_id):
            try:
                for index in range(15):
                    chain.append({"worker": worker_id, "n": index})
            except Exception as exc:   # pragma: no cover - 失败时用于报错
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert errors == []
        assert chain.verify() == (True, -1)
        records = core.read_jsonl(str(tmp_path / "audit.jsonl"))
        assert len(records) == total
        assert sorted(record["idx"] for record in records) == list(range(total))


# ---------------------------------------------------------------------------
# 8. 持久化 / 原子写
# ---------------------------------------------------------------------------

@pytest.mark.skipif(os.name == "nt", reason="Windows 无 POSIX 权限位")
class TestPersistence:
    """原子写权限、父目录权限、无临时文件、JSONL 读写。"""

    def test_atomic_write_permissions_and_content(self, tmp_path):
        target = tmp_path / "secret.json"
        core.atomic_write(str(target), "中文内容")
        assert target.read_text(encoding="utf-8") == "中文内容"
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
        core.atomic_write(str(target), b"bytes")
        assert target.read_bytes() == b"bytes"
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
        assert core.atomic_write(str(target), "x", 0o640) is None
        assert stat.S_IMODE(target.stat().st_mode) == 0o640

    def test_atomic_write_creates_parent_0700(self, tmp_path):
        target = tmp_path / "a" / "b" / "file.json"
        core.atomic_write(str(target), "{}")
        parent = tmp_path / "a" / "b"
        assert parent.is_dir()
        assert stat.S_IMODE(parent.stat().st_mode) == 0o700

    def test_atomic_write_leaves_no_temp_file(self, tmp_path):
        core.atomic_write(str(tmp_path / "f.json"), "data")
        leftovers = [name for name in os.listdir(str(tmp_path))
                     if ".tmp." in name]
        assert leftovers == []

    def test_atomic_write_type_error(self, tmp_path):
        with pytest.raises(TypeError):
            core.atomic_write(str(tmp_path / "f"), {"a": 1})

    def test_append_and_read_jsonl(self, tmp_path):
        path = str(tmp_path / "events.jsonl")
        for index in range(5):
            core.append_jsonl(path, {"n": index, "unicode": "值"})
        records = core.read_jsonl(path)
        assert [record["n"] for record in records] == list(range(5))
        assert core.read_jsonl(path, limit=2) == records[-2:]
        assert core.read_jsonl(str(tmp_path / "absent.jsonl")) == []
        assert core.read_jsonl(str(tmp_path / "events.jsonl"), limit=0) == records

    def test_append_jsonl_permissions(self, tmp_path):
        path = tmp_path / "events.jsonl"
        core.append_jsonl(str(path), {"a": 1})
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_read_jsonl_bad_line_raises(self, tmp_path):
        path = tmp_path / "bad.jsonl"
        path.write_text('{"ok":1}\nnot-json\n', encoding="utf-8")
        with pytest.raises(ValueError, match="第 2 行"):
            core.read_jsonl(str(path))

    def test_concurrent_jsonl_append_no_loss(self, tmp_path):
        path = str(tmp_path / "concurrent.jsonl")
        errors = []

        def worker(worker_id):
            try:
                for index in range(20):
                    core.append_jsonl(path, {"worker": worker_id, "n": index})
            except Exception as exc:   # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert errors == []
        records = core.read_jsonl(path)
        assert len(records) == 200
        assert all(isinstance(record, dict) for record in records)

    def test_constant_time_eq(self):
        assert core.constant_time_eq("abc", "abc") is True
        assert core.constant_time_eq("abc", "abd") is False
        assert core.constant_time_eq("abc", b"abc") is False
        assert core.constant_time_eq(None, None) is False
        assert core.constant_time_eq(b"\x00", b"\x00") is True

    def test_now_ms_and_clock_skew(self, monkeypatch):
        assert isinstance(core.now_ms(), int)
        patch_now(monkeypatch, 1000.0)
        assert core.now_ms() == 1000000
        assert core.check_clock_skew(1000.0) == (True, 0)
        assert core.check_clock_skew(900.0, 300) == (True, 100)
        assert core.check_clock_skew(500.0, 300) == (False, 500)
        assert core.check_clock_skew(1200.0, 300) == (True, -200)
        assert core.check_clock_skew(1200.0, 100) == (False, -200)
        with pytest.raises(ValueError):
            core.check_clock_skew("not-a-number")


# ---------------------------------------------------------------------------
# 9. 指标
# ---------------------------------------------------------------------------

class TestMetrics:
    """counter/gauge/histogram、快照、Prometheus 渲染与线程安全。"""

    def test_counter_and_labels(self):
        metrics = core.Metrics()
        metrics.inc("requests")
        metrics.inc("requests", 2, method="GET")
        snapshot = metrics.snapshot()
        assert snapshot["counters"]["requests"] == 1
        assert snapshot["counters"]['requests{method="GET"}'] == 2

    def test_gauge(self):
        metrics = core.Metrics()
        metrics.set_gauge("queue_depth", 3)
        metrics.set_gauge("queue_depth", 1.5)
        assert metrics.snapshot()["gauges"]["queue_depth"] == 1.5
        with pytest.raises(ValueError):
            metrics.set_gauge("x", "not-a-number")

    def test_histogram_buckets(self):
        metrics = core.Metrics()
        for value in (0.02, 50.0, 0.003):
            metrics.observe("latency_s", value)
        hist = metrics.snapshot()["histograms"]["latency_s"]
        assert hist["count"] == 3
        assert hist["sum"] == pytest.approx(50.023)
        assert hist["min"] == pytest.approx(0.003)
        assert hist["max"] == pytest.approx(50.0)
        # 桶计数为累计值（<= 上界）
        assert hist["buckets"]["0.005"] == 1
        assert hist["buckets"]["0.01"] == 1
        assert hist["buckets"]["10"] == 2
        assert hist["buckets"]["+Inf" if "+Inf" in hist["buckets"] else "10"] == 2
        with pytest.raises(ValueError):
            metrics.observe("x", "bad")

    def test_prometheus_rendering(self):
        metrics = core.Metrics()
        metrics.inc("requests", 2, method="GET")
        metrics.set_gauge("queue_depth", 3)
        metrics.observe("latency_s", 0.02, node="nas")
        text = metrics.render_prometheus()
        assert "# TYPE requests counter" in text
        assert 'requests{method="GET"} 2' in text
        assert "# TYPE queue_depth gauge" in text
        assert "queue_depth 3" in text
        assert "# TYPE latency_s histogram" in text
        assert 'latency_s_bucket{le="0.025",node="nas"} 1' in text
        assert 'latency_s_bucket{le="+Inf",node="nas"} 1' in text
        assert 'latency_s_sum{node="nas"} 0.02' in text
        assert 'latency_s_count{node="nas"} 1' in text
        assert text.endswith("\n")

    def test_metric_name_sanitized(self):
        metrics = core.Metrics()
        metrics.inc("weird-name.with spaces")
        assert "weird_name_with_spaces" in metrics.render_prometheus()

    def test_prefix(self):
        metrics = core.Metrics(prefix="fleet_")
        metrics.inc("requests")
        assert "fleet_requests" in metrics.snapshot()["counters"]

    def test_thread_safety(self):
        metrics = core.Metrics()
        errors = []

        def worker():
            try:
                for _ in range(100):
                    metrics.inc("hits")
                    metrics.observe("latency", 0.01)
            except Exception as exc:   # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert errors == []
        snapshot = metrics.snapshot()
        assert snapshot["counters"]["hits"] == 1000
        assert snapshot["histograms"]["latency"]["count"] == 1000

    def test_invalid_metric_name(self):
        metrics = core.Metrics()
        with pytest.raises(ValueError):
            metrics.inc("")


# ---------------------------------------------------------------------------
# 10. 身份文件
# ---------------------------------------------------------------------------

@pytest.mark.skipif(os.name == "nt", reason="Windows 无 POSIX 权限位")
class TestIdentityFile:
    """load_or_create 权限、持久化一致性、损坏文件处理。"""

    def test_create_load_and_permissions(self, tmp_path):
        path = str(tmp_path / "identity.json")
        created = core.Identity.load_or_create(path)
        assert os.path.exists(path)
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        loaded = core.Identity.load_or_create(path)
        assert loaded.node_id == created.node_id
        assert loaded.public_key_b64 == created.public_key_b64
        # 跨重载签名仍可验证
        signature = loaded.sign(b"hello")
        assert core.verify_signature(created.public_key_b64, b"hello", signature)

    def test_file_is_json_and_no_plaintext_leak_in_repr(self, tmp_path):
        path = tmp_path / "identity.json"
        identity = core.Identity.load_or_create(str(path))
        document = json.loads(path.read_text(encoding="utf-8"))
        assert document["node_id"] == identity.node_id
        assert document["public_key_b64"] == identity.public_key_b64
        assert identity.private_key_b64 not in repr(identity)

    def test_corrupt_file_raises(self, tmp_path):
        path = tmp_path / "identity.json"
        path.write_text("not-json", encoding="utf-8")
        with pytest.raises(ValueError, match="JSON"):
            core.Identity.load_or_create(str(path))

    def test_empty_file_raises(self, tmp_path):
        path = tmp_path / "identity.json"
        path.write_text("", encoding="utf-8")
        with pytest.raises(ValueError, match="为空"):
            core.Identity.load_or_create(str(path))

    def test_mismatched_public_key_raises(self, tmp_path):
        path = tmp_path / "identity.json"
        identity = core.Identity.load_or_create(str(path))
        document = json.loads(path.read_text(encoding="utf-8"))
        other = core.Identity(b"\x01" * 32)
        document["public_key_b64"] = other.public_key_b64
        path.write_text(json.dumps(document), encoding="utf-8")
        with pytest.raises(ValueError, match="公钥"):
            core.Identity.load_or_create(str(path))
        assert identity.node_id != other.node_id


# ---------------------------------------------------------------------------
# 11. TLS 自签（依赖系统 openssl；缺失时 skip）
# ---------------------------------------------------------------------------

class TestTls:
    """自签证书生成与指纹解析。"""

    def test_self_signed_cert_idempotent(self, tmp_path):
        paths = core.ensure_self_signed_cert(str(tmp_path / "tls"), "fleet-test")
        if paths is None:
            pytest.skip("系统无 openssl 或执行失败（降级路径）")
        cert, key = paths
        assert os.path.exists(cert) and os.path.exists(key)
        fingerprint = core.cert_fingerprint(cert)
        assert re.match(r"^sha256:([0-9A-F]{2}:){31}[0-9A-F]{2}$", fingerprint)
        # 第二次调用复用同一证书，指纹稳定
        again = core.ensure_self_signed_cert(str(tmp_path / "tls"), "fleet-test")
        assert again == (cert, key)
        assert core.cert_fingerprint(cert) == fingerprint
        if os.name != "nt":
            assert stat.S_IMODE(os.stat(key).st_mode) == 0o600

    def test_cert_fingerprint_missing_raises(self, tmp_path):
        with pytest.raises(ValueError, match="不存在"):
            core.cert_fingerprint(str(tmp_path / "nope.pem"))
        with pytest.raises(ValueError):
            core.cert_fingerprint("")


# ---------------------------------------------------------------------------
# 12. 兼容性 / 常量
# ---------------------------------------------------------------------------

class TestCompatibility:
    """版本常量与 Python 3.6 语法兼容。"""

    def test_version(self):
        assert core.VERSION == "5.1.1"

    def test_python36_syntax(self):
        source_path = core.__file__
        with open(source_path, "r", encoding="utf-8") as handle:
            source = handle.read()
        # feature_version=(3, 6) 会在遇到 walrus / f-string 之外的 3.7+ 语法时报错
        ast.parse(source, filename=source_path, feature_version=(3, 6))

    def test_no_forbidden_third_party_imports(self):
        with open(core.__file__, "r", encoding="utf-8") as handle:
            source = handle.read()
        for forbidden in ("import mcp_agent", "import fleet_proto",
                          "from mcp_agent", "from fleet_proto"):
            assert forbidden not in source
