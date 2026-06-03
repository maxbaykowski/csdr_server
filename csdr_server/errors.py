from __future__ import annotations

import socket
from dataclasses import dataclass
from pathlib import Path

class RequestValidationError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class NetworkBindError(Exception):
    pass


class DeviceResolutionRetryableError(Exception):
    pass


class DeviceResolutionFatalError(Exception):
    pass


class DeviceBusyRetryableError(Exception):
    pass


class DeviceAccessFatalError(Exception):
    pass


@dataclass(frozen=True)
class RtlDeviceInfo:
    index: int
    description: str
    serial: str | None


@dataclass(frozen=True)
class UsbDeviceInfo:
    path: Path
    vendor_id: str
    product_id: str
    serial: str | None
    description: str


@dataclass
class PendingStreamConnection:
    conn: socket.socket
    address: tuple[str, int]
    created_at: float
