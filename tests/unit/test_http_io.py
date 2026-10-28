"""Exercise bounded reading against the standard library's actual HTTP decoder."""
from http.client import HTTPResponse
import io

import pytest

from core.http_io import ResponseTooLarge, read_response_bytes


class WireSocket:
    def __init__(self, wire):
        self.wire = wire

    def makefile(self, *_args):
        return io.BytesIO(self.wire)


def response(headers, payload):
    result = HTTPResponse(WireSocket(b"HTTP/1.1 200 OK\r\n" + headers + b"\r\n\r\n" + payload))
    result.begin()
    return result


@pytest.mark.parametrize("length", [b"0", b"5", b"100000"])
def test_complete_content_length_response(length):
    payload = b"x" * int(length)
    with response(b"Content-Length: " + length, payload) as stream:
        assert read_response_bytes(stream, maximum_bytes=100000) == payload


@pytest.mark.parametrize("payload", [b"", b"partial audio", b"x" * 70000])
def test_premature_eof_is_rejected_even_when_read1_returns_normally(payload):
    with response(b"Content-Length: 100000", payload) as stream:
        with pytest.raises(OSError, match=f"ended after {len(payload)} bytes; expected 100000"):
            read_response_bytes(stream, maximum_bytes=100000)


def test_chunk_framing_controls_body_length():
    with response(b"Transfer-Encoding: chunked\r\nContent-Length: 999999", b"3\r\nabc\r\n0\r\n\r\n") as stream:
        assert read_response_bytes(stream, maximum_bytes=10) == b"abc"


def test_body_without_content_length_can_end_normally():
    with response(b"Connection: close", b"audio data") as stream:
        assert read_response_bytes(stream, maximum_bytes=100) == b"audio data"


def test_chunked_body_still_obeys_actual_size_limit():
    with response(b"Transfer-Encoding: chunked", b"b\r\nabcdefghijk\r\n0\r\n\r\n") as stream:
        with pytest.raises(ResponseTooLarge):
            read_response_bytes(stream, maximum_bytes=10)
