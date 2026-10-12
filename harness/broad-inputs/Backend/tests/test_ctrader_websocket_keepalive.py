import json
import struct

import ctrader_connector


class FakeSocket:
    def __init__(self, incoming=b""):
        self.incoming = bytearray(incoming)
        self.sent = []

    def recv(self, length):
        chunk = bytes(self.incoming[:length])
        del self.incoming[:length]
        return chunk

    def sendall(self, payload):
        self.sent.append(payload)


def server_frame(opcode, payload=b""):
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    assert len(payload) < 126
    return bytes([0x80 | opcode, len(payload)]) + payload


def decode_client_frame(frame):
    opcode = frame[0] & 0x0F
    assert frame[1] & 0x80
    length = frame[1] & 0x7F
    offset = 2
    if length == 126:
        length = struct.unpack("!H", frame[offset:offset + 2])[0]
        offset += 2
    mask = frame[offset:offset + 4]
    payload = frame[offset + 4:offset + 4 + length]
    decoded = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    return opcode, decoded


def test_ping_is_answered_with_matching_pong_before_text_is_returned():
    message = json.dumps({"payloadType": 2132})
    socket = FakeSocket(
        server_frame(0x9, b"ctrader-keepalive")
        + server_frame(0x1, message)
    )

    assert ctrader_connector.websocket_recv_text(socket) == message
    assert len(socket.sent) == 1
    assert decode_client_frame(socket.sent[0]) == (0xA, b"ctrader-keepalive")


def test_unsolicited_pong_is_ignored_before_text_is_returned():
    socket = FakeSocket(server_frame(0xA) + server_frame(0x1, "ready"))

    assert ctrader_connector.websocket_recv_text(socket) == "ready"
    assert socket.sent == []


def test_application_heartbeat_uses_ctrader_payload_type_51():
    socket = FakeSocket()

    ctrader_connector.send_ctrader_heartbeat(socket)

    assert len(socket.sent) == 1
    opcode, payload = decode_client_frame(socket.sent[0])
    message = json.loads(payload.decode("utf-8"))
    assert opcode == 0x1
    assert message["payloadType"] == 51
    assert message["payload"] == {}
    assert message["clientMsgId"]
