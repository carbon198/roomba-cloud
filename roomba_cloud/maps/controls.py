"""Fail-closed mission guard shared by user edits and automatic upload probes.

Only cycle="none" AND phase charge/stop/idle permits changes. A paused mission,
even while charging, is still a mission. Missing/unknown telemetry denies access;
malformed persisted shapes and database failures raise explicit errors. This
guard reads the latest persisted report, not a physical robot probe, and does not
claim an authoritative idle loaded-map identity. The browser adds online/UI
gating, but server checks here remain mandatory before payload construction.
Mapping runs and manual dock actions use docked_only: phase charge and cycle none.
"""

import contextlib
import json
from os import PathLike
from pathlib import Path
import sqlite3
from typing import TypedDict


class MapControlState(TypedDict):
    allowed: bool
    reason: str


def map_control_state(
    database: str | PathLike[str], robot_id: str, *, docked_only: bool = False
) -> MapControlState:
    with contextlib.closing(
        sqlite3.connect(Path(database).resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
    ) as db:
        row = db.execute(
            "SELECT document FROM shadows WHERE topic=?",
            (f"$aws/things/{robot_id}/shadow/name/ro-currentstate",),
        ).fetchone()
    document: object = json.loads(row[0]) if row else {}
    if not isinstance(document, dict):
        raise ValueError("Invalid persisted shadow document")
    state = document.get("state", {})
    if not isinstance(state, dict):
        raise ValueError("Invalid persisted shadow state")
    reported = state.get("reported", {})
    if not isinstance(reported, dict):
        raise ValueError("Invalid persisted reported state")
    mission = reported.get("cleanMissionStatus", {})
    allowed = (
        isinstance(mission, dict)
        and mission.get("cycle") == "none"
        and mission.get("phase") in (("charge",) if docked_only else ("charge", "stop", "idle"))
    )
    return {
        "allowed": allowed,
        "reason": ""
        if allowed
        else (
            "This action requires a docked robot with no active or paused mission."
            if docked_only
            else "Map controls require a known idle robot with no active or paused mission."
        ),
    }


def require_no_mission(database: str | PathLike[str], robot_id: str) -> None:
    """Raise before any publication when no explicit no-mission state is available."""
    state = map_control_state(database, robot_id)
    if not state["allowed"]:
        raise ValueError(state["reason"])
