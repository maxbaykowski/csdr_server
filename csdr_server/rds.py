from __future__ import annotations

import json
import queue
import subprocess
import threading

from .config import ServerConfig
from .constants import LOGGER, WFM_IQ_RATE
from .dsp import _extract_rds_fields
from .graph import SharedStream
from .utils import terminate_process

class RdsDecoder:
    def __init__(
        self,
        config: ServerConfig,
        frequency: int,
        manager: "StreamGraph",
        parent: SharedStream,
    ) -> None:
        self.config = config
        self.frequency = frequency
        self.manager = manager
        self.parent = parent
        self.process: subprocess.Popen[bytes] | None = None
        self.input_queue: queue.Queue[bytes | None] = queue.Queue(
            maxsize=config.stream_queue_chunks
        )
        self.closed = threading.Event()
        self.subscribers: set[ClientSession] = set()
        self.subscribers_lock = threading.Lock()
        self.input_thread: threading.Thread | None = None
        self.output_thread: threading.Thread | None = None
        self.stderr_thread: threading.Thread | None = None
        self.snapshot: dict[str, Any] = {}

    @property
    def name(self) -> str:
        return f"rds-{self.frequency}"

    def start(self) -> None:
        self.process = subprocess.Popen(
            self._build_command(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            start_new_session=True,
        )
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
        self.parent.add_subscriber(self)
        LOGGER.debug("started shared RDS decoder %s", self.name)

    def _build_command(self) -> list[str]:
        command = [
            "redsea",
            "-i",
            "mpx",
            "-r",
            str(WFM_IQ_RATE),
        ]
        if self.config.wfm_region == "us":
            command.append("-u")
        command.extend(
            [
                "-t",
                "%c",
            ]
        )
        return command

    def add_subscriber(self, subscriber: "ClientSession") -> None:
        with self.subscribers_lock:
            self.subscribers.add(subscriber)
            snapshot = dict(self.snapshot)
        if snapshot:
            subscriber.send_control(
                {
                    "event": "rds",
                    "frequency": self.frequency,
                    "fields": snapshot,
                }
            )

    def remove_subscriber(self, subscriber: "ClientSession") -> None:
        should_close = False
        with self.subscribers_lock:
            self.subscribers.discard(subscriber)
            should_close = not self.subscribers and not self.closed.is_set()
        if should_close:
            self.close("unused shared RDS decoder")

    def enqueue(self, chunk: bytes) -> bool:
        if self.closed.is_set():
            return False
        try:
            self.input_queue.put(chunk, timeout=self.config.enqueue_timeout_seconds)
            return True
        except queue.Full:
            LOGGER.debug("%s fell behind upstream input; closing branch", self.name)
            self.close("stream backlog")
            return False

    def close(self, reason: str) -> None:
        if self.closed.is_set():
            return
        LOGGER.debug("closing shared RDS decoder %s: %s", self.name, reason)
        self.closed.set()
        self.parent.remove_subscriber(self)
        self.manager.on_rds_decoder_closed(self)
        try:
            self.input_queue.put_nowait(None)
        except queue.Full:
            pass
        if self.process is not None:
            terminate_process(self.process, "redsea")

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
            LOGGER.debug("%s stdin closed", self.name)
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
                line = self.process.stdout.readline()
                if not line:
                    break
                message = json.loads(line.decode("utf-8"))
                fields = _extract_rds_fields(message)
                changed: dict[str, Any] = {}
                with self.subscribers_lock:
                    for key, value in fields.items():
                        if self.snapshot.get(key) != value:
                            self.snapshot[key] = value
                            changed[key] = value
                    subscribers = list(self.subscribers)
                if not changed:
                    continue
                payload = {
                    "event": "rds",
                    "frequency": self.frequency,
                    "fields": changed,
                }
                for subscriber in subscribers:
                    subscriber.send_control(payload)
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
            LOGGER.debug(
                "%s: %s",
                self.name,
                line.decode("utf-8", errors="replace").rstrip(),
            )
