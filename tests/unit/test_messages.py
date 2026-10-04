"""Every message type round-trips, built from Hypothesis strategies derived from the layouts themselves."""
import pytest
from hypothesis import given
from hypothesis import strategies as st

from qualitylab.spirescape import messages as m
from qualitylab.spirescape import wire

pytestmark = [pytest.mark.unit, pytest.mark.area("protocol")]

ALL_MESSAGES = sorted(m.REGISTRY.values(), key=lambda c: c.type_id)


def strategy_for(codec):
    """A Hypothesis strategy for any codec in messages.py, so new messages get tested for free."""
    if codec is wire.I32:
        return st.integers(min_value=-2**31, max_value=2**31 - 1)
    if codec is wire.U8:
        return st.integers(min_value=0, max_value=255)
    if codec is wire.BOOL:
        return st.booleans()
    if codec is wire.STR:
        return st.text(max_size=40)
    if codec is wire.BYTES:
        return st.binary(max_size=64)
    if isinstance(codec, wire.ListOf):
        return st.lists(strategy_for(codec.item), max_size=5)
    if isinstance(codec, type) and issubclass(codec, m.Struct):
        return st.builds(codec, **{name: strategy_for(c) for name, c in codec.layout})
    raise TypeError(f"no strategy for {codec!r}")


def test_registry_covers_the_protocol():
    """9 client messages (1-9) and 18 server messages (50-67), as in Messages.cs."""
    assert sorted(c.type_id for c in m.CLIENT_MESSAGES) == list(range(1, 10))
    assert sorted(c.type_id for c in m.SERVER_MESSAGES) == list(range(50, 68))


@pytest.mark.parametrize("cls", ALL_MESSAGES, ids=lambda c: c.__name__)
def test_default_message_round_trips(cls):
    msg = cls()
    assert m.decode(m.encode(msg)) == msg


@pytest.mark.parametrize("cls", ALL_MESSAGES, ids=lambda c: c.__name__)
@given(data=st.data())
def test_generated_message_round_trips(cls, data):
    msg = data.draw(strategy_for(cls))
    assert m.decode(m.encode(msg)) == msg


def test_hello_known_answer():
    """Bytes worked out by hand from BinaryWriter's rules: length 9, type 1, "Ann", version 1."""
    assert m.encode(m.Hello(name="Ann")) == bytes.fromhex("09000000" "01" "03416e6e" "01000000")


def test_decode_rejects_a_lying_length_prefix():
    frame = bytearray(m.encode(m.Chat(text="hi")))
    frame[0] += 1
    with pytest.raises(wire.WireError, match="length prefix"):
        m.decode(bytes(frame))


def test_decode_rejects_unknown_type():
    with pytest.raises(wire.WireError, match="unknown message type"):
        m.decode(b"\x01\x00\x00\x00\xc8")


def test_decode_rejects_trailing_bytes():
    frame = m.encode(m.MoveTo(x=1, y=2)) + b"\x00"
    frame = (len(frame) - 4).to_bytes(4, "little") + frame[4:]
    with pytest.raises(wire.WireError, match="trailing"):
        m.decode(frame)


def test_unknown_field_is_a_type_error():
    """A typo in a test (Chat(txt=...)) fails loudly instead of sending an empty string."""
    with pytest.raises(TypeError):
        m.Chat(txt="hello")
