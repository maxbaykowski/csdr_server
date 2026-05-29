from __future__ import annotations

import json
import socket
from typing import Any

from .constants import *
from .dsp import (
    _get_audio_channels,
    _get_audio_output_rate,
    _normalize_audio_modulation,
    _normalize_mode,
    _normalize_sample_format,
    _validate_audio_modulation,
    _validate_mode,
    _validate_sample_format,
)
from .errors import RequestValidationError

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

def _read_json_message_line(reader: Any) -> bytes | None:
    line = reader.readline(16_384)
    if not line:
        return None
    return line


def _parse_request_frequency(request: dict[str, Any]) -> int:
    if "frequency" not in request:
        raise RequestValidationError(EXIT_REQUEST_ERROR, "request must include frequency")
    try:
        return int(request["frequency"])
    except (TypeError, ValueError) as exc:
        raise RequestValidationError(EXIT_REQUEST_ERROR, f"invalid frequency: {request['frequency']!r}") from exc


def _parse_squelch_level(value: Any) -> int:
    try:
        level = int(value)
    except (TypeError, ValueError) as exc:
        raise RequestValidationError(EXIT_REQUEST_ERROR, f"invalid squelch level: {value!r}") from exc
    _validate_squelch_level(level)
    return level


def _validate_squelch_level(level: int) -> None:
    if level < SQUELCH_MIN_LEVEL or level > SQUELCH_MAX_LEVEL:
        raise RequestValidationError(
            EXIT_REQUEST_ERROR,
            f"squelch level must be between {SQUELCH_MIN_LEVEL} and {SQUELCH_MAX_LEVEL}",
        )


def parse_client_request(reader: Any) -> dict[str, Any]:
    line = _read_json_message_line(reader)
    if line is None:
        raise RequestValidationError(EXIT_REQUEST_ERROR, "client did not send a request line")
    try:
        request = json.loads(line.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise RequestValidationError(EXIT_REQUEST_ERROR, f"invalid request json: {exc}") from exc
    warnings: list[str] = []
    if "stream_token" not in request:
        raise RequestValidationError(EXIT_REQUEST_ERROR, "request must include stream_token")
    request["stream_token"] = str(request["stream_token"]).strip()
    if not request["stream_token"]:
        raise RequestValidationError(EXIT_REQUEST_ERROR, "stream_token must not be empty")
    _parse_request_frequency(request)
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
        request["squelch"] = _parse_squelch_level(request.get("squelch", 0))
    if request["mode"] == "iq":
        request["squelch"] = 0
    request["warnings"] = warnings
    return request


def parse_control_command(reader: Any) -> dict[str, Any] | None:
    line = _read_json_message_line(reader)
    if line is None:
        return None
    try:
        message = json.loads(line.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise RequestValidationError(EXIT_REQUEST_ERROR, f"invalid control json: {exc}") from exc
    command = str(message.get("command", "")).strip().lower()
    if not command:
        raise RequestValidationError(EXIT_REQUEST_ERROR, "control command must include command")
    message["command"] = command
    return message


def send_handshake(conn: socket.socket, payload: dict[str, Any]) -> None:
    conn.sendall(json.dumps(payload).encode("utf-8") + b"\n")


def parse_stream_token(conn: socket.socket) -> str:
    conn.settimeout(10.0)
    reader = conn.makefile("rb")
    line = reader.readline(256)
    if not line:
        raise RequestValidationError(EXIT_REQUEST_ERROR, "stream socket did not send a stream token")
    token = line.decode("utf-8", errors="replace").strip()
    if not token:
        raise RequestValidationError(EXIT_REQUEST_ERROR, "stream token must not be empty")
    conn.settimeout(None)
    return token
