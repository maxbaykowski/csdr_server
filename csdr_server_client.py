#!/usr/bin/env python3
"""
Minimal client for csdr_server.py.

The client sends one JSON request to the server, then writes the returned raw
IQ stream to stdout or plays demodulated audio through the default soundcard.
"""

from __future__ import annotations

import argparse
import ctypes
import importlib
import json
import os
import re
import signal
import socket
import sys
import threading
import uuid
from decimal import Decimal, InvalidOperation

EXIT_CONNECT_FAILED = 255
EXIT_OUT_OF_BAND = 1
EXIT_BAD_SAMPLE_RATE = 2
EXIT_REQUEST_ERROR = 3

SHUTDOWN_SIGNAL_EXIT = 0
PR_SET_NAME = 15
DEFAULT_MODE = "iq"
VALID_MODES = (DEFAULT_MODE, "audio")
DEFAULT_MODULATION = "am"
VALID_AUDIO_MODULATIONS = (DEFAULT_MODULATION, "lsb", "nfm", "usb", "wfm", "wfm-stereo")
DEFAULT_SAMPLE_FORMAT = "f32"
VALID_SAMPLE_FORMATS = (DEFAULT_SAMPLE_FORMAT, "s16")
DEFAULT_AUDIO_SAMPLE_FORMAT = "s16"
DEFAULT_AUDIO_CHANNELS = 1
PCM_SAMPLE_WIDTH_BYTES = 2
AUDIO_STREAM_READ_SIZE = 65_536
DEFAULT_AUDIO_PLAYBACK_PREBUFFER_SECONDS = 0.35
DEFAULT_AUDIO_PLAYBACK_LATENCY_SECONDS = 0.25


SUFFIXES = {
    "": 1,
    "K": 1_000,
    "M": 1_000_000,
    "G": 1_000_000_000,
}

_shutdown_requested = False
_active_socket: socket.socket | None = None
_control_socket: socket.socket | None = None


def _set_process_name(name: str) -> None:
    try:
        ctypes.CDLL(None).prctl(PR_SET_NAME, name.encode("utf-8")[:15], 0, 0, 0)
    except Exception:
        pass


def _request_shutdown(signum: int, _frame: object) -> None:
    global _shutdown_requested
    _shutdown_requested = True
    if _active_socket is not None:
        try:
            _active_socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
    if _control_socket is not None:
        try:
            _control_socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


def _install_signal_handlers() -> None:
    signal.signal(signal.SIGINT, _request_shutdown)
    signal.signal(signal.SIGTERM, _request_shutdown)
    if hasattr(signal, "SIGPIPE"):
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)


def _handle_stdout_pipe_closed() -> int:
    global _shutdown_requested
    _shutdown_requested = True
    if _active_socket is not None:
        try:
            _active_socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
    if _control_socket is not None:
        try:
            _control_socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
    try:
        devnull = open("/dev/null", "wb")
        os.dup2(devnull.fileno(), sys.stdout.fileno())
    except OSError:
        pass
    return 0


def _exit_after_stdout_pipe_closed() -> "NoReturn":
    _handle_stdout_pipe_closed()
    os._exit(0)


def _write_stdout_unbuffered(data: bytes) -> None:
    stdout_fd = sys.stdout.fileno()
    view = memoryview(data)
    while view:
        written = os.write(stdout_fd, view)
        view = view[written:]


def _option_was_provided(options: tuple[str, ...]) -> bool:
    argv = sys.argv[1:]
    for arg in argv:
        if arg in options:
            return True
        for option in options:
            if option.startswith("--") and arg.startswith(f"{option}="):
                return True
            if option.startswith("-") and not option.startswith("--") and arg.startswith(option):
                if arg != option:
                    return True
    return False


def parse_scaled_integer(value: str, label: str) -> int:
    text = value.strip().upper()
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)([KMG]?)", text)
    if not match:
        raise argparse.ArgumentTypeError(
            f"invalid {label}: {value!r}; expected a number with optional K, M, or G suffix"
        )

    try:
        magnitude = Decimal(match.group(1))
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError(f"invalid {label}: {value!r}") from exc

    scaled = magnitude * SUFFIXES[match.group(2)]
    if scaled <= 0:
        raise argparse.ArgumentTypeError(f"{label} must be positive")
    if scaled != scaled.to_integral_value():
        raise argparse.ArgumentTypeError(
            f"{label} must resolve to a whole number of Hz or samples per second"
        )
    return int(scaled)


def parse_frequency(value: str) -> int:
    return parse_scaled_integer(value, "frequency")


def parse_sample_rate(value: str) -> int:
    return parse_scaled_integer(value, "sample rate")


def normalize_audio_modulation(value: str) -> str:
    return value.strip().lower().replace("-", "_")


def parse_nonnegative_float(value: str, label: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid {label}: {value!r}") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError(f"{label} must be greater than or equal to 0")
    return parsed


def parse_audio_prebuffer(value: str) -> float:
    return parse_nonnegative_float(value, "audio prebuffer")


def parse_audio_latency(value: str) -> float:
    return parse_nonnegative_float(value, "audio latency")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Minimal client for csdr_server.py")
    parser.add_argument("-a", "--address", required=True, help="Server IP address or hostname")
    parser.add_argument("-p", "--port", required=True, type=int, help="Server TCP port")
    parser.add_argument("-f", "--frequency", required=True, type=parse_frequency, help="Tuned frequency in Hz, or with K/M/G suffix")
    parser.add_argument(
        "-m",
        "--mode",
        default=DEFAULT_MODE,
        choices=VALID_MODES,
        help="Request IQ or demodulated audio",
    )
    parser.add_argument(
        "-s",
        "--sample-rate",
        type=parse_sample_rate,
        help="Output sample rate in Sps, or with K/M/G suffix",
    )
    parser.add_argument(
        "-F",
        "--format",
        default=DEFAULT_SAMPLE_FORMAT,
        choices=VALID_SAMPLE_FORMATS,
        help="Requested output IQ format",
    )
    parser.add_argument(
        "-M",
        "--modulation",
        default=DEFAULT_MODULATION,
        choices=VALID_AUDIO_MODULATIONS,
        help="Audio modulation type",
    )
    parser.add_argument(
        "--stdout",
        action="store_true",
        help="Write received stream to stdout instead of playing audio locally in audio mode",
    )
    parser.add_argument(
        "-B",
        "--audio-prebuffer",
        type=parse_audio_prebuffer,
        default=DEFAULT_AUDIO_PLAYBACK_PREBUFFER_SECONDS,
        help="Audio playback prebuffer in seconds",
    )
    parser.add_argument(
        "-L",
        "--audio-latency",
        type=parse_audio_latency,
        default=DEFAULT_AUDIO_PLAYBACK_LATENCY_SECONDS,
        help="Requested audio device latency in seconds",
    )
    return parser.parse_args()


def _should_play_audio(args: argparse.Namespace) -> bool:
    return args.mode == "audio" and not args.stdout


def _load_sounddevice_module():
    try:
        return importlib.import_module("sounddevice")
    except ModuleNotFoundError:
        print(
            "error: audio playback requires the Python package 'sounddevice'; "
            "reinstall csdr_server_client or run 'python3 -m pip install sounddevice'",
            file=sys.stderr,
        )
        return None


def _stream_to_stdout(sock_file) -> int:
    while True:
        chunk = sock_file.read1(AUDIO_STREAM_READ_SIZE)
        if not chunk:
            break
        if _shutdown_requested:
            return SHUTDOWN_SIGNAL_EXIT
        _write_stdout_unbuffered(chunk)
    if _shutdown_requested:
        return SHUTDOWN_SIGNAL_EXIT
    return 0


def _stream_audio_to_soundcard(
    sock_file,
    handshake: dict[str, object],
    prebuffer_seconds: float,
    latency_seconds: float,
) -> int:
    sample_rate = int(handshake.get("sample_rate", 0))
    channels = int(handshake.get("channels", DEFAULT_AUDIO_CHANNELS))
    sample_format = str(handshake.get("format", "")).lower()
    if sample_rate <= 0:
        print("error: server audio handshake is missing a valid sample_rate", file=sys.stderr)
        return EXIT_REQUEST_ERROR
    if channels <= 0:
        print("error: server audio handshake is missing a valid channel count", file=sys.stderr)
        return EXIT_REQUEST_ERROR
    if sample_format != DEFAULT_AUDIO_SAMPLE_FORMAT:
        print(
            f"error: audio playback only supports {DEFAULT_AUDIO_SAMPLE_FORMAT}; server sent {sample_format!r}",
            file=sys.stderr,
        )
        return EXIT_REQUEST_ERROR

    sounddevice = _load_sounddevice_module()
    if sounddevice is None:
        return EXIT_REQUEST_ERROR

    bytes_per_frame = PCM_SAMPLE_WIDTH_BYTES * channels
    bytes_per_second = sample_rate * bytes_per_frame
    prebuffer_target = max(
        bytes_per_frame,
        int(bytes_per_second * prebuffer_seconds),
    )
    prebuffer_target -= prebuffer_target % bytes_per_frame
    if prebuffer_target <= 0:
        prebuffer_target = bytes_per_frame
    prebuffer = bytearray()
    remainder = b""
    try:
        while len(prebuffer) < prebuffer_target:
            chunk = sock_file.read1(AUDIO_STREAM_READ_SIZE)
            if not chunk:
                break
            if _shutdown_requested:
                return SHUTDOWN_SIGNAL_EXIT
            prebuffer.extend(chunk)

        aligned_prebuffer_size = len(prebuffer) - (len(prebuffer) % bytes_per_frame)
        prebuffer_remainder = b""
        if aligned_prebuffer_size < len(prebuffer):
            prebuffer_remainder = bytes(prebuffer[aligned_prebuffer_size:])
        initial_audio = bytes(prebuffer[:aligned_prebuffer_size])

        with sounddevice.RawOutputStream(
            samplerate=sample_rate,
            channels=channels,
            dtype="int16",
            latency=latency_seconds,
        ) as stream:
            if initial_audio:
                stream.write(initial_audio)
            remainder = prebuffer_remainder
            while True:
                chunk = sock_file.read1(AUDIO_STREAM_READ_SIZE)
                if not chunk:
                    break
                if _shutdown_requested:
                    return SHUTDOWN_SIGNAL_EXIT
                payload = remainder + chunk
                aligned_size = len(payload) - (len(payload) % bytes_per_frame)
                if aligned_size:
                    stream.write(payload[:aligned_size])
                remainder = payload[aligned_size:]
    except Exception as exc:
        error_type = type(exc).__name__
        print(f"error: audio playback failed ({error_type}): {exc}", file=sys.stderr)
        return EXIT_REQUEST_ERROR
    if _shutdown_requested:
        return SHUTDOWN_SIGNAL_EXIT
    return 0


def _start_interactive_control(
    control_sock: socket.socket,
    control_file,
) -> threading.Thread | None:
    if not sys.stdin.isatty():
        return None

    def _interactive_loop() -> None:
        print(
            "interactive control enabled; type 'frequency <value>' to retune",
            file=sys.stderr,
        )
        while not _shutdown_requested:
            line = sys.stdin.readline()
            if line == "":
                break
            text = line.strip()
            if not text:
                continue
            command, _, remainder = text.partition(" ")
            if command.lower() != "frequency":
                print("error: unsupported interactive command; use 'frequency <value>'", file=sys.stderr)
                continue
            if not remainder.strip():
                print("error: frequency command requires a value", file=sys.stderr)
                continue
            try:
                frequency = parse_frequency(remainder.strip())
            except argparse.ArgumentTypeError as exc:
                print(f"error: {exc}", file=sys.stderr)
                continue
            try:
                control_sock.sendall(
                    json.dumps(
                        {
                            "command": "retune",
                            "frequency": frequency,
                        }
                    ).encode("utf-8")
                    + b"\n"
                )
                response_line = control_file.readline(16_384)
                if not response_line:
                    print("error: control socket closed by server", file=sys.stderr)
                    break
                response = json.loads(response_line.decode("utf-8"))
                if response.get("status") != "ok":
                    print(
                        f"error: {response.get('error', 'retune rejected by server')}",
                        file=sys.stderr,
                    )
                    continue
                print(
                    f"retuned to {int(response.get('frequency', frequency))} Hz",
                    file=sys.stderr,
                )
            except OSError as exc:
                if not _shutdown_requested:
                    print(f"error: control command failed: {exc}", file=sys.stderr)
                break
            except json.JSONDecodeError as exc:
                print(f"error: invalid control response from server: {exc}", file=sys.stderr)
                break

    thread = threading.Thread(
        target=_interactive_loop,
        name="client-control-input",
        daemon=True,
    )
    thread.start()
    return thread


def main() -> int:
    global _active_socket, _control_socket

    _set_process_name("csdr_client")
    _install_signal_handlers()
    args = parse_args()
    if args.port >= 65535:
        print("error: stream port must be between 1 and 65534 so the control socket can use port+1", file=sys.stderr)
        return EXIT_CONNECT_FAILED
    stream_token = uuid.uuid4().hex
    request = {
        "frequency": args.frequency,
        "mode": args.mode,
        "stream_token": stream_token,
    }
    if args.mode == "iq":
        if args.sample_rate is None:
            print("error: iq mode requires --sample-rate", file=sys.stderr)
            return EXIT_REQUEST_ERROR
        request["sample_rate"] = args.sample_rate
        request["format"] = args.format
    else:
        if _option_was_provided(("-s", "--sample-rate")):
            request["sample_rate"] = args.sample_rate
        if _option_was_provided(("-F", "--format")):
            request["format"] = args.format
        request["modulation"] = normalize_audio_modulation(args.modulation)

    try:
        stream_sock = socket.create_connection((args.address, args.port), timeout=30.0)
    except socket.timeout:
        print(
            f"error: timed out after 30 seconds connecting to {args.address}:{args.port}",
            file=sys.stderr,
        )
        return EXIT_CONNECT_FAILED
    except ConnectionRefusedError:
        print(
            f"error: connection refused by {args.address}:{args.port}",
            file=sys.stderr,
        )
        return EXIT_CONNECT_FAILED
    except OSError as exc:
        print(
            f"error: could not connect to {args.address}:{args.port}: {exc}",
            file=sys.stderr,
        )
        return EXIT_CONNECT_FAILED

    try:
        stream_sock.sendall(stream_token.encode("utf-8") + b"\n")
    except OSError as exc:
        stream_sock.close()
        print(f"error: could not initialize stream socket: {exc}", file=sys.stderr)
        return EXIT_CONNECT_FAILED

    control_port = args.port + 1
    try:
        control_sock = socket.create_connection((args.address, control_port), timeout=30.0)
    except socket.timeout:
        stream_sock.close()
        print(
            f"error: timed out after 30 seconds connecting to {args.address}:{control_port}",
            file=sys.stderr,
        )
        return EXIT_CONNECT_FAILED
    except ConnectionRefusedError:
        stream_sock.close()
        print(
            f"error: connection refused by {args.address}:{control_port}",
            file=sys.stderr,
        )
        return EXIT_CONNECT_FAILED
    except OSError as exc:
        stream_sock.close()
        print(
            f"error: could not connect to {args.address}:{control_port}: {exc}",
            file=sys.stderr,
        )
        return EXIT_CONNECT_FAILED

    with stream_sock, control_sock:
        _active_socket = stream_sock
        _control_socket = control_sock
        try:
            control_sock.sendall(json.dumps(request).encode("utf-8") + b"\n")
            control_file = control_sock.makefile("rb")
            handshake_line = control_file.readline(16_384)
            if _shutdown_requested:
                return SHUTDOWN_SIGNAL_EXIT
            if not handshake_line:
                print("error: server closed connection before handshake", file=sys.stderr)
                return EXIT_REQUEST_ERROR
            handshake = json.loads(handshake_line.decode("utf-8"))
            if handshake.get("status") != "ok":
                message = handshake.get("error", "server rejected request")
                print(f"error: {message}", file=sys.stderr)
                return int(handshake.get("code", EXIT_REQUEST_ERROR))
            for warning in handshake.get("warnings", []):
                print(f"warning: {warning}", file=sys.stderr)

            _start_interactive_control(control_sock, control_file)

            stream_sock.settimeout(None)
            sock_file = stream_sock.makefile("rb")
            if _should_play_audio(args):
                return _stream_audio_to_soundcard(
                    sock_file,
                    handshake,
                    args.audio_prebuffer,
                    args.audio_latency,
                )
            return _stream_to_stdout(sock_file)
        except BrokenPipeError:
            _exit_after_stdout_pipe_closed()
        except json.JSONDecodeError as exc:
            print(f"error: invalid handshake from server: {exc}", file=sys.stderr)
            return EXIT_REQUEST_ERROR
        except ConnectionResetError:
            if _shutdown_requested:
                return SHUTDOWN_SIGNAL_EXIT
            print("error: connection reset by server", file=sys.stderr)
            return EXIT_REQUEST_ERROR
        except OSError as exc:
            if _shutdown_requested:
                return SHUTDOWN_SIGNAL_EXIT
            print(f"error: streaming failed: {exc}", file=sys.stderr)
            return EXIT_REQUEST_ERROR
        finally:
            _active_socket = None
            _control_socket = None

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        _exit_after_stdout_pipe_closed()
