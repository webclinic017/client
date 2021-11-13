import socket
import struct
from typing import Any, Optional
from typing import TYPE_CHECKING

from wandb.proto import wandb_server_pb2 as spb


if TYPE_CHECKING:
    from wandb.proto import wandb_internal_pb2 as pb


class SockClient:
    _sock: socket.socket
    _data: bytes

    HEADLEN = 1 + 4

    def __init__(self) -> None:
        self._data = b""

    def connect(self, port: int) -> None:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.connect(("localhost", port))
        self._sock = s

    def set_socket(self, sock: socket.socket) -> None:
        self._sock = sock

    def _send_message(self, msg: Any) -> None:
        raw_size = msg.ByteSize()
        data = msg.SerializeToString()
        assert len(data) == raw_size, "invalid serialization"
        header = struct.pack("<BI", ord("W"), raw_size)
        self._sock.send(header + data)

    def send_server_request(self, msg: Any) -> None:
        self._send_message(msg)

    def send_server_response(self, msg: Any) -> None:
        print("SEND_S_RESP", msg)
        self._send_message(msg)

    def send(
        self,
        *,
        inform_init: spb.ServerInformInitRequest = None,
        inform_finish: spb.ServerInformFinishRequest = None,
        inform_teardown: spb.ServerInformTeardownRequest = None
    ) -> None:
        server_req = spb.ServerRequest()
        if inform_init:
            server_req.inform_init.CopyFrom(inform_init)
        elif inform_finish:
            server_req.inform_finish.CopyFrom(inform_finish)
        elif inform_teardown:
            server_req.inform_teardown.CopyFrom(inform_teardown)
        else:
            raise Exception("unmatched")
        self.send_server_request(server_req)

    def send_record_communicate(self, record: "pb.Record") -> None:
        server_req = spb.ServerRequest()
        server_req.record_communicate.CopyFrom(record)
        self.send_server_request(server_req)

    def send_record_publish(self, record: "pb.Record") -> None:
        server_req = spb.ServerRequest()
        server_req.record_publish.CopyFrom(record)
        self.send_server_request(server_req)

    def _extract_packet_bytes(self) -> Optional[bytes]:
        # Do we have enough data to read the header?
        len_data = len(self._data)
        start_offset = self.HEADLEN
        if len_data >= start_offset:
            header = self._data[:start_offset]
            fields = struct.unpack("<BI", header)
            magic, dlength = fields
            assert magic == ord("W")
            # Do we have enough data to read the full record?
            end_offset = self.HEADLEN + dlength
            if len_data >= end_offset:
                rec_data = self._data[start_offset:end_offset]
                self._data = self._data[end_offset:]
                return rec_data
        return None

    def _read_packet_bytes(self, timeout: int = None) -> Optional[bytes]:
        while True:
            rec = self._extract_packet_bytes()
            if rec:
                return rec

            if timeout:
                self._sock.settimeout(timeout)
            try:
                data = self._sock.recv(4096)
            except socket.timeout:
                break
            if timeout:
                self._sock.settimeout(None)
            self._data += data
        return None

    def read_server_request(self) -> spb.ServerRequest:
        data = self._read_packet_bytes()
        assert data
        rec = spb.ServerRequest()
        rec.ParseFromString(data)
        return rec

    def read_server_response(self, timeout: int = None) -> Optional[spb.ServerResponse]:
        data = self._read_packet_bytes(timeout=timeout)
        if not data:
            return None
        rec = spb.ServerResponse()
        rec.ParseFromString(data)
        return rec