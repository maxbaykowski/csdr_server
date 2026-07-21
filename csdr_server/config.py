from __future__ import annotations

import json5
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .constants import *


MINIMUM_CSDR_VERSION = (0, 19, 3)

@dataclass(frozen=True)
class ServerConfig:
    rtl_device_index: int = 0
    rtl_serial: str | None = None
    center_frequency: int = 100_000_000
    automatic_tuning: bool = False
    rtl_sample_rate: int = 2_400_000
    automatic_gain_control: bool = False
    rtl_gain: float | None = None
    ppm_correction: int = 0
    bias_tee: bool = False
    dc_block: bool = False
    transition_bandwidth: float = 0.05
    audio_support: bool = True
    am_enabled: bool = True
    lsb_enabled: bool = True
    usb_enabled: bool = True
    nfm_enabled: bool = True
    nfm_deemphasis_tau: int | None = NFM_AUDIO_DEEMPHASIS_TAU
    nfm_lowpass_frequency: int | None = 3200
    nfm_lowpass_curve: float = 0.5
    wfm_enabled: bool = True
    enable_wfm_rds: bool = False
    wfm_region: str = WFM_DEEMPHASIS_REGION
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
        audio_section_present = "audio" in data and data["audio"] is not None
        explicit_demodulators = {
            name
            for name in ("am", "lsb", "usb", "nfm", "wfm")
            if name in audio_settings
        }
        am_settings = _get_config_section(audio_settings, "am")
        lsb_settings = _get_config_section(audio_settings, "lsb")
        usb_settings = _get_config_section(audio_settings, "usb")
        nfm_settings = _get_config_section(audio_settings, "nfm")
        wfm_settings = _get_config_section(audio_settings, "wfm")
        if audio_section_present and "audio_support" not in audio_settings:
            raise ValueError("audio.audio_support is required when the audio section is defined")
        audio_support = _parse_bool(
            audio_settings.get("audio_support", True),
            "audio.audio_support",
        )
        if audio_support:
            am_enabled = _parse_demodulator_enabled(
                "am",
                am_settings,
                explicit_demodulators,
            )
            lsb_enabled = _parse_demodulator_enabled(
                "lsb",
                lsb_settings,
                explicit_demodulators,
            )
            usb_enabled = _parse_demodulator_enabled(
                "usb",
                usb_settings,
                explicit_demodulators,
            )
            nfm_enabled = _parse_demodulator_enabled(
                "nfm",
                nfm_settings,
                explicit_demodulators,
            )
            wfm_enabled = _parse_demodulator_enabled(
                "wfm",
                wfm_settings,
                explicit_demodulators,
            )
        else:
            am_enabled = False
            lsb_enabled = False
            usb_enabled = False
            nfm_enabled = False
            wfm_enabled = False
        nfm_lowpass_frequency = (
            _optional_int(
                _config_value(
                    audio_settings,
                    nfm_settings,
                    "lowpass_frequency",
                    3200,
                ),
                "audio.nfm.lowpass_frequency",
            )
            if audio_support and nfm_enabled
            else 3200
        )
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
            automatic_tuning=_parse_bool(
                _config_value(data, rtl_settings, "automatic_tuning", False),
                "rtl.automatic_tuning",
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
            bias_tee=_parse_bool(
                _config_value(data, rtl_settings, "bias_tee", False),
                "rtl.bias_tee",
            ),
            dc_block=_parse_bool(
                _config_value(data, rtl_settings, "dc_block", False),
                "rtl.dc_block",
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
            nfm_lowpass_frequency=nfm_lowpass_frequency,
            nfm_lowpass_curve=(
                _parse_float(
                    _config_value(
                        audio_settings,
                        nfm_settings,
                        "lowpass_curve",
                        0.5,
                    ),
                    "audio.nfm.lowpass_curve",
                )
                if audio_support and nfm_enabled and nfm_lowpass_frequency is not None
                else 0.5
            ),
            wfm_enabled=wfm_enabled,
            enable_wfm_rds=(
                _parse_bool(
                    _config_value(
                        audio_settings,
                        wfm_settings,
                        "rds_support",
                        False,
                    ),
                    "audio.wfm.rds_support",
                )
                if audio_support and wfm_enabled
                else False
            ),
            wfm_region=(
                _normalize_wfm_region(
                    _config_value(
                        audio_settings,
                        wfm_settings,
                        "region",
                        wfm_settings.get(
                            "deemphasis_region",
                            audio_settings.get("wfm_deemphasis_region", WFM_DEEMPHASIS_REGION),
                        ),
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


def _parse_demodulator_enabled(
    name: str,
    settings: dict[str, Any],
    explicit_demodulators: set[str],
) -> bool:
    if not explicit_demodulators:
        return _parse_bool(settings.get("enabled", True), f"audio.{name}.enabled")
    if name not in explicit_demodulators:
        return False
    if "enabled" not in settings:
        raise ValueError(f"audio.{name}.enabled is required when audio.{name} is defined")
    return _parse_bool(settings.get("enabled", True), f"audio.{name}.enabled")


def _normalize_wfm_region(value: Any) -> str:
    return _parse_string(value, "audio.wfm.region").strip().lower()


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

def load_config(path: Path) -> ServerConfig:
    raw = json5.loads(path.read_text(encoding="utf-8"))
    return ServerConfig.from_dict(raw)


def _validate_config(config: ServerConfig) -> None:
    _validate_rtl_device_index(config.rtl_device_index)
    _validate_rtl_serial(config.rtl_serial)
    _validate_center_frequency(config.center_frequency)
    _validate_automatic_tuning(config.automatic_tuning)
    _validate_rtl_sample_rate(config.rtl_sample_rate)
    _validate_automatic_gain_control(config.automatic_gain_control)
    _validate_rtl_gain(config.automatic_gain_control, config.rtl_gain)
    _validate_ppm_correction(config.ppm_correction)
    _validate_bias_tee(config.bias_tee)
    _validate_dc_block(config.dc_block)
    _validate_transition_bandwidth(config.transition_bandwidth)
    _validate_listen_host(config.listen_host)
    _validate_listen_port(config.listen_port)
    _validate_read_chunk_size(config.read_chunk_size)
    _validate_rtl_read_timeout_seconds(config.rtl_read_timeout_seconds)
    _validate_stream_queue_chunks(config.stream_queue_chunks)
    _validate_client_queue_chunks(config.client_queue_chunks)
    _validate_enqueue_timeout_seconds(config.enqueue_timeout_seconds)
    _validate_audio_support(config.audio_support)
    if not config.audio_support:
        return
    _validate_demodulator_enabled("audio.am.enabled", config.am_enabled)
    _validate_demodulator_enabled("audio.lsb.enabled", config.lsb_enabled)
    _validate_demodulator_enabled("audio.usb.enabled", config.usb_enabled)
    _validate_demodulator_enabled("audio.nfm.enabled", config.nfm_enabled)
    _validate_demodulator_enabled("audio.wfm.enabled", config.wfm_enabled)
    _validate_enable_wfm_rds(config.enable_wfm_rds)
    if not any(
        (
            config.am_enabled,
            config.lsb_enabled,
            config.usb_enabled,
            config.nfm_enabled,
            config.wfm_enabled,
        )
    ):
        raise ValueError(
            "audio.audio_support is true, but all audio demodulators are disabled; "
            "enable at least one of audio.am.enabled, audio.lsb.enabled, "
            "audio.usb.enabled, audio.nfm.enabled, or audio.wfm.enabled"
        )
    if config.nfm_enabled:
        _validate_nfm_deemphasis_tau(config.nfm_deemphasis_tau)
        _validate_nfm_lowpass_frequency(config.nfm_lowpass_frequency)
        if config.nfm_lowpass_frequency is not None:
            _validate_nfm_lowpass_curve(config.nfm_lowpass_curve)
    if config.wfm_enabled:
        _validate_wfm_region(config.wfm_region)


def _validate_rtl_device_index(value: int) -> None:
    if value < 0:
        raise ValueError("rtl_device_index must be non-negative")


def _validate_rtl_serial(value: str | None) -> None:
    if value is not None and not value:
        raise ValueError("rtl_serial must not be empty")


def _validate_center_frequency(value: int) -> None:
    if value <= 0:
        raise ValueError("center_frequency must be positive")


def _validate_automatic_tuning(value: bool) -> None:
    if not isinstance(value, bool):
        raise ValueError("rtl.automatic_tuning must be true or false")


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


def _validate_bias_tee(value: bool) -> None:
    if not isinstance(value, bool):
        raise ValueError("rtl.bias_tee must be true or false")


def _validate_dc_block(value: bool) -> None:
    if not isinstance(value, bool):
        raise ValueError("rtl.dc_block must be true or false")


def _validate_transition_bandwidth(value: float) -> None:
    if not (0.005 <= value <= 0.5):
        raise ValueError("transition_bandwidth must be between 0.005 and 0.5")


def _validate_nfm_deemphasis_tau(value: int | None) -> None:
    if value is None:
        return
    if not (32 <= value <= 530):
        raise ValueError("audio.nfm.deemphasis_tau must be null or between 32 and 530")


def _validate_nfm_lowpass_frequency(value: int | None) -> None:
    if value is None:
        return
    if not (3000 <= value <= 8000):
        raise ValueError("audio.nfm.lowpass_frequency must be null or between 3000 and 8000")


def _validate_nfm_lowpass_curve(value: float) -> None:
    if not (0.005 <= value <= 0.5):
        raise ValueError("audio.nfm.lowpass_curve must be between 0.005 and 0.5")


def _validate_enable_wfm_rds(value: bool) -> None:
    if not isinstance(value, bool):
        raise ValueError("audio.wfm.rds_support must be true or false")


def _validate_wfm_region(value: str) -> None:
    from .dsp import _get_wfm_deemphasis_tau

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
    if value <= 0 or value >= 65535:
        raise ValueError("listen_port must be between 1 and 65534 so the control socket can use port+1")


def _validate_read_chunk_size(value: int) -> None:
    if value <= 0:
        raise ValueError("read_chunk_size must be positive")
    if value % 512 != 0:
        raise ValueError("read_chunk_size must be a multiple of 512 bytes for RTL-SDR USB alignment")
    if value % 2 != 0:
        raise ValueError("read_chunk_size must be an even number of bytes to preserve I/Q sample pairs")


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


def _format_version(version: tuple[int, ...]) -> str:
    return ".".join(str(part) for part in version)


def _parse_version_text(text: str) -> tuple[int, ...] | None:
    match = re.search(r"(\d+(?:\.\d+)*)(?:-[A-Za-z0-9_.-]+)?", text)
    if match is None:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def _version_at_least(version: tuple[int, ...], minimum: tuple[int, ...]) -> bool:
    width = max(len(version), len(minimum))
    padded_version = version + (0,) * (width - len(version))
    padded_minimum = minimum + (0,) * (width - len(minimum))
    return padded_version >= padded_minimum


def _check_csdr_version(csdr_path: str) -> None:
    try:
        result = subprocess.run(
            [csdr_path, "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            text=True,
            timeout=5.0,
        )
    except OSError as exc:
        raise FileNotFoundError(f"could not run csdr --version: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("csdr --version timed out") from exc

    output = result.stdout.strip()
    version = _parse_version_text(output)
    if version is None:
        raise RuntimeError(
            "could not determine CSDR version from `csdr --version` output"
            + (f": {output}" if output else "")
        )
    if not _version_at_least(version, MINIMUM_CSDR_VERSION):
        raise RuntimeError(
            "CSDR "
            f"{_format_version(MINIMUM_CSDR_VERSION)} or later is required; "
            f"found {_format_version(version)}"
        )


def _check_dependencies(config: ServerConfig) -> None:
    from .rtl import PYRTLSDR_IMPORT_ERROR

    if PYRTLSDR_IMPORT_ERROR is not None:
        raise ImportError(
            "could not load librtlsdr. Install your distribution's librtlsdr package "
            f"or install pyrtlsdrlib on supported architectures: {PYRTLSDR_IMPORT_ERROR}"
        )
    csdr_path = shutil.which("csdr")
    if csdr_path is None:
        raise FileNotFoundError("required command(s) not found in PATH: csdr")
    _check_csdr_version(csdr_path)
    if config.audio_support and config.wfm_enabled and config.enable_wfm_rds and shutil.which("redsea") is None:
        raise FileNotFoundError(
            "Please install redsea for WFM RDS support"
        )
