"""Separate DXLink collection process with a small snapshot request channel."""

import multiprocessing
import threading

from traderbot.data.tastytrade_market_data import TastytradeMarketDataClient
from traderbot.data.tastytrade_stream import TastytradeLiveStream


def _make_stream(symbols, start, timeframe):
    return TastytradeLiveStream(
        TastytradeMarketDataClient(), symbols, start, timeframe
    ).start_background()


def _serve_stream(connection, symbols, start, timeframe, stream_factory=None):
    stream = None
    try:
        stream = (stream_factory or _make_stream)(symbols, start, timeframe)
        while True:
            if not connection.poll(1):
                continue
            try:
                command, args = connection.recv()
            except EOFError:
                break
            if command == "stop":
                connection.send(("ok", None))
                break
            try:
                if command == "wait_ready":
                    result = stream.wait_ready(*args)
                elif command == "snapshot":
                    result = stream.snapshot(*args)
                else:
                    raise ValueError(f"Unknown stream command: {command}")
                connection.send(("ok", result))
            except Exception as exc:
                connection.send(("error", f"{type(exc).__name__}: {exc}"))
    except (BrokenPipeError, EOFError, OSError):
        pass
    finally:
        if stream is not None:
            stream.stop()
        connection.close()


class TastytradeStreamProcess:
    def __init__(self, symbols, start, timeframe="5Min", stream_factory=None):
        self.symbols = tuple(symbols)
        self.start = start
        self.timeframe = timeframe
        self.stream_factory = stream_factory
        self._lock = threading.Lock()
        self._connection = None
        self._process = None

    @property
    def pid(self):
        return self._process.pid if self._process is not None else None

    def is_alive(self):
        return self._process is not None and self._process.is_alive()

    def start_background(self):
        if self._process is not None:
            return self
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe()
        process = context.Process(
            target=_serve_stream,
            args=(child, self.symbols, self.start, self.timeframe, self.stream_factory),
            name="tastytrade-dxlink",
            daemon=True,
        )
        try:
            process.start()
        except Exception:
            parent.close()
            child.close()
            raise
        child.close()
        self._connection = parent
        self._process = process
        return self

    def _request(self, command, args, timeout):
        with self._lock:
            if not self.is_alive():
                raise RuntimeError("DXLink collector process is not running")
            try:
                self._connection.send((command, args))
                if not self._connection.poll(timeout):
                    raise TimeoutError(f"DXLink collector did not answer {command}")
                status, result = self._connection.recv()
            except (BrokenPipeError, EOFError, OSError) as exc:
                raise RuntimeError("DXLink collector process disconnected") from exc
            if status == "error":
                raise RuntimeError(result)
            return result

    def wait_ready(self, timeout):
        try:
            return self._request("wait_ready", (timeout,), timeout + 2)
        except (RuntimeError, TimeoutError):
            return False

    def snapshot(self, symbols, observed_at):
        return self._request("snapshot", (symbols, observed_at), 15)

    def stop(self):
        if self._process is None:
            return
        with self._lock:
            if self._process.is_alive():
                try:
                    self._connection.send(("stop", ()))
                    if self._connection.poll(3):
                        self._connection.recv()
                except (BrokenPipeError, EOFError, OSError):
                    pass
            self._process.join(timeout=3)
            if self._process.is_alive():
                self._process.terminate()
                self._process.join(timeout=3)
            self._connection.close()
            self._connection = None
            self._process = None
