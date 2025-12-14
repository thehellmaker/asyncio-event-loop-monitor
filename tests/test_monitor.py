"""Tests for the event loop monitor."""

from __future__ import annotations

import asyncio
import sys
import threading
import time

import pytest

from asyncio_event_loop_monitor import BlockingCallInfo, EventLoopMonitor, event_loop_monitor_ctx


class TestEventLoopMonitor:
    def test_init_with_default_threshold(self) -> None:
        monitor = EventLoopMonitor()
        assert monitor._threshold_ms == 50.0

    def test_init_with_custom_threshold(self) -> None:
        monitor = EventLoopMonitor(threshold_ms=25.0)
        assert monitor._threshold_ms == 25.0

    def test_init_with_include_paths(self) -> None:
        monitor = EventLoopMonitor(include_paths=["my_module"])
        assert "my_module" in monitor._include_paths

    def test_init_with_exclude_paths(self) -> None:
        monitor = EventLoopMonitor(exclude_paths=["skip_this"])
        assert "skip_this" in monitor._exclude_paths

    def test_activate_sets_profile(self) -> None:
        monitor = EventLoopMonitor()
        original = sys.getprofile()

        monitor.activate()
        assert sys.getprofile() is not None
        assert monitor.is_active is True

        monitor.deactivate()
        assert sys.getprofile() == original
        assert monitor.is_active is False

    def test_activate_is_idempotent(self) -> None:
        monitor = EventLoopMonitor()

        monitor.activate()
        profile_after_first = sys.getprofile()

        monitor.activate()
        profile_after_second = sys.getprofile()

        assert profile_after_first == profile_after_second
        monitor.deactivate()

    def test_deactivate_is_idempotent(self) -> None:
        monitor = EventLoopMonitor()
        monitor.deactivate()
        assert monitor.is_active is False

    def test_should_track_path_with_no_include_paths(self) -> None:
        monitor = EventLoopMonitor(exclude_paths=[])
        assert monitor._should_track_path("/path/to/my_module.py") is True

    def test_should_track_path_excludes_paths(self) -> None:
        monitor = EventLoopMonitor(exclude_paths=["skip_this"])
        assert monitor._should_track_path("/path/skip_this/module.py") is False
        assert monitor._should_track_path("/path/other/module.py") is True

    def test_should_track_path_with_include_paths(self) -> None:
        monitor = EventLoopMonitor(include_paths=["my_app"], exclude_paths=[])
        assert monitor._should_track_path("/path/my_app/module.py") is True
        assert monitor._should_track_path("/path/other/module.py") is False


class TestEventLoopMonitorCtx:
    def test_disabled_context_is_noop(self) -> None:
        with event_loop_monitor_ctx(enabled=False):
            pass

    @pytest.mark.asyncio
    async def test_context_captures_blocking_calls(self) -> None:
        captured: list[BlockingCallInfo] = []

        def callback(info: BlockingCallInfo) -> None:
            captured.append(info)

        with event_loop_monitor_ctx(
            threshold_ms=5.0,
            on_blocking_call=callback,
            include_paths=["test_monitor"],
            exclude_paths=[],
        ):
            time.sleep(0.02)

        assert len(captured) >= 1
        assert any(info.duration_ms >= 15 for info in captured)


class TestBlockingDetection:
    @pytest.mark.asyncio
    async def test_time_sleep_detected_in_async_context(self) -> None:
        captured: list[BlockingCallInfo] = []

        def callback(info: BlockingCallInfo) -> None:
            captured.append(info)

        with event_loop_monitor_ctx(
            threshold_ms=10.0,
            on_blocking_call=callback,
            exclude_paths=[],
        ):
            time.sleep(0.025)

        blocking_calls = [c for c in captured if c.duration_ms >= 20]
        assert len(blocking_calls) >= 1

    @pytest.mark.asyncio
    async def test_asyncio_sleep_not_detected(self) -> None:
        captured: list[BlockingCallInfo] = []

        def callback(info: BlockingCallInfo) -> None:
            captured.append(info)

        with event_loop_monitor_ctx(
            threshold_ms=0.0,
            on_blocking_call=callback,
            include_paths=["test_monitor"],
            exclude_paths=[],
        ):
            await asyncio.sleep(0.025)

        # asyncio.sleep should not be captured as blocking
        asyncio_calls = [c for c in captured if "asyncio" in c.method_name.lower()]
        assert len(asyncio_calls) == 0

    def test_no_detection_outside_event_loop(self) -> None:
        captured: list[BlockingCallInfo] = []

        def callback(info: BlockingCallInfo) -> None:
            captured.append(info)

        monitor = EventLoopMonitor(
            threshold_ms=0.0,
            on_blocking_call=callback,
            include_paths=["test_monitor"],
            exclude_paths=[],
        )
        monitor.activate()

        # This runs outside an event loop
        time.sleep(0.01)

        monitor.deactivate()

        # Should not capture anything - we're not in an event loop
        assert len(captured) == 0


class TestConcurrency:
    @pytest.mark.asyncio
    async def test_nested_contexts_work_correctly(self) -> None:
        captured: list[BlockingCallInfo] = []

        def callback(info: BlockingCallInfo) -> None:
            captured.append(info)

        with event_loop_monitor_ctx(
            threshold_ms=5.0,
            on_blocking_call=callback,
            include_paths=["test_monitor"],
            exclude_paths=[],
        ):
            with event_loop_monitor_ctx(
                threshold_ms=5.0,
                on_blocking_call=callback,
                include_paths=["test_monitor"],
                exclude_paths=[],
            ):
                time.sleep(0.015)
            # Monitor should still be active here
            time.sleep(0.015)

        # Both sleeps should be captured
        assert len(captured) >= 2

    def test_thread_safety(self) -> None:
        errors: list[Exception] = []

        def thread_func() -> None:
            try:
                with event_loop_monitor_ctx(threshold_ms=50.0):
                    time.sleep(0.01)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=thread_func) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0


class TestBlockingCallInfo:
    def test_dataclass_fields(self) -> None:
        info = BlockingCallInfo(
            method_name="mymodule.myfunction",
            duration_ms=25.5,
            is_c_call=False,
        )
        assert info.method_name == "mymodule.myfunction"
        assert info.duration_ms == 25.5
        assert info.is_c_call is False

    def test_c_call_flag(self) -> None:
        info = BlockingCallInfo(
            method_name="time.sleep",
            duration_ms=100.0,
            is_c_call=True,
        )
        assert info.is_c_call is True
