from __future__ import annotations

import queue
import socket
import threading
import time
from typing import Any

from .config import ServerConfig
from .constants import (
    EXIT_REQUEST_ERROR,
    LOGGER,
    SQUELCH_CLOSE_DELAY_SECONDS,
    SQUELCH_HYSTERESIS,
    SQUELCH_MIN_LEVEL,
    SQUELCH_OPEN_DELAY_SECONDS,
)
from .dsp import _normalize_audio_modulation, _validate_audio_modulation, _validate_audio_modulation_supported
from .errors import RequestValidationError
from .graph import IqPowerMonitor, SharedStream
from .opus_codec import DEFAULT_AUDIO_CODEC, DEFAULT_OPUS_BITRATE, validate_opus_bitrate
from .protocol import (
    _build_session_status_payload,
    _parse_request_frequency,
    _parse_squelch_level,
    _validate_squelch_level,
    parse_control_command,
    send_handshake,
)
from .rds import RdsDecoder

class ClientSession:
    def __init__(
        self,
        conn: socket.socket,
        address: tuple[str, int],
        manager: Any,
        config: ServerConfig,
        source_stream: SharedStream,
        frequency: int,
        mode: str,
        output_rate: int,
        sample_format: str | None,
        modulation: str | None,
        client_number: int = 0,
        squelch_level: int = 0,
        power_monitor: IqPowerMonitor | None = None,
        audio_codec: str = DEFAULT_AUDIO_CODEC,
        opus_bitrate: int = DEFAULT_OPUS_BITRATE,
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
        self.client_number = client_number
        self.squelch_level = squelch_level
        self.power_monitor = power_monitor
        self.audio_codec = audio_codec
        self.opus_bitrate = opus_bitrate
        self.squelch_open = True
        self.squelch_last_signal_at = time.monotonic()
        self.squelch_open_candidate_since: float | None = None
        self.chunk_queue: queue.Queue[bytes | None] = queue.Queue(
            maxsize=config.client_queue_chunks
        )
        self.closed = threading.Event()
        self.output_thread: threading.Thread | None = None
        self.control_conn: socket.socket | None = None
        self.control_reader: Any = None
        self.control_thread: threading.Thread | None = None
        self.control_send_lock = threading.Lock()
        self.rds_subscribed = False
        self.rds_decoder: RdsDecoder | None = None

    def start(self) -> None:
        self.source_stream.add_subscriber(self)
        if self.power_monitor is not None:
            self.power_monitor.add_client(self)

    def activate(self) -> None:
        self.output_thread = threading.Thread(
            target=self._output_loop,
            name=f"client-output-{self.address[0]}:{self.address[1]}",
            daemon=True,
        )
        self.output_thread.start()
        LOGGER.info(self._format_client_connected_log())

    def attach_control(self, conn: socket.socket, reader: Any) -> None:
        self.control_conn = conn
        self.control_reader = reader
        self.control_thread = threading.Thread(
            target=self._control_loop,
            name=f"client-control-{self.address[0]}:{self.address[1]}",
            daemon=True,
        )
        self.control_thread.start()

    def enqueue(self, chunk: bytes) -> None:
        if self.closed.is_set():
            return
        if self.audio_codec != "opus" and self._should_squelch_chunk():
            chunk = bytes(len(chunk))
        try:
            self.chunk_queue.put(chunk, timeout=self.config.enqueue_timeout_seconds)
        except queue.Full:
            LOGGER.warning(
                "Client %s couldn't keep up and was dropped",
                self.client_number,
            )
            self.close("client backlog")

    def close(self, reason: str) -> None:
        if self.closed.is_set():
            return
        LOGGER.info("Client %s has disconnected", self.client_number)
        LOGGER.debug("closing client %s:%s: %s", self.address[0], self.address[1], reason)
        self.closed.set()
        self.manager.unregister_client(self)
        if self.rds_decoder is not None:
            self.rds_decoder.remove_subscriber(self)
            self.rds_decoder = None
            self.rds_subscribed = False
        if self.power_monitor is not None:
            self.power_monitor.remove_client(self)
            self.power_monitor = None
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
        if self.control_conn is not None:
            try:
                self.control_conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self.control_conn.close()
            except OSError:
                pass

    def _output_loop(self) -> None:
        try:
            while not self.closed.is_set():
                data = self.chunk_queue.get()
                if data is None:
                    break
                self.conn.sendall(data)
        except (BrokenPipeError, ConnectionResetError, OSError):
            LOGGER.debug(
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
        self._reset_squelch_for_stream_change()
        old_stream = self.source_stream
        new_stream.add_subscriber(self)
        self.source_stream = new_stream
        old_stream.remove_subscriber(self)
        LOGGER.debug(
            "client %s:%s switched to stream %s",
            self.address[0],
            self.address[1],
            new_stream.name,
        )

    def switch_power_monitor(self, new_monitor: IqPowerMonitor | None) -> None:
        if self.closed.is_set() or new_monitor is self.power_monitor:
            return
        old_monitor = self.power_monitor
        if new_monitor is not None:
            new_monitor.add_client(self)
        self.power_monitor = new_monitor
        self._reset_squelch_for_stream_change()
        if old_monitor is not None:
            old_monitor.remove_client(self)

    def set_squelch_level(self, level: int) -> None:
        _validate_squelch_level(level)
        self.squelch_level = level
        if level <= 0:
            self.squelch_open = True
        elif self.power_monitor is not None and self.power_monitor.current_level() >= level:
            self.squelch_open = True
        else:
            self.squelch_open = False
        self.squelch_last_signal_at = time.monotonic()
        self.squelch_open_candidate_since = None

    def _reset_squelch_for_stream_change(self) -> None:
        if self.squelch_level > 0:
            self.squelch_open = False
        else:
            self.squelch_open = True
        self.squelch_last_signal_at = time.monotonic()
        self.squelch_open_candidate_since = None

    def _should_squelch_chunk(self) -> bool:
        if self.mode != "audio" or self.squelch_level <= 0 or self.power_monitor is None:
            return False
        now = time.monotonic()
        measured_level = self.power_monitor.current_level()
        if self.squelch_open:
            if measured_level >= max(SQUELCH_MIN_LEVEL, self.squelch_level - SQUELCH_HYSTERESIS):
                self.squelch_last_signal_at = now
                self.squelch_open_candidate_since = None
                return False
            if now - self.squelch_last_signal_at < SQUELCH_CLOSE_DELAY_SECONDS:
                return False
            self.squelch_open = False
            self.squelch_open_candidate_since = None
            return True

        if measured_level >= self.squelch_level:
            if self.squelch_open_candidate_since is None:
                self.squelch_open_candidate_since = now
            if now - self.squelch_open_candidate_since >= SQUELCH_OPEN_DELAY_SECONDS:
                self.squelch_open = True
                self.squelch_last_signal_at = now
                return False
        else:
            self.squelch_open_candidate_since = None
        return True

    def _control_loop(self) -> None:
        if self.control_conn is None or self.control_reader is None:
            return
        try:
            while not self.closed.is_set():
                try:
                    message = parse_control_command(self.control_reader)
                    if message is None:
                        break
                    command = message.get("command")
                    if command == "retune":
                        frequency = _parse_request_frequency(message)
                        self.manager.reconfigure_client(self, frequency=frequency)
                        LOGGER.info("Client %s retuned to %s Hz", self.client_number, self.frequency)
                        self.send_control(_build_session_status_payload(self, command="retune"))
                    elif command == "demod":
                        modulation = message.get("modulation")
                        if modulation is None:
                            raise RequestValidationError(
                                EXIT_REQUEST_ERROR,
                                "demod command must include modulation",
                            )
                        self.manager.reconfigure_client(
                            self,
                            modulation=_normalize_audio_modulation(modulation),
                        )
                        LOGGER.info(
                            "Client %s changed demodulation mode to %s",
                            self.client_number,
                            str(self.modulation).upper(),
                        )
                        self.send_control(_build_session_status_payload(self, command="demod"))
                    elif command == "rds":
                        action = str(message.get("action", "")).strip().lower()
                        if action not in {"start", "stop"}:
                            raise RequestValidationError(
                                EXIT_REQUEST_ERROR,
                                "rds command must include action 'start' or 'stop'",
                            )
                        self.manager.set_rds_subscription(self, enabled=(action == "start"))
                        LOGGER.info(
                            "Client %s has %s RDS decoding",
                            self.client_number,
                            "started" if action == "start" else "stopped",
                        )
                        self.send_control(_build_session_status_payload(self, command="rds"))
                    elif command == "squelch":
                        if self.mode != "audio":
                            raise RequestValidationError(
                                EXIT_REQUEST_ERROR,
                                "squelch command is only supported in audio mode",
                            )
                        level = _parse_squelch_level(message.get("level", 0))
                        self.set_squelch_level(level)
                        LOGGER.info(
                            "Client %s set squelch level to %s",
                            self.client_number,
                            self.squelch_level,
                        )
                        self.send_control(_build_session_status_payload(self, command="squelch"))
                    elif command == "bitrate":
                        if self.mode != "audio":
                            raise RequestValidationError(
                                EXIT_REQUEST_ERROR,
                                "bitrate command is only supported in audio mode",
                            )
                        if self.audio_codec != "opus":
                            raise RequestValidationError(
                                EXIT_REQUEST_ERROR,
                                "bitrate only applies when using the opus codec",
                            )
                        try:
                            bitrate = validate_opus_bitrate(message.get("bitrate"))
                        except ValueError as exc:
                            raise RequestValidationError(EXIT_REQUEST_ERROR, str(exc)) from exc
                        self.manager.reconfigure_client(self, opus_bitrate=bitrate)
                        LOGGER.info(
                            "Client %s set opus bitrate to %s bps",
                            self.client_number,
                            self.opus_bitrate,
                        )
                        self.send_control(_build_session_status_payload(self, command="bitrate"))
                    else:
                        raise RequestValidationError(
                            EXIT_REQUEST_ERROR,
                            f"unsupported control command {command!r}",
                        )
                except RequestValidationError as exc:
                    LOGGER.debug(
                        "rejecting control command from client %s:%s: %s",
                        self.address[0],
                        self.address[1],
                        exc,
                    )
                    self.send_control(
                        {
                            "status": "error",
                            "code": exc.code,
                            "error": exc.message,
                        },
                    )
        except OSError:
            if not self.closed.is_set():
                LOGGER.debug(
                    "control connection closed for client %s:%s",
                    self.address[0],
                    self.address[1],
                )
        except Exception:
            LOGGER.exception(
                "control loop failed for client %s:%s",
                self.address[0],
                self.address[1],
            )
        finally:
            self.close("control loop ended")

    def send_control(self, payload: dict[str, Any]) -> None:
        if self.control_conn is None:
            raise OSError("control connection is not attached")
        with self.control_send_lock:
            send_handshake(self.control_conn, payload)

    def _format_client_connected_log(self) -> str:
        lines = [
            "A client has connected:",
            f"Client number: {self.client_number}",
            f"IP address: {self.address[0]}",
            f"Mode: {'raw' if self.mode == 'iq' else 'audio'}",
            f"Frequency: {self.frequency} Hz",
        ]
        if self.mode == "audio":
            lines.extend(
                [
                    f"Demodulation mode: {str(self.modulation).replace('_', '-')}",
                    f"squelch: {self.squelch_level}",
                    f"Codec: {self.audio_codec}",
                ]
            )
            if self.audio_codec == "opus":
                lines.append(f"Bitrate: {self.opus_bitrate} bps")
        else:
            lines.extend(
                [
                    f"Sample rate: {self.output_rate} s/s",
                    f"IQ format: {self._display_iq_format()}",
                ]
            )
        return "\n".join(lines)

    def _display_iq_format(self) -> str:
        if self.sample_format == "f32":
            return "float"
        if self.sample_format == "s16":
            return "S16"
        return str(self.sample_format)
