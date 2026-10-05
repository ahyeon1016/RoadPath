(() => {
    "use strict";

    const routeForm = document.getElementById("routeForm");
    const routeInput = document.getElementById("routeInput");
    const submitButton = document.getElementById("submitButton");
    const voiceButton = document.getElementById("voiceButton");
    const inputHint = document.getElementById("inputHint");
    const chatMessages = document.getElementById("chatMessages");
    const mapTitle = document.getElementById("mapTitle");
    const routeSwitch = document.getElementById("routeSwitch");
    const navigationStartButton = document.getElementById("navigationStartButton");
    const navigationStopButton = document.getElementById("navigationStopButton");
    const navigationHud = document.getElementById("navigationHud");
    const navInstruction = document.getElementById("navInstruction");
    const navRoad = document.getElementById("navRoad");
    const navDistance = document.getElementById("navDistance");
    const navTime = document.getElementById("navTime");
    const navArrival = document.getElementById("navArrival");
    const navProgressBar = document.getElementById("navProgressBar");
    const ttsButton = document.getElementById("ttsButton");
    const resultSummary = document.getElementById("resultSummary");
    const agentTimeline = document.getElementById("agentTimeline");
    const segmentFilters = document.getElementById("segmentFilters");
    const segmentMeta = document.getElementById("segmentMeta");
    const segmentTableBody = document.getElementById("segmentTableBody");
    const mapElement = document.getElementById("map");
    const mapFallback = document.getElementById("mapFallback");

    const state = {
        result: null,
        requestInFlight: false,
        navigationRevision: 0,
        activeRoute: null,
        activeSegmentFilter: "전체",
        map: null,
        mapLayers: [],
        startMarker: null,
        endMarker: null,
        locationMarker: null,
        routeAnimationTimer: null,
        currentLocation: null,
        voiceRecognition: null,
        voiceListening: false,
        lastRequestMessage: "",
        navigation: {
            active: false,
            watchId: null,
            route: null,
            destination: null,
            searchOption: null,
            trafficCheckStart: null,
            trafficSignature: [],
            geometry: null,
            currentPosition: null,
            previousPosition: null,
            heading: null,
            currentGuideKey: null,
            currentTrafficSegmentKey: null,
            rerouteInFlight: false,
            trafficCheckInFlight: false,
            routeEntered: false,
            plannedStart: null,
            ttsEnabled: true,
        },
    };

    const trafficColors = {
        "원활": "#2f7d5c",
        "서행": "#c58a17",
        "지체": "#d2632f",
        "정체": "#bf3434",
        "정보없음": "#6f7a82",
        "알 수 없음": "#6f7a82",
    };


    function routeDisplayName(routeKey) {
        if (routeKey === "base") return "기본 경로";
        if (routeKey === "optimized") return "통합 최적 경로";
        if (String(routeKey).startsWith("alternative_")) {
            return `추가 경로 ${String(routeKey).split("_").pop()}`;
        }
        return "추가 경로";
    }

    function escapeHtml(value) {
        return String(value ?? "")
            .replaceAll("&", "&amp;")
            .replaceAll("<", "&lt;")
            .replaceAll(">", "&gt;")
            .replaceAll('"', "&quot;")
            .replaceAll("'", "&#039;");
    }

    function addMessage(role, text, loading = false) {
        const wrapper = document.createElement("div");
        wrapper.className = `message ${role === "user" ? "user-message" : "agent-message"}${loading ? " loading" : ""}`;
        wrapper.innerHTML = `
            <div class="message-label">${role === "user" ? "사용자" : "RoadPath"}</div>
            <p>${escapeHtml(text)}</p>
        `;
        chatMessages.appendChild(wrapper);
        chatMessages.scrollTop = chatMessages.scrollHeight;
        return wrapper;
    }

    function addPlaceChoices(data, originalMessage, useCurrentLocation) {
        const wrapper = document.createElement("div");
        wrapper.className = "message agent-message";
        wrapper.innerHTML = `
            <div class="message-label">RoadPath</div>
            <p>${escapeHtml(data.assistant_message || "원하는 장소를 선택해 주세요.")}</p>
        `;

        const list = document.createElement("div");
        list.className = "place-choice-list";
        (data.place_candidates || []).forEach((candidate) => {
            const button = document.createElement("button");
            button.type = "button";
            button.className = "place-choice-button";
            button.innerHTML = `
                <strong>${escapeHtml(candidate.name)}</strong>
                <span>${escapeHtml(candidate.address || candidate.region || "주소 확인 안 됨")}</span>
            `;
            button.addEventListener("click", () => {
                if (state.requestInFlight || wrapper.dataset.selected) return;
                wrapper.dataset.selected = "true";
                list.querySelectorAll("button").forEach((item) => { item.disabled = true; });
                submitRouteRequest(originalMessage, {
                    addUserMessage: false,
                    useCurrentLocation,
                    confirmedStart: data.start,
                    destination: data.end,
                    placeChoice: {
                        role: data.place_choice_role,
                        candidate,
                    },
                });
            });
            list.appendChild(button);
        });

        wrapper.appendChild(list);
        chatMessages.appendChild(wrapper);
        chatMessages.scrollTop = chatMessages.scrollHeight;
    }

    function setLoading(isLoading) {
        submitButton.disabled = isLoading;
        routeInput.disabled = isLoading;
        submitButton.textContent = isLoading ? "탐색 중" : "경로 탐색";
    }

    function initTabs() {
        document.querySelectorAll(".tab").forEach((button) => {
            button.addEventListener("click", () => {
                document.querySelectorAll(".tab").forEach((item) => item.classList.remove("active"));
                document.querySelectorAll(".tab-panel").forEach((item) => item.classList.remove("active"));
                button.classList.add("active");
                document.getElementById(`tab-${button.dataset.tab}`).classList.add("active");
                if (button.dataset.tab === "route" && state.map) {
                    window.setTimeout(() => state.map.invalidateSize(), 0);
                }
            });
        });
    }

    function initMap() {
        if (!window.L) {
            mapElement.hidden = true;
            mapFallback.hidden = false;
            return;
        }

        state.map = L.map("map", { zoomControl: true, preferCanvas: true }).setView([36.4, 127.8], 7);
        L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
            attribution: "&copy; OpenStreetMap contributors",
        }).addTo(state.map);
    }

    function clearRouteLayers() {
        if (state.routeAnimationTimer) {
            window.clearInterval(state.routeAnimationTimer);
            state.routeAnimationTimer = null;
        }
        if (!state.map) return;
        state.mapLayers.forEach((layer) => state.map.removeLayer(layer));
        state.mapLayers = [];
        if (state.startMarker) state.map.removeLayer(state.startMarker);
        if (state.endMarker) state.map.removeLayer(state.endMarker);
        state.startMarker = null;
        state.endMarker = null;
    }

    function toLatLngs(coords) {
        return (coords || [])
            .filter((coord) => Array.isArray(coord) && coord.length >= 2)
            .map((coord) => [Number(coord[1]), Number(coord[0])]);
    }

    function drawRouteData(route, start, end, title) {
        if (!route) return;
        mapTitle.textContent = title;
        if (!state.map) return;

        clearRouteLayers();
        const allPoints = [];
        (route.view?.paths || []).forEach((path) => {
            const latlngs = toLatLngs(path);
            if (latlngs.length < 2) return;
            allPoints.push(...latlngs);
            const baseLine = L.polyline(latlngs, {
                color: "#c8cdca",
                weight: 6,
                opacity: 0.72,
            }).addTo(state.map);
            state.mapLayers.push(baseLine);
        });

        const trafficLayers = [];
        (route.view?.traffic_segments || []).forEach((segment) => {
            const latlngs = toLatLngs(segment.coordinates);
            if (latlngs.length < 2) return;
            const casing = L.polyline(latlngs, {
                color: "#ffffff",
                weight: 10,
                opacity: 0.9,
                interactive: false,
            });
            const layer = L.polyline(latlngs, {
                color: trafficColors[segment.state] || trafficColors["알 수 없음"],
                weight: 7,
                opacity: 1,
            });
            const speedLabel = segment.speed_kmh === null || segment.speed_kmh === undefined || segment.speed_kmh === ""
                ? "확인 안 됨"
                : `${segment.speed_kmh} km/h`;
            layer.bindTooltip(`${escapeHtml(segment.road_name)} · ${escapeHtml(segment.state)} · ${escapeHtml(speedLabel)}`);
            trafficLayers.push(casing, layer);
        });

        const reducedMotion = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
        if (reducedMotion || state.navigation.active || trafficLayers.length === 0) {
            trafficLayers.forEach((layer) => {
                layer.addTo(state.map);
                state.mapLayers.push(layer);
            });
        } else {
            let cursor = 0;
            const batch = Math.max(1, Math.ceil(trafficLayers.length / 24));
            state.routeAnimationTimer = window.setInterval(() => {
                const next = Math.min(cursor + batch, trafficLayers.length);
                for (; cursor < next; cursor += 1) {
                    trafficLayers[cursor].addTo(state.map);
                    state.mapLayers.push(trafficLayers[cursor]);
                }
                if (cursor >= trafficLayers.length) {
                    window.clearInterval(state.routeAnimationTimer);
                    state.routeAnimationTimer = null;
                }
            }, 35);
        }

        if (start) {
            state.startMarker = L.circleMarker([start.lat, start.lon], {
                radius: 7,
                color: "#52675a",
                weight: 2,
                fillColor: "#e8eee9",
                fillOpacity: 1,
            }).addTo(state.map).bindPopup(`출발 · ${escapeHtml(start.name || "출발지")}`);
        }
        if (end) {
            state.endMarker = L.circleMarker([end.lat, end.lon], {
                radius: 7,
                color: "#756760",
                weight: 2,
                fillColor: "#eee9e6",
                fillOpacity: 1,
            }).addTo(state.map).bindPopup(`도착 · ${escapeHtml(end.name || "도착지")}`);
        }

        if (state.navigation.active) {
            const navigationFocus = state.navigation.routeEntered
                ? (state.navigation.currentPosition || state.currentLocation)
                : (state.navigation.plannedStart || start);
            if (navigationFocus) focusNavigationMap(navigationFocus);
        } else if (allPoints.length) {
            state.map.fitBounds(L.latLngBounds(allPoints), { padding: [28, 28] });
        }
    }

    function drawRoute(routeKey) {
        if (!state.result?.routes?.[routeKey]) return;
        state.activeRoute = routeKey;
        renderRouteSwitch();
        renderSummary();
        renderSegments();
        const route = state.result.routes[routeKey];
        const routeLabel = routeDisplayName(routeKey);
        drawRouteData(
            route,
            state.result.start,
            state.result.end,
            `${routeLabel} · ${route.search_option_label || ""}`,
        );
    }

    function renderRouteSwitch() {
        routeSwitch.innerHTML = "";
        if (state.navigation.active) {
            const badge = document.createElement("span");
            badge.className = "nav-road";
            badge.textContent = "내비게이션 진행 중";
            routeSwitch.appendChild(badge);
            return;
        }
        if (!state.result?.routes) return;

        Object.keys(state.result.routes).forEach((key) => {
            const button = document.createElement("button");
            button.type = "button";
            const isFinal = state.result.selected_route === key;
            button.textContent = `${routeDisplayName(key)}${isFinal ? " · 최종" : ""}`;
            button.classList.toggle("active", key === state.activeRoute);
            button.addEventListener("click", () => drawRoute(key));
            routeSwitch.appendChild(button);
        });
    }

    function renderSummary() {
        if (!state.result || state.result.status !== "ok") {
            resultSummary.className = "result-summary empty-state";
            resultSummary.innerHTML = `<p>${escapeHtml(state.result?.assistant_message || "경로 결과가 없습니다.")}</p>`;
            return;
        }

        const routeKey = state.activeRoute || state.result.selected_route;
        const activeRoute = state.result.routes?.[routeKey] || state.result.final;
        if (!activeRoute) {
            resultSummary.className = "result-summary empty-state";
            resultSummary.innerHTML = "<p>선택한 경로 정보를 확인할 수 없습니다.</p>";
            return;
        }

        const summary = activeRoute.summary || {};
        const counts = summary.traffic_counts || {};
        const isFinal = state.result.selected_route === routeKey;
        const routeName = routeDisplayName(routeKey);
        const judgmentTitle = isFinal ? "Agent 최종 판단" : `${routeName} 정보`;
        const judgmentText = isFinal
            ? state.result.decision_reason
            : `${routeName}의 TMAP 조회 결과입니다. 다른 경로를 선택하면 해당 경로의 거리·예상시간·교통정보로 바뀝니다.`;

        resultSummary.className = "result-summary";
        resultSummary.innerHTML = `
            <div class="summary-grid">
                <div class="metric-card">
                    <div class="metric-label">확인 중인 경로</div>
                    <div class="metric-value">${escapeHtml(routeName)}${isFinal ? " · 최종" : ""}</div>
                </div>
                <div class="metric-card">
                    <div class="metric-label">예상시간</div>
                    <div class="metric-value">${escapeHtml(summary.total_time_min)}분</div>
                </div>
                <div class="metric-card">
                    <div class="metric-label">총 거리</div>
                    <div class="metric-value">${escapeHtml(summary.total_distance_km)} km</div>
                </div>
                <div class="metric-card">
                    <div class="metric-label">탐색 방식</div>
                    <div class="metric-value" style="font-size:16px;line-height:1.4">${escapeHtml(activeRoute.search_option_label)}</div>
                </div>
                <div class="decision-card">
                    <h3>${escapeHtml(judgmentTitle)}</h3>
                    <p>${escapeHtml(judgmentText)}</p>
                    <div class="traffic-strip">
                        ${["원활", "서행", "지체", "정체", "정보없음"].map((name) => `
                            <div class="traffic-chip">
                                <strong>${escapeHtml(counts[name] ?? 0)}</strong>
                                <span>${name} 구간</span>
                            </div>
                        `).join("")}
                    </div>
                </div>
            </div>
        `;
    }

    function formatTimelineEvent(event) {
        if (event.stage !== "agent_tool_call") {
            return { label: event.label, detail: event.detail };
        }

        const toolName = String(event.detail || "").split(" · ", 1)[0];
        const toolLabels = {
            prepare_base_route: "출발지·도착지 확인 후 기본 경로 조회",
            prepare_current_route: "현재 위치에서 목적지까지 기본 경로 조회",
            find_place: "장소 정보 확인",
            search_route: "경로 조회",
            finish_route: "최종 경로 확정",
        };

        return {
            label: "Tool 호출",
            detail: toolLabels[toolName] || "경로 탐색에 필요한 정보를 확인합니다.",
        };
    }

    function renderTimeline() {
        const events = state.result?.events || [];
        if (!events.length) {
            agentTimeline.className = "timeline empty-state";
            agentTimeline.innerHTML = "<p>표시할 Agent 실행 기록이 없습니다.</p>";
            return;
        }

        agentTimeline.className = "timeline";
        agentTimeline.innerHTML = events.map((event) => {
            const displayEvent = formatTimelineEvent(event);
            return `
                <div class="timeline-item ${escapeHtml(event.stage)}">
                    <span class="timeline-dot" aria-hidden="true"></span>
                    <div class="timeline-copy">
                        <strong>${escapeHtml(displayEvent.label)}</strong>
                        <p>${escapeHtml(displayEvent.detail)}</p>
                    </div>
                </div>
            `;
        }).join("");
    }

    function buildSegmentFilters() {
        const filters = ["전체", "원활", "서행", "지체", "정체", "정보없음"];
        segmentFilters.innerHTML = "";
        filters.forEach((name) => {
            const button = document.createElement("button");
            button.type = "button";
            button.textContent = name;
            button.classList.toggle("active", state.activeSegmentFilter === name);
            button.addEventListener("click", () => {
                state.activeSegmentFilter = name;
                buildSegmentFilters();
                renderSegments();
            });
            segmentFilters.appendChild(button);
        });
    }

    function renderSegments() {
        buildSegmentFilters();
        const route = state.result?.routes?.[state.activeRoute];
        const segments = route?.summary?.traffic_segments || [];
        const filtered = state.activeSegmentFilter === "전체"
            ? segments
            : segments.filter((segment) => segment.state === state.activeSegmentFilter);

        segmentMeta.textContent = route
            ? `${routeDisplayName(state.activeRoute)} · 교통정보 세부 구간 ${segments.length}개`
            : "";

        if (!filtered.length) {
            segmentTableBody.innerHTML = `<tr><td colspan="4" class="empty-cell">해당 상태의 교통정보 구간이 없습니다.</td></tr>`;
            return;
        }

        segmentTableBody.innerHTML = filtered.map((segment) => `
            <tr>
                <td>${escapeHtml(segment.road_name)}</td>
                <td>${escapeHtml(segment.road_type_name)}</td>
                <td><span class="state-badge state-${escapeHtml(segment.state)}">${escapeHtml(segment.state)}</span></td>
                <td>${segment.speed_kmh === null || segment.speed_kmh === undefined || segment.speed_kmh === "" ? "확인 안 됨" : `${escapeHtml(segment.speed_kmh)} km/h`}</td>
            </tr>
        `).join("");
    }

    function shouldUseCurrentLocation(message) {
        const text = String(message || "");
        if (/현재\s*위치|내\s*위치|여기/.test(text)) return true;
        const hasExplicitStart = /에서|부터/.test(text);
        return !hasExplicitStart && /까지|길\s*안내|경로\s*안내/.test(text);
    }

    function geolocationErrorMessage(error) {
        if (!window.isSecureContext) {
            return "현재 위치 기능에 필요한 보안 연결이 아닙니다.";
        }
        if (!error) return "현재 위치를 확인할 수 없습니다.";

        const labels = {
            1: "위치 접근이 거부되었습니다. 위치 권한을 확인해 주세요.",
            2: "기기 또는 브라우저가 위치를 가져오지 못했습니다.",
            3: "위치 확인 시간이 초과되었습니다.",
        };

        const message = labels[error.code] || "현재 위치 확인 실패";
        return `${message} [코드 ${error.code}] ${error.message || ""}`;
    }   

    function requestCurrentLocation() {
        return new Promise((resolve, reject) => {
            if (!navigator.geolocation) {
                reject(new Error("이 브라우저에서는 현재 위치 기능을 사용할 수 없습니다."));
                return;
            }
            navigator.geolocation.getCurrentPosition(
                (position) => {
                    const location = {
                        name: "현재 위치",
                        lat: position.coords.latitude,
                        lon: position.coords.longitude,
                        accuracy: position.coords.accuracy,
                    };
                    state.currentLocation = location;
                    inputHint.textContent = "현재 위치가 필요한 요청에만 위치를 사용했습니다.";
                    updateLocationMarker(location, null, false);
                    resolve(location);
                },
                (error) => reject(new Error(geolocationErrorMessage(error))),
                { enableHighAccuracy: true },
            );
        });
    }

    function clearLocationMarker() {
        if (!state.map || !state.locationMarker) return;
        state.map.removeLayer(state.locationMarker);
        state.locationMarker = null;
    }

    function buildLocationIcon(navigationMode, heading) {
        if (navigationMode) {
            const normalizedHeading = Number.isFinite(heading) ? heading : 0;
            return L.divIcon({
                className: "",
                html: `<span class="navigation-position-marker" style="--heading:${normalizedHeading}deg" aria-hidden="true"><span class="navigation-position-arrow"></span></span>`,
                iconSize: [38, 38],
                iconAnchor: [19, 19],
            });
        }

        return L.divIcon({
            className: "",
            html: '<span class="current-location-marker" aria-hidden="true"></span>',
            iconSize: [18, 18],
            iconAnchor: [9, 9],
        });
    }

    function updateLocationMarker(location, heading = null, navigationMode = state.navigation.active) {
        if (!state.map || !location) return;
        const icon = buildLocationIcon(navigationMode, heading);
        if (state.locationMarker) {
            state.locationMarker.setLatLng([location.lat, location.lon]);
            state.locationMarker.setIcon(icon);
            return;
        }
        state.locationMarker = L.marker([location.lat, location.lon], {
            icon,
            zIndexOffset: navigationMode ? 1000 : 0,
        }).addTo(state.map);
    }

    function bearingDegrees(from, to) {
        if (!from || !to) return null;
        if (from.lat === to.lat && from.lon === to.lon) return null;
        const toRadians = (value) => value * Math.PI / 180;
        const toDegrees = (value) => value * 180 / Math.PI;
        const lat1 = toRadians(from.lat);
        const lat2 = toRadians(to.lat);
        const deltaLon = toRadians(to.lon - from.lon);
        const y = Math.sin(deltaLon) * Math.cos(lat2);
        const x = Math.cos(lat1) * Math.sin(lat2)
            - Math.sin(lat1) * Math.cos(lat2) * Math.cos(deltaLon);
        return (toDegrees(Math.atan2(y, x)) + 360) % 360;
    }

    function focusNavigationMap(location) {
        if (!state.map || !location) return;
        state.map.setView([location.lat, location.lon], 18, { animate: true });
    }

    async function submitRouteRequest(message, options = {}) {
        // 누적 개선: GPS 대기·장소 선택 중에도 같은 요청을 중복 전송하지 않는다.
        if (state.requestInFlight) return null;
        state.requestInFlight = true;
        document.querySelectorAll(".place-choice-button").forEach((button) => { button.disabled = true; });
        setLoading(true);
        if (state.navigation.active) stopNavigation(false);
        state.result = null;
        state.activeRoute = null;
        clearRouteLayers();
        renderSummary();
        renderRouteSwitch();
        renderTimeline();
        renderSegments();
        navigationStartButton.hidden = true;
        const addUser = options.addUserMessage !== false;
        const useCurrentLocation = options.useCurrentLocation ?? shouldUseCurrentLocation(message);
        let currentLocation = null;

        if (useCurrentLocation && !options.confirmedStart && options.placeChoice?.role !== "start") {
            try {
                currentLocation = await requestCurrentLocation();
            } catch (error) {
                inputHint.textContent = error.message;
                addMessage("agent", error.message + " 출발지를 직접 입력해 주세요.");
                state.requestInFlight = false;
                setLoading(false);
                return null;
            }
        } else if (!state.navigation.active) {
            state.currentLocation = null;
            clearLocationMarker();
        }

        if (addUser) addMessage("user", message);
        state.lastRequestMessage = message;
        setLoading(true);
        const loadingMessage = addMessage("agent", "TMAP 교통정보와 경로를 확인하고 있습니다", true);
        mapTitle.textContent = "Agent가 경로를 탐색하고 있습니다.";

        try {
            const payload = { message };
            if (currentLocation) payload.current_location = currentLocation;
            if (options.placeChoice) payload.place_choice = options.placeChoice;
            if (options.destination) payload.destination = options.destination;
            if (options.confirmedStart) payload.confirmed_start = options.confirmedStart;

            const response = await fetch("/api/route", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload),
            });
            const data = await response.json();
            if (!response.ok) throw new Error(data.detail || "경로 탐색 요청에 실패했습니다.");

            loadingMessage.remove();

            if (data.status === "needs_place_choice") {
                addPlaceChoices(data, message, useCurrentLocation);
                mapTitle.textContent = "장소 선택이 필요합니다.";
                return data;
            }

            state.result = data;
            state.activeRoute = data.selected_route || Object.keys(data.routes || {})[0] || null;
            addMessage("agent", data.assistant_message || "경로 탐색 결과를 확인해 주세요.");
            renderSummary();
            renderTimeline();
            renderRouteSwitch();
            renderSegments();
            navigationStartButton.hidden = data.status !== "ok";

            if (state.activeRoute) {
                drawRoute(state.activeRoute);
            } else {
                mapTitle.textContent = "추가 정보가 필요합니다.";
            }
            return data;
        } catch (error) {
            loadingMessage.remove();
            addMessage("agent", error.message || "경로 탐색 중 오류가 발생했습니다.");
            mapTitle.textContent = "경로 탐색에 실패했습니다.";
            return null;
        } finally {
            state.requestInFlight = false;
            setLoading(false);
            routeInput.focus();
        }
    }

    function haversineMeters(lat1, lon1, lat2, lon2) {
        const toRadians = (value) => value * Math.PI / 180;
        const earthRadiusMeters = 6371000;
        const dLat = toRadians(lat2 - lat1);
        const dLon = toRadians(lon2 - lon1);
        const a = Math.sin(dLat / 2) ** 2
            + Math.cos(toRadians(lat1)) * Math.cos(toRadians(lat2)) * Math.sin(dLon / 2) ** 2;
        return 2 * earthRadiusMeters * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
    }

    function nearestPointIndex(lat, lon, points) {
        let bestIndex = 0;
        let bestDistance = Number.POSITIVE_INFINITY;
        points.forEach((point, index) => {
            const distance = haversineMeters(lat, lon, point[0], point[1]);
            if (distance < bestDistance) {
                bestDistance = distance;
                bestIndex = index;
            }
        });
        return { index: bestIndex, distance: bestDistance };
    }

    function prepareNavigationGeometry(route) {
        const points = [];
        (route.view?.paths || []).forEach((path) => {
            toLatLngs(path).forEach((point) => {
                const previous = points[points.length - 1];
                if (!previous || previous[0] !== point[0] || previous[1] !== point[1]) {
                    points.push(point);
                }
            });
        });

        const remainingMeters = new Array(points.length).fill(0);
        for (let index = points.length - 2; index >= 0; index -= 1) {
            remainingMeters[index] = remainingMeters[index + 1]
                + haversineMeters(points[index][0], points[index][1], points[index + 1][0], points[index + 1][1]);
        }

        const guides = (route.view?.guides || []).map((guide, guideIndex) => {
            const coordinate = toLatLngs([guide.coordinates])[0];
            const routeIndex = coordinate && points.length
                ? nearestPointIndex(coordinate[0], coordinate[1], points).index
                : points.length - 1;
            return { ...guide, routeIndex, guideKey: `${guide.point_index ?? guideIndex}-${guide.turn_type ?? ""}` };
        }).sort((a, b) => a.routeIndex - b.routeIndex);

        return {
            points,
            remainingMeters,
            totalGeometryMeters: remainingMeters[0] || 0,
            guides,
        };
    }

    function nearestTrafficSegment(lat, lon, route) {
        let best = null;
        (route.view?.traffic_segments || []).forEach((segment) => {
            const points = toLatLngs(segment.coordinates);
            points.forEach((point) => {
                const distance = haversineMeters(lat, lon, point[0], point[1]);
                if (!best || distance < best.distance) {
                    best = { segment, distance };
                }
            });
        });
        return best;
    }

    function formatRemainingDistance(meters) {
        if (!Number.isFinite(meters)) return "-";
        if (meters < 1000) return `${Math.round(meters)} m`;
        return `${(meters / 1000).toFixed(1)} km`;
    }

    function speak(text) {
        if (!state.navigation.ttsEnabled || !text || !("speechSynthesis" in window)) return;
        window.speechSynthesis.cancel();
        const utterance = new SpeechSynthesisUtterance(text);
        utterance.lang = "ko-KR";
        window.speechSynthesis.speak(utterance);
    }

    function setNavigationRoute(route, start, destination) {
        state.navigation.route = route;
        state.navigation.destination = destination;
        state.navigation.searchOption = route.search_option;
        state.navigation.trafficCheckStart = start;
        state.navigation.trafficSignature = route.traffic_signature || [];
        state.navigation.geometry = prepareNavigationGeometry(route);
        state.navigation.currentGuideKey = null;
        state.navigation.currentTrafficSegmentKey = null;
        drawRouteData(route, start, destination, `내비게이션 · ${route.search_option_label || "경로 안내"}`);
        const navigationFocus = state.navigation.routeEntered
            ? (state.navigation.currentPosition || state.currentLocation)
            : (state.navigation.plannedStart || start);
        if (navigationFocus) focusNavigationMap(navigationFocus);
        renderRouteSwitch();
    }

    async function fetchNavigationReroute(currentLocation, announce = true) {
        if (state.navigation.rerouteInFlight || state.navigation.trafficCheckInFlight || !state.navigation.active || !state.navigation.destination) return false;
        state.navigation.rerouteInFlight = true;
        const revision = state.navigationRevision;
        try {
            const response = await fetch("/api/navigation/reroute", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    current_location: currentLocation,
                    destination: state.navigation.destination,
                    search_option: state.navigation.searchOption,
                }),
            });
            const data = await response.json();
            if (!response.ok) throw new Error(data.detail || "경로 재탐색에 실패했습니다.");
            if (!state.navigation.active || revision !== state.navigationRevision) return false;
            setNavigationRoute(data.route, data.start, data.end);
            if (announce) {
                addMessage("agent", "현재 위치를 기준으로 경로를 다시 탐색했습니다.");
                speak("경로를 다시 탐색했습니다.");
            }
            return true;
        } catch (error) {
            if (!state.navigation.active || revision !== state.navigationRevision) return false;
            addMessage("agent", error.message || "경로 재탐색에 실패했습니다.");
            return false;
        } finally {
            if (revision === state.navigationRevision) state.navigation.rerouteInFlight = false;
        }
    }

    async function checkLiveTraffic(currentLocation) {
        if (
            state.navigation.trafficCheckInFlight
            || state.navigation.rerouteInFlight
            || !state.navigation.active
            || !state.navigation.trafficCheckStart
            || !state.navigation.destination
        ) return;

        state.navigation.trafficCheckInFlight = true;
        const revision = state.navigationRevision;
        try {
            const response = await fetch("/api/navigation/traffic-check", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    start: state.navigation.trafficCheckStart,
                    destination: state.navigation.destination,
                    search_option: state.navigation.searchOption,
                    baseline_signature: state.navigation.trafficSignature,
                }),
            });
            const data = await response.json();
            if (!response.ok) throw new Error(data.detail || "실시간 교통정보 확인에 실패했습니다.");
            if (!state.navigation.active || revision !== state.navigationRevision) return;
            state.navigation.trafficSignature = data.traffic_signature || state.navigation.trafficSignature;
            if (data.changed) {
                await runAgentForTrafficChange(currentLocation);
            }
        } catch (error) {
            if (!state.navigation.active || revision !== state.navigationRevision) return;
            navRoad.textContent = error.message || "실시간 교통정보 확인 실패";
        } finally {
            if (revision === state.navigationRevision) state.navigation.trafficCheckInFlight = false;
        }
    }

    async function runAgentForTrafficChange(currentLocation) {
        if (!state.navigation.destination) return;
        const revision = state.navigationRevision;
        const previousInstruction = navInstruction.textContent;
        navInstruction.textContent = "교통상황 변화가 감지되어 경로를 다시 확인하고 있습니다.";

        try {
            const response = await fetch("/api/route", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    message: `현재 위치에서 ${state.navigation.destination.name || "목적지"}까지 실시간 교통 변화를 반영해 경로를 다시 확인해줘`,
                    current_location: currentLocation,
                    destination: state.navigation.destination,
                }),
            });
            const data = await response.json();
            if (!response.ok) throw new Error(data.detail || "실시간 교통 재탐색에 실패했습니다.");
            if (!state.navigation.active || revision !== state.navigationRevision) return;
            if (data.status !== "ok") {
                navInstruction.textContent = previousInstruction;
                return;
            }

            state.result = data;
            state.activeRoute = data.selected_route;
            renderRouteSwitch();
            renderSummary();
            renderTimeline();
            renderSegments();
            setNavigationRoute(data.final, data.start, data.end);
            addMessage("agent", `실시간 교통 변화 반영: ${data.assistant_message || "경로를 다시 확인했습니다."}`);
            speak("실시간 교통상황을 반영해 경로를 업데이트했습니다.");
        } catch (error) {
            if (!state.navigation.active || revision !== state.navigationRevision) return;
            navInstruction.textContent = previousInstruction;
            navRoad.textContent = error.message || "실시간 교통 재탐색 실패";
        }
    }

    async function handleNavigationPosition(position) {
        if (!state.navigation.active || !state.navigation.route || !state.navigation.geometry) return;

        const current = {
            name: "현재 위치",
            lat: position.coords.latitude,
            lon: position.coords.longitude,
            accuracy: position.coords.accuracy,
        };
        const gpsHeading = Number.isFinite(position.coords.heading) ? position.coords.heading : null;
        const calculatedHeading = bearingDegrees(state.navigation.previousPosition, current);
        const heading = gpsHeading ?? calculatedHeading ?? state.navigation.heading;
        const geometry = state.navigation.geometry;
        if (!geometry.points.length) return;
        const nearest = nearestPointIndex(current.lat, current.lon, geometry.points);

        state.currentLocation = current;

        if (!state.navigation.routeEntered) {
            state.navigation.previousPosition = current;
            const accuracy = Number.isFinite(current.accuracy) ? current.accuracy : null;
            if (accuracy === null || nearest.distance > accuracy) {
                navInstruction.textContent = "설정한 출발지에 도착하면 GPS 주행 안내가 시작됩니다.";
                navRoad.textContent = "현재 위치가 계획 경로의 출발 구간과 다릅니다.";
                return;
            }
            state.navigation.routeEntered = true;
            state.navigation.currentPosition = current;
            state.navigation.heading = heading;
            updateLocationMarker(current, heading, true);
            focusNavigationMap(current);
            navInstruction.textContent = "GPS 주행 안내를 시작합니다.";
            speak("GPS 주행 안내를 시작합니다.");
        } else {
            state.navigation.currentPosition = current;
            state.navigation.heading = heading;
            updateLocationMarker(current, heading, true);
            focusNavigationMap(current);
            state.navigation.previousPosition = current;
        }

        const destination = state.navigation.destination;
        const destinationDistance = haversineMeters(current.lat, current.lon, destination.lat, destination.lon);
        if (Number.isFinite(current.accuracy) && destinationDistance <= current.accuracy) {
            stopNavigation(true);
            return;
        }

        if (Number.isFinite(current.accuracy) && nearest.distance > current.accuracy) {
            navInstruction.textContent = "경로를 벗어나 재탐색하고 있습니다.";
            await fetchNavigationReroute(current, true);
            return;
        }

        const remainingGeometry = geometry.remainingMeters[nearest.index] || 0;
        const totalGeometry = geometry.totalGeometryMeters || remainingGeometry;
        const ratio = totalGeometry > 0 ? remainingGeometry / totalGeometry : 0;
        const totalDistanceMeters = Number(state.navigation.route.summary?.total_distance_km || 0) * 1000;
        const remainingDistance = totalDistanceMeters * ratio;
        const remainingMinutes = Number(state.navigation.route.summary?.total_time_min || 0) * ratio;
        const progress = totalGeometry > 0 ? ((totalGeometry - remainingGeometry) / totalGeometry) * 100 : 0;

        navDistance.textContent = formatRemainingDistance(remainingDistance);
        navTime.textContent = `${Math.max(0, Math.round(remainingMinutes))}분`;
        const arrival = new Date(Date.now() + Math.max(0, remainingMinutes) * 60 * 1000);
        navArrival.textContent = arrival.toLocaleTimeString("ko-KR", { hour: "2-digit", minute: "2-digit" });
        navProgressBar.style.width = `${Math.min(100, Math.max(0, progress))}%`;

        const nextGuide = geometry.guides.find((guide) => guide.routeIndex >= nearest.index && guide.point_type !== "S");
        if (nextGuide) {
            navInstruction.textContent = nextGuide.description || nextGuide.name || "경로를 따라 이동하세요.";
            if (nextGuide.guideKey !== state.navigation.currentGuideKey) {
                state.navigation.currentGuideKey = nextGuide.guideKey;
                speak(navInstruction.textContent);
            }
        } else {
            navInstruction.textContent = "목적지까지 경로를 따라 이동하세요.";
        }

        const trafficMatch = nearestTrafficSegment(current.lat, current.lon, state.navigation.route);
        if (trafficMatch) {
            const segment = trafficMatch.segment;
            navRoad.textContent = `${segment.road_name} · ${segment.state}`;
            state.navigation.currentTrafficSegmentKey = String(segment.segment_index);
        }

        await checkLiveTraffic(current);
    }

    async function startNavigation() {
        if (!state.result || state.result.status !== "ok" || !state.result.end || !state.result.final) return;
        if (state.navigation.active || state.requestInFlight) return;

        try {
            const plannedStart = state.result.start;
            const startsFromCurrentLocation = plannedStart?.query === "현재 위치" || plannedStart?.name === "현재 위치";
            let initialPosition = plannedStart;

            state.navigation.active = true;
            state.navigation.routeEntered = false;
            state.navigation.plannedStart = plannedStart;
            state.navigation.previousPosition = null;
            state.navigation.heading = null;
            state.navigation.destination = state.result.end;
            state.navigation.searchOption = state.result.final.search_option;

            if (startsFromCurrentLocation) {
                initialPosition = state.currentLocation || await requestCurrentLocation();
                state.navigation.currentPosition = initialPosition;
                state.navigation.routeEntered = true;
            } else {
                state.navigation.currentPosition = null;
            }

            mapElement.closest(".map-stage")?.classList.add("navigation-active");
            mapElement.closest(".map-card")?.classList.add("navigation-mode");
            navigationHud.hidden = false;
            navigationStartButton.hidden = true;

            setNavigationRoute(state.result.final, plannedStart, state.result.end);
            const routePoints = state.navigation.geometry?.points || [];
            let initialHeading = null;
            if (routePoints.length >= 2) {
                initialHeading = bearingDegrees(
                    { lat: routePoints[0][0], lon: routePoints[0][1] },
                    { lat: routePoints[1][0], lon: routePoints[1][1] },
                );
            }
            if (initialPosition) {
                state.navigation.heading = initialHeading;
                updateLocationMarker(initialPosition, initialHeading, true);
                focusNavigationMap(initialPosition);
            }

            navInstruction.textContent = startsFromCurrentLocation
                ? "현재 위치에서 길 안내를 시작합니다."
                : "설정한 출발지에서 길 안내를 시작합니다.";
            navRoad.textContent = startsFromCurrentLocation
                ? "GPS 위치 확인 중"
                : "출발지에 도착하면 GPS 주행 안내가 이어집니다.";
            speak("내비게이션을 시작합니다.");

            if (!navigator.geolocation) {
                throw new Error("이 브라우저에서는 현재 위치 기능을 사용할 수 없습니다.");
            }
            state.navigation.watchId = navigator.geolocation.watchPosition(
                (position) => { handleNavigationPosition(position); },
                (error) => {
                    addMessage("agent", geolocationErrorMessage(error));
                    stopNavigation(false);
                },
                { enableHighAccuracy: true },
            );
        } catch (error) {
            addMessage("agent", error.message || "내비게이션을 시작할 수 없습니다.");
            stopNavigation(false);
        }
    }

    function stopNavigation(arrived = false) {
        state.navigationRevision += 1;
        if (state.navigation.watchId !== null && navigator.geolocation) {
            navigator.geolocation.clearWatch(state.navigation.watchId);
        }
        state.navigation.active = false;
        state.navigation.watchId = null;
        state.navigation.route = null;
        state.navigation.geometry = null;
        state.navigation.previousPosition = null;
        state.navigation.heading = null;
        state.navigation.routeEntered = false;
        state.navigation.plannedStart = null;
        state.navigation.currentGuideKey = null;
        state.navigation.currentTrafficSegmentKey = null;
        state.navigation.rerouteInFlight = false;
        state.navigation.trafficCheckInFlight = false;
        navigationHud.hidden = true;
        mapElement.closest(".map-stage")?.classList.remove("navigation-active");
        mapElement.closest(".map-card")?.classList.remove("navigation-mode");
        if (state.currentLocation) updateLocationMarker(state.currentLocation, null, false);
        if ("speechSynthesis" in window) window.speechSynthesis.cancel();

        if (arrived) {
            addMessage("agent", "목적지에 도착했습니다. 길 안내를 종료합니다.");
            speak("목적지에 도착했습니다.");
            mapTitle.textContent = "목적지에 도착했습니다.";
        }

        navigationStartButton.hidden = !(state.result && state.result.status === "ok");
        renderRouteSwitch();
        if (state.activeRoute && state.result?.routes?.[state.activeRoute]) {
            drawRoute(state.activeRoute);
        }
    }

    function initVoiceRecognition() {
        const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
        if (!Recognition) {
            voiceButton.disabled = true;
            voiceButton.title = "이 브라우저에서는 음성 인식을 지원하지 않습니다.";
            return;
        }

        const recognition = new Recognition();
        recognition.lang = "ko-KR";
        recognition.continuous = true;
        recognition.interimResults = true;

        recognition.onresult = (event) => {
            let finalText = "";
            let interimText = "";
            for (let index = event.resultIndex; index < event.results.length; index += 1) {
                const transcript = event.results[index][0].transcript.trim();
                if (event.results[index].isFinal) finalText += `${transcript} `;
                else interimText += `${transcript} `;
            }

            const visibleText = (finalText || interimText).trim();
            if (visibleText) {
                inputHint.textContent = `음성 인식: ${visibleText}`;
            }

            if (finalText.trim()) {
                const match = finalText.trim().match(/로드\s*패스야[\s,]*(.+)/);
                if (match && match[1].trim()) {
                    routeInput.value = match[1].trim();
                    routeForm.requestSubmit();
                } else {
                    routeInput.value = finalText.trim();
                    inputHint.textContent = "'로드패스야'로 시작하면 인식한 요청을 자동 전송합니다.";
                }
            }
        };

        recognition.onerror = (event) => {
            inputHint.textContent = event.error === "not-allowed"
                ? "음성 인식 권한이 필요합니다."
                : "음성 인식을 계속할 수 없습니다.";
            if (event.error === "not-allowed") {
                state.voiceListening = false;
                voiceButton.classList.remove("active");
                voiceButton.setAttribute("aria-pressed", "false");
                voiceButton.textContent = "음성";
            }
        };

        recognition.onend = () => {
            if (state.voiceListening) {
                try { recognition.start(); } catch (_) { /* browser may already be restarting */ }
            }
        };

        state.voiceRecognition = recognition;
    }

    function toggleVoiceRecognition() {
        if (!state.voiceRecognition) return;
        state.voiceListening = !state.voiceListening;
        voiceButton.classList.toggle("active", state.voiceListening);
        voiceButton.setAttribute("aria-pressed", String(state.voiceListening));
        voiceButton.textContent = state.voiceListening ? "음성 듣는 중" : "음성";

        if (state.voiceListening) {
            try {
                state.voiceRecognition.start();
                inputHint.textContent = "'로드패스야' 다음에 목적지를 말하면 자동으로 전달됩니다.";
            } catch (_) {
                state.voiceListening = false;
            }
        } else {
            state.voiceRecognition.stop();
            inputHint.textContent = "음성 인식을 종료했습니다.";
        }
    }

    routeForm.addEventListener("submit", async (event) => {
        event.preventDefault();
        const message = routeInput.value.trim();
        if (!message) {
            routeInput.focus();
            return;
        }
        routeInput.value = "";
        await submitRouteRequest(message);
    });

    voiceButton.addEventListener("click", toggleVoiceRecognition);
    navigationStartButton.addEventListener("click", startNavigation);
    navigationStopButton.addEventListener("click", () => stopNavigation(false));
    ttsButton.addEventListener("click", () => {
        state.navigation.ttsEnabled = !state.navigation.ttsEnabled;
        ttsButton.setAttribute("aria-pressed", String(state.navigation.ttsEnabled));
        ttsButton.textContent = state.navigation.ttsEnabled ? "음성 안내 켜짐" : "음성 안내 꺼짐";
        if (!state.navigation.ttsEnabled && "speechSynthesis" in window) {
            window.speechSynthesis.cancel();
        }
    });

    initTabs();
    initMap();
    initVoiceRecognition();
    buildSegmentFilters();
})();
