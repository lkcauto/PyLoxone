"""Unit tests for pyloxone_api/message.py"""
import math
import struct
import uuid

import pytest

from ..exceptions import LoxoneException
from ..message import (
    BinaryFile,
    DaytimerStatesTable,
    Keepalive,
    LLResponse,
    MessageHeader,
    MessageType,
    TextMessage,
    TextStatesTable,
    ValueStatesTable,
    WeatherStatesTable,
    check_and_decode_if_needed,
    clean_up_control,
    parse_message,
)


# ---------------------------------------------------------------------------
# check_and_decode_if_needed
# ---------------------------------------------------------------------------

def test_decode_str_passthrough():
    assert check_and_decode_if_needed("hello") == "hello"


def test_decode_utf8_bytes():
    assert check_and_decode_if_needed(b"hello") == "hello"


def test_decode_bytearray():
    assert check_and_decode_if_needed(bytearray(b"hello")) == "hello"


def test_decode_empty_bytes():
    assert check_and_decode_if_needed(b"") == ""


def test_decode_latin1_bytes():
    b = "caf\xe9".encode("latin-1")  # valid latin-1 but not pure utf-8
    result = check_and_decode_if_needed(b)
    assert isinstance(result, str)
    assert len(result) > 0


def test_decode_utf8_multibyte():
    text = "éàü"  # é à ü
    assert check_and_decode_if_needed(text.encode("utf-8")) == text


# ---------------------------------------------------------------------------
# LLResponse
# ---------------------------------------------------------------------------

_LL_UPPER = '{"LL": {"control": "dev/sps/io/Test", "value": "1", "Code": "200"}}'
_LL_LOWER = '{"LL": {"control": "dev/sps/io/Test", "value": "0", "code": "404"}}'
_LL_DICT_VALUE = '{"LL": {"control": "dev/sps/io/Test", "value": {"key": "val"}, "Code": "200"}}'


def test_ll_response_code_upper():
    r = LLResponse(_LL_UPPER)
    assert r.code == 200
    assert r.control == "dev/sps/io/Test"
    assert r.value == "1"


def test_ll_response_code_lower():
    r = LLResponse(_LL_LOWER)
    assert r.code == 404
    assert r.value == "0"


def test_ll_response_bytes_input():
    r = LLResponse(_LL_UPPER.encode())
    assert r.code == 200


def test_ll_response_value_as_dict_scalar():
    r = LLResponse(_LL_UPPER)
    d = r.value_as_dict
    assert d["value"] == "1"


def test_ll_response_value_as_dict_nested():
    r = LLResponse(_LL_DICT_VALUE)
    d = r.value_as_dict
    assert d["key"] == "val"
    assert "value" in d


def test_ll_response_invalid_json():
    with pytest.raises(ValueError):
        LLResponse("not json")


def test_ll_response_missing_ll_key():
    with pytest.raises(ValueError):
        LLResponse('{"no_ll": {}}')


def test_ll_response_missing_control():
    with pytest.raises(ValueError):
        LLResponse('{"LL": {"value": "1", "Code": "200"}}')


# ---------------------------------------------------------------------------
# MessageHeader
# ---------------------------------------------------------------------------

def _make_header(msg_type: int, estimated: bool = False, payload_length: int = 0) -> bytes:
    info = b"\x80" if estimated else b"\x00"
    return struct.pack("<cBccI", b"\x03", msg_type, info, b"\x00", payload_length)


def test_message_header_text():
    h = MessageHeader(_make_header(MessageType.TEXT, payload_length=42))
    assert h.message_type == MessageType.TEXT
    assert h.payload_length == 42
    assert not h.estimated


def test_message_header_value_states():
    h = MessageHeader(_make_header(MessageType.VALUE_STATES, payload_length=48))
    assert h.message_type == MessageType.VALUE_STATES
    assert h.payload_length == 48


def test_message_header_keepalive():
    h = MessageHeader(_make_header(MessageType.KEEPALIVE, payload_length=9))
    assert h.message_type == MessageType.KEEPALIVE


def test_message_header_estimated_true():
    h = MessageHeader(_make_header(MessageType.BINARY, estimated=True, payload_length=10))
    assert h.estimated is True


def test_message_header_estimated_false():
    h = MessageHeader(_make_header(MessageType.BINARY, estimated=False, payload_length=10))
    assert h.estimated is False


def test_message_header_wrong_first_byte():
    bad = struct.pack("<cBccI", b"\x00", 0, b"\x00", b"\x00", 0)
    h = MessageHeader(bad)
    assert h.message_type == MessageType.UNKNOWN


def test_message_header_too_short():
    with pytest.raises(LoxoneException):
        MessageHeader(b"\x03\x00")  # too short to unpack


def test_message_header_all_types():
    for t in [
        MessageType.TEXT,
        MessageType.BINARY,
        MessageType.VALUE_STATES,
        MessageType.TEXT_STATES,
        MessageType.DAYTIMER_STATES,
        MessageType.KEEPALIVE,
        MessageType.WEATHER_STATES,
    ]:
        h = MessageHeader(_make_header(t))
        assert h.message_type == t


# ---------------------------------------------------------------------------
# ValueStatesTable
# ---------------------------------------------------------------------------

def _pack_value_event(uid: uuid.UUID, value: float) -> bytes:
    return uid.bytes_le + struct.pack("d", value)


def test_value_states_empty():
    assert ValueStatesTable(b"").as_dict() == {}


def test_value_states_single():
    uid = uuid.UUID("12345678-1234-5678-1234-567812345678")
    vst = ValueStatesTable(_pack_value_event(uid, 42.5))
    d = vst.as_dict()
    assert len(d) == 1
    assert list(d.values())[0] == pytest.approx(42.5)


def test_value_states_multiple():
    uid1 = uuid.UUID("12345678-1234-5678-1234-567812345678")
    uid2 = uuid.UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
    packet = _pack_value_event(uid1, 1.0) + _pack_value_event(uid2, 2.0)
    d = ValueStatesTable(packet).as_dict()
    assert len(d) == 2
    assert pytest.approx(1.0) in d.values()
    assert pytest.approx(2.0) in d.values()


def test_value_states_zero():
    uid = uuid.UUID("12345678-1234-5678-1234-567812345678")
    d = ValueStatesTable(_pack_value_event(uid, 0.0)).as_dict()
    assert list(d.values())[0] == pytest.approx(0.0)


def test_value_states_uuid_key_format():
    uid = uuid.UUID("12345678-1234-5678-1234-567812345678")
    d = ValueStatesTable(_pack_value_event(uid, 0.0)).as_dict()
    key = list(d.keys())[0]
    # Format is f0-f1-f2-f3f4 (3 dashes, 4 segments)
    assert len(key.split("-")) == 4


# ---------------------------------------------------------------------------
# TextStatesTable
# ---------------------------------------------------------------------------

def _pack_text_event(uid: uuid.UUID, icon_uid: uuid.UUID, text: bytes) -> bytes:
    text_length = len(text)
    record = uid.bytes_le + icon_uid.bytes_le + struct.pack("<I", text_length) + text
    advance = (math.floor((4 + text_length + 16 + 16 - 1) / 4) + 1) * 4
    padding = advance - len(record)
    return record + b"\x00" * padding


_ICON = uuid.UUID("ffffffff-ffff-4fff-bfff-ffffffffffff")


def test_text_states_single():
    uid = uuid.UUID("12345678-1234-5678-1234-567812345678")
    data = _pack_text_event(uid, _ICON, b"on")
    d = TextStatesTable(data).as_dict()
    assert len(d) == 1
    assert "on" in d.values()


def test_text_states_multiple():
    uid1 = uuid.UUID("12345678-1234-5678-1234-567812345678")
    uid2 = uuid.UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
    data = _pack_text_event(uid1, _ICON, b"on") + _pack_text_event(uid2, _ICON, b"off")
    d = TextStatesTable(data).as_dict()
    assert len(d) == 2
    assert "on" in d.values()
    assert "off" in d.values()


def test_text_states_empty_text():
    uid = uuid.UUID("12345678-1234-5678-1234-567812345678")
    data = _pack_text_event(uid, _ICON, b"")
    d = TextStatesTable(data).as_dict()
    assert len(d) == 1
    assert "" in d.values()


def test_text_states_requires_bytes():
    with pytest.raises(LoxoneException):
        TextStatesTable("not bytes").as_dict()


def test_text_states_longer_text():
    uid = uuid.UUID("12345678-1234-5678-1234-567812345678")
    text = b"A longer state value string"
    data = _pack_text_event(uid, _ICON, text)
    d = TextStatesTable(data).as_dict()
    assert list(d.values())[0] == "A longer state value string"


# ---------------------------------------------------------------------------
# Keepalive
# ---------------------------------------------------------------------------

def test_keepalive_as_dict():
    assert Keepalive(b"keepalive").as_dict() == {"keep_alive": "received"}


# ---------------------------------------------------------------------------
# TextMessage
# ---------------------------------------------------------------------------

def test_text_message_as_dict():
    tm = TextMessage(_LL_UPPER)
    d = tm.as_dict()
    assert d["value"] == "1"
    assert d["Code"] == 200
    assert "control" in d


def test_text_message_control_no_salt():
    tm = TextMessage(_LL_UPPER)
    assert tm.control == "dev/sps/io/Test"


def test_text_message_salt_stripped_in_as_dict():
    msg = '{"LL": {"control": "salt/DEADBEEF/dev/sps/io/Test", "value": "1", "Code": "200"}}'
    tm = TextMessage(msg)
    d = tm.as_dict()
    assert not d["control"].startswith("salt/")


# ---------------------------------------------------------------------------
# BinaryFile
# ---------------------------------------------------------------------------

def test_binary_file_as_dict():
    assert BinaryFile(b"\x00\x01\x02").as_dict() == {}


# ---------------------------------------------------------------------------
# DaytimerStatesTable / WeatherStatesTable
# ---------------------------------------------------------------------------

def test_daytimer_states_as_dict():
    assert DaytimerStatesTable(b"").as_dict() == {}


def test_weather_states_as_dict():
    assert WeatherStatesTable(b"").as_dict() == {}


# ---------------------------------------------------------------------------
# parse_message
# ---------------------------------------------------------------------------

def test_parse_message_text():
    result = parse_message(_LL_UPPER, MessageType.TEXT)
    assert isinstance(result, TextMessage)
    assert result.code == 200


def test_parse_message_text_invalid_raises():
    with pytest.raises((ValueError, Exception)):
        parse_message(b"not a valid ll response", MessageType.TEXT)


def test_parse_message_keepalive():
    assert isinstance(parse_message(b"keepalive", MessageType.KEEPALIVE), Keepalive)


def test_parse_message_binary():
    assert isinstance(parse_message(b"\x00\x01", MessageType.BINARY), BinaryFile)


def test_parse_message_value_states():
    assert isinstance(parse_message(b"", MessageType.VALUE_STATES), ValueStatesTable)


def test_parse_message_text_states():
    assert isinstance(parse_message(b"", MessageType.TEXT_STATES), TextStatesTable)


def test_parse_message_daytimer_states():
    assert isinstance(parse_message(b"", MessageType.DAYTIMER_STATES), DaytimerStatesTable)


def test_parse_message_weather_states():
    assert isinstance(parse_message(b"", MessageType.WEATHER_STATES), WeatherStatesTable)


def test_parse_message_unknown_type_raises():
    with pytest.raises(LoxoneException):
        parse_message(b"", 99)


# ---------------------------------------------------------------------------
# clean_up_control
# ---------------------------------------------------------------------------

def test_clean_up_control_no_salt():
    assert clean_up_control("dev/sps/io/Test") == "dev/sps/io/Test"


def test_clean_up_control_with_salt():
    result = clean_up_control("salt/abc123/dev/sps/io/Test")
    assert result == "/dev/sps/io/Test"


def test_clean_up_control_salt_uppercase_hex():
    result = clean_up_control("salt/DEADBEEF/some/control")
    assert result == "/some/control"


def test_clean_up_control_bytes_input():
    result = clean_up_control(b"dev/sps/io/Test")
    assert isinstance(result, str)
    assert "dev/sps/io/Test" in result


def test_clean_up_control_empty_string():
    assert clean_up_control("") == ""
