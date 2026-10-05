"""OSM 출발 분기 후보. 통행 가능성 확정은 TMAP 반환 경로로 판단한다."""
from __future__ import annotations

import json
import math
import os
from functools import lru_cache
from urllib.request import Request, urlopen

# 도로 수집 범위/통신 대기 설정이며 경로 선택 기준이 아니다.
RADIUS = int(os.getenv('ROADPATH_BRANCH_RADIUS_M', '300'))
TIMEOUT = float(os.getenv('ROADPATH_ROAD_TIMEOUT_S', '10'))
ENDPOINT = 'https://overpass-api.de/api/interpreter'
DRIVABLE = {'motorway', 'trunk', 'primary', 'secondary', 'tertiary',
            'unclassified', 'residential', 'living_street', 'service',
            'motorway_link', 'trunk_link', 'primary_link', 'secondary_link', 'tertiary_link'}


def xy(point, origin):
    return ((point[0] - origin[0]) * 111320 * math.cos(math.radians(origin[1])),
            (point[1] - origin[1]) * 111320)


def projection(point, a, b):
    ax, ay = xy(a, point)
    bx, by = xy(b, point)
    dx, dy = bx-ax, by-ay
    t = max(0, min(1, -(ax*dx+ay*dy)/(dx*dx+dy*dy))) if dx*dx+dy*dy else 0
    return math.hypot(ax+t*dx, ay+t*dy), t


@lru_cache(maxsize=32)
def fetch_roads(lat, lon, radius):
    # 도로 형상과 해당 도로의 통행 제한을 함께 조회한다. 좌표는 반올림하지 않는다.
    query = (f'[out:json][timeout:{max(1, int(TIMEOUT))}];'
             f'way(around:{radius},{lat},{lon})[highway]->.roads;'
             '(.roads;rel(bw.roads)[type=restriction];);out body;>;out skel qt;')
    request = Request(ENDPOINT, data=query.encode(),
                      headers={'Content-Type': 'text/plain', 'User-Agent': 'RoadPath/1.0'})
    with urlopen(request, timeout=TIMEOUT) as response:
        result = json.load(response)
    if result.get('remark') or not isinstance(result.get('elements'), list):
        raise ValueError('Incomplete road response')
    return result


def extract_candidates(data, start, base_view, radius=RADIUS):
    origin = (float(start['lon']), float(start['lat']))
    elements = data['elements']
    nodes = {e['id']: (e['lon'], e['lat']) for e in elements if e['type'] == 'node'}
    ways = {}
    graph = {}
    edges = []
    for e in elements:
        if e['type'] != 'way':
            continue
        tags = e.get('tags', {})
        access = next((tags[k] for k in ('motorcar', 'motor_vehicle', 'vehicle', 'access') if k in tags), 'yes')
        if tags.get('highway') not in DRIVABLE or access not in {'yes', 'permissive', 'designated'}:
            continue
        if any(':conditional' in k for k in tags):
            continue  # 시간/차종 조건을 해석하지 못한 도로는 후보에서 제외
        one = tags.get('oneway', 'yes' if tags.get('junction') == 'roundabout' or tags.get('highway') == 'motorway' else 'no')
        if one not in {'yes', '1', 'true', '-1', 'no', '0', 'false'}:
            continue
        ways[e['id']] = e
        for a, b in zip(e['nodes'], e['nodes'][1:]):
            if a not in nodes or b not in nodes or nodes[a] == nodes[b]:
                continue
            edges.append((a, b, e['id']))
            directions = [(a, b)] if one in {'yes', '1', 'true'} else [(b, a)] if one == '-1' else [(a, b), (b, a)]
            for u, v in directions:
                graph.setdefault(u, []).append((v, e['id']))
    if not edges:
        return {}
    # 같은 좌표에서 시작한다. OSM 매칭은 후보 생성용이며 실제 출발점은 변경하지 않는다.
    a, b, wid = min(edges, key=lambda e: projection(origin, nodes[e[0]], nodes[e[1]])[0])
    if projection(origin, nodes[a], nodes[b])[0] > radius:
        return {}
    restrictions = []
    blocked_ways = set()
    for e in elements:
        if e['type'] != 'relation' or e.get('tags', {}).get('type') != 'restriction':
            continue
        members = e.get('members', [])
        frm = [m['ref'] for m in members if m['role'] == 'from']
        to = [m['ref'] for m in members if m['role'] == 'to']
        via = [m for m in members if m['role'] == 'via']
        tag = e.get('tags', {})
        rule = tag.get('restriction:motorcar', tag.get('restriction:motor_vehicle', tag.get('restriction', '')))
        if len(frm) != 1 or len(to) != 1 or len(via) != 1 or via[0]['type'] != 'node' or any('conditional' in k for k in tag):
            blocked_ways.update(frm)  # 복잡한 제한은 낙관적으로 허용하지 않는다.
            continue
        restrictions.append((frm[0], via[0]['ref'], to[0], rule))

    def allowed(prev, node, outgoing):
        if prev in blocked_ways:
            return False
        for f, v, t, rule in restrictions:
            if f == prev and v == node:
                if rule.startswith('no_') and t == outgoing:
                    return False
                if rule.startswith('only_') and t != outgoing:
                    return False
        return True

    base_segments = [(tuple(p), tuple(q)) for path in base_view.get('paths', []) for p, q in zip(path, path[1:])]
    definitions = {}

    def add(u, v, way):
        p, q = nodes[u], nodes[v]
        # 같은 OSM/TMAP 좌표 구간인 경우만 사전 제외. 근접 도로를 임의로 동일시하지 않는다.
        if (p, q) in base_segments:
            return
        mid = {'lon': (p[0]+q[0])/2, 'lat': (p[1]+q[1])/2}
        key = f'departure_{way}_{u}_{v}'
        definitions[key] = {'waypoints': [mid], 'target_indexes': [],
                            'junction': list(p),
                            'road_name': ways[way].get('tags', {}).get('name', '이름 없는 도로'),
                            'source': 'OSM', 'verification': 'TMAP 조회 전 · 통행 및 개선 미확정'}

    # 출발 도로의 허용 방향별로 첫 분기까지만 따라간다. 이후 분기를 재귀 확장하지 않는다.
    seeds = []
    if (b, wid) in graph.get(a, []): seeds.append((a, b, wid))
    if (a, wid) in graph.get(b, []): seeds.append((b, a, wid))
    for previous, current, incoming in seeds:
        first = (previous, current, incoming)
        visited = set()
        while (previous, current, incoming) not in visited:
            visited.add((previous, current, incoming))
            if math.hypot(*xy(nodes[current], origin)) > radius:
                add(*first)
                break
            choices = [(v, w) for v, w in graph.get(current, [])
                       if v != previous and allowed(incoming, current, w)]
            if len(choices) != 1:
                for v, w in choices:
                    add(current, v, w)
                break
            previous, current, incoming = current, choices[0][0], choices[0][1]
    return definitions


def discover(start, base_view):
    try:
        lat, lon = float(start['lat']), float(start['lon'])
        if not (-90 <= lat <= 90 and -180 <= lon <= 180) or RADIUS <= 0 or TIMEOUT <= 0:
            raise ValueError('Invalid input')
        candidates = extract_candidates(fetch_roads(lat, lon, RADIUS), start, base_view)
        return candidates, f'출발 분기 후보 {len(candidates)}개 · OSM 기준, TMAP 검증 전'
    except Exception:
        # 인증키나 외부 서버 원문은 사용자에게 노출하지 않는다.
        return {}, '주변 도로 조회 실패 · 기존 경로 탐색을 유지합니다.'

def discover_before_congestion(base_view):
    from .route_analysis import find_congestion_blocks

    segments = []
    travelled = 0.0

    for path in base_view.get("paths", []):
        for a, b in zip(path, path[1:]):
            length = math.hypot(*xy(b, a))
            if length:
                segments.append((a, b, travelled, length))
                travelled += length

    if not segments:
        return {}, "정체 진입 전 탐색 대상 없음"

    result, notices = {}, []

    for index, block in enumerate(
        find_congestion_blocks(base_view)[:5], 1
    ):
        entry = block["start_coord"]

        # 정체 시작점이 경로의 꼭짓점과 정확히 같지 않아도
        # 경로 선분 위 위치를 찾는다.
        matches = [
            (projection(entry, a, b), offset, length)
            for a, b, offset, length in segments
        ]
        (_, fraction), offset, length = min(
            matches, key=lambda item: item[0][0]
        )
        stop = offset + fraction * length

        if stop <= 0:
            continue

        begin = max(0.0, stop - RADIUS)
        anchors = []

        # 기존 300m 범위에 포함되는 경로 지점들을 이용한다.
        for a, b, offset, length in segments:
            if offset >= stop or offset + length <= begin:
                continue

            fraction = max(0.0, (begin - offset) / length)
            anchors.append([
                a[0] + (b[0] - a[0]) * fraction,
                a[1] + (b[1] - a[1]) * fraction,
            ])

        try:
            # 같은 정체 구간의 도로 데이터는 한 번만 조회한다.
            data = fetch_roads(
                float(entry[1]), float(entry[0]), RADIUS
            )

            found = {}
            for anchor in anchors:
                found.update(extract_candidates(
                    data,
                    {"lon": anchor[0], "lat": anchor[1]},
                    base_view,
                ))

            count = 0

            for key, value in found.items():
                junction = value.get("junction")
                if not junction:
                    continue

                positions = [
                    (projection(junction, a, b), offset, length)
                    for a, b, offset, length in segments
                ]
                (_, fraction), offset, length = min(
                    positions, key=lambda item: item[0][0]
                )
                position = offset + fraction * length

                if not begin <= position < stop:
                    continue

                result[f"before_{index}_{key}"] = {
                    **value,
                    "target_indexes": [index],
                    "verification": (
                        "정체 이전 주변 분기 후보"
                        " · 실제 분기 이용 확인 안 됨"
                    ),
                }
                count += 1

            notices.append(f"정체 블록 {index}: 후보 {count}개")

        except Exception:
            notices.append(f"정체 블록 {index}: 도로 조회 실패")

    return (
        result,
        " / ".join(notices) or "정체 진입 전 탐색 대상 없음",
    )