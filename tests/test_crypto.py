# -*- coding: utf-8 -*-
"""加密与检索指纹测试（含 RFC 8439 官方向量）。"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from exerarchive import crypto  # noqa: E402


class CryptoTest(unittest.TestCase):
    def test_01_rfc8439_vector(self):
        """ChaCha20 必须与 RFC 8439 §2.4.2 的官方测试向量一致。"""
        key = bytes(range(32))
        nonce = bytes.fromhex("000000000000004a00000000")
        plaintext = (
            b"Ladies and Gentlemen of the class of '99: If I could offer you only one tip for "
            b"the future, sunscreen would be it."
        )
        expect = bytes.fromhex(
            "6e2e359a2568f98041ba0728dd0d6981e97e7aec1d4360c20a27afccfd9fae0b"
            "f91b65c5524733ab8f593dabcd62b3571639d624e65152ab8f530c359f0861d8"
            "07ca0dbf500d6a6156a38e088a22b65e52bc514d16ccf806818ce91ab7793736"
            "5af90bbf74a35be6b40b8eedf2785e42874d"
        )
        self.assertEqual(crypto.chacha20_xor(key, nonce, plaintext, counter=1), expect)

    def test_02_roundtrip(self):
        c = crypto.Cipher(os.urandom(32), "")
        for sample in ("", "错题本：∫₀¹ x² dx = 1/3 ✓", "a" * 100, "中文" * 500):
            self.assertEqual(c.decrypt_text(c.encrypt_text(sample)), sample)

    def test_03_nondeterministic_and_authenticated(self):
        c = crypto.Cipher(os.urandom(32), "")
        a = c.encrypt_text("同一段明文")
        b = c.encrypt_text("同一段明文")
        self.assertNotEqual(a, b, "相同明文必须产生不同密文（随机 salt/nonce）")
        # 篡改一个字节应当解密失败（MAC 校验）
        tampered = bytearray(a)
        tampered[-1] ^= 0x01
        with self.assertRaises(Exception):
            c.decrypt_bytes(bytes(tampered))

    def test_04_wrong_key_fails(self):
        c1 = crypto.Cipher(os.urandom(32), "")
        c2 = crypto.Cipher(os.urandom(32), "")
        blob = c1.encrypt_text("秘密")
        with self.assertRaises(Exception):
            c2.decrypt_bytes(blob)

    def test_05_passphrase_changes_key(self):
        master = os.urandom(32)
        c1 = crypto.Cipher(master, "pass1")
        c2 = crypto.Cipher(master, "pass2")
        blob = c1.encrypt_text("口令不同则解不开")
        with self.assertRaises(Exception):
            c2.decrypt_bytes(blob)
        self.assertEqual(c1.decrypt_text(blob), "口令不同则解不开")

    def test_06_plaintext_fallback(self):
        """没有魔术头的旧数据/明文数据要原样返回，避免升级后读不出老数据。"""
        c = crypto.Cipher(os.urandom(32), "")
        self.assertEqual(c.decrypt_bytes(b"plain old data"), b"plain old data")

    def test_07_index_tokens(self):
        """检索指纹：同文本指纹一致，不同文本指纹不同，且不含明文。"""
        c = crypto.Cipher(os.urandom(32), "")
        t1 = c.index_tokens("计算定积分")
        t2 = c.index_tokens("计算定积分")
        t3 = c.index_tokens("完全不同的内容")
        self.assertEqual(t1, t2)
        self.assertFalse(set(t1) & set(t3))
        joined = " ".join(t1)
        self.assertNotIn("积分", joined)
        # 2-gram 与 3-gram 都要有：这样 2 字查询词也能命中
        self.assertEqual(len(c.index_tokens("积分", (2,))), 1)
        self.assertEqual(len(c.index_tokens("积分", (3,))), 1)  # 短文本退化为整串指纹
        self.assertEqual(len(c.index_tokens("定积分计算", (3,))), 3)  # 定积分/积分计/分计算

    def test_08_self_test_helper(self):
        self.assertTrue(crypto.self_test())


if __name__ == "__main__":
    unittest.main(verbosity=2)
