#!/usr/bin/env python3
"""
Minimal RTL-SDR + CSDR network server.

The server runs a single wideband RTL-SDR capture process and fans the raw IQ
stream out to per-client CSDR pipelines. Each client sends one JSON line with a
target frequency and output sample rate, then receives a raw complex float32 IQ
stream.
"""

from __future__ import annotations

import argparse
import json
import logging
import queue
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any


LOGGER = logging.getLogger("csdr_server")


@dataclass(frozen=True)
class ServerConfig:
    rtl_device_index: int = 0
    rtl_serial: str | None = None
    center_frequency: int = 100_000_000
    rtl_sample_rate: int = 2_400_000
    rtl_gain: float | None = None
    transition_bandwidth: float = 0.05
    listen_host: str = "0.0.0.0"
    listen_port: int = 7355
    read_chunk_size: int = 262_144
    client_queue_chunks: int = 8

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ServerConfig":
        data = dict(raw)
        config = cls(
            rtl_device_index=int(data.get("rtl_device_index", 0)),
            rtl_serial=_optional_string(data.get("rtl_serial")),
            center_frequency=int(data["center_frequency"]),
            rtl_sample_rate=int(data["rtl_sample_rate"]),
            rtl_gain=_optional_float(data.get("rtl_gain")),
            transition_bandwidth=float(data["transition_bandwidth"]),
            listen_host=str(data.get("listen_host", "0.0.0.0")),
            listen_port=int(data.get("listen_port", 7355)),
            read_chunk_size=int(data.get("read_chunk_size", 262_144)),
            client_queue_chunks=int(data.get("client_queue_chunks", 8)),
        )
        _validate_config(config)
        return config


def _optional_string(value: Any) -> str | None:
    if value in (None, "", "null"):
        return None
    return str(value)


def _optional_float(value: Any) -> float | None:
    if value in (None, "", "null", "auto"):
        return None
    return float(value)


class CaptureManager:
    def __init__(self, config: ServerConfig) -> None:
        self.config = config
        self.process: subprocess.Popen[bytes] | None = None
        self.process_lock = threading.Lock()
        self.supervisor_thread: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.clients: set[ClientSession] = set()
        self.clients_lock = threading.Lock()

    def start(self) -> None:
        self.supervisor_thread = threading.Thread(
            target=self._supervise_capture,
            name="rtl-supervisor",
            daemon=True,
        )
        self.supervisor_thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        with self.clients_lock:
            clients = list(self.clients)
        for client in clients:
            client.close("server shutdown")
        with self.process_lock:
            process = self.process
        if process is not None:
            self._terminate_process(process, "rtl_sdr")
        if self.supervisor_thread is not None:
            self.supervisor_thread.join(timeout=2.0)

    def add_client(self, client: "ClientSession") -> None:
        with self.clients_lock:
            self.clients.add(client)

    def remove_client(self, client: "ClientSession") -> None:
        with self.clients_lock:
            self.clients.discard(client)

    def _build_rtl_command(self, device: str) -> list[str]:
        command = [
            "rtl_sdr",
            "-d",
            device,
            "-f",
            str(self.config.center_frequency),
            "-s",
            str(self.config.rtl_sample_rate),
            "-",
        ]
        if self.config.rtl_gain is not None:
            command[1:1] = ["-g", str(self.config.rtl_gain)]
        return command

    def _supervise_capture(self) -> None:
        while not self.stop_event.is_set():
            device = self._resolve_device()
            command = self._build_rtl_command(device)
            LOGGER.info("starting rtl_sdr: %s", " ".join(command))

            try:
                process = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    bufsize=0,
                )
            except Exception:
                LOGGER.exception("failed to start rtl_sdr")
                if not self.stop_event.wait(0.5):
                    continue
                break

            with self.process_lock:
                self.process = process

            stderr_thread = threading.Thread(
                target=self._stderr_loop,
                args=(process,),
                name="rtl-stderr",
                daemon=True,
            )
            stderr_thread.start()

            got_data = False
            try:
                assert process.stdout is not None
                while not self.stop_event.is_set():
                    chunk = process.stdout.read(self.config.read_chunk_size)
                    if not chunk:
                        break
                    got_data = True
                    with self.clients_lock:
                        clients = list(self.clients)
                    for client in clients:
                        client.enqueue(chunk)
            except Exception:
                LOGGER.exception("rtl_sdr reader loop failed")
            finally:
                self._terminate_process(process, "rtl_sdr")
                stderr_thread.join(timeout=2.0)
                with self.process_lock:
                    if self.process is process:
                        self.process = None

            if self.stop_event.is_set():
                break

            return_code = process.poll()
            if got_data:
                LOGGER.warning("rtl_sdr stopped after streaming data: rc=%s; restarting", return_code)
            else:
                LOGGER.warning("rtl_sdr exited before producing data: rc=%s; restarting", return_code)
            self.stop_event.wait(0.5)

    def _stderr_loop(self, process: subprocess.Popen[bytes]) -> None:
        assert process.stderr is not None
        for line in iter(process.stderr.readline, b""):
            if not line:
                break
            LOGGER.info("rtl_sdr: %s", line.decode("utf-8", errors="replace").rstrip())

    def _resolve_device(self) -> str:
        if not self.config.rtl_serial:
            return str(self.config.rtl_device_index)

        serial_index = self._find_device_index_by_serial(self.config.rtl_serial)
        if serial_index is None:
            LOGGER.warning(
                "serial %s not found; falling back to configured device index %s",
                self.config.rtl_serial,
                self.config.rtl_device_index,
            )
            return str(self.config.rtl_device_index)

        LOGGER.info(
            "resolved serial %s to rtl_sdr device index %s",
            self.config.rtl_serial,
            serial_index,
        )
        return str(serial_index)

    @staticmethod
    def _find_device_index_by_serial(serial: str) -> int | None:
        probe = subprocess.run(
            ["rtl_sdr", "-d", "9999", "-"],
            capture_output=True,
            text=True,
            check=False,
        )
        output = "\n".join(part for part in (probe.stdout, probe.stderr) if part)
        for line in output.splitlines():
            match = re.match(r"\s*(\d+):.*SN:\s*(\S+)\s*$", line)
            if match and match.group(2) == serial:
                return int(match.group(1))
        return None

    @staticmethod
    def _terminate_process(process: subprocess.Popen[bytes], name: str) -> None:
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            LOGGER.warning("%s did not exit after SIGTERM, killing it", name)
            process.kill()
            process.wait(timeout=2.0)


class ClientSession:
    def __init__(
        self,
        conn: socket.socket,
        address: tuple[str, int],
        config: ServerConfig,
        capture: CaptureManager,
        request: dict[str, Any],
    ) -> None:
        self.conn = conn
        self.address = address
        self.config = config
        self.capture = capture
        self.chunk_queue: queue.Queue[bytes | None] = queue.Queue(
            maxsize=config.client_queue_chunks
        )
        self.closed = threading.Event()
        self.pipeline: list[subprocess.Popen[bytes]] = []
        self.input_thread: threading.Thread | None = None
        self.output_thread: threading.Thread | None = None

        self.frequency = int(request["frequency"])
        self.output_rate = int(request.get("sample_rate", request.get("bandwidth")))
        self.shift_rate = (
            self.config.center_frequency - self.frequency
        ) / self.config.rtl_sample_rate
        self.decimation = _compute_decimation(
            self.config.rtl_sample_rate,
            self.output_rate,
        )
        _validate_request(self.config, self.frequency, self.output_rate)

    def start(self) -> None:
        self.pipeline = self._start_pipeline()
        self.capture.add_client(self)
        self.input_thread = threading.Thread(
            target=self._input_loop,
            name=f"client-input-{self.address[0]}:{self.address[1]}",
            daemon=True,
        )
        self.output_thread = threading.Thread(
            target=self._output_loop,
            name=f"client-output-{self.address[0]}:{self.address[1]}",
            daemon=True,
        )
        self.input_thread.start()
        self.output_thread.start()
        LOGGER.info(
            "client %s:%s started freq=%s sample_rate=%s decimation=%s",
            self.address[0],
            self.address[1],
            self.frequency,
            self.output_rate,
            self.decimation,
        )

    def enqueue(self, chunk: bytes) -> None:
        if self.closed.is_set():
            return
        try:
            self.chunk_queue.put_nowait(chunk)
        except queue.Full:
            LOGGER.warning(
                "client %s:%s fell behind capture rate; dropping connection",
                self.address[0],
                self.address[1],
            )
            self.close("client backlog")

    def close(self, reason: str) -> None:
        if self.closed.is_set():
            return
        LOGGER.info("closing client %s:%s: %s", self.address[0], self.address[1], reason)
        self.closed.set()
        self.capture.remove_client(self)
        try:
            self.chunk_queue.put_nowait(None)
        except queue.Full:
            pass
        try:
            self.conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.conn.close()
        for process in self.pipeline:
            CaptureManager._terminate_process(process, process.args[0])

    def _start_pipeline(self) -> list[subprocess.Popen[bytes]]:
        convert = subprocess.Popen(
            ["csdr", "convert", "-i", "char", "-o", "float"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        shift = subprocess.Popen(
            ["csdr", "shift", str(self.shift_rate)],
            stdin=convert.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        assert convert.stdout is not None
        convert.stdout.close()

        processes = [convert, shift]
        tail_stdout = shift.stdout
        if self.decimation > 1:
            decimate = subprocess.Popen(
                [
                    "csdr",
                    "firdecimate",
                    str(self.decimation),
                    str(self.config.transition_bandwidth),
                ],
                stdin=shift.stdout,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
            assert shift.stdout is not None
            shift.stdout.close()
            tail_stdout = decimate.stdout
            processes.append(decimate)

        for process in processes:
            threading.Thread(
                target=self._log_process_stderr,
                args=(process,),
                name=f"stderr-{process.args[1]}-{self.address[1]}",
                daemon=True,
            ).start()

        assert tail_stdout is not None
        return processes

    def _input_loop(self) -> None:
        try:
            process = self.pipeline[0]
            assert process.stdin is not None
            while not self.closed.is_set():
                chunk = self.chunk_queue.get()
                if chunk is None:
                    break
                process.stdin.write(chunk)
                process.stdin.flush()
        except BrokenPipeError:
            LOGGER.info(
                "client %s:%s processing pipeline closed upstream",
                self.address[0],
                self.address[1],
            )
        except Exception:
            LOGGER.exception("client input loop failed for %s:%s", *self.address)
        finally:
            if self.pipeline:
                stdin = self.pipeline[0].stdin
                if stdin is not None and not stdin.closed:
                    try:
                        stdin.close()
                    except OSError:
                        pass
            self.close("input loop ended")

    def _output_loop(self) -> None:
        tail = self.pipeline[-1]
        assert tail.stdout is not None
        try:
            while not self.closed.is_set():
                data = tail.stdout.read(65_536)
                if not data:
                    break
                self.conn.sendall(data)
        except (BrokenPipeError, ConnectionResetError, OSError):
            LOGGER.info(
                "client %s:%s disconnected during output",
                self.address[0],
                self.address[1],
            )
        except Exception:
            LOGGER.exception("client output loop failed for %s:%s", *self.address)
        finally:
            self.close("output loop ended")

    def _log_process_stderr(self, process: subprocess.Popen[bytes]) -> None:
        if process.stderr is None:
            return
        for line in iter(process.stderr.readline, b""):
            if not line:
                break
            LOGGER.info(
                "client %s:%s %s: %s",
                self.address[0],
                self.address[1],
                process.args[1],
                line.decode("utf-8", errors="replace").rstrip(),
            )


def _compute_decimation(input_rate: int, output_rate: int) -> int:
    if output_rate <= 0:
        raise ValueError("output sample rate must be positive")
    if output_rate > input_rate:
        raise ValueError("output sample rate cannot exceed rtl sample rate")
    if input_rate % output_rate != 0:
        raise ValueError(
            f"rtl sample rate {input_rate} is not an integer multiple of requested "
            f"sample rate {output_rate}"
        )
    return input_rate // output_rate


def _validate_request(config: ServerConfig, frequency: int, output_rate: int) -> None:
    half_band = config.rtl_sample_rate / 2
    max_offset = half_band - (output_rate / 2)
    offset = abs(frequency - config.center_frequency)
    if offset > max_offset:
        raise ValueError(
            "requested frequency and bandwidth do not fit in the current RTL capture window"
        )


def load_config(path: Path) -> ServerConfig:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return ServerConfig.from_dict(raw)


def _validate_config(config: ServerConfig) -> None:
    if config.center_frequency <= 0:
        raise ValueError("center_frequency must be positive")
    if config.rtl_sample_rate <= 0:
        raise ValueError("rtl_sample_rate must be positive")
    if config.transition_bandwidth <= 0:
        raise ValueError("transition_bandwidth must be positive")
    if config.listen_port <= 0 or config.listen_port > 65535:
        raise ValueError("listen_port must be between 1 and 65535")
    if config.read_chunk_size <= 0:
        raise ValueError("read_chunk_size must be positive")
    if config.client_queue_chunks <= 0:
        raise ValueError("client_queue_chunks must be positive")


def _check_dependencies() -> None:
    missing = [name for name in ("rtl_sdr", "csdr") if shutil.which(name) is None]
    if missing:
        raise FileNotFoundError(f"required command(s) not found in PATH: {', '.join(missing)}")


def parse_client_request(conn: socket.socket) -> dict[str, Any]:
    conn.settimeout(10.0)
    reader = conn.makefile("rb")
    line = reader.readline(16_384)
    if not line:
        raise ValueError("client did not send a request line")
    request = json.loads(line.decode("utf-8"))
    if "frequency" not in request:
        raise ValueError("request must include frequency")
    if "sample_rate" not in request and "bandwidth" not in request:
        raise ValueError("request must include sample_rate or bandwidth")
    conn.settimeout(None)
    return request


def serve(config: ServerConfig) -> int:
    capture = CaptureManager(config)
    capture.start()

    shutdown_event = threading.Event()

    def _handle_signal(signum: int, _frame: Any) -> None:
        LOGGER.info("received signal %s, shutting down", signum)
        shutdown_event.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((config.listen_host, config.listen_port))
        server.listen()
        server.settimeout(1.0)
        LOGGER.info(
            "listening on %s:%s, center_frequency=%s rtl_sample_rate=%s",
            config.listen_host,
            config.listen_port,
            config.center_frequency,
            config.rtl_sample_rate,
        )

        try:
            while not shutdown_event.is_set() and not capture.stop_event.is_set():
                try:
                    conn, address = server.accept()
                except socket.timeout:
                    continue

                try:
                    request = parse_client_request(conn)
                    session = ClientSession(conn, address, config, capture, request)
                    session.start()
                except Exception as exc:
                    LOGGER.warning(
                        "rejecting client %s:%s: %s",
                        address[0],
                        address[1],
                        exc,
                    )
                    try:
                        conn.sendall(f"error: {exc}\n".encode("utf-8"))
                    except OSError:
                        pass
                    conn.close()
        finally:
            capture.stop()

    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Minimal RTL-SDR + CSDR server")
    parser.add_argument(
        "-c",
        "--config",
        type=Path,
        default=Path("config.json"),
        help="Path to JSON configuration file",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        _check_dependencies()
        config = load_config(args.config)
        return serve(config)
    except FileNotFoundError:
        LOGGER.error("config file not found: %s", args.config)
        return 1
    except json.JSONDecodeError as exc:
        LOGGER.error("invalid json in %s: %s", args.config, exc)
        return 1
    except KeyboardInterrupt:
        return 0
    except Exception:
        LOGGER.exception("server failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
