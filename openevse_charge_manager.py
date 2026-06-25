#!/usr/bin/env python3
"""
evse_charge_manager.py

Cron-driven script to manage OpenEVSE charging based on:
  - Ambient temperature from a Wunderground personal weather station
  - Live charge current from the OpenEVSE status endpoint

Logic:
  - If temperature >= TEMP_DISABLE_F: disable output (heat degrades absorption efficiency)
  - If temperature < TEMP_ENABLE_F AND charger is disabled by this script: re-enable
  - If car is connected and charging current drops below AMP_CUTOFF_A: disable output
    (proxy for entering CV/absorption phase near full SOC)

Designed to run every 1-5 minutes via cron. State is persisted to a small JSON file
so the script knows whether it was the one that disabled the EVSE (vs a manual disable).

Cron example (every 2 minutes):
  */2 * * * * /usr/bin/python3 /path/to/evse_charge_manager.py >> /var/log/evse_manager.log 2>&1
"""

import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Configuration — edit these values
# ---------------------------------------------------------------------------

OPENEVSE_HOST = "172.28.11.186"       # Local IP or hostname of your OpenEVSE
OPENEVSE_USER = ""                     # Leave blank if auth is not enabled
OPENEVSE_PASS = ""                     # Leave blank if auth is not enabled

WU_API_KEY    = "f4058e72619a4e34858e72619a2e3460"     # Wunderground personal API key
WU_STATION_ID = "KVALEESB16"           # Your PWS station ID (e.g. KCASANFR123)

TEMP_DISABLE_F  = 73.0   # Disable charging at or above this ambient temp (°F)
TEMP_ENABLE_F   = 71.0   # Re-enable charging below this temp (hysteresis buffer)
AMP_CUTOFF_A    = 10.0   # Disable if charging current drops below this (amps)
                          # Adjust if your car's CC phase current differs

# Path to persist script state between runs
STATE_FILE = Path("/tmp/evse_manager_state.json")

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# OpenEVSE EVSE states (from firmware source)
# ---------------------------------------------------------------------------
EVSE_STATE_DISABLED  = 255
EVSE_STATE_SLEEPING  = 254
EVSE_STATE_CHARGING  = 3    # Actively delivering current
EVSE_STATE_CONNECTED = 2    # Vehicle connected, not yet charging
EVSE_STATE_NOT_CONNECTED = 1

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def openevse_url(path: str) -> str:
    return f"http://{OPENEVSE_HOST}{path}"


def openevse_auth():
    if OPENEVSE_USER and OPENEVSE_PASS:
        return (OPENEVSE_USER, OPENEVSE_PASS)
    return None


def get_evse_status() -> dict:
    """Fetch /status from OpenEVSE. Returns parsed JSON dict."""
    resp = requests.get(openevse_url("/status"), auth=openevse_auth(), timeout=10)
    resp.raise_for_status()
    return resp.json()


def set_override(state: str) -> None:
    """
    POST to /override to set EVSE state.
    state: "active" (enable charging) or "disabled" (stop output)
    Uses the manual override endpoint so it doesn't conflict with
    the scheduler or other claims.
    """
    payload = {"state": state}
    resp = requests.post(
        openevse_url("/override"),
        json=payload,
        auth=openevse_auth(),
        timeout=10,
    )
    resp.raise_for_status()
    log.info("Override set to '%s' — EVSE response: %s", state, resp.text.strip())


def clear_override() -> None:
    """DELETE /override to return control to normal scheduler/claims."""
    resp = requests.delete(
        openevse_url("/override"),
        auth=openevse_auth(),
        timeout=10,
    )
    resp.raise_for_status()
    log.info("Override cleared — EVSE response: %s", resp.text.strip())


def get_outdoor_temp_f() -> float:
    """
    Query Wunderground personal weather station API for current outdoor temp.
    Returns temperature in °F.
    """
    url = (
        "https://api.weather.com/v2/pws/observations/current"
        f"?stationId={WU_STATION_ID}&format=json&units=e&apiKey={WU_API_KEY}"
    )
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    obs = data["observations"][0]
    temp_f = obs["imperial"]["temp"]
    return float(temp_f)


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

def load_state() -> dict:
    default = {
        "script_disabled": False,   # True if this script issued the disable
        "disable_reason": None,     # "temperature" | "low_current"
        "disabled_at": None,
    }
    if STATE_FILE.exists():
        try:
            with STATE_FILE.open() as f:
                saved = json.load(f)
            default.update(saved)
        except (json.JSONDecodeError, KeyError):
            pass
    return default


def save_state(state: dict) -> None:
    with STATE_FILE.open("w") as f:
        json.dump(state, f, indent=2)


# ---------------------------------------------------------------------------
# Main logic
# ---------------------------------------------------------------------------

def main():
    state = load_state()

    # --- Get ambient temperature ---
    try:
        temp_f = get_outdoor_temp_f()
        log.info("PWS temp: %.1f°F (station: %s)", temp_f, WU_STATION_ID)
    except Exception as exc:
        log.error("Failed to fetch PWS temperature: %s", exc)
        sys.exit(1)

    # --- Get EVSE status ---
    try:
        evse = get_evse_status()
    except Exception as exc:
        log.error("Failed to fetch EVSE status: %s", exc)
        sys.exit(1)

    evse_state  = evse.get("state")
    amp_ma      = evse.get("amp", 0)        # milliamps
    amp_a       = amp_ma / 1000.0
    chargeport_state     = evse.get("state", 0)    # 1 = vehicle detected

    log.info(
        "EVSE state=%s  current=%.1fA  chargeport_state=%s",
        evse_state, amp_a, bool(chargeport_state),
    )

    # -----------------------------------------------------------------------
    # Decision tree
    # -----------------------------------------------------------------------

    # 1. Temperature gate — high temp: disable regardless of current
    if temp_f >= TEMP_DISABLE_F:
        if evse_state != EVSE_STATE_DISABLED or not state["script_disabled"]:
            log.info(
                "Temp %.1f°F >= %.1f°F threshold — disabling EVSE output.",
                temp_f, TEMP_DISABLE_F,
            )
            try:
                set_override("disabled")
                state["script_disabled"] = True
                state["disable_reason"]  = "temperature"
                state["disabled_at"]     = datetime.now().isoformat()
                save_state(state)
            except Exception as exc:
                log.error("Failed to disable EVSE: %s", exc)
        else:
            log.info("Temp above threshold and EVSE already disabled by this script — no action.")
        return

    # 2. Temperature cleared — re-enable if we were the one who disabled it
    if temp_f < TEMP_ENABLE_F and state["script_disabled"] and state["disable_reason"] == "temperature":
        log.info(
            "Temp %.1f°F < %.1f°F re-enable threshold — clearing override.",
            temp_f, TEMP_ENABLE_F,
        )
        try:
            clear_override()
            state["script_disabled"] = False
            state["disable_reason"]  = None
            state["disabled_at"]     = None
            save_state(state)
        except Exception as exc:
            log.error("Failed to clear override: %s", exc)
        return

    # 3. Current cutoff gate — car connected and current has tapered below threshold
    #    Only act if we're actually in a charging state (state 3)
    if evse_state == EVSE_STATE_CHARGING and chargeport_state and amp_a < AMP_CUTOFF_A:
        log.info(
            "Current %.1fA < %.1fA cutoff while charging — battery likely near full, disabling.",
            amp_a, AMP_CUTOFF_A,
        )
        try:
            set_override("disabled")
            state["script_disabled"] = True
            state["disable_reason"]  = "low_current"
            state["disabled_at"]     = datetime.now().isoformat()
            save_state(state)
        except Exception as exc:
            log.error("Failed to disable EVSE: %s", exc)
        return

    # 4. Re-enable after a low-current disable once the car is disconnected
    #    (vehicle plug-out resets the session; re-arm for the next session)
    if state["script_disabled"] and state["disable_reason"] == "low_current" and not chargeport_state:
        log.info("Vehicle disconnected after low-current disable — clearing override to re-arm.")
        try:
            clear_override()
            state["script_disabled"] = False
            state["disable_reason"]  = None
            state["disabled_at"]     = None
            save_state(state)
        except Exception as exc:
            log.error("Failed to clear override: %s", exc)
        return

    log.info("No action required this cycle.")


if __name__ == "__main__":
    main()
