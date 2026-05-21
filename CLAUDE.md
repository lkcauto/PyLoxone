# Loxone → Home Assistant Migration

## Architecture (non-negotiable)

- **Loxone is and stays the house brain.** All physical switches hardwired to Miniserver. All logic, scenes, schedules, TTS, safety stay in Loxone.
- **HA is the UI + intelligence layer.** Reads state via PyLoxone WebSocket. Adds AC automations, geofencing, energy monitoring, Fermax.
- **Nothing in Loxone Config gets changed.** All control is via the existing WebSocket API (UUID-based commands).
- **Development branch:** `claude/create-claude-md-QlfbO` on `lkcauto/pyloxone`

## Infrastructure

- Miniserver: `192.168.1.42:80`
- Mini PC (HA host, WSL2): `192.168.1.22:8123`
- HA binary: `~/homeassistant/venv/bin/hass --config ~/homeassistant/config`
- HA config: `~/homeassistant/config`
- PyLoxone source: `~/homeassistant/pyloxone-src`
- Deploy: `cp custom_components/loxone/climate.py ~/homeassistant/config/custom_components/loxone/climate.py`

## What's done

### `custom_components/loxone/airzone_config.py`
UUID map for the Airzone AC system:
- `AIRZONE_GLOBAL_MODE_UUID` = `20348b9e-02d8-61ca-ffff4a67748f279a` (Radio: 0=Stop, 1=Cool, 3=Fan, 5=Heat, 6=Dry)
- `AIRZONE_MASTER_SETPOINT_UUID` = `20347d11-0155-ea6f-ffff5b4be2d603d4`
- `AIRZONE_ZONE_OFFSET` = 3.0°C (non-master zones can go ±3° from master)
- Three zones: Master Suite (is_master=True), Living Room, Guest Suite

### `custom_components/loxone/climate.py`
Five climate entity classes:
1. `LoxoneRoomController` — legacy IRoomController, has async_turn_on/off
2. `LoxoneRoomControllerV2` — IRoomControllerV2, has async_turn_on/off
3. `LoxoneAcControl` — AcControl blocks
4. `LoxoneAirzoneZone` — one per Airzone zone (damper switch + setpoint + temp)
5. `LoxoneAirzoneGlobalMode` — "Whole House AC" controls the Radio only

**Key behaviour:**
- Zone OFF = close that zone's damper only (other zones unaffected)
- Zone mode change = open damper + set global Radio
- Whole House AC OFF = Radio=0 + close all 3 dampers
- Whole House AC mode change = Radio only, no damper involvement
- `_find_control()` searches recursively through `subControls` (Guest Suite switch is nested)

## Airzone UUID map

| Zone | Block | Type | UUID |
|------|-------|------|------|
| Master Suite | Temperature | InfoOnlyAnalog | `2034acd2-033e-466a-ffff4a67748f279a` |
| Master Suite | Setpoint | ValueSelector | `20347d11-0155-ea6f-ffff5b4be2d603d4` |
| Master Suite | Damper | Switch | `1d5faa58-03e4-d9af-ffff4a67748f279a` |
| Living Room | Temperature | InfoOnlyAnalog | `2034b111-03ba-7a35-ffff4a67748f279a` |
| Living Room | Setpoint | ValueSelector | `202e30d5-0260-c623-ffff4a67748f279a` |
| Living Room | Damper | Switch | `1d5fbfcc-0362-0cac-ffff4a67748f279a` |
| Guest Suite | Temperature | InfoOnlyAnalog | `2034ac18-0132-b918-ffff4a67748f279a` |
| Guest Suite | Setpoint | ValueSelector | `202e36f0-0086-d326-ffff3a8e542eca6a` |
| Guest Suite | Damper | Switch | `20af9482-02a2-f6f7-ffff4a67748f279a` |
| Global | AC Mode | Radio | `20348b9e-02d8-61ca-ffff4a67748f279a` |

## What PyLoxone auto-creates (no custom work needed)

| Loxone block | HA platform |
|---|---|
| InfoOnlyAnalog | sensor |
| InfoOnlyDigital | binary_sensor |
| PresenceDetector | binary_sensor (type=presence) |
| LightControllerV2 | light |
| Dimmer | light |
| Jalousie | cover |
| Switch / TimedSwitch | switch |
| ValueSelector | number |
| AudioZoneV2 | media_player |
| Alarm | alarm_control_panel |
| IRoomControllerV2 | climate |

## Known issues / active bugs

- Guest Suite IRoomControllerV2 is a dead block in Loxone (no linkedAcControls) — disable the entity in HA UI
- Fan entities (`loxone_room_ventilation_controller_*`) don't support turn_on/off — needs `LoxoneKeoliVentilation` custom class (UUIDs unknown, need LoxApp3.json)
- Bluetooth/HomeKit errors on startup are harmless — WSL2 has no Bluetooth hardware
- Miniserver 503 reconnects every few minutes are normal — PyLoxone handles reconnect automatically

## Next steps (in order)

1. **HACS install** — `pkill -f "venv/bin/hass" && wget -O - https://get.hacs.xyz | bash -` then restart HA, then Settings → Integrations → HACS
2. **e-distribución integration** — HACS → search `aedistribucion` → install → configure with CUPS `ES0031405430470026JH`, tariff 0.122 €/kWh flat (Octopus Relax 2.0TD)
3. **LoxApp3.json** — download from `http://192.168.1.42/data/LoxApp3.json` to get presence UUIDs, Keoli ventilation block UUIDs, intercom UUIDs
4. **LoxoneKeoliVentilation** — custom fan entity (same pattern as LoxoneAirzoneZone), UUIDs from step 3
5. **Presence automations** — fire immediately on binary_sensor off (hardware already waited 40-45 min); night exclusion 23:00-07:00; manual-on vs presence-restored distinction via input_boolean flags
6. **Loxone Video Intercom** — RTSP camera + binary_sensor ring + script door unlock
7. **Fermax Wibox** — research fermax_blue HACS integration for local API

## User context

- NIF: Y5076847W
- CUPS: ES0031405430470026JH
- Tariff: Octopus Relax 2.0TD — flat 0.122 €/kWh all periods
- Distribuidora: EDISTRIBUCIÓN REDES DIGITALES S.L.
- Has both e-distribución portal account and Octopus Energy Spain app
