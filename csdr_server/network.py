from __future__ import annotations

import json
import select
import signal
import socket
import threading
import time
from pathlib import Path
from typing import Any

from .config import ServerConfig
from .constants import DEFAULT_AUDIO_OUTPUT_FORMAT, EXIT_REQUEST_ERROR, LOGGER
from .dsp import _get_audio_channels, _get_audio_output_rate
from .errors import PendingStreamConnection, RequestValidationError
from .protocol import parse_client_request, parse_stream_token, send_handshake
from .rtl import CaptureManager
from .sessions import ClientSession

def serve(config_path: Path, config: ServerConfig) -> int:
    capture = CaptureManager(config)
    capture.start()

    shutdown_event = threading.Event()
    reload_event = threading.Event()
    pending_streams: dict[str, PendingStreamConnection] = {}
    pending_stream_timeout_seconds = 30.0

    def _handle_signal(signum: int, _frame: Any) -> None:
        LOGGER.info("received signal %s, shutting down", signum)
        shutdown_event.set()

    def _handle_reload(_signum: int, _frame: Any) -> None:
        LOGGER.info("received SIGHUP, reloading config from %s", config_path)
        reload_event.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGHUP, _handle_reload)
    stream_port = config.listen_port
    control_port = config.listen_port + 1

    def _cleanup_expired_pending_streams() -> None:
        now = time.time()
        expired_tokens = [
            token
            for token, pending in pending_streams.items()
            if now - pending.created_at > pending_stream_timeout_seconds
        ]
        for token in expired_tokens:
            pending = pending_streams.pop(token)
            try:
                pending.conn.close()
            except OSError:
                pass
            LOGGER.info(
                "dropped pending stream connection %s:%s after timeout",
                pending.address[0],
                pending.address[1],
            )

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as stream_server, socket.socket(socket.AF_INET, socket.SOCK_STREAM) as control_server:
        stream_server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        control_server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        stream_server.bind((config.listen_host, stream_port))
        control_server.bind((config.listen_host, control_port))
        stream_server.listen()
        control_server.listen()
        LOGGER.info(
            "listening on stream %s:%s and control %s:%s, center_frequency=%s rtl_sample_rate=%s",
            config.listen_host,
            stream_port,
            config.listen_host,
            control_port,
            config.center_frequency,
            config.rtl_sample_rate,
        )

        try:
            while not shutdown_event.is_set() and not capture.stop_event.is_set():
                _cleanup_expired_pending_streams()
                if reload_event.is_set():
                    reload_event.clear()
                    try:
                        capture.reload_config(config_path)
                    except Exception:
                        LOGGER.exception("config reload failed")
                ready_sockets, _, _ = select.select([stream_server, control_server], [], [], 1.0)
                for ready_socket in ready_sockets:
                    if ready_socket is stream_server:
                        conn, address = stream_server.accept()
                        try:
                            token = parse_stream_token(conn)
                            if token in pending_streams:
                                previous = pending_streams.pop(token)
                                try:
                                    previous.conn.close()
                                except OSError:
                                    pass
                            pending_streams[token] = PendingStreamConnection(
                                conn=conn,
                                address=address,
                                created_at=time.time(),
                            )
                        except Exception:
                            conn.close()
                    else:
                        conn, address = control_server.accept()
                        pending: PendingStreamConnection | None = None
                        keep_control_open = False
                        try:
                            conn.settimeout(10.0)
                            control_reader = conn.makefile("rb")
                            request = parse_client_request(control_reader)
                            conn.settimeout(None)
                            token = request["stream_token"]
                            pending = pending_streams.pop(token, None)
                            if pending is None:
                                raise RequestValidationError(
                                    EXIT_REQUEST_ERROR,
                                    "no matching stream socket is connected for this request",
                                )
                            frequency = int(request["frequency"])
                            mode = request["mode"]
                            output_rate = None
                            if mode == "iq":
                                output_rate = int(request.get("sample_rate", request.get("bandwidth")))
                            else:
                                output_rate = _get_audio_output_rate(request["modulation"])
                            sample_format = request["format"]
                            modulation = request["modulation"]
                            squelch_level = int(request["squelch"])
                            request_warnings = request["warnings"]
                            capture.prepare_request(
                                frequency,
                                mode,
                                output_rate,
                                modulation,
                            )
                            source_stream = capture.get_output_stream(
                                frequency,
                                mode,
                                output_rate,
                                sample_format,
                                modulation,
                            )
                            power_monitor = capture.get_audio_power_monitor(
                                frequency,
                                modulation,
                            ) if mode == "audio" else None
                            session = ClientSession(
                                conn=pending.conn,
                                address=pending.address,
                                manager=capture,
                                config=capture.config,
                                source_stream=source_stream,
                                frequency=frequency,
                                mode=mode,
                                output_rate=output_rate,
                                sample_format=sample_format,
                                modulation=modulation,
                                squelch_level=squelch_level,
                                power_monitor=power_monitor,
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
                                handshake["channels"] = _get_audio_channels(modulation)
                                handshake["squelch"] = squelch_level
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
                            session.attach_control(conn, control_reader)
                            keep_control_open = True
                        except RequestValidationError as exc:
                            LOGGER.warning(
                                "rejecting control client %s:%s: %s",
                                address[0],
                                address[1],
                                exc,
                            )
                            if pending is not None:
                                try:
                                    pending.conn.close()
                                except OSError:
                                    pass
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
                        except Exception as exc:
                            LOGGER.warning(
                                "rejecting control client %s:%s: %s",
                                address[0],
                                address[1],
                                exc,
                            )
                            if pending is not None:
                                try:
                                    pending.conn.close()
                                except OSError:
                                    pass
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
                        finally:
                            if not keep_control_open:
                                try:
                                    conn.close()
                                except OSError:
                                    pass
        finally:
            for pending in pending_streams.values():
                try:
                    pending.conn.close()
                except OSError:
                    pass
            capture.stop()

    if capture.fatal_error is not None:
        raise capture.fatal_error

    return 0
