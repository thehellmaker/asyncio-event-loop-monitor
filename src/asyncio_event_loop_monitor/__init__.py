"""
asyncio-event-loop-monitor: Detect and monitor synchronous blocking calls in Python asyncio event loops.

This module provides tools to detect synchronous blocking calls that occur within an async event loop context.
When a sync function takes longer than a configurable threshold, it emits callbacks that can be used for
logging, metrics, or alerting.

Example usage:
    from asyncio_event_loop_monitor import EventLoopMonitor, event_loop_monitor_ctx

    # Using context manager (recommended)
    with event_loop_monitor_ctx(threshold_ms=10.0):
        await my_async_function()

    # Manual usage
    monitor = EventLoopMonitor(threshold_ms=10.0)
    monitor.activate()
    try:
        await my_async_function()
    finally:
        monitor.deactivate()
"""

from asyncio_event_loop_monitor.monitor import (
    BlockingCallInfo,
    EventLoopMonitor,
    event_loop_monitor_ctx,
)

__all__ = [
    "EventLoopMonitor",
    "BlockingCallInfo",
    "event_loop_monitor_ctx",
]

__version__ = "0.1.0"
