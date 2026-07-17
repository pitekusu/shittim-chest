"""Production process entry point."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import NoReturn

from shittim_chest.bootstrap import run_from_environment
from shittim_chest.config import StartupConfigurationError


def main() -> int:
    """Run the process with content-free terminal error reporting."""

    try:
        asyncio.run(run_from_environment())
    except StartupConfigurationError as error:
        _log_terminal_failure(error.code)
        return 2
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        _log_terminal_failure("runtime_failed", error_type=type(error).__name__)
        return 1
    return 0


def _log_terminal_failure(code: str, *, error_type: str | None = None) -> None:
    logging.basicConfig(level=logging.ERROR, format="%(message)s")
    payload = {"event": "application_start_failed", "code": code}
    if error_type is not None:
        payload["error_type"] = error_type
    logging.getLogger("shittim_chest").error(
        json.dumps(payload, sort_keys=True, separators=(",", ":"))
    )


def _entrypoint() -> NoReturn:
    raise SystemExit(main())


if __name__ == "__main__":
    _entrypoint()
