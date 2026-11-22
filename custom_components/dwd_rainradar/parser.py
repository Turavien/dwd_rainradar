"""Parser for DWD HDF5 files."""

from __future__ import annotations

from datetime import (
    UTC,
    datetime,
)

from io import BytesIO

import h5py

from .models import ParsedValue
from .products import HYMEC_UNDETECT, PRECIPITATION_TYPES


def _text_attribute(
    value: object,
) -> str:
    """Return one HDF5 text attribute as a string."""

    if isinstance(
        value,
        bytes,
    ):
        return value.decode()

    if isinstance(
        value,
        str,
    ):
        return value

    raise ValueError(
        "Unsupported DWD HDF5 text attribute."
    )


class Parser:
    """Read values from DWD HDF5 files."""

    def read(
        self,
        data: bytes,
        grid_cell: tuple[int, int],
    ) -> tuple[ParsedValue, ...]:
        """Read one precipitation value from a DWD HDF5 file."""

        return (
            self.read_cells(
                data,
                (grid_cell,),
            )[grid_cell],
        )

    def read_cells(
        self,
        data: bytes,
        grid_cells: tuple[tuple[int, int], ...],
        *,
        categorical: bool = False,
    ) -> dict[tuple[int, int], ParsedValue]:
        """Open one frame and share its metadata and chunk cache across cells."""

        with h5py.File(BytesIO(data), "r") as hdf5:
            try:
                group = hdf5["dataset1/data1"]
                raster = group["data"]
                attrs = group["what"].attrs
                product = hdf5["what"].attrs
                interval = hdf5["dataset1/what"].attrs
                timestamp = _timestamp(product, "")
                start = _timestamp(interval, "start")
                end = _timestamp(interval, "end")
                nodata = float(attrs["nodata"])
                undetect = float(attrs["undetect"])
                if categorical:
                    if _text_attribute(attrs["quantity"]) != "CLASS":
                        raise ValueError("Expected a HymecNG CLASS raster.")
                    if (
                        _text_attribute(interval["prodname"]) != "HymecNG_top_view"
                        or raster.shape != (1200, 1100)
                    ):
                        raise ValueError("Unsupported HymecNG product or grid.")
                else:
                    gain = float(attrs["gain"])
                    offset = float(attrs["offset"])
            except KeyError as err:
                raise ValueError("Unsupported DWD HDF5 structure.") from err

            values = {}
            for cell in grid_cells:
                raw = float(raster[cell])
                if raw == nodata:
                    value = None
                elif categorical:
                    if raw == undetect:
                        value = HYMEC_UNDETECT
                    else:
                        value = raw if raw in PRECIPITATION_TYPES and raw != HYMEC_UNDETECT else None
                elif raw == undetect:
                    value = 0.0
                else:
                    value = max(raw * gain + offset, 0.0)
                values[cell] = ParsedValue(timestamp, start, end, value)
            return values


def _timestamp(attrs: h5py.AttributeManager, prefix: str) -> datetime:
    """Read an ODIM date/time pair as UTC."""
    date = _text_attribute(attrs[f"{prefix}date"])
    time = _text_attribute(attrs[f"{prefix}time"])
    return datetime.strptime(date + time, "%Y%m%d%H%M%S").replace(tzinfo=UTC)
