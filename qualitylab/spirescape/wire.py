"""Primitive codecs for SpireScape's wire format.

The server is .NET and uses BinaryWriter/BinaryReader, so the rules are:
  - int32 and other numbers are little-endian
  - bool is one byte, and any non-zero byte reads as true
  - a string is its UTF-8 byte length as a 7-bit varint, then the bytes
  - a frame is [int32 length][byte message type][body], where length covers type + body

Each codec here has a read, a write and a default, so message layouts can be declared as data
(see messages.py) instead of hand-writing every encoder.
"""
from __future__ import annotations

import struct

MAX_FRAME = 256 * 1024  # Framing.MaxFrame on the server


class WireError(ValueError):
    """Bytes that don't decode: truncated, oversized, or an unknown message type."""


class Writer:
    def __init__(self) -> None:
        self.buf = bytearray()

    def i32(self, v: int) -> None:
        self.buf += struct.pack("<i", v)

    def u8(self, v: int) -> None:
        self.buf += struct.pack("<B", v)

    def bool(self, v: bool) -> None:
        self.buf.append(1 if v else 0)

    def varint(self, n: int) -> None:
        if n < 0:
            raise WireError("negative length")
        while n >= 0x80:
            self.buf.append((n & 0x7F) | 0x80)
            n >>= 7
        self.buf.append(n)

    def str(self, s: str) -> None:
        b = s.encode("utf-8")
        self.varint(len(b))
        self.buf += b

    def raw(self, b: bytes) -> None:
        self.buf += b


class Reader:
    def __init__(self, data: bytes, pos: int = 0) -> None:
        self.data = memoryview(data)
        self.pos = pos

    def _take(self, n: int) -> memoryview:
        if n < 0 or self.pos + n > len(self.data):
            raise WireError(f"truncated: wanted {n} bytes at offset {self.pos}, have {len(self.data) - self.pos}")
        out = self.data[self.pos:self.pos + n]
        self.pos += n
        return out

    def i32(self) -> int:
        return struct.unpack("<i", self._take(4))[0]

    def u8(self) -> int:
        return self._take(1)[0]

    def bool(self) -> bool:
        return self._take(1)[0] != 0

    def varint(self) -> int:
        n, shift = 0, 0
        while True:
            b = self.u8()
            n |= (b & 0x7F) << shift
            if not b & 0x80:
                return n
            shift += 7
            if shift > 28:  # .NET rejects more than five bytes
                raise WireError("bad 7-bit length")

    def str(self) -> str:
        n = self.varint()
        try:
            return bytes(self._take(n)).decode("utf-8")
        except UnicodeDecodeError as e:
            raise WireError(f"bad UTF-8: {e}") from e

    def remaining(self) -> int:
        return len(self.data) - self.pos


# ------------------------------------------------------------------------------------------------
# Codecs: objects with read(reader), write(writer, value) and default().
# ------------------------------------------------------------------------------------------------

class _Prim:
    def __init__(self, name: str, default) -> None:
        self.name = name
        self._default = default

    def read(self, r: Reader):
        return getattr(r, self.name)()

    def write(self, w: Writer, v) -> None:
        getattr(w, self.name)(v)

    def default(self):
        return self._default

    def __repr__(self) -> str:
        return self.name.upper()


I32 = _Prim("i32", 0)
U8 = _Prim("u8", 0)
BOOL = _Prim("bool", False)
STR = _Prim("str", "")


class _Bytes:
    """int32 length, then raw bytes (MapData.Tiles)."""

    def read(self, r: Reader) -> bytes:
        n = r.i32()
        return bytes(r._take(n))

    def write(self, w: Writer, v: bytes) -> None:
        w.i32(len(v))
        w.raw(v)

    def default(self) -> bytes:
        return b""

    def __repr__(self) -> str:
        return "BYTES"


BYTES = _Bytes()


class ListOf:
    """int32 count, then each item. Wire.WriteStrings/Ints/Bools and every counted list of structs."""

    def __init__(self, item) -> None:
        self.item = item

    def read(self, r: Reader) -> list:
        n = r.i32()
        if n < 0 or n > r.remaining():  # every item is at least one byte
            raise WireError(f"bad list count {n}")
        return [self.item.read(r) for _ in range(n)]

    def write(self, w: Writer, v: list) -> None:
        w.i32(len(v))
        for x in v:
            self.item.write(w, x)

    def default(self) -> list:
        return []

    def __repr__(self) -> str:
        return f"ListOf({self.item!r})"
