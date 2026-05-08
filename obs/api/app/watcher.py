"""
File watcher — monitors log directories via inotify (watchdog).

When a matching file is created or modified, a debounced callback is triggered.
Debounce window: 2 seconds after the last write event on the same file.

Bridge: watchdog runs in a background thread; ingest runs as an asyncio
coroutine. We use asyncio.run_coroutine_threadsafe() to cross the boundary.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from collections.abc import Callable

from watchdog.events import FileSystemEventHandler, FileSystemEvent, FileMovedEvent
from watchdog.observers import Observer

log = logging.getLogger(__name__)

DEBOUNCE_SECONDS = 2.0


class _FileChangeHandler(FileSystemEventHandler):
    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        callback: Callable,
        suffixes: tuple[str, ...],
        label: str,
    ):
        super().__init__()
        self._loop = loop
        self._callback = callback
        self._suffixes = suffixes
        self._label = label
        self._timers: dict[str, threading.Timer] = {}
        self._lock = threading.Lock()

    def _schedule(self, path: str) -> None:
        with self._lock:
            existing = self._timers.pop(path, None)
            if existing:
                existing.cancel()
            t = threading.Timer(DEBOUNCE_SECONDS, self._fire, args=(path,))
            self._timers[path] = t
            t.start()

    def _fire(self, path: str) -> None:
        with self._lock:
            self._timers.pop(path, None)
        log.info("watcher: change detected → %s", path)
        future = asyncio.run_coroutine_threadsafe(self._callback(), self._loop)

        def _done(fut):
            try:
                result = fut.result()
                log.info("watcher: %s complete for %s: %s", self._label, path, result)
            except Exception:
                log.exception("watcher: %s failed for %s", self._label, path)

        future.add_done_callback(_done)

    def _matches(self, path: str) -> bool:
        return path.endswith(self._suffixes)

    def on_modified(self, event: FileSystemEvent) -> None:
        path = str(event.src_path)
        if not event.is_directory and self._matches(path):
            self._schedule(path)

    def on_created(self, event: FileSystemEvent) -> None:
        path = str(event.src_path)
        if not event.is_directory and self._matches(path):
            self._schedule(path)

    def on_moved(self, event: FileMovedEvent) -> None:
        path = str(event.dest_path)
        if not event.is_directory and self._matches(path):
            self._schedule(path)


class MultiLogWatcher:
    """Watches multiple file-change targets with a single Observer."""

    def __init__(self, targets: list[tuple]) -> None:
        """
        targets may be:
        - (log_dir, ingest_fn), matching *.jsonl and labeling callback as ingest.
        - (log_dir, callback, suffixes, label), for other file-change callbacks.
        callback must be a zero-argument callable returning a coroutine.
        """
        self._targets = targets
        self._observer: Observer | None = None

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self._observer = Observer()
        scheduled = 0
        for target in self._targets:
            if len(target) == 2:
                log_dir, callback = target
                suffixes = (".jsonl",)
                label = "ingest"
            else:
                log_dir, callback, suffixes, label = target
            suffixes = tuple(suffixes)
            if not os.path.isdir(log_dir):
                log.warning("watcher: log_dir not found: %s — skipping", log_dir)
                continue
            handler = _FileChangeHandler(loop=loop, callback=callback, suffixes=suffixes, label=label)
            self._observer.schedule(handler, log_dir, recursive=True)
            log.info("watcher: watching %s for %s", log_dir, ", ".join(suffixes))
            scheduled += 1

        if scheduled > 0:
            self._observer.start()
        else:
            log.warning("watcher: no valid directories to watch — observer not started")

    def stop(self) -> None:
        if self._observer and self._observer.is_alive():
            self._observer.stop()
            self._observer.join()
            log.info("watcher: stopped")


# Backward-compatible alias
class LogWatcher(MultiLogWatcher):
    def __init__(self, log_dir: str, ingest_fn) -> None:
        super().__init__([(log_dir, ingest_fn)])
