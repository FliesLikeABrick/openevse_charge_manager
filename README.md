# OpenEVSE Charge Manager

A cron-driven Python script for managing charge sessions on an OpenEVSE EVSE, written specifically for early-generation Nissan Leaf owners whose cars lack any onboard charge management capability.

## Why I wrote this

The early Nissan Leaf (roughly 2011–2017, depending on trim) has no way to set a maximum state of charge, no onboard charge scheduling, and no temperature-aware charge management. This matters because the Leaf uses a passively air-cooled battery pack that is well-documented to degrade faster when repeatedly charged to 100% SOC and when charged in high ambient temperatures. The car will happily charge to full regardless of how hot it is outside or how full the battery already is — there is no way to tell it otherwise from inside the car.

I built this script to fill that gap using my OpenEVSE EVSE, which exposes a local HTTP API that makes it possible to control and monitor charging programmatically from anything on the local network.

## What it does

The script runs as a cron job every 1–2 minutes and enforces two conditions:

**Ambient temperature gate.** If the outdoor temperature is at or above a configurable threshold (I use 73°F), the script disables the EVSE output via the OpenEVSE override API. Charging resumes automatically once the temperature drops below a slightly lower re-enable threshold (I use 71°F — the two-degree gap provides hysteresis so the EVSE doesn't oscillate on and off when temperature is right at the boundary). My reasoning here is that lithium battery absorption efficiency degrades meaningfully in higher ambient temperatures, and since my car has no active thermal management, ambient temperature is a reasonable proxy for battery temperature during a charge session.

**Low-current cutoff.** If the car is connected and actively charging but the measured charge current drops below a configurable threshold (I use 10A), the script disables the EVSE. My Leaf charges at roughly 27A during the constant-current phase of a charge session. When current starts tapering, the car has entered the CV (constant voltage / absorption) phase, which corresponds to the battery approaching a full charge. By cutting off at 10A I stop the session before the battery reaches 100% SOC. The car does not have an onboard way to set a charge limit, so this is my workaround.

Once the EVSE has been disabled by the low-current cutoff, it stays disabled until the car is physically unplugged. On the next plug-in, the script re-arms automatically and a new session proceeds normally. This is intentional — I don't want the EVSE to re-enable mid-session after cutting off.

## How it works

The script uses two data sources on each run:

- **OpenEVSE `/status` endpoint** — returns the current EVSE state (charging, connected, disconnected, disabled), measured charge current in milliamps, and a vehicle-detected flag indicating whether a car is physically connected.
- **Weather Underground personal weather station API** — returns current outdoor temperature from a nearby PWS. I use my own station but any PWS station ID will work.

Control actions are taken via the OpenEVSE `/override` endpoint, which accepts `{"state": "disabled"}` to stop output and is cleared with a DELETE request to re-enable normal operation. The override layer sits above the EVSE's internal scheduler, so it doesn't interfere with any charge timers you may have configured.

A small JSON state file (`/tmp/evse_manager_state.json`) persists between runs so the script knows whether it was the thing that disabled the EVSE, what the reason was, and whether a charge session is currently in progress. This prevents the script from clearing a disable that was set manually via the OpenEVSE web UI, and correctly handles the re-arm logic after a low-current cutoff.

## Requirements

- Python 3.7+
- `requests` library (`pip install requests`)
- An OpenEVSE running the ESP32 WiFi firmware (v4.x or later), reachable on your local network
- A Weather Underground personal weather station API key and station ID (free tier is sufficient)

## Setup

1. Clone or download the script.
2. Install dependencies:
   ```
   pip install requests
   ```
3. Edit the configuration block at the top of `evse_charge_manager.py`:
   ```python
   OPENEVSE_HOST = "192.168.1.100"   # IP or hostname of your OpenEVSE
   OPENEVSE_USER = ""                 # only if HTTP auth is enabled
   OPENEVSE_PASS = ""

   WU_API_KEY    = "your-api-key"
   WU_STATION_ID = "KXXXXXX1"        # your PWS station ID

   TEMP_DISABLE_F = 73.0             # disable above this temperature (°F)
   TEMP_ENABLE_F  = 71.0             # re-enable below this temperature (°F)
   AMP_CUTOFF_A   = 10.0             # disable below this charge current (amps)
   ```
4. Test it manually first:
   ```
   python3 evse_charge_manager.py
   ```
5. Add to crontab once satisfied:
   ```
   */2 * * * * /usr/bin/python3 /path/to/evse_charge_manager.py >> /var/log/evse_manager.log 2>&1
   ```

## Adapting the temperature source

The temperature gate is built around the Weather Underground PWS API because that's what I have, but `get_outdoor_temp_f()` is deliberately isolated — it just needs to return a float in degrees Fahrenheit. If you have a different temperature source, replace the function body with whatever makes sense for your setup. Some examples:

**Local sensor (e.g. a DHT22 or BME280 on a Raspberry Pi):**

SHT35 would be similar
```python
def get_outdoor_temp_f() -> float:
    # Read from your sensor library of choice and return °F
    temp_c = my_sensor.read_temperature()
    return (temp_c * 9 / 5) + 32
```

**Home Assistant REST API:**

This is a guess based on my understanding of how Home Assistant works for those who use it.

```python
def get_outdoor_temp_f() -> float:
    resp = requests.get(
        "http://homeassistant.local:8123/api/states/sensor.outdoor_temperature",
        headers={"Authorization": "Bearer YOUR_HA_LONG_LIVED_TOKEN"},
        timeout=10,
    )
    resp.raise_for_status()
    return float(resp.json()["state"])
```

**A fixed value (effectively disables the temperature gate):**
```python
def get_outdoor_temp_f() -> float:
    return 0.0   # always below any reasonable threshold
```

## Disabling the temperature gate entirely

If you only want the low-current SOC cutoff and don't care about ambient temperature, the simplest approach is to return a value from `get_outdoor_temp_f()` that will never exceed `TEMP_DISABLE_F`:

```python
def get_outdoor_temp_f() -> float:
    return 0.0
```

Alternatively, set `TEMP_DISABLE_F` to a value that will never be reached in practice, like `150.0`. Either way the low-current cutoff continues to operate normally.

## Disabling the low-current SOC cutoff

If you only want the temperature gate, set `AMP_CUTOFF_A` to `0.0`. Since measured charge current will never drop below zero amps, the cutoff condition will never fire.

## A note on the low-current threshold

The 10A default reflects my specific car (a 2016 Nissan Leaf 30kwh) charging on a 240V Level 2 circuit, where the constant-current phase runs at approximately 27A. The right value for your situation depends on your car's charge rate and how conservatively you want to cut off. Setting it too high risks cutting off before the battery is at a useful charge level; setting it too low risks letting the CV phase run long enough to bring SOC close to 100% anyway. I'd suggest logging a few complete sessions and looking at where current starts to taper before committing to a threshold.

## Caveats

- The script has no way to know the actual battery SOC — it infers "approaching full" from current taper, which is a reasonable but imperfect proxy.
- If the EVSE is rebooted or the override is cleared manually while the script has a disable in effect, the state file may become stale. The script includes a reconciliation check that detects this condition (override gone but state file still says disabled) and resets gracefully on the next run.
- The `vehicle` field in the OpenEVSE status response has been unreliable in some firmware versions and hardware configurations. If you find that the low-current re-arm after unplug is not working, check whether `/status` correctly shows `"vehicle": 1` while a car is connected and `"vehicle": 0` after unplugging. If not, the proximity/pilot wiring to the handle switch is the first place to look.

- Other EVSEs with local APIs
This script is written for OpenEVSE, but the logic is straightforward enough that someone comfortable with Python could adapt it for other EVSEs that expose a local HTTP API; for example, SmartEVSE
