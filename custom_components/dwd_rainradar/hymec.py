"""Shared, optional HymecNG analysis for configured locations."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from time import monotonic

from aiohttp import ClientError
from homeassistant.core import HomeAssistant

from .decoder import Decoder
from .fetcher import Fetcher
from .models import DecodedProduct, FetchResult
from .products import HYMEC
from .state import State
from .storage import Storage

_LOGGER = logging.getLogger(__name__)
Cell = tuple[int, int]


class Hymec:
    """Run at most one analysis request independently of forecast updates."""

    def __init__(self, hass: HomeAssistant, fetcher: Fetcher, decoder: Decoder, storage: Storage) -> None:
        self._hass = hass
        self._fetcher = fetcher
        self._decoder = decoder
        self._storage = storage
        self._listeners: dict[Callable[[], None], Cell] = {}
        self._values: dict[Cell, DecodedProduct] = {}
        self._latest: FetchResult | None = None
        self._task: asyncio.Task[None] | None = None
        self._next_check = 0.0
        self._failed = False

    def subscribe(self, cell: Cell, callback: Callable[[], None]) -> None:
        """Include this location in the next shared analysis."""
        self._listeners[callback] = cell

    def unsubscribe(self, callback: Callable[[], None]) -> None:
        """Release a location and stop polling after the last subscriber leaves."""
        cell = self._listeners.pop(callback)
        if cell not in self._listeners.values():
            self._values.pop(cell, None)
        if not self._listeners and self._task is not None:
            self._task.cancel()

    def current(self, cell: Cell) -> DecodedProduct | None:
        """Start due background work and immediately return the latest analysis."""
        if (self._task is None or self._task.done()) and (
            monotonic() >= self._next_check or (self._latest is not None and cell not in self._values and not self._failed)
        ):
            self._task = self._hass.async_create_background_task(
                self._update(), "DWD Rain Radar HymecNG",
            )
        return self._values.get(cell)

    async def _update(self) -> None:
        try:
            if self._latest is None:
                try:
                    cached = await self._storage.async_read_latest_product(HYMEC)
                    await self._accept(cached)
                except (OSError, ValueError, IndexError):
                    # A missing or damaged local analysis can be replaced remotely.
                    pass

            await self._fill_missing_cells()
            latest = self._latest
            result = await self._fetcher.async_download(
                HYMEC, latest.metadata if latest else None, latest.timestamp if latest else None,
            )
            if result.downloaded:
                await self._accept(result)
            await self._fill_missing_cells()
            if not result.remote_checked:
                return
        except (ClientError, OSError, ValueError, IndexError, RuntimeError) as err:
            if not self._failed:
                _LOGGER.warning("Could not update HymecNG: %s", err)
            self._failed = True
        else:
            if self._failed:
                _LOGGER.info("HymecNG updates resumed")
            self._failed = False
        finally:
            self._next_check = monotonic() + 30

    def _publish(self, values: dict[Cell, DecodedProduct], *, replace: bool) -> None:
        if replace:
            self._values.clear()
        for callback, cell in tuple(self._listeners.items()):
            if cell in values:
                self._values[cell] = values[cell]
                callback()

    async def _fill_missing_cells(self) -> None:
        """Include subscribers that joined while a download or decode was in flight."""
        while self._latest is not None:
            missing = set(self._listeners.values()) - self._values.keys()
            if not missing:
                return
            values = await self._decoder.async_decode_cells(self._latest, tuple(missing))
            self._publish(values, replace=False)

    async def _accept(self, result: FetchResult) -> None:
        cells = tuple(set(self._listeners.values()))
        values = await self._decoder.async_decode_cells(result, cells)
        if self._latest and result.timestamp < self._latest.timestamp:
            return
        if result.downloaded:
            await self._storage.async_store_product(result)
            await self._storage.async_delete_old_files(HYMEC, result.valid_until)
        self._latest = result
        self._publish(values, replace=True)

    def diagnostics(self, state: State | None) -> dict[str, object]:
        """Expose analysis freshness without station coordinates."""
        latest = self._latest
        return {
            "last_product_timestamp": latest.timestamp.isoformat() if latest else None,
            "fresh": state.is_product_fresh(HYMEC) if state else None,
            "update_failed": self._failed,
        }

    async def shutdown(self) -> None:
        """Wait for pending background work to finish cancelling."""
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
