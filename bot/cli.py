"""Command-line entry point: ``meshai --config config.toml [--headless]``."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from collections.abc import Sequence

from meshcore import EventType, MeshCore
from meshcore.serial_cx import SerialConnection

from bot import __version__
from bot.backends import make_backend
from bot.config import Config, ConfigError, load_config
from bot.guard import InjectionGate
from bot.history import History
from bot.jsonlog import EventLog
from bot.ratelimit import RateLimiter
from bot.service import BotService, ChannelError
from bot.utilization import UtilizationMonitor


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="meshai", description="MeshCore channel bot backed by a local LLM")
    parser.add_argument("--config", default="config.toml", help="path to the TOML config (default: config.toml)")
    parser.add_argument("--headless", action="store_true", help="no TUI; JSON log only")
    parser.add_argument("--log-file", default=None, help="override log_file from config")
    parser.add_argument(
        "--debug",
        action="store_true",
        help="also write meshcore's frame-level debug logging to <log file>.debug (or stderr when headless)",
    )
    parser.add_argument("--version", action="version", version=f"meshai {__version__}")
    return parser


def build_service(cfg: Config, meshcore, log: EventLog) -> BotService:
    limiter = RateLimiter(
        global_per_min=cfg.global_rate_per_min,
        global_burst=cfg.global_burst,
        sender_per_min=cfg.sender_rate_per_min,
        sender_burst=cfg.sender_burst,
    )
    monitor = None
    if cfg.adaptive_enabled:
        monitor = UtilizationMonitor(
            meshcore=meshcore,
            limiter=limiter,
            log=log,
            poll_s=cfg.utilization_poll_s,
            window_s=cfg.utilization_window_s,
            duty_low=cfg.duty_low,
            duty_high=cfg.duty_high,
        )
    return BotService(
        cfg=cfg,
        meshcore=meshcore,
        backend=make_backend(cfg),
        gate=InjectionGate(threshold=cfg.vordur_threshold, sanitize=cfg.vordur_sanitize),
        limiter=limiter,
        history=History(cfg.history_size),
        log=log,
        monitor=monitor,
    )


def release_boot_lines(meshcore) -> None:
    """Drop DTR and RTS after opening the port.

    pyserial asserts DTR on open and meshcore then clears RTS. On the ESP32
    auto-program circuit (CP2102 boards such as the Heltec Wireless Paper) that
    combination holds IO0 low for the whole session: the chip keeps running, but
    any reset while the port is open (brownout at full TX power, watchdog, crash)
    lands it in the serial bootloader instead of MeshCore, silently. With both
    lines released a reset boots MeshCore normally.
    """
    try:
        ser = meshcore.connection_manager.connection.transport.serial
        ser.dtr = False
        ser.rts = False
    except AttributeError:
        pass  # not a pyserial transport (tests, other connection types)


async def connect(cfg: Config, attempts: int = 3, boot_delay_s: float = 2.5):
    """Open the companion and complete the app-start handshake.

    Same steps as ``MeshCore.create_serial`` (dispatcher, port, app start), with two
    changes: the boot lines are released right after the port opens, and the
    handshake waits for a possible reboot and is retried without reopening the
    port, because reopening would toggle the lines again.
    """
    meshcore = MeshCore(SerialConnection(cfg.port, 115200))
    await meshcore.dispatcher.start()
    if await meshcore.connection_manager.connect() is None:
        await meshcore.disconnect()
        return None
    release_boot_lines(meshcore)
    await asyncio.sleep(boot_delay_s)
    for attempt in range(1, attempts + 1):
        res = await meshcore.commands.send_appstart()
        if res is not None and res.type != EventType.ERROR:
            return meshcore
        if attempt < attempts:
            print(f"no handshake from {cfg.port}, retrying ({attempt}/{attempts})...", file=sys.stderr)
    await meshcore.disconnect()
    return None


async def run(cfg: Config, headless: bool, log: EventLog) -> int:
    meshcore = await connect(cfg)
    if meshcore is None:
        print(
            f"error: no response from a MeshCore companion on {cfg.port} "
            "(if it was running before, unplug and replug it: it may be stuck in the bootloader)",
            file=sys.stderr,
        )
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
        monitor=service.monitor,
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
    if log_path is None and not args.headless:
        log_path = "meshai.jsonl"  # the TUI owns the terminal, so stderr is not a usable log target
        print(f"JSON log: {log_path}", file=sys.stderr)
    if args.debug:
        # meshcore calls logging.basicConfig at import, so configure handlers explicitly.
        root = logging.getLogger()
        root.setLevel(logging.DEBUG)
        handler: logging.Handler = (
            logging.FileHandler(f"{log_path}.debug", encoding="utf-8") if log_path else logging.StreamHandler(sys.stderr)
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
        for existing in list(root.handlers):
            root.removeHandler(existing)
        root.addHandler(handler)
    log = EventLog(path=log_path)
    try:
        return asyncio.run(run(cfg, headless=args.headless, log=log))
    finally:
        log.close()


if __name__ == "__main__":
    sys.exit(main())
