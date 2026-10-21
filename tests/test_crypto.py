# -*- coding: utf-8 -*-
"""v5.0 落盘加密与 hello 签名单元测试（fleet_proto.crypto）。"""
import base64

import pytest

from fleet_proto import (CRYPTO_PREFIX, CRYPTO_V2_PREFIX, decrypt_text, encrypt_text,
                         hello_signature, verify_hello_signature)


class TestEncryptDecrypt:
    def test_roundtrip_unicode(self):
        ct = encrypt_text("tok", "帮我看看NAS硬盘还剩多少 🛰")
        assert ct.startswith((CRYPTO_PREFIX, CRYPTO_V2_PREFIX))
        assert decrypt_text("tok", ct) == "帮我看看NAS硬盘还剩多少 🛰"

    def test_roundtrip_long_text(self):
        pt = "x" * 50000  # 跨多个 keystream 块
        assert decrypt_text("k", encrypt_text("k", pt)) == pt

    def test_empty_and_short(self):
        assert decrypt_text("k", encrypt_text("k", "")) == ""
        assert decrypt_text("k", encrypt_text("k", "a")) == "a"

    def test_ciphertext_differs_per_call(self):
        """随机 salt+nonce：同明文两次加密密文不同。"""
        a = encrypt_text("k", "same")
        b = encrypt_text("k", "same")
        assert a != b

    def test_wrong_key_rejected(self):
        ct = encrypt_text("right", "secret")
        with pytest.raises(ValueError):
            decrypt_text("wrong", ct)

    def test_tamper_detected(self):
        ct = encrypt_text("k", "secret")
        prefix = CRYPTO_V2_PREFIX if ct.startswith(CRYPTO_V2_PREFIX) else CRYPTO_PREFIX
        raw = bytearray(base64.b64decode(ct[len(prefix):]))
        raw[len(raw) // 2] ^= 0x01
        with pytest.raises(ValueError):
            decrypt_text("k", prefix + base64.b64encode(bytes(raw)).decode())

    def test_not_a_blob_rejected(self):
        with pytest.raises(ValueError):
            decrypt_text("k", "plain text")

    def test_corrupt_base64_rejected(self):
        with pytest.raises(Exception):
            decrypt_text("k", (CRYPTO_V2_PREFIX if _HAS_V2() else CRYPTO_PREFIX)
                         + "!!!not-base64!!!")


class TestHelloSignature:
    def test_sign_verify_roundtrip(self):
        sig = hello_signature("peer-token", "nas-a3f2", 3100, 1697551600.5)
        pkt = {"name": "nas-a3f2", "port": 3100, "ts": 1697551600.5, "sig": sig}
        assert verify_hello_signature("peer-token", pkt) is True

    def test_wrong_token_fails(self):
        sig = hello_signature("real-token", "n", 3100, 1.5)
        pkt = {"name": "n", "port": 3100, "ts": 1.5, "sig": sig}
        assert verify_hello_signature("attacker-guess", pkt) is False

    def test_forged_field_fails(self):
        """攻击者改 port 字段后签名失效。"""
        sig = hello_signature("t", "n", 3100, 1.5)
        pkt = {"name": "n", "port": 9999, "ts": 1.5, "sig": sig}  # 篡改端口
        assert verify_hello_signature("t", pkt) is False

    def test_json_wire_roundtrip(self):
        """模拟跨节点 json 序列化往返后验签（float ts 精度）。"""
        import json
        sig = hello_signature("t", "cloud-b2c3", 3100, time_ts())
        wire = json.dumps({"name": "cloud-b2c3", "port": 3100,
                           "ts": time_ts(), "sig": sig})
        assert verify_hello_signature("t", json.loads(wire)) is True


def _HAS_V2():
    return encrypt_text("k", "x").startswith(CRYPTO_V2_PREFIX)


def time_ts():
    return 1697551600.123456
