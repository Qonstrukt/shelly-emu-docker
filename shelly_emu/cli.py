"""Container / command line entry point."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys

from . import __version__
from .config import ConfigError, load_config
from .runner import Runner


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="shelly-emu", description="Shelly Gen2 emulator backed by Home Assistant")
    parser.add_argument("-c", "--config", default=os.environ.get("CONFIG", "/config/config.yaml"), help="YAML config file")
    parser.add_argument("--check", action="store_true", help="validate the configuration and exit")
    parser.add_argument("--version", action="version", version=f"shelly-emu {__version__}")
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
    except ConfigError as err:
        print(f"configuration error: {err}", file=sys.stderr)
        return 2
    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("zeroconf").setLevel(logging.WARNING)
    if args.check:
        for d in config.devices:
            print(f"{d.spec.model}: mac={d.spec.mac} name={d.spec.name!r} bind={d.bind} entities={len(d.entity_ids)}")
        print("configuration OK")
        return 0
    return asyncio.run(_run(config))


async def _run(config) -> int:
    runner = Runner(config)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, runner.stop)
        except NotImplementedError:
            pass
    await runner.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
