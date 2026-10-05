from __future__ import annotations

import re
from typing import Any

import requests


class TMapError(RuntimeError):
    """Raised when a TMAP request or response cannot be used."""


class TMapAmbiguousPlace(TMapError):
    def __init__(self, keyword: str, candidates: list[dict[str, Any]]):
        super().__init__(f"여러 지역에서 장소가 검색되었습니다: {keyword}")
        self.keyword = keyword
        self.candidates = candidates


class TMapClient:
    POI_URL = "https://apis.openapi.sk.com/tmap/pois"
    ROUTE_URL = "https://apis.openapi.sk.com/tmap/routes"

    def __init__(self, app_key: str):
        if not app_key:
            raise ValueError("TMAP_API_KEY를 찾을 수 없습니다.")
        self.app_key = app_key
        self.session = requests.Session()

    @staticmethod
    def _normalize_name(value: str) -> str:
        value = re.sub(r"\[[^\]]*\]", "", value or "")
        value = re.sub(r"\([^)]*\)", "", value)
        return re.sub(r"\s+", "", value).strip().lower()

    @staticmethod
    def _poi_to_place(keyword: str, poi: dict[str, Any]) -> dict[str, Any]:
        address_parts = [
            poi.get("upperAddrName"),
            poi.get("middleAddrName"),
            poi.get("lowerAddrName"),
            poi.get("detailAddrName"),
        ]
        address = " ".join(str(part).strip() for part in address_parts if part)
        road_address = (poi.get("roadName") or "").strip()

        return {
            "query": keyword,
            "id": poi.get("id"),
            "name": poi.get("name") or poi.get("orgName") or keyword,
            "org_name": poi.get("orgName") or poi.get("name") or keyword,
            "address": road_address or address,
            "region": (poi.get("upperAddrName") or "").strip(),
            "lat": float(poi["frontLat"]),
            "lon": float(poi["frontLon"]),
        }

    def search_places(self, keyword: str) -> list[dict[str, Any]]:
        keyword = (keyword or "").strip()
        if not keyword:
            raise TMapError("장소 검색어가 비어 있습니다.")

        headers = {
            "Accept": "application/json",
            "appKey": self.app_key,
        }
        params = {
            "version": "1",
            "searchKeyword": keyword,
            "searchType": "all",
            "page": "1",
            "resCoordType": "WGS84GEO",
            "reqCoordType": "WGS84GEO",
        }

        try:
            response = self.session.get(self.POI_URL, headers=headers, params=params)
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as exc:
            raise TMapError(f"TMAP 장소 검색 요청 실패: {exc}") from exc
        except ValueError as exc:
            raise TMapError("TMAP 장소 검색 응답을 JSON으로 해석할 수 없습니다.") from exc

        try:
            pois = data["searchPoiInfo"]["pois"]["poi"]
        except (KeyError, TypeError) as exc:
            raise TMapError("TMAP 장소 검색 응답 형식을 확인할 수 없습니다.") from exc

        if not pois:
            raise TMapError(f"장소 검색 결과가 없습니다: {keyword}")
        if isinstance(pois, dict):
            pois = [pois]

        places: list[dict[str, Any]] = []
        for poi in pois:
            try:
                places.append(self._poi_to_place(keyword, poi))
            except (KeyError, TypeError, ValueError):
                continue

        if not places:
            raise TMapError("TMAP 장소 검색 결과의 좌표를 확인할 수 없습니다.")
        return places

    def search_place(self, keyword: str) -> dict[str, Any]:
        places = self.search_places(keyword)
        if len(places) == 1:
            return places[0]

        normalized_keyword = self._normalize_name(keyword)
        name_matches = [
            place
            for place in places
            if self._normalize_name(place.get("name", "")) == normalized_keyword
            or self._normalize_name(place.get("org_name", "")) == normalized_keyword
        ]
        candidates = name_matches or places
        regions = {place.get("region") for place in candidates if place.get("region")}

        if len(regions) > 1:
            unique_candidates: list[dict[str, Any]] = []
            seen: set[tuple[Any, ...]] = set()
            for place in candidates:
                key = (
                    place.get("name"),
                    place.get("address"),
                    place.get("lat"),
                    place.get("lon"),
                )
                if key in seen:
                    continue
                seen.add(key)
                unique_candidates.append(place)
            raise TMapAmbiguousPlace(keyword, unique_candidates)

        return candidates[0]

    def search_route(
        self,
        start: dict[str, Any],
        end: dict[str, Any],
        search_option: int = 0,
        pass_list: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "appKey": self.app_key,
        }
        params = {"version": "1"}
        body = {
            "startX": start["lon"],
            "startY": start["lat"],
            "endX": end["lon"],
            "endY": end["lat"],
            "reqCoordType": "WGS84GEO",
            "resCoordType": "WGS84GEO",
            "sort": "index",
            "trafficInfo": "Y",
            "searchOption": search_option,
        }

        if pass_list:
            if len(pass_list) > 5:
                raise TMapError("TMAP 경유지는 최대 5개까지 지정할 수 있습니다.")

            encoded_points: list[str] = []
            for point in pass_list:
                try:
                    lon = float(point["lon"])
                    lat = float(point["lat"])
                except (KeyError, TypeError, ValueError) as exc:
                    raise TMapError("경유지 좌표를 확인할 수 없습니다.") from exc
                encoded_points.append(f"{lon},{lat}")

            body["passList"] = "_".join(encoded_points)

        try:
            response = self.session.post(
                self.ROUTE_URL,
                headers=headers,
                params=params,
                json=body,
            )
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as exc:
            raise TMapError(f"TMAP 경로 탐색 요청 실패: {exc}") from exc
        except ValueError as exc:
            raise TMapError("TMAP 경로 탐색 응답을 JSON으로 해석할 수 없습니다.") from exc

        if not isinstance(data, dict) or not data.get("features"):
            raise TMapError("TMAP 경로 탐색 결과가 비어 있습니다.")

        return data
