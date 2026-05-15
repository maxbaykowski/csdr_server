#!/usr/bin/env python3
"""
Minimal client for csdr_server.py.

The client sends one JSON request to the server, then writes the returned raw
complex float32 IQ stream to stdout.
"""

from __future__ import annotations

import argparse
import json
import re
import signal
import socket
import sys
from decimal import Decimal, InvalidOperation

EXIT_CONNECT_FAILED = 255
EXIT_OUT_OF_BAND = 1
EXIT_BAD_SAMPLE_RATE = 2
EXIT_REQUEST_ERROR = 3

SHUTDOWN_SIGNAL_EXIT = 0


SUFFIXES = {
    "": 1,
    "K": 1_000,
    "M": 1_000_000,
    "G": 1_000_000_000,
}

_shutdown_requested = False
_active_socket: socket.socket | None = None


def _request_shutdown(signum: int, _frame: object) -> None:
    global _shutdown_requested
    _shutdown_requested = True
    if _active_socket is not None:
        try:
            _active_socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


def _install_signal_handlers() -> None:
    signal.signal(signal.SIGINT, _request_shutdown)
    signal.signal(signal.SIGTERM, _request_shutdown)


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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Minimal client for csdr_server.py")
    parser.add_argument("-a", "--address", required=True, help="Server IP address or hostname")
    parser.add_argument("-p", "--port", required=True, type=int, help="Server TCP port")
    parser.add_argument("-f", "--frequency", required=True, type=parse_frequency, help="Tuned frequency in Hz, or with K/M/G suffix")
    parser.add_argument("-s", "--sample-rate", required=True, type=parse_sample_rate, help="Output sample rate in Sps, or with K/M/G suffix")
    return parser.parse_args()


def main() -> int:
    global _active_socket

    _install_signal_handlers()
    args = parse_args()
    request = {
        "frequency": args.frequency,
        "sample_rate": args.sample_rate,
    }

    try:
        sock = socket.create_connection((args.address, args.port), timeout=30.0)
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

    with sock:
        _active_socket = sock
        try:
            sock.sendall(json.dumps(request).encode("utf-8") + b"\n")
            sock_file = sock.makefile("rb")
            handshake_line = sock_file.readline(16_384)
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

            sock.settimeout(None)
            stdout = sys.stdout.buffer
            while True:
                chunk = sock_file.read1(65_536)
                if not chunk:
                    break
                if _shutdown_requested:
                    return SHUTDOWN_SIGNAL_EXIT
                stdout.write(chunk)
                stdout.flush()
            if _shutdown_requested:
                return SHUTDOWN_SIGNAL_EXIT
        except BrokenPipeError:
            return SHUTDOWN_SIGNAL_EXIT if _shutdown_requested else EXIT_REQUEST_ERROR
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

    return 0


if __name__ == "__main__":
    sys.exit(main())
