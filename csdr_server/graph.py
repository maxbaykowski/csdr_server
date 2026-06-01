from __future__ import annotations

import os
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from .config import ServerConfig
from .constants import (
    DEFAULT_AUDIO_STREAM_WARMUP_TIMEOUT_SECONDS,
    DEFAULT_SAMPLE_FORMAT,
    DEFAULT_STREAM_OUTPUT_READ_SIZE,
    LOGGER,
    WFM_AUDIO_TRANSITION_BANDWIDTH,
    WFM_IQ_RATE,
)
from .dsp import (
    _build_audio_stream,
    _build_output_format_stream,
    _compute_fractional_decimation_ratio,
    _compute_integer_decimation,
    _format_csdr_float,
    _get_audio_iq_rate,
    _get_audio_transition_bandwidth,
    _get_decimation_strategy,
    _measure_complex_float_power_level,
    _validate_audio_modulation,
    _validate_audio_modulation_supported,
    _validate_mode,
    _validate_output_rate,
    _validate_request_frequency,
    _validate_sample_format,
    _get_required_bandwidth,
)
from .utils import _get_runtime_dir, terminate_process

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
        self.output_ready = threading.Event()

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
            terminate_process(self.process, self.command[0])
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
                self.output_ready.set()
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

    def wait_for_output(self, timeout: float) -> bool:
        return self.output_ready.wait(timeout)

    def _setup_control_fifo(self) -> None:
        if self.control_fifo_value is None:
            return
        runtime_dir = _get_runtime_dir()
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

class IqPowerMonitor:
    def __init__(
        self,
        config: ServerConfig,
        name: str,
        manager: "StreamGraph",
        parent: SharedStream,
    ) -> None:
        self.config = config
        self.name = name
        self.manager = manager
        self.parent = parent
        self.input_queue: queue.Queue[bytes | None] = queue.Queue(maxsize=4)
        self.closed = threading.Event()
        self.clients: set[ClientSession] = set()
        self.clients_lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.level = 0.0
        self.level_lock = threading.Lock()

    def start(self) -> None:
        self.thread = threading.Thread(
            target=self._monitor_loop,
            name=f"{self.name}-monitor",
            daemon=True,
        )
        self.thread.start()
        self.parent.add_subscriber(self)
        LOGGER.info("started IQ power monitor %s", self.name)

    def add_client(self, client: "ClientSession") -> None:
        with self.clients_lock:
            self.clients.add(client)

    def remove_client(self, client: "ClientSession") -> None:
        should_close = False
        with self.clients_lock:
            self.clients.discard(client)
            should_close = not self.clients and not self.closed.is_set()
        if should_close:
            self.close("unused IQ power monitor")

    def enqueue(self, chunk: bytes) -> bool:
        if self.closed.is_set():
            return False
        try:
            self.input_queue.put_nowait(chunk)
        except queue.Full:
            # Level metering must never backpressure the shared DSP graph.
            pass
        return True

    def close(self, reason: str) -> None:
        if self.closed.is_set():
            return
        LOGGER.info("closing IQ power monitor %s: %s", self.name, reason)
        self.closed.set()
        self.parent.remove_subscriber(self)
        self.manager.on_power_monitor_closed(self)
        try:
            self.input_queue.put_nowait(None)
        except queue.Full:
            pass

    def current_level(self) -> float:
        with self.level_lock:
            return self.level

    def _monitor_loop(self) -> None:
        while not self.closed.is_set():
            chunk = self.input_queue.get()
            if chunk is None:
                break
            level = _measure_complex_float_power_level(chunk)
            with self.level_lock:
                if self.level <= 0:
                    self.level = level
                else:
                    self.level = (self.level * 0.75) + (level * 0.25)


class StreamGraph:
    def __init__(self, config: ServerConfig) -> None:
        self.config = config
        self.lock = threading.Lock()
        self.root_stream: SharedStream | None = None
        self.root_dcblock_stream: SharedStream | None = None
        self.shift_streams: dict[int, SharedStream] = {}
        self.decimation_streams: dict[tuple[int, int, float, str], SharedStream] = {}
        self.format_streams: dict[tuple[int, int, str], Any] = {}
        self.audio_streams: dict[tuple[int, str], SharedStream] = {}
        self.rds_decoders: dict[int, RdsDecoder] = {}
        self.power_monitors: dict[tuple[int, int, float, str], IqPowerMonitor] = {}

    def stop(self, reason: str) -> None:
        with self.lock:
            root = self.root_stream
            root_dcblock = self.root_dcblock_stream
            audio = list(self.audio_streams.values())
            rds_decoders = list(self.rds_decoders.values())
            power_monitors = list(self.power_monitors.values())
            formats = list(self.format_streams.values())
            shifts = list(self.shift_streams.values())
            decimations = list(self.decimation_streams.values())
            self.root_stream = None
            self.root_dcblock_stream = None
            self.audio_streams = {}
            self.rds_decoders = {}
            self.power_monitors = {}
            self.format_streams = {}
            self.shift_streams = {}
            self.decimation_streams = {}
        for decoder in rds_decoders:
            decoder.close(reason)
        for monitor in power_monitors:
            monitor.close(reason)
        for stream in audio:
            stream.close(reason, propagate=True)
        for stream in formats:
            stream.close(reason, propagate=True)
        for stream in decimations:
            stream.close(reason, propagate=True)
        for stream in shifts:
            stream.close(reason, propagate=True)
        if root_dcblock is not None:
            root_dcblock.close(reason, propagate=True)
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
            stream = self._get_output_stream_locked(
                frequency,
                mode,
                output_rate,
                sample_format,
                modulation,
            )
        if mode == "audio":
            self._wait_for_audio_stream_handoff(stream, frequency, modulation)
        return stream

    def apply_runtime_config(
        self,
        new_config: ServerConfig,
        sessions: list["ClientSession"],
        rebuild_shift_path: bool,
        rebuild_decimators: bool,
        rebuild_audio_modulations: set[str],
    ) -> None:
        old_shift_streams: list[SharedStream] = []
        old_decimation_streams: list[SharedStream] = []
        old_format_streams: list[Any] = []
        old_audio_streams: list[SharedStream] = []
        old_rds_decoders: list[RdsDecoder] = []
        old_power_monitors: list[IqPowerMonitor] = []
        old_root_dcblock_stream: SharedStream | None = None
        stream_switches: list[tuple[ClientSession, SharedStream, IqPowerMonitor | None]] = []
        with self.lock:
            self.config = new_config
            if self.root_stream is not None:
                self.root_stream.config = new_config
            if self.root_dcblock_stream is not None:
                self.root_dcblock_stream.config = new_config
            for frequency, stream in self.shift_streams.items():
                stream.config = new_config
                stream.send_control_value(
                    str((new_config.center_frequency - frequency) / new_config.rtl_sample_rate)
                )

            if rebuild_shift_path:
                old_shift_streams = list(self.shift_streams.values())
                old_decimation_streams = list(self.decimation_streams.values())
                old_format_streams = list(self.format_streams.values())
                old_audio_streams = list(self.audio_streams.values())
                old_rds_decoders = list(self.rds_decoders.values())
                old_power_monitors = list(self.power_monitors.values())
                old_root_dcblock_stream = self.root_dcblock_stream
                self.shift_streams = {}
                self.decimation_streams = {}
                self.format_streams = {}
                self.audio_streams = {}
                self.rds_decoders = {}
                self.power_monitors = {}
                self.root_dcblock_stream = None
            elif rebuild_decimators:
                old_decimation_streams = list(self.decimation_streams.values())
                old_format_streams = list(self.format_streams.values())
                old_audio_streams = list(self.audio_streams.values())
                old_rds_decoders = list(self.rds_decoders.values())
                old_power_monitors = list(self.power_monitors.values())
                self.decimation_streams = {}
                self.format_streams = {}
                self.audio_streams = {}
                self.rds_decoders = {}
                self.power_monitors = {}
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
                if {"wfm", "wfm_stereo"} & rebuild_audio_modulations:
                    old_rds_decoders = list(self.rds_decoders.values())
                    self.rds_decoders = {}

            for session in sessions:
                session.config = new_config
                desired_stream = self._get_output_stream_locked(
                    session.frequency,
                    session.mode,
                    session.output_rate,
                    session.sample_format,
                    session.modulation,
                )
                desired_monitor = self._get_audio_power_monitor_locked(
                    session.frequency,
                    session.modulation,
                ) if session.mode == "audio" else None
                stream_switches.append((session, desired_stream, desired_monitor))

        for session, desired_stream, _desired_monitor in stream_switches:
            if session.mode == "audio":
                self._wait_for_audio_stream_handoff(
                    desired_stream,
                    session.frequency,
                    session.modulation,
                )

        for session, desired_stream, desired_monitor in stream_switches:
            session.switch_power_monitor(desired_monitor)
            session.switch_source_stream(desired_stream)

        for decoder in old_rds_decoders:
            decoder.close("reconfigured rds decoder")
        for monitor in old_power_monitors:
            monitor.close("reconfigured power monitor")
        for stream in old_audio_streams:
            stream.close("reconfigured audio stream", propagate=False)
        for stream in old_format_streams:
            stream.close("reconfigured output format", propagate=False)
        for stream in old_decimation_streams:
            stream.close("reconfigured decimator", propagate=False)
        for stream in old_shift_streams:
            stream.close("reconfigured shift stream", propagate=False)
        if old_root_dcblock_stream is not None:
            old_root_dcblock_stream.close("reconfigured root dcblock", propagate=False)

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

        shift_parent = self._get_root_shift_parent_locked(root)
        shift_stream = self._get_shift_stream_locked(frequency, shift_parent)

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
            audio_parent = self._get_audio_demod_parent_locked(frequency, modulation, base_stream)
            if modulation == "wfm_stereo":
                audio_parent = self._get_wfm_shared_s16_mpx_locked(frequency, audio_parent)
            audio_stream = _build_audio_stream(
                config=self.config,
                frequency=frequency,
                modulation=modulation,
                manager=self,
                parent=audio_parent,
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

    def _wait_for_audio_stream_handoff(
        self,
        stream: SharedStream,
        frequency: int,
        modulation: str | None,
    ) -> None:
        if stream.wait_for_output(DEFAULT_AUDIO_STREAM_WARMUP_TIMEOUT_SECONDS):
            return
        LOGGER.debug(
            "audio stream frequency=%s modulation=%s did not produce data within %.3fs before handoff",
            frequency,
            modulation,
            DEFAULT_AUDIO_STREAM_WARMUP_TIMEOUT_SECONDS,
        )

    def _get_shift_stream_locked(
        self,
        frequency: int,
        root: SharedStream,
    ) -> SharedStream:
        shift_stream = self.shift_streams.get(frequency)
        if shift_stream is None:
            shift_rate = (self.config.center_frequency - frequency) / self.config.rtl_sample_rate
            shift_name = f"shift-{frequency}"
            shift_fifo_path = _get_runtime_dir() / f"csdr_server_{shift_name}_{os.getpid()}.fifo"
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
        return shift_stream

    def _get_root_shift_parent_locked(self, root: SharedStream) -> SharedStream:
        if not self.config.dc_block:
            return root
        root_dcblock = self.root_dcblock_stream
        if root_dcblock is None:
            root_dcblock = SharedStream(
                config=self.config,
                name="iq-root-dcblock",
                command=[
                    "csdr",
                    "dcblock",
                    str(self.config.rtl_sample_rate),
                    "--cutoff",
                    "15",
                    "--fade",
                    "0.5",
                ],
                manager=self,
                parent=root,
                close_when_unused=True,
            )
            root_dcblock.start()
            self.root_dcblock_stream = root_dcblock
            LOGGER.debug(
                "created shared root IQ dcblock stream rtl_sample_rate=%s",
                self.config.rtl_sample_rate,
            )
        else:
            root_dcblock.config = self.config
            LOGGER.debug(
                "reusing shared root IQ dcblock stream rtl_sample_rate=%s",
                self.config.rtl_sample_rate,
            )
        return root_dcblock

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

    def _get_wfm_shared_s16_mpx_locked(
        self,
        frequency: int,
        parent: SharedStream,
    ) -> SharedStream:
        pcm_key = (frequency, "wfm_shared_s16_mpx")
        pcm_stream = self.audio_streams.get(pcm_key)
        if pcm_stream is None:
            pcm_stream = SharedStream(
                config=self.config,
                name=f"audio-wfm-{frequency}-s16-mpx",
                command=["csdr", "convert", "-i", "float", "-o", "s16"],
                manager=self,
                parent=parent,
                close_when_unused=True,
            )
            pcm_stream.start()
            self.audio_streams[pcm_key] = pcm_stream
            LOGGER.debug("created shared WFM s16 MPX stream for frequency=%s", frequency)
        else:
            pcm_stream.config = self.config
            LOGGER.debug("reusing shared WFM s16 MPX stream for frequency=%s", frequency)
        return pcm_stream

    def get_rds_decoder(self, frequency: int) -> RdsDecoder:
        from .rds import RdsDecoder

        with self.lock:
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
            shift_parent = self._get_root_shift_parent_locked(root)
            shift_stream = self._get_shift_stream_locked(frequency, shift_parent)
            base_stream = self._get_decimation_stream_locked(
                frequency,
                WFM_IQ_RATE,
                WFM_AUDIO_TRANSITION_BANDWIDTH,
                shift_stream,
            )
            demod_stream = self._get_audio_demod_parent_locked(frequency, "wfm", base_stream)
            pcm_stream = self._get_wfm_shared_s16_mpx_locked(frequency, demod_stream)
            decoder = self.rds_decoders.get(frequency)
            if decoder is None:
                decoder = RdsDecoder(
                    config=self.config,
                    frequency=frequency,
                    manager=self,
                    parent=pcm_stream,
                )
                decoder.start()
                self.rds_decoders[frequency] = decoder
                LOGGER.debug("created shared RDS decoder for frequency=%s", frequency)
            else:
                decoder.config = self.config
                LOGGER.debug("reusing shared RDS decoder for frequency=%s", frequency)
            return decoder

    def get_audio_power_monitor(
        self,
        frequency: int,
        modulation: str | None,
    ) -> IqPowerMonitor | None:
        if modulation is None:
            return None
        with self.lock:
            return self._get_audio_power_monitor_locked(frequency, modulation)

    def _get_audio_power_monitor_locked(
        self,
        frequency: int,
        modulation: str | None,
    ) -> IqPowerMonitor | None:
        if modulation is None:
            return None
        _validate_audio_modulation(modulation)
        _validate_audio_modulation_supported(self.config, modulation)
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
        shift_parent = self._get_root_shift_parent_locked(root)
        shift_stream = self._get_shift_stream_locked(frequency, shift_parent)
        output_rate = _get_audio_iq_rate(modulation)
        transition_bandwidth = _get_audio_transition_bandwidth(modulation)
        base_stream = self._get_decimation_stream_locked(
            frequency,
            output_rate,
            transition_bandwidth,
            shift_stream,
        )
        strategy = _get_decimation_strategy(self.config.rtl_sample_rate, output_rate)
        key = (frequency, output_rate, transition_bandwidth, strategy)
        monitor = self.power_monitors.get(key)
        if monitor is None:
            monitor = IqPowerMonitor(
                config=self.config,
                name=f"power-{frequency}-{output_rate}-{transition_bandwidth}",
                manager=self,
                parent=base_stream,
            )
            monitor.start()
            self.power_monitors[key] = monitor
            LOGGER.debug(
                "created shared IQ power monitor frequency=%s output_rate=%s transition_bandwidth=%s",
                frequency,
                output_rate,
                transition_bandwidth,
            )
        else:
            monitor.config = self.config
            LOGGER.debug(
                "reusing shared IQ power monitor frequency=%s output_rate=%s transition_bandwidth=%s",
                frequency,
                output_rate,
                transition_bandwidth,
            )
        return monitor

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
            LOGGER.debug(
                "using identity IQ stream frequency=%s output_rate=%s",
                frequency,
                output_rate,
            )
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
            elif strategy == "fractional":
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
            elif strategy == "identity":
                LOGGER.debug(
                    "using identity IQ stream frequency=%s output_rate=%s",
                    frequency,
                    output_rate,
                )
                decimation_stream = parent
            else:
                raise ValueError(f"unsupported decimation strategy {strategy!r}")
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
            if self.root_dcblock_stream is stream:
                self.root_dcblock_stream = None
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

    def on_power_monitor_closed(self, monitor: IqPowerMonitor) -> None:
        with self.lock:
            for key, candidate in list(self.power_monitors.items()):
                if candidate is monitor:
                    del self.power_monitors[key]

    def on_rds_decoder_closed(self, decoder: RdsDecoder) -> None:
        with self.lock:
            for frequency, candidate in list(self.rds_decoders.items()):
                if candidate is decoder:
                    del self.rds_decoders[frequency]
