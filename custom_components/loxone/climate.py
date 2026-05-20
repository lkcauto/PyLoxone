"""
Loxone climate

For more details about this component, please refer to the documentation at
https://github.com/JoDehli/PyLoxone
"""

import logging
import json
from abc import ABC

from homeassistant.components.climate import PLATFORM_SCHEMA, ClimateEntity
from homeassistant.components.climate.const import (ClimateEntityFeature,
                                                    HVACAction, HVACMode)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType
from voluptuous import All, Optional, Range

from . import LoxoneEntity
from .const import CONF_HVAC_AUTO_MODE, EVENT, SENDDOMAIN
from .helpers import (add_room_and_cat_to_value_values, get_all,
                      get_or_create_device)
from .miniserver import get_miniserver_from_hass

try:
    from .airzone_config import (
        AIRZONE_GLOBAL_MODE_UUID,
        AIRZONE_MODE_TO_VALUE,
        AIRZONE_VALUE_TO_MODE,
        AIRZONE_ZONES,
    )
    _HAS_AIRZONE = True
except ImportError:
    AIRZONE_ZONES = []
    _HAS_AIRZONE = False

_LOGGER = logging.getLogger(__name__)


OPMODES = {
    None: HVACMode.OFF,
    -1: HVACMode.OFF,
    0: HVACMode.AUTO,
    1: HVACMode.AUTO,
    2: HVACMode.AUTO,
    3: HVACMode.HEAT_COOL,
    4: HVACMode.HEAT,
    5: HVACMode.HEAT_COOL,
}

OPMODETOLOXONE = {
    HVACMode.HEAT_COOL: 3,
    HVACMode.HEAT: 4,
    HVACMode.COOL: 5,
    HVACMode.OFF: -1,
}


PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        Optional(CONF_HVAC_AUTO_MODE, default=0): All(int, Range(min=0, max=2)),
    }
)


# noinspection PyUnusedLocal
async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    return True


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Loxone climate entities."""
    miniserver = get_miniserver_from_hass(hass)
    loxconfig = miniserver.lox_config.json
    entities = []

    for climate in get_all(loxconfig, "IRoomControllerV2"):
        climate = add_room_and_cat_to_value_values(loxconfig, climate)
        climate.update(
            {
                "hass": hass,
                CONF_HVAC_AUTO_MODE: 0,
            }
        )
        entities.append(LoxoneRoomControllerV2(**climate))

    for climate in get_all(loxconfig, "IRoomController"):
        climate = add_room_and_cat_to_value_values(loxconfig, climate)
        climate.update(
            {
                "hass": hass,
                CONF_HVAC_AUTO_MODE: 0,
            }
        )
        entities.append(LoxoneRoomController(**climate))

    for accontrol in get_all(loxconfig, "AcControl"):
        accontrol = add_room_and_cat_to_value_values(loxconfig, accontrol)
        accontrol.update(
            {
                "hass": hass,
            }
        )
        entities.append(LoxoneAcControl(**accontrol))

    for zone_cfg in AIRZONE_ZONES:
        entities.append(LoxoneAirzoneZone(hass, zone_cfg))

    async_add_entities(entities)


class LoxoneRoomController(LoxoneEntity, ClimateEntity, ABC):
    """Loxone room controller (legacy, non-V2)"""

    def __init__(self, **kwargs):
        # Add room name to entity name for better identification in HomeKit
        if "room" in kwargs and kwargs["room"]:
            kwargs["name"] = f"{kwargs['room']} Climate"

        super().__init__(**kwargs)
        self.hass = kwargs["hass"]
        self._autoMode = kwargs[CONF_HVAC_AUTO_MODE]
        self._stateAttribUuids = kwargs["states"]
        self._stateAttribValues = {}
        self.type = "RoomController"

        # Set supported features
        self._attr_supported_features = (
            ClimateEntityFeature.TARGET_TEMPERATURE
            | ClimateEntityFeature.TURN_OFF
            | ClimateEntityFeature.TURN_ON
        )

        # Flatten UUID values - some might be lists (e.g., "temperatures")
        self._all_uuids = set()
        for value in self._stateAttribUuids.values():
            if isinstance(value, list):
                self._all_uuids.update(value)
            else:
                self._all_uuids.add(value)

        self._attr_device_info = get_or_create_device(
            self.unique_id, self.name, self.type, self.room
        )

    async def event_handler(self, event):
        update = False

        for key in self._all_uuids & event.data.keys():
            self._stateAttribValues[key] = event.data[key]
            update = True

        if update:
            self.async_write_ha_state()

    def get_state_value(self, name):
        uuid = self._stateAttribUuids.get(name)
        if isinstance(uuid, list):
            # For "temperatures" which is a list of UUIDs
            return [
                self._stateAttribValues.get(u)
                for u in uuid
                if u in self._stateAttribValues
            ]
        return (
            self._stateAttribValues[uuid]
            if uuid and uuid in self._stateAttribValues
            else None
        )

    @property
    def extra_state_attributes(self):
        """Return device specific state attributes."""
        return {
            **self._attr_extra_state_attributes,
            "mode": self.get_state_value("mode"),
            "override": self.get_state_value("override"),
            "open_window": self.get_state_value("openWindow"),
            "curr_heat_temp_ix": self.get_state_value("currHeatTempIx"),
            "curr_cool_temp_ix": self.get_state_value("currCoolTempIx"),
        }

    @property
    def current_temperature(self):
        """Return the current temperature."""
        return self.get_state_value("tempActual")

    @property
    def target_temperature(self) -> float | None:
        """Return the temperature we try to reach."""
        return self.get_state_value("tempTarget")

    def set_temperature(self, **kwargs):
        """Set new target temperature"""
        temp = kwargs.get("temperature")
        if temp is None:
            return

        mode = self.get_state_value("mode")
        temp_idx = self.get_state_value("currHeatTempIx")
        if mode == 2:  # Cooling mode
            cool_idx = self.get_state_value("currCoolTempIx")
            if cool_idx is not None:
                temp_idx = cool_idx

        if temp_idx is not None:
            self.hass.bus.fire(
                SENDDOMAIN,
                dict(
                    uuid=self.uuidAction,
                    value=f"setTemp/{int(temp_idx)}/{temp}",
                ),
            )
            self.schedule_update_ha_state()

    @property
    def hvac_action(self) -> HVACAction | None:
        """Return the current HVAC action (heating, cooling)."""
        valve_heat = self.get_state_value("valveHeat")
        valve_cool = self.get_state_value("valveCool")

        if valve_heat and valve_heat > 0:
            return HVACAction.HEATING
        elif valve_cool and valve_cool > 0:
            return HVACAction.COOLING

        if self.get_state_value("isPreparing") == 1:
            return HVACAction.PREHEATING

        return HVACAction.IDLE

    @property
    def hvac_mode(self) -> HVACMode | None:
        """Return hvac operation mode."""
        mode = self.get_state_value("mode")

        if mode == 0:
            return HVACMode.AUTO
        elif mode == 1:
            return HVACMode.HEAT
        elif mode == 2:
            return HVACMode.COOL
        elif mode == 3:
            return HVACMode.HEAT_COOL
        else:
            return HVACMode.OFF

    @property
    def hvac_modes(self) -> list[HVACMode]:
        """Return the list of available hvac operation modes."""
        return [
            HVACMode.OFF,
            HVACMode.AUTO,
            HVACMode.HEAT,
            HVACMode.COOL,
            HVACMode.HEAT_COOL,
        ]

    @property
    def temperature_unit(self) -> str:
        """Return the unit of measurement used by the platform."""
        format_str = self.details.get("format")

        if format_str is None:
            return UnitOfTemperature.CELSIUS

        if "°F" in format_str or "F" in format_str:
            return UnitOfTemperature.FAHRENHEIT

        if "°C" in format_str or "C" in format_str:
            return UnitOfTemperature.CELSIUS

        return UnitOfTemperature.CELSIUS

    @property
    def target_temperature_step(self) -> float | None:
        """Return the supported step of target temperature."""
        return 0.5

    @property
    def min_temp(self) -> float:
        """Return the minimum temperature."""
        return 7.0

    @property
    def max_temp(self) -> float:
        """Return the maximum temperature."""
        return 35.0

    def set_hvac_mode(self, hvac_mode: str):
        """Set new target hvac mode."""
        mode_map = {
            HVACMode.OFF: 4,
            HVACMode.AUTO: 0,
            HVACMode.HEAT: 1,
            HVACMode.COOL: 2,
            HVACMode.HEAT_COOL: 3,
        }

        target_mode = mode_map.get(hvac_mode, 0)

        self.hass.bus.fire(
            SENDDOMAIN,
            dict(uuid=self.uuidAction, value=f"setMode/{target_mode}"),
        )

        self.schedule_update_ha_state()


class LoxoneRoomControllerV2(LoxoneEntity, ClimateEntity, ABC):
    """Loxone room controller"""

    _attr_supported_features = (
        ClimateEntityFeature.PRESET_MODE
        | ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.TURN_OFF
        | ClimateEntityFeature.TURN_ON
    )

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.hass = kwargs["hass"]
        self._autoMode = kwargs[CONF_HVAC_AUTO_MODE]
        self._stateAttribUuids = kwargs["states"]
        self._stateAttribValues = {}
        self.type = "RoomControllerV2"
        self._modeList = kwargs["details"]["timerModes"]

        self._attr_device_info = get_or_create_device(
            self.unique_id, self.name, self.type, self.room
        )

    def get_mode_from_id(self, mode_id):
        for mode in self._modeList:
            if mode["id"] == mode_id:
                return mode["name"]

    async def event_handler(self, event):
        update = False

        for key in set(self._stateAttribUuids.values()) & event.data.keys():
            self._stateAttribValues[key] = event.data[key]
            update = True

        if update:
            self.schedule_update_ha_state()

    def get_state_value(self, name):
        uuid = self._stateAttribUuids[name]
        return (
            self._stateAttribValues[uuid] if uuid in self._stateAttribValues else None
        )

    @property
    def extra_state_attributes(self):
        return {
            **self._attr_extra_state_attributes,
            "is_overridden": self.is_overridden,
        }

    @property
    def is_overridden(self) -> bool:
        # Needed because loxone uses these variables names. Simply workaround define it also here.
        true = True
        false = False
        null = None
        _override_entries = self.get_state_value("overrideEntries")
        if _override_entries:
            _override_entries = eval(_override_entries)
            if isinstance(_override_entries, list) and len(_override_entries) > 0:
                return True
        return False

    @property
    def current_temperature(self):
        """Return the current temperature."""
        return self.get_state_value("tempActual")

    def set_temperature(self, **kwargs):
        """Set new target temperature"""
        if (
            self.get_state_value("operatingMode") > 2
        ):  # Set manual temp if any of the manual modes selected
            self.hass.bus.fire(
                SENDDOMAIN,
                dict(
                    uuid=self.uuidAction,
                    value=f'setManualTemperature/{kwargs["temperature"]}',
                ),
            )
        else:  # Set comfort temp offset otherwise
            new_offset = kwargs["temperature"] - self.get_state_value(
                "comfortTemperature"
            )
            self.hass.bus.fire(
                SENDDOMAIN,
                dict(uuid=self.uuidAction, value=f"setComfortModeTemp/{new_offset}"),
            )

    @property
    def hvac_action(self) -> HVACAction | None:
        """Return the current HVAC action (heating, cooling)."""
        if self.get_state_value("prepareState") == 1:
            return HVACAction.PREHEATING
        return None

    @property
    def hvac_mode(self) -> HVACMode | None:
        return OPMODES[self.get_state_value("operatingMode")]

    @property
    def hvac_modes(self) -> list[HVACMode]:
        return [
            HVACMode.AUTO,
            HVACMode.HEAT,
            HVACMode.HEAT_COOL,
            HVACMode.COOL,
            HVACMode.OFF,
        ]

    @property
    def temperature_unit(self) -> str:
        """Return the unit of measurement used by the platform."""
        format_str = self.details.get("format")

        if format_str is None:
            return UnitOfTemperature.CELSIUS

        if "°F" in format_str or "F" in format_str:
            return UnitOfTemperature.FAHRENHEIT

        if "°C" in format_str or "C" in format_str:
            return UnitOfTemperature.CELSIUS

        return UnitOfTemperature.CELSIUS

    @property
    def target_temperature(self) -> float | None:
        return self.get_state_value("tempTarget")

    @property
    def target_temperature_step(self) -> float | None:
        return 0.5

    @property
    def preset_mode(self):
        return self.get_mode_from_id(self.get_state_value("activeMode"))

    @property
    def preset_modes(self):
        return [mode["name"] for mode in self._modeList]

    def set_hvac_mode(self, hvac_mode: str):
        """Set new target hvac mode."""
        target_mode = (
            self._autoMode if hvac_mode == HVACMode.AUTO else OPMODETOLOXONE[hvac_mode]
        )

        self.hass.bus.fire(
            SENDDOMAIN,
            dict(uuid=self.uuidAction, value=f"setOperatingMode/{target_mode}"),
        )

        self.schedule_update_ha_state()

    def set_preset_mode(self, preset_mode: str):
        """Set new preset mode."""
        mode_id = next(
            (mode["id"] for mode in self._modeList if mode["name"] == preset_mode), None
        )
        if mode_id is not None:
            self.hass.bus.fire(
                SENDDOMAIN, dict(uuid=self.uuidAction, value=f"override/{mode_id}")
            )
            self.schedule_update_ha_state()


# ------------------ AC CONTROL --------------------------------------------------------
class LoxoneAcControl(LoxoneEntity, ClimateEntity, ABC):
    """Representation of a ACControl Loxone device."""

    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.FAN_MODE
        | ClimateEntityFeature.SWING_MODE
        | ClimateEntityFeature.TURN_OFF
        | ClimateEntityFeature.TURN_ON
    )

    def __init__(self, **kwargs):
        _LOGGER.debug(f"Input AcControl: {kwargs}")
        super().__init__(**kwargs)
        self.hass = kwargs["hass"]

        self._stateAttribUuids = kwargs["states"]
        self._stateAttribValues = {}
        self.type = "AcControl"
        self._attr_device_info = get_or_create_device(
            self.unique_id, self.name, self.type, self.room
        )

    async def event_handler(self, event):
        update = False

        for key in set(self._stateAttribUuids.values()) & event.data.keys():
            self._stateAttribValues[key] = event.data[key]
            update = True

        if update:
            self.schedule_update_ha_state()

    def get_state_value(self, name):
        uuid = self._stateAttribUuids[name]
        return (
            self._stateAttribValues[uuid] if uuid in self._stateAttribValues else None
        )

    @property
    def extra_state_attributes(self):
        return {
            **self._attr_extra_state_attributes,
            "device_type": self.type,
        }

    @property
    def current_temperature(self):
        """Return the current temperature."""
        return self.get_state_value("temperature")

    def set_temperature(self, **kwargs):
        """Set new target temperature"""
        self.hass.bus.fire(
            SENDDOMAIN,
            dict(
                uuid=self.uuidAction,
                value=f'setTarget/{kwargs["targetTemperature"]}',
            ),
        )

    @property
    def hvac_mode(self) -> HVACMode | None:
        if self.get_state_value("status"):
            if self.get_state_value("mode") == 2:
                return HVACMode.HEAT
            elif self.get_state_value("mode") == 3:
                return HVACMode.COOL
            elif self.get_state_value("mode") == 4:
                return HVACMode.DRY
            elif self.get_state_value("mode") == 5:
                return HVACMode.FAN_ONLY
            else:
                return HVACMode.AUTO
        return HVACMode.OFF

    def set_hvac_mode(self, hvac_mode):
        """Set new target hvac mode."""
        mode = 1
        match hvac_mode:
            case HVACMode.HEAT:
                mode = 2
            case HVACMode.COOL:
                mode = 3
            case HVACMode.DRY:
                mode = 4
            case HVACMode.FAN_ONLY:
                mode = 5

        self.hass.bus.fire(
            SENDDOMAIN,
            dict(
                uuid=self.uuidAction,
                value="off" if hvac_mode == HVACMode.OFF else "on",
            ),
        )

        self.hass.bus.fire(
            SENDDOMAIN,
            dict(
                uuid=self.uuidAction,
                value=f"setMode/{mode}",
            ),
        )

    @property
    def hvac_modes(self) -> list[HVACMode]:
        return [HVACMode.OFF, HVACMode.HEAT, HVACMode.COOL, HVACMode.DRY, HVACMode.FAN_ONLY, HVACMode.AUTO]

    @property
    def temperature_unit(self) -> str:
        if "format" in self.details:
            if self.details["format"].find("°"):
                return UnitOfTemperature.CELSIUS
            return UnitOfTemperature.FAHRENHEIT
        return UnitOfTemperature.CELSIUS

    @property
    def target_temperature(self) -> float | None:
        return self.get_state_value("targetTemperature")

    @property
    def target_temperature_step(self) -> float | None:
        return 0.5

    @property
    def fan_mode(self) -> str | None:
        if self.get_state_value("fanspeeds") is not None:
            modes = json.loads(self.get_state_value("fanspeeds"))
            for mode in modes:
                if self.get_state_value("fan") == mode["id"]:
                    return mode["name"]
        return "Auto"

    def set_fan_mode(self, fan_mode):
        """Set new target fan mode."""
        self.hass.bus.fire(
            SENDDOMAIN,
            dict(
                uuid=self.uuidAction,
                value=f'setFan/{next((o["id"] for o in json.loads(self.get_state_value("fanspeeds")) if o["name"] == fan_mode), None)}',
            ),
        )

    @property
    def fan_modes(self) -> list[str]:
        if self.get_state_value("fanspeeds") is not None:
            return [o["name"] for o in json.loads(self.get_state_value("fanspeeds"))]
        return []

    @property
    def swing_mode(self) -> str | None:
        if self.get_state_value("airflows") is not None:
            modes = json.loads(self.get_state_value("airflows"))
            for mode in modes:
                if self.get_state_value("ventMode") == mode["id"]:
                    return mode["name"]
        return "Auto"

    def set_swing_mode(self, swing_mode):
        """Set new target swing mode."""
        self.hass.bus.fire(
            SENDDOMAIN,
            dict(
                uuid=self.uuidAction,
                value=f'setAirDir/{next((o["id"] for o in json.loads(self.get_state_value("airflows")) if o["name"] == swing_mode), None)}',
            ),
        )

    @property
    def swing_modes(self) -> list[str]:
        if self.get_state_value("airflows") is not None:
            return [o["name"] for o in json.loads(self.get_state_value("airflows"))]
        return []


# ------------------ AIRZONE ZONES -----------------------------------------------------
class LoxoneAirzoneZone(ClimateEntity):
    """One Airzone zone: raw Loxone blocks (damper switch + setpoint + temp sensor).

    Airzone systems expose each zone via separate Loxone blocks rather than
    a native AcControl block. This entity groups those blocks into a proper
    HA climate entity.

    The master zone also controls the global AC operating mode (cool/heat/fan/dry).
    Non-master zones control only their damper and setpoint; they inherit the
    global mode for display purposes.
    """

    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_hvac_modes = [
        HVACMode.OFF,
        HVACMode.COOL,
        HVACMode.HEAT,
        HVACMode.FAN_ONLY,
        HVACMode.DRY,
    ]

    def __init__(self, hass: HomeAssistant, zone_cfg: dict) -> None:
        self.hass = hass
        self._attr_name = zone_cfg["name"]
        self._attr_unique_id = zone_cfg["unique_id"]
        self._room = zone_cfg["room"]
        self._is_master = zone_cfg["is_master"]

        self._temp_uuid = zone_cfg["temperature_uuid"]
        self._setpoint_uuid = zone_cfg["setpoint_uuid"]
        self._switch_uuid = zone_cfg["switch_uuid"]

        self._attr_min_temp = zone_cfg["setpoint_min"]
        self._attr_max_temp = zone_cfg["setpoint_max"]
        self._attr_target_temperature_step = zone_cfg["setpoint_step"]

        self._attr_supported_features = (
            ClimateEntityFeature.TARGET_TEMPERATURE
            | ClimateEntityFeature.TURN_OFF
            | ClimateEntityFeature.TURN_ON
        )

        self._current_temp: float | None = None
        self._target_temp: float | None = None
        self._switch_on: bool = False
        self._mode_value: int = 0

        self._attr_device_info = get_or_create_device(
            zone_cfg["unique_id"], zone_cfg["name"], "AirzoneZone", zone_cfg["room"]
        )
        self._attr_extra_state_attributes = {
            "platform": "loxone",
            "room": zone_cfg["room"],
            "is_master": self._is_master,
        }

        self._watched_uuids = {
            self._temp_uuid,
            self._setpoint_uuid,
            self._switch_uuid,
            AIRZONE_GLOBAL_MODE_UUID,
        }
        self._listener = None

    async def async_added_to_hass(self) -> None:
        self._listener = self.hass.bus.async_listen(EVENT, self._handle_event)

    async def async_will_remove_from_hass(self) -> None:
        if self._listener:
            self._listener()
            self._listener = None

    async def _handle_event(self, event) -> None:
        updated = False
        data: dict = event.data
        relevant = self._watched_uuids & data.keys()
        if not relevant:
            return

        if self._temp_uuid in data:
            val = data[self._temp_uuid]
            self._current_temp = float(val) if val is not None else None
            updated = True

        if self._setpoint_uuid in data:
            val = data[self._setpoint_uuid]
            self._target_temp = float(val) if val is not None else None
            updated = True

        if self._switch_uuid in data:
            self._switch_on = bool(data[self._switch_uuid])
            updated = True

        if AIRZONE_GLOBAL_MODE_UUID in data:
            self._mode_value = int(data[AIRZONE_GLOBAL_MODE_UUID])
            updated = True

        if updated:
            self.async_write_ha_state()

    @property
    def current_temperature(self) -> float | None:
        return self._current_temp

    @property
    def target_temperature(self) -> float | None:
        return self._target_temp

    @property
    def hvac_mode(self) -> HVACMode:
        if not self._switch_on:
            return HVACMode.OFF
        return {
            1: HVACMode.COOL,
            3: HVACMode.FAN_ONLY,
            5: HVACMode.HEAT,
            6: HVACMode.DRY,
        }.get(self._mode_value, HVACMode.COOL)

    async def async_set_temperature(self, **kwargs) -> None:
        temp = kwargs.get("temperature")
        if temp is None:
            return
        self.hass.bus.fire(SENDDOMAIN, {"uuid": self._setpoint_uuid, "value": temp})

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        if hvac_mode == HVACMode.OFF:
            self.hass.bus.fire(SENDDOMAIN, {"uuid": self._switch_uuid, "value": "off"})
        else:
            self.hass.bus.fire(SENDDOMAIN, {"uuid": self._switch_uuid, "value": "on"})
            if self._is_master:
                mode_val = AIRZONE_MODE_TO_VALUE.get(hvac_mode.value, 1)
                self.hass.bus.fire(
                    SENDDOMAIN,
                    {"uuid": AIRZONE_GLOBAL_MODE_UUID, "value": mode_val},
                )
