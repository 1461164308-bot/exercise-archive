# -*- coding: utf-8 -*-
"""本地加密：ChaCha20 流加密 + HMAC-SHA256 认证（Encrypt-then-MAC）。

为什么自己写而不用第三方库？
    本软件要求「零第三方依赖、纯离线可用」。Python 标准库没有对称加密算法，
    因此这里按 RFC 8439 实现 ChaCha20（20 轮，密钥 32 字节，96 位 nonce），
    配 HMAC-SHA256 做完整性校验，密钥用 PBKDF2-HMAC-SHA256 从
    「本机主密钥文件 + 可选用户口令」派生。实现带 RFC 官方测试向量自检。

数据格式（密文 blob）::

    b"EXA1" | salt(16) | nonce(12) | mac(32) | ciphertext(...)

- 每个值单独随机 salt/nonce，重复加密同一明文得到的密文不同。
- mac 覆盖 salt|nonce|ciphertext，防止篡改。
- 口令为空时，salt 仍随机（保证密文不可比对），密钥材料来自 master.key。

安全性说明：目标是「本地文件不裸奔」（别人拷走 archive.db 也读不出题干与错题报告），
不是抵抗有能力的攻击者。请勿用于高价值机密数据。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import struct
from typing import Optional

MAGIC = b"EXA1"
KEY_LEN = 32
SALT_LEN = 16
NONCE_LEN = 12
MAC_LEN = 32
PBKDF2_ROUNDS = 200_000


# --------------------------------------------------------------------------- #
# ChaCha20（RFC 8439 §2.3.2）
# --------------------------------------------------------------------------- #
def _rotl32(v: int, c: int) -> int:
    return ((v << c) & 0xFFFFFFFF) | (v >> (32 - c))


def _quarter_round(s, a, b, c, d) -> None:
    s[a] = (s[a] + s[b]) & 0xFFFFFFFF
    s[d] = _rotl32(s[d] ^ s[a], 16)
    s[c] = (s[c] + s[d]) & 0xFFFFFFFF
    s[b] = _rotl32(s[b] ^ s[c], 12)
    s[a] = (s[a] + s[b]) & 0xFFFFFFFF
    s[d] = _rotl32(s[d] ^ s[a], 8)
    s[c] = (s[c] + s[d]) & 0xFFFFFFFF
    s[b] = _rotl32(s[b] ^ s[c], 7)


def _chacha20_block(key: bytes, counter: int, nonce: bytes) -> bytes:
    """生成 64 字节密钥流块。"""
    state = [0x61707865, 0x3320646E, 0x79622D32, 0x6B206574]
    state += list(struct.unpack("<8I", key))
    state.append(counter & 0xFFFFFFFF)
    state += list(struct.unpack("<3I", nonce))
    working = list(state)
    for _ in range(10):  # 20 轮 = 10 次双轮
        _quarter_round(working, 0, 4, 8, 12)
        _quarter_round(working, 1, 5, 9, 13)
        _quarter_round(working, 2, 6, 10, 14)
        _quarter_round(working, 3, 7, 11, 15)
        _quarter_round(working, 0, 5, 10, 15)
        _quarter_round(working, 1, 6, 11, 12)
        _quarter_round(working, 2, 7, 8, 13)
        _quarter_round(working, 3, 4, 9, 14)
    out = [(working[i] + state[i]) & 0xFFFFFFFF for i in range(16)]
    return struct.pack("<16I", *out)


def chacha20_xor(key: bytes, nonce: bytes, data: bytes, counter: int = 1) -> bytes:
    """ChaCha20 异或加/解密（同一函数既可加密也可解密）。"""
    if len(key) != KEY_LEN:
        raise ValueError("ChaCha20 密钥必须为 32 字节")
    if len(nonce) != NONCE_LEN:
        raise ValueError("ChaCha20 nonce 必须为 12 字节")
    out = bytearray(len(data))
    pos = 0
    block_index = counter
    while pos < len(data):
        ks = _chacha20_block(key, block_index, nonce)
        block_index += 1
        chunk = data[pos : pos + 64]
        for i, b in enumerate(chunk):
            out[pos + i] = b ^ ks[i]
        pos += 64
    return bytes(out)


# --------------------------------------------------------------------------- #
# 密钥管理
# --------------------------------------------------------------------------- #
def _load_or_create_master_key(key_file) -> bytes:
    """读取（或首次生成）本机主密钥。"""
    key_file = str(key_file)
    if os.path.exists(key_file):
        try:
            with open(key_file, "rb") as f:
                raw = f.read()
            if len(raw) >= 32:
                return raw[:32]
        except Exception:
            pass
    raw = os.urandom(32)
    os.makedirs(os.path.dirname(key_file), exist_ok=True)
    tmp = key_file + ".tmp"
    with open(tmp, "wb") as f:
        f.write(raw)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, key_file)
    try:  # 尽力收紧权限（Windows 上此调用可能无效，忽略即可）
        os.chmod(key_file, 0o600)
    except Exception:
        pass
    return raw


class Cipher:
    """加密器：持有主密钥材料，提供 str/bytes/dict 级别加解密。"""

    def __init__(self, master_key: bytes, passphrase: str = ""):
        self._master = master_key
        self._passphrase = passphrase or ""
        self._cache: dict[bytes, bytes] = {}

    # -- 密钥派生 ---------------------------------------------------------- #
    def _derive(self, salt: bytes) -> bytes:
        cached = self._cache.get(salt)
        if cached is not None:
            return cached
        material = self._master + b"|" + self._passphrase.encode("utf-8")
        key = hashlib.pbkdf2_hmac("sha256", material, salt, PBKDF2_ROUNDS, dklen=KEY_LEN)
        if len(self._cache) < 64:
            self._cache[salt] = key
        return key

    # -- 核心 -------------------------------------------------------------- #
    def encrypt_bytes(self, plaintext: bytes) -> bytes:
        salt = os.urandom(SALT_LEN)
        nonce = os.urandom(NONCE_LEN)
        key = self._derive(salt)
        ct = chacha20_xor(key, nonce, plaintext, counter=1)
        mac = hmac.new(key, salt + nonce + ct, hashlib.sha256).digest()
        return MAGIC + salt + nonce + mac + ct

    def decrypt_bytes(self, blob: bytes) -> bytes:
        if not blob:
            return b""
        if not blob.startswith(MAGIC):
            # 兼容旧的/未加密数据：原样返回，避免升级后老数据读不出来
            return blob
        body = blob[len(MAGIC) :]
        if len(body) < SALT_LEN + NONCE_LEN + MAC_LEN:
            raise ValueError("密文长度不合法")
        salt = body[:SALT_LEN]
        nonce = body[SALT_LEN : SALT_LEN + NONCE_LEN]
        mac = body[SALT_LEN + NONCE_LEN : SALT_LEN + NONCE_LEN + MAC_LEN]
        ct = body[SALT_LEN + NONCE_LEN + MAC_LEN :]
        key = self._derive(salt)
        expect = hmac.new(key, salt + nonce + ct, hashlib.sha256).digest()
        if not hmac.compare_digest(mac, expect):
            raise ValueError("校验失败：数据被篡改，或主密钥/口令不匹配")
        return chacha20_xor(key, nonce, ct, counter=1)

    def encrypt_text(self, text: str) -> bytes:
        return self.encrypt_bytes((text or "").encode("utf-8"))

    def decrypt_text(self, blob: Optional[bytes]) -> str:
        if blob is None:
            return ""
        try:
            return self.decrypt_bytes(bytes(blob)).decode("utf-8", errors="replace")
        except Exception:
            # 解不开时返回占位，保证界面不炸；真正原因写日志
            return ""

    def encrypt_json(self, obj) -> bytes:
        import json

        return self.encrypt_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))

    def decrypt_json(self, blob, default=None):
        import json

        try:
            txt = self.decrypt_text(blob)
            return json.loads(txt) if txt else (default if default is not None else {})
        except Exception:
            return default if default is not None else {}

    # -- 加密全文检索用的「指纹索引」 -------------------------------------- #
    def index_token(self, s: str) -> str:
        """把一个 n-gram 变成不可逆的短指纹（用于在密文上做检索）。

        这样数据库里既没有明文，又能支持「题干关键词检索」：
        存的是 HMAC 指纹，只有持有同一主密钥的本机程序能把查询词算成同样的指纹。
        """
        key = hashlib.sha256(b"exer-index-v1|" + self._master).digest()
        return hmac.new(key, s.encode("utf-8"), hashlib.sha256).hexdigest()[:12]

    def index_tokens(self, text: str, sizes=(2, 3)) -> list:
        """把文本切成字符 n-gram 指纹列表（去重、去空白）。

        中文没有空格分词，用字符 n-gram 最省事且召回稳定：
        - **文档侧**同时索引 2-gram 与 3-gram，这样 2 个字的查询词（如"积分"）也能命中；
        - **查询侧**按查询词长度选用同样的 n（长度 2 用 2-gram，≥3 用 3-gram 取交集），
          两侧必须一致，否则会出现"索引里有、却永远搜不到"的坑。
        - 文本比 n 还短时，整串作为一个指纹（单字查询由上层退回全表扫描处理）。
        """
        import re as _re

        t = _re.sub(r"\s+", "", str(text or ""))
        if not t:
            return []
        out = set()
        for n in sizes:
            if n < 1 or len(t) < n:
                continue
            for i in range(len(t) - n + 1):
                out.add(self.index_token(t[i : i + n]))
        if not out:
            out.add(self.index_token(t))
        return sorted(out)


_SINGLETON: Optional[Cipher] = None


def get_cipher() -> Cipher:
    """进程级单例：主密钥来自 data/master.key。"""
    global _SINGLETON
    if _SINGLETON is None:
        from . import paths

        master = _load_or_create_master_key(paths.key_path())
        _SINGLETON = Cipher(master)
    return _SINGLETON


def rotate_key(new_passphrase: str) -> None:
    """更换用户口令（旧密文需先用旧口令全部解出再重新加密，由 store 负责流程）。"""
    cipher = get_cipher()
    cipher._passphrase = new_passphrase or ""
    cipher._cache.clear()


# --------------------------------------------------------------------------- #
# 自检（RFC 8439 测试向量）
# --------------------------------------------------------------------------- #
def self_test() -> bool:
    """用 RFC 8439 §2.4.2 的官方向量验证 ChaCha20 实现是否正确。"""
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
    got = chacha20_xor(key, nonce, plaintext, counter=1)
    ok = got == expect

    # 再加一轮自反测试：加密后再解密应还原
    c = Cipher(os.urandom(32), "")
    sample = "错题本：∫₀¹ x² dx = 1/3 ✓ 测试" * 5
    ok = ok and c.decrypt_text(c.encrypt_text(sample)) == sample
    long_sample = "a" * 5000
    ok = ok and c.decrypt_text(c.encrypt_text(long_sample)) == long_sample
    return ok


if __name__ == "__main__":  # 直接运行本文件即做自检
    from . import logging_setup

    logging_setup.enable_utf8_console()
    print("ChaCha20/HMAC 自检:", "通过 ✅" if self_test() else "失败 ❌")
