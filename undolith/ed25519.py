"""Pure-Python Ed25519 (RFC 8032), so signed ledgers need zero dependencies.

Adapted from the reference implementation in RFC 8032 section 6. It is not
constant-time: fine for signing an audit log on your own machine, not for
hostile multi-tenant environments where timing side channels matter.
"""

from __future__ import annotations

import hashlib

__all__ = ["public_key", "sign", "verify"]

_p = 2**255 - 19
_q = 2**252 + 27742317777372353535851937790883648493


def _inv(x: int) -> int:
    return pow(x, _p - 2, _p)


_d = -121665 * _inv(121666) % _p
_sqrt_m1 = pow(2, (_p - 1) // 4, _p)


def _sha512_modq(data: bytes) -> int:
    return int.from_bytes(hashlib.sha512(data).digest(), "little") % _q


def _add(P, Q):
    a = (P[1] - P[0]) * (Q[1] - Q[0]) % _p
    b = (P[1] + P[0]) * (Q[1] + Q[0]) % _p
    c = 2 * P[3] * Q[3] * _d % _p
    dd = 2 * P[2] * Q[2] % _p
    e, f, g, h = b - a, dd - c, dd + c, b + a
    return (e * f, g * h, f * g, e * h)


def _mul(s: int, P):
    Q = (0, 1, 1, 0)
    while s > 0:
        if s & 1:
            Q = _add(Q, P)
        P = _add(P, P)
        s >>= 1
    return Q


def _equal(P, Q) -> bool:
    if (P[0] * Q[2] - Q[0] * P[2]) % _p != 0:
        return False
    return (P[1] * Q[2] - Q[1] * P[2]) % _p == 0


def _recover_x(y: int, sign: int):
    if y >= _p:
        return None
    x2 = (y * y - 1) * _inv(_d * y * y + 1)
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (_p + 3) // 8, _p)
    if (x * x - x2) % _p != 0:
        x = x * _sqrt_m1 % _p
    if (x * x - x2) % _p != 0:
        return None
    if (x & 1) != sign:
        x = _p - x
    return x


_gy = 4 * _inv(5) % _p
_gx = _recover_x(_gy, 0)
_G = (_gx, _gy, 1, _gx * _gy % _p)


def _base_table():
    table, P = [], _G
    for _ in range(256):
        table.append(P)
        P = _add(P, P)
    return table


_G_TABLE = _base_table()  # G * 2^i: fixed-base multiplication needs additions only


def _mul_base(s: int):
    Q, i = (0, 1, 1, 0), 0
    while s > 0:
        if s & 1:
            Q = _add(Q, _G_TABLE[i])
        s >>= 1
        i += 1
    return Q


def _compress(P) -> bytes:
    zinv = _inv(P[2])
    x = P[0] * zinv % _p
    y = P[1] * zinv % _p
    return int.to_bytes(y | ((x & 1) << 255), 32, "little")


def _decompress(s: bytes):
    if len(s) != 32:
        return None
    y = int.from_bytes(s, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _recover_x(y, sign)
    if x is None:
        return None
    return (x, y, 1, x * y % _p)


def _expand(secret: bytes):
    if len(secret) != 32:
        raise ValueError("Ed25519 secret key must be 32 bytes")
    h = hashlib.sha512(secret).digest()
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return a, h[32:]


def public_key(secret: bytes) -> bytes:
    a, _ = _expand(secret)
    return _compress(_mul_base(a))


def sign(secret: bytes, msg: bytes, public: bytes | None = None) -> bytes:
    a, prefix = _expand(secret)
    A = public if public is not None else _compress(_mul_base(a))
    r = _sha512_modq(prefix + msg)
    Rs = _compress(_mul_base(r))
    h = _sha512_modq(Rs + A + msg)
    s = (r + h * a) % _q
    return Rs + int.to_bytes(s, 32, "little")


def verify(public: bytes, msg: bytes, signature: bytes) -> bool:
    if len(public) != 32 or len(signature) != 64:
        return False
    A = _decompress(public)
    if A is None:
        return False
    Rs = signature[:32]
    R = _decompress(Rs)
    if R is None:
        return False
    s = int.from_bytes(signature[32:], "little")
    if s >= _q:
        return False
    h = _sha512_modq(Rs + public + msg)
    return _equal(_mul_base(s), _add(R, _mul(h, A)))
