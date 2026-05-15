#!/usr/bin/env python3
"""
Minimal RTL-SDR + CSDR network server.

The server runs a single wideband RTL-SDR capture path and fans the raw IQ
stream out to per-client CSDR pipelines. Each client sends one JSON line with a
target frequency and output sample rate, then receives a raw complex float32 IQ
stream.
"""

from __future__ import annotations

import argparse
from ctypes import c_ubyte
import json
import logging
import os
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

try:
    import rtlsdr.librtlsdr as rtlsdr_lib
    from rtlsdr.rtlsdr import BaseRtlSdr, LibUSBError
    PYRTLSDR_IMPORT_ERROR: Exception | None = None
except Exception as exc:
    rtlsdr_lib = None  # type: ignore[assignment]
    BaseRtlSdr = None  # type: ignore[assignment]
    LibUSBError = IOError  # type: ignore[assignment]
    PYRTLSDR_IMPORT_ERROR = exc


LOGGER = logging.getLogger("csdr_server")

EXIT_OUT_OF_BAND = 1
EXIT_BAD_SAMPLE_RATE = 2
EXIT_REQUEST_ERROR = 3


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
    rtl_read_timeout_seconds: float = 2.0
    stream_queue_chunks: int = 64
    client_queue_chunks: int = 64
    enqueue_timeout_seconds: float = 0.25

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
            rtl_read_timeout_seconds=float(data.get("rtl_read_timeout_seconds", 2.0)),
            stream_queue_chunks=int(data.get("stream_queue_chunks", 64)),
            client_queue_chunks=int(data.get("client_queue_chunks", 64)),
            enqueue_timeout_seconds=float(data.get("enqueue_timeout_seconds", 0.25)),
        )
        _validate_config(config)
        return config


class RequestValidationError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class DeviceResolutionRetryableError(Exception):
    pass


class DeviceResolutionFatalError(Exception):
    pass


class DeviceBusyRetryableError(Exception):
    pass


class DeviceAccessFatalError(Exception):
    pass


@dataclass(frozen=True)
class RtlDeviceInfo:
    index: int
    description: str
    serial: str | None


@dataclass(frozen=True)
class UsbDeviceInfo:
    path: Path
    vendor_id: str
    product_id: str
    serial: str | None
    description: str


def _optional_string(value: Any) -> str | None:
    if value in (None, "", "null"):
        return None
    return str(value)


def _optional_float(value: Any) -> float | None:
    if value in (None, "", "null", "auto"):
        return None
    return float(value)


def _read_sysfs_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip() or None
    except FileNotFoundError:
        return None


class CaptureManager:
    def __init__(self, config: ServerConfig) -> None:
        self.config = config
        self.sdr: BaseRtlSdr | None = None
        self.sdr_lock = threading.Lock()
        self.supervisor_thread: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.fatal_error: Exception | None = None
        self.device_wait_logged = False
        self.device_busy_logged = False
        self.graph = StreamGraph(config)

    def start(self) -> None:
        self.supervisor_thread = threading.Thread(
            target=self._supervise_capture,
            name="rtl-supervisor",
            daemon=True,
        )
        self.supervisor_thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.graph.stop("server shutdown")
        self._close_sdr()
        if self.supervisor_thread is not None:
            self.supervisor_thread.join(timeout=2.0)

    def _supervise_capture(self) -> None:
        while not self.stop_event.is_set():
            try:
                device_index = self._resolve_device()
            except DeviceResolutionRetryableError as exc:
                if not self.device_wait_logged:
                    LOGGER.warning("%s", exc)
                    self.device_wait_logged = True
                self.stop_event.wait(0.5)
                continue
            except DeviceResolutionFatalError as exc:
                LOGGER.error("%s", exc)
                self.fatal_error = exc
                self.stop_event.set()
                break
            except DeviceAccessFatalError as exc:
                LOGGER.error("%s", exc)
                self.fatal_error = exc
                self.stop_event.set()
                break

            if self.device_wait_logged:
                LOGGER.info("Configured device is now available.")
                self.device_wait_logged = False
            if self.device_busy_logged:
                LOGGER.info("Configured device is no longer busy.")
                self.device_busy_logged = False

            try:
                sdr = self._open_sdr(device_index)
            except DeviceBusyRetryableError as exc:
                if not self.device_busy_logged:
                    LOGGER.warning("%s", exc)
                    self.device_busy_logged = True
                self.stop_event.wait(0.5)
                continue
            except Exception:
                LOGGER.exception("failed to start pyrtlsdr capture")
                if not self.stop_event.wait(0.5):
                    continue
                break

            got_data = False
            data_timeout = False
            capture_queue: queue.Queue[bytes | Exception | None] = queue.Queue(maxsize=4)
            reader_stop = threading.Event()
            reader_thread = threading.Thread(
                target=self._sdr_reader_loop,
                args=(sdr, capture_queue, reader_stop),
                name="rtl-reader",
                daemon=True,
            )
            reader_thread.start()
            try:
                while not self.stop_event.is_set():
                    try:
                        item = capture_queue.get(timeout=self.config.rtl_read_timeout_seconds)
                    except queue.Empty:
                        data_timeout = True
                        LOGGER.warning(
                            "pyrtlsdr produced no data for %.3f seconds; restarting from device discovery",
                            self.config.rtl_read_timeout_seconds,
                        )
                        break
                    if item is None:
                        break
                    if isinstance(item, Exception):
                        raise item
                    chunk = item
                    got_data = True
                    self.graph.feed_raw(chunk)
            except (LibUSBError, IOError, OSError):
                LOGGER.exception("pyrtlsdr reader loop failed")
            except Exception:
                LOGGER.exception("pyrtlsdr reader loop failed")
            finally:
                reader_stop.set()
                self._close_sdr()
                reader_thread.join(timeout=2.0)

            if self.stop_event.is_set():
                break

            if data_timeout:
                LOGGER.info("Re-entering USB and device-index discovery after pyrtlsdr data timeout.")
            elif got_data:
                LOGGER.warning(
                    "pyrtlsdr stopped after streaming data; re-entering device discovery"
                )
            else:
                LOGGER.warning(
                    "pyrtlsdr stopped before producing data; re-entering device discovery"
                )
            self.stop_event.wait(0.5)

    def get_output_stream(self, frequency: int, output_rate: int) -> "SharedStream":
        return self.graph.get_output_stream(frequency, output_rate)

    def _open_sdr(self, device_index: int) -> BaseRtlSdr:
        assert BaseRtlSdr is not None
        assert rtlsdr_lib is not None
        LOGGER.info("starting pyrtlsdr capture on device index %s", device_index)
        try:
            sdr = BaseRtlSdr(device_index=device_index, dithering_enabled=False)
        except LibUSBError as exc:
            if getattr(exc, "errno", None) == -3:
                raise DeviceAccessFatalError(
                    f"Access denied while opening device index {device_index}. "
                    "Check udev permissions or run with sufficient access."
                ) from exc
            if getattr(exc, "errno", None) == -6:
                raise DeviceBusyRetryableError(
                    f"Configured device index {device_index} is busy. Waiting for it to become available."
                ) from exc
            raise
        sdr.sample_rate = self.config.rtl_sample_rate
        sdr.center_freq = self.config.center_frequency
        sdr.gain = "auto" if self.config.rtl_gain is None else self.config.rtl_gain
        result = rtlsdr_lib.rtlsdr_reset_buffer(sdr.dev_p)
        if result < 0:
            sdr.close()
            raise LibUSBError(result, "Could not reset buffer")
        with self.sdr_lock:
            self.sdr = sdr
        return sdr

    def _close_sdr(self) -> None:
        with self.sdr_lock:
            sdr = self.sdr
            self.sdr = None
        if sdr is not None:
            try:
                sdr.close()
            except Exception:
                LOGGER.exception("failed to close pyrtlsdr device")

    def _sdr_reader_loop(
        self,
        sdr: BaseRtlSdr,
        output_queue: queue.Queue[bytes | Exception | None],
        stop_event: threading.Event,
    ) -> None:
        try:
            while not self.stop_event.is_set() and not stop_event.is_set():
                output_queue.put(sdr.read_bytes(self.config.read_chunk_size))
        except Exception as exc:
            try:
                output_queue.put_nowait(exc)
            except queue.Full:
                pass
        finally:
            try:
                output_queue.put_nowait(None)
            except queue.Full:
                pass

    def _resolve_device(self) -> int:
        if not self.config.rtl_serial:
            devices = self._probe_rtl_devices()
            if not any(device.index == self.config.rtl_device_index for device in devices):
                raise DeviceResolutionFatalError(
                    f"Configured rtl_device_index {self.config.rtl_device_index} does not exist. "
                    "Set rtl_serial to a valid device serial number, or choose an existing device index."
                )
            return self.config.rtl_device_index

        self._wait_for_unique_usb_serial(self.config.rtl_serial)
        devices = self._probe_rtl_devices()
        matches = [device for device in devices if device.serial == self.config.rtl_serial]
        if not matches:
            raise DeviceResolutionRetryableError(
                f"Configured serial {self.config.rtl_serial} is present on USB but was not yet visible to pyrtlsdr. "
                "Waiting for librtlsdr to detect that device."
            )
        if len(matches) > 1:
            raise DeviceResolutionFatalError(
                f"Multiple RTL-SDR devices were found by pyrtlsdr with serial {self.config.rtl_serial}. "
                "Set rtl_serial to null to use rtl_device_index instead, or assign unique serial numbers "
                "to each device using rtl_eeprom."
            )

        LOGGER.info(
            "resolved serial %s to rtl_sdr device index %s",
            self.config.rtl_serial,
            matches[0].index,
        )
        return matches[0].index

    @staticmethod
    def _wait_for_unique_usb_serial(serial: str) -> None:
        try:
            devices = CaptureManager._probe_usb_rtl_devices()
        except OSError as exc:
            LOGGER.warning(
                "USB probe failed (%s); falling back to librtlsdr-only serial detection",
                exc,
            )
            return

        matches = [device for device in devices if device.serial == serial]
        if not matches:
            raise DeviceResolutionRetryableError(
                f"Configured serial {serial} was not found on USB. Waiting for that device to appear."
            )
        if len(matches) > 1:
            raise DeviceResolutionFatalError(
                f"Multiple USB RTL-SDR devices were found with serial {serial}. "
                "Set rtl_serial to null to use rtl_device_index instead, or assign unique serial numbers "
                "to each device using rtl_eeprom."
            )

    @staticmethod
    def _probe_usb_rtl_devices() -> list[UsbDeviceInfo]:
        usb_root = Path("/sys/bus/usb/devices")
        devices: list[UsbDeviceInfo] = []
        for entry in usb_root.iterdir():
            vendor_id = _read_sysfs_text(entry / "idVendor")
            product_id = _read_sysfs_text(entry / "idProduct")
            if vendor_id is None or product_id is None:
                continue
            vendor_id = vendor_id.lower()
            product_id = product_id.lower()
            if vendor_id != "0bda" or product_id not in {"2832", "2838"}:
                continue
            serial = _read_sysfs_text(entry / "serial")
            manufacturer = _read_sysfs_text(entry / "manufacturer")
            product = _read_sysfs_text(entry / "product")
            description_parts = [part for part in (manufacturer, product) if part]
            description = ", ".join(description_parts) or entry.name
            devices.append(
                UsbDeviceInfo(
                    path=entry,
                    vendor_id=vendor_id,
                    product_id=product_id,
                    serial=serial,
                    description=description,
                )
            )
        return devices

    @staticmethod
    def _probe_rtl_devices() -> list[RtlDeviceInfo]:
        devices: list[RtlDeviceInfo] = []
        assert rtlsdr_lib is not None
        device_count = int(rtlsdr_lib.rtlsdr_get_device_count())
        for index in range(device_count):
            manufacturer = (c_ubyte * 256)()
            product = (c_ubyte * 256)()
            serial = (c_ubyte * 256)()
            result = rtlsdr_lib.rtlsdr_get_device_usb_strings(index, manufacturer, product, serial)
            if result != 0:
                if result == -3:
                    raise DeviceAccessFatalError(
                        f"Access denied while reading USB strings for device {index}. "
                        "Check udev permissions or run with sufficient access."
                    )
                raise LibUSBError(result, f"while reading USB strings (device {index})")
            manufacturer_text = "".join(chr(value) for value in manufacturer if value > 0)
            product_text = "".join(chr(value) for value in product if value > 0)
            serial_text = "".join(chr(value) for value in serial if value > 0)
            description_parts = [part for part in (manufacturer, product) if part]
            name = rtlsdr_lib.rtlsdr_get_device_name(index)
            name_text = name.decode("utf-8", errors="replace") if name else ""
            devices.append(
                RtlDeviceInfo(
                    index=index,
                    description=", ".join(
                        part for part in (manufacturer_text, product_text) if part
                    ) or name_text,
                    serial=serial_text or None,
                )
            )
        return devices

    @staticmethod
    def _terminate_process(process: subprocess.Popen[bytes], name: str) -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            LOGGER.warning("%s did not exit after SIGTERM, killing it", name)
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                return
            process.wait(timeout=2.0)


class SharedStream:
    def __init__(
        self,
        config: ServerConfig,
        name: str,
        command: list[str],
        manager: "StreamGraph",
        parent: "SharedStream | None" = None,
        close_when_unused: bool = True,
    ) -> None:
        self.config = config
        self.name = name
        self.command = command
        self.manager = manager
        self.parent = parent
        self.close_when_unused = close_when_unused
        self.process: subprocess.Popen[bytes] | None = None
        self.input_queue: queue.Queue[bytes | None] = queue.Queue(
            maxsize=config.stream_queue_chunks
        )
        self.closed = threading.Event()
        self.subscribers: set[Any] = set()
        self.subscribers_lock = threading.Lock()
        self.input_thread: threading.Thread | None = None
        self.output_thread: threading.Thread | None = None
        self.stderr_thread: threading.Thread | None = None

    def start(self) -> None:
        self.process = subprocess.Popen(
            self.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            start_new_session=True,
        )
        self.input_thread = threading.Thread(
            target=self._input_loop,
            name=f"{self.name}-input",
            daemon=True,
        )
        self.output_thread = threading.Thread(
            target=self._output_loop,
            name=f"{self.name}-output",
            daemon=True,
        )
        self.stderr_thread = threading.Thread(
            target=self._stderr_loop,
            name=f"{self.name}-stderr",
            daemon=True,
        )
        self.input_thread.start()
        self.output_thread.start()
        self.stderr_thread.start()
        if self.parent is not None:
            self.parent.add_subscriber(self)
        LOGGER.info("started shared stream %s: %s", self.name, " ".join(self.command))

    def add_subscriber(self, subscriber: Any) -> None:
        with self.subscribers_lock:
            self.subscribers.add(subscriber)

    def remove_subscriber(self, subscriber: Any) -> None:
        should_close = False
        with self.subscribers_lock:
            self.subscribers.discard(subscriber)
            should_close = (
                self.close_when_unused
                and not self.subscribers
                and not self.closed.is_set()
            )
        if should_close:
            self.close("unused shared stream", propagate=False)

    def enqueue(self, chunk: bytes) -> bool:
        if self.closed.is_set():
            return False
        try:
            self.input_queue.put(chunk, timeout=self.config.enqueue_timeout_seconds)
            return True
        except queue.Full:
            LOGGER.warning("%s fell behind upstream input; closing branch", self.name)
            self.close("stream backlog")
            return False

    def close(self, reason: str, propagate: bool = True) -> None:
        if self.closed.is_set():
            return
        LOGGER.info("closing shared stream %s: %s", self.name, reason)
        self.closed.set()
        if self.parent is not None:
            self.parent.remove_subscriber(self)
        self.manager.on_stream_closed(self)
        try:
            self.input_queue.put_nowait(None)
        except queue.Full:
            pass
        with self.subscribers_lock:
            subscribers = list(self.subscribers)
            self.subscribers.clear()
        if propagate:
            for subscriber in subscribers:
                subscriber.close(f"upstream stream closed: {self.name}")
        if self.process is not None:
            CaptureManager._terminate_process(self.process, self.command[0])

    def _input_loop(self) -> None:
        try:
            assert self.process is not None
            assert self.process.stdin is not None
            while not self.closed.is_set():
                chunk = self.input_queue.get()
                if chunk is None:
                    break
                self.process.stdin.write(chunk)
        except BrokenPipeError:
            LOGGER.info("%s stdin closed", self.name)
        except Exception:
            LOGGER.exception("%s input loop failed", self.name)
        finally:
            if self.process is not None and self.process.stdin is not None and not self.process.stdin.closed:
                try:
                    self.process.stdin.close()
                except OSError:
                    pass

    def _output_loop(self) -> None:
        try:
            assert self.process is not None
            assert self.process.stdout is not None
            while not self.closed.is_set():
                data = self.process.stdout.read(65_536)
                if not data:
                    break
                with self.subscribers_lock:
                    subscribers = list(self.subscribers)
                for subscriber in subscribers:
                    subscriber.enqueue(data)
        except Exception:
            LOGGER.exception("%s output loop failed", self.name)
        finally:
            if not self.closed.is_set():
                self.close("process output ended")

    def _stderr_loop(self) -> None:
        assert self.process is not None
        if self.process.stderr is None:
            return
        for line in iter(self.process.stderr.readline, b""):
            if not line:
                break
            LOGGER.info(
                "%s: %s",
                self.name,
                line.decode("utf-8", errors="replace").rstrip(),
            )


class StreamGraph:
    def __init__(self, config: ServerConfig) -> None:
        self.config = config
        self.lock = threading.Lock()
        self.root_stream: SharedStream | None = None
        self.shift_streams: dict[int, SharedStream] = {}
        self.decimation_streams: dict[tuple[int, int], SharedStream] = {}

    def stop(self, reason: str) -> None:
        with self.lock:
            root = self.root_stream
            shifts = list(self.shift_streams.values())
            decimations = list(self.decimation_streams.values())
            self.root_stream = None
            self.shift_streams = {}
            self.decimation_streams = {}
        for stream in decimations:
            stream.close(reason, propagate=True)
        for stream in shifts:
            stream.close(reason, propagate=True)
        if root is not None:
            root.close(reason, propagate=True)

    def feed_raw(self, chunk: bytes) -> None:
        with self.lock:
            root = self.root_stream
        if root is None:
            return
        if not root.enqueue(chunk):
            self.stop("root convert stream failed")

    def get_output_stream(self, frequency: int, output_rate: int) -> SharedStream:
        decimation = _compute_decimation(self.config.rtl_sample_rate, output_rate)
        _validate_request(self.config, frequency, output_rate)

        with self.lock:
            root = self.root_stream
            if root is None:
                root = SharedStream(
                    config=self.config,
                    name="convert",
                    command=["csdr", "convert", "-i", "char", "-o", "float"],
                    manager=self,
                    parent=None,
                    close_when_unused=True,
                )
                root.start()
                self.root_stream = root

            shift_stream = self.shift_streams.get(frequency)
            if shift_stream is None:
                shift_rate = (self.config.center_frequency - frequency) / self.config.rtl_sample_rate
                shift_stream = SharedStream(
                    config=self.config,
                    name=f"shift-{frequency}",
                    command=["csdr", "shift", str(shift_rate)],
                    manager=self,
                    parent=root,
                    close_when_unused=True,
                )
                shift_stream.start()
                self.shift_streams[frequency] = shift_stream

            if decimation == 1:
                return shift_stream

            key = (frequency, output_rate)
            decimation_stream = self.decimation_streams.get(key)
            if decimation_stream is None:
                decimation_stream = SharedStream(
                    config=self.config,
                    name=f"firdecimate-{frequency}-{output_rate}",
                    command=[
                        "csdr",
                        "firdecimate",
                        str(decimation),
                        str(self.config.transition_bandwidth),
                    ],
                    manager=self,
                    parent=shift_stream,
                    close_when_unused=True,
                )
                decimation_stream.start()
                self.decimation_streams[key] = decimation_stream
            return decimation_stream

    def on_stream_closed(self, stream: SharedStream) -> None:
        with self.lock:
            if self.root_stream is stream:
                self.root_stream = None
            for frequency, candidate in list(self.shift_streams.items()):
                if candidate is stream:
                    del self.shift_streams[frequency]
            for key, candidate in list(self.decimation_streams.items()):
                if candidate is stream:
                    del self.decimation_streams[key]


class ClientSession:
    def __init__(
        self,
        conn: socket.socket,
        address: tuple[str, int],
        config: ServerConfig,
        source_stream: SharedStream,
        frequency: int,
        output_rate: int,
    ) -> None:
        self.conn = conn
        self.address = address
        self.config = config
        self.source_stream = source_stream
        self.frequency = frequency
        self.output_rate = output_rate
        self.chunk_queue: queue.Queue[bytes | None] = queue.Queue(
            maxsize=config.client_queue_chunks
        )
        self.closed = threading.Event()
        self.output_thread: threading.Thread | None = None

    def start(self) -> None:
        self.source_stream.add_subscriber(self)

    def activate(self) -> None:
        self.output_thread = threading.Thread(
            target=self._output_loop,
            name=f"client-output-{self.address[0]}:{self.address[1]}",
            daemon=True,
        )
        self.output_thread.start()
        LOGGER.info(
            "client %s:%s started freq=%s sample_rate=%s",
            self.address[0],
            self.address[1],
            self.frequency,
            self.output_rate,
        )

    def enqueue(self, chunk: bytes) -> None:
        if self.closed.is_set():
            return
        try:
            self.chunk_queue.put(chunk, timeout=self.config.enqueue_timeout_seconds)
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
        self.source_stream.remove_subscriber(self)
        try:
            self.chunk_queue.put_nowait(None)
        except queue.Full:
            pass
        try:
            self.conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.conn.close()

    def _output_loop(self) -> None:
        try:
            while not self.closed.is_set():
                data = self.chunk_queue.get()
                if data is None:
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


def _compute_decimation(input_rate: int, output_rate: int) -> int:
    if output_rate <= 0:
        raise RequestValidationError(EXIT_BAD_SAMPLE_RATE, "output sample rate must be positive")
    if output_rate > input_rate:
        raise RequestValidationError(
            EXIT_BAD_SAMPLE_RATE,
            "output sample rate cannot exceed rtl sample rate",
        )
    if input_rate % output_rate != 0:
        raise RequestValidationError(
            EXIT_BAD_SAMPLE_RATE,
            f"rtl sample rate {input_rate} is not an integer multiple of requested "
            f"sample rate {output_rate}",
        )
    return input_rate // output_rate


def _validate_request(config: ServerConfig, frequency: int, output_rate: int) -> None:
    shift_rate = (config.center_frequency - frequency) / config.rtl_sample_rate
    if shift_rate < -0.5 or shift_rate > 0.5:
        raise RequestValidationError(
            EXIT_OUT_OF_BAND,
            "requested frequency is out of band for the current RTL capture window",
        )


def load_config(path: Path) -> ServerConfig:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return ServerConfig.from_dict(raw)


def _validate_config(config: ServerConfig) -> None:
    if config.center_frequency <= 0:
        raise ValueError("center_frequency must be positive")
    if not _is_valid_rtl_sample_rate(config.rtl_sample_rate):
        raise ValueError(
            f"Cannot sample at {config.rtl_sample_rate} S/s. "
            "The sample rate must be between 225001 S/s and 300000 S/s "
            "or 900001 S/s and 3200000 S/s."
        )
    if config.rtl_gain is not None and not (1.0 <= config.rtl_gain <= 49.6):
        raise ValueError("rtl_gain must be between 1.0 dB and 49.6 dB")
    if not (0.005 <= config.transition_bandwidth <= 0.05):
        raise ValueError("transition_bandwidth must be between 0.005 and 0.05")
    if config.listen_port <= 0 or config.listen_port > 65535:
        raise ValueError("listen_port must be between 1 and 65535")
    if config.read_chunk_size <= 0:
        raise ValueError("read_chunk_size must be positive")
    if config.rtl_read_timeout_seconds <= 0:
        raise ValueError("rtl_read_timeout_seconds must be positive")
    if config.stream_queue_chunks <= 0:
        raise ValueError("stream_queue_chunks must be positive")
    if config.client_queue_chunks <= 0:
        raise ValueError("client_queue_chunks must be positive")
    if config.enqueue_timeout_seconds < 0:
        raise ValueError("enqueue_timeout_seconds must be non-negative")


def _is_valid_rtl_sample_rate(sample_rate: int) -> bool:
    return (
        225_001 <= sample_rate <= 300_000
        or 900_001 <= sample_rate <= 3_200_000
    )


def _check_dependencies() -> None:
    if PYRTLSDR_IMPORT_ERROR is not None:
        raise ImportError(f"pyrtlsdr compatibility layer could not load librtlsdr: {PYRTLSDR_IMPORT_ERROR}")
    if shutil.which("csdr") is None:
        raise FileNotFoundError("required command(s) not found in PATH: csdr")


def parse_client_request(conn: socket.socket) -> dict[str, Any]:
    conn.settimeout(10.0)
    reader = conn.makefile("rb")
    line = reader.readline(16_384)
    if not line:
        raise RequestValidationError(EXIT_REQUEST_ERROR, "client did not send a request line")
    try:
        request = json.loads(line.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise RequestValidationError(EXIT_REQUEST_ERROR, f"invalid request json: {exc}") from exc
    if "frequency" not in request:
        raise RequestValidationError(EXIT_REQUEST_ERROR, "request must include frequency")
    if "sample_rate" not in request and "bandwidth" not in request:
        raise RequestValidationError(
            EXIT_REQUEST_ERROR,
            "request must include sample_rate or bandwidth",
        )
    conn.settimeout(None)
    return request


def send_handshake(conn: socket.socket, payload: dict[str, Any]) -> None:
    conn.sendall(json.dumps(payload).encode("utf-8") + b"\n")


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
                    frequency = int(request["frequency"])
                    output_rate = int(request.get("sample_rate", request.get("bandwidth")))
                    source_stream = capture.get_output_stream(frequency, output_rate)
                    session = ClientSession(
                        conn=conn,
                        address=address,
                        config=config,
                        source_stream=source_stream,
                        frequency=frequency,
                        output_rate=output_rate,
                    )
                    session.start()
                    send_handshake(conn, {"status": "ok"})
                    session.activate()
                except RequestValidationError as exc:
                    LOGGER.warning(
                        "rejecting client %s:%s: %s",
                        address[0],
                        address[1],
                        exc,
                    )
                    try:
                        send_handshake(
                            conn,
                            {
                                "status": "error",
                                "code": exc.code,
                                "error": exc.message,
                            },
                        )
                    except OSError:
                        pass
                    conn.close()
                except Exception as exc:
                    LOGGER.warning(
                        "rejecting client %s:%s: %s",
                        address[0],
                        address[1],
                        exc,
                    )
                    try:
                        send_handshake(
                            conn,
                            {
                                "status": "error",
                                "code": EXIT_REQUEST_ERROR,
                                "error": str(exc),
                            },
                        )
                    except OSError:
                        pass
                    conn.close()
        finally:
            capture.stop()

    if capture.fatal_error is not None:
        raise capture.fatal_error

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
    except SystemExit:
        raise
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
