from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from core.agent import (
    LANGCHAIN_IMPORT_ERROR,
    SEARCH_OPTION_LABELS,
    RouteAgent,
    RouteModelError,
)
from core.config import get_settings
from core.route_analysis import build_route_view, build_traffic_signature, summarize_route
from core.tmap_client import TMapClient, TMapError


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="RoadPath")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class LocationData(BaseModel):
    lat: float
    lon: float
    name: str | None = None


class PlaceChoiceData(BaseModel):
    role: str
    candidate: dict[str, Any]


class RouteRequest(BaseModel):
    message: str
    current_location: LocationData | None = None
    place_choice: PlaceChoiceData | None = None
    destination: dict[str, Any] | None = None
    confirmed_start: dict[str, Any] | None = None


class NavigationRerouteRequest(BaseModel):
    current_location: LocationData
    destination: dict[str, Any]
    search_option: int


class TrafficCheckRequest(BaseModel):
    start: dict[str, Any]
    destination: dict[str, Any]
    search_option: int
    baseline_signature: list[dict[str, Any]]


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health() -> dict:
    settings = get_settings()
    langchain_available = LANGCHAIN_IMPORT_ERROR is None
    return {
        "ready": settings.ready and langchain_available,
        "langchain_available": langchain_available,
        "gemini_key": bool(settings.gemini_api_key),
        "tmap_key": bool(settings.tmap_api_key),
    }


def _get_tmap() -> TMapClient:
    settings = get_settings()
    if not settings.tmap_api_key:
        raise HTTPException(status_code=500, detail="TMAP_API_KEY가 설정되지 않았습니다.")
    return TMapClient(settings.tmap_api_key)


def _route_payload(tmap: TMapClient, start: dict[str, Any], end: dict[str, Any], search_option: int) -> dict:
    if search_option not in SEARCH_OPTION_LABELS:
        raise HTTPException(status_code=400, detail="지원하지 않는 경로 탐색 옵션입니다.")

    raw = tmap.search_route(start, end, search_option)
    summary = summarize_route(raw)
    return {
        "search_option": search_option,
        "search_option_label": SEARCH_OPTION_LABELS[search_option],
        "summary": summary,
        "view": build_route_view(raw),
        "traffic_signature": build_traffic_signature(summary),
    }


@app.post("/api/route")
def route(request: RouteRequest) -> dict:
    message = request.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="이동 요청을 입력해 주세요.")

    settings = get_settings()
    if not settings.gemini_api_key:
        raise HTTPException(status_code=500, detail="GEMINI_API_KEY가 설정되지 않았습니다.")
    if not settings.tmap_api_key:
        raise HTTPException(status_code=500, detail="TMAP_API_KEY가 설정되지 않았습니다.")

    current_location = request.current_location.model_dump() if request.current_location else None
    place_choice = request.place_choice.model_dump() if request.place_choice else None

    try:
        tmap = TMapClient(settings.tmap_api_key)
        agent = RouteAgent(tmap, settings.gemini_model)
        return agent.run(
            message,
            current_location=current_location,
            place_choice=place_choice,
            preset_end=request.destination,
            preset_start=request.confirmed_start,
        )
    except RouteModelError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.public_message) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail="경로 탐색 중 오류가 발생했습니다.") from exc


@app.post("/api/navigation/reroute")
def navigation_reroute(request: NavigationRerouteRequest) -> dict:
    try:
        tmap = _get_tmap()
        start = request.current_location.model_dump()
        route_data = _route_payload(tmap, start, request.destination, request.search_option)
        return {
            "status": "ok",
            "start": start,
            "end": request.destination,
            "route": route_data,
        }
    except TMapError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/navigation/traffic-check")
def navigation_traffic_check(request: TrafficCheckRequest) -> dict:
    try:
        tmap = _get_tmap()
        refreshed = _route_payload(tmap, request.start, request.destination, request.search_option)
        signature = refreshed["traffic_signature"]
        return {
            "status": "ok",
            "changed": signature != request.baseline_signature,
            "traffic_signature": signature,
        }
    except TMapError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
