"""Test config-entry migration for DWD Rain Radar."""

from homeassistant.core import HomeAssistant

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.dwd_rainradar import async_migrate_entry
from custom_components.dwd_rainradar.const import (
    CONF_SENSOR_GROUPS,
    DEFAULT_SENSOR_GROUPS,
    DOMAIN,
    SENSOR_GROUP_CURRENT,
    SENSOR_GROUP_EVENT,
    SENSOR_GROUP_TYPE,
)


async def test_migrate_minor_4_sensor_groups_to_options(
    hass: HomeAssistant,
) -> None:
    """Test migration of sensor groups from data to options."""

    selected_groups = [
        SENSOR_GROUP_CURRENT,
        SENSOR_GROUP_EVENT,
    ]

    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        minor_version=4,
        data={
            "name": "Test",
            "latitude": 52.52,
            "longitude": 13.405,
            "grid_cell": (416, 784),
            CONF_SENSOR_GROUPS: selected_groups,
        },
    )
    entry.add_to_hass(hass)

    assert await async_migrate_entry(
        hass,
        entry,
    )

    assert entry.minor_version == 6
    assert CONF_SENSOR_GROUPS not in entry.data
    assert entry.options[CONF_SENSOR_GROUPS] == [*selected_groups, SENSOR_GROUP_TYPE]


async def test_migrate_old_entry_adds_default_groups(
    hass: HomeAssistant,
) -> None:
    """Test migration of an old entry without sensor groups."""

    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        minor_version=1,
        data={
            "name": "Test",
            "latitude": 52.52,
            "longitude": 13.405,
            "grid_cell": (416, 784),
        },
    )
    entry.add_to_hass(hass)

    assert await async_migrate_entry(
        hass,
        entry,
    )

    assert entry.minor_version == 6
    assert CONF_SENSOR_GROUPS not in entry.data
    assert entry.options[CONF_SENSOR_GROUPS] == DEFAULT_SENSOR_GROUPS


async def test_migration_preserves_existing_options(
    hass: HomeAssistant,
) -> None:
    """Test that existing options take precedence during migration."""

    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        minor_version=4,
        data={
            "name": "Test",
            "latitude": 52.52,
            "longitude": 13.405,
            "grid_cell": (416, 784),
            CONF_SENSOR_GROUPS: [SENSOR_GROUP_CURRENT],
        },
        options={
            CONF_SENSOR_GROUPS: [SENSOR_GROUP_EVENT],
        },
    )
    entry.add_to_hass(hass)

    assert await async_migrate_entry(
        hass,
        entry,
    )

    assert entry.minor_version == 6
    assert CONF_SENSOR_GROUPS not in entry.data
    assert entry.options[CONF_SENSOR_GROUPS] == [SENSOR_GROUP_EVENT, SENSOR_GROUP_TYPE]


async def test_config_entry_setup_runs_migration(
    hass: HomeAssistant,
    mock_setup_entry,
) -> None:
    """Test Home Assistant migrates the entry before setup."""

    selected_groups = [
        SENSOR_GROUP_CURRENT,
        SENSOR_GROUP_EVENT,
    ]

    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        minor_version=4,
        title="Test",
        data={
            "name": "Test",
            "latitude": 52.52,
            "longitude": 13.405,
            "grid_cell": (416, 784),
            CONF_SENSOR_GROUPS: selected_groups,
        },
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(
        entry.entry_id,
    )

    await hass.async_block_till_done()

    assert entry.minor_version == 6

    assert CONF_SENSOR_GROUPS not in entry.data

    assert entry.options[
        CONF_SENSOR_GROUPS
    ] == [*selected_groups, SENSOR_GROUP_TYPE]

    mock_setup_entry.assert_awaited_once_with(
        hass,
        entry,
    )


async def test_hymec_upgrade_is_once_per_entry(hass: HomeAssistant) -> None:
    """Preserve explicit selections, unrelated options and a subsequent opt-out."""
    for selected in ([], [SENSOR_GROUP_EVENT], [SENSOR_GROUP_TYPE]):
        entry = MockConfigEntry(
            domain=DOMAIN, version=1, minor_version=5, unique_id="id-" + str(len(hass.config_entries.async_entries(DOMAIN))),
            options={CONF_SENSOR_GROUPS: selected, "other": 123},
        )
        entry.add_to_hass(hass)
        unique_id = entry.unique_id
        assert await async_migrate_entry(hass, entry)
        assert entry.unique_id == unique_id
        assert entry.options == {
            CONF_SENSOR_GROUPS: list(dict.fromkeys([*selected, SENSOR_GROUP_TYPE])), "other": 123,
        }
        hass.config_entries.async_update_entry(entry, options={CONF_SENSOR_GROUPS: []})
        assert await async_migrate_entry(hass, entry)
        assert entry.options[CONF_SENSOR_GROUPS] == []


async def test_future_major_is_preserved(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, version=2, minor_version=1)
    entry.add_to_hass(hass)
    assert not await async_migrate_entry(hass, entry)
    assert (entry.version, entry.minor_version) == (2, 1)
