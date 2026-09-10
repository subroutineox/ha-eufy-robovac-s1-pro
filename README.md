# [Eufy RoboVac S1 Pro - Home Assistant Integration](https://github.com/subroutineox/ha-eufy-robovac-s1-pro)

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)

> Fork of [tkoba1974/ha-eufy-robovac-s1-pro](https://github.com/tkoba1974/ha-eufy-robovac-s1-pro)
> adding **local room cleaning**, a fix for cleaning mode detection, and
> compatibility with Home Assistant 2026.9.

## Overview

This custom integration enables control of the Eufy RoboVac S1 Pro through Home Assistant.

Everything runs over the local network. The Eufy account is only used once, to
fetch the local key.

## Features

- 🤖 Start/Pause/Resume cleaning
- 🏠 Return to dock
- 🧹 **Clean specific rooms — locally, without the cloud**
- 🔋 Battery level monitoring
- 🗺️ Cleaning mode selection
- 💧 Water level adjustment
- 🎯 Suction power control
- 📊 Cleaning statistics display
- 🧴 Consumable wear sensors (brushes, filters, mop, tanks)

## Room cleaning

The direct room command (`START_SELECT_ROOMS_CLEAN` on DPS 152) is accepted by
this firmware but its room list is ignored — the robot just starts cleaning
somewhere. Scheduled tasks, on the other hand, honour the room list perfectly.

So this fork takes the scheduling route: `clean_rooms` writes a one-shot timer
set to the next full minute, and the robot starts it by itself. Fully local, and
the room selection actually works.

```yaml
action: eufy_robovac_s1_pro.clean_rooms
data:
  rooms: [3, 4]      # cleaned in this order
  delay: 120         # seconds, rounded up to the next full minute
```

```yaml
action: eufy_robovac_s1_pro.cancel_clean
```

`cancel_clean` removes the task again, so you can offer an "abort" button while
the countdown runs. It only deletes the task this integration created — your own
schedules stay untouched.

Passing `cycle` (a weekday bitmask, `127` = daily) creates a recurring schedule
instead of a one-shot task.

### Finding your room IDs

Room IDs are per-device and there is no way to read the room *names* locally —
they only travel over Eufy's encrypted P2P channel. But you can map them by
renaming: write a `RENAME_ROOM` request to DPS 170 for each ID and see which room
changes name in the app.

```yaml
action: eufy_robovac_s1_pro.write_dps
data:
  dps_id: "170"
  value: "DAgDGAFKBggCEgJSMg=="   # rename room 2 to "R2"
```

IDs usually start at 0 and are contiguous. Rename them back afterwards.

### Caveats

- Do Not Disturb suppresses scheduled tasks. A task landing inside that window
  silently does nothing.
- Because tasks fire on the minute, there is up to a minute of latency.
- Only tested on firmware 7.0.154.

## Diagnostic services

`dump_dps` writes the full DPS state to the log, `write_dps` writes a raw value
to any DPS. Both are meant for reverse engineering.

**`write_dps` can break things.** Writing an unknown value to an unknown DPS may
confuse the robot or corrupt its map. Only use it if you know what the payload
means.

## Requirements

- Home Assistant 2024.1.0 or later
- Eufy RoboVac S1 Pro
- Local network connection

## Installation

### Via HACS (Recommended)

1. Open HACS
2. Click on "Integrations"
3. Click the three dots menu in the top right and select "Custom repositories"
4. Add repository URL `https://github.com/subroutineox/ha-eufy-robovac-s1-pro`
5. Select "Integration" as the category
6. Click "Add"
7. Search for "Eufy RoboVac S1 Pro" in HACS and install it
8. Restart Home Assistant

### Manual Installation

1. Download this repository
2. Copy the `custom_components/eufy_robovac_s1_pro` folder to your Home Assistant's `config/custom_components/` directory
3. Restart Home Assistant

### Notes on running Home Assistant inside Docker container

You need to open 6666 and 6667 UDP ports to Home Assistant.
Please add these ports in the docker-compose.yaml as follows and rebuild the container.
```
ports:
      - '8123:8123'
      - '6666:6666/udp'
      - '6667:6667/udp'
```

## Configuration

1. Go to Home Assistant's Settings → Devices & Services
2. Click "Add Integration"
3. Search for "Eufy RoboVac S1 Pro"
4. Follow the on-screen instructions to complete the setup

### Required Information

You'll need the following information during setup:

- **username**: User ID of eufylife.com (Confirmed from eufy Clean app)
- **password**: Password for above User ID
- **IP address** (optional): Set this if UDP discovery does not find the robot,
  for example when Home Assistant sits on a different subnet or VLAN.

## Supported Entities

### Vacuum
- Basic vacuum functions (start, pause, resume, return to dock)

### Sensors
- Battery level
- Running status
- Cleaning statistics (Total Cleaning Area, Total Cleaning Count, Total Cleaning Time)
- Consumables remaining: side brush, rolling brush, high-performance filter,
  sensors, rolling mop, dirty water tank, dirty water tank filter, mop cleaning tray

### Select
- Cleaning mode and water level selection
- Suction power level

### Switch
- Auto-return toggle

### Services
- `clean_rooms` — clean specific rooms
- `cancel_clean` — cancel a pending room clean
- `dump_dps` / `write_dps` — diagnostics

## Troubleshooting

### Device Not Found

1. Verify the robot vacuum is on the same network
2. Check if the IP address is correct — or set it manually during setup
3. Review firewall settings

### Connection Errors

1. Verify the username/password is correct
2. Check if the device is online in the Eufy app
3. Check Home Assistant logs for details

### Cleaning mode shows "unknown" after a restart

The robot only publishes DPS 154 when something changes. After a restart it may
take a while before the mode is known. Selecting any mode once fixes it.

### A scheduled room clean never started

Check Do Not Disturb. Tasks falling inside that window are silently dropped.

## Contributing

Please report bugs and feature requests via [Issues](https://github.com/subroutineox/ha-eufy-robovac-s1-pro/issues).

Pull requests are welcome!

## Changelog

### v1.1.0 (fork)
- **New: local room cleaning** — `clean_rooms` and `cancel_clean` services. Room
  selection is delivered through the timer channel (DPS 164) because the direct
  command on DPS 152 ignores the room list on this firmware.
- **Fix: cleaning mode stuck on "unknown"** — DPS 154 was compared as a whole
  base64 string, but that value also changes with the suction level, so none of
  the four stored strings matched. Mopping state is now parsed structurally and
  the water level read from DPS 10.
- **Fix: Home Assistant 2026.9 compatibility** — removed `VacuumEntityFeature.BATTERY`
  and the deprecated `battery_level` property, moved `DeviceInfo` import to
  `device_registry`.
- **New: optional manual IP** in the config flow, skipping UDP discovery.
- **New: diagnostic services** `dump_dps` and `write_dps`, plus a DPS diff logger
  (enable debug logging for `custom_components.eufy_robovac_s1_pro`).
- Sensors from upstream v1.0.5: total cleaning time, eight consumable sensors,
  and a fixed DPS 167 parser.

### v1.0.3
- **Fix: Eufy API login failure** — Updated login headers (`User-Agent`, `clientType`, `client_secret` key name) to match the latest Eufy Home app (v3.1.3). Added v1/v2 endpoint fallback to handle potential future API endpoint deprecation.
- **Fix: Entity states showing "unavailable" / "unknown" after restart** — Added `RestoreEntity` support to Running Status, Cleaning Mode, Total Cleaning Count, and Total Cleaning Area entities. These now retain their last known values across Home Assistant restarts until live DPS data becomes available.
- **Cleanup: Remove verbose debug logging** — Removed DPS discovery and update debug logs from `coordinators.py` that were cluttering the log output.

### v1.0.2
- Improve Running Status Sensor to indicate more detailed status

### v1.0.1
- Fix status indication and improve varying Total Cleaning Area

### v1.0.0
- Initial release

## Credits

This project is based on:
- [tkoba1974/ha-eufy-robovac-s1-pro](https://github.com/tkoba1974/ha-eufy-robovac-s1-pro)
- [ha-eufy-robovac-g10-hybrid](https://github.com/Rjevski/ha-eufy-robovac-g10-hybrid)

Protobuf definitions taken from
[jeppesens/eufy-clean](https://github.com/jeppesens/eufy-clean).

## License

Released under the MIT License. See the [LICENSE](LICENSE) file for details.

## Disclaimer

This integration is unofficial and not supported by Anker/Eufy. Use at your own risk.
