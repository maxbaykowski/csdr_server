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
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import queue

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
SERVER_AUDIO_SAMPLE_RATE = 48_000
SERVER_AUDIO_CHANNELS = 2
DEFAULT_AUDIO_CHANNELS = SERVER_AUDIO_CHANNELS
PCM_SAMPLE_WIDTH_BYTES = 2
AUDIO_STREAM_READ_SIZE = 65_536
MAX_AUDIO_RECONFIGURE_DRAIN_BYTES = 4 * 1024 * 1024
FALLBACK_AUDIO_PLAYBACK_SAMPLE_RATE = 48_000
FALLBACK_AUDIO_PLAYBACK_CHANNELS = 2
DEFAULT_AUDIO_PLAYBACK_PREBUFFER_SECONDS = 0.35
DEFAULT_AUDIO_PLAYBACK_LATENCY_SECONDS = 0.25
DEFAULT_WINDOWS_AUDIO_PLAYBACK_LATENCY_SECONDS = 0.05
WINDOWS_WASAPI_HOST_API_NAME = "Windows WASAPI"
PORTAUDIO_HOST_API_ALIASES = {
    "alsa": ("ALSA",),
    "jack": ("JACK Audio Connection Kit", "JACK"),
    "oss": ("OSS",),
    "wasapi": (WINDOWS_WASAPI_HOST_API_NAME,),
    "directsound": ("Windows DirectSound",),
    "mme": ("MME",),
    "wdmks": ("Windows WDM-KS",),
    "coreaudio": ("Core Audio",),
}
SQUELCH_MIN_LEVEL = 0
SQUELCH_MAX_LEVEL = 100


SUFFIXES = {
    "": 1,
    "K": 1_000,
    "M": 1_000_000,
    "G": 1_000_000_000,
}

_shutdown_requested = False
_active_socket: socket.socket | None = None
_control_socket: socket.socket | None = None
_fatal_control_failure = False
INTERACTIVE_PROMPT = "control> "


@dataclass(frozen=True)
class AudioPlaybackConfig:
    sample_rate: int
    channels: int
    sample_format: str


class AudioPlaybackState:
    def __init__(self, initial_config: AudioPlaybackConfig) -> None:
        self._lock = threading.Lock()
        self._active_config = initial_config
        self._pending_config: AudioPlaybackConfig | None = None

    def active_config(self) -> AudioPlaybackConfig:
        with self._lock:
            return self._active_config

    def request_reconfigure(self, config: AudioPlaybackConfig) -> None:
        with self._lock:
            if config == self._active_config:
                return
            self._pending_config = config

    def take_pending_for(self, current: AudioPlaybackConfig) -> AudioPlaybackConfig | None:
        with self._lock:
            if self._pending_config is None or self._active_config != current:
                return None
            pending = self._pending_config
            self._active_config = pending
            self._pending_config = None
            return pending


class RdsDisplay:
    def __init__(self, output, prompt_callback=None) -> None:
        self.output = output
        self.prompt_callback = prompt_callback
        self.fields: dict[str, str] = {}
        self._lock = threading.Lock()

    def update(self, changed_fields: dict[str, object]) -> None:
        with self._lock:
            for key, value in changed_fields.items():
                if value is None:
                    self.fields.pop(key, None)
                else:
                    self.fields[key] = str(value)
            self._render_locked()

    def clear(self) -> None:
        with self._lock:
            self.fields.clear()
            self._render_locked(clear_only=True)

    def _render_locked(self, clear_only: bool = False) -> None:
        if clear_only:
            return

        ordered_keys = [
            ("callsign", "Call"),
            ("program_service", "PS"),
            ("radiotext", "Text"),
            ("artist", "Artist"),
            ("title", "Title"),
            ("program_type", "Type"),
        ]
        lines = [f"  {label}: {self.fields[key]}" for key, label in ordered_keys if key in self.fields]
        if not lines:
            return
        print("", file=self.output)
        print("RDS:", file=self.output)
        for line in lines:
            print(line, file=self.output)
        print("", file=self.output, flush=True)
        if self.prompt_callback is not None:
            self.prompt_callback()


class ControlChannel:
    def __init__(
        self,
        sock: socket.socket,
        reader,
        playback_state: AudioPlaybackState | None,
        rds_output,
        prompt_callback=None,
    ) -> None:
        self.sock = sock
        self.reader = reader
        self.playback_state = playback_state
        self.rds_display = RdsDisplay(rds_output, prompt_callback=prompt_callback)
        self.response_queue: queue.Queue[dict[str, object] | None] = queue.Queue()
        self.thread = threading.Thread(
            target=self._reader_loop,
            name="client-control-reader",
            daemon=True,
        )
        self.thread.start()

    def send_command(self, payload: dict[str, object]) -> dict[str, object] | None:
        self.sock.sendall(json.dumps(payload).encode("utf-8") + b"\n")
        while not _shutdown_requested:
            try:
                return self.response_queue.get(timeout=0.25)
            except queue.Empty:
                continue
        return None

    def _reader_loop(self) -> None:
        try:
            while not _shutdown_requested:
                line = self.reader.readline(16_384)
                if not line:
                    if not _shutdown_requested:
                        print("error: control socket closed by server", file=sys.stderr)
                        _fail_control_channel()
                    break
                message = json.loads(line.decode("utf-8"))
                if message.get("event") == "rds":
                    fields = message.get("fields", {})
                    if isinstance(fields, dict):
                        self.rds_display.update(fields)
                    continue
                self.response_queue.put(message)
        except json.JSONDecodeError as exc:
            print(f"error: invalid control response from server: {exc}", file=sys.stderr)
            _fail_control_channel()
        except OSError as exc:
            if not _shutdown_requested:
                print(f"error: control channel failed: {exc}", file=sys.stderr)
                _fail_control_channel()
        finally:
            self.response_queue.put(None)


def _set_process_name(name: str) -> None:
    try:
        ctypes.CDLL(None).prctl(PR_SET_NAME, name.encode("utf-8")[:15], 0, 0, 0)
    except Exception:
        pass


def _request_shutdown(signum: int, _frame: object) -> None:
    global _shutdown_requested
    _shutdown_requested = True
    _shutdown_sockets()


def _shutdown_sockets() -> None:
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


def _fail_control_channel() -> None:
    global _shutdown_requested, _fatal_control_failure
    _fatal_control_failure = True
    _shutdown_requested = True
    _shutdown_sockets()


def _install_signal_handlers() -> None:
    signal.signal(signal.SIGINT, _request_shutdown)
    signal.signal(signal.SIGTERM, _request_shutdown)
    if hasattr(signal, "SIGPIPE"):
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)


def _handle_stdout_pipe_closed() -> int:
    global _shutdown_requested
    _shutdown_requested = True
    _shutdown_sockets()
    try:
        devnull = open(os.devnull, "wb")
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


def _set_stdout_binary_mode() -> None:
    if os.name != "nt":
        return
    try:
        import msvcrt

        msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)
    except (ImportError, OSError, AttributeError):
        pass


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


def parse_control_modulation(value: str) -> str:
    normalized = normalize_audio_modulation(value)
    valid = {normalize_audio_modulation(item) for item in VALID_AUDIO_MODULATIONS}
    if normalized not in valid:
        raise argparse.ArgumentTypeError(
            f"invalid modulation: {value!r}; expected one of {', '.join(VALID_AUDIO_MODULATIONS)}"
        )
    return normalized


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


def parse_audio_device(value: str) -> int | str:
    try:
        return int(value)
    except ValueError:
        return value


def parse_squelch_level(value: str) -> int:
    try:
        level = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid squelch level: {value!r}") from exc
    if level < SQUELCH_MIN_LEVEL or level > SQUELCH_MAX_LEVEL:
        raise argparse.ArgumentTypeError(
            f"squelch level must be between {SQUELCH_MIN_LEVEL} and {SQUELCH_MAX_LEVEL}"
        )
    return level


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
        "--rds",
        action="store_true",
        help="Subscribe to WFM RDS events immediately after connect",
    )
    parser.add_argument(
        "-l",
        "--squelch",
        type=parse_squelch_level,
        default=0,
        help="Audio squelch level from 0 to 100; 0 disables squelch",
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
        default=None,
        help="Requested audio device latency in seconds",
    )
    parser.add_argument(
        "--audio-device",
        type=parse_audio_device,
        help="Audio output device index or case-insensitive name substring",
    )
    parser.add_argument(
        "--audio-hostapi",
        choices=("auto", "default", "alsa", "jack", "oss", "wasapi", "directsound", "mme", "wdmks", "coreaudio"),
        default="auto",
        help="Audio host API preference; auto uses platform defaults, except Windows prefers WASAPI",
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


def _load_audio_resampler_modules():
    try:
        soxr = importlib.import_module("soxr")
    except ModuleNotFoundError:
        print(
            "error: audio playback requires the Python package 'soxr'; "
            "reinstall csdr_server_client or run 'python3 -m pip install soxr'",
            file=sys.stderr,
        )
        return None, None
    try:
        numpy = importlib.import_module("numpy")
    except ModuleNotFoundError:
        print(
            "error: audio playback requires the Python package 'numpy', which should be installed with soxr",
            file=sys.stderr,
        )
        return None, None
    return soxr, numpy


def _audio_platform_hint() -> str:
    if sys.platform == "win32":
        return (
            " Check that a usable Windows output device exists and is not busy, "
            "and that PortAudio support is available to python-sounddevice."
        )
    if sys.platform == "darwin":
        return (
            " Check that a usable macOS output device exists and that PortAudio/CoreAudio "
            "support is available to python-sounddevice."
        )
    return (
        " Check that a usable output device exists and that PortAudio support is installed "
        "for python-sounddevice."
    )


def _default_audio_latency() -> float:
    if sys.platform == "win32":
        return DEFAULT_WINDOWS_AUDIO_PLAYBACK_LATENCY_SECONDS
    return DEFAULT_AUDIO_PLAYBACK_LATENCY_SECONDS


def _hostapi_matches(hostapi: dict[str, object], requested: str) -> bool:
    hostapi_name = str(hostapi.get("name", "")).lower()
    return any(
        hostapi_name == alias.lower()
        for alias in PORTAUDIO_HOST_API_ALIASES.get(requested, (requested,))
    )


def _resolve_audio_output_device(sounddevice, requested_device: int | str | None, hostapi_preference: str) -> int | str | None:
    if hostapi_preference == "auto" and sys.platform != "win32":
        hostapi_preference = "default"
    if hostapi_preference == "auto":
        hostapi_preference = "wasapi"
    if hostapi_preference == "default" and requested_device is None:
        return None

    hostapis = sounddevice.query_hostapis()
    devices = sounddevice.query_devices()
    allowed_hostapi_indexes = {
        index
        for index, hostapi in enumerate(hostapis)
        if hostapi_preference == "default" or _hostapi_matches(hostapi, hostapi_preference)
    }
    if not allowed_hostapi_indexes:
        raise RuntimeError(f"requested audio host API {hostapi_preference!r} is not available")

    if requested_device is None:
        for hostapi_index in allowed_hostapi_indexes:
            default_output = int(hostapis[hostapi_index].get("default_output_device", -1))
            if default_output >= 0:
                return default_output
        raise RuntimeError(f"requested audio host API {hostapi_preference!r} has no default output device")

    if isinstance(requested_device, int):
        device = devices[requested_device]
        if int(device.get("max_output_channels", 0)) <= 0:
            raise RuntimeError(f"audio device {requested_device} has no output channels")
        if int(device.get("hostapi", -1)) not in allowed_hostapi_indexes:
            raise RuntimeError(
                f"audio device {requested_device} is not on requested host API {hostapi_preference!r}"
            )
        return requested_device

    requested_text = requested_device.lower()
    matches = [
        index
        for index, device in enumerate(devices)
        if int(device.get("max_output_channels", 0)) > 0
        and int(device.get("hostapi", -1)) in allowed_hostapi_indexes
        and requested_text in str(device.get("name", "")).lower()
    ]
    if not matches:
        raise RuntimeError(
            f"no output audio device matching {requested_device!r} on host API {hostapi_preference!r}"
        )
    if len(matches) > 1:
        raise RuntimeError(
            f"audio device name {requested_device!r} matched multiple output devices: {matches}; use a device index"
        )
    return matches[0]


def _get_default_output_device_index(sounddevice) -> int | None:
    default_device = sounddevice.default.device
    if isinstance(default_device, (list, tuple)):
        if len(default_device) >= 2 and int(default_device[1]) >= 0:
            return int(default_device[1])
        return None
    try:
        device_index = int(default_device)
    except (TypeError, ValueError):
        return None
    return device_index if device_index >= 0 else None


def _get_audio_device_info(sounddevice, audio_device: int | str | None) -> dict[str, object] | None:
    device_index = audio_device if isinstance(audio_device, int) else _get_default_output_device_index(sounddevice)
    if device_index is None:
        return None
    return sounddevice.query_devices()[device_index]


def _get_audio_playback_sample_rate(sounddevice, audio_device: int | str | None) -> int:
    device_info = _get_audio_device_info(sounddevice, audio_device)
    if device_info is not None:
        try:
            sample_rate = int(round(float(device_info.get("default_samplerate", 0))))
        except (TypeError, ValueError):
            sample_rate = 0
        if sample_rate > 0:
            return sample_rate
    return FALLBACK_AUDIO_PLAYBACK_SAMPLE_RATE


def _get_audio_playback_channels(sounddevice, audio_device: int | str | None) -> int:
    device_info = _get_audio_device_info(sounddevice, audio_device)
    if device_info is None:
        return FALLBACK_AUDIO_PLAYBACK_CHANNELS

    try:
        max_channels = int(device_info.get("max_output_channels", 0))
    except (TypeError, ValueError):
        max_channels = 0
    if max_channels <= 0:
        return FALLBACK_AUDIO_PLAYBACK_CHANNELS

    if sys.platform == "win32":
        return max_channels
    return min(FALLBACK_AUDIO_PLAYBACK_CHANNELS, max_channels)


def _validate_audio_playback_config(audio_config: AudioPlaybackConfig) -> bool:
    if audio_config.sample_format != DEFAULT_AUDIO_SAMPLE_FORMAT:
        print(
            f"error: audio playback only supports {DEFAULT_AUDIO_SAMPLE_FORMAT}; server sent {audio_config.sample_format!r}",
            file=sys.stderr,
        )
        return False
    if audio_config.sample_rate != SERVER_AUDIO_SAMPLE_RATE:
        print(
            f"error: audio playback expects {SERVER_AUDIO_SAMPLE_RATE} Hz PCM from the server; "
            f"server sent {audio_config.sample_rate} Hz",
            file=sys.stderr,
        )
        return False
    if audio_config.channels != SERVER_AUDIO_CHANNELS:
        print(
            f"error: audio playback expects {SERVER_AUDIO_CHANNELS} channel PCM from the server; "
            f"server sent {audio_config.channels} channels",
            file=sys.stderr,
        )
        return False
    return True


class AudioPlaybackAdapter:
    def __init__(
        self,
        soxr,
        numpy,
        playback_sample_rate: int,
        playback_channels: int,
    ) -> None:
        self.soxr = soxr
        self.np = numpy
        self.playback_sample_rate = playback_sample_rate
        self.playback_channels = playback_channels
        self.bytes_per_frame = PCM_SAMPLE_WIDTH_BYTES * SERVER_AUDIO_CHANNELS
        self.remainder = b""
        self.stream = soxr.ResampleStream(
            SERVER_AUDIO_SAMPLE_RATE,
            playback_sample_rate,
            SERVER_AUDIO_CHANNELS,
            dtype="int16",
            quality="HQ",
        )

    def process(self, chunk: bytes) -> bytes:
        payload = self.remainder + chunk
        aligned_size = len(payload) - (len(payload) % self.bytes_per_frame)
        if aligned_size <= 0:
            self.remainder = payload
            return b""

        aligned = payload[:aligned_size]
        self.remainder = payload[aligned_size:]

        samples = self.np.frombuffer(aligned, dtype=self.np.int16)
        samples = samples.reshape(-1, SERVER_AUDIO_CHANNELS)

        resampled = self.stream.resample_chunk(samples, last=False)
        if SERVER_AUDIO_CHANNELS != self.playback_channels:
            resampled = self._fit_channels(resampled)
        return self.np.ascontiguousarray(resampled, dtype=self.np.int16).tobytes()

    def _fit_channels(self, samples):
        if samples.ndim == 1:
            samples = samples.reshape(-1, 1)
        source_channels = samples.shape[1]
        if source_channels == self.playback_channels:
            return samples

        if self.playback_channels == 1:
            return samples[:, :1]

        if source_channels == 1:
            output = self.np.zeros((samples.shape[0], self.playback_channels), dtype=samples.dtype)
            output[:, 0] = samples[:, 0]
            output[:, 1] = samples[:, 0]
            return output

        if source_channels >= self.playback_channels:
            return samples[:, :self.playback_channels]

        output = self.np.zeros((samples.shape[0], self.playback_channels), dtype=samples.dtype)
        output[:, :source_channels] = samples
        return output


def _stream_to_stdout(sock_file) -> int:
    while True:
        chunk = sock_file.read1(AUDIO_STREAM_READ_SIZE)
        if not chunk:
            break
        if _shutdown_requested:
            return EXIT_REQUEST_ERROR if _fatal_control_failure else SHUTDOWN_SIGNAL_EXIT
        _write_stdout_unbuffered(chunk)
    if _shutdown_requested:
        return EXIT_REQUEST_ERROR if _fatal_control_failure else SHUTDOWN_SIGNAL_EXIT
    return 0


def _stream_audio_to_soundcard(
    stream_sock: socket.socket,
    playback_state: AudioPlaybackState,
    prebuffer_seconds: float,
    latency_seconds: float,
    requested_device: int | str | None,
    hostapi_preference: str,
) -> int:
    sounddevice = _load_sounddevice_module()
    if sounddevice is None:
        return EXIT_REQUEST_ERROR
    soxr, numpy = _load_audio_resampler_modules()
    if soxr is None or numpy is None:
        return EXIT_REQUEST_ERROR
    try:
        audio_device = _resolve_audio_output_device(
            sounddevice,
            requested_device,
            hostapi_preference,
        )
    except Exception as exc:
        print(f"error: audio device selection failed: {exc}.{_audio_platform_hint()}", file=sys.stderr)
        return EXIT_REQUEST_ERROR

    try:
        playback_sample_rate = _get_audio_playback_sample_rate(sounddevice, audio_device)
        playback_channels = _get_audio_playback_channels(sounddevice, audio_device)
        audio_config = playback_state.active_config()
        if not _validate_audio_playback_config(audio_config):
            return EXIT_REQUEST_ERROR

        source_bytes_per_frame = PCM_SAMPLE_WIDTH_BYTES * SERVER_AUDIO_CHANNELS
        source_bytes_per_second = SERVER_AUDIO_SAMPLE_RATE * source_bytes_per_frame
        prebuffer_target = max(
            source_bytes_per_frame,
            int(source_bytes_per_second * prebuffer_seconds),
        )
        prebuffer_target -= prebuffer_target % source_bytes_per_frame
        if prebuffer_target <= 0:
            prebuffer_target = source_bytes_per_frame

        prebuffer = bytearray()
        while len(prebuffer) < prebuffer_target:
            pending_config = playback_state.take_pending_for(audio_config)
            if pending_config is not None:
                audio_config = pending_config
                if not _validate_audio_playback_config(audio_config):
                    return EXIT_REQUEST_ERROR
                prebuffer.clear()
                if _drain_audio_stream_after_reconfigure(stream_sock):
                    return 0
                continue
            chunk = stream_sock.recv(AUDIO_STREAM_READ_SIZE)
            if not chunk:
                break
            if _shutdown_requested:
                return EXIT_REQUEST_ERROR if _fatal_control_failure else SHUTDOWN_SIGNAL_EXIT
            prebuffer.extend(chunk)

        if not prebuffer:
            if _shutdown_requested:
                return EXIT_REQUEST_ERROR if _fatal_control_failure else SHUTDOWN_SIGNAL_EXIT
            return 0

        adapter = AudioPlaybackAdapter(
            soxr,
            numpy,
            playback_sample_rate,
            playback_channels,
        )
        initial_audio = adapter.process(bytes(prebuffer))

        with sounddevice.RawOutputStream(
            samplerate=playback_sample_rate,
            channels=playback_channels,
            dtype="int16",
            latency=latency_seconds,
            device=audio_device,
        ) as stream:
            if initial_audio:
                stream.write(initial_audio)

            while not _shutdown_requested:
                pending_config = playback_state.take_pending_for(audio_config)
                if pending_config is not None:
                    audio_config = pending_config
                    if not _validate_audio_playback_config(audio_config):
                        return EXIT_REQUEST_ERROR
                    if _drain_audio_stream_after_reconfigure(stream_sock):
                        break
                    continue

                chunk = stream_sock.recv(AUDIO_STREAM_READ_SIZE)
                if not chunk:
                    break
                if _shutdown_requested:
                    return EXIT_REQUEST_ERROR if _fatal_control_failure else SHUTDOWN_SIGNAL_EXIT
                output = adapter.process(chunk)
                if output:
                    stream.write(output)
    except Exception as exc:
        error_type = type(exc).__name__
        print(
            f"error: audio playback failed ({error_type}): {exc}.{_audio_platform_hint()}",
            file=sys.stderr,
        )
        return EXIT_REQUEST_ERROR
    if _shutdown_requested:
        return EXIT_REQUEST_ERROR if _fatal_control_failure else SHUTDOWN_SIGNAL_EXIT
    return 0


def _drain_audio_stream_after_reconfigure(stream_sock: socket.socket) -> bool:
    previous_timeout = stream_sock.gettimeout()
    drained = 0
    try:
        stream_sock.setblocking(False)
        while drained < MAX_AUDIO_RECONFIGURE_DRAIN_BYTES:
            try:
                chunk = stream_sock.recv(AUDIO_STREAM_READ_SIZE)
            except (BlockingIOError, InterruptedError):
                break
            except socket.timeout:
                break
            if chunk is None:
                break
            if not chunk:
                return True
            drained += len(chunk)
    finally:
        stream_sock.settimeout(previous_timeout)
    return False


def _print_interactive_prompt() -> None:
    if not sys.stdin.isatty() or _shutdown_requested:
        return
    try:
        print(INTERACTIVE_PROMPT, end="", file=sys.stderr, flush=True)
    except OSError:
        pass


def _start_interactive_control(
    control_channel: ControlChannel,
    args: argparse.Namespace,
    playback_state: AudioPlaybackState | None,
) -> threading.Thread | None:
    if not sys.stdin.isatty():
        return None

    def _interactive_loop() -> None:
        if args.mode == "audio":
            print(
                "interactive control enabled; use 'frequency <value>', 'demod <mode>', 'squelch <0-100>', and 'rds start|stop'",
                file=sys.stderr,
            )
        else:
            print(
                "interactive control enabled; use 'frequency <value>'",
                file=sys.stderr,
            )
        while not _shutdown_requested:
            _print_interactive_prompt()
            line = sys.stdin.readline()
            if line == "":
                break
            raw_text = line.strip()
            if not raw_text:
                continue
            commands = [segment.strip() for segment in raw_text.split(";") if segment.strip()]
            for text in commands:
                command, _, remainder = text.partition(" ")
                if command.lower() == "frequency":
                    if not remainder.strip():
                        print("error: frequency command requires a value", file=sys.stderr)
                        continue
                    try:
                        payload = {
                            "command": "retune",
                            "frequency": parse_frequency(remainder.strip()),
                        }
                    except argparse.ArgumentTypeError as exc:
                        print(f"error: {exc}", file=sys.stderr)
                        continue
                elif command.lower() == "demod":
                    if args.mode != "audio":
                        print("error: demod command is only supported in audio mode", file=sys.stderr)
                        continue
                    if not remainder.strip():
                        print("error: demod command requires a modulation", file=sys.stderr)
                        continue
                    try:
                        payload = {
                            "command": "demod",
                            "modulation": parse_control_modulation(remainder.strip()),
                        }
                    except argparse.ArgumentTypeError as exc:
                        print(f"error: {exc}", file=sys.stderr)
                        continue
                elif command.lower() == "rds":
                    if args.mode != "audio":
                        print("error: rds command is only supported in audio mode", file=sys.stderr)
                        continue
                    action = remainder.strip().lower()
                    if action not in {"start", "stop"}:
                        print("error: rds command requires 'start' or 'stop'", file=sys.stderr)
                        continue
                    payload = {
                        "command": "rds",
                        "action": action,
                    }
                elif command.lower() == "squelch":
                    if args.mode != "audio":
                        print("error: squelch command is only supported in audio mode", file=sys.stderr)
                        continue
                    if not remainder.strip():
                        print("error: squelch command requires a level from 0 to 100", file=sys.stderr)
                        continue
                    try:
                        payload = {
                            "command": "squelch",
                            "level": parse_squelch_level(remainder.strip()),
                        }
                    except argparse.ArgumentTypeError as exc:
                        print(f"error: {exc}", file=sys.stderr)
                        continue
                else:
                    if args.mode == "audio":
                        print(
                            "error: unsupported interactive command; use 'frequency <value>', 'demod <mode>', 'squelch <0-100>', or 'rds start|stop'",
                            file=sys.stderr,
                        )
                    else:
                        print(
                            "error: unsupported interactive command; use 'frequency <value>'",
                            file=sys.stderr,
                        )
                    continue

                try:
                    response = control_channel.send_command(payload)
                    if response is None:
                        print("error: control socket closed by server", file=sys.stderr)
                        return
                    if response.get("status") != "ok":
                        print(
                            f"error: {response.get('error', 'control command rejected by server')}",
                            file=sys.stderr,
                        )
                        continue
                    if playback_state is not None and response.get("mode") == "audio":
                        playback_state.request_reconfigure(
                            AudioPlaybackConfig(
                                sample_rate=int(response.get("sample_rate", 0)),
                                channels=int(response.get("channels", DEFAULT_AUDIO_CHANNELS)),
                                sample_format=str(response.get("format", "")).lower(),
                            )
                        )
                    if response.get("rds_active") is False:
                        control_channel.rds_display.clear()
                    if payload["command"] == "retune":
                        control_channel.rds_display.clear()
                        print(
                            f"retuned to {int(response.get('frequency', payload['frequency']))} Hz",
                            file=sys.stderr,
                        )
                    elif payload["command"] == "demod":
                        print(
                            f"switched demod to {str(response.get('modulation', payload['modulation'])).replace('_', '-')}",
                            file=sys.stderr,
                        )
                    elif payload["command"] == "rds":
                        print(
                            f"RDS {'enabled' if response.get('rds_active') else 'disabled'}",
                            file=sys.stderr,
                        )
                    else:
                        print(
                            f"squelch set to {int(response.get('squelch', payload['level']))}",
                            file=sys.stderr,
                        )
                except OSError as exc:
                    if not _shutdown_requested:
                        print(f"error: control command failed: {exc}", file=sys.stderr)
                    return

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
    if args.mode == "iq" or args.stdout:
        _set_stdout_binary_mode()
    if args.port >= 65535:
        print("error: stream port must be between 1 and 65534 so the control socket can use port+1", file=sys.stderr)
        return EXIT_CONNECT_FAILED
    if args.mode != "audio" and args.squelch:
        print("error: squelch is only supported in audio mode", file=sys.stderr)
        return EXIT_REQUEST_ERROR
    if args.mode != "audio" and (args.audio_device is not None or args.audio_hostapi != "auto"):
        print("error: audio device options are only supported in audio mode", file=sys.stderr)
        return EXIT_REQUEST_ERROR
    audio_latency = args.audio_latency if args.audio_latency is not None else _default_audio_latency()
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
        request["squelch"] = args.squelch

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
            control_sock.settimeout(None)

            playback_state: AudioPlaybackState | None = None
            if _should_play_audio(args):
                playback_state = AudioPlaybackState(
                    AudioPlaybackConfig(
                        sample_rate=int(handshake.get("sample_rate", 0)),
                        channels=int(handshake.get("channels", DEFAULT_AUDIO_CHANNELS)),
                        sample_format=str(handshake.get("format", "")).lower(),
                    )
                )

            rds_output = sys.stdout if _should_play_audio(args) else sys.stderr
            control_channel = ControlChannel(
                control_sock,
                control_file,
                playback_state,
                rds_output,
                prompt_callback=_print_interactive_prompt if sys.stdin.isatty() else None,
            )
            if args.rds:
                response = control_channel.send_command(
                    {
                        "command": "rds",
                        "action": "start",
                    }
                )
                if response is None:
                    print("error: control socket closed by server", file=sys.stderr)
                    return EXIT_REQUEST_ERROR
                if response.get("status") != "ok":
                    message = response.get("error", "server rejected RDS subscription")
                    print(f"error: {message}", file=sys.stderr)
                    return int(response.get("code", EXIT_REQUEST_ERROR))
                print("RDS enabled", file=sys.stderr)
            _start_interactive_control(control_channel, args, playback_state)

            stream_sock.settimeout(None)
            if _should_play_audio(args):
                return _stream_audio_to_soundcard(
                    stream_sock,
                    playback_state,
                    args.audio_prebuffer,
                    audio_latency,
                    args.audio_device,
                    args.audio_hostapi,
                )
            sock_file = stream_sock.makefile("rb")
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
