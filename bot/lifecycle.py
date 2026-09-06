"""Bounded resource cleanup, including partially opened companion connections."""

from __future__ import annotations

import asyncio


async def close_step(step, timeout: float, log=None) -> bool:
    task = asyncio.current_task()
    cancelling = task.cancelling()
    try:
        await asyncio.wait_for(step(), timeout)
        return True
    except (asyncio.CancelledError, Exception) as exc:
        if isinstance(exc, asyncio.CancelledError) and task.cancelling() > cancelling:
            raise  # Preserve cancellation of the owner, not a step that cancelled itself.
        if log is not None:
            log.emit("shutdown_error", step=getattr(step, "__name__", str(step)), error=f"{type(exc).__name__}: {exc}")
        return False


async def disconnect(meshcore, timeout: float = 3.0, log=None) -> None:
    """Try library cleanup, but always release the serial transport on failure.

    Some companion-library versions wait forever draining a stopped dispatcher.
    This adapter bounds that wait without modifying or vendoring the dependency.
    """
    closed = await close_step(meshcore.disconnect, timeout, log)
    manager = getattr(meshcore, "connection_manager", None)
    connection = getattr(manager, "connection", None)
    if not closed:
        dispatcher = getattr(meshcore, "dispatcher", None)
        tasks = set(getattr(dispatcher, "_background_tasks", ()))
        tasks.update(getattr(connection, "_background_tasks", ()))
        task = getattr(dispatcher, "_task", None)
        if task is not None:
            tasks.add(task)
        reconnect = getattr(manager, "_reconnect_task", None)
        if reconnect is not None:
            tasks.add(reconnect)
        tasks.discard(asyncio.current_task())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
    # A partial open may never set the manager's connected flag, so its normal
    # disconnect may leave an already-created transport behind.
    transport = getattr(connection, "transport", None)
    if transport is not None:
        try:
            transport.close()
        except Exception as exc:
            if log is not None:
                log.emit("shutdown_error", step="transport.close", error=str(exc))
