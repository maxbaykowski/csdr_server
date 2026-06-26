from __future__ import annotations

import errno
import json
import os
import select
import signal
import socket
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import ServerConfig
from .constants import DEFAULT_AUDIO_OUTPUT_FORMAT, EXIT_REQUEST_ERROR, LOGGER
from .dsp import _get_audio_channels, _get_audio_output_rate
from .errors import NetworkBindError, PendingStreamConnection, RequestValidationError
from .opus_codec import (
    DEFAULT_AUDIO_CODEC,
    DEFAULT_OPUS_BITRATE,
    OPUS_FRAME_MS,
    OpusCodecError,
    probe_opus_encoder,
)
from .protocol import parse_client_request, parse_stream_token, send_handshake
from .rtl import CaptureManager
from .sessions import ClientSession


@dataclass(frozen=True)
class PortOwner:
    pid: int
    name: str


@dataclass(frozen=True)
class ListenerEndpoint:
    host: str
    port: int

    @property
    def control_port(self) -> int:
        return self.port + 1


def serve(config_path: Path, config: ServerConfig) -> int:
    capture = CaptureManager(config)

    shutdown_event = threading.Event()
    reload_event = threading.Event()
    pending_streams: dict[str, PendingStreamConnection] = {}
    pending_stream_timeout_seconds = 30.0
    active_endpoint = ListenerEndpoint(config.listen_host, config.listen_port)
    pending_endpoint: ListenerEndpoint | None = None
    pending_endpoint_attempted = False

    def _handle_signal(signum: int, _frame: Any) -> None:
        LOGGER.info("Shutdown signal received; stopping server")
        shutdown_event.set()

    def _handle_reload(_signum: int, _frame: Any) -> None:
        LOGGER.info("Reloading configuration from %s", config_path)
        reload_event.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGHUP, _handle_reload)
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
            LOGGER.debug(
                "dropped pending stream connection %s:%s after timeout",
                pending.address[0],
                pending.address[1],
            )

    stream_server, control_server = _create_listener_pair(active_endpoint)
    try:
        capture.start()
        LOGGER.info(
            "csdr_server is listening on %s:%s (control port %s)",
            active_endpoint.host,
            active_endpoint.port,
            active_endpoint.control_port,
        )

        try:
            while not shutdown_event.is_set() and not capture.stop_event.is_set():
                _cleanup_expired_pending_streams()
                if reload_event.is_set():
                    reload_event.clear()
                    try:
                        loaded_config = capture.reload_config(config_path)
                        loaded_endpoint = ListenerEndpoint(
                            loaded_config.listen_host,
                            loaded_config.listen_port,
                        )
                        if loaded_endpoint != active_endpoint:
                            pending_endpoint = loaded_endpoint
                            pending_endpoint_attempted = False
                            LOGGER.info(
                                "listener change queued for %s:%s (control port %s); it will be applied when all clients disconnect",
                                pending_endpoint.host,
                                pending_endpoint.port,
                                pending_endpoint.control_port,
                            )
                        else:
                            pending_endpoint = None
                            pending_endpoint_attempted = False
                    except Exception:
                        LOGGER.exception("config reload failed")
                if (
                    pending_endpoint is not None
                    and not pending_endpoint_attempted
                    and capture.active_client_count() == 0
                ):
                    for pending in pending_streams.values():
                        try:
                            pending.conn.close()
                        except OSError:
                            pass
                    pending_streams.clear()
                    pending_endpoint_attempted = True
                    try:
                        (
                            stream_server,
                            control_server,
                            active_endpoint,
                        ) = _try_apply_listener_endpoint(
                            stream_server,
                            control_server,
                            active_endpoint,
                            pending_endpoint,
                        )
                    except Exception:
                        LOGGER.exception(
                            "failed to apply queued listener change; continuing on %s:%s",
                            active_endpoint.host,
                            active_endpoint.port,
                        )
                    else:
                        if active_endpoint == pending_endpoint:
                            pending_endpoint = None
                            pending_endpoint_attempted = False
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
                            audio_codec = request.get("audio_codec", DEFAULT_AUDIO_CODEC)
                            opus_bitrate = int(request.get("opus_bitrate", DEFAULT_OPUS_BITRATE))
                            request_warnings = request["warnings"]
                            if mode == "audio" and audio_codec == "opus":
                                try:
                                    probe_opus_encoder(opus_bitrate)
                                except OpusCodecError as exc:
                                    LOGGER.warning(
                                        "A client requested Opus audio, but Opus is unavailable on the server. PCM audio will be used instead. Details: %s",
                                        exc,
                                    )
                                    audio_codec = DEFAULT_AUDIO_CODEC
                                    request_warnings.append(
                                        f"Opus transport is unavailable on the server; using {DEFAULT_AUDIO_CODEC} audio transport instead"
                                    )
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
                                audio_codec,
                                opus_bitrate,
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
                                audio_codec=audio_codec,
                                opus_bitrate=opus_bitrate,
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
                                handshake["audio_codec"] = audio_codec
                                if audio_codec == "opus":
                                    handshake["opus_bitrate"] = opus_bitrate
                                    handshake["opus_frame_ms"] = OPUS_FRAME_MS
                            if request_warnings:
                                handshake["warnings"] = request_warnings
                                for warning in request_warnings:
                                    LOGGER.warning("Client %s: %s", session.client_number, warning)
                            send_handshake(conn, handshake)
                            session.activate()
                            session.attach_control(conn, control_reader)
                            keep_control_open = True
                        except RequestValidationError as exc:
                            LOGGER.warning("A client request from %s was rejected: %s", address[0], exc)
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
                            LOGGER.warning("A client request from %s was rejected: %s", address[0], exc)
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
            _close_server_socket(stream_server)
            _close_server_socket(control_server)
    finally:
        _close_server_socket(stream_server)
        _close_server_socket(control_server)

    if capture.fatal_error is not None:
        raise capture.fatal_error

    return 0


def _create_listener_pair(endpoint: ListenerEndpoint) -> tuple[socket.socket, socket.socket]:
    _ensure_port_available(endpoint.host, endpoint.port)
    _ensure_port_available(endpoint.host, endpoint.control_port)
    stream_server: socket.socket | None = None
    control_server: socket.socket | None = None
    try:
        stream_server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        control_server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        stream_server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        control_server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        _bind_server_socket(stream_server, endpoint.host, endpoint.port)
        _bind_server_socket(control_server, endpoint.host, endpoint.control_port)
        stream_server.listen()
        control_server.listen()
        return stream_server, control_server
    except Exception:
        if stream_server is not None:
            _close_server_socket(stream_server)
        if control_server is not None:
            _close_server_socket(control_server)
        raise


def _try_apply_listener_endpoint(
    stream_server: socket.socket,
    control_server: socket.socket,
    active_endpoint: ListenerEndpoint,
    target_endpoint: ListenerEndpoint,
) -> tuple[socket.socket, socket.socket, ListenerEndpoint]:
    if target_endpoint == active_endpoint:
        return stream_server, control_server, active_endpoint

    LOGGER.info(
        "attempting listener change to %s:%s (control port %s)",
        target_endpoint.host,
        target_endpoint.port,
        target_endpoint.control_port,
    )
    active_ports = {active_endpoint.port, active_endpoint.control_port}
    target_ports = {target_endpoint.port, target_endpoint.control_port}
    must_close_first = bool(active_ports & target_ports)

    if not must_close_first:
        try:
            next_stream_server, next_control_server = _create_listener_pair(target_endpoint)
        except Exception as exc:
            LOGGER.error("%s", exc)
            LOGGER.warning(
                "continuing on existing listener %s:%s (control port %s)",
                active_endpoint.host,
                active_endpoint.port,
                active_endpoint.control_port,
            )
            return stream_server, control_server, active_endpoint
        _close_server_socket(stream_server)
        _close_server_socket(control_server)
        LOGGER.info(
            "listener changed to %s:%s (control port %s)",
            target_endpoint.host,
            target_endpoint.port,
            target_endpoint.control_port,
        )
        return next_stream_server, next_control_server, target_endpoint

    _close_server_socket(stream_server)
    _close_server_socket(control_server)
    try:
        next_stream_server, next_control_server = _create_listener_pair(target_endpoint)
    except Exception as exc:
        LOGGER.error("%s", exc)
        LOGGER.warning(
            "listener change failed; restoring previous listener %s:%s (control port %s)",
            active_endpoint.host,
            active_endpoint.port,
            active_endpoint.control_port,
        )
        restored_stream_server, restored_control_server = _create_listener_pair(active_endpoint)
        return restored_stream_server, restored_control_server, active_endpoint

    LOGGER.info(
        "listener changed to %s:%s (control port %s)",
        target_endpoint.host,
        target_endpoint.port,
        target_endpoint.control_port,
    )
    return next_stream_server, next_control_server, target_endpoint


def _close_server_socket(sock: socket.socket) -> None:
    try:
        sock.close()
    except OSError:
        pass


def _bind_server_socket(sock: socket.socket, host: str, port: int) -> None:
    try:
        sock.bind((host, port))
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            raise NetworkBindError(_format_port_in_use_message(host, port)) from exc
        raise


def _ensure_port_available(host: str, port: int) -> None:
    owner = _find_port_owner(host, port)
    if owner is not None:
        raise NetworkBindError(_format_port_in_use_message(host, port, owner))


def _format_port_in_use_message(
    host: str,
    port: int,
    owner: PortOwner | None = None,
) -> str:
    if owner is None:
        return (
            f"Port {port} is already in use on {host}. "
            "Please either kill the process using it or select a different port."
        )
    return (
        f"Port {port} is bound by {owner.name} (PID {owner.pid}) on {host}. "
        "Please either kill the process or select a different port."
    )


def _find_port_owner(host: str, port: int) -> PortOwner | None:
    listening_inodes = _find_listening_socket_inodes(host, port)
    if not listening_inodes:
        return None
    return _find_process_for_socket_inode(listening_inodes)


def _find_listening_socket_inodes(host: str, port: int) -> set[str]:
    inodes: set[str] = set()
    for path in (Path("/proc/net/tcp"), Path("/proc/net/tcp6")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if len(fields) < 10:
                continue
            local_address = fields[1]
            state = fields[3]
            inode = fields[9]
            if state != "0A":
                continue
            address_hex, port_hex = local_address.rsplit(":", 1)
            try:
                local_port = int(port_hex, 16)
            except ValueError:
                continue
            if local_port != port:
                continue
            if _socket_address_conflicts(host, address_hex, path.name == "tcp6"):
                inodes.add(inode)
    return inodes


def _socket_address_conflicts(host: str, address_hex: str, is_ipv6: bool) -> bool:
    if host in {"", "0.0.0.0", "::"}:
        return True
    if is_ipv6:
        if address_hex == "0" * 32:
            return True
        try:
            host_ip = socket.getaddrinfo(host, None, socket.AF_INET6)[0][4][0]
            return socket.inet_pton(socket.AF_INET6, host_ip) == bytes.fromhex(address_hex)
        except (OSError, ValueError, IndexError):
            return True
    try:
        packed = bytes.fromhex(address_hex)
        local_ip = socket.inet_ntop(socket.AF_INET, packed[::-1])
        return local_ip == "0.0.0.0" or local_ip == socket.gethostbyname(host)
    except (OSError, ValueError):
        return True


def _find_process_for_socket_inode(inodes: set[str]) -> PortOwner | None:
    socket_targets = {f"socket:[{inode}]" for inode in inodes}
    for proc_entry in Path("/proc").iterdir():
        if not proc_entry.name.isdigit():
            continue
        fd_dir = proc_entry / "fd"
        try:
            fd_entries = list(fd_dir.iterdir())
        except OSError:
            continue
        for fd_entry in fd_entries:
            try:
                if os.readlink(fd_entry) not in socket_targets:
                    continue
            except OSError:
                continue
            pid = int(proc_entry.name)
            return PortOwner(pid=pid, name=_read_process_name(proc_entry))
    return None


def _read_process_name(proc_entry: Path) -> str:
    try:
        cmdline = (proc_entry / "cmdline").read_bytes().replace(b"\x00", b" ").strip()
        if cmdline:
            return cmdline.decode("utf-8", errors="replace")
    except OSError:
        pass
    try:
        comm = (proc_entry / "comm").read_text(encoding="utf-8").strip()
        if comm:
            return comm
    except OSError:
        pass
    return "unknown process"
