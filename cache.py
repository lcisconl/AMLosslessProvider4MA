"""On-disk cache for decrypted Apple Music Lossless tracks."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from music_assistant_models.errors import MediaNotFoundError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

_ENTRY_PATTERN = re.compile(r"^(?P<item>.+)\.alac\.(?P<rate>\d+)\.(?P<depth>\d+)\.mp4$")
_UNSAFE_PATTERN = re.compile(r"[^0-9A-Za-z_-]")
_MIN_FILE_SIZE = 64 * 1024
_READ_CHUNK = 256 * 1024
_TOUCH_INTERVAL = 60 * 60
_PRUNE_INTERVAL = 60.0
_PARTIAL_MAX_AGE = 6 * 60 * 60


@dataclass(slots=True)
class CachedTrack:
    """A decrypted track stored on disk."""

    path: Path
    item_id: str
    sample_rate: int
    bit_depth: int
    size: int
    mtime: float

    @property
    def quality(self) -> tuple[int, int]:
        """Return the sample rate and bit depth used for quality ranking."""
        return (self.sample_rate, self.bit_depth)


class TrackCache:
    """LRU file cache for decrypted ALAC tracks."""

    def __init__(
        self,
        logger: logging.Logger,
        root: Path,
        max_bytes: int,
        enabled: bool = True,
    ) -> None:
        """Initialize the cache, without touching the filesystem yet."""
        self._logger = logger
        self._root = root
        self._max_bytes = max(0, int(max_bytes))
        self._enabled = bool(enabled) and self._max_bytes > 0
        self._available = False
        self._locks: dict[str, asyncio.Lock] = {}
        self._prune_lock = asyncio.Lock()
        self._prune_task: asyncio.Task[None] | None = None
        self._last_prune = 0.0
        self.hits = 0
        self.misses = 0
        self.evictions = 0

    @property
    def logger(self) -> logging.Logger:
        """Return the logger used by the cache."""
        return self._logger

    @property
    def enabled(self) -> bool:
        """Return whether the cache can currently read and write tracks."""
        return self._enabled and self._available

    @property
    def root(self) -> Path:
        """Return the cache directory."""
        return self._root

    @property
    def max_bytes(self) -> int:
        """Return the maximum size of the cache in bytes."""
        return self._max_bytes

    async def initialize(self) -> None:
        """Create the cache directory and schedule startup maintenance."""
        if not self._enabled:
            # Turning the cache off also releases the disk space it used.
            await asyncio.to_thread(self._purge_all)
            self._logger.debug(
                "Apple Music lossless track cache disabled, removed cached files"
            )
            return
        try:
            await asyncio.to_thread(self._root.mkdir, parents=True, exist_ok=True)
        except OSError as err:
            self._logger.warning(
                "Apple Music lossless track cache disabled, cannot create %s: %s",
                self._root,
                err,
            )
            return
        self._available = True
        self._logger.debug("Apple Music lossless track cache at %s", self._root)
        self.schedule_prune()

    async def close(self) -> None:
        """Stop background cache maintenance."""
        task = self._prune_task
        self._prune_task = None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def find(self, item_id: str) -> CachedTrack | None:
        """Return the best cached quality for a track, without counting statistics."""
        if not self.enabled:
            return None
        tracks = self._entries(item_id)
        if not tracks:
            return None
        track = max(tracks, key=lambda entry: entry.quality)
        self._touch(track.path)
        return track

    def lookup(self, item_id: str) -> CachedTrack | None:
        """Return the best cached quality for a track and update hit statistics."""
        track = self.find(item_id)
        if track is None:
            self.misses += 1
        else:
            self.hits += 1
        return track

    async def iterate(self, track: CachedTrack) -> AsyncIterator[bytes]:
        """Yield a cached track from disk."""
        try:
            handle = await asyncio.to_thread(open, track.path, "rb")
        except OSError as err:
            raise MediaNotFoundError(f"Cached track is unavailable: {err}") from err
        try:
            while True:
                chunk = await asyncio.to_thread(handle.read, _READ_CHUNK)
                if not chunk:
                    break
                yield chunk
        finally:
            handle.close()

    async def begin_write(
        self, item_id: str, sample_rate: int, bit_depth: int
    ) -> TrackCacheWriter | None:
        """Open a writer for an uncached track that no other stream is writing."""
        if not self.enabled or self.find(item_id) is not None:
            return None
        lock = self._locks.get(item_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[item_id] = lock
        if lock.locked():
            return None
        await lock.acquire()
        writer: TrackCacheWriter | None = None
        try:
            writer = TrackCacheWriter(self, item_id, sample_rate, bit_depth)
        except OSError as err:
            self._logger.warning("Cannot cache %s: %s", item_id, err)
        if writer is None:
            lock.release()
            return None
        self._prune_locks()
        return writer

    def end_write(self, item_id: str) -> None:
        """Release the writer lock for a track."""
        lock = self._locks.get(item_id)
        if lock is not None and lock.locked():
            lock.release()

    def entry_path(self, item_id: str, sample_rate: int, bit_depth: int) -> Path:
        """Return the final cache path for a track and quality."""
        safe = _UNSAFE_PATTERN.sub("_", item_id) or "track"
        return self._root / f"{safe}.alac.{int(sample_rate)}.{int(bit_depth)}.mp4"

    def temp_path(self, item_id: str, sample_rate: int, bit_depth: int) -> Path:
        """Return a unique temporary path for an in-progress download."""
        path = self.entry_path(item_id, sample_rate, bit_depth)
        return path.with_name(f"{path.name}.{os.getpid()}.{uuid4().hex[:8]}.part")

    def schedule_prune(self) -> None:
        """Schedule cache maintenance without blocking playback."""
        if not self.enabled:
            return
        if self._prune_task is not None and not self._prune_task.done():
            return
        if time.monotonic() - self._last_prune < _PRUNE_INTERVAL:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._prune_task = loop.create_task(self._prune())

    async def stats(self) -> dict[str, Any]:
        """Return cache statistics for provider diagnostics."""
        tracks = await asyncio.to_thread(self._scan)
        total = sum(track.size for track in tracks)
        return {
            "enabled": self.enabled,
            "available": self._available,
            "path": str(self._root),
            "limit_mb": round(self._max_bytes / 1048576, 1),
            "size_mb": round(total / 1048576, 1),
            "tracks": len(tracks),
            "hits": self.hits,
            "misses": self.misses,
            "evictions": self.evictions,
        }

    def _entries(self, item_id: str) -> list[CachedTrack]:
        """Return cached variants for one track."""
        safe = _UNSAFE_PATTERN.sub("_", item_id) or "track"
        try:
            paths = list(self._root.glob(f"{safe}.alac.*.mp4"))
        except OSError:
            return []
        tracks: list[CachedTrack] = []
        for path in paths:
            track = self._describe(path)
            if track is not None and track.item_id == item_id:
                tracks.append(track)
        return tracks

    def _scan(self) -> list[CachedTrack]:
        """Return all complete cache entries."""
        try:
            paths = list(self._root.glob("*.alac.*.mp4"))
        except OSError:
            return []
        tracks: list[CachedTrack] = []
        for path in paths:
            track = self._describe(path)
            if track is not None:
                tracks.append(track)
        return tracks

    def _describe(self, path: Path) -> CachedTrack | None:
        """Return the descriptor for a cache file, or None when it is unusable."""
        match = _ENTRY_PATTERN.match(path.name)
        if match is None:
            return None
        try:
            stat = path.stat()
        except OSError:
            return None
        if stat.st_size < _MIN_FILE_SIZE:
            return None
        return CachedTrack(
            path=path,
            item_id=match.group("item"),
            sample_rate=int(match.group("rate")),
            bit_depth=int(match.group("depth")),
            size=stat.st_size,
            mtime=stat.st_mtime,
        )

    def _touch(self, path: Path) -> None:
        """Refresh the LRU timestamp of a recently used track."""
        try:
            if time.time() - path.stat().st_mtime > _TOUCH_INTERVAL:
                os.utime(path, None)
        except OSError:
            pass

    def _prune_locks(self) -> None:
        """Drop locks for tracks that are no longer being written."""
        if len(self._locks) <= 128:
            return
        for key, lock in list(self._locks.items()):
            if not lock.locked():
                del self._locks[key]

    def _clean_partials(self) -> None:
        """Remove stale partial downloads left by a crashed provider."""
        now = time.time()
        for path in self._root.glob("*.part"):
            with contextlib.suppress(OSError):
                if now - path.stat().st_mtime > _PARTIAL_MAX_AGE:
                    path.unlink()

    def _purge_all(self) -> None:
        """Remove every cached track and partial download."""
        for pattern in ("*.alac.*.mp4", "*.part"):
            for path in self._root.glob(pattern):
                with contextlib.suppress(OSError):
                    path.unlink()

    def _purge_all(self) -> None:
        """Remove every cached track and partial download."""
        for pattern in ("*.alac.*.mp4", "*.part"):
            for path in self._root.glob(pattern):
                with contextlib.suppress(OSError):
                    path.unlink()

    async def _prune(self) -> None:
        """Enforce the size limit, evicting least recently used tracks first."""
        self._last_prune = time.monotonic()
        try:
            async with self._prune_lock:
                tracks = await asyncio.to_thread(self._scan)
                total = sum(track.size for track in tracks)
                if total > self._max_bytes:
                    for track in sorted(tracks, key=lambda entry: entry.mtime):
                        if total <= self._max_bytes:
                            break
                        try:
                            await asyncio.to_thread(track.path.unlink)
                        except OSError:
                            continue
                        total -= track.size
                        self.evictions += 1
                        self._logger.debug(
                            "Evicted cached track %s", track.path.name
                        )
                await asyncio.to_thread(self._clean_partials)
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001
            self._logger.warning("Track cache maintenance failed: %s", err)


class TrackCacheWriter:
    """Write one track to a temporary file and commit it into the cache."""

    def __init__(
        self,
        cache: TrackCache,
        item_id: str,
        sample_rate: int,
        bit_depth: int,
    ) -> None:
        """Open the temporary file that receives the decrypted stream."""
        self._cache = cache
        self.item_id = item_id
        self.sample_rate = sample_rate
        self.bit_depth = bit_depth
        self._final_path = cache.entry_path(item_id, sample_rate, bit_depth)
        self._temp_path = cache.temp_path(item_id, sample_rate, bit_depth)
        self._handle = open(  # noqa: SIM115
            self._temp_path, "wb", buffering=1024 * 1024
        )
        self._size = 0
        self._closed = False

    @property
    def written(self) -> int:
        """Return the number of bytes written so far."""
        return self._size

    @property
    def closed(self) -> bool:
        """Return whether the writer was committed or aborted."""
        return self._closed

    async def write(self, data: bytes) -> None:
        """Write one chunk off the event loop, abandoning the cache copy on failure."""
        if self._closed or not data:
            return
        if self._size + len(data) > self._cache.max_bytes:
            self._cache.logger.debug(
                "Skipping cache for %s: exceeds cache size limit", self.item_id
            )
            self.abort()
            return
        try:
            await asyncio.to_thread(self._handle.write, data)
        except (OSError, ValueError) as err:
            self._cache.logger.warning(
                "Track cache write failed for %s: %s", self.item_id, err
            )
            self.abort()
            return
        self._size += len(data)

    def commit(self) -> bool:
        """Finish the download and atomically move it into the cache."""
        if self._closed:
            return False
        try:
            self._handle.close()
        finally:
            self._closed = True
        if self._size < _MIN_FILE_SIZE:
            self._unlink(self._temp_path)
            return False
        try:
            os.replace(self._temp_path, self._final_path)
            now = time.time()
            os.utime(self._final_path, (now, now))
        except OSError as err:
            self._cache.logger.warning(
                "Track cache commit failed for %s: %s", self.item_id, err
            )
            self._unlink(self._temp_path)
            return False
        self._cache.logger.debug(
            "Cached %s (%s bytes) at %s", self.item_id, self._size, self._final_path
        )
        self._cache.schedule_prune()
        return True

    def abort(self) -> None:
        """Close and remove the partial download."""
        if not self._closed:
            self._closed = True
            with contextlib.suppress(OSError, ValueError):
                self._handle.close()
        self._unlink(self._temp_path)

    @staticmethod
    def _unlink(path: Path) -> None:
        """Remove a file, ignoring missing files and locked handles."""
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)
