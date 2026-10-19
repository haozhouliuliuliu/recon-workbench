"""任务级锁：同一进程内，同一个 job 同时只允许一个后台任务推进。

- 用 ``threading.Lock``，因为 FastAPI 的后台任务跑在线程池里；
- 非阻塞获取：拿不到锁说明已有任务在跑，调用方据此返回 409 或跳过；
- 只解决单进程并发；多 worker/多副本需要数据库抢占（见已知限制）。
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager


class JobLockRegistry:
    def __init__(self) -> None:
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def _lock(self, job_id: str) -> threading.Lock:
        with self._guard:
            lock = self._locks.get(job_id)
            if lock is None:
                lock = threading.Lock()
                self._locks[job_id] = lock
            return lock

    def try_acquire(self, job_id: str) -> bool:
        return self._lock(job_id).acquire(blocking=False)

    def release(self, job_id: str) -> None:
        lock = self._lock(job_id)
        if lock.locked():
            lock.release()

    def is_busy(self, job_id: str) -> bool:
        return self._lock(job_id).locked()

    @contextmanager
    def hold(self, job_id: str) -> Iterator[bool]:
        acquired = self.try_acquire(job_id)
        try:
            yield acquired
        finally:
            if acquired:
                self.release(job_id)

    def active_count(self) -> int:
        with self._guard:
            return sum(1 for lock in self._locks.values() if lock.locked())
