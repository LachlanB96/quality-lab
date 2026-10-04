"""The wire codec on its own: known answers from .NET's BinaryWriter, and malformed input."""
import pytest
from hypothesis import given
from hypothesis import strategies as st

from qualitylab.spirescape.wire import BOOL, I32, STR, ListOf, Reader, WireError, Writer

pytestmark = [pytest.mark.unit, pytest.mark.area("protocol")]


@pytest.mark.parametrize("n, encoded", [
    (0, b"\x00"),
    (1, b"\x01"),
    (127, b"\x7f"),
    (128, b"\x80\x01"),
    (300, b"\xac\x02"),
    (16384, b"\x80\x80\x01"),
])
def test_varint_matches_dotnet_7bit_encoding(n, encoded):
    """Lengths use .NET's 7-bit encoding: low bits first, high bit means 'more'."""
    w = Writer()
    w.varint(n)
    assert bytes(w.buf) == encoded
    assert Reader(encoded).varint() == n


def test_string_is_utf8_byte_length_not_character_count():
    """'é' is one character but two bytes, and the prefix counts bytes."""
    w = Writer()
    w.str("é")
    assert bytes(w.buf) == b"\x02\xc3\xa9"


def test_int32_is_little_endian():
    w = Writer()
    w.i32(1)
    w.i32(-2)
    assert bytes(w.buf) == b"\x01\x00\x00\x00\xfe\xff\xff\xff"


def test_any_nonzero_byte_reads_as_true():
    """BinaryReader.ReadBoolean treats every non-zero byte as true."""
    assert [BOOL.read(Reader(bytes([b]))) for b in (0, 1, 2, 255)] == [False, True, True, True]


@given(st.text())
def test_strings_round_trip(s):
    w = Writer()
    STR.write(w, s)
    r = Reader(bytes(w.buf))
    assert STR.read(r) == s
    assert r.remaining() == 0


@given(st.lists(st.integers(min_value=-2**31, max_value=2**31 - 1)))
def test_int_lists_round_trip(xs):
    w = Writer()
    ListOf(I32).write(w, xs)
    assert ListOf(I32).read(Reader(bytes(w.buf))) == xs


@pytest.mark.parametrize("data, why", [
    (b"\x01\x00", "int32 needs four bytes"),
    (b"\x05ab", "string shorter than its prefix"),
    (b"\xff\xff\xff\xff\xff\xff", "a 7-bit length longer than five bytes"),
    (b"\xff\xff\xff\x7f", "a list count larger than the bytes left"),
    (b"\x02\xff\xfe", "invalid UTF-8"),
])
def test_malformed_input_raises_wire_error(data, why):
    readers = {
        "int32 needs four bytes": I32.read,
        "string shorter than its prefix": STR.read,
        "a 7-bit length longer than five bytes": Reader.varint,
        "a list count larger than the bytes left": ListOf(I32).read,
        "invalid UTF-8": STR.read,
    }
    with pytest.raises(WireError):
        readers[why](Reader(data))
