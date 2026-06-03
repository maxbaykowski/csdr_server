from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .config import _check_dependencies, load_config
from .constants import LOGGER
from .errors import NetworkBindError
from .network import serve
from .utils import _set_process_name

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Minimal RTL-SDR + CSDR server")
    parser.add_argument(
        "-c",
        "--config",
        type=Path,
        default=Path("config.json5"),
        help="Path to JSON5 configuration file",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable detailed internal debug logging",
    )
    return parser.parse_args()


def main() -> int:
    _set_process_name("csdr_server")
    args = parse_args()
    log_level = "DEBUG" if args.debug else args.log_level
    logging.basicConfig(
        level=getattr(logging, log_level),
        format=(
            "%(asctime)s %(levelname)s %(name)s: %(message)s"
            if log_level == "DEBUG"
            else "%(message)s"
        ),
    )
    if not sys.platform.startswith("linux"):
        LOGGER.error("csdr_server is supported on Linux only")
        return 1
    try:
        config = load_config(args.config)
        _check_dependencies(config)
        return serve(args.config, config)
    except SystemExit:
        raise
    except FileNotFoundError as exc:
        if exc.filename and Path(exc.filename) == args.config:
            LOGGER.error("config file not found: %s", args.config)
        else:
            LOGGER.error("%s", exc)
        return 1
    except ValueError as exc:
        LOGGER.error("invalid JSON5 or configuration in %s: %s", args.config, exc)
        return 1
    except NetworkBindError as exc:
        LOGGER.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        return 0
    except Exception:
        LOGGER.exception("server failed")
        return 1
