from __future__ import annotations

import ctypes
import ctypes.util
from ctypes import c_ubyte
import importlib
import queue
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from .constants import EXIT_REQUEST_ERROR, LOGGER
from .config import ServerConfig, load_config
from .dsp import (
    _compute_automatic_center_frequency,
    _get_audio_output_rate,
    _get_required_bandwidth,
    _validate_audio_modulation,
    _validate_audio_modulation_supported,
    _validate_request_frequency,
    _validate_requested_mode_supported,
    _validate_session_request,
)
from .opus_codec import probe_opus_encoder, validate_opus_bitrate
from .errors import (
    DeviceAccessFatalError,
    DeviceBusyRetryableError,
    DeviceResolutionFatalError,
    DeviceResolutionRetryableError,
    RequestValidationError,
    RtlDeviceInfo,
    UsbDeviceInfo,
)
from .graph import StreamGraph
from .utils import _read_sysfs_text


RTLSDR_READ_ASYNC_CALLBACK = ctypes.CFUNCTYPE(
    None,
    ctypes.POINTER(ctypes.c_ubyte),
    ctypes.c_uint32,
    ctypes.c_void_p,
)


class CompatLibUSBError(IOError):
    _errno_map = {
        -1: ("LIBUSB_ERROR_IO", "Input/output error"),
        -2: ("LIBUSB_ERROR_INVALID_PARAM", "Invalid parameter"),
        -3: ("LIBUSB_ERROR_ACCESS", "Access denied (insufficient permissions)"),
        -4: ("LIBUSB_ERROR_NO_DEVICE", "No such device (it may have been disconnected)"),
        -5: ("LIBUSB_ERROR_NOT_FOUND", "Entity not found"),
        -6: ("LIBUSB_ERROR_BUSY", "Resource busy"),
        -7: ("LIBUSB_ERROR_TIMEOUT", "Operation timed out"),
        -8: ("LIBUSB_ERROR_OVERFLOW", "Overflow"),
        -9: ("LIBUSB_ERROR_PIPE", "Pipe error"),
        -10: ("LIBUSB_ERROR_INTERRUPTED", "System call interrupted"),
        -11: ("LIBUSB_ERROR_NO_MEM", "Insufficient memory"),
        -12: ("LIBUSB_ERROR_NOT_SUPPORTED", "Operation not supported"),
        -99: ("LIBUSB_ERROR_OTHER", "Other error"),
    }

    def __init__(self, errno: int, msg: str = "") -> None:
        super().__init__(errno, msg)
        self.errno = errno
        self.msg = msg

    def __str__(self) -> str:
        mapped = self._errno_map.get(self.errno)
        if mapped is None:
            return f'Error code {self.errno}: "{self.msg}"'
        error_id, error_message = mapped
        return f'<{error_id} ({self.errno}): {error_message}> "{self.msg}"'


def _load_system_librtlsdr() -> ctypes.CDLL:
    candidates = [
        ctypes.util.find_library("rtlsdr"),
        ctypes.util.find_library("librtlsdr"),
        "librtlsdr.so",
        "librtlsdr.so.0",
    ]
    errors: list[str] = []
    for candidate in candidates:
        if not candidate:
            continue
        try:
            library = ctypes.CDLL(candidate)
            _configure_librtlsdr_functions(library)
            return library
        except OSError as exc:
            errors.append(f"{candidate}: {exc}")
    detail = "; ".join(errors) if errors else "ctypes could not locate librtlsdr"
    raise ImportError(
        "could not load librtlsdr. Install your distribution's librtlsdr package "
        f"or install pyrtlsdrlib on supported architectures. Details: {detail}"
    )


def _required_librtlsdr_symbol(library: ctypes.CDLL, name: str):
    try:
        return getattr(library, name)
    except AttributeError as exc:
        raise ImportError(
            f"librtlsdr is missing required symbol {name}; install a complete librtlsdr package"
        ) from exc


def _configure_librtlsdr_functions(library: ctypes.CDLL) -> None:
    p_rtlsdr_dev = ctypes.c_void_p
    required_signatures = {
        "rtlsdr_get_device_count": (ctypes.c_uint, []),
        "rtlsdr_get_device_name": (ctypes.c_char_p, [ctypes.c_uint]),
        "rtlsdr_get_device_usb_strings": (
            ctypes.c_int,
            [
                ctypes.c_uint,
                ctypes.POINTER(ctypes.c_ubyte),
                ctypes.POINTER(ctypes.c_ubyte),
                ctypes.POINTER(ctypes.c_ubyte),
            ],
        ),
        "rtlsdr_open": (ctypes.c_int, [ctypes.POINTER(p_rtlsdr_dev), ctypes.c_uint]),
        "rtlsdr_close": (ctypes.c_int, [p_rtlsdr_dev]),
        "rtlsdr_set_center_freq": (ctypes.c_int, [p_rtlsdr_dev, ctypes.c_uint]),
        "rtlsdr_set_freq_correction": (ctypes.c_int, [p_rtlsdr_dev, ctypes.c_int]),
        "rtlsdr_set_tuner_gain": (ctypes.c_int, [p_rtlsdr_dev, ctypes.c_int]),
        "rtlsdr_get_tuner_gains": (
            ctypes.c_int,
            [p_rtlsdr_dev, ctypes.POINTER(ctypes.c_int)],
        ),
        "rtlsdr_set_tuner_gain_mode": (ctypes.c_int, [p_rtlsdr_dev, ctypes.c_int]),
        "rtlsdr_set_sample_rate": (ctypes.c_int, [p_rtlsdr_dev, ctypes.c_uint]),
        "rtlsdr_reset_buffer": (ctypes.c_int, [p_rtlsdr_dev]),
        "rtlsdr_read_async": (
            ctypes.c_int,
            [
                p_rtlsdr_dev,
                RTLSDR_READ_ASYNC_CALLBACK,
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.c_uint32,
            ],
        ),
        "rtlsdr_cancel_async": (ctypes.c_int, [p_rtlsdr_dev]),
    }
    for name, (restype, argtypes) in required_signatures.items():
        function = _required_librtlsdr_symbol(library, name)
        function.restype = restype
        function.argtypes = argtypes

    optional_signatures = {
        "rtlsdr_set_testmode": (ctypes.c_int, [p_rtlsdr_dev, ctypes.c_int]),
        "rtlsdr_set_dithering": (ctypes.c_int, [p_rtlsdr_dev, ctypes.c_int]),
        "rtlsdr_set_agc_mode": (ctypes.c_int, [p_rtlsdr_dev, ctypes.c_int]),
        "rtlsdr_set_bias_tee": (ctypes.c_int, [p_rtlsdr_dev, ctypes.c_int]),
    }
    for name, (restype, argtypes) in optional_signatures.items():
        function = getattr(library, name, None)
        if function is not None:
            function.restype = restype
            function.argtypes = argtypes


class CompatBaseRtlSdr:
    def __init__(
        self,
        device_index: int = 0,
        test_mode_enabled: bool = False,
        serial_number: str | None = None,
        dithering_enabled: bool = True,
    ) -> None:
        if serial_number is not None:
            raise NotImplementedError("serial_number is not supported by the compatibility RTL-SDR wrapper")
        assert rtlsdr_lib is not None
        self.dev_p = ctypes.c_void_p(None)
        self.device_opened = False
        result = rtlsdr_lib.rtlsdr_open(ctypes.byref(self.dev_p), int(device_index))
        if result < 0:
            raise CompatLibUSBError(result, f"Could not open SDR (device index = {device_index})")
        self.device_opened = True
        try:
            self._set_optional_int("rtlsdr_set_testmode", int(test_mode_enabled), "Could not set test mode")
            self._set_optional_int(
                "rtlsdr_set_dithering",
                int(dithering_enabled),
                "Could not set PLL dithering mode",
            )
            self._reset_buffer()
            self.gain_values = self.get_gains()
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        if not self.device_opened:
            return
        assert rtlsdr_lib is not None
        rtlsdr_lib.rtlsdr_close(self.dev_p)
        self.device_opened = False

    def _set_optional_int(self, name: str, value: int, message: str) -> None:
        function = getattr(rtlsdr_lib, name, None)
        if function is None:
            LOGGER.debug("librtlsdr does not expose %s; skipping", name)
            return
        result = function(self.dev_p, value)
        if result < 0:
            raise CompatLibUSBError(result, message)

    def _reset_buffer(self) -> None:
        assert rtlsdr_lib is not None
        result = rtlsdr_lib.rtlsdr_reset_buffer(self.dev_p)
        if result < 0:
            raise CompatLibUSBError(result, "Could not reset buffer")

    @property
    def sample_rate(self) -> int:
        raise AttributeError("sample_rate is write-only in compatibility mode")

    @sample_rate.setter
    def sample_rate(self, rate: int) -> None:
        assert rtlsdr_lib is not None
        result = rtlsdr_lib.rtlsdr_set_sample_rate(self.dev_p, int(rate))
        if result < 0:
            self.close()
            raise CompatLibUSBError(result, f"Could not set sample rate to {int(rate)} Hz")

    @property
    def center_freq(self) -> int:
        raise AttributeError("center_freq is write-only in compatibility mode")

    @center_freq.setter
    def center_freq(self, freq: int) -> None:
        assert rtlsdr_lib is not None
        result = rtlsdr_lib.rtlsdr_set_center_freq(self.dev_p, int(freq))
        if result < 0:
            self.close()
            raise CompatLibUSBError(result, f"Could not set center_freq to {int(freq)} Hz")

    @property
    def freq_correction(self) -> int:
        raise AttributeError("freq_correction is write-only in compatibility mode")

    @freq_correction.setter
    def freq_correction(self, ppm: int) -> None:
        assert rtlsdr_lib is not None
        result = rtlsdr_lib.rtlsdr_set_freq_correction(self.dev_p, int(ppm))
        if result < 0:
            self.close()
            raise CompatLibUSBError(result, f"Could not set freq. offset to {int(ppm)} ppm")

    @property
    def gain(self) -> float | str:
        raise AttributeError("gain is write-only in compatibility mode")

    @gain.setter
    def gain(self, gain: float | str) -> None:
        assert rtlsdr_lib is not None
        if isinstance(gain, str) and gain == "auto":
            result = rtlsdr_lib.rtlsdr_set_tuner_gain_mode(self.dev_p, 0)
            if result < 0:
                raise CompatLibUSBError(result, "Could not set tuner gain mode")
            agc = getattr(rtlsdr_lib, "rtlsdr_set_agc_mode", None)
            if agc is not None:
                result = agc(self.dev_p, 1)
                if result < 0:
                    raise CompatLibUSBError(result, "Could not set AGC mode")
            return

        requested_tenths = int(round(float(gain) * 10))
        selected_gain = requested_tenths
        if self.gain_values:
            selected_gain = min(self.gain_values, key=lambda value: abs(value - requested_tenths))
        result = rtlsdr_lib.rtlsdr_set_tuner_gain_mode(self.dev_p, 1)
        if result < 0:
            raise CompatLibUSBError(result, "Could not set tuner gain mode")
        result = rtlsdr_lib.rtlsdr_set_tuner_gain(self.dev_p, selected_gain)
        if result < 0:
            self.close()
            raise CompatLibUSBError(result, f"Could not set gain to {gain}")

    def get_gains(self) -> list[int]:
        assert rtlsdr_lib is not None
        buffer = (ctypes.c_int * 50)()
        result = rtlsdr_lib.rtlsdr_get_tuner_gains(self.dev_p, buffer)
        if result <= 0:
            return []
        return [buffer[index] for index in range(result)]

    def set_bias_tee(self, enabled: bool) -> None:
        function = getattr(rtlsdr_lib, "rtlsdr_set_bias_tee", None)
        if function is None:
            if enabled:
                raise RuntimeError(
                    "rtl.bias_tee is true, but this librtlsdr does not support rtlsdr_set_bias_tee"
                )
            return
        result = function(self.dev_p, int(enabled))
        if result < 0:
            raise CompatLibUSBError(result, "Could not set bias tee")


try:
    rtlsdr_librtlsdr_module = importlib.import_module("rtlsdr.librtlsdr")
    rtlsdr_lib = rtlsdr_librtlsdr_module.librtlsdr
    from rtlsdr.rtlsdr import BaseRtlSdr, LibUSBError
    _configure_librtlsdr_functions(rtlsdr_lib)
    PYRTLSDR_IMPORT_ERROR: Exception | None = None
except Exception as exc:
    try:
        rtlsdr_lib = _load_system_librtlsdr()
        BaseRtlSdr = CompatBaseRtlSdr
        LibUSBError = CompatLibUSBError
        PYRTLSDR_IMPORT_ERROR: Exception | None = None
        LOGGER.warning(
            "PyRTLSDR could not initialize its native wrapper (%s); using direct librtlsdr compatibility mode",
            exc,
        )
    except Exception as fallback_exc:
        rtlsdr_lib = None  # type: ignore[assignment]
        BaseRtlSdr = None  # type: ignore[assignment]
        LibUSBError = IOError  # type: ignore[assignment]
        PYRTLSDR_IMPORT_ERROR = fallback_exc


class _RestartCapture(Exception):
    pass


_NO_CAPTURE_ITEM = object()


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
        self.clients: set[ClientSession] = set()
        self.client_numbers_in_use: set[int] = set()
        self.clients_lock = threading.Lock()
        self.reconfigure_lock = threading.Lock()
        self.pending_device_config: ServerConfig | None = None
        self.pending_device_lock = threading.Lock()
        self.replacement_queue: queue.Queue[
            tuple[ServerConfig, int, BaseRtlSdr, bytes] | Exception
        ] = queue.Queue(maxsize=1)
        self.hotswap_thread: threading.Thread | None = None
        self.deferred_rtl_tuning_config: ServerConfig | None = None

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
        with self.clients_lock:
            clients = list(self.clients)
            self.clients.clear()
            self.client_numbers_in_use.clear()
        for client in clients:
            client.close("server shutdown")
        self._cancel_sdr_async()
        if self.supervisor_thread is not None:
            self.supervisor_thread.join(timeout=2.0)
        self._close_sdr()
        self._close_queued_replacement()

    def _supervise_capture(self) -> None:
        while not self.stop_event.is_set():
            config = self._capture_config()
            with self.sdr_lock:
                sdr = self.sdr
            if sdr is None:
                try:
                    device_index = self._resolve_device(config)
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
                    sdr = self._open_sdr(config, device_index, activate=True)
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
            switched_capture = False
            capture_queue: queue.Queue[bytes | Exception | None] = queue.Queue(maxsize=4)
            reader_stop = threading.Event()
            reader_thread = threading.Thread(
                target=self._sdr_reader_loop,
                args=(sdr, config, capture_queue, reader_stop),
                name="rtl-reader",
                daemon=True,
            )
            reader_thread.start()
            try:
                while not self.stop_event.is_set():
                    self._start_hotswap_worker_if_needed()
                    self._raise_if_replacement_ready(reader_stop, reader_thread, sdr)
                    try:
                        item = capture_queue.get(timeout=self.config.rtl_read_timeout_seconds)
                    except queue.Empty:
                        self._raise_if_replacement_ready(reader_stop, reader_thread, sdr)
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
                    self._raise_if_replacement_ready(reader_stop, reader_thread, sdr)
            except _RestartCapture:
                got_data = True
                switched_capture = True
            except (LibUSBError, IOError, OSError):
                LOGGER.exception("pyrtlsdr reader loop failed")
            except Exception:
                LOGGER.exception("pyrtlsdr reader loop failed")
            finally:
                reader_stop.set()
                self._cancel_specific_sdr_async(sdr)
                reader_thread.join(timeout=2.0)
                self._close_sdr_if_current(sdr)

            if self.stop_event.is_set():
                break
            if switched_capture:
                continue

            if data_timeout:
                LOGGER.info("RTL-SDR stopped producing data; checking whether the device is still connected")
            elif got_data:
                LOGGER.warning(
                    "RTL-SDR stopped after streaming data; checking whether the device is still connected"
                )
            else:
                LOGGER.warning(
                    "RTL-SDR stopped before producing data; checking whether the device is still connected"
                )
            self.stop_event.wait(0.5)

    def _capture_config(self) -> ServerConfig:
        with self.pending_device_lock:
            return self.pending_device_config or self.config

    def _device_identity_changed(
        self,
        current_config: ServerConfig,
        loaded_config: ServerConfig,
    ) -> bool:
        return (
            current_config.rtl_serial != loaded_config.rtl_serial
            or (
                not current_config.rtl_serial
                and not loaded_config.rtl_serial
                and current_config.rtl_device_index != loaded_config.rtl_device_index
            )
        )

    def _queue_device_hotswap(self, loaded_config: ServerConfig) -> None:
        with self.pending_device_lock:
            self.pending_device_config = loaded_config
        LOGGER.info(
            "RTL-SDR device change queued; capture will switch to %s after the replacement starts producing data",
            (
                f"serial {loaded_config.rtl_serial}"
                if loaded_config.rtl_serial
                else f"device index {loaded_config.rtl_device_index}"
            ),
        )
        self._start_hotswap_worker_if_needed()

    def _start_hotswap_worker_if_needed(self) -> None:
        with self.pending_device_lock:
            if self.pending_device_config is None:
                return
            if self.hotswap_thread is not None and self.hotswap_thread.is_alive():
                return
            self.hotswap_thread = threading.Thread(
                target=self._hotswap_worker,
                name="rtl-hotswap",
                daemon=True,
            )
            self.hotswap_thread.start()

    def _hotswap_worker(self) -> None:
        wait_logged = False
        busy_logged = False
        while not self.stop_event.is_set():
            with self.pending_device_lock:
                config = self.pending_device_config
            if config is None:
                return
            try:
                device_index = self._resolve_device(config, validate_index_exists=False)
                sdr = self._open_sdr(config, device_index, activate=False)
                try:
                    chunk = self._read_probe_chunk(sdr, config)
                except Exception:
                    self._close_specific_sdr(sdr)
                    raise
            except DeviceResolutionRetryableError as exc:
                if not wait_logged:
                    LOGGER.warning("%s", exc)
                    wait_logged = True
                self.stop_event.wait(0.5)
                continue
            except DeviceBusyRetryableError as exc:
                if not busy_logged:
                    LOGGER.warning("%s", exc)
                    busy_logged = True
                self.stop_event.wait(0.5)
                continue
            except (DeviceResolutionFatalError, DeviceAccessFatalError) as exc:
                LOGGER.error("RTL-SDR hot-swap refused: %s", exc)
                with self.pending_device_lock:
                    if self.pending_device_config is config:
                        self.pending_device_config = None
                return
            except Exception:
                LOGGER.exception("RTL-SDR hot-swap failed while opening replacement device")
                self.stop_event.wait(0.5)
                continue
            with self.pending_device_lock:
                if self.pending_device_config is not config:
                    self._close_specific_sdr(sdr)
                    return
            self._put_replacement(config, device_index, sdr, chunk)
            return

    def _validate_device_hotswap_request(self, config: ServerConfig) -> None:
        if not config.rtl_serial:
            return
        try:
            usb_devices = self._probe_usb_rtl_devices()
        except OSError as exc:
            LOGGER.warning(
                "USB probe failed during RTL-SDR hot-swap validation (%s); falling back to librtlsdr duplicate checks",
                exc,
            )
        else:
            usb_matches = [device for device in usb_devices if device.serial == config.rtl_serial]
            if len(usb_matches) > 1:
                raise DeviceResolutionFatalError(
                    f"Multiple USB RTL-SDR devices were found with serial {config.rtl_serial}. "
                    "Set rtl_serial to null to use rtl_device_index instead, or assign unique serial numbers "
                    "to each device using rtl_eeprom."
                )
        devices = self._probe_rtl_devices()
        matches = [device for device in devices if device.serial == config.rtl_serial]
        if len(matches) > 1:
            raise DeviceResolutionFatalError(
                f"Multiple RTL-SDR devices were found by pyrtlsdr with serial {config.rtl_serial}. "
                "Set rtl_serial to null to use rtl_device_index instead, or assign unique serial numbers "
                "to each device using rtl_eeprom."
            )

    def _read_probe_chunk(self, sdr: BaseRtlSdr, config: ServerConfig) -> bytes:
        output_queue: queue.Queue[bytes | Exception | None] = queue.Queue(maxsize=1)
        reader_stop = threading.Event()
        reader = threading.Thread(
            target=self._sdr_reader_loop,
            args=(sdr, config, output_queue, reader_stop),
            name="rtl-hotswap-probe",
            daemon=True,
        )
        reader.start()
        try:
            item: bytes | Exception | None | object = _NO_CAPTURE_ITEM
            deadline = time.monotonic() + config.rtl_read_timeout_seconds
            while not self.stop_event.is_set():
                timeout = min(0.25, max(0.0, deadline - time.monotonic()))
                if timeout <= 0:
                    break
                try:
                    item = output_queue.get(timeout=timeout)
                    break
                except queue.Empty:
                    continue
            else:
                raise DeviceResolutionRetryableError(
                    "Server is stopping before the replacement RTL-SDR produced data."
                )
            if item is _NO_CAPTURE_ITEM:
                raise queue.Empty
            if isinstance(item, Exception):
                raise item
            if item is None:
                raise DeviceResolutionRetryableError(
                    "Replacement RTL-SDR stopped before producing data. Waiting for it to start streaming."
                )
            return item
        except queue.Empty as exc:
            raise DeviceResolutionRetryableError(
                "Replacement RTL-SDR produced no data yet. Waiting for it to start streaming."
            ) from exc
        finally:
            reader_stop.set()
            self._cancel_specific_sdr_async(sdr)
            reader.join(timeout=2.0)

    def _put_replacement(
        self,
        config: ServerConfig,
        device_index: int,
        sdr: BaseRtlSdr,
        chunk: bytes,
    ) -> None:
        replacement = (config, device_index, sdr, chunk)
        try:
            self.replacement_queue.put_nowait(replacement)
        except queue.Full:
            old = self.replacement_queue.get_nowait()
            if not isinstance(old, Exception):
                self._close_specific_sdr(old[2])
            self.replacement_queue.put_nowait(replacement)

    def _raise_if_replacement_ready(
        self,
        reader_stop: threading.Event,
        reader_thread: threading.Thread,
        old_sdr: BaseRtlSdr,
    ) -> None:
        try:
            replacement = self.replacement_queue.get_nowait()
        except queue.Empty:
            return
        if isinstance(replacement, Exception):
            raise replacement
        config, device_index, new_sdr, first_chunk = replacement
        with self.pending_device_lock:
            if self.pending_device_config is not config:
                self._close_specific_sdr(new_sdr)
                return
        reader_stop.set()
        self._cancel_specific_sdr_async(old_sdr)
        reader_thread.join(timeout=2.0)
        with self.sdr_lock:
            if self.sdr is old_sdr:
                self.sdr = new_sdr
        self._close_specific_sdr(old_sdr)
        with self.reconfigure_lock:
            self.config = config
            self.graph.apply_runtime_config(
                new_config=config,
                sessions=self._snapshot_client_requests(),
                rebuild_shift_path=True,
                rebuild_decimators=True,
                rebuild_audio_modulations=set(),
            )
        with self.pending_device_lock:
            if self.pending_device_config is config:
                self.pending_device_config = None
        self.graph.feed_raw(first_chunk)
        LOGGER.info("RTL-SDR hot-swap complete on device index %s", device_index)
        raise _RestartCapture

    def get_output_stream(
        self,
        frequency: int,
        mode: str,
        output_rate: int | None,
        sample_format: str | None,
        modulation: str | None,
        audio_codec: str = "pcm",
        opus_bitrate: int = 24_000,
        dsp_config: ServerConfig | None = None,
        validate_audio_support: bool = True,
    ) -> "SharedStream":
        return self.graph.get_output_stream(
            frequency,
            mode,
            output_rate,
            sample_format,
            modulation,
            audio_codec,
            opus_bitrate,
            dsp_config,
            validate_audio_support,
        )

    def get_audio_power_monitor(
        self,
        frequency: int,
        modulation: str | None,
    ) -> "IqPowerMonitor | None":
        return self.graph.get_audio_power_monitor(frequency, modulation)

    def prepare_request(
        self,
        frequency: int,
        mode: str,
        output_rate: int | None,
        modulation: str | None,
        exclude_session: "ClientSession | None" = None,
    ) -> None:
        with self.reconfigure_lock:
            _validate_requested_mode_supported(self.config, mode, modulation)
            required_bandwidth = _get_required_bandwidth(mode, output_rate, modulation)
            if not self.config.automatic_tuning:
                _validate_request_frequency(self.config, frequency, required_bandwidth)
                return

            sessions = self._snapshot_client_requests(exclude_session=exclude_session)
            desired_center = _compute_automatic_center_frequency(
                self.config.rtl_sample_rate,
                [
                    (session.frequency, _get_required_bandwidth(session.mode, session.output_rate, session.modulation))
                    for session in sessions
                ]
                + [(frequency, required_bandwidth)],
            )
            if desired_center == self.config.center_frequency:
                return

            current_config = self.config
            next_config = replace(current_config, center_frequency=desired_center)
            self._apply_runtime_radio_config(current_config, next_config)
            self.graph.apply_runtime_config(
                new_config=next_config,
                sessions=sessions,
                rebuild_shift_path=False,
                rebuild_decimators=False,
                rebuild_audio_modulations=set(),
            )
            self.config = next_config
            LOGGER.info(
                "automatic tuning set center_frequency=%s for %s requested stream(s)",
                desired_center,
                len(sessions) + 1,
            )

    def register_client(self, client: "ClientSession") -> None:
        with self.clients_lock:
            if client.client_number <= 0:
                client.client_number = self._allocate_client_number_locked()
            self.clients.add(client)

    def unregister_client(self, client: "ClientSession") -> None:
        with self.clients_lock:
            self.clients.discard(client)
            if client.client_number > 0:
                self.client_numbers_in_use.discard(client.client_number)
        if self.stop_event.is_set():
            return
        if self.deferred_rtl_tuning_config is not None:
            with self.reconfigure_lock:
                applied_deferred = self._try_apply_deferred_rtl_tuning_locked("client disconnected")
            if applied_deferred:
                return
        if self.config.automatic_tuning:
            self._retune_for_active_clients()

    def active_client_count(self) -> int:
        with self.clients_lock:
            return sum(1 for client in self.clients if not client.closed.is_set())

    def _allocate_client_number_locked(self) -> int:
        client_number = 1
        while client_number in self.client_numbers_in_use:
            client_number += 1
        self.client_numbers_in_use.add(client_number)
        return client_number

    def _retune_for_active_clients(self) -> None:
        with self.reconfigure_lock:
            sessions = self._snapshot_client_requests()
            if not sessions:
                return
            desired_center = _compute_automatic_center_frequency(
                self.config.rtl_sample_rate,
                [
                    (session.frequency, _get_required_bandwidth(session.mode, session.output_rate, session.modulation))
                    for session in sessions
                ],
            )
            if desired_center == self.config.center_frequency:
                return
            current_config = self.config
            next_config = replace(current_config, center_frequency=desired_center)
            self._apply_runtime_radio_config(current_config, next_config)
            self.graph.apply_runtime_config(
                new_config=next_config,
                sessions=sessions,
                rebuild_shift_path=False,
                rebuild_decimators=False,
                rebuild_audio_modulations=set(),
            )
            self.config = next_config
            LOGGER.info(
                "automatic tuning retuned center_frequency=%s for %s active client(s)",
                desired_center,
                len(sessions),
            )

    def reload_config(self, path: Path) -> ServerConfig:
        with self.reconfigure_lock:
            current_config = self.config
            try:
                loaded_config = load_config(path)
            except ValueError as exc:
                LOGGER.error(
                    "config reload rejected; keeping previous live settings: %s",
                    exc,
                )
                return current_config
            device_changed = self._device_identity_changed(current_config, loaded_config)
            if device_changed:
                try:
                    self._validate_device_hotswap_request(loaded_config)
                except (DeviceResolutionFatalError, DeviceAccessFatalError) as exc:
                    LOGGER.error("RTL-SDR hot-swap refused: %s", exc)
                    return loaded_config
            reloadable_fields = {
                "automatic_gain_control",
                "rtl_gain",
                "ppm_correction",
                "bias_tee",
                "dc_block",
                "transition_bandwidth",
                "nfm_deemphasis_tau",
                "nfm_lowpass_frequency",
                "nfm_lowpass_curve",
                "audio_support",
                "am_enabled",
                "lsb_enabled",
                "usb_enabled",
                "nfm_enabled",
                "wfm_enabled",
                "enable_wfm_rds",
                "wfm_region",
            }
            rtl_tuning_live_fields = {
                "automatic_tuning",
                "center_frequency",
                "rtl_sample_rate",
            }
            network_live_fields = {
                "listen_host",
                "listen_port",
            }
            device_live_fields = {
                "rtl_device_index",
                "rtl_serial",
            }
            ignored_fields = [
                field_name
                for field_name in current_config.__dataclass_fields__
                if field_name not in reloadable_fields
                and field_name not in rtl_tuning_live_fields
                and field_name not in network_live_fields
                and field_name not in device_live_fields
                and getattr(loaded_config, field_name) != getattr(current_config, field_name)
            ]
            if ignored_fields:
                LOGGER.warning(
                    "config reload ignored non-live settings that still require a restart: %s",
                    ", ".join(sorted(ignored_fields)),
                )
            immediate_config = current_config
            for field_name in reloadable_fields:
                immediate_config = replace(
                    immediate_config,
                    **{field_name: getattr(loaded_config, field_name)},
                )

            if device_changed:
                immediate_config = replace(
                    immediate_config,
                    rtl_device_index=loaded_config.rtl_device_index,
                    rtl_serial=loaded_config.rtl_serial,
                )

            transition_changed = (
                immediate_config.transition_bandwidth != current_config.transition_bandwidth
            )
            dc_block_changed = immediate_config.dc_block != current_config.dc_block
            rebuild_audio_modulations: set[str] = set()
            if (
                immediate_config.audio_support
                and immediate_config.nfm_enabled
                and (
                    immediate_config.nfm_deemphasis_tau != current_config.nfm_deemphasis_tau
                    or immediate_config.nfm_lowpass_frequency != current_config.nfm_lowpass_frequency
                    or immediate_config.nfm_lowpass_curve != current_config.nfm_lowpass_curve
                )
            ):
                rebuild_audio_modulations.add("nfm")
            if (
                immediate_config.audio_support
                and immediate_config.wfm_enabled
                and immediate_config.wfm_region != current_config.wfm_region
            ):
                rebuild_audio_modulations.update({"wfm", "wfm_stereo"})
            client_requests = self._snapshot_client_requests()
            self._apply_audio_session_config_snapshots(client_requests, immediate_config)

            self._apply_runtime_radio_config(current_config, immediate_config)
            self.graph.apply_runtime_config(
                new_config=immediate_config,
                sessions=client_requests,
                rebuild_shift_path=dc_block_changed,
                rebuild_decimators=transition_changed or dc_block_changed,
                rebuild_audio_modulations=rebuild_audio_modulations,
            )
            self.config = immediate_config
            tuning_config, tuning_errors = self._build_rtl_tuning_config(
                immediate_config,
                loaded_config,
                client_requests,
            )
            if tuning_config is None:
                self.deferred_rtl_tuning_config = loaded_config
                self._log_deferred_rtl_tuning(tuning_errors)
            else:
                tuning_changed = self._rtl_tuning_changed(immediate_config, tuning_config)
                if tuning_changed:
                    tuning_errors = self._find_incompatible_requests(tuning_config, client_requests)
                    if tuning_errors:
                        self.deferred_rtl_tuning_config = loaded_config
                        self._log_deferred_rtl_tuning(tuning_errors)
                    else:
                        self._apply_rtl_tuning_config_locked(
                            tuning_config,
                            client_requests,
                            "config reload",
                        )
                        self.deferred_rtl_tuning_config = None
                else:
                    self.config = tuning_config
                    self.deferred_rtl_tuning_config = None
            for session in client_requests:
                self._refresh_rds_subscription_locked(session)
            if device_changed:
                try:
                    self._queue_device_hotswap(self.config)
                except (DeviceResolutionFatalError, DeviceAccessFatalError) as exc:
                    LOGGER.error("RTL-SDR hot-swap refused: %s", exc)
                    self.config = current_config
                    self.graph.apply_runtime_config(
                        new_config=current_config,
                        sessions=client_requests,
                        rebuild_shift_path=dc_block_changed,
                        rebuild_decimators=transition_changed or dc_block_changed,
                        rebuild_audio_modulations=rebuild_audio_modulations,
                    )
                    return loaded_config
            LOGGER.info("Configuration reload applied")
            LOGGER.debug(
                "config reload applied: center_frequency=%s rtl_sample_rate=%s automatic_gain_control=%s rtl_gain=%s ppm_correction=%s bias_tee=%s dc_block=%s transition_bandwidth=%s nfm_deemphasis_tau=%s wfm_region=%s",
                self.config.center_frequency,
                self.config.rtl_sample_rate,
                self.config.automatic_gain_control,
                self.config.rtl_gain,
                self.config.ppm_correction,
                self.config.bias_tee,
                self.config.dc_block,
                self.config.transition_bandwidth,
                self.config.nfm_deemphasis_tau,
                self.config.wfm_region,
            )
            return loaded_config

    def _snapshot_client_requests(
        self,
        exclude_session: "ClientSession | None" = None,
    ) -> list["ClientSession"]:
        with self.clients_lock:
            return [
                client
                for client in self.clients
                if not client.closed.is_set() and client is not exclude_session
            ]

    @staticmethod
    def _apply_audio_session_config_snapshots(
        sessions: list["ClientSession"],
        loaded_config: ServerConfig,
    ) -> None:
        for session in sessions:
            if session.mode != "audio" or session.modulation is None:
                session.dsp_config = loaded_config
                continue
            if CaptureManager._audio_modulation_enabled_for_existing_session(
                loaded_config,
                session.modulation,
            ):
                session.dsp_config = loaded_config

    @staticmethod
    def _audio_modulation_enabled_for_existing_session(
        config: ServerConfig,
        modulation: str,
    ) -> bool:
        if not config.audio_support:
            return False
        if modulation == "am":
            return config.am_enabled
        if modulation == "lsb":
            return config.lsb_enabled
        if modulation == "usb":
            return config.usb_enabled
        if modulation == "nfm":
            return config.nfm_enabled
        if modulation in {"wfm", "wfm_stereo"}:
            return config.wfm_enabled
        return False

    def reconfigure_client(
        self,
        session: "ClientSession",
        *,
        frequency: int | None = None,
        modulation: str | None = None,
        opus_bitrate: int | None = None,
    ) -> None:
        with self.reconfigure_lock:
            next_frequency = session.frequency if frequency is None else frequency
            next_modulation = session.modulation
            next_output_rate = session.output_rate
            next_sample_format = session.sample_format
            next_audio_codec = session.audio_codec
            next_opus_bitrate = session.opus_bitrate
            next_dsp_config = session.dsp_config

            if opus_bitrate is not None:
                if session.mode != "audio":
                    raise RequestValidationError(
                        EXIT_REQUEST_ERROR,
                        "bitrate command is only supported in audio mode",
                    )
                if session.audio_codec != "opus":
                    raise RequestValidationError(
                        EXIT_REQUEST_ERROR,
                        "bitrate only applies when using the opus codec",
                    )
                next_opus_bitrate = validate_opus_bitrate(opus_bitrate)
                try:
                    probe_opus_encoder(next_opus_bitrate)
                except Exception as exc:
                    raise RequestValidationError(
                        EXIT_REQUEST_ERROR,
                        f"Opus transport is unavailable on the server: {exc}",
                    ) from exc

            if modulation is not None:
                if session.mode != "audio":
                    raise RequestValidationError(
                        EXIT_REQUEST_ERROR,
                        "demod command is only supported in audio mode",
                    )
                _validate_audio_modulation(modulation)
                _validate_audio_modulation_supported(self.config, modulation)
                next_modulation = modulation
                next_output_rate = _get_audio_output_rate(modulation)
                next_sample_format = None
                next_dsp_config = self.config

            required_bandwidth = _get_required_bandwidth(
                session.mode,
                next_output_rate,
                next_modulation,
            )
            if not self.config.automatic_tuning:
                _validate_request_frequency(self.config, next_frequency, required_bandwidth)
                source_stream = self.graph.get_output_stream(
                    next_frequency,
                    session.mode,
                    next_output_rate,
                    next_sample_format,
                    next_modulation,
                    next_audio_codec,
                    next_opus_bitrate,
                    next_dsp_config,
                    False,
                )
                power_monitor = self.graph.get_audio_power_monitor(
                    next_frequency,
                    next_modulation,
                ) if session.mode == "audio" else None
                session.frequency = next_frequency
                session.modulation = next_modulation
                session.output_rate = next_output_rate
                session.sample_format = next_sample_format
                session.audio_codec = next_audio_codec
                session.opus_bitrate = next_opus_bitrate
                session.dsp_config = next_dsp_config
                session.switch_power_monitor(power_monitor)
                session.switch_source_stream(source_stream)
                self._refresh_rds_subscription_locked(session)
                LOGGER.debug(
                    "reconfigured client %s:%s to frequency=%s modulation=%s audio_codec=%s opus_bitrate=%s",
                    session.address[0],
                    session.address[1],
                    next_frequency,
                    next_modulation,
                    next_audio_codec,
                    next_opus_bitrate,
                )
                self._try_apply_deferred_rtl_tuning_locked("client reconfigured")
                return

            remaining_sessions = self._snapshot_client_requests(exclude_session=session)
            desired_center = _compute_automatic_center_frequency(
                self.config.rtl_sample_rate,
                [
                    (client.frequency, _get_required_bandwidth(client.mode, client.output_rate, client.modulation))
                    for client in remaining_sessions
                ] + [(next_frequency, required_bandwidth)],
            )
            current_config = self.config
            next_config = replace(current_config, center_frequency=desired_center)
            source_stream = self.graph.get_output_stream(
                next_frequency,
                session.mode,
                next_output_rate,
                next_sample_format,
                next_modulation,
                next_audio_codec,
                next_opus_bitrate,
                next_dsp_config,
                False,
            ) if desired_center == current_config.center_frequency else None
            power_monitor = self.graph.get_audio_power_monitor(
                next_frequency,
                next_modulation,
            ) if session.mode == "audio" and desired_center == current_config.center_frequency else None

            old_frequency = session.frequency
            old_modulation = session.modulation
            old_output_rate = session.output_rate
            old_sample_format = session.sample_format
            old_audio_codec = session.audio_codec
            old_opus_bitrate = session.opus_bitrate
            old_dsp_config = session.dsp_config
            session.frequency = next_frequency
            session.modulation = next_modulation
            session.output_rate = next_output_rate
            session.sample_format = next_sample_format
            session.audio_codec = next_audio_codec
            session.opus_bitrate = next_opus_bitrate
            session.dsp_config = next_dsp_config
            try:
                if desired_center != current_config.center_frequency:
                    self._apply_runtime_radio_config(current_config, next_config)
                    self.graph.apply_runtime_config(
                        new_config=next_config,
                        sessions=remaining_sessions + [session],
                        rebuild_shift_path=False,
                        rebuild_decimators=False,
                        rebuild_audio_modulations=set(),
                    )
                    self.config = next_config
                    LOGGER.debug(
                        "automatic tuning retuned center_frequency=%s after client %s:%s requested frequency=%s modulation=%s",
                        desired_center,
                        session.address[0],
                        session.address[1],
                        next_frequency,
                        next_modulation,
                    )
                else:
                    assert source_stream is not None
                    session.switch_power_monitor(power_monitor)
                    session.switch_source_stream(source_stream)
                self._refresh_rds_subscription_locked(session)
            except Exception:
                session.frequency = old_frequency
                session.modulation = old_modulation
                session.output_rate = old_output_rate
                session.sample_format = old_sample_format
                session.audio_codec = old_audio_codec
                session.opus_bitrate = old_opus_bitrate
                session.dsp_config = old_dsp_config
                raise
            LOGGER.debug(
                "reconfigured client %s:%s to frequency=%s modulation=%s audio_codec=%s opus_bitrate=%s",
                session.address[0],
                session.address[1],
                next_frequency,
                next_modulation,
                next_audio_codec,
                next_opus_bitrate,
            )
            self._try_apply_deferred_rtl_tuning_locked("client reconfigured")

    def set_rds_subscription(self, session: "ClientSession", enabled: bool) -> None:
        with self.reconfigure_lock:
            if enabled:
                if session.mode != "audio" or session.modulation not in {"wfm", "wfm_stereo"}:
                    raise RequestValidationError(
                        EXIT_REQUEST_ERROR,
                        "RDS is only available in WFM mode",
                    )
                if not self.config.enable_wfm_rds:
                    raise RequestValidationError(
                        EXIT_REQUEST_ERROR,
                        "Server does not support decoding of RDS data",
                    )
                decoder = self.graph.get_rds_decoder(session.frequency)
                if session.rds_decoder is not None and session.rds_decoder is not decoder:
                    session.rds_decoder.remove_subscriber(session)
                session.rds_decoder = decoder
                session.rds_subscribed = True
                decoder.add_subscriber(session)
            else:
                if session.rds_decoder is not None:
                    session.rds_decoder.remove_subscriber(session)
                session.rds_decoder = None
                session.rds_subscribed = False

    def _refresh_rds_subscription_locked(self, session: "ClientSession") -> None:
        if not session.rds_subscribed:
            return
        if session.mode != "audio" or session.modulation not in {"wfm", "wfm_stereo"} or not self.config.enable_wfm_rds:
            if session.rds_decoder is not None:
                session.rds_decoder.remove_subscriber(session)
            session.rds_decoder = None
            session.rds_subscribed = False
            return
        decoder = self.graph.get_rds_decoder(session.frequency)
        if session.rds_decoder is decoder:
            return
        if session.rds_decoder is not None:
            session.rds_decoder.remove_subscriber(session)
        session.rds_decoder = decoder
        decoder.add_subscriber(session)

    def _find_incompatible_requests(
        self,
        config: ServerConfig,
        sessions: list["ClientSession"],
    ) -> list[str]:
        errors: list[str] = []
        for session in sessions:
            try:
                _validate_session_request(
                    config,
                    session,
                    validate_audio_support=session.mode != "audio",
                )
            except RequestValidationError as exc:
                errors.append(
                    f"client {session.address[0]}:{session.address[1]} "
                    f"mode={session.mode} freq={session.frequency} "
                    f"sample_rate={session.output_rate} format={session.sample_format} "
                    f"modulation={session.modulation}: {exc.message}"
                )
        return errors

    def _build_rtl_tuning_config(
        self,
        base_config: ServerConfig,
        desired_config: ServerConfig,
        sessions: list["ClientSession"],
    ) -> tuple[ServerConfig | None, list[str]]:
        candidate = replace(
            base_config,
            automatic_tuning=desired_config.automatic_tuning,
            rtl_sample_rate=desired_config.rtl_sample_rate,
            center_frequency=desired_config.center_frequency,
        )
        if desired_config.automatic_tuning:
            if sessions:
                try:
                    center_frequency = _compute_automatic_center_frequency(
                        desired_config.rtl_sample_rate,
                        [
                            (
                                session.frequency,
                                _get_required_bandwidth(
                                    session.mode,
                                    session.output_rate,
                                    session.modulation,
                                ),
                            )
                            for session in sessions
                        ],
                    )
                except RequestValidationError as exc:
                    return None, [exc.message]
                except ValueError as exc:
                    return None, [str(exc)]
            else:
                center_frequency = base_config.center_frequency
            candidate = replace(candidate, center_frequency=center_frequency)

        errors = self._find_incompatible_requests(candidate, sessions)
        if errors:
            return None, errors
        return candidate, []

    @staticmethod
    def _rtl_tuning_changed(
        current_config: ServerConfig,
        next_config: ServerConfig,
    ) -> bool:
        return (
            next_config.automatic_tuning != current_config.automatic_tuning
            or next_config.center_frequency != current_config.center_frequency
            or next_config.rtl_sample_rate != current_config.rtl_sample_rate
        )

    def _apply_rtl_tuning_config_locked(
        self,
        next_config: ServerConfig,
        sessions: list["ClientSession"],
        reason: str,
    ) -> None:
        current_config = self.config
        tuning_changed = self._rtl_tuning_changed(current_config, next_config)
        center_or_rate_changed = (
            next_config.center_frequency != current_config.center_frequency
            or next_config.rtl_sample_rate != current_config.rtl_sample_rate
        )
        if not tuning_changed:
            self.config = next_config
            return
        self._apply_runtime_radio_config(current_config, next_config)
        self.graph.apply_runtime_config(
            new_config=next_config,
            sessions=sessions,
            rebuild_shift_path=center_or_rate_changed,
            rebuild_decimators=center_or_rate_changed,
            rebuild_audio_modulations=set(),
        )
        self.config = next_config
        LOGGER.info(
            "RTL tuning change applied after %s: automatic_tuning=%s center_frequency=%s rtl_sample_rate=%s",
            reason,
            next_config.automatic_tuning,
            next_config.center_frequency,
            next_config.rtl_sample_rate,
        )

    def _try_apply_deferred_rtl_tuning_locked(self, reason: str) -> bool:
        if self.deferred_rtl_tuning_config is None:
            return False
        sessions = self._snapshot_client_requests()
        candidate, errors = self._build_rtl_tuning_config(
            self.config,
            self.deferred_rtl_tuning_config,
            sessions,
        )
        if candidate is None:
            LOGGER.debug(
                "deferred RTL tuning still cannot be applied after %s: %s",
                reason,
                errors[0] if errors else "active clients are incompatible",
            )
            return False
        self._apply_rtl_tuning_config_locked(candidate, sessions, reason)
        self.deferred_rtl_tuning_config = None
        return True

    @staticmethod
    def _log_deferred_rtl_tuning(errors: list[str]) -> None:
        if errors:
            LOGGER.warning(
                "RTL tuning change deferred until connected clients are compatible: %s",
                errors[0],
            )
            if len(errors) > 1:
                LOGGER.debug(
                    "additional deferred RTL tuning incompatibilities: %s",
                    "; ".join(errors[1:]),
                )
            return
        LOGGER.warning("RTL tuning change deferred until connected clients are compatible")

    def _apply_runtime_radio_config(
        self,
        current_config: ServerConfig,
        next_config: ServerConfig,
    ) -> None:
        with self.sdr_lock:
            sdr = self.sdr
            if sdr is None:
                return
            if next_config.bias_tee != current_config.bias_tee:
                self._set_bias_tee(sdr, next_config.bias_tee)
            gain_mode_changed = (
                next_config.automatic_gain_control != current_config.automatic_gain_control
            )
            gain_value_changed = next_config.rtl_gain != current_config.rtl_gain
            if gain_mode_changed or gain_value_changed:
                if next_config.automatic_gain_control:
                    sdr.gain = "auto"
                else:
                    sdr.gain = next_config.rtl_gain
            if next_config.ppm_correction != current_config.ppm_correction:
                sdr.freq_correction = next_config.ppm_correction
            if next_config.center_frequency != current_config.center_frequency:
                sdr.center_freq = next_config.center_frequency
            if next_config.rtl_sample_rate != current_config.rtl_sample_rate:
                assert rtlsdr_lib is not None
                sdr.sample_rate = next_config.rtl_sample_rate
                result = rtlsdr_lib.rtlsdr_reset_buffer(sdr.dev_p)
                if result < 0:
                    raise LibUSBError(result, "Could not reset buffer after sample-rate change")

    def _open_sdr(
        self,
        config: ServerConfig,
        device_index: int,
        *,
        activate: bool,
    ) -> BaseRtlSdr:
        assert BaseRtlSdr is not None
        assert rtlsdr_lib is not None
        LOGGER.info("Starting RTL-SDR capture on device index %s", device_index)
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
        sdr.sample_rate = config.rtl_sample_rate
        sdr.center_freq = config.center_frequency
        if config.ppm_correction != 0:
            sdr.freq_correction = config.ppm_correction
        self._set_bias_tee(sdr, config.bias_tee)
        sdr.gain = "auto" if config.automatic_gain_control else config.rtl_gain
        result = rtlsdr_lib.rtlsdr_reset_buffer(sdr.dev_p)
        if result < 0:
            sdr.close()
            raise LibUSBError(result, "Could not reset buffer")
        if activate:
            with self.sdr_lock:
                self.sdr = sdr
        return sdr

    @staticmethod
    def _set_bias_tee(sdr: BaseRtlSdr, enabled: bool) -> None:
        set_bias_tee = getattr(sdr, "set_bias_tee", None)
        if set_bias_tee is None:
            if enabled:
                raise RuntimeError(
                    "rtl.bias_tee is true, but this pyrtlsdr installation does not support set_bias_tee"
                )
            return
        set_bias_tee(enabled)
        LOGGER.info("RTL-SDR bias tee %s", "enabled" if enabled else "disabled")

    def _close_sdr(self) -> None:
        with self.sdr_lock:
            sdr = self.sdr
            self.sdr = None
        if sdr is not None:
            self._close_specific_sdr(sdr)

    def _close_sdr_if_current(self, sdr: BaseRtlSdr) -> None:
        should_close = False
        with self.sdr_lock:
            if self.sdr is sdr:
                self.sdr = None
                should_close = True
        if should_close:
            self._close_specific_sdr(sdr)

    def _close_specific_sdr(self, sdr: BaseRtlSdr) -> None:
        if sdr is not None:
            try:
                self._cancel_specific_sdr_async(sdr)
                sdr.close()
            except Exception:
                LOGGER.exception("failed to close pyrtlsdr device")

    def _close_queued_replacement(self) -> None:
        while True:
            try:
                replacement = self.replacement_queue.get_nowait()
            except queue.Empty:
                return
            if not isinstance(replacement, Exception):
                self._close_specific_sdr(replacement[2])

    def _cancel_sdr_async(self) -> None:
        if rtlsdr_lib is None:
            return
        with self.sdr_lock:
            sdr = self.sdr
        self._cancel_specific_sdr_async(sdr)

    @staticmethod
    def _cancel_specific_sdr_async(sdr: BaseRtlSdr | None) -> None:
        if rtlsdr_lib is None:
            return
        if sdr is None:
            return
        try:
            rtlsdr_lib.rtlsdr_cancel_async(sdr.dev_p)
        except Exception:
            LOGGER.debug("failed to cancel RTL-SDR async read", exc_info=True)

    def _sdr_reader_loop(
        self,
        sdr: BaseRtlSdr,
        config: ServerConfig,
        output_queue: queue.Queue[bytes | Exception | None],
        stop_event: threading.Event,
    ) -> None:
        assert rtlsdr_lib is not None
        chunk_size = int(config.read_chunk_size)
        reservoir = bytearray()
        async_done = threading.Event()
        callback_errors: list[Exception] = []

        def should_stop() -> bool:
            return self.stop_event.is_set() or stop_event.is_set()

        def enqueue_capture_item(item: bytes | Exception | None) -> bool:
            while not should_stop():
                try:
                    output_queue.put(item, timeout=0.25)
                    return True
                except queue.Full:
                    continue
            return False

        def cancel_async_read() -> None:
            try:
                rtlsdr_lib.rtlsdr_cancel_async(sdr.dev_p)
            except Exception:
                LOGGER.debug("failed to cancel RTL-SDR async read", exc_info=True)

        def cancel_when_stopped() -> None:
            while not async_done.is_set():
                if stop_event.wait(0.1) or self.stop_event.is_set():
                    cancel_async_read()
                    return

        def async_callback(buffer, length: int, _context) -> None:
            if should_stop():
                return
            try:
                reservoir.extend(ctypes.string_at(buffer, int(length)))
                while len(reservoir) >= chunk_size:
                    chunk = bytes(reservoir[:chunk_size])
                    del reservoir[:chunk_size]
                    if not enqueue_capture_item(chunk):
                        return
            except Exception as exc:
                callback_errors.append(exc)
                cancel_async_read()

        callback = RTLSDR_READ_ASYNC_CALLBACK(async_callback)
        cancel_thread = threading.Thread(
            target=cancel_when_stopped,
            name="rtl-async-cancel",
            daemon=True,
        )
        try:
            cancel_thread.start()
            result = rtlsdr_lib.rtlsdr_read_async(
                sdr.dev_p,
                callback,
                None,
                0,
                0,
            )
            if callback_errors:
                raise callback_errors[0]
            if result < 0 and not should_stop():
                raise LibUSBError(result, "RTL-SDR async read failed")
        except Exception as exc:
            enqueue_capture_item(exc)
        finally:
            async_done.set()
            cancel_thread.join(timeout=1.0)
            enqueue_capture_item(None)

    def _resolve_device(
        self,
        config: ServerConfig,
        *,
        validate_index_exists: bool = True,
    ) -> int:
        if not config.rtl_serial:
            if not validate_index_exists:
                return config.rtl_device_index
            devices = self._probe_rtl_devices()
            if not any(device.index == config.rtl_device_index for device in devices):
                raise DeviceResolutionFatalError(
                    f"Configured rtl_device_index {config.rtl_device_index} does not exist. "
                    "Set rtl_serial to a valid device serial number, or choose an existing device index."
                )
            return config.rtl_device_index

        self._wait_for_unique_usb_serial(config.rtl_serial)
        devices = self._probe_rtl_devices()
        matches = [device for device in devices if device.serial == config.rtl_serial]
        if not matches:
            raise DeviceResolutionRetryableError(
                f"Configured serial {config.rtl_serial} is present on USB but was not yet visible to pyrtlsdr. "
                "Waiting for librtlsdr to detect that device."
            )
        if len(matches) > 1:
            raise DeviceResolutionFatalError(
                f"Multiple RTL-SDR devices were found by pyrtlsdr with serial {config.rtl_serial}. "
                "Set rtl_serial to null to use rtl_device_index instead, or assign unique serial numbers "
                "to each device using rtl_eeprom."
            )

        LOGGER.info(
            "resolved serial %s to rtl_sdr device index %s",
            config.rtl_serial,
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
