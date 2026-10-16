"""Serve revisioned private PNGs from the robot-scoped archive catalog.

The HTTP handler authenticates before calling image. Map/version/digest all
must match current metadata: stale image URLs return no image, not an unrelated
new revision. Cache keys include archive stat/digest identity and overlay state.
The DB pruning read lock must cover decoding/rendering, not just catalog lookup.
No cloud map download/restore protocol is implemented by these preview images.
"""

from functools import lru_cache

from roomba_cloud.maps.preview import decode_geometry, render_png
from roomba_cloud.maps.catalog import archive_maps, map_catalog


class MapImageError(ValueError):
    """A safe processing stage, never private geometry or filesystem data."""

    def __init__(self, stage):
        super().__init__(stage)
        self.stage = stage


def descriptors(maps):
    """Expose only public map identity/version/revision, never private archive paths."""
    return [
        {"id": map_id, "version": item["version"], "revision": item["archive"][3]}
        for map_id, item in sorted(maps.items())
    ]


@lru_cache(maxsize=4)
def geometry(path, size, modified, digest, map_id, version):
    """Decode the exact archive member; modified participates in cache invalidation."""
    for identifier, candidate, compressed in archive_maps(path, size, digest):
        if identifier == map_id and candidate == version:
            try:
                return decode_geometry(compressed)
            except ValueError as exc:
                raise MapImageError("geometry_decode") from exc
    raise ValueError("Requested map is absent from its archive")


@lru_cache(maxsize=8)
def png(archive, map_id, version, kind, position=None, trail=(), status="unavailable"):
    """Cache renders including pose/trail/status so overlays cannot freeze behind a digest."""
    model = geometry(*archive, map_id, version)
    try:
        return render_png(
            model,
            position_unavailable=kind == "live" and status == "unavailable",
            position=position,
            trail=trail,
            position_status=status,
        )
    except ValueError as exc:
        raise MapImageError("png_render") from exc


def image(shadow_db, upload_db, robot_id, map_id, version, revision, kind):
    """Resolve a current room/live image, keeping source and overlay coordinate systems intact."""
    if kind not in ("room", "live"):
        raise ValueError("Unknown map image kind")
    # Keep the upload read lock through rendering, not just catalog resolution.
    # Pruning commits its tombstone before unlinking any archive.
    with map_catalog(shadow_db, upload_db, robot_id) as maps:
        item = maps.get(map_id)
        if item is None or item["version"] != version or item["archive"][3] != revision:
            return None
        from roomba_cloud.maps.live_position import overlays

        overlay = overlays(shadow_db, robot_id).get(map_id, {}) if kind == "live" else {}
        pose = overlay.get("position")
        position = (pose["x"], pose["y"], pose["heading"]) if pose else None
        trail = tuple(tuple(point) for point in overlay.get("points", []))
        return png(
            item["archive"],
            map_id,
            item["archive_version"],
            kind,
            position,
            trail,
            overlay.get("status", "unavailable"),
        )
