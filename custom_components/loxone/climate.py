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
        AIRZONE_ZONE_OFFSET,
        AIRZONE_ZONES,
    )
    _HAS_AIRZONE = True
except ImportError:
    AIRZONE_ZONES = []
    _HAS_AIRZONE = False

_LOGGER = logging.getLogger(__name__)

# Shared (hass.data) key for the last non-Stop Airzone Radio mode value seen by
# any zone or the Whole House AC entity. Lets turning a zone back on restore
# whatever mode was active before the system was stopped, instead of opening
# a damper into a stopped system.
DATA_LAST_ACTIVE_AIRZONE_MODE = "loxone_last_active_airzone_mode"

# Shared (hass.data) key tracking each zone's current on/off switch state, keyed
# by zone unique_id. Lets turning off the last remaining zone also stop the
# global Radio, so "Operation Mode" doesn't keep showing Cool/Heat/etc. when
# nothing is actually running.
DATA_ZONE_SWITCH_STATES = "loxone_zone_switch_states"

# Shared (hass.data) key: set of zone unique_ids that were on the last time
# any zone was on. Lets reactivating a mode from Stop (with all zones off)
# automatically restore whichever zones were last running, instead of
# silently doing nothing (no dampers open) or requiring the user to manually
# re-pick zones every time.
DATA_LAST_ACTIVE_ZONES = "loxone_last_active_zones"


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
    if target_uuid in controls:
        return controls[target_uuid]
    for ctrl in controls.values():
        if not isinstance(ctrl, dict):
            continue
        if ctrl.get("uuidAction") == target_uuid:
            return ctrl
        sub = ctrl.get("subControls", {})
        if sub:
            result = _find_control(sub, target_uuid)
            if result:
                return result
    return {}


def _state_uuid(controls: dict, command_uuid: str, state_key: str) -> str:
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


def _airzone_mode_from_value(val: int) -> HVACMode:
    """Map a Radio activeOutput Modbus register value to an HVACMode.

    Register values mirror Modbus, not sequential output indices:
      0   → Stop (handled upstream as OFF)
      1   → Cool
      3   → Fan only
      5   → Heat (standard encoding)
      6   → Dry
      >6  → Heat (alternative Airzone/Modbus encoding, e.g. 258)
    """
    if val == 1:
        return HVACMode.COOL
    if val == 3:
        return HVACMode.FAN_ONLY
    if val == 6:
        return HVACMode.DRY
    if val == 5 or val > 6:
        return HVACMode.HEAT
    return HVACMode.COOL


# Entity providing the real outdoor temperature reading, used by the Fan-only
# half-degree shift direction (Airzone Sleep-style whole/half tracking, see
# LoxoneAirzoneZone._handle_event).
OUTDOOR_TEMPERATURE_ENTITY_ID = "sensor.loxone_outdoor_temperature_outdoor_temperature"


def _is_half_degree(value: float) -> bool:
    """Return True if value's fractional part is .5 (robust to float error)."""
    return round(value * 2) % 2 == 1


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
        climate.update({"hass": hass, CONF_HVAC_AUTO_MODE: 0})
        entities.append(LoxoneRoomControllerV2(**climate))

    for climate in get_all(loxconfig, "IRoomController"):
        climate = add_room_and_cat_to_value_values(loxconfig, climate)
        climate.update({"hass": hass, CONF_HVAC_AUTO_MODE: 0})
        entities.append(LoxoneRoomController(**climate))

    for accontrol in get_all(loxconfig, "AcControl"):
        accontrol = add_room_and_cat_to_value_values(loxconfig, accontrol)
        accontrol.update({"hass": hass})
        entities.append(LoxoneAcControl(**accontrol))

    if _HAS_AIRZONE:
        controls = loxconfig.get("controls", {})

        mode_state_uuid = _state_uuid(controls, AIRZONE_GLOBAL_MODE_UUID, "activeOutput")
        master_setpoint_state_uuid = _state_uuid(controls, AIRZONE_MASTER_SETPOINT_UUID, "value")

        master_zone_cfg = next(z for z in AIRZONE_ZONES if z["is_master"])
        master_temp_state_uuid = _state_uuid(controls, master_zone_cfg["temperature_uuid"], "value")
        entities.append(LoxoneAirzoneGlobalMode(
            hass,
            mode_state_uuid=mode_state_uuid,
            temp_state_uuid=master_temp_state_uuid,
            setpoint_state_uuid=master_setpoint_state_uuid,
        ))

        for zone_cfg in AIRZONE_ZONES:
            temp_state_uuid = _state_uuid(controls, zone_cfg["temperature_uuid"], "value")
            setpoint_state_uuid = _state_uuid(controls, zone_cfg["setpoint_uuid"], "value")
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
        if not self.enabled:
            return
        update = False
        for key in self._all_uuids & event.data.keys():
            self._stateAttribValues[key] = event.data[key]
            update = True
        if update:
            self.async_write_ha_state()

    def get_state_value(self, name):
        uuid = self._stateAttribUuids.get(name)
        if isinstance(uuid, list):
            return [self._stateAttribValues.get(u) for u in uuid if u in self._stateAttribValues]
        return self._stateAttribValues[uuid] if uuid and uuid in self._stateAttribValues else None

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

    async def async_turn_on(self) -> None:
        self.hass.bus.fire(
            SENDDOMAIN, dict(uuid=self.uuidAction, value="setMode/0")
        )
        self.schedule_update_ha_state()

    async def async_turn_off(self) -> None:
        self.hass.bus.fire(
            SENDDOMAIN, dict(uuid=self.uuidAction, value="setMode/4")
        )
        self.schedule_update_ha_state()

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
        if not self.enabled:
            return
        update = False
        for key in set(self._stateAttribUuids.values()) & event.data.keys():
            self._stateAttribValues[key] = event.data[key]
            update = True
        if update:
            self.schedule_update_ha_state()

    def get_state_value(self, name):
        uuid = self._stateAttribUuids[name]
        return self._stateAttribValues[uuid] if uuid in self._stateAttribValues else None

    @property
    def extra_state_attributes(self):
        return {**self._attr_extra_state_attributes, "is_overridden": self.is_overridden}

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

    async def async_turn_on(self) -> None:
        self.hass.bus.fire(
            SENDDOMAIN, dict(uuid=self.uuidAction, value="setOperatingMode/0")
        )
        self.schedule_update_ha_state()

    async def async_turn_off(self) -> None:
        self.hass.bus.fire(
            SENDDOMAIN, dict(uuid=self.uuidAction, value="setOperatingMode/-1")
        )
        self.schedule_update_ha_state()

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
        if not self.enabled:
            return
        update = False
        for key in set(self._stateAttribUuids.values()) & event.data.keys():
            self._stateAttribValues[key] = event.data[key]
            update = True
        if update:
            self.schedule_update_ha_state()

    def get_state_value(self, name):
        uuid = self._stateAttribUuids[name]
        return self._stateAttribValues[uuid] if uuid in self._stateAttribValues else None

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

    OFF closes this zone's damper only — other zones are unaffected.
    Selecting any mode opens this zone's damper and sets the global AC mode.
    Non-master zones track the master setpoint ± AIRZONE_ZONE_OFFSET for slider limits.
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

        self._switch_uuid = zone_cfg["switch_uuid"]
        self._setpoint_uuid = zone_cfg["setpoint_uuid"]

        self._temp_state_uuid = zone_cfg.get("temp_state_uuid", zone_cfg["temperature_uuid"])
        self._setpoint_state_uuid = zone_cfg.get("setpoint_state_uuid", zone_cfg["setpoint_uuid"])
        self._switch_state_uuid = zone_cfg.get("switch_state_uuid", zone_cfg["switch_uuid"])
        self._mode_state_uuid = zone_cfg.get("mode_state_uuid", AIRZONE_GLOBAL_MODE_UUID)
        self._master_setpoint_state_uuid = zone_cfg.get(
            "master_setpoint_state_uuid", AIRZONE_MASTER_SETPOINT_UUID
        )

        self._abs_min = zone_cfg["setpoint_min"]
        self._abs_max = zone_cfg["setpoint_max"]
        # Base step size (1.0 for non-master zones, 0.5 for master). Non-master
        # zones temporarily switch to a 0.5 step whenever the master setpoint
        # is currently sitting on a half-degree value — see the
        # target_temperature_step property below.
        self._configured_step = zone_cfg["setpoint_step"]

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

        # Tracks whether the master setpoint was last seen at a half-degree
        # value, so we can detect whole<->half *transitions* (not just any
        # master change) and shift/restore this zone's own setpoint to match.
        self._master_setpoint_is_half: bool | None = None
        self._pre_half_shift_temp: float | None = None

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
            self.hass.data.setdefault(DATA_ZONE_SWITCH_STATES, {})[
                self._attr_unique_id
            ] = self._switch_on
            if self._switch_on:
                self.hass.data.setdefault(DATA_LAST_ACTIVE_ZONES, set()).add(
                    self._attr_unique_id
                )
            else:
                self._stop_system_if_all_zones_off()

        if self._mode_state_uuid in data:
            self._mode_value = int(data[self._mode_state_uuid])
            updated = True
            if self._mode_value != 0:
                self.hass.data[DATA_LAST_ACTIVE_AIRZONE_MODE] = self._mode_value

        if self._master_setpoint_state_uuid in data:
            val = data[self._master_setpoint_state_uuid]
            self._master_setpoint = float(val) if val is not None else None
            updated = True
            if not self._is_master and self._master_setpoint is not None:
                self._handle_master_half_transition()

        if updated:
            self.async_write_ha_state()

    def _half_shift_delta(self) -> float:
        """Direction/size of the whole<->half tracking shift for this zone.

        Heat always shifts the zone up; Cool/Dry always shift it down.
        Fan-only depends on whether it's warmer or colder than 20°C outside
        (mirrors whichever of Cool/Heat is more appropriate for the weather).
        """
        mode = _airzone_mode_from_value(self._mode_value)
        if mode == HVACMode.HEAT:
            return 0.5
        if mode in (HVACMode.COOL, HVACMode.DRY):
            return -0.5
        if mode == HVACMode.FAN_ONLY:
            outdoor_state = self.hass.states.get(OUTDOOR_TEMPERATURE_ENTITY_ID)
            try:
                outdoor_temp = float(outdoor_state.state) if outdoor_state else None
            except (ValueError, TypeError):
                outdoor_temp = None
            if outdoor_temp is not None and outdoor_temp > 20:
                return -0.5
            return 0.5
        return 0.0

    def _handle_master_half_transition(self) -> None:
        """Shift/restore this zone's setpoint when master crosses a whole<->half boundary.

        Only fires on the transition itself (whole->half or half->whole) —
        arbitrary whole-to-whole or half-to-half master moves leave this
        zone's setpoint untouched.
        """
        was_half = self._master_setpoint_is_half
        is_half = _is_half_degree(self._master_setpoint)
        self._master_setpoint_is_half = is_half

        if was_half is None or was_half == is_half or self._target_temp is None:
            return

        if is_half:
            # Whole -> half: remember the current whole-number value and shift.
            self._pre_half_shift_temp = self._target_temp
            new_temp = self._target_temp + self._half_shift_delta()
        else:
            # Half -> whole: restore the remembered value (or hold current if
            # we never captured one, e.g. entity started up mid-shift).
            new_temp = (
                self._pre_half_shift_temp
                if self._pre_half_shift_temp is not None
                else self._target_temp
            )
            self._pre_half_shift_temp = None

        new_temp = max(self.min_temp, min(self.max_temp, new_temp))
        if new_temp != self._target_temp:
            self.hass.bus.fire(SENDDOMAIN, {"uuid": self._setpoint_uuid, "value": new_temp})
            self._target_temp = new_temp

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
    def target_temperature_step(self) -> float:
        if not self._is_master and self._master_setpoint_is_half:
            return 0.5
        return self._configured_step

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
        return _airzone_mode_from_value(self._mode_value)

    async def async_turn_on(self) -> None:
        self.hass.bus.fire(SENDDOMAIN, {"uuid": self._switch_uuid, "value": "on"})
        self._switch_on = True
        self.hass.data.setdefault(DATA_ZONE_SWITCH_STATES, {})[
            self._attr_unique_id
        ] = True
        self.hass.data.setdefault(DATA_LAST_ACTIVE_ZONES, set()).add(
            self._attr_unique_id
        )
        if self._mode_value == 0:
            # System is currently stopped — opening this damper alone would do
            # nothing, so restore whichever mode was last active.
            last_mode = self.hass.data.get(DATA_LAST_ACTIVE_AIRZONE_MODE, 1)
            self.hass.bus.fire(
                SENDDOMAIN, {"uuid": AIRZONE_GLOBAL_MODE_UUID, "value": last_mode}
            )
            self._mode_value = last_mode
        self.async_write_ha_state()

    async def async_turn_off(self) -> None:
        self.hass.bus.fire(SENDDOMAIN, {"uuid": self._switch_uuid, "value": "off"})
        self._switch_on = False
        self.hass.data.setdefault(DATA_ZONE_SWITCH_STATES, {})[
            self._attr_unique_id
        ] = False
        self._stop_system_if_all_zones_off()
        self.async_write_ha_state()

    def _stop_system_if_all_zones_off(self) -> None:
        """Stop the global Radio once every zone is confirmed off.

        Runs from both the HA-initiated turn_off path and from Loxone-reported
        switch-state events (e.g. the initial state sync on startup), since
        either can be the source of the last zone turning off.
        """
        zone_states = self.hass.data.setdefault(DATA_ZONE_SWITCH_STATES, {})
        all_zone_ids = {z["unique_id"] for z in AIRZONE_ZONES}
        all_zones_known_off = all_zone_ids <= zone_states.keys() and not any(
            zone_states[uid] for uid in all_zone_ids
        )
        if self._mode_value != 0 and all_zones_known_off:
            # Nothing is asking for air anymore — stop the whole system too
            # instead of leaving the Radio (and "Operation Mode") showing a
            # mode that's doing nothing.
            self.hass.bus.fire(SENDDOMAIN, {"uuid": AIRZONE_GLOBAL_MODE_UUID, "value": 0})
            self._mode_value = 0

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


# ------------------ WHOLE HOUSE AC ----------------------------------------------------
class LoxoneAirzoneGlobalMode(ClimateEntity):
    """Whole House AC — controls the global AC Mode Radio.

    Mode changes (Cool/Heat/Fan/Dry) fire to the Radio only — no damper involvement.
    OFF sends Radio=0 (Toshiba stop) AND closes all three zone dampers so the
    master zone cannot hold the mode active.
    Temperature and setpoint mirror the Master Suite zone for context.
    """

    _attr_name = "Whole House AC"
    _attr_unique_id = "airzone_whole_house"
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_hvac_modes = [
        HVACMode.OFF,
        HVACMode.COOL,
        HVACMode.HEAT,
        HVACMode.FAN_ONLY,
        HVACMode.DRY,
    ]
    _attr_supported_features = ClimateEntityFeature.TARGET_TEMPERATURE
    _attr_target_temperature_step = 0.5
    _attr_min_temp = 17.5
    _attr_max_temp = 26.5

    def __init__(
        self,
        hass: HomeAssistant,
        mode_state_uuid: str,
        temp_state_uuid: str,
        setpoint_state_uuid: str,
    ) -> None:
        self.hass = hass
        self._mode_state_uuid = mode_state_uuid
        self._temp_state_uuid = temp_state_uuid
        self._setpoint_state_uuid = setpoint_state_uuid

        self._mode_value: int = 0
        self._current_temp: float | None = None
        self._target_temp: float | None = None
        self._listener = None

        self._watched_uuids = {
            self._mode_state_uuid,
            self._temp_state_uuid,
            self._setpoint_state_uuid,
        }
        self._attr_device_info = get_or_create_device(
            "airzone_whole_house", "Whole House AC", "AirzoneGlobal", "System"
        )
        self._attr_extra_state_attributes = {
            "platform": "loxone",
            "mode_state_uuid": self._mode_state_uuid,
        }

    async def async_added_to_hass(self) -> None:
        self._listener = self.hass.bus.async_listen(EVENT, self._handle_event)

    async def async_will_remove_from_hass(self) -> None:
        if self._listener:
            self._listener()
            self._listener = None

    async def _handle_event(self, event) -> None:
        data: dict = event.data
        if not (self._watched_uuids & data.keys()):
            return
        updated = False

        if self._mode_state_uuid in data:
            self._mode_value = int(data[self._mode_state_uuid])
            updated = True
            if self._mode_value != 0:
                self.hass.data[DATA_LAST_ACTIVE_AIRZONE_MODE] = self._mode_value

        if self._temp_state_uuid in data:
            val = data[self._temp_state_uuid]
            self._current_temp = float(val) if val is not None else None
            updated = True

        if self._setpoint_state_uuid in data:
            val = data[self._setpoint_state_uuid]
            self._target_temp = float(val) if val is not None else None
            updated = True

        if updated:
            self.async_write_ha_state()

    @property
    def hvac_mode(self) -> HVACMode:
        if self._mode_value == 0:
            return HVACMode.OFF
        return _airzone_mode_from_value(self._mode_value)

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        if hvac_mode == HVACMode.OFF:
            self.hass.bus.fire(SENDDOMAIN, {"uuid": AIRZONE_GLOBAL_MODE_UUID, "value": 0})
            for zone in AIRZONE_ZONES:
                self.hass.bus.fire(SENDDOMAIN, {"uuid": zone["switch_uuid"], "value": "off"})
            self._mode_value = 0
        else:
            mode_val = AIRZONE_MODE_TO_VALUE.get(hvac_mode.value, 1)
            self.hass.bus.fire(SENDDOMAIN, {"uuid": AIRZONE_GLOBAL_MODE_UUID, "value": mode_val})
            self._mode_value = mode_val

            zone_states = self.hass.data.setdefault(DATA_ZONE_SWITCH_STATES, {})
            all_zone_ids = {z["unique_id"] for z in AIRZONE_ZONES}
            all_zones_currently_off = not any(
                zone_states.get(uid) for uid in all_zone_ids
            )
            if all_zones_currently_off:
                # Nothing is on to actually carry this mode — reopen whichever
                # zones were last running (or all of them, if we've never
                # seen any) instead of silently doing nothing.
                zones_to_restore = self.hass.data.get(DATA_LAST_ACTIVE_ZONES) or all_zone_ids
                for zone in AIRZONE_ZONES:
                    if zone["unique_id"] in zones_to_restore:
                        self.hass.bus.fire(
                            SENDDOMAIN, {"uuid": zone["switch_uuid"], "value": "on"}
                        )
        self.async_write_ha_state()

    @property
    def current_temperature(self) -> float | None:
        return self._current_temp

    @property
    def target_temperature(self) -> float | None:
        return self._target_temp

    async def async_set_temperature(self, **kwargs) -> None:
        temp = kwargs.get("temperature")
        if temp is None:
            return
        temp = max(self._attr_min_temp, min(self._attr_max_temp, float(temp)))
        self.hass.bus.fire(SENDDOMAIN, {"uuid": AIRZONE_MASTER_SETPOINT_UUID, "value": temp})
        self._target_temp = temp
        self.async_write_ha_state()
