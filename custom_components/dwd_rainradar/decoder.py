"""Decoder for DWD product files."""

from __future__ import annotations

from homeassistant.core import HomeAssistant

from .extractor import Extractor
from .models import (
    DecodedProduct,
    FetchResult,
    ParsedValue,
)
from .parser import Parser
from .products import FileType, HYMEC


class Decoder:
    """Decode downloaded DWD product files."""

    def __init__(
        self,
        hass: HomeAssistant,
    ) -> None:
        """Initialize decoder."""

        self._hass = hass

        self._extractor = Extractor()
        self._parser = Parser()

    async def async_decode(
        self,
        result: FetchResult,
        grid_cell: tuple[int, int],
    ) -> DecodedProduct:
        """Decode one product outside the Home Assistant event loop."""

        return await self._hass.async_add_executor_job(
            self.decode,
            result,
            grid_cell,
        )

    async def async_decode_cells(
        self,
        result: FetchResult,
        grid_cells: tuple[
            tuple[int, int],
            ...
        ],
    ) -> dict[
        tuple[int, int],
        DecodedProduct,
    ]:
        """Decode multiple grid cells outside the Home Assistant event loop."""

        return await self._hass.async_add_executor_job(
            self.decode_cells,
            result,
            grid_cells,
        )

    def decode(
        self,
        result: FetchResult,
        grid_cell: tuple[int, int],
    ) -> DecodedProduct:
        """Decode one DWD product for one grid cell."""

        return self.decode_cells(result, (grid_cell,))[grid_cell]

    def decode_cells(
        self,
        result: FetchResult,
        grid_cells: tuple[tuple[int, int], ...],
    ) -> dict[tuple[int, int], DecodedProduct]:
        """Decode all requested cells during one pass through the product."""
        if result.data is None:
            raise ValueError("FetchResult contains no data.")
        if result.product.file_type is FileType.TAR:
            frames = self._extractor.extract(result.data).values()
        elif result.product.file_type is FileType.HDF5:
            frames = (result.data,)
        else:
            raise ValueError(f"Unsupported file type: {result.product.file_type}")

        series: dict[tuple[int, int], list[ParsedValue]] = {}
        for frame in frames:
            values = self._parser.read_cells(
                frame, grid_cells, categorical=result.product == HYMEC,
            )
            for cell, value in values.items():
                series.setdefault(cell, []).append(value)

        if not series:
            raise ValueError(f"{result.product.key} contains no values.")

        decoded = {
            cell: DecodedProduct(
                product=result.product,
                metadata=result.metadata,
                values=tuple(sorted(values, key=lambda value: value.valid_from)),
            )
            for cell, values in series.items()
        }
        first_series = next(iter(decoded.values())).values
        if result.timestamp is None:
            result.timestamp = first_series[0].timestamp
        result.valid_from = first_series[0].valid_from
        result.valid_until = first_series[-1].valid_until
        return decoded
