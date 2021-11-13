import queue
import socket
import threading
import time
from typing import Any, Callable
from typing import TYPE_CHECKING

from wandb.proto import wandb_server_pb2 as spb

from .streams import _dict_from_pbmap
from .streams import StreamMux
from ..lib.sock_client import SockClient


if TYPE_CHECKING:
    from ..interface.interface_relay import InterfaceRelay


class SockServerInterfaceReaderThread(threading.Thread):
    _socket_client: SockClient

    def __init__(self, sock_client: SockClient, iface: "InterfaceRelay") -> None:
        self._iface = iface
        self._sock_client = sock_client
        threading.Thread.__init__(self)
        self.name = "SockSrvIntRdThr"

    def run(self) -> None:
        assert self._iface.relay_q
        while True:
            try:
                result = self._iface.relay_q.get(timeout=1)
            except queue.Empty:
                continue
            sresp = spb.ServerResponse()
            sresp.result_communicate.CopyFrom(result)
            self._sock_client.send_server_response(sresp)


class SockServerReadThread(threading.Thread):
    _sock_client: SockClient
    _mux: StreamMux

    def __init__(self, conn: socket.socket, mux: StreamMux) -> None:
        self._mux = mux
        threading.Thread.__init__(self)
        self.name = "SockSrvRdThr"
        sock_client = SockClient()
        sock_client.set_socket(conn)
        self._sock_client = sock_client

    def run(self) -> None:
        while True:
            sreq = self._sock_client.read_server_request()
            if not sreq:
                break
            sreq_type = sreq.WhichOneof("server_request_type")
            print(f"SERVER read: {sreq_type}")
            shandler_str = "server_" + sreq_type
            shandler: "Callable[[spb.ServerRequest], None]" = getattr(
                self, shandler_str, None
            )
            assert shandler, "unknown handle: {}".format(shandler_str)
            shandler(sreq)

        print("done read")

    def server_inform_init(self, sreq: "spb.ServerRequest") -> None:
        request = sreq.inform_init
        stream_id = request._info.stream_id
        settings = _dict_from_pbmap(request._settings_map)
        self._mux.add_stream(stream_id, settings=settings)

        iface = self._mux.get_stream(stream_id).interface
        iface_reader_thread = SockServerInterfaceReaderThread(
            sock_client=self._sock_client, iface=iface
        )
        iface_reader_thread.start()

    def server_record_communicate(self, sreq: "spb.ServerRequest") -> None:
        record = sreq.record_communicate
        # print("GOT rec", record)
        stream_id = record._info.stream_id
        iface = self._mux.get_stream(stream_id).interface
        assert iface.record_q
        iface.record_q.put(record)

    def server_record_publish(self, sreq: "spb.ServerRequest") -> None:
        record = sreq.record_publish
        # print("GOT rec", record)
        stream_id = record._info.stream_id
        iface = self._mux.get_stream(stream_id).interface
        assert iface.record_q
        iface.record_q.put(record)

    def server_inform_finish(self, sreq: "spb.ServerRequest") -> None:
        print("INF FIN")
        request = sreq.inform_finish
        stream_id = request._info.stream_id
        self._mux.del_stream(stream_id)

    def server_inform_teardown(self, sreq: "spb.ServerRequest") -> None:
        request = sreq.inform_teardown
        exit_code = request.exit_code
        self._mux.teardown(exit_code)


class SockAcceptThread(threading.Thread):
    _sock: socket.socket
    _mux: StreamMux

    def __init__(self, sock: socket.socket, mux: StreamMux) -> None:
        self._sock = sock
        self._mux = mux
        threading.Thread.__init__(self)
        self.name = "SockAcceptThr"

    def run(self) -> None:
        self._sock.listen(5)
        conn, addr = self._sock.accept()
        print("GOT", type(conn))
        print("Connected by", addr)
        sr = SockServerReadThread(conn=conn, mux=self._mux)
        sr.start()


class DebugThread(threading.Thread):
    def __init__(self) -> None:
        threading.Thread.__init__(self)
        self.name = "DebugThr"

    def run(self) -> None:
        while True:
            time.sleep(30)
            for thread in threading.enumerate():
                print(f"DEBUG: {thread.name}")


class SocketServer:
    _mux: StreamMux
    _address: str
    _port: int
    _sock: socket.socket

    def __init__(self, mux: Any, address: str, port: int) -> None:
        self._mux = mux
        self._address = address
        self._port = port
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    def _bind(self) -> None:
        self._sock.bind((self._address, self._port))
        self._port = self._sock.getsockname()[1]

    @property
    def port(self) -> int:
        return self._port

    def start(self) -> None:
        self._bind()
        print(f"Running at port: {self.port}")
        self._thread = SockAcceptThread(sock=self._sock, mux=self._mux)
        self._thread.start()
        self._dbg_thread = DebugThread()
        self._dbg_thread.start()