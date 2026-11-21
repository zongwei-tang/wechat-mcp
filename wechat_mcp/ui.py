"""Single-threaded executor for every GUI operation.

WeChat is one window driven by one virtual mouse/keyboard, so all UI work is
serialised through one worker thread. Async MCP tools await `ui(fn, ...)`.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import functools
import threading

_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="ui")
_local = threading.local()


async def ui(fn, *args, timeout: float = 120.0, **kwargs):
    loop = asyncio.get_running_loop()
    fut = loop.run_in_executor(_executor, functools.partial(fn, *args, **kwargs))
    return await asyncio.wait_for(fut, timeout)


def ui_sync(fn, *args, **kwargs):
    """Run fn on the UI thread from synchronous code (blocks)."""
    return _executor.submit(fn, *args, **kwargs).result()
