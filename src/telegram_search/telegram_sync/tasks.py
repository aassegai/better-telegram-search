"""Finish a filesystem/SQLite commit before releasing its async ownership lock."""

import asyncio
from functools import partial


async def committed_thread(function, *args, **kwargs):
    """Return (result, cancelled); the caller publishes state before re-raising cancel."""
    write = asyncio.get_running_loop().run_in_executor(None, partial(function, *args, **kwargs))
    cancelled = False
    while True:
        try:
            return await asyncio.shield(write), cancelled
        except asyncio.CancelledError:
            # Cancelling the await does not cancel the actual filesystem/SQLite write.
            # Keep ownership until the thread commits, including repeated cancellation.
            cancelled = True
