# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dependencies (requires Home Assistant installed)
./scripts/setup

# Format code
./scripts/lint               # runs: ruff format .

# Run tests
pytest custom_components/loxone/pyloxone_api/tests/

# Run a single test
pytest custom_components/loxone/pyloxone_api/tests/test_discover.py::test_name
```

Ruff is configured via `ruff.toml` targeting Python 3.13 with a 120-character line length. The `ruff check --fix` pass is currently commented out in `scripts/lint`.

## Architecture

This is a **Home Assistant custom integration** (`custom_components/loxone/`) that bridges Loxone Miniservers to HA entities via a persistent WebSocket connection.

### Two-layer structure

**Integration layer** (`custom_components/loxone/*.py`) — HA-facing code:
- `__init__.py` — entry point: sets up the integration, registers services (`loxone.event_websocket_command`), and loads platforms
- `coordinator.py` — `LoxoneCoordinator` (extends `DataUpdateCoordinator`) owns the `LoxoneConnection` and `MiniServer`; it opens the WebSocket on first refresh and is the single source of truth for connection state
- `miniserver.py` — `MiniServer` dataclass wrapping the raw `LoxApp3.json` structure file; accessed via `get_miniserver_from_hass()`
- `config_flow.py` — UI-based config entry setup
- One file per HA platform: `light.py`, `cover.py`, `climate.py`, `alarm_control_panel.py`, `binary_sensor.py`, `sensor.py`, `switch.py`, `fan.py`, `media_player.py`, `number.py`, `text.py`, `button.py`, `scene.py`
- `lights/` sub-package handles the four distinct light types: `LightControllerV2` (moods), `Dimmer`, `ColorPickerV2`, and plain `Switch`

**API layer** (`custom_components/loxone/pyloxone_api/`) — protocol implementation:
- `connection.py` — `LoxoneBaseConnection` / `LoxoneConnection`: async WebSocket state machine that handles the full token-auth handshake (RSA public key → AES session key → encrypted credentials → token), keepalives, reconnect logic, and message dispatch
- `loxone_token.py` — token lifecycle (request, refresh, storage)
- `message.py` — binary message framing: parses the Loxone WebSocket message header and dispatches to `TextMessage`, `BinaryFile`, `LLResponse`, `Keepalive`
- `websocket_protocol.py` — low-level `LoxoneClientConnection` wrapping `websockets`
- `loxone_http_client.py` — HTTP client for the token handshake endpoints
- `discover.py` — UDP broadcast discovery of Miniservers on the LAN

### Data flow

1. `LoxoneCoordinator.async_config_entry_first_refresh()` opens `LoxoneConnection`
2. Connection performs the full auth handshake over WebSocket, downloads `LoxApp3.json`, then sends `jdev/sps/enablebinstatistic` to start receiving state events
3. Incoming state change events fire HA events (`loxone_event`) consumed by platform entities registered via `async_dispatcher_connect`
4. Commands flow back via `loxone.event_websocket_command` service → `LoxoneConnection` → `jdev/sps/io/{uuid}/{cmd}`

### Entity model

Each HA entity stores its Loxone `uuidAction`. On state events it checks whether the event UUID matches its own and updates state accordingly. All entities inherit from `helpers.py` base mixins. Loxone rooms map to HA areas if `ATTR_AREA_CREATE` is enabled.

### Config and token storage

Credentials come from the HA config entry. Token data is stored inside the config entry's `data` dict (persisted by HA), not in a file.
