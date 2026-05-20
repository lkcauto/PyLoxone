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
        AIRZONE_MASTER_SETPOINT_UUID,
        AIRZONE_MODE_TO_VALUE,
        AIRZONE_VALUE_TO_MODE,
        AIRZONE_ZONE_OFFSET,
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


def _find_control(controls: dict, target_uuid: str) -> dict:
    """Return the loxconfig control entry for target_uuid.

    Checks both the dict key (common case) and the uuidAction field
    (fallback) so lookup works regardless of how the JSON is structured.
    """
    if target_uuid in controls:
        return controls[target_uuid]
    for ctrl in controls.values():
        if ctrl.get("uuidAction") == target_uuid:
            return ctrl
    return {}


def _state_uuid(controls: dict, command_uuid: str, state_key: str) -> str:
    """Return the state output UUID for a block, falling back to command_uuid.

    Loxone block types and their state key names:
      InfoOnlyAnalog  → "value"
      ValueSelector   → "value"
      Switch          → "active"
      Radio           → "activeOutput"
    """
    ctrl = _find_control(controls, command_uuid)
    resolved = ctrl.get("states", {}).get(state_key)
    if resolved:
        _LOGGER.debug("Airzone UUID resolved: %s → %s (key=%s)", command_uuid, resolved, state_key)
        return resolved
    _LOGGER.warning(
        "Airzone: could not resolve state UUID for %s (key=%s); "
        "state updates from Loxone may not work for this block",
        command_uuid, state_key,
    )
    return command_uuid


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

    if _HAS_AIRZONE:
        controls = loxconfig.get("controls", {})

        # Resolve shared state UUIDs (same block for all zones)
        # Radio block: activeOutput carries the selected mode number
        mode_state_uuid = _state_uuid(controls, AIRZONE_GLOBAL_MODE_UUID, "activeOutput")
        # Master setpoint ValueSelector: value carries the current setpoint
        master_setpoint_state_uuid = _state_uuid(controls, AIRZONE_MASTER_SETPOINT_UUID, "value")

        for zone_cfg in AIRZONE_ZONES:
            # InfoOnlyAnalog temperature sensor: value carries the current reading
            temp_state_uuid = _state_uuid(controls, zone_cfg["temperature_uuid"], "value")
            # ValueSelector setpoint: value carries the current setpoint
            setpoint_state_uuid = _state_uuid(controls, zone_cfg["setpoint_uuid"], "value")
            # Switch damper block: active carries 0.0/1.0 on/off state
            switch_state_uuid = _state_uuid(controls, zone_cfg["switch_uuid"], "active")

            enriched_cfg = {
                **zone_cfg,
                "temp_state_uuid": temp_state_uuid,
                "setpoint_state_uuid": setpoint_state_uuid,
                "switch_state_uuid": switch_state_uuid,
                "mode_state_uuid": mode_state_uuid,
                "master_setpoint_state_uuid": master_setpoint_state_uuid,
            }
            entities.append(LoxoneAirzoneZone(hass, enriched_cfg))

    async_add_entities(entities)


class LoxoneRoomController(LoxoneEntity, ClimateEntity, ABC):
    """Loxone room controller (legacy, non-V2)"""

    def __init__(self, **kwargs):
        if "room" in kwargs and kwargs["room"]:
            kwargs["name"] = f"{kwargs['room']} Climate"

        super().__init__(**kwargs)
        self.hass = kwargs["hass"]
        self._autoMode = kwargs[CONF_HVAC_AUTO_MODE]
        self._stateAttribUuids = kwargs["states"]
        self._stateAttribValues = {}
        self.type = "RoomController"

        self._attr_supported_features = (
            ClimateEntityFeature.TARGET_TEMPERATURE
            | ClimateEntityFeature.TURN_OFF
            | ClimateEntityFeature.TURN_ON
        )

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
        return self.get_state_value("tempActual")

    @property
    def target_temperature(self) -> float | None:
        return self.get_state_value("tempTarget")

    def set_temperature(self, **kwargs):
        temp = kwargs.get("temperature")
        if temp is None:
            return

        mode = self.get_state_value("mode")
        temp_idx = self.get_state_value("currHeatTempIx")
        if mode == 2:
            cool_idx = self.get_state_value("currCoolTempIx")
            if cool_idx is not None:
                temp_idx = cool_idx

        if temp_idx is not None:
            self.hass.bus.fire(
                SENDDOMAIN,
                dict(uuid=self.uuidAction, value=f"setTemp/{int(temp_idx)}/{temp}"),
            )
            self.schedule_update_ha_state()

    @property
    def hvac_action(self) -> HVACAction | None:
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
        return [HVACMode.OFF, HVACMode.AUTO, HVACMode.HEAT, HVACMode.COOL, HVACMode.HEAT_COOL]

    @property
    def temperature_unit(self) -> str:
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
        return 0.5

    @property
    def min_temp(self) -> float:
        return 7.0

    @property
    def max_temp(self) -> float:
        return 35.0

    def set_hvac_mode(self, hvac_mode: str):
        mode_map = {
            HVACMode.OFF: 4,
            HVACMode.AUTO: 0,
            HVACMode.HEAT: 1,
            HVACMode.COOL: 2,
            HVACMode.HEAT_COOL: 3,
        }
        self.hass.bus.fire(
            SENDDOMAIN,
            dict(uuid=self.uuidAction, value=f"setMode/{mode_map.get(hvac_mode, 0)}"),
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
        return self.get_state_value("tempActual")

    def set_temperature(self, **kwargs):
        if self.get_state_value("operatingMode") > 2:
            self.hass.bus.fire(
                SENDDOMAIN,
                dict(uuid=self.uuidAction, value=f'setManualTemperature/{kwargs["temperature"]}'),
            )
        else:
            new_offset = kwargs["temperature"] - self.get_state_value("comfortTemperature")
            self.hass.bus.fire(
                SENDDOMAIN,
                dict(uuid=self.uuidAction, value=f"setComfortModeTemp/{new_offset}"),
            )

    @property
    def hvac_action(self) -> HVACAction | None:
        if self.get_state_value("prepareState") == 1:
            return HVACAction.PREHEATING
        return None

    @property
    def hvac_mode(self) -> HVACMode | None:
        return OPMODES[self.get_state_value("operatingMode")]

    @property
    def hvac_modes(self) -> list[HVACMode]:
        return [HVACMode.AUTO, HVACMode.HEAT, HVACMode.HEAT_COOL, HVACMode.COOL, HVACMode.OFF]

    @property
    def temperature_unit(self) -> str:
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
        target_mode = (
            self._autoMode if hvac_mode == HVACMode.AUTO else OPMODETOLOXONE[hvac_mode]
        )
        self.hass.bus.fire(
            SENDDOMAIN,
            dict(uuid=self.uuidAction, value=f"setOperatingMode/{target_mode}"),
        )
        self.schedule_update_ha_state()

    def set_preset_mode(self, preset_mode: str):
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
        return {**self._attr_extra_state_attributes, "device_type": self.type}

    @property
    def current_temperature(self):
        return self.get_state_value("temperature")

    def set_temperature(self, **kwargs):
        self.hass.bus.fire(
            SENDDOMAIN,
            dict(uuid=self.uuidAction, value=f'setTarget/{kwargs["targetTemperature"]}'),
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
            dict(uuid=self.uuidAction, value="off" if hvac_mode == HVACMode.OFF else "on"),
        )
        self.hass.bus.fire(
            SENDDOMAIN,
            dict(uuid=self.uuidAction, value=f"setMode/{mode}"),
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

    All zones control the global AC operating mode — Loxone virtualises the AC Mode
    Radio block to all zones, so any zone can change the global mode.
    Non-master zones control only their damper and setpoint; their min/max
    temperature range tracks the master setpoint ± AIRZONE_ZONE_OFFSET so that
    HA's UI slider always enforces the Airzone hardware constraint.

    Command UUIDs (uuidAction) are kept separately from state UUIDs (states dict)
    because Loxone uses different UUIDs for sending commands vs receiving state events.
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

        # Command UUIDs — used to send commands to Loxone (uuidAction)
        self._switch_uuid = zone_cfg["switch_uuid"]           # Switch damper on/off
        self._setpoint_uuid = zone_cfg["setpoint_uuid"]       # ValueSelector set value
        # AIRZONE_GLOBAL_MODE_UUID used directly when firing mode commands

        # State UUIDs — the block's states dict output UUIDs; what Loxone uses in WebSocket events
        self._temp_state_uuid = zone_cfg.get("temp_state_uuid", zone_cfg["temperature_uuid"])
        self._setpoint_state_uuid = zone_cfg.get("setpoint_state_uuid", zone_cfg["setpoint_uuid"])
        self._switch_state_uuid = zone_cfg.get("switch_state_uuid", zone_cfg["switch_uuid"])
        self._mode_state_uuid = zone_cfg.get("mode_state_uuid", AIRZONE_GLOBAL_MODE_UUID)
        self._master_setpoint_state_uuid = zone_cfg.get(
            "master_setpoint_state_uuid", AIRZONE_MASTER_SETPOINT_UUID
        )

        # Absolute hardware limits for this zone
        self._abs_min = zone_cfg["setpoint_min"]
        self._abs_max = zone_cfg["setpoint_max"]
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
        self._master_setpoint: float | None = None

        self._attr_device_info = get_or_create_device(
            zone_cfg["unique_id"], zone_cfg["name"], "AirzoneZone", zone_cfg["room"]
        )
        self._attr_extra_state_attributes = {
            "platform": "loxone",
            "room": zone_cfg["room"],
            "is_master": self._is_master,
            "switch_state_uuid": self._switch_state_uuid,
            "mode_state_uuid": self._mode_state_uuid,
        }

        self._watched_uuids = {
            self._temp_state_uuid,
            self._setpoint_state_uuid,
            self._switch_state_uuid,
            self._mode_state_uuid,
            self._master_setpoint_state_uuid,
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
        if not (self._watched_uuids & data.keys()):
            return

        if self._temp_state_uuid in data:
            val = data[self._temp_state_uuid]
            self._current_temp = float(val) if val is not None else None
            updated = True

        if self._setpoint_state_uuid in data:
            val = data[self._setpoint_state_uuid]
            self._target_temp = float(val) if val is not None else None
            updated = True

        if self._switch_state_uuid in data:
            self._switch_on = bool(data[self._switch_state_uuid])
            updated = True

        if self._mode_state_uuid in data:
            self._mode_value = int(data[self._mode_state_uuid])
            updated = True

        if self._master_setpoint_state_uuid in data:
            val = data[self._master_setpoint_state_uuid]
            self._master_setpoint = float(val) if val is not None else None
            updated = True

        if updated:
            self.async_write_ha_state()

    @property
    def min_temp(self) -> float:
        if self._is_master or self._master_setpoint is None:
            return self._abs_min
        return max(self._abs_min, self._master_setpoint - AIRZONE_ZONE_OFFSET)

    @property
    def max_temp(self) -> float:
        if self._is_master or self._master_setpoint is None:
            return self._abs_max
        return min(self._abs_max, self._master_setpoint + AIRZONE_ZONE_OFFSET)

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
        temp = max(self.min_temp, min(self.max_temp, float(temp)))
        self.hass.bus.fire(SENDDOMAIN, {"uuid": self._setpoint_uuid, "value": temp})
        self._target_temp = temp
        self.async_write_ha_state()

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        if hvac_mode == HVACMode.OFF:
            self.hass.bus.fire(SENDDOMAIN, {"uuid": self._switch_uuid, "value": "off"})
            self._switch_on = False
        else:
            self.hass.bus.fire(SENDDOMAIN, {"uuid": self._switch_uuid, "value": "on"})
            self._switch_on = True
            mode_val = AIRZONE_MODE_TO_VALUE.get(hvac_mode.value, 1)
            self.hass.bus.fire(
                SENDDOMAIN,
                {"uuid": AIRZONE_GLOBAL_MODE_UUID, "value": mode_val},
            )
            self._mode_value = mode_val
        self.async_write_ha_state()
