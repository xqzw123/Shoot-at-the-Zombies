"""RSA-2048 / PKCS#1 v1.5 / SHA-256 签名，纯标准库实现。

客户端要跑在「什么环境都没有」的新电脑上，验签必须零第三方依赖；
作者端签发器复用同一模块，也不会被 pip 版本差异卡住。

这是标准算法的标准实现，不是自创密码学。密钥生成只在作者端执行一次，
运行时客户端只做 pow(sig, e, n) 验签。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets

E = 65537
TOKEN_PREFIX = "SZL1"
REQUEST_PREFIX = "SZREQ1"
_SHA256_DIGESTINFO = bytes.fromhex("3031300d060960864801650304020105000420")


def _small_primes(limit: int = 2000) -> list[int]:
    sieve = [True] * (limit + 1)
    sieve[0] = sieve[1] = False
    for i in range(2, int(limit ** 0.5) + 1):
        if sieve[i]:
            for j in range(i * i, limit + 1, i):
                sieve[j] = False
    return [i for i, ok in enumerate(sieve) if ok]


_SMALL = _small_primes()


def _is_probable_prime(n: int, rounds: int = 40) -> bool:
    if n < 2:
        return False
    for p in _SMALL:
        if n % p == 0:
            return n == p
    d = n - 1
    s = 0
    while d % 2 == 0:
        d //= 2
        s += 1
    for _ in range(rounds):
        a = secrets.randbelow(n - 3) + 2
        x = pow(a, d, n)
        if x == 1 or x == n - 1:
            continue
        for _ in range(s - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def _gen_prime(bits: int) -> int:
    while True:
        candidate = secrets.randbits(bits) | (1 << (bits - 1)) | 1
        if not _is_probable_prime(candidate, 8):
            continue
        if _is_probable_prime(candidate, 40):
            return candidate


def generate_keypair(bits: int = 2048) -> dict:
    """生成密钥对；返回十进制整数，序列化时写成十进制字符串。"""
    if bits < 2048:
        raise ValueError("出于安全考虑不接受 2048 位以下的密钥")
    while True:
        p = _gen_prime(bits // 2)
        q = _gen_prime(bits // 2)
        if p == q:
            continue
        n = p * q
        if n.bit_length() != bits:
            continue
        phi = (p - 1) * (q - 1)
        if phi % E == 0:
            continue
        return {"n": n, "e": E, "d": pow(E, -1, phi)}


def _i2osp(n: int, length: int) -> bytes:
    return n.to_bytes(length, "big")


def _encode(msg: bytes, k: int) -> bytes:
    t = _SHA256_DIGESTINFO + hashlib.sha256(msg).digest()
    if k < len(t) + 11:
        raise ValueError("密钥长度不足")
    return b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t


def sign(msg: bytes, private_key: dict) -> bytes:
    n = int(private_key["n"])
    d = int(private_key["d"])
    k = (n.bit_length() + 7) // 8
    m = int.from_bytes(_encode(msg, k), "big")
    return _i2osp(pow(m, d, n), k)


def verify(msg: bytes, signature: bytes, public_key: dict) -> bool:
    n = int(public_key["n"])
    e = int(public_key["e"])
    if n <= 0:
        return False
    k = (n.bit_length() + 7) // 8
    if len(signature) != k:
        return False
    s = int.from_bytes(signature, "big")
    if s >= n:
        return False
    try:
        expected = _encode(msg, k)
        actual = _i2osp(pow(s, e, n), k)
    except Exception:
        return False
    return hmac.compare_digest(expected, actual)


def b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def canonical(obj: dict) -> bytes:
    """稳定序列化：签发与验签必须用完全相同的字节。"""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def make_token(payload: dict, private_key: dict) -> str:
    body = canonical(payload)
    return "%s.%s.%s" % (TOKEN_PREFIX, b64e(body), b64e(sign(body, private_key)))


def parse_token(token: str) -> tuple[dict, bytes]:
    # 记事本另存的 UTF-8 BOM 会粘在 token 开头，去掉它而不是报「格式不正确」
    cleaned = "".join(token.split()).lstrip("\ufeff")
    parts = cleaned.split(".")
    if len(parts) != 3 or parts[0] != TOKEN_PREFIX:
        raise ValueError("许可证格式不正确（应为 SZL1.xxx.xxx）")
    body = b64d(parts[1])
    sig = b64d(parts[2])
    payload = json.loads(body.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("许可证内容不是对象")
    return payload, sig


def verify_token(token: str, public_key: dict) -> tuple[bool, dict | None, str]:
    """返回 (是否通过, payload, 原因)。只做签名校验，不做业务校验。"""
    try:
        payload, sig = parse_token(token)
    except Exception as exc:
        return False, None, "许可证解析失败：%s" % exc
    if not verify(canonical(payload), sig, public_key):
        return False, None, "签名无效：许可证被篡改或不是本产品签发的"
    return True, payload, "OK"


def split_lines(text: str, width: int = 76) -> str:
    """长密钥换行便于复制；解析时会去掉所有空白。"""
    return "\n".join(text[i:i + width] for i in range(0, len(text), width))


def public_fingerprint(public_key: dict) -> str:
    """公钥指纹，供作者核对客户端内置的公钥是否正确。"""
    return hashlib.sha256(str(int(public_key["n"])).encode("ascii")).hexdigest()[:16]
