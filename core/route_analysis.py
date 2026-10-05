from __future__ import annotations

from collections import OrderedDict
from math import atan2, cos, radians, sin, sqrt
from typing import Any


TRAFFIC_LABELS = {
    0: "정보없음",
    1: "원활",
    2: "서행",
    3: "지체",
    4: "정체",
}

ROAD_TYPE_LABELS = {
    0: "고속도로",
    1: "자동차전용도로",
    2: "국도",
    3: "국가지원 지방도",
    4: "지방도",
    5: "주요도로1",
    6: "주요도로2",
    7: "주요도로3",
    8: "기타도로1",
    9: "기타도로2",
    10: "페리항로",
    11: "단지내도로(아파트)",
    12: "단지내도로(시장)",
    16: "일반도로",
    20: "번화가링크",
}

TRAFFIC_STATES = tuple(TRAFFIC_LABELS.values())
ATTENTION_STATES = {"서행", "지체", "정체"}


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _normalize_traffic(raw_traffic: Any) -> list[list[Any]]:
    if not isinstance(raw_traffic, list) or not raw_traffic:
        return []

    if len(raw_traffic) >= 4 and not isinstance(raw_traffic[0], (list, tuple)):
        return [raw_traffic]

    return [list(item) for item in raw_traffic if isinstance(item, (list, tuple))]


def _find_total_properties(route_data: dict[str, Any]) -> dict[str, Any]:
    for feature in route_data.get("features", []):
        properties = feature.get("properties", {}) or {}
        if "totalDistance" in properties and "totalTime" in properties:
            return properties
    raise ValueError("TMAP 응답에서 전체 거리/시간 정보를 찾을 수 없습니다.")


def summarize_route(route_data: dict[str, Any]) -> dict[str, Any]:
    total_properties = _find_total_properties(route_data)

    summary: dict[str, Any] = {
        "total_distance_km": round(float(total_properties["totalDistance"]) / 1000, 1),
        "total_time_min": round(float(total_properties["totalTime"]) / 60),
        "traffic_counts": {state: 0 for state in TRAFFIC_STATES},
        "congested_roads": [],
        "traffic_segments": [],
        "sections": [],
    }

    congested_road_names: set[str] = set()

    for feature in route_data.get("features", []):
        geometry = feature.get("geometry", {}) or {}
        properties = feature.get("properties", {}) or {}

        if geometry.get("type") != "LineString":
            continue

        road_name = properties.get("name") or "도로명 없음"
        road_type = _as_int(properties.get("roadType"))
        road_type_name = ROAD_TYPE_LABELS.get(road_type, "기타/확인 안 됨")
        coordinates = geometry.get("coordinates", []) or []
        traffic_records = _normalize_traffic(geometry.get("traffic", []))

        section: dict[str, Any] = {
            "road_name": road_name,
            "road_type": road_type,
            "road_type_name": road_type_name,
            "distance_m": properties.get("distance", 0),
            "time_sec": properties.get("time", 0),
            "traffic": [],
        }

        for item in traffic_records:
            if len(item) < 4:
                continue

            start_index = _as_int(item[0])
            end_index = _as_int(item[1])
            congestion_code = _as_int(item[2])
            speed = item[3]
            state = TRAFFIC_LABELS.get(congestion_code, "알 수 없음")

            start_coord = None
            end_coord = None

            if start_index is not None and 0 <= start_index < len(coordinates):
                start_coord = coordinates[start_index]
            if end_index is not None and 0 <= end_index < len(coordinates):
                end_coord = coordinates[end_index]

            traffic_segment = {
                "road_name": road_name,
                "road_type": road_type,
                "road_type_name": road_type_name,
                "start_index": start_index,
                "end_index": end_index,
                "start_coord": start_coord,
                "end_coord": end_coord,
                "state": state,
                "speed_kmh": speed,
            }

            section["traffic"].append(traffic_segment)
            summary["traffic_segments"].append(traffic_segment)

            if state in summary["traffic_counts"]:
                summary["traffic_counts"][state] += 1

            if state in {"지체", "정체"} and road_name not in congested_road_names:
                congested_road_names.add(road_name)
                summary["congested_roads"].append(road_name)

        summary["sections"].append(section)

    return summary


def build_route_view(route_data: dict[str, Any]) -> dict[str, Any]:
    paths: list[list[list[float]]] = []
    traffic_segments: list[dict[str, Any]] = []
    guides: list[dict[str, Any]] = []
    traffic_segment_index = 0

    for feature in route_data.get("features", []):
        geometry = feature.get("geometry", {}) or {}
        properties = feature.get("properties", {}) or {}
        geometry_type = geometry.get("type")

        if geometry_type == "Point":
            coordinates = geometry.get("coordinates", []) or []
            if isinstance(coordinates, list) and len(coordinates) >= 2:
                guides.append(
                    {
                        "index": properties.get("index"),
                        "point_index": properties.get("pointIndex"),
                        "name": properties.get("name") or "",
                        "description": properties.get("description") or "",
                        "next_road_name": properties.get("nextRoadName") or "",
                        "turn_type": _as_int(properties.get("turnType")),
                        "point_type": properties.get("pointType") or "",
                        "coordinates": coordinates,
                    }
                )
            continue

        if geometry_type != "LineString":
            continue

        coordinates = geometry.get("coordinates", []) or []
        path_index = len(paths)
        if coordinates:
            paths.append(coordinates)

        road_name = properties.get("name") or "도로명 없음"
        road_type = _as_int(properties.get("roadType"))
        road_type_name = ROAD_TYPE_LABELS.get(road_type, "기타/확인 안 됨")

        for item in _normalize_traffic(geometry.get("traffic", [])):
            if len(item) < 4:
                continue

            start_index = _as_int(item[0])
            end_index = _as_int(item[1])
            congestion_code = _as_int(item[2])

            if (
                start_index is None
                or end_index is None
                or start_index < 0
                or end_index < start_index
                or end_index >= len(coordinates)
            ):
                continue

            segment_coordinates = coordinates[start_index : end_index + 1]
            if len(segment_coordinates) < 2:
                continue

            traffic_segments.append(
                {
                    "segment_index": traffic_segment_index,
                    "path_index": path_index,
                    "road_name": road_name,
                    "road_type": road_type,
                    "road_type_name": road_type_name,
                    "start_index": start_index,
                    "end_index": end_index,
                    "state": TRAFFIC_LABELS.get(congestion_code, "알 수 없음"),
                    "speed_kmh": item[3],
                    "coordinates": segment_coordinates,
                }
            )
            traffic_segment_index += 1

    return {
        "paths": paths,
        "traffic_segments": traffic_segments,
        "guides": guides,
    }


def build_traffic_signature(summary: dict[str, Any]) -> list[dict[str, Any]]:
    """Create an exact, compact signature of TMAP traffic states.

    Speed values are intentionally excluded. The signature changes only when
    the ordered road/state structure returned by TMAP changes, so small speed
    fluctuations do not by themselves trigger an Agent re-evaluation.
    """

    return [
        {
            "road_name": segment.get("road_name"),
            "road_type": segment.get("road_type"),
            "state": segment.get("state"),
        }
        for segment in summary.get("traffic_segments", [])
    ]


def build_agent_route_summary(summary: dict[str, Any]) -> dict[str, Any]:
    """Return only the route data needed for LLM judgment."""

    road_groups: OrderedDict[tuple[str, Any], dict[str, Any]] = OrderedDict()

    for segment in summary.get("traffic_segments", []):
        state = segment.get("state")
        if state not in ATTENTION_STATES:
            continue

        key = (segment.get("road_name"), segment.get("road_type"))
        group = road_groups.setdefault(
            key,
            {
                "road_name": segment.get("road_name"),
                "road_type": segment.get("road_type"),
                "road_type_name": segment.get("road_type_name"),
                "traffic_counts": {state_name: 0 for state_name in ATTENTION_STATES},
                "speeds_kmh": [],
            },
        )

        group["traffic_counts"][state] += 1
        speed = segment.get("speed_kmh")
        if isinstance(speed, (int, float)):
            group["speeds_kmh"].append(speed)

    road_summary = []
    for group in road_groups.values():
        speeds = group.pop("speeds_kmh")
        group["min_speed_kmh"] = min(speeds) if speeds else None
        group["max_speed_kmh"] = max(speeds) if speeds else None
        road_summary.append(group)

    attention_segments = [
        {
            "road_name": segment.get("road_name"),
            "road_type_name": segment.get("road_type_name"),
            "state": segment.get("state"),
            "speed_kmh": segment.get("speed_kmh"),
        }
        for segment in summary.get("traffic_segments", [])
        if segment.get("state") in ATTENTION_STATES
    ]

    return {
        "total_distance_km": summary.get("total_distance_km"),
        "total_time_min": summary.get("total_time_min"),
        "traffic_counts": summary.get("traffic_counts", {}),
        "road_traffic_summary": road_summary,
        "attention_segments": attention_segments,
    }


EARTH_RADIUS_M = 6371000.0
SEVERE_TRAFFIC_STATES = {"지체", "정체"}


def _haversine_meters(a: list[float], b: list[float]) -> float:
    lon1, lat1 = float(a[0]), float(a[1])
    lon2, lat2 = float(b[0]), float(b[1])
    phi1 = radians(lat1)
    phi2 = radians(lat2)
    d_phi = radians(lat2 - lat1)
    d_lambda = radians(lon2 - lon1)
    value = sin(d_phi / 2) ** 2 + cos(phi1) * cos(phi2) * sin(d_lambda / 2) ** 2
    return 2 * EARTH_RADIUS_M * atan2(sqrt(value), sqrt(max(0.0, 1 - value)))


def _dedupe_coordinates(coordinates: list[list[float]]) -> list[list[float]]:
    result: list[list[float]] = []
    for coordinate in coordinates:
        if not isinstance(coordinate, list) or len(coordinate) < 2:
            continue
        point = [float(coordinate[0]), float(coordinate[1])]
        if not result or result[-1] != point:
            result.append(point)
    return result


def _build_congestion_block(block_segments: list[dict[str, Any]]) -> dict[str, Any] | None:
    coordinates: list[list[float]] = []
    for segment in block_segments:
        coordinates.extend(segment.get("coordinates", []) or [])
    coordinates = _dedupe_coordinates(coordinates)

    if len(coordinates) < 2:
        return None

    road_names: list[str] = []
    states: list[str] = []
    for segment in block_segments:
        road_name = segment.get("road_name") or "도로명 없음"
        if road_name not in road_names:
            road_names.append(road_name)
        state = segment.get("state")
        if state and state not in states:
            states.append(state)

    return {
        "segments": list(block_segments),
        "coordinates": coordinates,
        "start_coord": coordinates[0],
        "end_coord": coordinates[-1],
        "midpoint_coord": coordinates[len(coordinates) // 2],
        "road_names": road_names,
        "states": states,
    }


def _traffic_segments_are_contiguous(
    previous: dict[str, Any],
    current: dict[str, Any],
) -> bool:
    """Return True when two TMAP traffic fragments are actually adjacent on the route."""

    previous_path = previous.get("path_index")
    current_path = current.get("path_index")
    previous_end = previous.get("end_index")
    current_start = current.get("start_index")

    if (
        previous_path == current_path
        and isinstance(previous_end, int)
        and isinstance(current_start, int)
        and current_start <= previous_end + 1
    ):
        return True

    previous_coordinates = _dedupe_coordinates(previous.get("coordinates", []) or [])
    current_coordinates = _dedupe_coordinates(current.get("coordinates", []) or [])
    return bool(
        previous_coordinates
        and current_coordinates
        and previous_coordinates[-1] == current_coordinates[0]
    )


def find_congestion_blocks(route_view: dict[str, Any]) -> list[dict[str, Any]]:
    """Return physically consecutive TMAP traffic blocks marked 지체/정체 in route order."""

    blocks: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []

    def flush_current() -> None:
        nonlocal current
        if current:
            block = _build_congestion_block(current)
            if block is not None:
                blocks.append(block)
            current = []

    for segment in route_view.get("traffic_segments", []):
        if segment.get("state") not in SEVERE_TRAFFIC_STATES:
            flush_current()
            continue

        if current and not _traffic_segments_are_contiguous(current[-1], segment):
            flush_current()
        current.append(segment)

    flush_current()
    return blocks


def find_first_congestion_block(route_view: dict[str, Any]) -> dict[str, Any] | None:
    """Return the first consecutive TMAP traffic block marked 지체/정체."""

    blocks = find_congestion_blocks(route_view)
    return blocks[0] if blocks else None


def build_bypass_waypoints(congestion_block: dict[str, Any]) -> list[dict[str, Any]]:
    """Build two geometry-derived side waypoints around the congestion block.

    No fixed detour distance is introduced. The lateral distance is derived from
    the actual congestion geometry: the greater straight-line distance from the
    block midpoint to either block boundary.
    """

    start = congestion_block.get("start_coord")
    end = congestion_block.get("end_coord")
    midpoint = congestion_block.get("midpoint_coord")
    if not start or not end or not midpoint:
        return []

    lon0, lat0 = float(midpoint[0]), float(midpoint[1])
    cos_lat = cos(radians(lat0))
    if cos_lat == 0:
        return []

    def to_local(point: list[float]) -> tuple[float, float]:
        lon, lat = float(point[0]), float(point[1])
        x = EARTH_RADIUS_M * radians(lon - lon0) * cos_lat
        y = EARTH_RADIUS_M * radians(lat - lat0)
        return x, y

    start_x, start_y = to_local(start)
    end_x, end_y = to_local(end)
    dx = end_x - start_x
    dy = end_y - start_y
    direction_length = sqrt(dx * dx + dy * dy)
    if direction_length == 0:
        return []

    offset_m = max(_haversine_meters(midpoint, start), _haversine_meters(midpoint, end))
    if offset_m == 0:
        return []

    perpendicular_x = -dy / direction_length
    perpendicular_y = dx / direction_length

    result: list[dict[str, Any]] = []
    for side, sign in (("left", 1.0), ("right", -1.0)):
        x = perpendicular_x * offset_m * sign
        y = perpendicular_y * offset_m * sign
        lon = lon0 + (x / (EARTH_RADIUS_M * cos_lat)) * 180.0 / 3.141592653589793
        lat = lat0 + (y / EARTH_RADIUS_M) * 180.0 / 3.141592653589793
        result.append({"side": side, "lon": lon, "lat": lat})

    return result


def _edge_key(a: list[float], b: list[float]) -> tuple[tuple[float, float], tuple[float, float]]:
    first = (float(a[0]), float(a[1]))
    second = (float(b[0]), float(b[1]))
    return (first, second) if first <= second else (second, first)


def route_crosses_congestion_block(
    candidate_view: dict[str, Any],
    congestion_block: dict[str, Any],
) -> bool | None:
    """True: 재통과, False: 회피 확인, None: 검증 불가.

    기존 좌표 edge 일치 비교는 유지한다. 좌표 부족을 회피 성공으로 처리하지 않는다.
    """
    try:
        block_coordinates = _dedupe_coordinates(congestion_block.get("coordinates", []) or [])
        paths = candidate_view.get("paths", []) or []
        candidate_paths = [_dedupe_coordinates(path or []) for path in paths]
        if len(block_coordinates) < 2 or not candidate_paths:
            return None
        if any(len(path) < 2 for path in candidate_paths):
            return None
        from math import isfinite
        if not all(isfinite(value) for path in [block_coordinates, *candidate_paths]
                   for point in path for value in point):
            return None
    except (TypeError, ValueError, OverflowError):
        return None

    blocked_edges = {
        _edge_key(block_coordinates[index], block_coordinates[index + 1])
        for index in range(len(block_coordinates) - 1)
    }
    for coordinates in candidate_paths:
        for index in range(len(coordinates) - 1):
            if _edge_key(coordinates[index], coordinates[index + 1]) in blocked_edges:
                return True
    return False


def extract_route_detour_waypoint(
    candidate_view: dict[str, Any],
    congestion_block: dict[str, Any],
) -> dict[str, float] | None:
    """Pick a waypoint that lies on the TMAP-generated detour route itself.

    The candidate section between the route points nearest the congestion start
    and end is inspected, then the point farthest from the blocked geometry is
    returned.  No fixed detour distance or road threshold is introduced.
    """

    route_coordinates: list[list[float]] = []
    for path in candidate_view.get("paths", []):
        for point in _dedupe_coordinates(path or []):
            if not route_coordinates or route_coordinates[-1] != point:
                route_coordinates.append(point)

    block_coordinates = _dedupe_coordinates(congestion_block.get("coordinates", []) or [])
    start = congestion_block.get("start_coord")
    end = congestion_block.get("end_coord")
    if len(route_coordinates) < 2 or not block_coordinates or not start or not end:
        return None

    start_index = min(
        range(len(route_coordinates)),
        key=lambda index: _haversine_meters(route_coordinates[index], start),
    )
    end_index = min(
        range(len(route_coordinates)),
        key=lambda index: _haversine_meters(route_coordinates[index], end),
    )
    lower, upper = sorted((start_index, end_index))
    section = route_coordinates[lower : upper + 1]
    if not section:
        return None

    def distance_from_block(point: list[float]) -> float:
        return min(_haversine_meters(point, blocked) for blocked in block_coordinates)

    selected = max(section, key=distance_from_block)
    return {"lon": float(selected[0]), "lat": float(selected[1])}
