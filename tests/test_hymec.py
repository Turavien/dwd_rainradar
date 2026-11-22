"""Exercise the optional analysis from its ODIM pixel to the HA sensor."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import h5py
import pytest
from homeassistant.components.sensor import SensorDeviceClass

from custom_components.dwd_rainradar.coordinator import UpdateCoordinator
from custom_components.dwd_rainradar.decoder import Decoder
from custom_components.dwd_rainradar.hymec import Hymec
from custom_components.dwd_rainradar.models import FetchResult, ProductMetadata
from custom_components.dwd_rainradar.products import HYMEC, PRECIPITATION_TYPES
from custom_components.dwd_rainradar.sensor import DwdRainRadarSensor, PRECIPITATION_TYPE_ICONS, TYPE_SENSORS
from custom_components.dwd_rainradar.state import State
from custom_components.dwd_rainradar.storage import Storage

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
CODES = [*range(11), 254, 255, 42]
CELLS = tuple((0, n) for n in range(len(CODES)))


def analysis(codes=CODES, timestamp=NOW, quantity="CLASS", shape=(1200, 1100)):
    """Construct a real ODIM byte stream with the published categorical encoding."""
    buffer = BytesIO()
    with h5py.File(buffer, "w") as file:
        date, time = timestamp.strftime("%Y%m%d"), timestamp.strftime("%H%M%S")
        file.create_group("what").attrs.update(date=date, time=time)
        group = file.create_group("dataset1")
        group.create_group("what").attrs.update(prodname="HymecNG_top_view", startdate=date, starttime=time, enddate=date, endtime=time)
        data = group.create_group("data1")
        raster = data.create_dataset("data", shape, chunks=(1, 32), dtype="uint8")
        raster[0, :len(codes)] = codes
        data.create_group("what").attrs.update(quantity=quantity, nodata=255, undetect=254)
    return FetchResult(HYMEC, True, None, data=buffer.getvalue(), metadata=ProductMetadata(etag=str(codes)))


def test_enum_sensor_and_translation_contract():
    decoded = Decoder(MagicMock()).decode_cells(analysis(), CELLS)
    entity = DwdRainRadarSensor(MagicMock(), TYPE_SENSORS[0])
    assert entity.device_class is SensorDeviceClass.ENUM
    assert entity.options == list(PRECIPITATION_TYPES.values())
    assert entity.native_unit_of_measurement is None
    assert entity.state_class is None
    state = State({}, {}, NOW)
    assert state.precipitation_type is None
    for cell, expected in zip(CELLS, (*PRECIPITATION_TYPES.values(), None, None), strict=True):
        entity.coordinator.data = state.with_product(decoded[cell])
        assert entity.native_value == expected
        assert entity.icon == PRECIPITATION_TYPE_ICONS.get(expected, "mdi:weather-cloudy")
        assert entity.available
        entity.coordinator.data = entity.coordinator.data.with_reference_time(NOW + timedelta(minutes=14))
        assert not entity.available
    folder = Path(__file__).resolve().parents[1] / "custom_components/dwd_rainradar"
    for language in ("de", "en"):
        translations = json.loads((folder / "translations" / f"{language}.json").read_text())
        assert set(translations["entity"]["sensor"]["precipitation_type"]["state"]) == set(entity.options)
    assert set(PRECIPITATION_TYPE_ICONS) == set(entity.options)
    with pytest.raises(ValueError, match="CLASS"):
        Decoder(MagicMock()).decode_cells(analysis(quantity="RATE"), CELLS)


    with pytest.raises(ValueError, match="grid"):
        Decoder(MagicMock()).decode_cells(analysis(shape=(10, 32)), CELLS)


async def test_background_analysis_lifecycle(hass, tmp_path):
    hass.config.config_dir = str(tmp_path)
    fetcher = AsyncMock()
    decoder = Decoder(hass)
    storage = Storage(hass)
    service = Hymec(hass, fetcher, decoder, storage)
    a, b = MagicMock(), MagicMock()
    service.subscribe(CELLS[0], a)
    duplicate = MagicMock()
    service.subscribe(CELLS[0], duplicate)
    service.subscribe(CELLS[1], b)
    fetcher.async_download.return_value = analysis()
    assert service.current(CELLS[0]) is None
    task = service._task
    assert service.current(CELLS[1]) is None
    assert service._task is task
    await task
    fetcher.async_download.assert_awaited_once()
    duplicate.assert_called_once()
    service.unsubscribe(duplicate)
    a.assert_called_once()
    b.assert_called_once()
    assert service.current(CELLS[1]).values[0].value == 1
    assert service.diagnostics(None)["last_product_timestamp"] == NOW.isoformat()

    # A third station shares the latest compressed frame after an HTTP 304.
    c = MagicMock()
    service.subscribe(CELLS[2], c)
    fetcher.async_download.return_value = FetchResult(HYMEC, False, None)
    assert service.current(CELLS[2]) is None
    await service._task
    assert service.current(CELLS[2]).values[0].value == 2
    assert a.call_count == 1

    # A delayed frame cannot replace a newer local observation.
    fetcher.async_download.return_value = analysis(timestamp=NOW - timedelta(minutes=5))
    await service._update()
    assert service.current(CELLS[0]).values[0].timestamp == NOW

    fetcher.async_download.side_effect = TimeoutError()
    await service._update()
    await service._update()
    assert service.diagnostics(None)["update_failed"]
    assert service.current(CELLS[0]).values[0].value == 0

    # A revision with the same timestamp must replace values. Prune at the next timestamp.
    fetcher.async_download.side_effect = None
    fetcher.async_download.return_value = FetchResult(HYMEC, False, None, remote_checked=False)
    await service._update()
    assert service.diagnostics(None)["update_failed"]
    fetcher.async_download.return_value = analysis([10, 10, 10])
    await service._update()
    assert service.current(CELLS[0]).values[0].value == 10
    fetcher.async_download.return_value = analysis([7, 7, 7], NOW + timedelta(minutes=5))
    await service._update()
    assert len(list((tmp_path / ".cache/dwd_rainradar/hymecng").glob("*.hdf5"))) == 1
    for callback in (a, b, c):
        service.unsubscribe(callback)
    await service.shutdown()

    # On restart the local frame remains usable during a network outage.
    restarted = Hymec(hass, fetcher, decoder, storage)
    restarted.subscribe(CELLS[0], a)
    fetcher.async_download.side_effect = TimeoutError()
    await restarted._update()
    assert restarted.current(CELLS[0]).values[0].value == 7
    await restarted.shutdown()


async def test_slow_hymec_does_not_block_radar_and_unload_cancels(hass, tmp_path):
    hass.config.config_dir = str(tmp_path)
    engine = MagicMock()
    fetcher = AsyncMock()
    waiting = asyncio.Event()

    async def slow(*args):
        waiting.set()
        await asyncio.Event().wait()

    fetcher.async_download.side_effect = slow
    engine.hymec = Hymec(hass, fetcher, Decoder(hass), Storage(hass))
    entry = MagicMock()
    entry.data = {"grid_cell": CELLS[0]}
    entry.options = {"sensor_groups": ["precipitation_type"]}
    coordinator = UpdateCoordinator(hass, entry, engine)
    state = State({}, {}, NOW)
    engine.async_update = AsyncMock(return_value=state)
    assert await coordinator._async_update_data() is state
    await asyncio.wait_for(waiting.wait(), timeout=5)
    assert coordinator.get_diagnostics() is engine.get_diagnostics.return_value
    coordinator.unregister_engine()
    await engine.hymec.shutdown()
    assert engine.hymec._task.cancelled()


@pytest.mark.parametrize(("code", "expected", "icon"), [(3, "rain", "mdi:weather-rainy"), (7, "snow", "mdi:weather-snowy")])
async def test_registered_sensor_opt_in_and_reload(hass, tmp_path, code, expected, icon):
    """Start with the group disabled, enable the enum, and remove just that entity again."""
    from unittest.mock import patch

    from homeassistant.helpers import entity_registry as er
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.dwd_rainradar.const import CONF_SENSOR_GROUPS, DEFAULT_SENSOR_GROUPS, DOMAIN
    from custom_components.dwd_rainradar.engine import Engine

    hass.config.config_dir = str(tmp_path)
    cell = CELLS[code]
    original_groups = [g for g in DEFAULT_SENSOR_GROUPS if g != "precipitation_type"]
    entry = MockConfigEntry(domain=DOMAIN, title="Radar", version=1, minor_version=6,
                            data={"grid_cell": cell}, options={CONF_SENSOR_GROUPS: original_groups})
    entry.add_to_hass(hass)
    now = datetime.now(UTC)
    state = State({}, {}, now)
    registry = er.async_get(hass)
    with (
        patch.object(Engine, "async_update", new_callable=AsyncMock, return_value=state),
        patch("custom_components.dwd_rainradar.fetcher.Fetcher.async_download",
              new_callable=AsyncMock, return_value=analysis(timestamp=now)) as download,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        old = {e.entity_id for e in er.async_entries_for_config_entry(registry, entry.entry_id)}
        assert len(old) == 19
        download.assert_not_awaited()

        hass.config_entries.async_update_entry(entry, options={
            CONF_SENSOR_GROUPS: DEFAULT_SENSOR_GROUPS,
        })
        await hass.async_block_till_done()
        coordinator = entry.runtime_data.coordinator
        await coordinator._engine.hymec._task
        await coordinator.async_refresh()
        current = {e.entity_id for e in er.async_entries_for_config_entry(registry, entry.entry_id)}
        entity_id, = current - old
        assert current == old | {entity_id}
        assert hass.states.get(entity_id).state == expected
        assert hass.states.get(entity_id).attributes["icon"] == icon
        assert hass.states.get(entity_id).attributes["options"] == list(PRECIPITATION_TYPES.values())
        assert coordinator.get_diagnostics()["hymecng"]["fresh"]
        hass.config_entries.async_update_entry(entry, options={CONF_SENSOR_GROUPS: original_groups})
        await hass.async_block_till_done()
        assert old == {e.entity_id for e in er.async_entries_for_config_entry(registry, entry.entry_id)}
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_location_joining_during_executor_decode(hass, tmp_path):
    """A location added during first refresh receives the same revision immediately."""
    hass.config.config_dir = str(tmp_path)
    fetcher = AsyncMock()
    fetcher.async_download.return_value = analysis()
    decoder = Decoder(hass)
    started, resume = asyncio.Event(), asyncio.Event()
    decode = decoder.async_decode_cells

    async def delayed(result, cells):
        started.set()
        await resume.wait()
        return await decode(result, cells)

    decoder.async_decode_cells = AsyncMock(side_effect=delayed)
    service = Hymec(hass, fetcher, decoder, Storage(hass))
    a, b = MagicMock(), MagicMock()
    service.subscribe(CELLS[0], a)
    service.current(CELLS[0])
    await asyncio.wait_for(started.wait(), 5)
    service.subscribe(CELLS[1], b)
    assert service.current(CELLS[1]) is None
    resume.set()
    await service._task
    assert service.current(CELLS[1]).values[0].value == 1
    assert decoder.async_decode_cells.await_count == 2
    fetcher.async_download.assert_awaited_once()
    a.assert_called_once()
    b.assert_called_once()
    await service.shutdown()
