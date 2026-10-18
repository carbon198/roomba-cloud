"""Decode verified map geometry and render private, display-only PNGs.

SCMap geometry uses bottom-up raster cells and world coordinates. SavedMap keeps
the original raster/contours/adjacency for edit validation; all visual cleanup is
on copies. A solid colored polygon is NOT measured navigable area, obstacle
classification or cleaning coverage. No flooring-material layer is decoded.

Rendering uses 2x supersampling, an equal nearest/bilinear fill blend, separately
drawn walls/overlays, then Lanczos downsampling. This is the user-approved middle
ground between stair steps and blur. Text is drawn at final resolution. Structural
walls replace navigable contour strokes when available; contours are fallback.
Renderer-only changes require an emulator restart, not an HA integration update.
Maps on floors without a dock can report an out-of-bounds dock pose (observed
1100,1100). Represent that optional marker as unavailable, not invalid geometry.
"""

import argparse
from dataclasses import dataclass
import hashlib
import io
import math
import os
from pathlib import Path
import stat
import struct

from roomba_cloud.maps.catalog import archive_maps, decode_rooms, decompress_map, wire_fields

MAX_CELLS = 4_000_000
MAX_POINTS = 100_000
BACKGROUND = (34, 34, 34)
PALETTE = (
    (50, 105, 210),
    (235, 85, 180),
    (50, 200, 215),
    (85, 180, 65),
    (240, 215, 65),
    (150, 70, 195),
    (240, 135, 40),
    (205, 55, 65),
)
COLOR_NAMES = ("Blue", "Magenta", "Cyan", "Green", "Yellow", "Purple", "Orange", "Red")


def room_colors(rooms):
    """Assign deterministic colors by numeric room ID, never infer flooring/material types."""
    return {
        key: (PALETTE[index % len(PALETTE)], COLOR_NAMES[index % len(PALETTE)])
        for index, key in enumerate(sorted(rooms, key=int))
    }


def singular(data):
    fields = {}
    for number, wire, value in wire_fields(data):
        if number in fields:
            raise ValueError("Duplicate scalar map field")
        fields[number] = (wire, value)
    return fields


def required(fields, number, wire):
    if number not in fields or fields[number][0] != wire:
        raise ValueError("Missing or invalid map geometry field")
    return fields[number][1]


def finite_float(fields, number):
    value = struct.unpack("<f", required(fields, number, 5))[0]
    if not math.isfinite(value) or abs(value) > 1_000_000:
        raise ValueError("Invalid map coordinate")
    return value


@dataclass(frozen=True)
class SavedMap:
    """Verified source geometry, not a smoothed display representation.

    origin/resolution convert grid coordinates to world units. Contours are
    integer raster coordinates; dock and structural lines are converted to grid
    space during decode. None adjacency means unavailable, not no neighbors.
    Never derive merge eligibility from polygon proximity or rendered pixels.
    """

    width: int
    height: int
    resolution: float
    raster: bytes
    rooms: dict[str, str]
    contours: dict[str, tuple[tuple[int, int], ...]]
    dock: tuple[float, float] | None
    structural_lines: tuple[tuple[tuple[float, float], tuple[float, float]], ...] = ()
    origin: tuple[float, float] = (0, 0)
    adjacent_rooms: dict[str, tuple[str, ...]] | None = None


def decode_geometry(compressed):
    """Decode head=3, raster=4, dock=7, adjacency=13, contours=14, walls=19.

    Reject inconsistent bounds, missing contours, duplicate IDs, malformed
    adjacency and out-of-range room geometry before it reaches rendering or edits.
    A finite out-of-bounds dock pose means no usable dock marker for this map.
    Structural endpoints are world-space cell boundaries, hence their -0.5
    alignment; do not reuse that half-cell adjustment for split-room commands.
    """
    data = decompress_map(compressed)
    rooms = decode_rooms(compressed)
    selected = {}
    chains = []
    for number, wire, value in wire_fields(data):
        if number not in (3, 4, 7, 13, 14, 19):
            continue
        if wire != 2:
            raise ValueError("Invalid map geometry message")
        if number == 14:
            if len(chains) >= 100:
                raise ValueError("Too many room contours")
            chains.append(value)
        else:
            if number in selected:
                raise ValueError("Duplicate map geometry message")
            selected[number] = value if number == 19 else singular(value)
    head = selected.get(3, {})
    width, height = required(head, 2, 0), required(head, 3, 0)
    if not 1 <= width <= 4096 or not 1 <= height <= 4096 or width * height > MAX_CELLS:
        raise ValueError("Unsupported map raster dimensions")
    xmin, ymin, xmax, ymax, resolution = [finite_float(head, n) for n in range(4, 9)]
    if (
        not 0.001 <= resolution <= 1
        or not math.isclose(xmax - xmin, width * resolution, abs_tol=resolution / 10)
        or not math.isclose(ymax - ymin, height * resolution, abs_tol=resolution / 10)
    ):
        raise ValueError("Map bounds do not match raster dimensions and resolution")
    raster = required(selected.get(4, {}), 1, 2)
    if len(raster) != width * height:
        raise ValueError("Map raster length does not match dimensions")
    contours = {}
    total_points = 0
    for chain in chains:
        identifiers = []
        points = []
        for number, wire, value in wire_fields(chain):
            if number == 1:
                if wire != 0:
                    raise ValueError("Invalid contour room ID")
                identifiers.append(str(value))
            elif number == 2:
                total_points += 1
                if wire != 2 or total_points > MAX_POINTS:
                    raise ValueError("Invalid or oversized room contour")
                point = singular(value)
                x, y = required(point, 1, 0), required(point, 2, 0)
                if not 0 <= x < width or not 0 <= y < height:
                    raise ValueError("Contour point is outside the raster")
                points.append((x, y))
        if len(identifiers) != 1 or identifiers[0] not in rooms or identifiers[0] in contours:
            raise ValueError("Unknown or duplicate contour room ID")
        if len(points) < 3:
            raise ValueError("Incomplete room contour")
        contours[identifiers[0]] = tuple(points)
    if contours.keys() != rooms.keys():
        raise ValueError("Room table and contours do not match")
    adjacent_rooms = None
    if 13 in selected:
        matrix = required(selected[13], 1, 2)
        identifiers = list(rooms)
        count = len(identifiers)
        if len(matrix) != count * count:
            raise ValueError("Room adjacency matrix dimensions do not match room table")
        if any(matrix[i * count + i] for i in range(count)) or any(
            matrix[i * count + j] != matrix[j * count + i] for i in range(count) for j in range(i)
        ):
            raise ValueError("Invalid room adjacency matrix")
        adjacent_rooms = {
            room: tuple(
                other for j, other in enumerate(identifiers) if i != j and matrix[i * count + j]
            )
            for i, room in enumerate(identifiers)
        }
    dock = None
    if 7 in selected:
        pose = selected[7]
        x, y = finite_float(pose, 1), finite_float(pose, 2)
        position = ((x - xmin) / resolution, (y - ymin) / resolution)
        if 0 <= position[0] < width and 0 <= position[1] < height:
            dock = position
    structural_lines = []
    if 19 in selected:
        room_count = 0
        for number, wire, room in wire_fields(selected[19]):
            if number != 1:
                continue
            room_count += 1
            if wire != 2 or room_count > 100:
                raise ValueError("Invalid structural room geometry")
            room_ids = []
            for key, kind, value in wire_fields(room):
                if key == 1:
                    if kind != 0:
                        raise ValueError("Invalid structural room ID")
                    room_ids.append(str(value))
                elif key == 2:
                    if kind != 2 or len(structural_lines) >= MAX_POINTS:
                        raise ValueError("Invalid or oversized structural line list")
                    line = singular(value)
                    endpoints = []
                    for endpoint in (2, 3):
                        point = singular(required(line, endpoint, 2))
                        x, y = finite_float(point, 1), finite_float(point, 2)
                        if not xmin <= x <= xmax or not ymin <= y <= ymax:
                            raise ValueError("Structural endpoint is outside map bounds")
                        # Endpoints are world coordinates, not integer contour cells.
                        endpoints.append(
                            ((x - xmin) / resolution - 0.5, (y - ymin) / resolution - 0.5)
                        )
                    structural_lines.append(tuple(endpoints))
            # Post-cleaning uploads also contain wall records under ID 0, outside
            # the named-room table. Keep their validated lines, not a synthetic room.
            if len(room_ids) != 1 or (room_ids[0] != "0" and room_ids[0] not in rooms):
                raise ValueError("Unknown structural room ID")
    return SavedMap(
        width,
        height,
        resolution,
        raster,
        rooms,
        contours,
        dock,
        tuple(structural_lines),
        (xmin, ymin),
        adjacent_rooms,
    )


def display_raster(model):
    """Fill only unambiguous room contours, preserving known cells and overlaps.

    This intentionally hides grey/black interior patches, including obstacles.
    Unknown cells outside polygons and ambiguous overlapping polygons remain
    unchanged. It is presentation only: uploaded bytes, edit contours and
    adjacency must never be replaced with these synthesized fills.
    """
    from PIL import Image, ImageChops, ImageDraw

    size = (model.width, model.height)
    source = Image.frombytes("L", size, model.raster)
    fills = source.copy()
    covered = Image.new("1", size)
    overlaps = Image.new("1", size)
    for key, points in model.contours.items():
        identifier = int(key)
        if not 1 <= identifier <= 255:
            continue
        mask = Image.new("1", size)
        ImageDraw.Draw(mask).polygon(points, fill=1)
        overlaps = ImageChops.logical_or(overlaps, ImageChops.logical_and(covered, mask))
        covered = ImageChops.logical_or(covered, mask)
        fills.paste(identifier, mask=mask)
    room_ids = {int(key) for key in model.rooms}
    known = source.point([255 if value in room_ids else 0 for value in range(256)], "1")
    preserve = ImageChops.logical_or(known, overlaps)
    fills.paste(source, mask=preserve)
    return fills.tobytes()


def render_png(
    model,
    *,
    outlines_only=False,
    size=1000,
    position_unavailable=False,
    position=None,
    trail=(),
    position_status="live",
):
    """Project grid/world overlays into a cropped antialiased image.

    Trail samples carry explicit gaps and uninterpreted activity flags; never
    draw across missing samples/out-of-map points or guess cleaning-mode colors.
    Docked markers use the verified dock, while unavailable/last-known states are
    labeled explicitly rather than pretending a stale position is live.
    """
    from PIL import Image, ImageDraw

    if type(size) is not int or not 128 <= size <= 2048:
        raise ValueError("Preview size must be between 128 and 2048")
    colors = {int(key): color for key, (color, _) in room_colors(model.rooms).items()}
    # Unknown cell classes remain neutral; they are not invented room/zone types.
    palette = [
        component
        for value in range(256)
        for component in colors.get(value, BACKGROUND if value == 0 else (68, 68, 68))
    ]
    floor = Image.frombytes(
        "P", (model.width, model.height), model.raster if outlines_only else display_raster(model)
    )
    floor.putpalette(palette)
    bounds = (
        [point for points in model.contours.values() for point in points]
        + [point for line in model.structural_lines for point in line]
        + ([model.dock] if model.dock is not None else [])
    )
    left = max(0, math.floor(min(x for x, y in bounds)) - 10)
    bottom = max(0, math.floor(min(y for x, y in bounds)) - 10)
    right = min(model.width, math.ceil(max(x for x, y in bounds)) + 11)
    top = min(model.height, math.ceil(max(y for x, y in bounds)) + 11)
    scale = (size - 40) * 0.85 / max(right - left, top - bottom)
    scaled_width = max(1, round((right - left) * scale))
    scaled_height = max(1, round((top - bottom) * scale))
    offset_x, offset_y = (size - scaled_width) // 2, (size - scaled_height) // 2
    supersample = 2
    image = Image.new("RGB", (size * supersample, size * supersample), BACKGROUND)
    if not outlines_only:
        crop = floor.crop((left, bottom, right, top)).transpose(Image.Transpose.FLIP_TOP_BOTTOM)
        crop = crop.convert("RGB")
        target = (scaled_width * supersample, scaled_height * supersample)
        crisp = crop.resize(target, Image.Resampling.NEAREST)
        smooth = crop.resize(target, Image.Resampling.BILINEAR)
        image.paste(
            Image.blend(crisp, smooth, 0.5), (offset_x * supersample, offset_y * supersample)
        )

    def pixel(point):
        x, y = point
        return (
            supersample * (offset_x + (x - left + 0.5) * scaled_width / (right - left)),
            supersample * (offset_y + (top - y - 0.5) * scaled_height / (top - bottom)),
        )

    draw = ImageDraw.Draw(image)
    if not model.structural_lines:
        for points in model.contours.values():
            draw.line(
                [pixel(point) for point in (*points, points[0])],
                fill=(170, 170, 170),
                width=supersample * (1 if outlines_only else 2),
            )
    for line in model.structural_lines:
        draw.line([pixel(point) for point in line], fill=(180, 180, 180), width=2 * supersample)
    previous = None
    if len(trail) > 12_000:
        raise ValueError("Oversized position trail")
    for x, y, mode, break_line in trail:
        if (
            type(x) not in (int, float)
            or type(y) not in (int, float)
            or not math.isfinite(x)
            or not math.isfinite(y)
            or type(mode) is not int
            or type(break_line) is not bool
        ):
            raise ValueError("Invalid position trail")
        grid = ((x - model.origin[0]) / model.resolution, (y - model.origin[1]) / model.resolution)
        if not 0 <= grid[0] < model.width or not 0 <= grid[1] < model.height:
            previous = None
            continue
        point = pixel(grid)
        if previous is not None and not break_line:
            # Activity flags are retained but not assigned unverified meanings.
            draw.line((previous, point), fill=(210, 210, 210), width=2 * supersample)
        previous = point
    if model.dock is not None:
        x, y = pixel(model.dock)
        draw.rectangle(
            (x - 6 * supersample, y - 6 * supersample, x + 6 * supersample, y + 6 * supersample),
            fill=(204, 211, 87),
            outline=(245, 245, 245),
            width=2 * supersample,
        )
    labels = []
    if position_unavailable or position_status == "docked" and model.dock is None:
        labels.append("Position unavailable")
    if position_status == "last_known":
        labels.append("Last known position")
    if position_status == "docked" and model.dock is not None:
        position = (
            model.origin[0] + model.dock[0] * model.resolution,
            model.origin[1] + model.dock[1] * model.resolution,
            0,
        )
    if position is not None:
        x, y, heading = position
        grid = ((x - model.origin[0]) / model.resolution, (y - model.origin[1]) / model.resolution)
        if not all(math.isfinite(v) for v in position) or not (
            0 <= grid[0] < model.width and 0 <= grid[1] < model.height
        ):
            raise ValueError("Live position is outside saved map")
        x, y = pixel(grid)
        draw.ellipse(
            (x - 9 * supersample, y - 9 * supersample, x + 9 * supersample, y + 9 * supersample),
            fill=(240, 240, 240),
            outline=(25, 25, 25),
            width=2 * supersample,
        )
        if position_status != "docked":
            draw.line(
                (
                    x,
                    y,
                    x + math.cos(heading) * 14 * supersample,
                    y - math.sin(heading) * 14 * supersample,
                ),
                fill=(50, 180, 255),
                width=3 * supersample,
            )
    image = image.resize((size, size), Image.Resampling.LANCZOS)
    for label in labels:
        ImageDraw.Draw(image).text((12, 8), label, fill=(245, 245, 245))
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def write_private(path, data):
    """Create an exclusive mode-0600 preview; never overwrite an existing artifact."""
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "wb") as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path, help="completed saved smart-map archive")
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="existing private mode-0700 directory; outputs must not already exist",
    )
    args = parser.parse_args(argv)
    if not args.output_dir.is_dir() or args.output_dir.stat().st_mode & 0o077:
        parser.error("Output directory must be private (mode 0700)")
    info = args.archive.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > 64 * 1024 * 1024:
        parser.error("Input must be a regular archive file no larger than 64 MiB")
    with args.archive.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    maps = list(archive_maps(str(args.archive), info.st_size, digest, smart_only=True))
    if not maps:
        parser.error("Archive has no saved smart maps")
    outputs = []
    for index, (_, _, compressed) in enumerate(maps, 1):
        model = decode_geometry(compressed)
        for style, outlines_only in (("floor", False), ("rooms", True)):
            path = args.output_dir / f"saved-map-{index}-{style}.png"
            outputs.append((path, render_png(model, outlines_only=outlines_only)))
    if any(path.exists() for path, _ in outputs):
        parser.error("Preview output already exists; choose a new private directory")
    for path, data in outputs:
        write_private(path, data)
        print(path)


if __name__ == "__main__":
    main()
