"""Allowlisted Prime settings constrained by reported values and capabilities.

Do not invent unsupported switches/options from model names. Expose only known
wire fields with exact expected types; bool is deliberately not interchangeable
with int. Robot/dock capability reports suppress unsupported features and narrow
auto-evacuation choices. Unknown settings or unreported values are not writable.
The returned values are robot reports, not optimistic desired state; settings
deltas are one-shot and MQTT acknowledgement does not establish adoption.
"""

import contextlib
import json
from pathlib import Path
import sqlite3


SETTINGS = {
    "child_lock": ("childLock", "Child lock", "switch", bool, None),
    "eco_charge": ("ecoCharge", "Eco charging", "switch", bool, None),
    "carpet_boost": ("carpetBoost", "Carpet boost", "switch", bool, None),
    "two_pass": ("twoPass", "Two-pass cleaning", "switch", bool, None),
    "pad_dry_allowed": ("padDryAllowed", "Pad drying", "switch", int, {0: "Off", 1: "On"}),
    "pad_wash_allowed": (
        "padWashAllowed",
        "Pad washing allowed",
        "switch",
        int,
        {0: "Off", 1: "On"},
    ),
    "auto_evac_frequency": (
        "autoevacFreq",
        "Auto-empty frequency",
        "select",
        int,
        {
            0: "Every routine",
            1: "Every 2 routines",
            2: "Every 3 routines",
            4: "On dock return",
            10: "10 m2",
            15: "15 m2",
            25: "25 m2",
            30: "30 m2",
            50: "50 m2",
        },
    ),
    "pad_dry_duration": (
        "padDryDur",
        "Mop dry duration",
        "select",
        int,
        {value: f"{value} hours" for value in range(2, 7)},
    ),
    "pad_wash_area_interval": (
        "pwAreaInterval",
        "Mop wash area interval",
        "select",
        int,
        {value: f"{value} m2" for value in (6, 8, 10, 15, 20)},
    ),
    "pad_wash_frequency": (
        "pwReturn",
        "Mop wash frequency",
        "select",
        int,
        {0: "After each room", 1: "By time", 2: "By area"},
    ),
    "pad_wash_time_interval": (
        "pwTimeInterval",
        "Mop wash time interval",
        "select",
        int,
        {value: f"{value} minutes" for value in (10, 15, 20, 25)},
    ),
    "suction_level": (
        "suctionLevel",
        "Suction level",
        "select",
        int,
        {2: "Light", 3: "Normal", 4: "Deep"},
    ),
}


def cleaning_modes(path, robot_id):
    """Interpret oMode capability bits for captured vacuum=2, mop=4, combo=6 commands."""
    with contextlib.closing(
        sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    ) as db:
        row = db.execute(
            "SELECT document FROM shadows WHERE topic=?", (f"$aws/things/{robot_id}/shadow",)
        ).fetchone()
    cap = (
        json.loads(row[0]).get("state", {}).get("reported", {}).get("cap", {}).get("oMode")
        if row
        else None
    )
    if type(cap) is not int or cap <= 0:
        return []
    # Combo commands use 2|4 (captured via Matter), not capability bit 32.
    return [mode for mode, required in ((2, 2), (4, 4), (6, 6)) if cap & required == required]


def configuration(path, robot_id):
    """Read settings/robot/dock capabilities in one snapshot and return supported controls."""
    prefix = f"$aws/things/{robot_id}/shadow"
    with contextlib.closing(
        sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    ) as db:
        db.execute("BEGIN")
        documents = {}
        for name, topic in (
            ("settings", prefix + "/name/rw-settings"),
            ("robot", prefix),
            ("current", prefix + "/name/ro-currentstate"),
        ):
            row = db.execute("SELECT document FROM shadows WHERE topic=?", (topic,)).fetchone()
            documents[name] = json.loads(row[0]).get("state", {}).get("reported", {}) if row else {}
    reported = documents["settings"]
    cap = documents["robot"].get("cap", {})
    dock = documents["current"].get("dock", {}).get("cap", {})
    result = []
    for key, (wire, name, kind, expected, options) in SETTINGS.items():
        value = reported.get(wire)
        if type(value) is not expected:
            continue
        if key.startswith("pad_dry") and dock.get("pd") == 0:
            continue
        if key.startswith("pad_wash") and dock.get("pw") == 0:
            continue
        if key == "two_pass" and cap.get("multiPass") in (0, 3):
            continue
        if key == "auto_evac_frequency":
            allowed = {
                0: (),
                1: (0, 1, 2),
                2: (0, 1, 2, 10, 15, 25, 30, 50),
                3: (0, 1, 2, 4, 10, 15, 25, 30, 50),
            }.get(cap.get("autoevac"), ())
            options = {number: label for number, label in options.items() if number in allowed}
        if options is not None and value not in options:
            continue
        result.append(
            {
                "key": key,
                "name": name,
                "kind": kind,
                "value": value,
                "options": {str(number): label for number, label in options.items()}
                if options is not None
                else {},
            }
        )
    return result


def setting_patch(path, robot_id, key, value):
    """Revalidate one requested setting against current reports before building a delta."""
    if not isinstance(key, str) or key not in SETTINGS:
        raise ValueError("Unsupported configuration setting")
    available = {item["key"]: item for item in configuration(path, robot_id)}
    if key not in available:
        raise ValueError("Setting is not reported or supported by this robot")
    wire, _, _, expected, _ = SETTINGS[key]
    if type(value) is not expected:
        raise ValueError("Invalid setting value type")
    options = available[key]["options"]
    if options and str(value) not in options:
        raise ValueError("Unsupported setting value")
    return {wire: value}
