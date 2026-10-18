"""Public static dashboard assets; authenticated inventory and robot-backed editor.

HTML/JS contain no robot data or customer key. cloud_http protects inventory/
editor APIs and supplies no-store/CSP headers. Render untrusted robot names/IDs
with textContent or SVG attributes, never HTML interpolation. SKU is the reported
identifier, not a guessed marketing name; absent details remain unknown.

Auth is origin-scoped localStorage, saved only after successful inventory auth.
It survives browser restarts, so use trusted devices: any script on this origin
can read the key. Logout/401 removes it and storage events sign out other tabs.
Storage failures warn explicitly and retain page-only sign-in. Never put the
key in cookies, query strings, bundled assets or robot logs.

The inventory refreshes every ten seconds. Mission gates fail closed on offline,
missing/unknown state and refresh errors. Disable all editor fields/interactions
but keep Reload for inspection; an empty map list is not a selectable default.
Server checks remain authoritative. Submitted edits are one-shot with generation
guards and persistent map locks, not optimistic changes to source geometry.
Mapping runs require confirmation, online state and phase charge/cycle none.
The command service rechecks docking under command ownership before publishing
train once; delivery acknowledgement does not establish robot execution.
"""

import contextlib
import json
from pathlib import Path
import re
import sqlite3
import time


def inventory(path):
    """Combine durable connection history and shadows without returning raw documents.

    Online comes from MQTT leases, not an active probe. Old shadows can discover
    a robot without an IP; only later registration/connection supplies one.
    Anonymous discovery requests cannot identify a robot. Last IP may be NAT.
    """
    from roomba_cloud.maps.controls import map_control_state

    robots = {}
    now = time.time()
    with contextlib.closing(
        sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    ) as db:
        db.execute("BEGIN")
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}

        def robot(robot_id):
            return robots.setdefault(
                robot_id,
                {
                    "robot_id": robot_id,
                    "ip": None,
                    "sku": None,
                    "firmware": None,
                    "battery": None,
                    "phase": None,
                    "online": False,
                    "first_seen": None,
                    "last_seen": None,
                },
            )

        def seen(item, timestamp):
            if type(timestamp) in (int, float):
                item["last_seen"] = max(item["last_seen"] or 0, timestamp)

        if "robot_connections" in tables:
            for robot_id, ip, first, last, sku in db.execute("SELECT * FROM robot_connections"):
                item = robot(robot_id)
                item.update(ip=ip, first_seen=first, last_seen=last, sku=sku)
        for robot_id, expires in db.execute("SELECT robot_id, expires FROM mqtt_sessions"):
            item = robot(robot_id)
            item["online"] = item["online"] or expires >= now
        for robot_id, received in db.execute("SELECT robot_id, received_at FROM robot_data"):
            seen(robot(robot_id), received)
        for topic, raw in db.execute("SELECT topic, document FROM shadows"):
            match = re.fullmatch(r"\$aws/things/([^/]+)/shadow(?:/name/([^/]+))?", topic)
            if not match:
                continue
            item = robot(match[1])
            document = json.loads(raw)
            seen(item, document.get("timestamp"))
            reported = document.get("state", {}).get("reported", {})
            name = match[2]
            if name is None and isinstance(reported.get("sku"), str):
                item["sku"] = reported["sku"]
            if name == "rw-software" and isinstance(reported.get("softwareVer"), str):
                item["firmware"] = reported["softwareVer"]
            if name == "ro-currentstate":
                battery = reported.get("batPct")
                if type(battery) is int and 0 <= battery <= 100:
                    item["battery"] = battery
                mission = reported.get("cleanMissionStatus")
                if isinstance(mission, dict) and isinstance(mission.get("phase"), str):
                    item["phase"] = mission["phase"]
    for key, item in robots.items():
        item["map_controls"] = map_control_state(path, key)
        item["mapping_controls"] = map_control_state(path, key, docked_only=True)
    return {"robots": [robots[key] for key in sorted(robots)]}


HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Roomba cloud</title><link rel="stylesheet" href="/dashboard.css">
<script src="/dashboard.js" defer></script></head>
<body><main><h1>Roomba cloud</h1><p>Robots that have connected to this emulator.</p>
<form id="login"><label for="token">Customer token</label>
<input id="token" type="password" required autocomplete="off" spellcheck="false">
<button>Sign in</button></form>
<section id="dashboard" hidden><button id="refresh">Refresh</button>
<button id="logout">Sign out</button>
<p>Mapping runs make the robot leave its dock and explore. They require a docked
robot with no active or paused mission and confirmation before sending.</p>
<div class="scroll"><table><thead><tr><th>Robot ID</th><th>Model / SKU</th>
<th>Last IP</th><th>MQTT</th><th>Battery</th><th>Phase</th><th>Firmware</th>
<th>Last seen</th><th>Actions</th></tr></thead><tbody id="robots"></tbody></table></div>
<section id="editor" hidden><h2>Room editor</h2>
<p>Maps cannot be edited or deleted while the robot is running or has a paused mission.
The robot must be idle with no mission; controls are also locked when mission state is unknown.</p>
<p id="edit-mission-lock" role="status"></p>
<button id="edit-reload">Reload map</button>
<fieldset id="edit-fields" disabled><legend>Map edits</legend>
<label>Map <select id="edit-map"></select></label>
<button id="edit-delete" disabled>Delete map</button>
<div class="editor-layout"><svg id="edit-canvas" role="img" aria-label="Room map"></svg>
<div><label>Room <select id="edit-room"></select></label>
<label>Operation <select id="edit-operation">
<option value="rename">Rename room</option><option value="split">Split room</option>
<option value="merge">Merge rooms</option></select></label>
<label id="edit-name-label">New name <input id="edit-name" aria-describedby="edit-name-help"></label>
<small id="edit-name-help">1-64 characters after trimming spaces. International text and punctuation are allowed; control and invisible formatting characters are not.</small>
<label id="edit-other-label" hidden>Merge with <select id="edit-other"></select></label>
<p id="edit-hint"></p><button id="edit-clear">Clear split line</button>
<label><input id="edit-enable" type="checkbox"> I understand that map changes cannot be reverted</label>
<button id="edit-submit">Apply to robot</button>
<p id="edit-status" role="status" aria-live="polite"></p></div></div></fieldset></section></section>
<p id="status" role="status" aria-live="polite"></p>
<p id="storage-status" role="status" aria-live="polite"></p>
<p class="note">The token is remembered in this browser until you sign out.
Use only on a trusted device. Unknown details
appear as a dash until reported. Model / SKU is the robot-reported identifier,
not a guessed marketing model. Last IP is the address seen by the server.</p>
</main></body></html>
"""

CSS = """
:root { color-scheme: light dark; font: 16px system-ui, sans-serif; }
body { margin: 0; background: light-dark(#f5f7fa, #15191f); }
main { max-width: 1400px; margin: 3rem auto; padding: 0 1.5rem; }
h1 { margin-bottom: .5rem; }
form { display: flex; flex-wrap: wrap; gap: .75rem; align-items: center; margin: 2rem 0; }
[hidden] { display: none !important; }
input, button { font: inherit; padding: .65rem; border: 1px solid #7c8795; border-radius: .4rem; }
input { min-width: 15rem; }
button { cursor: pointer; }
.scroll { overflow-x: auto; margin-top: 1rem; }
table { border-collapse: collapse; width: 100%; background: light-dark(white, #222831); }
th, td { padding: .85rem; text-align: left; border-bottom: 1px solid #7c8795; }
td:first-child { font: .85rem monospace; }
td { overflow-wrap: anywhere; }
.editor-layout { display: grid; grid-template-columns: minmax(0, 2fr) minmax(15rem, 1fr); gap: 1rem; }
#editor label { display: block; margin: .8rem 0; }
#editor select { max-width: 100%; font: inherit; padding: .4rem; }
#edit-canvas { width: 100%; max-height: 70vh; background: #222; touch-action: none; }
#edit-canvas polygon { fill: #3269d2; fill-opacity: .6; stroke: #ddd; stroke-width: 1; vector-effect: non-scaling-stroke; cursor: pointer; }
#edit-canvas polygon.selected { fill: #eb55b4; }
#edit-canvas line { stroke: #ffdc40; stroke-width: 3; vector-effect: non-scaling-stroke; }
#editor input[type=checkbox] { min-width: 0; }
@media (max-width: 700px) { .editor-layout { grid-template-columns: 1fr; } }
.note { max-width: 65rem; color: light-dark(#4c5664, #bac4d0); font-size: .9rem; }
"""

JS = """
'use strict';
let token = '';
let busy = false;
let generation = 0;
// Generation counters invalidate late responses after logout or robot/map changes.
// They cancel UI adoption, not an already submitted robot operation.
let editRobot = null;
let editMaps = [];
let splitPoints = [];
let editing = false;
let editGeneration = 0;
let mapControlsAllowed = false;
function updateMapControls(control) {
  // UX gate only: room_edits rechecks persisted mission state before publication.
  mapControlsAllowed = control?.allowed === true;
  document.querySelector('#edit-fields').disabled = editing || !mapControlsAllowed || !editMaps.length;
  document.querySelector('#edit-mission-lock').textContent = mapControlsAllowed ? '' :
    (control?.reason || 'Map editing locked: mission state is unavailable.');
  document.querySelector('#edit-submit').disabled = !canSubmitEdit();
  document.querySelector('#edit-delete').disabled = !canDeleteMap();
}
const login = document.querySelector('#login');
const dashboard = document.querySelector('#dashboard');
const status = document.querySelector('#status');
const rows = document.querySelector('#robots');
let mappingButtons = [];
const tokenStorageKey = 'roomba-cloud.customer-token';
function storeToken(value) {
  // Origin-local persistence is intentional; storage denial must not silently fail.
  try {
    if (value) localStorage.setItem(tokenStorageKey, value);
    else localStorage.removeItem(tokenStorageKey);
    document.querySelector('#storage-status').textContent = '';
  } catch (error) {
    document.querySelector('#storage-status').textContent = value
      ? 'Browser storage is unavailable. The key cannot be remembered after a reload.'
      : 'Unable to remove the saved key. Clear this site\\'s browser data before leaving a shared device.';
  }
}
function logout() {
  token = '';
  storeToken('');
  generation++;
  editGeneration++;
  editRobot = null;
  editMaps = [];
  updateMapControls(null);
  document.querySelector('#editor').hidden = true;
  document.querySelector('#edit-canvas').replaceChildren();
  document.querySelector('#edit-enable').checked = false;
  rows.replaceChildren();
  dashboard.hidden = true;
  login.hidden = false;
  document.querySelector('#token').value = '';
  status.textContent = 'Signed out.';
}
async function refresh() {
  // Save a key only after authenticated inventory succeeds; 503 is not bad auth.
  if (!token || busy) return;
  busy = true;
  for (const button of mappingButtons) button.disabled = true;
  const requestGeneration = generation;
  status.textContent = 'Loading...';
  try {
    const response = await fetch('/v1/customer/robots', {
      headers: {Authorization: 'Bearer ' + token}, cache: 'no-store',
      signal: AbortSignal.timeout(10000)
    });
    if (generation !== requestGeneration) return;
    if (response.status === 401) {
      logout();
      status.textContent = 'Invalid customer token. Please sign in again.';
      return;
    }
    if (!response.ok) throw new Error('Server returned HTTP ' + response.status);
    const data = await response.json();
    if (generation !== requestGeneration) return;
    storeToken(token);
    if (editRobot) {
      const robot = data.robots.find(item => item.robot_id === editRobot);
      updateMapControls(robot?.online ? robot.map_controls : null);
    }
    rows.replaceChildren();
    mappingButtons = [];
    for (const robot of data.robots) {
      const row = document.createElement('tr');
      const cells = [robot.robot_id, robot.sku, robot.ip,
        robot.online ? 'Online' : 'Offline',
        robot.battery === null ? null : robot.battery + '%',
        robot.phase, robot.firmware,
        robot.last_seen === null ? null : new Date(robot.last_seen * 1000).toLocaleString()];
      for (const value of cells) {
        const cell = document.createElement('td');
        cell.textContent = value === null ? '-' : String(value);
        row.append(cell);
      }
      rows.append(row);
      const editCell = document.createElement('td');
      const editButton = document.createElement('button');
      editButton.textContent = 'Edit rooms';
      editButton.addEventListener('click', () => loadEditor(robot.robot_id));
      editCell.append(editButton);
      const trainButton = document.createElement('button');
      trainButton.className = 'mapping-run';
      mappingButtons.push(trainButton);
      trainButton.textContent = 'Mapping run';
      trainButton.disabled = !robot.online || robot.mapping_controls?.allowed !== true;
      trainButton.title = robot.online
        ? robot.mapping_controls?.reason || 'Start an exploration run from the dock'
        : 'Robot must be online and docked';
      trainButton.addEventListener('click', async () => {
        if (trainButton.disabled || !token) return;
        if (!confirm('Start a mapping run? The robot will leave the dock and explore your home.')) return;
        trainButton.disabled = true;
        const actionGeneration = generation;
        status.textContent = 'Sending mapping request...';
        try {
          const response = await fetch('/v1/customer/robots/' + robot.robot_id + '/commands/train', {
            method: 'POST', headers: {Authorization: 'Bearer ' + token},
            signal: AbortSignal.timeout(10000)
          });
          if (generation !== actionGeneration) return;
          if (response.status === 401) { logout(); return; }
          if (!response.ok) throw new Error('Server returned HTTP ' + response.status);
          status.textContent = 'Mapping request delivered; robot execution is not confirmed.';
        } catch (error) {
          if (generation === actionGeneration) status.textContent =
            'Mapping request not confirmed: ' + error.message + '. Check the robot before retrying.';
        }
      });
      editCell.append(trainButton);
      row.append(editCell);
    }
    login.hidden = true;
    dashboard.hidden = false;
    status.textContent = data.robots.length
      ? data.robots.length + ' robot(s). Updates every 10 seconds.'
      : 'No robots have connected yet. Updates every 10 seconds.';
  } catch (error) {
    updateMapControls(null);
    if (generation === requestGeneration) status.textContent = 'Unable to load robots: ' + error.message;
  } finally {
    busy = false;
  }
}
login.addEventListener('submit', event => {
  event.preventDefault();
  token = document.querySelector('#token').value.trim();
  document.querySelector('#token').value = '';
  refresh();
});
document.querySelector('#refresh').addEventListener('click', refresh);
document.querySelector('#logout').addEventListener('click', logout);
setInterval(refresh, 10000);

const byId = id => document.getElementById(id);
async function editorRequest(path, body) {
  const response = await fetch('/v1/customer/robots/' + editRobot + path, {
    method: body === undefined ? 'GET' : 'POST',
    headers: {Authorization: 'Bearer ' + token,
      ...(body === undefined ? {} : {'Content-Type': 'application/json'})},
    body: body === undefined ? undefined : JSON.stringify(body),
    cache: 'no-store', signal: AbortSignal.timeout(10000)
  });
  if (response.status === 401) { logout(); throw new Error('Please sign in again.'); }
  if (!response.ok) {
    const detail = await response.json().catch(() => null);
    throw new Error(detail && detail.error ? detail.error : 'HTTP ' + response.status);
  }
  return response.json();
}
function options(select, entries) {
  select.replaceChildren();
  for (const [value, name] of entries) {
    const option = document.createElement('option');
    option.value = value; option.textContent = name; select.append(option);
  }
}
function activeMap() { return editMaps.find(map => map.id === byId('edit-map').value); }
function canSubmitEdit() {
  const operation = byId('edit-operation').value;
  return mapControlsAllowed && !editing && !activeMap()?.awaiting_upload && activeMap()?.operations.includes(operation) &&
    (operation !== 'merge' || byId('edit-other').options.length > 0) &&
    (operation !== 'split' || splitPoints.length === 2);
}
function canDeleteMap() {
  return mapControlsAllowed && !editing && activeMap() &&
    !activeMap().awaiting_upload && activeMap().operations.includes('delete');
}
async function loadEditor(robot) {
  if (editing) return;
  editRobot = robot;
  updateMapControls(null);
  const epoch = ++editGeneration;
  editMaps = [];
  byId('edit-canvas').replaceChildren();
  byId('edit-enable').checked = false;
  byId('editor').hidden = false;
  byId('edit-submit').disabled = true;
  byId('edit-status').textContent = 'Loading map...';
  try {
    const document = await editorRequest('/room-editor');
    if (epoch !== editGeneration) return;
    editMaps = document.maps;
    updateMapControls(document.map_controls);
    options(byId('edit-map'), editMaps.map((map, i) => [map.id, 'Map ' + (i + 1) + ' - ' + map.id]));
    changeMap();
    byId('edit-status').textContent = editMaps.length ? 'Map loaded. Changes are drafts until applied.' : 'No map geometry available.';
  } catch (error) { if (epoch === editGeneration) byId('edit-status').textContent = error.message; }
  finally { if (epoch === editGeneration) byId('edit-submit').disabled = !canSubmitEdit(); }
}
function changeMap() {
  const map = activeMap();
  splitPoints = [];
  options(byId('edit-room'), map ? map.rooms.map(room => [room.id, room.name]) : []);
  changeOperation();
}
function changeOperation() {
  // Merge neighbors come from the uploaded matrix, not touching SVG polygons.
  const operation = byId('edit-operation').value;
  const map = activeMap();
  const selectedRoom = map?.rooms.find(item => item.id === byId('edit-room').value);
  const previousOther = byId('edit-other').value;
  const neighbors = map ? map.rooms.filter(room => selectedRoom?.adjacent_room_ids.includes(room.id)) : [];
  options(byId('edit-other'), neighbors.map(room => [room.id, room.name]));
  if (neighbors.some(room => room.id === previousOther)) byId('edit-other').value = previousOther;
  byId('edit-submit').disabled = !canSubmitEdit();
  byId('edit-delete').disabled = !canDeleteMap();
  byId('edit-name-label').hidden = operation !== 'rename';
  byId('edit-name-help').hidden = operation !== 'rename';
  byId('edit-other-label').hidden = operation !== 'merge';
  byId('edit-clear').hidden = operation !== 'split';
  byId('edit-hint').textContent = operation === 'split'
    ? 'Select the room, then click two endpoints to draw one cut across it. Place endpoints on or beyond its walls, inside the map. Click again to redraw.'
    : operation === 'merge' ? (neighbors.length
      ? 'Only robot-reported adjacent rooms are listed. The robot decides the resulting name and room ID; reload after merging.'
      : 'No verified adjacent rooms available for merging in this map.')
    : 'Enter the room name to save on the robot.';
  if (map?.awaiting_upload) byId('edit-hint').textContent =
    map.pending_deletion ? 'Deletion pending: waiting for the robot inventory to remove this map. Reload to check.' :
      'Editing locked: waiting for a new upload of this map after the previous edit. Reload to check.';
  const room = activeMap()?.rooms.find(item => item.id === byId('edit-room').value);
  byId('edit-name').value = room ? room.name : '';
  drawEditor();
}
function drawEditor() {
  // Source contours are bottom-up grid coordinates; flip SVG display only.
  // The click handler inverts Y once so the server receives unshifted grid points.
  const svg = byId('edit-canvas'), map = activeMap();
  byId('edit-submit').disabled = !canSubmitEdit();
  svg.replaceChildren();
  if (!map) return;
  svg.setAttribute('viewBox', '0 0 ' + map.width + ' ' + map.height);
  const group = document.createElementNS('http://www.w3.org/2000/svg', 'g');
  group.setAttribute('transform', 'translate(0 ' + map.height + ') scale(1 -1)');
  for (const room of map.rooms) {
    const polygon = document.createElementNS(svg.namespaceURI, 'polygon');
    polygon.setAttribute('points', room.contour.map(point => point.join(',')).join(' '));
    if (room.id === byId('edit-room').value ||
        (byId('edit-operation').value === 'merge' && room.id === byId('edit-other').value))
      polygon.classList.add('selected');
    const title = document.createElementNS(svg.namespaceURI, 'title');
    title.textContent = room.name; polygon.append(title);
    polygon.addEventListener('click', () => {
      if (mapControlsAllowed && !editing && byId('edit-operation').value !== 'split') {
        byId('edit-room').value = room.id; changeOperation();
      }
    });
    group.append(polygon);
  }
  if (splitPoints.length) {
    const line = document.createElementNS(svg.namespaceURI, 'line');
    const end = splitPoints[1] || splitPoints[0];
    for (const [key, value] of Object.entries({x1: splitPoints[0][0], y1: splitPoints[0][1], x2: end[0], y2: end[1]}))
      line.setAttribute(key, value);
    group.append(line);
    for (const [x, y] of splitPoints) {
      const marker = document.createElementNS(svg.namespaceURI, 'circle');
      marker.setAttribute('cx', x); marker.setAttribute('cy', y);
      marker.setAttribute('r', Math.max(map.width, map.height) / 150);
      marker.setAttribute('fill', 'white');
      group.append(marker);
    }
  }
  svg.append(group);
}
byId('edit-canvas').addEventListener('click', event => {
  if (byId('edit-operation').value !== 'split' || editing || !mapControlsAllowed) return;
  const map = activeMap(), svg = byId('edit-canvas');
  if (!map) return;
  const point = svg.createSVGPoint(); point.x = event.clientX; point.y = event.clientY;
  const local = point.matrixTransform(svg.getScreenCTM().inverse());
  if (local.x < 0 || local.x >= map.width || local.y <= 0 || local.y > map.height) return;
  if (splitPoints.length === 2) splitPoints = [];
  splitPoints.push([local.x, map.height - local.y]); drawEditor();
});
byId('edit-map').addEventListener('change', changeMap);
byId('edit-room').addEventListener('change', () => { splitPoints = []; changeOperation(); });
byId('edit-other').addEventListener('change', drawEditor);
byId('edit-operation').addEventListener('change', () => { splitPoints = []; changeOperation(); });
byId('edit-clear').addEventListener('click', () => { splitPoints = []; drawEditor(); });
byId('edit-reload').addEventListener('click', () => loadEditor(editRobot));
byId('edit-submit').addEventListener('click', () => submitEdit(byId('edit-operation').value));
byId('edit-delete').addEventListener('click', () => submitEdit('delete'));
function normalizeRoomName(name) {
  // Match room_edits.normalize_room_name: Unicode code points, not UTF-16 units.
  // Reject controls/formats before trim, including hidden trailing line breaks.
  if (/[\\p{Cc}\\p{Cf}\\p{Cs}\\p{Zl}\\p{Zp}]/u.test(name)) {
    throw new Error('Room names cannot contain control or invisible formatting characters');
  }
  name = name.trim();
  if ([...name].length < 1 || [...name].length > 64) {
    throw new Error('Room names must contain 1-64 characters after trimming whitespace');
  }
  return name;
}
async function submitEdit(operation) {
  // Do not retry after request failure: delivery/execution may already have happened.
  // A confirmation/result is not fresh geometry; Reload reconciles persistent locks.
  const map = activeMap(), deleting = operation === 'delete';
  if (!map || !(deleting ? canDeleteMap() : canSubmitEdit())) return;
  if (!byId('edit-enable').checked) { byId('edit-status').textContent = 'Acknowledge that map changes cannot be reverted first.'; return; }
  const body = {map_id: map.id, version: map.version, revision: map.revision, operation};
  if (!deleting) body.room_id = byId('edit-room').value;
  if (operation === 'rename') {
    try {
      body.name = normalizeRoomName(byId('edit-name').value);
      byId('edit-name').value = body.name;
    } catch (error) {
      byId('edit-status').textContent = error.message;
      return;
    }
  }
  if (operation === 'merge') body.other_room_id = byId('edit-other').value;
  if (operation === 'split') body.points = splitPoints;
  const room = map.rooms.find(item => item.id === body.room_id);
  const question = deleting
    ? 'Permanently delete map ' + map.id + ' and all its rooms from the robot? This cannot be reverted. Retained emulator uploads are not a restore mechanism.'
    : 'Apply this ' + operation + ' to room "' + room.name + '" on map ' +
      map.id + '? The robot may switch active maps. There is no verified undo.';
  if (!confirm(question)) return;
  editing = true;
  byId('edit-fields').disabled = true; byId('edit-submit').disabled = true;
  const epoch = editGeneration;
  try {
    const result = await editorRequest('/room-edits', body);
    if (epoch !== editGeneration) return;
    map.awaiting_upload = true;
    map.pending_deletion = deleting;
    byId('edit-status').textContent = 'Sent. Waiting for the robot result...';
    for (let attempt = 0; attempt < 30; attempt++) {
      await new Promise(resolve => setTimeout(resolve, 2000));
      if (epoch !== editGeneration) return;
      const state = await editorRequest('/room-edits/' + result.transaction_id);
      if (epoch !== editGeneration) return;
      byId('edit-status').textContent = state.message;
      if (state.status === 'deleted') {
        editMaps = editMaps.filter(item => item.id !== map.id);
        options(byId('edit-map'), editMaps.map((item, i) => [item.id, 'Map ' + (i + 1) + ' - ' + item.id]));
        changeMap();
      }
      if (state.status !== 'pending') break;
    }
  } catch (error) {
    map.awaiting_upload = true;
    if (epoch === editGeneration) byId('edit-status').textContent =
      error.message + ' Do not retry automatically; reload and check the robot map first.';
  } finally {
    editing = false;
    byId('edit-fields').disabled = !mapControlsAllowed || !editMaps.length;
    byId('edit-delete').disabled = !canDeleteMap();
    if (epoch === editGeneration) byId('edit-submit').disabled = !canSubmitEdit();
  }
}
try {
  token = localStorage.getItem(tokenStorageKey) || '';
} catch (error) {
  document.querySelector('#storage-status').textContent =
    'Browser storage is unavailable. Enter the key to sign in for this page only.';
}
window.addEventListener('storage', event => {
  if (event.storageArea === localStorage &&
      (event.key === tokenStorageKey || event.key === null) && !event.newValue) logout();
});
if (token) refresh();
"""

ASSETS = {
    "/": ("text/html; charset=utf-8", HTML),
    "/dashboard.css": ("text/css; charset=utf-8", CSS),
    "/dashboard.js": ("text/javascript; charset=utf-8", JS),
}
