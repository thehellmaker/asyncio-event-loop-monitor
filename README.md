# asyncio-event-loop-monitor

[![PyPI version](https://badge.fury.io/py/asyncio-event-loop-monitor.svg)](https://badge.fury.io/py/asyncio-event-loop-monitor)
[![Python Versions](https://img.shields.io/pypi/pyversions/asyncio-event-loop-monitor.svg)](https://pypi.org/project/asyncio-event-loop-monitor/)
[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)

Detect and monitor synchronous blocking calls in Python asyncio event loops.

## The Problem

In asyncio applications, blocking synchronous calls can severely degrade performance by preventing the event loop from processing other tasks. Common culprits include:

- `time.sleep()` instead of `await asyncio.sleep()`
- Synchronous file I/O (`open()`, `read()`, `write()`)
- Blocking network calls
- CPU-intensive computations
- Database queries without async drivers

These blocking calls are often difficult to detect because:
1. The code runs without errors
2. Performance degradation may only appear under load
3. They can be hidden deep in third-party libraries

## The Solution

`asyncio-event-loop-monitor` uses Python's `sys.setprofile()` to monitor function calls and detect when synchronous code blocks the event loop for longer than a configurable threshold.

## Installation

```bash
pip install asyncio-event-loop-monitor
```

Or with uv:

```bash
uv add asyncio-event-loop-monitor
```

## Quick Start

### Using the Context Manager (Recommended)

```python
import asyncio
from asyncio_event_loop_monitor import event_loop_monitor_ctx

async def main():
    with event_loop_monitor_ctx(threshold_ms=10.0):
        await process_requests()

asyncio.run(main())
```

### Using the Monitor Directly

```python
import asyncio
from asyncio_event_loop_monitor import EventLoopMonitor

async def main():
    monitor = EventLoopMonitor(threshold_ms=10.0)
    monitor.activate()
    try:
        await process_requests()
    finally:
        monitor.deactivate()

asyncio.run(main())
```

### Custom Callback

```python
from asyncio_event_loop_monitor import event_loop_monitor_ctx, BlockingCallInfo

def my_callback(info: BlockingCallInfo) -> None:
    print(f"BLOCKING: {info.method_name} took {info.duration_ms:.2f}ms")
    # Send to your metrics system
    # statsd.distribution("blocking_call.duration", info.duration_ms, tags=[f"method:{info.method_name}"])

async def main():
    with event_loop_monitor_ctx(threshold_ms=10.0, on_blocking_call=my_callback):
        await process_requests()
```

## How It Works

### sys.setprofile - The Core Mechanism

Python's `sys.setprofile(callback)` registers a callback that gets invoked on every function call, return, and exception:

```
def callback(frame: FrameType, event: str, arg: Any) -> None
```

The monitor tracks:
- `"call"` events - Records start time
- `"return"` events - Calculates duration and emits callback if above threshold
- `"c_call"` / `"c_return"` - Same for C extension functions

### Why Coroutines Are Skipped

Coroutine frames (async def functions) are skipped because their execution time includes time spent yielded to the event loop, which is NOT blocking:

```python
async def my_coroutine():
    await asyncio.sleep(1)  # Yields for 1 second - NOT blocking!
```

The coroutine frame would show 1 second duration, but the event loop was free to run other tasks during that time.

### How Blocking Calls Inside Coroutines Are Captured

When a coroutine calls a synchronous function, that sync function's frame does NOT have the CO_COROUTINE flag, so it gets tracked:

```python
async def my_coroutine():       # ← CO_COROUTINE flag = SKIPPED
    time.sleep(0.1)             # ← Regular sync frame = TRACKED!
    json.dumps(big_data)        # ← Regular sync frame = TRACKED!
    await asyncio.sleep(1)      # ← CO_COROUTINE flag = SKIPPED
```

## Configuration Options

### EventLoopMonitor / event_loop_monitor_ctx

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `threshold_ms` | `float` | `50.0` | Minimum duration in milliseconds for a call to be considered blocking |
| `include_paths` | `list[str] \| None` | `None` | List of path substrings to include. If provided, only matching paths are monitored |
| `exclude_paths` | `list[str] \| None` | `[...]` | List of path substrings to exclude from monitoring |
| `on_blocking_call` | `Callable[[BlockingCallInfo], None] \| None` | `None` | Callback function invoked when blocking is detected |

### Default Exclude Paths

By default, the following paths are excluded to reduce noise:
- `asyncio_event_loop_monitor` (the monitor itself)
- `<frozen` (frozen modules)
- `importlib`
- `typing`
- `contextlib`
- `functools`
- `threading`
- `asyncio`
- `concurrent`

## BlockingCallInfo

The callback receives a `BlockingCallInfo` dataclass with:

| Field | Type | Description |
|-------|------|-------------|
| `method_name` | `str` | Fully qualified method name (e.g., `myapp.service.process_data`) |
| `duration_ms` | `float` | Duration of the blocking call in milliseconds |
| `is_c_call` | `bool` | Whether this was a C extension function call |

## Performance Considerations

**WARNING**: `sys.setprofile` adds significant overhead because it's called on EVERY Python function call and return. Typical impact:
- 10-30% slowdown on CPU-bound code
- Higher for code with many small function calls
- Memory overhead for tracking call stacks

### Recommended Use Cases

1. **Debugging sessions** - Enable temporarily to find blocking calls
2. **CI/testing** - Run in test suite to catch regressions
3. **Canary deployments** - Enable on a small percentage of production pods
4. **On-demand profiling** - Enable via feature flag or request header

### NOT Recommended For

- Always-on production monitoring on all pods
- High-throughput, latency-sensitive code paths

## Example: Integration with Metrics

```python
from asyncio_event_loop_monitor import event_loop_monitor_ctx, BlockingCallInfo
from datadog import statsd

def emit_metrics(info: BlockingCallInfo) -> None:
    statsd.distribution(
        "event_loop.blocking.duration_ms",
        info.duration_ms,
        tags=[f"method:{info.method_name}"]
    )

# In your request handler or main loop
async def handle_request(request):
    # Only enable for sampled requests
    should_profile = request.headers.get("X-Enable-Profiling") == "true"

    with event_loop_monitor_ctx(enabled=should_profile, on_blocking_call=emit_metrics):
        return await process_request(request)
```

## Example: Finding Blocking Calls in Tests

```python
import pytest
from asyncio_event_loop_monitor import event_loop_monitor_ctx, BlockingCallInfo

@pytest.fixture
def blocking_call_detector():
    blocking_calls = []

    def callback(info: BlockingCallInfo) -> None:
        blocking_calls.append(info)

    yield blocking_calls, callback

@pytest.mark.asyncio
async def test_no_blocking_calls(blocking_call_detector):
    blocking_calls, callback = blocking_call_detector

    with event_loop_monitor_ctx(threshold_ms=5.0, on_blocking_call=callback):
        await my_async_function()

    assert len(blocking_calls) == 0, f"Found blocking calls: {blocking_calls}"
```

## License

Apache License 2.0

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.
