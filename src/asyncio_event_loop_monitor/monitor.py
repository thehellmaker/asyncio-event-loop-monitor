"""
Event Loop Monitor - Detects synchronous blocking calls within async event loop contexts.

This module uses Python's sys.setprofile to monitor function calls and detect when synchronous
code blocks the event loop for longer than a configurable threshold.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from types import FrameType
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_SLOW_THRESHOLD_MS = 50.0

_monitor_active: ContextVar[bool] = ContextVar("monitor_active", default=False)


@dataclass
class BlockingCallInfo:
    """Information about a detected blocking call."""

    method_name: str
    """Fully qualified method name (e.g., 'myapp.service.process_data')"""

    duration_ms: float
    """Duration of the blocking call in milliseconds"""

    is_c_call: bool = False
    """Whether this was a C extension function call"""


BlockingCallCallback = Callable[[BlockingCallInfo], None]
"""Type alias for blocking call callback functions."""


def _default_callback(info: BlockingCallInfo) -> None:
    """Default callback that logs blocking calls."""
    log.warning(
        "Event loop blocking detected: %s took %.2fms",
        info.method_name,
        info.duration_ms,
    )


def _is_inside_event_loop() -> bool:
    """Check if we're currently inside an asyncio event loop."""
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


def _is_coroutine_frame(frame: FrameType) -> bool:
    """Check if a frame represents a coroutine (async function)."""
    code = frame.f_code
    return bool(code.co_flags & (inspect.CO_COROUTINE | inspect.CO_ASYNC_GENERATOR | inspect.CO_ITERABLE_COROUTINE))


def _build_metric_name(frame: FrameType) -> str:
    """Build a metric name from a stack frame."""
    module = frame.f_globals.get("__name__", "unknown")
    func_name = frame.f_code.co_qualname
    return f"{module}.{func_name}"


def _build_c_metric_name(func: Any) -> str:
    """Build a metric name from a C function."""
    module = getattr(func, "__module__", None)
    name = getattr(func, "__name__", None)
    qualname = getattr(func, "__qualname__", None)

    if qualname and "." in qualname:
        func_name = qualname
    elif hasattr(func, "__self__"):
        self_obj = func.__self__
        if self_obj is not None and not isinstance(self_obj, type(sys)):
            self_type = type(self_obj).__name__
            func_name = f"{self_type}.{name}" if name else repr(func)
        else:
            func_name = name or repr(func)
    else:
        func_name = name or repr(func)

    if module:
        return f"{module}.{func_name}"
    return f"builtin.{func_name}"


class EventLoopMonitor:
    """
    Detects synchronous blocking calls that occur within an async event loop context.

    WHAT IT DOES
    ------------
    Monitors Python function calls and identifies synchronous code that blocks the
    event loop. When a sync function takes longer than the threshold, it invokes
    a callback with the method name and duration.

    SYS.SETPROFILE - THE CORE MECHANISM
    -----------------------------------
    Python's sys.setprofile(callback) registers a callback that gets invoked on
    every function call, return, and exception. The callback signature is:

        def callback(frame: FrameType, event: str, arg: Any) -> None

    Parameters:
        frame   - The current stack frame object containing:
                  - f_code.co_filename: Source file path
                  - f_code.co_qualname: Qualified function name (e.g., "MyClass.method")
                  - f_code.co_flags: Flags including CO_COROUTINE for async functions
                  - f_globals["__name__"]: Module name
        event   - One of: "call", "return", "c_call", "c_return", "c_exception"
        arg     - For "return": the return value; for exceptions: exception info

    Events we care about:
        "call"   - Function is being called. We record start time.
        "return" - Function is returning. We calculate duration.
        "c_call" - C extension function is being called.
        "c_return" - C extension function is returning.

    WHY COROUTINES ARE SKIPPED
    --------------------------
    Coroutine frames (async def functions) are skipped because their execution time
    includes time spent yielded to the event loop, which is NOT blocking:

        async def my_coroutine():
            await asyncio.sleep(1)  # Yields for 1 second - NOT blocking!

    The coroutine frame would show 1 second duration, but the event loop was free
    to run other tasks during that time.

    HOW BLOCKING CALLS INSIDE COROUTINES ARE CAPTURED
    -------------------------------------------------
    When a coroutine calls a synchronous function, that sync function's frame
    does NOT have the CO_COROUTINE flag, so it gets tracked:

        async def my_coroutine():       # ← CO_COROUTINE flag = SKIPPED
            time.sleep(0.1)             # ← Regular sync frame = TRACKED!
            json.dumps(big_data)        # ← Regular sync frame = TRACKED!
            await asyncio.sleep(1)      # ← CO_COROUTINE flag = SKIPPED

    PERFORMANCE OVERHEAD
    --------------------
    WARNING: sys.setprofile adds significant overhead because it's called on
    EVERY Python function call and return. Typical impact:
        - 10-30% slowdown on CPU-bound code
        - Higher for code with many small function calls
        - Memory overhead for _call_stack entries

    Not recommended for always-on production use. Better for:
        - Debugging/profiling sessions
        - CI/testing to catch regressions
        - Specific canary pods only
        - On-demand via feature flag or request header

    USAGE
    -----
    Basic usage:
        monitor = EventLoopMonitor(threshold_ms=10.0)
        monitor.activate()
        # ... async code runs, blocking calls are detected ...
        monitor.deactivate()

    With custom callback:
        def my_callback(info: BlockingCallInfo) -> None:
            print(f"{info.method_name} blocked for {info.duration_ms}ms")

        monitor = EventLoopMonitor(threshold_ms=10.0, on_blocking_call=my_callback)
        monitor.activate()
        try:
            await process_request()
        finally:
            monitor.deactivate()

    Context manager:
        with event_loop_monitor_ctx(threshold_ms=10.0):
            await my_async_function()
    """

    def __init__(
        self,
        threshold_ms: float = DEFAULT_SLOW_THRESHOLD_MS,
        include_paths: list[str] | None = None,
        exclude_paths: list[str] | None = None,
        on_blocking_call: BlockingCallCallback | None = None,
    ) -> None:
        """
        Initialize the event loop monitor.

        Args:
            threshold_ms: Minimum duration in milliseconds for a call to be considered blocking.
                         Defaults to 50.0ms.
            include_paths: List of path substrings to include. If provided, only paths containing
                          one of these substrings will be monitored. If None, all paths are included.
            exclude_paths: List of path substrings to exclude. Paths containing any of these
                          substrings will not be monitored. Defaults to common library paths.
            on_blocking_call: Callback function invoked when a blocking call is detected.
                             If None, uses default logging callback.
        """
        self._threshold_ms = threshold_ms
        self._include_paths = include_paths if include_paths is not None else []
        if exclude_paths is not None:
            self._exclude_paths = exclude_paths
        else:
            self._exclude_paths = [
                "asyncio_event_loop_monitor",
                "<frozen",
                "importlib",
                "typing",
                "contextlib",
                "functools",
                "threading",
                "asyncio",
                "concurrent",
            ]
        self._on_blocking_call = on_blocking_call or _default_callback
        self._call_stack: dict[int, tuple[str, float]] = {}
        self._c_call_stack: dict[int, tuple[str, float]] = {}
        self._original_profile: Any = None
        self._active = False

    def _should_track_path(self, filename: str) -> bool:
        """Check if a file path should be tracked based on include/exclude rules."""
        for exclude in self._exclude_paths:
            if exclude in filename:
                return False

        if not self._include_paths:
            return True

        for include in self._include_paths:
            if include in filename:
                return True
        return False

    def _should_track_frame(self, frame: FrameType) -> bool:
        """Check if a frame should be tracked."""
        if _monitor_active.get():
            return False
        if not _is_inside_event_loop():
            return False
        if _is_coroutine_frame(frame):
            return False
        if not self._should_track_path(frame.f_code.co_filename):
            return False
        return True

    def _should_track_c_call(self) -> bool:
        """Check if a C call should be tracked."""
        if _monitor_active.get():
            return False
        if not _is_inside_event_loop():
            return False
        return True

    def _handle_call_event(self, frame: FrameType) -> None:
        """Handle a function call event."""
        if not self._should_track_frame(frame):
            return
        metric_name = _build_metric_name(frame)
        self._call_stack[id(frame)] = (metric_name, time.perf_counter())

    def _handle_return_event(self, frame: FrameType) -> None:
        """Handle a function return event."""
        call_data = self._call_stack.pop(id(frame), None)
        if call_data is None:
            return
        if _monitor_active.get():
            return
        metric_name, start_time = call_data
        duration_ms = (time.perf_counter() - start_time) * 1000
        if duration_ms >= self._threshold_ms:
            self._emit_callback_safely(metric_name, duration_ms, is_c_call=False)

    def _handle_c_call_event(self, func: Any) -> None:
        """Handle a C function call event."""
        if not self._should_track_c_call():
            return
        metric_name = _build_c_metric_name(func)
        self._c_call_stack[id(func)] = (metric_name, time.perf_counter())

    def _handle_c_return_event(self, func: Any) -> None:
        """Handle a C function return event."""
        call_data = self._c_call_stack.pop(id(func), None)
        if call_data is None:
            return
        if _monitor_active.get():
            return
        metric_name, start_time = call_data
        duration_ms = (time.perf_counter() - start_time) * 1000
        if duration_ms >= self._threshold_ms:
            self._emit_callback_safely(metric_name, duration_ms, is_c_call=True)

    def _emit_callback_safely(self, metric_name: str, duration_ms: float, is_c_call: bool) -> None:
        """Emit the callback, preventing recursion."""
        token = _monitor_active.set(True)
        try:
            info = BlockingCallInfo(
                method_name=metric_name,
                duration_ms=duration_ms,
                is_c_call=is_c_call,
            )
            self._on_blocking_call(info)
        finally:
            _monitor_active.reset(token)

    def _profile_handler(self, frame: FrameType, event: str, arg: Any) -> None:
        """Profile handler callback registered with sys.setprofile."""
        if event == "return":
            self._handle_return_event(frame)
        elif event == "call":
            self._handle_call_event(frame)
        elif event == "c_return":
            self._handle_c_return_event(arg)
        elif event == "c_call":
            self._handle_c_call_event(arg)

    def activate(self) -> None:
        """Activate the monitor by installing the profile handler."""
        if self._active:
            return
        self._original_profile = sys.getprofile()
        sys.setprofile(self._profile_handler)
        self._active = True

    def deactivate(self) -> None:
        """Deactivate the monitor by restoring the original profile handler."""
        if not self._active:
            return
        sys.setprofile(self._original_profile)
        self._call_stack.clear()
        self._c_call_stack.clear()
        self._active = False

    @property
    def is_active(self) -> bool:
        """Check if the monitor is currently active."""
        return self._active


class _RefCountedMonitor:
    """
    Reference-counted monitor for shared usage across multiple contexts.

    This class ensures that when multiple context managers are nested or used
    concurrently, the monitor is only activated once and deactivated when the
    last context exits.
    """

    _lock = threading.Lock()
    _ref_count = 0
    _monitor: EventLoopMonitor | None = None
    _threshold_ms: float = DEFAULT_SLOW_THRESHOLD_MS
    _include_paths: list[str] | None = None
    _exclude_paths: list[str] | None = None
    _on_blocking_call: BlockingCallCallback | None = None

    @classmethod
    def configure(
        cls,
        threshold_ms: float = DEFAULT_SLOW_THRESHOLD_MS,
        include_paths: list[str] | None = None,
        exclude_paths: list[str] | None = None,
        on_blocking_call: BlockingCallCallback | None = None,
    ) -> None:
        """Configure the shared monitor settings."""
        with cls._lock:
            cls._threshold_ms = threshold_ms
            cls._include_paths = include_paths
            cls._exclude_paths = exclude_paths
            cls._on_blocking_call = on_blocking_call

    @classmethod
    def acquire(cls) -> None:
        """Acquire a reference to the monitor, activating it if this is the first reference."""
        with cls._lock:
            cls._ref_count += 1
            if cls._ref_count == 1:
                cls._monitor = EventLoopMonitor(
                    threshold_ms=cls._threshold_ms,
                    include_paths=cls._include_paths,
                    exclude_paths=cls._exclude_paths,
                    on_blocking_call=cls._on_blocking_call,
                )
                cls._monitor.activate()
                log.debug("Event loop monitor activated (ref_count=%d)", cls._ref_count)
            else:
                log.debug("Event loop monitor ref_count increased to %d", cls._ref_count)

    @classmethod
    def release(cls) -> None:
        """Release a reference to the monitor, deactivating it if this is the last reference."""
        with cls._lock:
            if cls._ref_count <= 0:
                return

            cls._ref_count -= 1
            if cls._ref_count == 0 and cls._monitor:
                cls._monitor.deactivate()
                cls._monitor = None
                log.debug("Event loop monitor deactivated (ref_count=0)")
            else:
                log.debug("Event loop monitor ref_count decreased to %d", cls._ref_count)


@contextmanager
def event_loop_monitor_ctx(
    enabled: bool = True,
    threshold_ms: float = DEFAULT_SLOW_THRESHOLD_MS,
    include_paths: list[str] | None = None,
    exclude_paths: list[str] | None = None,
    on_blocking_call: BlockingCallCallback | None = None,
) -> Iterator[None]:
    """
    Context manager for monitoring blocking calls in the event loop.

    This context manager uses reference counting to support nested usage.
    The monitor is activated when the first context is entered and deactivated
    when the last context exits.

    Args:
        enabled: Whether to enable monitoring. If False, the context manager is a no-op.
        threshold_ms: Minimum duration in milliseconds for a call to be considered blocking.
        include_paths: List of path substrings to include in monitoring.
        exclude_paths: List of path substrings to exclude from monitoring.
        on_blocking_call: Callback function invoked when a blocking call is detected.

    Example:
        async def main():
            with event_loop_monitor_ctx(threshold_ms=10.0):
                await process_request()

        # With custom callback
        def my_callback(info: BlockingCallInfo) -> None:
            metrics.record("blocking_call", info.duration_ms, tags={"method": info.method_name})

        with event_loop_monitor_ctx(on_blocking_call=my_callback):
            await process_request()
    """
    if not enabled:
        yield
        return

    _RefCountedMonitor.configure(
        threshold_ms=threshold_ms,
        include_paths=include_paths,
        exclude_paths=exclude_paths,
        on_blocking_call=on_blocking_call,
    )
    _RefCountedMonitor.acquire()
    try:
        yield
    finally:
        _RefCountedMonitor.release()
