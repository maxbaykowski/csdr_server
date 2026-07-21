from __future__ import annotations

import math
import sys
from typing import Any

from .config import ServerConfig
from .constants import *
from .errors import RequestValidationError

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
    return DEFAULT_AUDIO_OUTPUT_RATE


def _get_audio_channels(modulation: str) -> int:
    return DEFAULT_AUDIO_OUTPUT_CHANNELS


def _build_session_status_payload(
    session: "ClientSession",
    *,
    command: str,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": "ok",
        "command": command,
        "frequency": session.frequency,
        "mode": session.mode,
        "rds_active": session.rds_subscribed,
        "squelch": session.squelch_level,
    }
    if session.mode == "iq":
        payload["format"] = session.sample_format
        payload["sample_rate"] = session.output_rate
    else:
        assert session.modulation is not None
        payload["format"] = DEFAULT_AUDIO_OUTPUT_FORMAT
        payload["modulation"] = session.modulation
        payload["sample_rate"] = _get_audio_output_rate(session.modulation)
        payload["channels"] = _get_audio_channels(session.modulation)
    return payload


def _normalize_rds_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _extract_rds_fields(message: dict[str, Any]) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    callsign = _normalize_rds_text(message.get("callsign"))
    if callsign is None:
        callsign = _normalize_rds_text(message.get("callsign_uncertain"))
    if callsign is not None:
        fields["callsign"] = callsign
    program_service = _normalize_rds_text(message.get("ps"))
    if program_service is not None:
        fields["program_service"] = program_service
    radiotext = _normalize_rds_text(message.get("radiotext"))
    if radiotext is not None:
        fields["radiotext"] = radiotext
    program_type = _normalize_rds_text(message.get("prog_type"))
    if program_type is not None:
        fields["program_type"] = program_type
    radiotext_plus = message.get("radiotext_plus")
    if isinstance(radiotext_plus, dict):
        tags = radiotext_plus.get("tags")
        if isinstance(tags, list):
            for tag in tags:
                if not isinstance(tag, dict):
                    continue
                content_type = tag.get("content-type")
                data = _normalize_rds_text(tag.get("data"))
                if data is None:
                    continue
                if content_type == "item.artist":
                    fields["artist"] = data
                elif content_type == "item.title":
                    fields["title"] = data
    return fields


def _get_audio_iq_rate(modulation: str) -> int:
    if modulation in {"wfm", "wfm_stereo"}:
        return WFM_IQ_RATE
    return NARROW_AUDIO_IQ_RATE


def _get_audio_transition_bandwidth(modulation: str) -> float:
    if modulation in {"wfm", "wfm_stereo"}:
        return WFM_AUDIO_TRANSITION_BANDWIDTH
    return AM_AUDIO_TRANSITION_BANDWIDTH


def _build_audio_pcm_command(input_rate: int) -> list[str]:
    return [
        sys.executable,
        "-m",
        "csdr_server.audio_pcm",
        "--input-rate",
        str(input_rate),
        "--output-rate",
        str(DEFAULT_AUDIO_OUTPUT_RATE),
        "--output-channels",
        str(DEFAULT_AUDIO_OUTPUT_CHANNELS),
    ]


def _validate_requested_mode_supported(
    config: ServerConfig,
    mode: str,
    modulation: str | None,
) -> None:
    if mode == "audio":
        if modulation is None:
            raise RequestValidationError(
                EXIT_REQUEST_ERROR,
                "audio mode request must include modulation",
            )
        _validate_audio_modulation_supported(config, modulation)


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


def _get_wfm_deemphasis_tau(region: str) -> int:
    normalized_region = region.strip().lower()
    if normalized_region == "us":
        return 75
    if normalized_region == "europe":
        return 50
    raise ValueError(
        "audio.wfm.region must be either 'us' or 'europe'"
    )


def _build_output_format_stream(
    config: ServerConfig,
    frequency: int,
    output_rate: int,
    sample_format: str,
    manager: "StreamGraph",
    parent: SharedStream,
) -> SharedStream:
    from .graph import SharedStream

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
    from .graph import SharedStream

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
            command=_build_audio_pcm_command(NARROW_AUDIO_IQ_RATE),
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
            command=_build_audio_pcm_command(NARROW_AUDIO_IQ_RATE),
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
                    str(NARROW_AUDIO_IQ_RATE),
                    f"{config.nfm_deemphasis_tau}e-6",
                ],
                manager=manager,
                parent=demod_stream,
                close_when_unused=True,
            )
            nfm_parent = deemphasis_stream
        lowpass_stream: SharedStream | None = None
        if config.nfm_lowpass_frequency is not None:
            lowpass_cutoff = config.nfm_lowpass_frequency / float(NARROW_AUDIO_IQ_RATE)
            lowpass_stream = SharedStream(
                config=config,
                name=f"audio-{modulation}-{frequency}-lowpass",
                command=[
                    "csdr",
                    "lowpass",
                    "--format",
                    "float",
                    _format_csdr_float(lowpass_cutoff),
                    _format_csdr_float(config.nfm_lowpass_curve),
                ],
                manager=manager,
                parent=nfm_parent,
                close_when_unused=True,
            )
            nfm_parent = lowpass_stream
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
            command=_build_audio_pcm_command(NARROW_AUDIO_IQ_RATE),
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
            if lowpass_stream is not None:
                lowpass_stream.start()
                started_streams.append(lowpass_stream)
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
            DEFAULT_AUDIO_OUTPUT_RATE,
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
        deemphasis_tau = _get_wfm_deemphasis_tau(config.wfm_region)
        deemphasis_stream = SharedStream(
            config=config,
            name=f"audio-{modulation}-{frequency}-deemphasis",
            command=[
                "csdr",
                "deemphasis",
                "--wfm",
                str(DEFAULT_AUDIO_OUTPUT_RATE),
                f"{deemphasis_tau}e-6",
            ],
            manager=manager,
            parent=audio_resample_stream,
            close_when_unused=True,
        )
        output_stream = SharedStream(
            config=config,
            name=f"audio-{modulation}-{frequency}",
            command=_build_audio_pcm_command(DEFAULT_AUDIO_OUTPUT_RATE),
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
        stereo_stream = SharedStream(
            config=config,
            name=f"audio-{modulation}-{frequency}-stereofm",
            command=[
                "csdr",
                "stereofm",
                str(WFM_IQ_RATE),
            ],
            manager=manager,
            parent=parent,
            close_when_unused=True,
        )
        audio_decimation_ratio = _compute_fractional_decimation_ratio(
            WFM_IQ_RATE,
            DEFAULT_AUDIO_OUTPUT_RATE,
        )
        audio_resample_stream = SharedStream(
            config=config,
            name=f"audio-{modulation}-{frequency}-fractionaldecimator",
            command=[
                "csdr",
                "fractionaldecimator",
                _format_csdr_float(audio_decimation_ratio),
                "--format",
                "float",
                "--channels",
                "2",
                "--prefilter",
            ],
            manager=manager,
            parent=stereo_stream,
            close_when_unused=True,
        )
        deemphasis_tau = _get_wfm_deemphasis_tau(config.wfm_region)
        deemphasis_stream = SharedStream(
            config=config,
            name=f"audio-{modulation}-{frequency}-deemphasis",
            command=[
                "csdr",
                "deemphasis",
                "--wfm",
                str(DEFAULT_AUDIO_OUTPUT_RATE),
                f"{deemphasis_tau}e-6",
                "--channels",
                "2",
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
            stereo_stream.start()
            started_streams.append(stereo_stream)
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

    raise ValueError(f"unsupported audio modulation {modulation!r}")


def _compute_output_read_size(sample_format: str, output_rate: int) -> int:
    bytes_per_complex_sample = {
        "f32": 8,
        "s16": 4,
    }[sample_format]
    target_ms = 100
    size = int((output_rate * bytes_per_complex_sample * target_ms) / 1000)
    return max(4096, min(DEFAULT_STREAM_OUTPUT_READ_SIZE, size))


def _measure_complex_float_power_level(chunk: bytes) -> float:
    usable_size = len(chunk) - (len(chunk) % 8)
    if usable_size <= 0:
        return 0.0
    samples = memoryview(chunk[:usable_size]).cast("f")
    power = 0.0
    sample_count = len(samples) // 2
    if sample_count <= 0:
        return 0.0
    for index in range(0, sample_count * 2, 2):
        i_sample = samples[index]
        q_sample = samples[index + 1]
        power += (i_sample * i_sample) + (q_sample * q_sample)
    rms = math.sqrt(power / sample_count)
    if rms <= 0:
        return 0.0
    # CSDR float IQ often has useful signals well below 0.05 RMS. Map dBFS
    # into rtl_fm-like 0-100 user levels so low squelch values are practical.
    dbfs = 20.0 * math.log10(rms)
    return max(0.0, min(100.0, dbfs + 100.0))


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


def _requested_passband_edges(
    frequency: int,
    required_bandwidth: int,
) -> tuple[float, float]:
    half_required_bandwidth = required_bandwidth / 2.0
    return frequency - half_required_bandwidth, frequency + half_required_bandwidth


def _compute_automatic_center_frequency(
    rtl_sample_rate: int,
    requests: list[tuple[int, int]],
) -> int:
    if not requests:
        raise ValueError("at least one request is required to compute automatic center frequency")
    if len(requests) == 1:
        frequency, required_bandwidth = requests[0]
        _validate_output_rate(rtl_sample_rate, required_bandwidth)
        return frequency
    lower_edges: list[float] = []
    upper_edges: list[float] = []
    for frequency, required_bandwidth in requests:
        lower_edge, upper_edge = _requested_passband_edges(frequency, required_bandwidth)
        lower_edges.append(lower_edge)
        upper_edges.append(upper_edge)
    min_lower_edge = min(lower_edges)
    max_upper_edge = max(upper_edges)
    if max_upper_edge - min_lower_edge > rtl_sample_rate:
        raise RequestValidationError(
            EXIT_OUT_OF_BAND,
            "requested frequency is out of band for the current RTL capture window",
        )
    return int(round((min_lower_edge + max_upper_edge) / 2.0))


def _validate_request_frequency(
    config: ServerConfig,
    frequency: int,
    required_bandwidth: int,
) -> None:
    half_capture_bandwidth = config.rtl_sample_rate / 2.0
    half_required_bandwidth = required_bandwidth / 2.0
    center_offset = abs(config.center_frequency - frequency)
    if center_offset + half_required_bandwidth > half_capture_bandwidth:
        if required_bandwidth != config.rtl_sample_rate:
            message = (
                "requested frequency is out of band for the current RTL capture window "
                f"at required bandwidth {required_bandwidth} S/s"
            )
        else:
            message = "requested frequency is out of band for the current RTL capture window"
        raise RequestValidationError(
            EXIT_OUT_OF_BAND,
            message,
        )


def _validate_session_request(
    config: ServerConfig,
    session: "ClientSession",
    validate_audio_support: bool = True,
) -> None:
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
        if validate_audio_support:
            _validate_audio_modulation_supported(config, session.modulation)
        _validate_output_rate(config.rtl_sample_rate, _get_audio_iq_rate(session.modulation))
        return
    raise RequestValidationError(
        EXIT_REQUEST_ERROR,
        f"unsupported session mode {session.mode!r}",
    )
