"""AES-128 ECB 복호화 — 한글 배포용 문서의 본문(ViewText)을 읽는 데만 쓴다.

폐쇄망 반입 목록을 늘리지 않으려고 표준 라이브러리만으로 구현했다. 암호화는
만들지 않는다 — 이 프로그램은 원본을 읽기만 한다.
"""

from __future__ import annotations

BLOCK = 16


def _tables():
    # GF(2^8) 곱셈표. 생성원 3으로 지수·로그 표를 만든다.
    exp, log = [0] * 510, [0] * 256
    value = 1
    for index in range(255):
        exp[index] = value
        log[value] = index
        value ^= (value << 1) ^ (0x11B if value & 0x80 else 0)
        value &= 0xFF
    for index in range(255, 510):
        exp[index] = exp[index - 255]

    def mul(a: int, b: int) -> int:
        return exp[log[a] + log[b]] if a and b else 0

    sbox = [0] * 256
    for byte in range(256):
        inverse = exp[255 - log[byte]] if byte else 0
        out = inverse
        for shift in range(1, 5):
            out ^= ((inverse << shift) | (inverse >> (8 - shift))) & 0xFF
        sbox[byte] = out ^ 0x63
    inv_sbox = [0] * 256
    for byte, out in enumerate(sbox):
        inv_sbox[out] = byte
    return sbox, inv_sbox, tuple([mul(i, k) for i in range(256)] for k in (9, 11, 13, 14))


_SBOX, _INV_SBOX, (_M9, _M11, _M13, _M14) = _tables()
# InvShiftRows: 열 우선 배치에서 r행을 오른쪽으로 r칸 민다.
_INV_SHIFT = [(r + 4 * ((c - r) % 4)) for c in range(4) for r in range(4)]


def _expand(key: bytes) -> list[list[int]]:
    words = [list(key[i:i + 4]) for i in range(0, 16, 4)]
    rcon = 1
    for index in range(4, 44):
        word = list(words[index - 1])
        if index % 4 == 0:
            word = [_SBOX[b] for b in word[1:] + word[:1]]
            word[0] ^= rcon
            rcon = ((rcon << 1) ^ (0x11B if rcon & 0x80 else 0)) & 0xFF
        words.append([a ^ b for a, b in zip(words[index - 4], word)])
    return [sum(words[r * 4:r * 4 + 4], []) for r in range(11)]


def decrypt_ecb(key: bytes, data: bytes) -> bytes:
    """16바이트 단위로 푼다. 남는 꼬리는 버린다."""
    if len(key) != BLOCK:
        raise ValueError("AES-128 키는 16바이트여야 합니다")
    keys = _expand(key)
    out = bytearray(len(data) - len(data) % BLOCK)
    inv_sbox, shift = _INV_SBOX, _INV_SHIFT
    m9, m11, m13, m14 = _M9, _M11, _M13, _M14
    for start in range(0, len(out), BLOCK):
        state = [b ^ k for b, k in zip(data[start:start + BLOCK], keys[10])]
        for round_key in keys[9:0:-1]:
            state = [inv_sbox[state[i]] ^ k for i, k in zip(shift, round_key)]
            mixed = []
            for c in (0, 4, 8, 12):
                a, b, d, e = state[c:c + 4]
                mixed += (m14[a] ^ m11[b] ^ m13[d] ^ m9[e],
                          m9[a] ^ m14[b] ^ m11[d] ^ m13[e],
                          m13[a] ^ m9[b] ^ m14[d] ^ m11[e],
                          m11[a] ^ m13[b] ^ m9[d] ^ m14[e])
            state = mixed
        out[start:start + BLOCK] = bytes(inv_sbox[state[i]] ^ k for i, k in zip(shift, keys[0]))
    return bytes(out)
