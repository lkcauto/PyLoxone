"""
Airzone AC zone configuration for this Loxone installation.

Edit the UUIDs below to match your LoxApp3.json.
Find UUIDs in Loxone Config or via: lox ls -o json | python3 -c
  "import json,sys; d=json.load(sys.stdin); [print(v['type'], v['name'], k) for k,v in d['controls'].items()]"

Radio block output numbers mirror Modbus register values — they are NOT
sequential 1-based output indices. The Airzone Modbus register values are:
  0   = allOff / stop
  1   = Cool
  3   = Fan only
  5   = Heat (standard)
  6   = Dry
  258 = Heat (alternative Modbus encoding used on some Airzone firmware)

Rule for reading activeOutput back from Loxone:
  val == 1          → Cool
  val == 3          → Fan
  val == 6          → Dry
  val == 5 OR val > 6 → Heat  (covers both 5 and 258 and any future variants)
  val == 0          → Off / stop

Commands sent TO the Radio block always use 5 for heat (Loxone handles the
translation; we never need to send 258).

Non-master zone setpoints are always constrained to master_setpoint ± AIRZONE_ZONE_OFFSET.
This matches the Airzone hardware limit (3°C either side of master).
"""

# UUID of the Radio block that sets the global AC operating mode (shared by all zones)
AIRZONE_GLOBAL_MODE_UUID = "20348b9e-02d8-61ca-ffff4a67748f279a"

# UUID of the master zone setpoint — non-master zones track this for their min/max limits
AIRZONE_MASTER_SETPOINT_UUID = "20347d11-0155-ea6f-ffff5b4be2d603d4"

# How far a non-master zone setpoint can deviate from the master (Airzone hardware limit)
AIRZONE_ZONE_OFFSET = 3.0

# Values sent TO the Radio block (command direction only — always use 5 for heat)
AIRZONE_MODE_TO_VALUE: dict[str, int] = {
    "cool": 1,
    "heat": 5,
    "fan_only": 3,
    "dry": 6,
    "off": 0,
}

# One entry per Airzone zone.
# is_master=True zone controls the global AC mode and setpoint anchor.
# Non-master zone min/max are computed dynamically as master_setpoint ± AIRZONE_ZONE_OFFSET.
AIRZONE_ZONES: list[dict] = [
    {
        "name": "Master Suite AC",
        "unique_id": "airzone_master_suite",
        "room": "Master Suite",
        "is_master": True,
        "temperature_uuid": "2034acd2-033e-466a-ffff4a67748f279a",
        "setpoint_uuid": "20347d11-0155-ea6f-ffff5b4be2d603d4",
        "switch_uuid": "1d5faa58-03e4-d9af-ffff4a67748f279a",
        "setpoint_min": 16.0,
        "setpoint_max": 30.0,
        "setpoint_step": 0.5,
    },
    {
        "name": "Living Room AC",
        "unique_id": "airzone_living_room",
        "room": "Living Room",
        "is_master": False,
        "temperature_uuid": "2034b111-03ba-7a35-ffff4a67748f279a",
        "setpoint_uuid": "202e30d5-0260-c623-ffff4a67748f279a",
        "switch_uuid": "1d5fbfcc-0362-0cac-ffff4a67748f279a",
        "setpoint_min": 16.0,
        "setpoint_max": 30.0,
        "setpoint_step": 1.0,
    },
    {
        "name": "Guest Suite AC",
        "unique_id": "airzone_guest_suite",
        "room": "Guest Suite",
        "is_master": False,
        "temperature_uuid": "2034ac18-0132-b918-ffff4a67748f279a",
        "setpoint_uuid": "202e36f0-0086-d326-ffff3a8e542eca6a",
        "switch_uuid": "20af9482-02a2-f6f7-ffff4a67748f279a",
        "setpoint_min": 16.0,
        "setpoint_max": 30.0,
        "setpoint_step": 1.0,
    },
]
