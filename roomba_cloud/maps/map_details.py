"""Build Matter map metadata only from complete, current robot map archives."""

import json

from roomba_cloud.maps.catalog import IDENTIFIER, map_catalog
from roomba_cloud.shadow_service import finite_float, reject_constant

MAX_REQUEST_BYTES = 64 * 1024
MAX_MAPS_PER_REQUEST = 10
MAX_REQUEST_ID_LENGTH = 128


class MapDetailsError(ValueError):
    """A safe request or availability failure that can be logged without payload data."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise MapDetailsError("duplicate_json_key")
        result[key] = value
    return result


def _parse_request(payload):
    if not isinstance(payload, bytes) or len(payload) > MAX_REQUEST_BYTES:
        raise MapDetailsError("invalid_request_size")
    try:
        request = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=reject_constant,
            parse_float=finite_float,
        )
    except MapDetailsError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise MapDetailsError("invalid_request_json") from exc
    if not isinstance(request, dict):
        raise MapDetailsError("request_must_be_an_object")

    request_id = request.get("request_id")
    valid_request_id = (
        isinstance(request_id, str)
        and 0 < len(request_id) <= MAX_REQUEST_ID_LENGTH
        and not any(ord(char) < 32 for char in request_id)
    ) or (type(request_id) is int and 0 <= request_id <= 2**63 - 1)
    map_ids = request.get("p2map_ids")
    if (
        not valid_request_id
        or not isinstance(map_ids, list)
        or not 1 <= len(map_ids) <= MAX_MAPS_PER_REQUEST
        or any(
            not isinstance(map_id, str) or not IDENTIFIER.fullmatch(map_id) for map_id in map_ids
        )
        or len(set(map_ids)) != len(map_ids)
    ):
        raise MapDetailsError("invalid_request_schema")
    return request_id, map_ids


def respond(shadow_db, upload_db, topic, payload, robot_id):
    """Return a map-details response only when every requested current map is available."""
    request_topic = f"v011-irbthbu/things/{robot_id}/mapdetails/req"
    if topic != request_topic:
        return None
    if shadow_db is None or upload_db is None:
        raise MapDetailsError("map_storage_not_configured")

    request_id, map_ids = _parse_request(payload)
    with map_catalog(shadow_db, upload_db, robot_id, version_matched_only=True) as maps:
        if any(map_id not in maps for map_id in map_ids):
            raise MapDetailsError("requested_maps_not_fully_available")

        response_maps = []
        for map_id in map_ids:
            item = maps[map_id]
            if item["archive_version"] != item["version"] or not item["rooms"]:
                raise MapDetailsError("requested_maps_not_fully_available")
            regions = [
                {"region_id": region_id, "region_type": "rid", "name": name}
                for region_id, name in sorted(
                    item["rooms"].items(), key=lambda entry: int(entry[0])
                )
            ]
            response_maps.append(
                {
                    "p2map_id": map_id,
                    "p2mapv_id": item["version"],
                    "regions": regions,
                }
            )

    return (
        f"v011-irbthbu/things/{robot_id}/mapdetails/resp",
        {"request_id": request_id, "maps": response_maps},
    )
