"""Command line entry point.

Runs in the foreground and stops on SIGINT or SIGTERM, which is what both a developer with
Ctrl-C and a container runtime expect. A shutdown signal has to be handled rather than
ignored: the agent uses it to drain what it can and close the database cleanly, and a process
that ignores SIGTERM is killed outright a few seconds later.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
from pathlib import Path
from threading import Event

from factoryfleet_agent import __version__, config as config_module
from factoryfleet_agent.agent import Agent

DEFAULT_CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"

logger = logging.getLogger("factoryfleet_agent")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="factoryfleet-agent",
        description="Samples one plant-floor machine and reports it to the cloud.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_DIR,
        help="configuration directory holding agent.toml and sensors/ (default: %(default)s)",
    )
    parser.add_argument(
        "--log-level",
        choices=sorted(config_module.LOG_LEVELS),
        help="override the level in agent.toml, for a one-off debugging run",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser.parse_args(argv)


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)-32s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
        stream=sys.stdout,
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    try:
        config = config_module.load(args.config)
    except config_module.ConfigError as error:
        # Before logging is configured, and a stack trace would bury the one line that
        # matters — which file and key are wrong.
        print(f"configuration error: {error}", file=sys.stderr)
        return 2

    configure_logging(args.log_level or config.log_level)

    stopping = Event()

    def request_stop(signal_number: int, _frame: object) -> None:
        logger.info("Received %s", signal.Signals(signal_number).name)
        stopping.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    agent = Agent(config, version=__version__)
    try:
        agent.start()
    except Exception as error:  # noqa: BLE001 — report the cause rather than a traceback wall
        logger.critical("Agent failed to start: %s", error, exc_info=error)
        return 1

    try:
        stopping.wait()
    finally:
        agent.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
