#!/usr/bin/env python3
"""
Minimal RTL-SDR + CSDR network server.

The server runs a single wideband RTL-SDR capture path and fans the raw IQ
stream out to per-client CSDR pipelines. Each client sends one JSON line with a
target frequency, output sample rate, and optional output format, then receives
raw IQ data in the requested format.
"""

from __future__ import annotations

import argparse
import ctypes
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
from dataclasses import dataclass, replace
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

PR_SET_NAME = 15

DEFAULT_MODE = "iq"
VALID_MODES = {DEFAULT_MODE, "audio"}
DEFAULT_MODULATION = "am"
VALID_AUDIO_MODULATIONS = {DEFAULT_MODULATION, "lsb", "nfm", "usb", "wfm", "wfm_stereo"}
DEFAULT_SAMPLE_FORMAT = "f32"
VALID_SAMPLE_FORMATS = {DEFAULT_SAMPLE_FORMAT, "s16"}
DEFAULT_STREAM_OUTPUT_READ_SIZE = 65_536
DEFAULT_AUDIO_OUTPUT_FORMAT = "s16"
AM_AUDIO_OUTPUT_RATE = 16_000
AM_AUDIO_TRANSITION_BANDWIDTH = 0.005
AM_AUDIO_AGC_REFERENCE = 0.2
NFM_AUDIO_DEEMPHASIS_TAU = 300
WFM_IQ_RATE = 170_000
WFM_AUDIO_OUTPUT_RATE = 32_000
WFM_AUDIO_TRANSITION_BANDWIDTH = 0.05
WFM_DEEMPHASIS_REGION = "us"


@dataclass(frozen=True)
class ServerConfig:
    rtl_device_index: int = 0
    rtl_serial: str | None = None
    center_frequency: int = 100_000_000
    rtl_sample_rate: int = 2_400_000
    automatic_gain_control: bool = False
    rtl_gain: float | None = None
    ppm_correction: int = 0
    transition_bandwidth: float = 0.05
    audio_support: bool = True
    am_enabled: bool = True
    lsb_enabled: bool = True
    usb_enabled: bool = True
    nfm_enabled: bool = True
    nfm_deemphasis_tau: int | None = NFM_AUDIO_DEEMPHASIS_TAU
    wfm_enabled: bool = True
    enable_wfm_stereo: bool = False
    wfm_deemphasis_region: str = WFM_DEEMPHASIS_REGION
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
        rtl_settings = _get_config_section(data, "rtl")
        server_settings = _get_config_section(data, "server")
        audio_settings = _get_config_section(data, "audio")
        am_settings = _get_config_section(audio_settings, "am")
        lsb_settings = _get_config_section(audio_settings, "lsb")
        usb_settings = _get_config_section(audio_settings, "usb")
        nfm_settings = _get_config_section(audio_settings, "nfm")
        wfm_settings = _get_config_section(audio_settings, "wfm")
        audio_support = _parse_bool(
            audio_settings.get("audio_support", True),
            "audio.audio_support",
        )
        am_enabled = _parse_bool(am_settings.get("enabled", True), "audio.am.enabled")
        lsb_enabled = _parse_bool(lsb_settings.get("enabled", True), "audio.lsb.enabled")
        usb_enabled = _parse_bool(usb_settings.get("enabled", True), "audio.usb.enabled")
        nfm_enabled = _parse_bool(nfm_settings.get("enabled", True), "audio.nfm.enabled")
        wfm_enabled = _parse_bool(wfm_settings.get("enabled", True), "audio.wfm.enabled")
        config = cls(
            rtl_device_index=_parse_int(
                _config_value(data, rtl_settings, "rtl_device_index", 0),
                "rtl.rtl_device_index",
            ),
            rtl_serial=_optional_string(
                _config_value(data, rtl_settings, "rtl_serial")
            ),
            center_frequency=_parse_int(
                _config_value(data, rtl_settings, "center_frequency"),
                "rtl.center_frequency",
            ),
            rtl_sample_rate=_parse_int(
                _config_value(data, rtl_settings, "rtl_sample_rate"),
                "rtl.rtl_sample_rate",
            ),
            automatic_gain_control=_parse_bool(
                _config_value(data, rtl_settings, "automatic_gain_control", False),
                "rtl.automatic_gain_control",
            ),
            rtl_gain=_optional_float(_config_value(data, rtl_settings, "rtl_gain")),
            ppm_correction=_parse_int(
                _config_value(data, rtl_settings, "ppm_correction", 0),
                "rtl.ppm_correction",
            ),
            transition_bandwidth=_parse_float(
                _config_value(data, rtl_settings, "transition_bandwidth"),
                "rtl.transition_bandwidth",
            ),
            audio_support=audio_support,
            am_enabled=am_enabled,
            lsb_enabled=lsb_enabled,
            usb_enabled=usb_enabled,
            nfm_enabled=nfm_enabled,
            nfm_deemphasis_tau=(
                _optional_int(
                    _config_value(
                        audio_settings,
                        nfm_settings,
                        "deemphasis_tau",
                        audio_settings.get("nfm_deemphasis_tau", NFM_AUDIO_DEEMPHASIS_TAU),
                    ),
                    "audio.nfm.deemphasis_tau",
                )
                if audio_support and nfm_enabled
                else NFM_AUDIO_DEEMPHASIS_TAU
            ),
            wfm_enabled=wfm_enabled,
            enable_wfm_stereo=_parse_bool(
                _config_value(
                    audio_settings,
                    wfm_settings,
                    "stereo_support",
                    audio_settings.get("enable_wfm_stereo", False),
                ),
                "audio.wfm.stereo_support",
            ),
            wfm_deemphasis_region=(
                _normalize_wfm_deemphasis_region(
                    _config_value(
                        audio_settings,
                        wfm_settings,
                        "deemphasis_region",
                        audio_settings.get("wfm_deemphasis_region", WFM_DEEMPHASIS_REGION),
                    )
                )
                if audio_support and wfm_enabled
                else WFM_DEEMPHASIS_REGION
            ),
            listen_host=_parse_string(
                _config_value(data, server_settings, "listen_host", "0.0.0.0"),
                "server.listen_host",
            ),
            listen_port=_parse_int(
                _config_value(data, server_settings, "listen_port", 7355),
                "server.listen_port",
            ),
            read_chunk_size=_parse_int(
                _config_value(data, server_settings, "read_chunk_size", 262_144),
                "server.read_chunk_size",
            ),
            rtl_read_timeout_seconds=_parse_float(
                _config_value(data, server_settings, "rtl_read_timeout_seconds", 2.0),
                "server.rtl_read_timeout_seconds",
            ),
            stream_queue_chunks=_parse_int(
                _config_value(data, server_settings, "stream_queue_chunks", 64),
                "server.stream_queue_chunks",
            ),
            client_queue_chunks=_parse_int(
                _config_value(data, server_settings, "client_queue_chunks", 64),
                "server.client_queue_chunks",
            ),
            enqueue_timeout_seconds=_parse_float(
                _config_value(data, server_settings, "enqueue_timeout_seconds", 0.25),
                "server.enqueue_timeout_seconds",
            ),
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
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid rtl_gain: {value!r}") from exc


def _optional_int(value: Any, name: str) -> int | None:
    if value in (None, "", "null"):
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid {name}: {value!r}") from exc


def _parse_int(value: Any, name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid {name}: {value!r}") from exc


def _parse_float(value: Any, name: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid {name}: {value!r}") from exc


def _parse_string(value: Any, name: str) -> str:
    if value is None:
        raise ValueError(f"{name} must not be null")
    text = str(value)
    if not text:
        raise ValueError(f"{name} must not be empty")
    return text


def _parse_bool(value: Any, name: str) -> bool:
    if isinstance(value, bool):
        return value
    raise ValueError(f"{name} must be true or false")


def _normalize_wfm_deemphasis_region(value: Any) -> str:
    return _parse_string(value, "audio.wfm.deemphasis_region").strip().lower()


def _get_config_section(data: dict[str, Any], name: str) -> dict[str, Any]:
    section = data.get(name, {})
    if section is None:
        return {}
    if not isinstance(section, dict):
        raise ValueError(f"{name} must be an object")
    return section


def _config_value(
    root: dict[str, Any],
    section: dict[str, Any],
    key: str,
    default: Any = None,
) -> Any:
    if key in section:
        return section[key]
    if key in root:
        return root[key]
    return default


def _read_sysfs_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip() or None
    except FileNotFoundError:
        return None


def _set_process_name(name: str) -> None:
    try:
        ctypes.CDLL(None).prctl(PR_SET_NAME, name.encode("utf-8")[:15], 0, 0, 0)
    except Exception:
        pass


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
        self.graph.stop("server shutdown")
        with self.clients_lock:
            clients = list(self.clients)
            self.clients.clear()
        for client in clients:
            client.close("server shutdown")
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

    def get_output_stream(
        self,
        frequency: int,
        mode: str,
        output_rate: int | None,
        sample_format: str | None,
        modulation: str | None,
    ) -> "SharedStream":
        return self.graph.get_output_stream(
            frequency,
            mode,
            output_rate,
            sample_format,
            modulation,
        )

    def register_client(self, client: "ClientSession") -> None:
        with self.clients_lock:
            self.clients.add(client)

    def unregister_client(self, client: "ClientSession") -> None:
        with self.clients_lock:
            self.clients.discard(client)

    def reload_config(self, path: Path) -> bool:
        loaded_config = load_config(path)
        current_config = self.config
        reloadable_fields = {
            "automatic_gain_control",
            "rtl_gain",
            "ppm_correction",
            "rtl_sample_rate",
            "center_frequency",
            "transition_bandwidth",
            "nfm_deemphasis_tau",
            "wfm_deemphasis_region",
        }
        ignored_fields = [
            field_name
            for field_name in current_config.__dataclass_fields__
            if field_name not in reloadable_fields
            and getattr(loaded_config, field_name) != getattr(current_config, field_name)
        ]
        if ignored_fields:
            LOGGER.warning(
                "config reload ignored non-live settings that still require a restart: %s",
                ", ".join(sorted(ignored_fields)),
            )
        next_config = current_config
        for field_name in reloadable_fields:
            next_config = replace(
                next_config,
                **{field_name: getattr(loaded_config, field_name)},
            )

        center_or_rate_changed = (
            next_config.center_frequency != current_config.center_frequency
            or next_config.rtl_sample_rate != current_config.rtl_sample_rate
        )
        transition_changed = (
            next_config.transition_bandwidth != current_config.transition_bandwidth
        )
        rebuild_audio_modulations: set[str] = set()
        if next_config.nfm_deemphasis_tau != current_config.nfm_deemphasis_tau:
            rebuild_audio_modulations.add("nfm")
        if next_config.wfm_deemphasis_region != current_config.wfm_deemphasis_region:
            rebuild_audio_modulations.add("wfm")
            rebuild_audio_modulations.add("wfm_stereo")

        client_requests = self._snapshot_client_requests()
        if center_or_rate_changed:
            incompatible_errors = self._find_incompatible_requests(next_config, client_requests)
            if incompatible_errors:
                LOGGER.error(
                    "config reload requires a server restart: %s",
                    incompatible_errors[0],
                )
                if len(incompatible_errors) > 1:
                    LOGGER.error(
                        "additional incompatible client requests: %s",
                        "; ".join(incompatible_errors[1:]),
                    )
                next_config = replace(
                    next_config,
                    center_frequency=current_config.center_frequency,
                    rtl_sample_rate=current_config.rtl_sample_rate,
                )
                center_or_rate_changed = False

        self._apply_runtime_radio_config(current_config, next_config)
        self.graph.apply_runtime_config(
            new_config=next_config,
            sessions=client_requests,
            rebuild_decimators=center_or_rate_changed or transition_changed,
            rebuild_audio_modulations=rebuild_audio_modulations,
        )
        self.config = next_config
        LOGGER.info(
            "config reload applied: center_frequency=%s rtl_sample_rate=%s automatic_gain_control=%s rtl_gain=%s ppm_correction=%s transition_bandwidth=%s nfm_deemphasis_tau=%s wfm_deemphasis_region=%s",
            next_config.center_frequency,
            next_config.rtl_sample_rate,
            next_config.automatic_gain_control,
            next_config.rtl_gain,
            next_config.ppm_correction,
            next_config.transition_bandwidth,
            next_config.nfm_deemphasis_tau,
            next_config.wfm_deemphasis_region,
        )
        return True

    def _snapshot_client_requests(self) -> list["ClientSession"]:
        with self.clients_lock:
            return [client for client in self.clients if not client.closed.is_set()]

    def _find_incompatible_requests(
        self,
        config: ServerConfig,
        sessions: list["ClientSession"],
    ) -> list[str]:
        errors: list[str] = []
        for session in sessions:
            try:
                _validate_session_request(config, session)
            except RequestValidationError as exc:
                errors.append(
                    f"client {session.address[0]}:{session.address[1]} "
                    f"mode={session.mode} freq={session.frequency} "
                    f"sample_rate={session.output_rate} format={session.sample_format} "
                    f"modulation={session.modulation}: {exc.message}"
                )
        return errors

    def _apply_runtime_radio_config(
        self,
        current_config: ServerConfig,
        next_config: ServerConfig,
    ) -> None:
        with self.sdr_lock:
            sdr = self.sdr
            if sdr is None:
                return
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
        if self.config.ppm_correction != 0:
            sdr.freq_correction = self.config.ppm_correction
        sdr.gain = "auto" if self.config.automatic_gain_control else self.config.rtl_gain
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
                with self.sdr_lock:
                    chunk = sdr.read_bytes(self.config.read_chunk_size)
                output_queue.put(chunk)
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
        control_fifo_value: str | None = None,
        control_fifo_path: Path | None = None,
        output_read_size: int = DEFAULT_STREAM_OUTPUT_READ_SIZE,
    ) -> None:
        self.config = config
        self.name = name
        self.command = command
        self.manager = manager
        self.parent = parent
        self.close_when_unused = close_when_unused
        self.control_fifo_value = control_fifo_value
        self.control_fifo_path = control_fifo_path
        self.output_read_size = output_read_size
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
        self.control_fifo_fd: int | None = None

    def start(self) -> None:
        self._setup_control_fifo()
        try:
            self.process = subprocess.Popen(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                start_new_session=True,
            )
            self._send_initial_control_value()
        except Exception:
            self._cleanup_control_fifo()
            raise
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
        self._cleanup_control_fifo()

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
                data = self.process.stdout.read(self.output_read_size)
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

    def _setup_control_fifo(self) -> None:
        if self.control_fifo_value is None:
            return
        runtime_dir = Path("/run/user") / str(os.getuid())
        runtime_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        fifo_path = self.control_fifo_path or (runtime_dir / f"csdr_server_{self.name}_{os.getpid()}.fifo")
        if fifo_path.exists():
            fifo_path.unlink()
        os.mkfifo(fifo_path, 0o600)
        self.control_fifo_path = fifo_path
        self.control_fifo_fd = os.open(fifo_path, os.O_RDWR | os.O_NONBLOCK)

    def _send_initial_control_value(self) -> None:
        if self.control_fifo_value is None or self.control_fifo_fd is None:
            return
        self.send_control_value(self.control_fifo_value)

    def send_control_value(self, value: str) -> None:
        if self.control_fifo_fd is None:
            return
        payload = f"{value}\n".encode("utf-8")
        os.write(self.control_fifo_fd, payload)
        self.control_fifo_value = value

    def _cleanup_control_fifo(self) -> None:
        if self.control_fifo_fd is not None:
            try:
                os.close(self.control_fifo_fd)
            except OSError:
                pass
            self.control_fifo_fd = None
        if self.control_fifo_path is not None:
            try:
                self.control_fifo_path.unlink(missing_ok=True)
            except OSError:
                pass
            self.control_fifo_path = None


class StreamGraph:
    def __init__(self, config: ServerConfig) -> None:
        self.config = config
        self.lock = threading.Lock()
        self.root_stream: SharedStream | None = None
        self.shift_streams: dict[int, SharedStream] = {}
        self.decimation_streams: dict[tuple[int, int, float, str], SharedStream] = {}
        self.format_streams: dict[tuple[int, int, str], Any] = {}
        self.audio_streams: dict[tuple[int, str], SharedStream] = {}

    def stop(self, reason: str) -> None:
        with self.lock:
            root = self.root_stream
            audio = list(self.audio_streams.values())
            formats = list(self.format_streams.values())
            shifts = list(self.shift_streams.values())
            decimations = list(self.decimation_streams.values())
            self.root_stream = None
            self.audio_streams = {}
            self.format_streams = {}
            self.shift_streams = {}
            self.decimation_streams = {}
        for stream in audio:
            stream.close(reason, propagate=True)
        for stream in formats:
            stream.close(reason, propagate=True)
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

    def get_output_stream(
        self,
        frequency: int,
        mode: str,
        output_rate: int | None,
        sample_format: str | None,
        modulation: str | None,
    ) -> SharedStream:
        with self.lock:
            return self._get_output_stream_locked(
                frequency,
                mode,
                output_rate,
                sample_format,
                modulation,
            )

    def apply_runtime_config(
        self,
        new_config: ServerConfig,
        sessions: list["ClientSession"],
        rebuild_decimators: bool,
        rebuild_audio_modulations: set[str],
    ) -> None:
        old_decimation_streams: list[SharedStream] = []
        old_format_streams: list[Any] = []
        old_audio_streams: list[SharedStream] = []
        stream_switches: list[tuple[ClientSession, SharedStream]] = []
        with self.lock:
            self.config = new_config
            if self.root_stream is not None:
                self.root_stream.config = new_config
            for frequency, stream in self.shift_streams.items():
                stream.config = new_config
                stream.send_control_value(
                    str((new_config.center_frequency - frequency) / new_config.rtl_sample_rate)
                )

            if rebuild_decimators:
                old_decimation_streams = list(self.decimation_streams.values())
                old_format_streams = list(self.format_streams.values())
                old_audio_streams = list(self.audio_streams.values())
                self.decimation_streams = {}
                self.format_streams = {}
                self.audio_streams = {}
            elif rebuild_audio_modulations:
                old_audio_streams = [
                    stream
                    for key, stream in self.audio_streams.items()
                    if key[1] in rebuild_audio_modulations
                ]
                self.audio_streams = {
                    key: stream
                    for key, stream in self.audio_streams.items()
                    if key[1] not in rebuild_audio_modulations
                }

            for session in sessions:
                session.config = new_config
                stream_switches.append(
                    (
                        session,
                        self._get_output_stream_locked(
                            session.frequency,
                            session.mode,
                            session.output_rate,
                            session.sample_format,
                            session.modulation,
                        ),
                    )
                )

        for session, desired_stream in stream_switches:
            session.switch_source_stream(desired_stream)

        for stream in old_audio_streams:
            stream.close("reconfigured audio stream", propagate=False)
        for stream in old_format_streams:
            stream.close("reconfigured output format", propagate=False)
        for stream in old_decimation_streams:
            stream.close("reconfigured decimator", propagate=False)

    def _get_output_stream_locked(
        self,
        frequency: int,
        mode: str,
        output_rate: int | None,
        sample_format: str | None,
        modulation: str | None,
    ) -> SharedStream:
        _validate_mode(mode)
        required_bandwidth = _get_required_bandwidth(mode, output_rate, modulation)
        _validate_request_frequency(self.config, frequency, required_bandwidth)

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
            LOGGER.debug("created shared root stream convert")
        else:
            root.config = self.config
            LOGGER.debug("reusing shared root stream convert")

        shift_stream = self.shift_streams.get(frequency)
        if shift_stream is None:
            shift_rate = (self.config.center_frequency - frequency) / self.config.rtl_sample_rate
            shift_name = f"shift-{frequency}"
            shift_fifo_path = Path("/run/user") / str(os.getuid()) / f"csdr_server_{shift_name}_{os.getpid()}.fifo"
            shift_stream = SharedStream(
                config=self.config,
                name=shift_name,
                command=["csdr", "shift", "--fifo", str(shift_fifo_path)],
                manager=self,
                parent=root,
                close_when_unused=True,
                control_fifo_value=str(shift_rate),
                control_fifo_path=shift_fifo_path,
            )
            shift_stream.start()
            self.shift_streams[frequency] = shift_stream
            LOGGER.debug(
                "created shared shift stream for frequency=%s shift_rate=%s",
                frequency,
                shift_rate,
            )
        else:
            shift_stream.config = self.config
            LOGGER.debug("reusing shared shift stream for frequency=%s", frequency)

        if mode == "iq":
            assert output_rate is not None
            assert sample_format is not None
            _validate_sample_format(sample_format)
            base_stream = self._get_decimation_stream_locked(
                frequency,
                output_rate,
                self.config.transition_bandwidth,
                shift_stream,
            )
            if sample_format == DEFAULT_SAMPLE_FORMAT:
                return base_stream

            format_key = (frequency, output_rate, sample_format)
            format_stream = self.format_streams.get(format_key)
            if format_stream is None:
                format_stream = _build_output_format_stream(
                    config=self.config,
                    frequency=frequency,
                    output_rate=output_rate,
                    sample_format=sample_format,
                    manager=self,
                    parent=base_stream,
                )
                format_stream.start()
                self.format_streams[format_key] = format_stream
                LOGGER.debug(
                    "created shared IQ format stream frequency=%s output_rate=%s format=%s",
                    frequency,
                    output_rate,
                    sample_format,
                )
            else:
                format_stream.config = self.config
                LOGGER.debug(
                    "reusing shared IQ format stream frequency=%s output_rate=%s format=%s",
                    frequency,
                    output_rate,
                    sample_format,
                )
            return format_stream

        assert modulation is not None
        _validate_audio_modulation(modulation)
        _validate_audio_modulation_supported(self.config, modulation)
        audio_iq_rate = _get_audio_iq_rate(modulation)
        audio_transition_bandwidth = _get_audio_transition_bandwidth(modulation)
        base_stream = self._get_decimation_stream_locked(
            frequency,
            audio_iq_rate,
            audio_transition_bandwidth,
            shift_stream,
        )
        audio_key = (frequency, modulation)
        audio_stream = self.audio_streams.get(audio_key)
        if audio_stream is None:
            audio_stream = _build_audio_stream(
                config=self.config,
                frequency=frequency,
                modulation=modulation,
                manager=self,
                parent=self._get_audio_demod_parent_locked(frequency, modulation, base_stream),
            )
            self.audio_streams[audio_key] = audio_stream
            LOGGER.debug(
                "created shared audio stream frequency=%s modulation=%s",
                frequency,
                modulation,
            )
        else:
            audio_stream.config = self.config
            LOGGER.debug(
                "reusing shared audio stream frequency=%s modulation=%s",
                frequency,
                modulation,
            )
        return audio_stream

    def _get_audio_demod_parent_locked(
        self,
        frequency: int,
        modulation: str,
        parent: SharedStream,
    ) -> SharedStream:
        if modulation not in {"wfm", "wfm_stereo"}:
            return parent
        demod_key = (frequency, "wfm_shared_fmdemod")
        demod_stream = self.audio_streams.get(demod_key)
        if demod_stream is None:
            demod_stream = SharedStream(
                config=self.config,
                name=f"audio-wfm-{frequency}-fmdemod",
                command=["csdr", "fmdemod"],
                manager=self,
                parent=parent,
                close_when_unused=True,
            )
            demod_stream.start()
            self.audio_streams[demod_key] = demod_stream
            LOGGER.debug("created shared WFM demod stream for frequency=%s", frequency)
        else:
            demod_stream.config = self.config
            LOGGER.debug("reusing shared WFM demod stream for frequency=%s", frequency)
        return demod_stream

    def _get_decimation_stream_locked(
        self,
        frequency: int,
        output_rate: int,
        transition_bandwidth: float,
        parent: SharedStream,
    ) -> SharedStream:
        _validate_output_rate(self.config.rtl_sample_rate, output_rate)
        strategy = _get_decimation_strategy(self.config.rtl_sample_rate, output_rate)
        if strategy == "identity":
            return parent
        key = (frequency, output_rate, transition_bandwidth, strategy)
        decimation_stream = self.decimation_streams.get(key)
        if decimation_stream is None:
            if strategy == "integer":
                decimation = _compute_integer_decimation(
                    self.config.rtl_sample_rate,
                    output_rate,
                )
                decimation_stream = SharedStream(
                    config=self.config,
                    name=f"firdecimate-{frequency}-{output_rate}-{transition_bandwidth}",
                    command=[
                        "csdr",
                        "firdecimate",
                        str(decimation),
                        _format_csdr_float(transition_bandwidth),
                    ],
                    manager=self,
                    parent=parent,
                    close_when_unused=True,
                )
                decimation_stream.start()
                LOGGER.debug(
                    "created shared integer decimation stream frequency=%s output_rate=%s transition_bandwidth=%s decimation=%s",
                    frequency,
                    output_rate,
                    transition_bandwidth,
                    decimation,
                )
            else:
                decimation_ratio = _compute_fractional_decimation_ratio(
                    self.config.rtl_sample_rate,
                    output_rate,
                )
                decimation_stream = SharedStream(
                    config=self.config,
                    name=f"fractionaldecimator-{frequency}-{output_rate}-{transition_bandwidth}",
                    command=[
                        "csdr",
                        "fractionaldecimator",
                        "-f",
                        "complex",
                        "--prefilter",
                        "--transition",
                        _format_csdr_float(transition_bandwidth),
                        _format_csdr_float(decimation_ratio),
                    ],
                    manager=self,
                    parent=parent,
                    close_when_unused=True,
                )
                decimation_stream.start()
                LOGGER.debug(
                    "created shared fractional decimation stream frequency=%s output_rate=%s transition_bandwidth=%s decimation_ratio=%s",
                    frequency,
                    output_rate,
                    transition_bandwidth,
                    decimation_ratio,
                )
            self.decimation_streams[key] = decimation_stream
        else:
            decimation_stream.config = self.config
            LOGGER.debug(
                "reusing shared %s decimation stream frequency=%s output_rate=%s transition_bandwidth=%s",
                strategy,
                frequency,
                output_rate,
                transition_bandwidth,
            )
        return decimation_stream

    def on_stream_closed(self, stream: SharedStream) -> None:
        with self.lock:
            if self.root_stream is stream:
                self.root_stream = None
            for key, candidate in list(self.audio_streams.items()):
                if candidate is stream:
                    del self.audio_streams[key]
            for key, candidate in list(self.format_streams.items()):
                if candidate is stream:
                    del self.format_streams[key]
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
        manager: CaptureManager,
        config: ServerConfig,
        source_stream: SharedStream,
        frequency: int,
        mode: str,
        output_rate: int,
        sample_format: str | None,
        modulation: str | None,
    ) -> None:
        self.conn = conn
        self.address = address
        self.manager = manager
        self.config = config
        self.source_stream = source_stream
        self.frequency = frequency
        self.mode = mode
        self.output_rate = output_rate
        self.sample_format = sample_format
        self.modulation = modulation
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
            "client %s:%s started mode=%s freq=%s sample_rate=%s format=%s modulation=%s",
            self.address[0],
            self.address[1],
            self.mode,
            self.frequency,
            self.output_rate,
            self.sample_format,
            self.modulation,
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
        self.manager.unregister_client(self)
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

    def switch_source_stream(self, new_stream: SharedStream) -> None:
        if self.closed.is_set() or new_stream is self.source_stream:
            return
        old_stream = self.source_stream
        new_stream.add_subscriber(self)
        self.source_stream = new_stream
        old_stream.remove_subscriber(self)
        LOGGER.info(
            "client %s:%s switched to stream %s",
            self.address[0],
            self.address[1],
            new_stream.name,
        )


def _validate_output_rate(input_rate: int, output_rate: int) -> None:
    if output_rate <= 0:
        raise RequestValidationError(EXIT_BAD_SAMPLE_RATE, "output sample rate must be positive")
    if output_rate > input_rate:
        raise RequestValidationError(
            EXIT_BAD_SAMPLE_RATE,
            "output sample rate cannot exceed rtl sample rate",
        )


def _get_decimation_strategy(input_rate: int, output_rate: int) -> str:
    _validate_output_rate(input_rate, output_rate)
    if input_rate == output_rate:
        return "identity"
    if input_rate % output_rate == 0:
        return "integer"
    return "fractional"


def _compute_integer_decimation(input_rate: int, output_rate: int) -> int:
    _validate_output_rate(input_rate, output_rate)
    if input_rate % output_rate != 0:
        raise RequestValidationError(
            EXIT_BAD_SAMPLE_RATE,
            f"rtl sample rate {input_rate} is not an integer multiple of requested "
            f"sample rate {output_rate}",
        )
    return input_rate // output_rate


def _compute_fractional_decimation_ratio(input_rate: int, output_rate: int) -> float:
    _validate_output_rate(input_rate, output_rate)
    return float(input_rate) / float(output_rate)


def _format_csdr_float(value: float) -> str:
    return format(value, ".12g")


def _normalize_sample_format(value: Any) -> str:
    return str(value).strip().lower()


def _normalize_mode(value: Any) -> str:
    return str(value).strip().lower()


def _normalize_audio_modulation(value: Any) -> str:
    return str(value).strip().lower().replace("-", "_")


def _validate_mode(mode: str) -> None:
    if mode not in VALID_MODES:
        raise RequestValidationError(
            EXIT_REQUEST_ERROR,
            f"unsupported mode {mode!r}; expected one of {', '.join(sorted(VALID_MODES))}",
        )


def _validate_sample_format(sample_format: str) -> None:
    if sample_format not in VALID_SAMPLE_FORMATS:
        raise RequestValidationError(
            EXIT_REQUEST_ERROR,
            f"unsupported sample format {sample_format!r}; expected one of "
            f"{', '.join(sorted(VALID_SAMPLE_FORMATS))}",
        )


def _validate_audio_modulation(modulation: str) -> None:
    if modulation not in VALID_AUDIO_MODULATIONS:
        raise RequestValidationError(
            EXIT_REQUEST_ERROR,
            "unsupported audio modulation "
            f"{modulation!r}; expected one of {', '.join(sorted(VALID_AUDIO_MODULATIONS))}",
        )


def _get_audio_output_rate(modulation: str) -> int:
    if modulation in {"wfm", "wfm_stereo"}:
        return WFM_AUDIO_OUTPUT_RATE
    return AM_AUDIO_OUTPUT_RATE


def _get_audio_iq_rate(modulation: str) -> int:
    if modulation in {"wfm", "wfm_stereo"}:
        return WFM_IQ_RATE
    return AM_AUDIO_OUTPUT_RATE


def _get_audio_transition_bandwidth(modulation: str) -> float:
    if modulation in {"wfm", "wfm_stereo"}:
        return WFM_AUDIO_TRANSITION_BANDWIDTH
    return AM_AUDIO_TRANSITION_BANDWIDTH


def _validate_audio_modulation_supported(config: ServerConfig, modulation: str) -> None:
    if not config.audio_support:
        raise RequestValidationError(
            EXIT_REQUEST_ERROR,
            "Audio mode is disabled on this server",
        )
    if modulation == "am" and not config.am_enabled:
        raise RequestValidationError(
            EXIT_REQUEST_ERROR,
            "AM support is disabled on this server",
        )
    if modulation == "lsb" and not config.lsb_enabled:
        raise RequestValidationError(
            EXIT_REQUEST_ERROR,
            "LSB support is disabled on this server",
        )
    if modulation == "usb" and not config.usb_enabled:
        raise RequestValidationError(
            EXIT_REQUEST_ERROR,
            "USB support is disabled on this server",
        )
    if modulation == "nfm" and not config.nfm_enabled:
        raise RequestValidationError(
            EXIT_REQUEST_ERROR,
            "NFM support is disabled on this server",
        )
    if modulation in {"wfm", "wfm_stereo"} and not config.wfm_enabled:
        raise RequestValidationError(
            EXIT_REQUEST_ERROR,
            f"{modulation.upper()} support is disabled on this server",
        )
    if modulation == "wfm_stereo" and not config.enable_wfm_stereo:
        raise RequestValidationError(
            EXIT_REQUEST_ERROR,
            "Server does not support WFM stereo",
        )


def _get_wfm_deemphasis_tau(region: str) -> int:
    normalized_region = region.strip().lower()
    if normalized_region == "us":
        return 75
    if normalized_region == "europe":
        return 50
    raise ValueError(
        "audio.wfm.deemphasis_region must be either 'us' or 'europe'"
    )


def _build_output_format_stream(
    config: ServerConfig,
    frequency: int,
    output_rate: int,
    sample_format: str,
    manager: "StreamGraph",
    parent: SharedStream,
) -> SharedStream:
    if sample_format == "s16":
        return SharedStream(
            config=config,
            name=f"{sample_format}-{frequency}-{output_rate}",
            command=["csdr", "convert", "-i", "float", "-o", "s16"],
            manager=manager,
            parent=parent,
            close_when_unused=True,
            output_read_size=_compute_output_read_size(sample_format, output_rate),
        )
    raise ValueError(f"unsupported sample format {sample_format!r}")


def _build_audio_stream(
    config: ServerConfig,
    frequency: int,
    modulation: str,
    manager: "StreamGraph",
    parent: SharedStream,
) -> SharedStream:
    started_streams: list[SharedStream] = []

    if modulation == "am":
        demod_stream = SharedStream(
            config=config,
            name=f"audio-{modulation}-{frequency}-amdemod",
            command=["csdr", "amdemod"],
            manager=manager,
            parent=parent,
            close_when_unused=True,
        )
        dcblock_stream = SharedStream(
            config=config,
            name=f"audio-{modulation}-{frequency}-dcblock",
            command=["csdr", "dcblock"],
            manager=manager,
            parent=demod_stream,
            close_when_unused=True,
        )
        agc_stream = SharedStream(
            config=config,
            name=f"audio-{modulation}-{frequency}-agc",
            command=["csdr", "agc", "-r", str(AM_AUDIO_AGC_REFERENCE)],
            manager=manager,
            parent=dcblock_stream,
            close_when_unused=True,
        )
        output_stream = SharedStream(
            config=config,
            name=f"audio-{modulation}-{frequency}",
            command=["csdr", "convert", "-i", "float", "-o", "s16"],
            manager=manager,
            parent=agc_stream,
            close_when_unused=True,
            output_read_size=_compute_output_read_size("s16", _get_audio_output_rate(modulation)),
        )
        try:
            demod_stream.start()
            started_streams.append(demod_stream)
            dcblock_stream.start()
            started_streams.append(dcblock_stream)
            agc_stream.start()
            started_streams.append(agc_stream)
            output_stream.start()
            started_streams.append(output_stream)
        except Exception:
            for stream in reversed(started_streams):
                stream.close("audio stream startup failed", propagate=False)
            raise
        return output_stream

    if modulation in {"usb", "lsb"}:
        if modulation == "usb":
            bandpass_command = [
                "csdr",
                "bandpass",
                "--fft",
                "--low",
                "0",
                "--high",
                "0.3",
                "0.05",
            ]
        else:
            bandpass_command = [
                "csdr",
                "bandpass",
                "--fft",
                "--low",
                "0.3",
                "--high",
                "0",
                "0.05",
            ]

        bandpass_stream = SharedStream(
            config=config,
            name=f"audio-{modulation}-{frequency}-bandpass",
            command=bandpass_command,
            manager=manager,
            parent=parent,
            close_when_unused=True,
        )
        realpart_stream = SharedStream(
            config=config,
            name=f"audio-{modulation}-{frequency}-realpart",
            command=["csdr", "realpart"],
            manager=manager,
            parent=bandpass_stream,
            close_when_unused=True,
        )
        agc_stream = SharedStream(
            config=config,
            name=f"audio-{modulation}-{frequency}-agc",
            command=["csdr", "agc", "-r", str(AM_AUDIO_AGC_REFERENCE)],
            manager=manager,
            parent=realpart_stream,
            close_when_unused=True,
        )
        output_stream = SharedStream(
            config=config,
            name=f"audio-{modulation}-{frequency}",
            command=["csdr", "convert", "-i", "float", "-o", "s16"],
            manager=manager,
            parent=agc_stream,
            close_when_unused=True,
            output_read_size=_compute_output_read_size("s16", _get_audio_output_rate(modulation)),
        )
        try:
            bandpass_stream.start()
            started_streams.append(bandpass_stream)
            realpart_stream.start()
            started_streams.append(realpart_stream)
            agc_stream.start()
            started_streams.append(agc_stream)
            output_stream.start()
            started_streams.append(output_stream)
        except Exception:
            for stream in reversed(started_streams):
                stream.close("audio stream startup failed", propagate=False)
            raise
        return output_stream

    if modulation == "nfm":
        demod_stream = SharedStream(
            config=config,
            name=f"audio-{modulation}-{frequency}-fmdemod",
            command=["csdr", "fmdemod"],
            manager=manager,
            parent=parent,
            close_when_unused=True,
        )
        nfm_parent: SharedStream = demod_stream
        deemphasis_stream: SharedStream | None = None
        if config.nfm_deemphasis_tau is not None:
            deemphasis_stream = SharedStream(
                config=config,
                name=f"audio-{modulation}-{frequency}-deemphasis",
                command=[
                    "csdr",
                    "deemphasis",
                    "--wfm",
                    str(_get_audio_output_rate(modulation)),
                    f"{config.nfm_deemphasis_tau}e-6",
                ],
                manager=manager,
                parent=demod_stream,
                close_when_unused=True,
            )
            nfm_parent = deemphasis_stream
        dcblock_stream = SharedStream(
            config=config,
            name=f"audio-{modulation}-{frequency}-dcblock",
            command=["csdr", "dcblock"],
            manager=manager,
            parent=nfm_parent,
            close_when_unused=True,
        )
        output_stream = SharedStream(
            config=config,
            name=f"audio-{modulation}-{frequency}",
            command=["csdr", "convert", "-i", "float", "-o", "s16"],
            manager=manager,
            parent=dcblock_stream,
            close_when_unused=True,
            output_read_size=_compute_output_read_size("s16", _get_audio_output_rate(modulation)),
        )
        try:
            demod_stream.start()
            started_streams.append(demod_stream)
            if deemphasis_stream is not None:
                deemphasis_stream.start()
                started_streams.append(deemphasis_stream)
            dcblock_stream.start()
            started_streams.append(dcblock_stream)
            output_stream.start()
            started_streams.append(output_stream)
        except Exception:
            for stream in reversed(started_streams):
                stream.close("audio stream startup failed", propagate=False)
            raise
        return output_stream

    if modulation == "wfm":
        audio_decimation_ratio = _compute_fractional_decimation_ratio(
            WFM_IQ_RATE,
            WFM_AUDIO_OUTPUT_RATE,
        )
        audio_resample_stream = SharedStream(
            config=config,
            name=f"audio-{modulation}-{frequency}-fractionaldecimator",
            command=[
                "csdr",
                "fractionaldecimator",
                "--format",
                "float",
                "--prefilter",
                _format_csdr_float(audio_decimation_ratio),
            ],
            manager=manager,
            parent=parent,
            close_when_unused=True,
        )
        deemphasis_tau = _get_wfm_deemphasis_tau(config.wfm_deemphasis_region)
        deemphasis_stream = SharedStream(
            config=config,
            name=f"audio-{modulation}-{frequency}-deemphasis",
            command=[
                "csdr",
                "deemphasis",
                "--wfm",
                str(WFM_AUDIO_OUTPUT_RATE),
                f"{deemphasis_tau}e-6",
            ],
            manager=manager,
            parent=audio_resample_stream,
            close_when_unused=True,
        )
        output_stream = SharedStream(
            config=config,
            name=f"audio-{modulation}-{frequency}",
            command=["csdr", "convert", "-i", "float", "-o", "s16"],
            manager=manager,
            parent=deemphasis_stream,
            close_when_unused=True,
            output_read_size=_compute_output_read_size("s16", _get_audio_output_rate(modulation)),
        )
        try:
            audio_resample_stream.start()
            started_streams.append(audio_resample_stream)
            deemphasis_stream.start()
            started_streams.append(deemphasis_stream)
            output_stream.start()
            started_streams.append(output_stream)
        except Exception:
            for stream in reversed(started_streams):
                stream.close("audio stream startup failed", propagate=False)
            raise
        return output_stream

    if modulation == "wfm_stereo":
        pcm_stream = SharedStream(
            config=config,
            name=f"audio-{modulation}-{frequency}-s16",
            command=["csdr", "convert", "-i", "float", "-o", "s16"],
            manager=manager,
            parent=parent,
            close_when_unused=True,
        )
        deemphasis_tau = _get_wfm_deemphasis_tau(config.wfm_deemphasis_region)
        output_stream = SharedStream(
            config=config,
            name=f"audio-{modulation}-{frequency}",
            command=[
                "demux",
                "-r",
                str(WFM_IQ_RATE),
                "-R",
                str(WFM_AUDIO_OUTPUT_RATE),
                "-d",
                str(deemphasis_tau),
            ],
            manager=manager,
            parent=pcm_stream,
            close_when_unused=True,
            output_read_size=_compute_output_read_size("s16", _get_audio_output_rate(modulation)),
        )
        try:
            pcm_stream.start()
            started_streams.append(pcm_stream)
            output_stream.start()
            started_streams.append(output_stream)
        except Exception:
            for stream in reversed(started_streams):
                stream.close("audio stream startup failed", propagate=False)
            raise
        return output_stream

    raise ValueError(f"unsupported audio modulation {modulation!r}")


def _compute_output_read_size(sample_format: str, output_rate: int) -> int:
    bytes_per_complex_sample = {
        "f32": 8,
        "s16": 4,
    }[sample_format]
    target_ms = 100
    size = int((output_rate * bytes_per_complex_sample * target_ms) / 1000)
    return max(4096, min(DEFAULT_STREAM_OUTPUT_READ_SIZE, size))


def _get_required_bandwidth(
    mode: str,
    output_rate: int | None,
    modulation: str | None,
) -> int:
    if mode == "iq":
        if output_rate is None:
            raise RequestValidationError(
                EXIT_REQUEST_ERROR,
                "iq request is missing output sample rate",
            )
        return output_rate
    if modulation is None:
        raise RequestValidationError(
            EXIT_REQUEST_ERROR,
            "audio request is missing modulation",
        )
    return _get_audio_iq_rate(modulation)


def _validate_request_frequency(
    config: ServerConfig,
    frequency: int,
    required_bandwidth: int,
) -> None:
    half_capture_bandwidth = config.rtl_sample_rate / 2.0
    half_required_bandwidth = required_bandwidth / 2.0
    center_offset = abs(config.center_frequency - frequency)
    if center_offset + half_required_bandwidth > half_capture_bandwidth:
        raise RequestValidationError(
            EXIT_OUT_OF_BAND,
            "requested frequency is out of band for the current RTL capture window",
        )


def _validate_session_request(config: ServerConfig, session: "ClientSession") -> None:
    required_bandwidth = _get_required_bandwidth(
        session.mode,
        session.output_rate,
        session.modulation,
    )
    _validate_request_frequency(config, session.frequency, required_bandwidth)
    if session.mode == "iq":
        if session.output_rate is None or session.sample_format is None:
            raise RequestValidationError(
                EXIT_REQUEST_ERROR,
                "iq session is missing output sample rate or format",
            )
        _validate_sample_format(session.sample_format)
        _validate_output_rate(config.rtl_sample_rate, session.output_rate)
        return
    if session.mode == "audio":
        if session.modulation is None:
            raise RequestValidationError(
                EXIT_REQUEST_ERROR,
                "audio session is missing modulation",
            )
        _validate_audio_modulation(session.modulation)
        _validate_audio_modulation_supported(config, session.modulation)
        _validate_output_rate(config.rtl_sample_rate, _get_audio_iq_rate(session.modulation))
        return
    raise RequestValidationError(
        EXIT_REQUEST_ERROR,
        f"unsupported session mode {session.mode!r}",
    )


def load_config(path: Path) -> ServerConfig:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return ServerConfig.from_dict(raw)


def _validate_config(config: ServerConfig) -> None:
    _validate_rtl_device_index(config.rtl_device_index)
    _validate_rtl_serial(config.rtl_serial)
    _validate_center_frequency(config.center_frequency)
    _validate_rtl_sample_rate(config.rtl_sample_rate)
    _validate_automatic_gain_control(config.automatic_gain_control)
    _validate_rtl_gain(config.automatic_gain_control, config.rtl_gain)
    _validate_ppm_correction(config.ppm_correction)
    _validate_transition_bandwidth(config.transition_bandwidth)
    _validate_listen_host(config.listen_host)
    _validate_listen_port(config.listen_port)
    _validate_read_chunk_size(config.read_chunk_size)
    _validate_rtl_read_timeout_seconds(config.rtl_read_timeout_seconds)
    _validate_stream_queue_chunks(config.stream_queue_chunks)
    _validate_client_queue_chunks(config.client_queue_chunks)
    _validate_enqueue_timeout_seconds(config.enqueue_timeout_seconds)
    _validate_audio_support(config.audio_support)
    _validate_demodulator_enabled("audio.am.enabled", config.am_enabled)
    _validate_demodulator_enabled("audio.lsb.enabled", config.lsb_enabled)
    _validate_demodulator_enabled("audio.usb.enabled", config.usb_enabled)
    _validate_demodulator_enabled("audio.nfm.enabled", config.nfm_enabled)
    _validate_demodulator_enabled("audio.wfm.enabled", config.wfm_enabled)
    _validate_enable_wfm_stereo(config.enable_wfm_stereo)
    if not config.audio_support:
        return
    if config.nfm_enabled:
        _validate_nfm_deemphasis_tau(config.nfm_deemphasis_tau)
    if config.wfm_enabled:
        _validate_wfm_deemphasis_region(config.wfm_deemphasis_region)


def _validate_rtl_device_index(value: int) -> None:
    if value < 0:
        raise ValueError("rtl_device_index must be non-negative")


def _validate_rtl_serial(value: str | None) -> None:
    if value is not None and not value:
        raise ValueError("rtl_serial must not be empty")


def _validate_center_frequency(value: int) -> None:
    if value <= 0:
        raise ValueError("center_frequency must be positive")


def _validate_rtl_sample_rate(value: int) -> None:
    if not _is_valid_rtl_sample_rate(value):
        raise ValueError(
            f"Cannot sample at {value} S/s. "
            "The sample rate must be between 225001 S/s and 300000 S/s "
            "or 900001 S/s and 3200000 S/s."
        )


def _validate_automatic_gain_control(value: bool) -> None:
    if not isinstance(value, bool):
        raise ValueError("automatic_gain_control must be true or false")


def _validate_rtl_gain(automatic_gain_control: bool, value: float | None) -> None:
    if automatic_gain_control:
        return
    if value is None:
        raise ValueError("rtl_gain must be set when automatic_gain_control is false")
    if not (1.0 <= value <= 49.6):
        raise ValueError("rtl_gain must be between 1.0 dB and 49.6 dB")


def _validate_ppm_correction(value: int) -> None:
    if not (-500 <= value <= 500):
        raise ValueError("ppm_correction must be between -500 and 500")


def _validate_transition_bandwidth(value: float) -> None:
    if not (0.005 <= value <= 0.05):
        raise ValueError("transition_bandwidth must be between 0.005 and 0.05")


def _validate_nfm_deemphasis_tau(value: int | None) -> None:
    if value is None:
        return
    if not (32 <= value <= 530):
        raise ValueError("audio.nfm.deemphasis_tau must be null or between 32 and 530")


def _validate_enable_wfm_stereo(value: bool) -> None:
    if not isinstance(value, bool):
        raise ValueError("audio.wfm.stereo_support must be true or false")


def _validate_wfm_deemphasis_region(value: str) -> None:
    _get_wfm_deemphasis_tau(value)


def _validate_audio_support(value: bool) -> None:
    if not isinstance(value, bool):
        raise ValueError("audio.audio_support must be true or false")


def _validate_demodulator_enabled(name: str, value: bool) -> None:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be true or false")


def _validate_listen_host(value: str) -> None:
    if not value:
        raise ValueError("listen_host must not be empty")


def _validate_listen_port(value: int) -> None:
    if value <= 0 or value > 65535:
        raise ValueError("listen_port must be between 1 and 65535")


def _validate_read_chunk_size(value: int) -> None:
    if value <= 0:
        raise ValueError("read_chunk_size must be positive")


def _validate_rtl_read_timeout_seconds(value: float) -> None:
    if value <= 0:
        raise ValueError("rtl_read_timeout_seconds must be positive")


def _validate_stream_queue_chunks(value: int) -> None:
    if value <= 0:
        raise ValueError("stream_queue_chunks must be positive")


def _validate_client_queue_chunks(value: int) -> None:
    if value <= 0:
        raise ValueError("client_queue_chunks must be positive")


def _validate_enqueue_timeout_seconds(value: float) -> None:
    if value < 0:
        raise ValueError("enqueue_timeout_seconds must be non-negative")


def _is_valid_rtl_sample_rate(sample_rate: int) -> bool:
    return (
        225_001 <= sample_rate <= 300_000
        or 900_001 <= sample_rate <= 3_200_000
    )


def _check_dependencies(config: ServerConfig) -> None:
    if PYRTLSDR_IMPORT_ERROR is not None:
        raise ImportError(f"pyrtlsdr compatibility layer could not load librtlsdr: {PYRTLSDR_IMPORT_ERROR}")
    if shutil.which("csdr") is None:
        raise FileNotFoundError("required command(s) not found in PATH: csdr")
    if config.audio_support and config.wfm_enabled and config.enable_wfm_stereo and shutil.which("demux") is None:
        raise FileNotFoundError(
            "Please install Stereo Demux for WFM stereo support"
        )


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
    warnings: list[str] = []
    if "frequency" not in request:
        raise RequestValidationError(EXIT_REQUEST_ERROR, "request must include frequency")
    request["mode"] = _normalize_mode(request.get("mode", DEFAULT_MODE))
    _validate_mode(request["mode"])
    if request["mode"] == "iq":
        if "sample_rate" not in request and "bandwidth" not in request:
            raise RequestValidationError(
                EXIT_REQUEST_ERROR,
                "iq mode request must include sample_rate or bandwidth",
            )
        request["format"] = _normalize_sample_format(
            request.get("format", DEFAULT_SAMPLE_FORMAT)
        )
        _validate_sample_format(request["format"])
        request["modulation"] = None
    else:
        if "modulation" not in request:
            raise RequestValidationError(
                EXIT_REQUEST_ERROR,
                "audio mode request must include modulation",
            )
        if "sample_rate" in request or "bandwidth" in request:
            warnings.append("sample rate is fixed in audio mode and will be ignored")
        if "format" in request:
            warnings.append("format is fixed to s16 in audio mode and will be ignored")
        request["modulation"] = _normalize_audio_modulation(request["modulation"])
        _validate_audio_modulation(request["modulation"])
        request["format"] = None
    request["warnings"] = warnings
    conn.settimeout(None)
    return request


def send_handshake(conn: socket.socket, payload: dict[str, Any]) -> None:
    conn.sendall(json.dumps(payload).encode("utf-8") + b"\n")


def serve(config_path: Path, config: ServerConfig) -> int:
    capture = CaptureManager(config)
    capture.start()

    shutdown_event = threading.Event()
    reload_event = threading.Event()

    def _handle_signal(signum: int, _frame: Any) -> None:
        LOGGER.info("received signal %s, shutting down", signum)
        shutdown_event.set()

    def _handle_reload(_signum: int, _frame: Any) -> None:
        LOGGER.info("received SIGHUP, reloading config from %s", config_path)
        reload_event.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGHUP, _handle_reload)

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
                if reload_event.is_set():
                    reload_event.clear()
                    try:
                        capture.reload_config(config_path)
                    except Exception:
                        LOGGER.exception("config reload failed")
                try:
                    conn, address = server.accept()
                except socket.timeout:
                    continue

                try:
                    request = parse_client_request(conn)
                    frequency = int(request["frequency"])
                    mode = request["mode"]
                    output_rate = None
                    if mode == "iq":
                        output_rate = int(request.get("sample_rate", request.get("bandwidth")))
                    else:
                        output_rate = _get_audio_output_rate(request["modulation"])
                    sample_format = request["format"]
                    modulation = request["modulation"]
                    request_warnings = request["warnings"]
                    source_stream = capture.get_output_stream(
                        frequency,
                        mode,
                        output_rate,
                        sample_format,
                        modulation,
                    )
                    session = ClientSession(
                        conn=conn,
                        address=address,
                        manager=capture,
                        config=capture.config,
                        source_stream=source_stream,
                        frequency=frequency,
                        mode=mode,
                        output_rate=output_rate,
                        sample_format=sample_format,
                        modulation=modulation,
                    )
                    capture.register_client(session)
                    session.start()
                    handshake = {"status": "ok", "mode": mode}
                    if mode == "iq":
                        handshake["format"] = sample_format
                    else:
                        handshake["format"] = DEFAULT_AUDIO_OUTPUT_FORMAT
                        handshake["modulation"] = modulation
                        handshake["sample_rate"] = _get_audio_output_rate(modulation)
                    if request_warnings:
                        handshake["warnings"] = request_warnings
                        for warning in request_warnings:
                            LOGGER.warning(
                                "client %s:%s: %s",
                                address[0],
                                address[1],
                                warning,
                            )
                    send_handshake(conn, handshake)
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
    _set_process_name("csdr_server")
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        config = load_config(args.config)
        _check_dependencies(config)
        return serve(args.config, config)
    except SystemExit:
        raise
    except FileNotFoundError as exc:
        if exc.filename and Path(exc.filename) == args.config:
            LOGGER.error("config file not found: %s", args.config)
        else:
            LOGGER.error("%s", exc)
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
