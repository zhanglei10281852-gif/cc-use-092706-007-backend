from __future__ import annotations

from app.core.clock import Clock, SystemClock

_clock: Clock = SystemClock()


def get_clock() -> Clock:
    return _clock


def set_clock(clock: Clock) -> None:
    """替换全局时钟，供测试模拟确认期限超时。"""
    global _clock
    _clock = clock


def reset_clock() -> None:
    global _clock
    _clock = SystemClock()
