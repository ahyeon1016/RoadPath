from __future__ import annotations

import json
from .departure_branches import (
    discover as discover_departure_branches,
    discover_before_congestion,
)
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field

from .route_analysis import (
    build_agent_route_summary,
    build_route_view,
    build_traffic_signature,
    extract_route_detour_waypoint,
    find_congestion_blocks,
    route_crosses_congestion_block,
    summarize_route,
)
from .tmap_client import TMapAmbiguousPlace, TMapClient, TMapError

try:
    from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
    from langchain_core.tools import tool
    from langchain_google_genai import ChatGoogleGenerativeAI
except ImportError as exc:
    HumanMessage = SystemMessage = ToolMessage = None
    tool = None
    ChatGoogleGenerativeAI = None
    LANGCHAIN_IMPORT_ERROR = exc
else:
    LANGCHAIN_IMPORT_ERROR = None


SEARCH_OPTION_LABELS = {
    0: "교통최적+추천",
    1: "교통최적+무료우선",
    2: "교통최적+최소시간",
    3: "교통최적+초보",
    4: "교통최적+고속도로우선",
    10: "최단거리+유/무료",
    19: "교통최적+어린이보호구역 회피",
}

# 최초 전략이 실패했을 때 사용자 허용 범위 내에서 재탐색 전략을 최대 두 번 더 시도한다.
MAX_ALTERNATIVE_PLAN_ATTEMPTS = 3


class RouteModelError(RuntimeError):
    def __init__(self, public_message: str, status_code: int = 503):
        super().__init__(public_message)
        self.public_message = public_message
        self.status_code = status_code


class PrepareBaseRouteInput(BaseModel):
    start_keyword: str = Field(description="사용자 요청에 실제로 포함된 출발지 장소명")
    end_keyword: str = Field(description="사용자 요청에 실제로 포함된 도착지 장소명")


class PrepareCurrentRouteInput(BaseModel):
    end_keyword: str = Field(description="사용자가 요청한 도착지 장소명")


class FindPlaceInput(BaseModel):
    role: Literal["start", "end"] = Field(description="start는 출발지, end는 도착지")
    keyword: str = Field(description="사용자 요청에 실제로 포함된 장소명")


class SearchRouteInput(BaseModel):
    route_kind: Literal["base", "alternative"] = Field(
        description="base는 최초 경로, alternative는 추가 탐색 경로"
    )
    search_option: int = Field(
        description="TMAP 자동차 경로 탐색 옵션. 허용 값: 0, 1, 2, 3, 4, 10, 19"
    )


class PlanAlternativeRouteInput(BaseModel):
    search_option: int = Field(
        description="TMAP 재탐색에 사용할 searchOption. 허용 값: 0, 1, 2, 3, 4, 10, 19"
    )
    candidate_order: list[str] = Field(
        description="다음에 실행할 후보 ID 하나를 담은 목록. 실행 결과를 받은 뒤 다음 후보를 판단"
    )
    reason: str = Field(description="기본 경로의 실제 교통데이터에 근거한 재탐색 전략 선택 이유")


class FinishRouteInput(BaseModel):
    selected_route: str = Field(description="실제로 조회된 경로 키. 예: base, alternative_1, alternative_2, optimized")
    reason: str = Field(description="TMAP 데이터에 근거한 최종 선택 이유")


@dataclass
class RouteRunContext:
    start: dict[str, Any] | None = None
    end: dict[str, Any] | None = None
    routes: dict[str, dict[str, Any]] = field(default_factory=dict)
    final_route: str | None = None
    final_reason: str | None = None
    ambiguity: dict[str, Any] | None = None
    alternative_attempted: bool = False
    alternative_failure_reason: str | None = None
    alternative_plan: dict[str, Any] | None = None
    alternative_plan_attempts: int = 0
    alternative_plan_history: list[str] = field(default_factory=list)
    alternative_evaluations: list[dict[str, Any]] = field(default_factory=list)
    route_api_calls: int = 0
    fallback_notice: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)

    def add_event(self, stage: str, label: str, detail: str = "") -> None:
        self.events.append({"stage": stage, "label": label, "detail": detail})


class RouteAgent:
    def __init__(self, tmap_client: TMapClient, model_name: str):
        if LANGCHAIN_IMPORT_ERROR is not None:
            raise RuntimeError(
                "LangChain 패키지가 설치되어 있지 않습니다. "
                "requirements.txt 설치 후 다시 실행하세요."
            ) from LANGCHAIN_IMPORT_ERROR

        self.tmap = tmap_client
        self.model = ChatGoogleGenerativeAI(model=model_name)

    @staticmethod
    def _json(data: Any) -> str:
        return json.dumps(data, ensure_ascii=False)

    @staticmethod
    def _invoke_model(bound_model: Any, messages: list[Any]) -> Any:
        try:
            return bound_model.invoke(messages)
        except Exception as exc:
            error_text = str(exc)
            upper_text = error_text.upper()

            if "RESOURCE_EXHAUSTED" in upper_text or "429" in error_text:
                raise RouteModelError(
                    "현재 AI 요청 한도를 초과했습니다. 잠시 후 다시 시도해 주세요.",
                    status_code=429,
                ) from exc

            if "UNAVAILABLE" in upper_text or "503" in error_text:
                raise RouteModelError(
                    "AI 모델 연결이 일시적으로 원활하지 않습니다. 잠시 후 다시 시도해 주세요.",
                    status_code=503,
                ) from exc

            raise RouteModelError("AI 경로 판단 중 오류가 발생했습니다.", status_code=503) from exc

    def run(
        self,
        user_message: str,
        current_location: dict[str, Any] | None = None,
        place_choice: dict[str, Any] | None = None,
        preset_end: dict[str, Any] | None = None,
        preset_start: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        ctx = RouteRunContext()
        ctx.add_event("agent", "사용자 요청 수신", user_message)

        if current_location is not None:
            ctx.start = {
                "query": "현재 위치",
                "name": current_location.get("name") or "현재 위치",
                "lat": float(current_location["lat"]),
                "lon": float(current_location["lon"]),
            }
            ctx.add_event("location", "현재 위치 확인", ctx.start["name"])

        if preset_start is not None:
            ctx.start = dict(preset_start)

        if preset_end is not None:
            ctx.end = dict(preset_end)
            ctx.add_event("location", "목적지 확인", ctx.end.get("name") or "선택한 목적지")

        if place_choice:
            role = place_choice.get("role")
            candidate = place_choice.get("candidate") or {}
            if role in {"start", "end"} and "lat" in candidate and "lon" in candidate:
                selected = dict(candidate)
                if role == "start":
                    ctx.start = selected
                else:
                    ctx.end = selected
                ctx.add_event(
                    "location",
                    "장소 선택",
                    f"{role} · {selected.get('name') or selected.get('address') or '선택 장소'}",
                )

        def lookup_place(role: str, keyword: str) -> dict[str, Any]:
            existing = ctx.start if role == "start" else ctx.end
            if existing is not None:
                return {"ok": True, "role": role, "place": existing, "reused": True}

            try:
                place = self.tmap.search_place(keyword)
            except TMapAmbiguousPlace as exc:
                ctx.ambiguity = {
                    "role": role,
                    "keyword": keyword,
                    "candidates": exc.candidates,
                }
                ctx.add_event("needs_input", "장소 선택 필요", keyword)
                return {
                    "ok": False,
                    "ambiguous": True,
                    "role": role,
                    "keyword": keyword,
                    "candidates": exc.candidates,
                }

            if role == "start":
                ctx.start = place
                label = "출발지 검색"
            else:
                ctx.end = place
                label = "도착지 검색"

            ctx.add_event("tool_result", label, f"{keyword} → {place['name']}")
            return {"ok": True, "role": role, "place": place}

        def route_rank(route: dict[str, Any]) -> tuple[float, float]:
            summary = route["summary"]
            return (summary["total_time_min"], summary["total_distance_km"])

        def route_structure_signature(summary: dict[str, Any]) -> tuple[tuple[Any, Any], ...]:
            """TMAP 도로 구간의 순서를 사용해 구조적으로 같은 경로를 식별한다.

            좌표 겹침 비율 같은 임의 수치 기준은 사용하지 않는다. 같은 도로명/도로종류
            순서가 연속해서 반복될 때만 하나로 접어 TMAP 좌표 샘플링 차이만 제거한다.
            """
            signature: list[tuple[Any, Any]] = []
            for section in summary.get("sections", []):
                road_name = section.get("road_name")
                if not road_name or road_name == "도로명 없음":
                    return ()
                item = (road_name, section.get("road_type"))
                if not signature or signature[-1] != item:
                    signature.append(item)
            return tuple(signature)

        departure_candidates = None

        def candidate_definitions() -> dict[str, dict[str, Any]]:
            nonlocal departure_candidates

            definitions = {"direct": {"waypoints": [], "target_indexes": []}}
            base = ctx.routes.get("base")
            if not base:
                return definitions

            if departure_candidates is None:
                departure_candidates, notice = discover_departure_branches(
                    ctx.start, base["view"]
                )
                ctx.add_event("road_lookup", "출발 분기 후보 조회", notice)
                before_candidates, before_notice = (
                    discover_before_congestion(base["view"])
                )

                existing = {
                    tuple((p["lon"], p["lat"]) for p in item["waypoints"])
                    for item in departure_candidates.values()
                }

                for key, item in before_candidates.items():
                    signature = tuple(
                        (p["lon"], p["lat"]) for p in item["waypoints"]
                    )
                    if signature not in existing:
                        departure_candidates[key] = item
                        existing.add(signature)

                ctx.add_event(
                    "road_lookup", "정체 진입 전 분기 후보 조회", before_notice
                )

            # 이미 조회한 분기는 옵션만 바꾸어 다시 탐색하지 않는다.
            tried = {
                item.get("candidate")
                for item in ctx.alternative_evaluations
            }
            definitions.update({
                key: value
                for key, value in departure_candidates.items()
                if key not in tried
            })

            # 통합 경로도 자동 호출하지 않는다. 검증된 서로 다른 블록의 경유점이 있을 때 제공한다.
            best_by_block = {}
            for route in ctx.routes.values():
                bypass = route.get("bypass") or {}
                index = bypass.get("target_block_index")
                point = bypass.get("refined_waypoint")
                if index and point and index in bypass.get("avoided_block_indexes", []):
                    if index not in best_by_block or route_rank(route) < route_rank(best_by_block[index]):
                        best_by_block[index] = route
            if len(best_by_block) > 1:
                definitions["combined"] = {
                    "waypoints": [best_by_block[i]["bypass"]["refined_waypoint"] for i in sorted(best_by_block)],
                    "target_indexes": sorted(best_by_block)}
            return definitions

        def strategy_signature(option: int, definition: dict[str, Any]) -> str:
            return json.dumps({"search_option": option, "waypoints": definition["waypoints"]},
                              ensure_ascii=False, sort_keys=True)

        def build_alternative_search_context() -> dict[str, Any]:
            base = ctx.routes.get("base")
            blocks = find_congestion_blocks(base["view"]) if base else []
            definitions = candidate_definitions()
            untried = {}
            for candidate, definition in definitions.items():
                untried[candidate] = [option for option in SEARCH_OPTION_LABELS
                    if not (option == 0 and candidate == "direct")
                    and strategy_signature(option, definition) not in ctx.alternative_plan_history]
            return {
                "required": False,
                "congestion_blocks": [{"block_id": f"block_{i}", "road_names": b.get("road_names", []),
                                       "states": b.get("states", [])} for i, b in enumerate(blocks[:5], 1)],
                "available_candidate_ids": [key for key, options in untried.items() if options],
                "departure_candidates": {
                    key: value
                    for key, value in definitions.items()
                    if key.startswith(("departure_", "before_"))
                },
                "untried_search_options": untried,
                "search_options": SEARCH_OPTION_LABELS,
                "attempts_remaining": max(0, MAX_ALTERNATIVE_PLAN_ATTEMPTS - ctx.alternative_plan_attempts),
            }

        def lookup_route(route_kind: str, search_option: int) -> dict[str, Any]:
            if ctx.start is None or ctx.end is None:
                return {"ok": False, "error": "출발지와 도착지 검색이 먼저 필요합니다."}

            if search_option not in SEARCH_OPTION_LABELS:
                return {
                    "ok": False,
                    "error": "지원하지 않는 search_option입니다. 0, 1, 2, 3, 4, 10, 19 중 하나를 사용해야 합니다.",
                }

            if route_kind == "base" and search_option != 0:
                return {"ok": False, "error": "기본 경로는 search_option 0으로 조회해야 합니다."}
            if route_kind == "alternative":
                if ctx.alternative_plan is None:
                    return {
                        "ok": False,
                        "requires_plan": True,
                        "error": "기본 경로 데이터를 바탕으로 재탐색 전략을 먼저 결정해야 합니다.",
                    }
                if search_option != ctx.alternative_plan.get("search_option"):
                    return {
                        "ok": False,
                        "error": "search_option이 Gemini가 결정한 재탐색 전략과 일치하지 않습니다.",
                    }

            def store_route(
                key: str,
                raw: dict[str, Any],
                option: int,
                label: str,
                bypass: dict[str, Any] | None = None,
            ) -> dict[str, Any]:
                summary = summarize_route(raw)
                view = build_route_view(raw)
                if not view.get("paths"):
                    raise ValueError("지도에 표시할 경로 좌표가 없습니다.")
                entry = {
                    "search_option": option,
                    "search_option_label": label,
                    "summary": summary,
                    "view": view,
                    "traffic_signature": build_traffic_signature(summary),
                }
                if bypass is not None:
                    entry["bypass"] = bypass
                ctx.routes[key] = entry
                return entry

            def tool_route_payload(key: str, entry: dict[str, Any]) -> dict[str, Any]:
                return {
                    "route_key": key,
                    "search_option": entry["search_option"],
                    "search_option_label": entry["search_option_label"],
                    "summary": build_agent_route_summary(entry["summary"]),
                    "bypass": entry.get("bypass"),
                }

            if route_kind == "base":
                if "base" in ctx.routes:
                    existing = ctx.routes["base"]
                    return {
                        "ok": True,
                        "route_kind": "base",
                        **tool_route_payload("base", existing),
                        "alternative_search_context": build_alternative_search_context(),
                        "reused": True,
                    }

                ctx.route_api_calls += 1
                raw = self.tmap.search_route(ctx.start, ctx.end, 0)
                option_label = SEARCH_OPTION_LABELS[0]
                entry = store_route("base", raw, 0, option_label)
                summary = entry["summary"]

                has_delay_or_congestion = any(
                    segment.get("state") in {"지체", "정체"}
                    for segment in summary.get("traffic_segments", [])
                )
                if has_delay_or_congestion:
                    ctx.add_event(
                        "decision_required",
                        "추가 경로 탐색 여부 판단",
                        "기본 경로 일부 구간에서 지체 또는 정체가 확인되었습니다.",
                    )

                ctx.add_event(
                    "tool_result",
                    "기본 경로 조회",
                    f"{option_label} · {summary['total_distance_km']} km · {summary['total_time_min']}분",
                )
                return {
                    "ok": True,
                    "route_kind": "base",
                    **tool_route_payload("base", entry),
                    "alternative_search_context": build_alternative_search_context(),
                }

            # 누적 개선: 후보 하나를 실행한 뒤 결과를 Agent에 돌려준다. 기존 최대 3회는 유지한다.
            if ctx.alternative_plan_attempts >= MAX_ALTERNATIVE_PLAN_ATTEMPTS:
                return {"ok": False, "error": "기존 추가 탐색 한도에 도달했습니다. 조회된 경로로 확정하세요."}
            plan = ctx.alternative_plan or {}
            definition = plan.get("definition")
            if not definition:
                return {"ok": False, "error": "유효한 추가 탐색 계획이 필요합니다."}
            signature = strategy_signature(search_option, definition)
            if signature in ctx.alternative_plan_history:
                return {"ok": False, "reused": True, "error": "이미 실행한 동일 요청입니다. 추가 호출하지 않았습니다.",
                        "evaluation_feedback": ctx.alternative_evaluations[-1] if ctx.alternative_evaluations else None}
            candidate = plan["candidate_order"][0]
            best_before_key = min(ctx.routes, key=lambda key: route_rank(ctx.routes[key]))
            before = route_rank(ctx.routes[best_before_key])
            ctx.alternative_plan_history.append(signature)
            ctx.alternative_plan_attempts += 1
            ctx.alternative_attempted = True
            feedback = {"attempt": ctx.alternative_plan_attempts,
                        "candidate": candidate, "search_option": search_option,
                        "best_before": {"route_key": best_before_key, "total_time_min": before[0],
                                        "total_distance_km": before[1]}, "accepted_routes": [], "rejected_candidates": []}
            ctx.add_event("tool_call", "추가 경로 조회", f"{candidate} · {SEARCH_OPTION_LABELS[search_option]}")
            ctx.route_api_calls += 1
            try:
                raw = self.tmap.search_route(ctx.start, ctx.end, search_option,
                                             pass_list=definition["waypoints"] or None)
                summary = summarize_route(raw)
                view = build_route_view(raw)
                if not view.get("paths"):
                    raise ValueError("지도에 표시할 경로 좌표가 없습니다.")
                rank = (summary["total_time_min"], summary["total_distance_km"])
                feedback.update(candidate_time_min=rank[0], candidate_distance_km=rank[1],
                                time_change_min=rank[0]-before[0], distance_change_km=round(rank[1]-before[1], 1),
                                improvement="improved" if rank < before else "equal" if rank == before else "worse")
                blocks = find_congestion_blocks(ctx.routes["base"]["view"])
                checks = [
                    route_crosses_congestion_block(view, block)
                    for block in blocks
                ]

                avoided = [
                    i for i, result in enumerate(checks, 1)
                    if result is False
                ]
                unknown = [
                    i for i, result in enumerate(checks, 1)
                    if result is None
                ]
                reentered = [
                    i for i, result in enumerate(checks, 1)
                    if result is True
                ]

                feedback.update(
                    avoided_blocks=avoided,
                    unverified_blocks=unknown,
                    reentered_blocks=reentered,
                )

                target = definition.get("target_indexes", [])

                # before_ 후보는 대상으로 지정된 정체 블록을
                # 실제로 회피한 경우에만 추가 경로로 인정한다.
                target_bypass_failed = bool(target) and any(
                    index not in avoided
                    for index in target
                )

                candidate_structure = route_structure_signature(summary)
                duplicate = next(
                    (
                        key
                        for key, route in ctx.routes.items()
                        if route["view"]["paths"] == view["paths"]
                        or (
                            candidate_structure
                            and route_structure_signature(route["summary"])
                            == candidate_structure
                        )
                    ),
                    None,
                )
                if target_bypass_failed:
                    feedback["rejected_candidates"].append(
                        {
                            "reason": "target_congestion_not_avoided",
                            "target_block_indexes": target,
                            "reentered_blocks": [
                                index for index in target
                                if index in reentered
                            ],
                            "unverified_blocks": [
                                index for index in target
                                if index in unknown
                            ],
                        }
                    )

                    feedback["improvement"] = "bypass_failed"

                elif duplicate:
                    feedback["rejected_candidates"].append(
                        {
                            "reason": "duplicate_route",
                            "route_key": duplicate,
                        }
                    )
                    feedback["improvement"] = "duplicate"

                else:
                    number = len([
                        key for key in ctx.routes
                        if key.startswith("alternative_")
                    ]) + 1

                    key = f"alternative_{number}"
                    refined = (extract_route_detour_waypoint(view, blocks[target[0]-1])
                               if len(target) == 1 and target[0] in avoided else None)
                    entry = store_route(key, raw, search_option, f"추가 경로 {number} · {SEARCH_OPTION_LABELS[search_option]}",
                        bypass={"verified": bool(avoided) and not unknown,
                                "candidate_type": candidate, "waypoints": definition["waypoints"],
                                "target_block_index": target[0] if len(target) == 1 else None,
                                "refined_waypoint": refined, "avoided_block_indexes": avoided,
                                "unverified_block_indexes": unknown, "gemini_plan_reason": plan["reason"]})
                    feedback["accepted_routes"].append(tool_route_payload(key, entry))
                label = {
                    "improved": "개선",
                    "equal": "동일",
                    "worse": "악화",
                    "duplicate": "중복",
                    "bypass_failed": "정체 회피 실패",
                }[feedback["improvement"]]
                ctx.add_event("evaluation", "경로 개선 여부", f"현재 최선 대비 {label} · 시간 {feedback['time_change_min']:+}분 · 거리 {feedback['distance_change_km']:+g} km")
            except (TMapError, ValueError, KeyError, TypeError) as exc:
                feedback.update(improvement="unavailable", error=str(exc))
                feedback["rejected_candidates"].append({"reason": "request_or_response_error"})
                ctx.add_event("tool_error", "추가 경로 조회 실패", str(exc))
            best_key = min(ctx.routes, key=lambda key: route_rank(ctx.routes[key]))
            feedback["best_after"] = {"route_key": best_key, **ctx.routes[best_key]["summary"]}
            feedback["best_after"] = {key: feedback["best_after"][key] for key in ("route_key", "total_time_min", "total_distance_km")}
            feedback["can_replan"] = ctx.alternative_plan_attempts < MAX_ALTERNATIVE_PLAN_ATTEMPTS
            ctx.alternative_evaluations.append(feedback)
            ctx.add_event("feedback", "다음 행동 판단", "시간·거리 변화와 실패 이유를 근거로 추가 탐색 또는 종료를 판단합니다.")
            return {"ok": feedback["improvement"] != "unavailable", "evaluation_feedback": feedback,
                    "alternative_search_context": build_alternative_search_context()}

        @tool("prepare_base_route", args_schema=PrepareBaseRouteInput)
        def prepare_base_route(start_keyword: str, end_keyword: str) -> str:
            """출발지·도착지를 TMAP에서 확인하고 교통최적+추천 기본 경로까지 한 번에 조회한다."""
            try:
                start_result = lookup_place("start", start_keyword)
                if not start_result.get("ok"):
                    return self._json(start_result)
                end_result = lookup_place("end", end_keyword)
                if not end_result.get("ok"):
                    return self._json(end_result)
                route_result = lookup_route("base", 0)
            except TMapError as exc:
                ctx.add_event("tool_error", "기본 경로 준비 실패", str(exc))
                return self._json({"ok": False, "error": str(exc)})
            except (ValueError, KeyError, TypeError) as exc:
                ctx.add_event("tool_error", "기본 경로 분석 실패", str(exc))
                return self._json({"ok": False, "error": str(exc)})

            if not route_result.get("ok"):
                return self._json(route_result)

            return self._json(
                {
                    "ok": True,
                    "start": start_result.get("place"),
                    "end": end_result.get("place"),
                    "base_route": route_result,
                }
            )

        @tool("prepare_current_route", args_schema=PrepareCurrentRouteInput)
        def prepare_current_route(end_keyword: str) -> str:
            """브라우저에서 확인된 현재 위치를 출발지로 사용해 목적지를 찾고 기본 경로를 조회한다."""
            if ctx.start is None:
                return self._json(
                    {"ok": False, "needs_location": True, "error": "현재 위치 정보가 필요합니다."}
                )
            try:
                end_result = lookup_place("end", end_keyword)
                if not end_result.get("ok"):
                    return self._json(end_result)
                route_result = lookup_route("base", 0)
            except TMapError as exc:
                ctx.add_event("tool_error", "현재 위치 경로 준비 실패", str(exc))
                return self._json({"ok": False, "error": str(exc)})
            except (ValueError, KeyError, TypeError) as exc:
                ctx.add_event("tool_error", "현재 위치 경로 분석 실패", str(exc))
                return self._json({"ok": False, "error": str(exc)})

            return self._json(
                {
                    "ok": True,
                    "start": ctx.start,
                    "end": end_result.get("place"),
                    "base_route": route_result,
                }
            )

        @tool("find_place", args_schema=FindPlaceInput)
        def find_place(role: str, keyword: str) -> str:
            """TMAP에서 사용자가 말한 출발지 또는 도착지를 개별적으로 검색한다."""
            try:
                return self._json(lookup_place(role, keyword))
            except TMapError as exc:
                ctx.add_event("tool_error", "장소 검색 실패", str(exc))
                return self._json({"ok": False, "error": str(exc)})

        @tool("plan_alternative_route", args_schema=PlanAlternativeRouteInput)
        def plan_alternative_route(
            search_option: int,
            candidate_order: list[str],
            reason: str,
        ) -> str:
            """기본 경로의 실제 TMAP 교통데이터를 보고 추가 탐색에 사용할 TMAP 파라미터를 결정한다."""
            if "base" not in ctx.routes:
                return self._json({"ok": False, "error": "기본 경로를 먼저 조회해야 합니다."})
            if search_option not in SEARCH_OPTION_LABELS:
                return self._json(
                    {
                        "ok": False,
                        "error": "지원하지 않는 search_option입니다. 0, 1, 2, 3, 4, 10, 19 중 하나를 사용해야 합니다.",
                    }
                )

            if ctx.alternative_plan_attempts >= MAX_ALTERNATIVE_PLAN_ATTEMPTS:
                return self._json({"ok": False, "error": "기존 추가 탐색 한도에 도달했습니다. finish_route로 확정하세요."})
            if not reason.strip():
                return self._json({"ok": False, "error": "실제 조회 결과에 근거한 추가 탐색 이유가 필요합니다."})
            definitions = candidate_definitions()
            if len(candidate_order) != 1 or candidate_order[0] not in definitions:
                return self._json({"ok": False, "error": "결과를 순차 평가할 수 있도록 제공된 후보 하나를 선택하세요.",
                                   "alternative_search_context": build_alternative_search_context()})
            candidate = candidate_order[0]
            definition = definitions[candidate]
            signature = strategy_signature(search_option, definition)
            if (candidate == "direct" and search_option == 0) or signature in ctx.alternative_plan_history:
                return self._json({"ok": False, "error": "이미 조회한 요청입니다. 다른 탐색 근거가 없으면 종료하세요.",
                                   "alternative_search_context": build_alternative_search_context()})
            ctx.alternative_plan = {"search_option": search_option, "candidate_order": [candidate],
                                    "definition": definition, "reason": reason.strip()}
            ctx.alternative_attempted = False
            ctx.add_event("planning", "Gemini 추가 탐색 판단", f"{candidate} · {SEARCH_OPTION_LABELS[search_option]} · {reason.strip()}")
            return self._json({"ok": True, "plan": {k: v for k, v in ctx.alternative_plan.items() if k != "definition"}})

        @tool("search_route", args_schema=SearchRouteInput)
        def search_route(route_kind: str, search_option: int) -> str:
            """TMAP 실시간 교통정보가 포함된 자동차 경로를 조회하고 구간별 교통상태를 분석한다."""
            try:
                return self._json(lookup_route(route_kind, search_option))
            except (TMapError, ValueError, KeyError, TypeError) as exc:
                ctx.add_event("tool_error", "경로 탐색 실패", str(exc))
                return self._json({"ok": False, "error": str(exc)})

        def route_display_name(route_key: str) -> str:
            if route_key == "base":
                return "기본 경로"
            if route_key == "optimized":
                return "통합 최적 경로"
            if route_key.startswith("alternative_"):
                return f"추가 경로 {route_key.split('_')[-1]}"
            return "추가 경로"

        def preferred_route_keys() -> list[str]:
            # 누적 개선: 기존 분/km 단위를 유지하고 소요시간 → 거리 순으로 비교한다.
            ranks = {}
            for key, route in ctx.routes.items():
                summary = route.get("summary", {})
                duration = summary.get("total_time_min")
                distance = summary.get("total_distance_km")
                if isinstance(duration, (int, float)) and isinstance(distance, (int, float)):
                    ranks[key] = (duration, distance)
            if not ranks:
                return []
            best = min(ranks.values())
            return [key for key, rank in ranks.items() if rank == best]

        @tool("finish_route", args_schema=FinishRouteInput)
        def finish_route(selected_route: str, reason: str) -> str:
            """조회된 실제 경로 중 최종 이동 경로를 확정한다."""
            if selected_route not in ctx.routes:
                return self._json({"ok": False, "error": "선택한 경로가 아직 조회되지 않았습니다."})
            # 정체가 있으면 실제 추가 조회 전에 최적이라고 단정하지 않는다.
            has_congestion = any(
                segment.get("state") in {"지체", "정체"}
                for segment in ctx.routes.get("base", {})
                .get("summary", {}).get("traffic_segments", [])
            )
            if (
                has_congestion
                and ctx.alternative_plan_attempts == 0
                and not ctx.fallback_notice
            ):
                return self._json({
                    "ok": False,
                    "requires_comparison": True,
                    "error": (
                        "정체가 있으나 추가 경로를 조회하지 않았습니다. "
                        "alternative_search_context에서 before_ 후보를 우선 확인하고, "
                        "없으면 departure_ 후보, 둘 다 없으면 direct를 검토해 "
                        "plan_alternative_route 후 실제 TMAP 경로를 조회하세요."
                    ),
                })
            stop_reason = reason.strip()
            # 누적 개선: 순위 오류를 LLM에 재요청하지 않고 Python에서 확정한다.
            preferred = preferred_route_keys()
            if preferred:
                if selected_route not in preferred:
                    selected_route = preferred[0]
                summary = ctx.routes[selected_route]["summary"]
                if len(ctx.routes) == 1:
                    reason = (
                        f"현재 확보된 기본 경로를 안내합니다. "
                        f"예상 {summary['total_time_min']}분 · "
                        f"{summary['total_distance_km']} km"
                    )
                else:
                    reason = (
                        f"조회된 경로를 소요시간 우선, 동일하면 거리 순으로 비교해 "
                        f"{route_display_name(selected_route)}를 선택했습니다. "
                        f"예상 {summary['total_time_min']}분 · "
                        f"{summary['total_distance_km']} km"
                    )

            base_summary = ctx.routes.get("base", {}).get("summary", {})
            chosen = ctx.routes[selected_route]["summary"]
            if ctx.alternative_plan_attempts:
                if selected_route == "base":
                    reason += " 추가 탐색에서 더 나은 경로를 확인하지 못해 기본 경로를 유지합니다."
                else:
                    time_saved = base_summary["total_time_min"] - chosen["total_time_min"]
                    distance_saved = round(base_summary["total_distance_km"] - chosen["total_distance_km"], 1)
                    reason += (f" 기본 경로보다 예상시간 {time_saved}분 단축." if time_saved > 0 else
                               f" 기본 경로와 예상시간이 같고 거리 {distance_saved:g} km 단축." if distance_saved > 0 else
                               " 기본 경로와 예상시간·거리가 같습니다.")
            if ctx.fallback_notice:
                reason += f" {ctx.fallback_notice} 확보한 경로 중에서 선택했습니다."
            ctx.add_event("decision", "탐색 종료 판단", stop_reason or "조회된 경로 비교 완료")
            ctx.add_event("evaluation", "경로 API 호출 집계", f"기본·추가 경로 조회 합계 {ctx.route_api_calls}회")

            # 후보들의 합집합과 별개로, 최종 선택한 단일 경로를 다시 판정한다.
            base = ctx.routes.get("base", {})
            blocks = find_congestion_blocks(base.get("view", {}))
            results = {"avoided_blocks": [], "reentered_blocks": [], "unverified_blocks": []}
            for index, block in enumerate(blocks, start=1):
                crossing = route_crosses_congestion_block(ctx.routes[selected_route]["view"], block)
                category = ("unverified_blocks" if crossing is None else
                            "reentered_blocks" if crossing else "avoided_blocks")
                results[category].append(index)
            ctx.routes[selected_route]["selected_congestion_evaluation"] = results
            if blocks:
                detail = (f"선택 경로 기준 · 회피 확인 {len(results['avoided_blocks'])}개 · "
                          f"{'포함된 정체 블록' if selected_route == 'base' else '기존 정체 블록 재통과'} "
                          f"{len(results['reentered_blocks'])}개 · "
                          f"검증 불가 {len(results['unverified_blocks'])}개")
                ctx.add_event("evaluation", "최종 경로 정체 검증", detail)
                reason = f"{reason.strip()}\n{detail}"

            ctx.final_route = selected_route
            ctx.final_reason = reason.strip()
            route_label = route_display_name(selected_route)
            ctx.add_event("decision", "최종 경로 선택", f"{route_label} · {ctx.final_reason}")
            return self._json(
                {"ok": True, "selected_route": selected_route, "reason": ctx.final_reason}
            )

        tools = [
            prepare_base_route,
            prepare_current_route,
            find_place,
            plan_alternative_route,
            search_route,
            finish_route,
        ]
        tool_map = {item.name: item for item in tools}
        model_with_tools = self.model.bind_tools(tools, tool_choice="auto")

        location_context = (
            "브라우저가 사용자의 현재 위치 좌표를 제공했습니다. 사용자가 출발지를 생략했거나 현재 위치/여기에서 출발한다고 요청한 경우 이 좌표를 출발지로 사용합니다."
            if current_location is not None
            else "브라우저 현재 위치 좌표는 제공되지 않았습니다. 출발지가 없는 요청에서 임의 위치를 추측하지 않습니다."
        )
        preset_context = (
            "출발지와 목적지 좌표가 이미 확인되어 있으므로 장소 검색 없이 search_route(route_kind=\"base\", search_option=0)부터 호출합니다."
            if ctx.start is not None and ctx.end is not None
            else ""
        )

        system_prompt = f"""
당신은 실시간 교통정보 기반 경로 탐색 AI Agent입니다.
사용자의 자연어 이동 요청을 이해하고 TMAP 도구를 직접 사용해 경로를 결정합니다.

현재 위치 상태:
- {location_context}
- {preset_context}

반드시 지킬 흐름:
- 사용자가 출발지와 도착지를 모두 말한 일반 요청은 prepare_base_route를 우선 호출합니다.
- 사용자가 출발지를 생략했고 브라우저 현재 위치가 제공된 경우 prepare_current_route를 우선 호출합니다.
- 출발지와 목적지 좌표가 이미 확인된 경우 search_route로 기본 경로를 바로 조회합니다.
- prepare_base_route 또는 prepare_current_route가 성공했다면 같은 장소를 다시 검색하거나 base 경로를 다시 조회하지 않습니다.
- find_place는 장소를 개별적으로 다시 확인해야 하는 경우에만 사용합니다.
- 기본 경로의 총 거리, 예상시간, 교통정보 세부 구간과 속도를 확인합니다.
- 기본 경로 Tool 결과에는 alternative_search_context가 함께 제공됩니다. 여기에는 지체/정체 블록과 실제로 사용할 수 있는 우회 후보 ID, TMAP searchOption 목록이 들어 있습니다.
- 첫 기본 경로를 분석한 뒤 alternative_search_context에 제공된 후보만 사용합니다. 좌표를 추측하지 않습니다.
- before_ 후보가 있으면 정체 진입 전 도로 분기를 우선 검토합니다. before_ 후보가 없으면 departure_ 후보를 검토하고, 둘 다 없으면 direct를 검토합니다.
- plan_alternative_route의 candidate_order에는 제공된 후보 ID 하나만 넣습니다. 제공될 수 있는 후보는 before_, departure_, direct, 그리고 검증된 경유점을 조합한 combined입니다.
- search_option은 실제 교통정보와 소요시간 우선 목표를 근거로 선택합니다. 옵션 이름만 다르다는 이유로 추가 조회하지 않습니다.
- 계획한 뒤 search_route(route_kind="alternative", search_option=계획한 값)를 호출합니다. 후보 하나의 실제 TMAP 결과를 받은 뒤 다음 행동을 결정합니다.
- evaluation_feedback의 improvement, 시간·거리 변화, 회피·재통과·검증 불가 결과, 중복 또는 실패 이유를 사용합니다. 별도 평가용 LLM 호출은 만들지 않습니다.
- 동일 경로 또는 구조적으로 같은 도로 구간 순서가 반환되면 별도 추가 경로로 취급하지 않습니다.
- 개선된 경로를 얻었거나, 더 검토할 분기 후보가 없거나, 탐색 한도에 도달하면 finish_route로 종료할 수 있습니다.
- 같은 TMAP 요청은 반복하지 않으며 기존 최대 탐색 횟수 안에서 판단합니다. 오류 발생 시에도 이미 조회된 경로는 사용할 수 있습니다.
- before_ 후보로 조회한 경로가 대상 정체 블록을 다시 통과하거나 회피 여부를 검증할 수 없으면 해당 결과는 추가 경로로 채택하지 않습니다.
- 해당 before_ 후보가 실패했고 아직 조회하지 않은 다른 before_ 후보와 탐색 횟수가 남아 있으면 다음 후보를 검토합니다.
- 대상 정체 블록을 실제로 회피한 경로만 우회 성공 경로로 취급합니다.
- 분기 후보의 경유점은 도구가 제공한 좌표를 사용하며 출발 좌표는 바꾸지 않습니다.
- OSM 분기 후보라는 이유만으로 통행 가능하거나 빠르다고 단정하지 않습니다. 최종 선택은 실제 TMAP 조회 결과를 사용합니다.
- 최종 순위는 기존 분/km 단위로 소요시간 → 거리입니다. finish_route는 Python이 순위를 확정하므로 재선택 호출은 필요 없습니다.
- finish_route는 이미 조회된 경로만 확정합니다. finish_route 내부에서 미조회 후보를 자동으로 TMAP에 요청하지 않습니다.
- finish_route의 reason에는 실제 조회 결과에 근거한 종료 이유만 간결하게 남깁니다. 개선이 없으면 “조회한 후보에서 개선을 확인하지 못했다”고 설명합니다. 조회하지 않은 경로까지 개선이 불가능하다고 단정하지 않습니다.

TMAP 탐색 옵션:
0 교통최적+추천
1 교통최적+무료우선
2 교통최적+최소시간
3 교통최적+초보
4 교통최적+고속도로우선
10 최단거리+유/무료
19 교통최적+어린이보호구역 회피

판단 규칙:
- 사용자가 말하지 않은 장소, 선호조건, 교통상황을 추측하지 않습니다.
- 현재 위치 좌표가 제공되지 않았는데 사용자가 출발지를 생략하면 임의의 출발지를 만들지 않습니다.
- 장소 검색 결과가 여러 지역으로 모호하면 임의로 선택하지 않습니다.
- 임의의 거리·시간·혼잡 임계값을 만들지 않습니다.
- traffic_counts는 사고 횟수나 정체 발생 횟수가 아니라 TMAP 교통정보 세부 구간 수입니다.
- 하나의 긴 도로에 정체 구간이 포함되어 있어도 도로 전체가 정체라고 표현하지 않습니다.
- road_traffic_summary는 서행·지체·정체가 확인된 도로만 요약한 판단용 데이터입니다.
- attention_segments는 서행·지체·정체로 확인된 실제 세부 구간이며 지도용 좌표는 Agent 판단 데이터에서 제외되어 있습니다.
- 우회 탐색 옵션은 기본 경로의 실제 교통데이터 또는 사용자의 명시적 요구와 관련된 경우에만 선택합니다.
- Gemini의 역할은 TMAP 재탐색 파라미터와 후보 순서를 계획하고 결과를 비교하는 것입니다. 실제 경로 계산은 반드시 TMAP이 수행합니다.
- Evaluator 역할은 Python이 담당합니다. Gemini는 Python이 판정한 성공/실패를 다시 임의로 뒤집지 않고, 실패 이유를 다음 재계획에 반영합니다.
- 정체 종료 이후 기존 경로로 다시 합류하는 것은 정상적인 우회로 인정합니다.
- Python Evaluator는 단순 도로명이나 교차점 한 점이 겹친다는 이유로 우회 실패로 판단하지 않고, 기존 정체 geometry의 실제 도로 구간을 다시 사용했는지 검증합니다.
- 도구 결과가 실패하면 evaluation_feedback을 확인하고 수정할 수 있을 때만 다시 시도합니다.
- 최종 경로는 반드시 실제로 조회된 경로 중에서 선택하며, 일반적인 A→B 요청에서는 예상 소요시간이 가장 짧은 경로를 선택합니다.
""".strip()

        messages = [SystemMessage(content=system_prompt), HumanMessage(content=user_message)]
        seen_calls: set[tuple[str, str, tuple[Any, ...]]] = set()
        assistant_text = ""
        while ctx.final_route is None:
            try:
                ai_message = self._invoke_model(model_with_tools, messages)
            except RouteModelError as exc:
                if not ctx.routes:
                    raise
                ctx.fallback_notice = exc.public_message
                ctx.add_event("tool_error", "AI 판단 중단", exc.public_message)
                finish_route.invoke({"selected_route": preferred_route_keys()[0], "reason": "AI 연결 오류로 추가 판단을 중단합니다."})
                break
            messages.append(ai_message)
            tool_calls = getattr(ai_message, "tool_calls", None) or []
            if not tool_calls:
                content = ai_message.content
                if isinstance(content, list):
                    content = "\n".join(
                        block["text"]
                        for block in content
                        if isinstance(block, dict)
                        and block.get("type") == "text"
                        and isinstance(block.get("text"), str)
                    )
                assistant_text = content.strip() if isinstance(content, str) else ""
                if ctx.routes:
                    result = json.loads(finish_route.invoke({
                        "selected_route": preferred_route_keys()[0],
                        "reason": assistant_text or "추가 탐색 없이 종료 판단",
                    }))
                    if result.get("requires_comparison"):
                        reminder = result["error"]
                        already_reminded = any(
                            isinstance(message, HumanMessage)
                            and message.content == reminder
                            for message in messages
                        )
                        if already_reminded:
                            ctx.fallback_notice = (
                                "AI가 추가 탐색을 실행하지 못해 경로 비교는 완료하지 못했습니다."
                            )
                            finish_route.invoke({
                                "selected_route": preferred_route_keys()[0],
                                "reason": ctx.fallback_notice,
                            })
                            break
                        messages.append(HumanMessage(content=reminder))
                        assistant_text = ""
                        continue
                break

            for call in tool_calls:
                name = call.get("name")
                args = call.get("args") or {}
                call_id = call.get("id")

                context_signature = (
                    ctx.start is not None,
                    ctx.end is not None,
                    tuple(sorted(ctx.routes.keys())),
                    json.dumps(ctx.alternative_plan, ensure_ascii=False, sort_keys=True) if ctx.alternative_plan else None,
                    ctx.alternative_attempted,
                    ctx.final_route,
                )
                call_key = (
                    str(name),
                    json.dumps(args, ensure_ascii=False, sort_keys=True),
                    context_signature,
                )
                if call_key in seen_calls:
                    assistant_text = "동일한 상태에서 같은 도구 호출이 반복되어 실행을 중단했습니다."
                    ctx.add_event("tool_error", "반복 호출 감지", assistant_text)
                    break
                seen_calls.add(call_key)

                selected_tool = tool_map.get(name)
                if selected_tool is None:
                    output = self._json({"ok": False, "error": f"알 수 없는 도구: {name}"})
                else:
                    ctx.add_event(
                        "agent_tool_call",
                        "Agent Tool 선택",
                        f"{name} · {json.dumps(args, ensure_ascii=False)}",
                    )
                    try:
                        output = selected_tool.invoke(args)
                    except Exception as exc:
                        output = self._json({"ok": False, "error": str(exc)})
                        ctx.add_event("tool_error", f"{name} 실행 오류", str(exc))

                messages.append(ToolMessage(content=output, tool_call_id=call_id))

                if ctx.final_route is not None or ctx.ambiguity is not None:
                    break

            if ctx.ambiguity is not None or assistant_text:
                break

        if ctx.ambiguity is not None:
            return {
                "status": "needs_place_choice",
                "assistant_message": "같은 이름의 장소가 여러 지역에서 검색되었습니다. 원하는 장소를 선택해 주세요.",
                "place_choice_role": ctx.ambiguity["role"],
                "place_choice_keyword": ctx.ambiguity["keyword"],
                "place_candidates": ctx.ambiguity["candidates"],
                "start": ctx.start,
                "end": ctx.end,
                "routes": ctx.routes,
                "events": ctx.events,
            }

        if ctx.final_route is None and ctx.routes:
            ctx.fallback_notice = assistant_text or "Agent 판단을 완료하지 못했습니다."
            finish_route.invoke({"selected_route": preferred_route_keys()[0], "reason": "반복 또는 실행 오류로 추가 호출을 중단합니다."})

        if ctx.final_route is None:
            status = "needs_input" if not ctx.routes else "incomplete"
            return {
                "status": status,
                "assistant_message": assistant_text or "경로 결정을 완료하지 못했습니다.",
                "start": ctx.start,
                "end": ctx.end,
                "routes": ctx.routes,
                "events": ctx.events,
            }

        final_data = ctx.routes[ctx.final_route]
        return {
            "status": "ok",
            "assistant_message": ctx.final_reason,
            "start": ctx.start,
            "end": ctx.end,
            "selected_route": ctx.final_route,
            "selected_route_label": route_display_name(ctx.final_route),
            "decision_reason": ctx.final_reason,
            "route_api_calls": ctx.route_api_calls,
            "evaluation_feedback": ctx.alternative_evaluations,
            "degraded": bool(ctx.fallback_notice),
            "final": final_data,
            "routes": ctx.routes,
            "events": ctx.events,
        }