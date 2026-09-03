"""Command-line entry point: ``meshai --config config.toml [--headless]``."""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
from collections.abc import Sequence

from meshcore import MeshCore

from bot import __version__
from bot.backends import make_backend
from bot.config import Config, ConfigError, load_config
from bot.guard import InjectionGate
from bot.history import History
from bot.jsonlog import EventLog
from bot.ratelimit import RateLimiter
from bot.service import BotService, ChannelError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="meshai", description="MeshCore channel bot backed by a local LLM")
    parser.add_argument("--config", default="config.toml", help="path to the TOML config (default: config.toml)")
    parser.add_argument("--headless", action="store_true", help="no TUI; JSON log only")
    parser.add_argument("--log-file", default=None, help="override log_file from config")
    parser.add_argument("--version", action="version", version=f"meshai {__version__}")
    return parser


def build_service(cfg: Config, meshcore, log: EventLog) -> BotService:
    return BotService(
        cfg=cfg,
        meshcore=meshcore,
        backend=make_backend(cfg),
        gate=InjectionGate(threshold=cfg.vordur_threshold, sanitize=cfg.vordur_sanitize),
        limiter=RateLimiter(
            global_per_min=cfg.global_rate_per_min,
            global_burst=cfg.global_burst,
            sender_per_min=cfg.sender_rate_per_min,
            sender_burst=cfg.sender_burst,
        ),
        history=History(cfg.history_size),
        log=log,
    )


async def run(cfg: Config, headless: bool, log: EventLog) -> int:
    meshcore = await MeshCore.create_serial(cfg.port)
    if meshcore is None:
        print(f"error: no response from a MeshCore companion on {cfg.port}", file=sys.stderr)
        return 2
    service = build_service(cfg, meshcore, log)
    loop = asyncio.get_running_loop()

    if headless:
        stop = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        try:
            await service.start()
        except ChannelError as exc:
            print(f"error: {exc}", file=sys.stderr)
            await service.stop()
            return 3
        await stop.wait()
        await service.stop()
        return 0

    from bot.tui import MeshAIApp  # imported lazily so headless runs need no terminal features

    async def run_service() -> None:
        await service.start()
        await asyncio.Event().wait()  # until quit or cancellation

    app = MeshAIApp(
        cfg=cfg,
        stats=service.stats,
        limiter=service.limiter,
        subscribe_log=log.subscribe,
        run_service=run_service,
        stop_service=service.stop,
    )
    loop.add_signal_handler(signal.SIGTERM, lambda: asyncio.ensure_future(app.action_quit()))
    await app.run_async()
    await service.stop()
    if app.exit_error:
        print(f"error: {app.exit_error}", file=sys.stderr)
        return 3
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1
    log_path = args.log_file if args.log_file is not None else (cfg.log_file or None)
    log = EventLog(path=log_path)
    try:
        return asyncio.run(run(cfg, headless=args.headless, log=log))
    finally:
        log.close()


if __name__ == "__main__":
    sys.exit(main())
