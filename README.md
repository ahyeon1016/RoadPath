# RoadPath

제4회 경남 AI·SW 경진대회 대학부 「사회문제 해결형 AI Agent」 출품작입니다.

RoadPath는 사용자가 입력한 출발지와 도착지를 기준으로 실시간 교통정보와 실제 도로 구조를 분석하고, AI Agent가 필요에 따라 추가 경로 탐색 전략을 판단하여 최종 이동 경로를 제시하는 서비스입니다.

## 프로젝트 목적

기존 길찾기 서비스에서는 사용자가 제공된 경로와 교통상황을 확인하고, 다른 경로가 필요한 경우 직접 조건을 변경하거나 추가 검색을 수행해야 합니다.

RoadPath는 다음 과정을 AI Agent가 하나의 흐름으로 수행하도록 구현했습니다.

```text
사용자 요청
→ 기본 경로 및 교통정보 조회
→ 첫 경로 분석
→ 추가 경로 탐색 전략 판단
→ 실제 경로 탐색
→ 결과 평가
→ 필요 시 재탐색
→ 후보 경로 비교
→ 최종 경로 결정
```

## AI Agent Workflow

```text
출발지·도착지 입력
        ↓
TMAP 기본 경로 및 교통정보 조회
        ↓
첫 경로 분석
        ↓
출발지 주변 도로 분기 탐색
+
지체·정체구간 진입 전 도로 분기 탐색
        ↓
Gemini가 추가 탐색 전략 판단
        ↓
TMAP 경로 Tool 실행
        ↓
Python Evaluator가 결과 평가
        ↓
필요 시 Feedback을 반영하여 재탐색
        ↓
조회된 후보 경로 비교
        ↓
최종 경로 결정
```

RoadPath는 LLM이 임의로 경로나 소요시간을 생성하지 않습니다.

- **Gemini**: 추가 경로 탐색 전략 및 Tool 사용 계획 판단
- **TMAP API**: 실제 자동차 경로, 예상 소요시간, 거리 및 교통정보 조회
- **OpenStreetMap 기반 도로정보**: 출발지와 정체구간 이전의 실제 도로 분기 후보 탐색
- **Python Evaluator**: 조회된 경로 결과 평가 및 비교

최종 경로는 실제 조회된 후보 중 **예상 소요시간을 우선**하여 결정하며, 예상 소요시간이 동일한 경우 이동거리를 비교합니다.

## 주요 기능

- 출발지·도착지 기반 자동차 경로 탐색
- TMAP 기반 실시간 교통정보 확인
- 기본 경로의 세부 교통구간 분석
- 출발지 주변 실제 도로 분기 탐색
- 지체·정체구간 진입 전 도로 분기 탐색
- Gemini 기반 추가 경로 탐색 전략 판단
- 추가 경로 순차 탐색
- 탐색 결과 Feedback 및 재탐색
- Python 기반 후보 경로 평가
- 예상 소요시간 우선 최종 경로 선택
- 지도 기반 경로 시각화 및 경로 안내

## AI Agent 구성

### Goal

사용자의 출발지와 도착지를 파악하고 현재 교통상황을 고려하여 적합한 이동 경로를 탐색합니다.

### Planning

기본 경로 분석 결과와 탐색 가능한 도로 분기 후보를 바탕으로 추가로 탐색할 경로와 탐색 전략을 결정합니다.

### Reasoning

현재 경로의 교통상황, 출발지 주변 분기, 정체 이전 분기 및 기존 탐색 결과를 바탕으로 다음 행동을 판단합니다.

### Tool Use

- TMAP API
- OpenStreetMap 기반 도로정보

외부 Tool을 이용하여 실제 도로 및 경로 데이터를 조회합니다.

### Memory / State

한 번의 경로 탐색 과정에서 이미 시도한 후보와 조회된 경로 정보를 유지하여 동일한 탐색을 반복하지 않도록 합니다.

### Feedback / Evaluation

TMAP을 통해 조회한 경로를 Python Evaluator가 확인하고, 평가 결과를 다음 탐색 판단에 반영합니다.

## 기술 스택

### AI / Agent

- Google Gemini
- LangChain

### Backend

- Python

### Frontend

- JavaScript
- HTML
- CSS

### API / Data

- SK telecom TMAP API
- OpenStreetMap 기반 도로정보

## 프로젝트 구조

```text
RoadPath/
├── core/
├── static/
├── app.py
├── requirements.txt
├── .env.example
├── .gitignore
└── README.md
```

## 실행 전 준비사항

RoadPath의 실제 경로 탐색 및 AI Agent 기능을 사용하려면 다음 API Key가 필요합니다.

- **Google Gemini API Key**
- **SK telecom TMAP API Key**

보안을 위해 실제 API Key는 GitHub 저장소에 포함되어 있지 않습니다.

저장소의 `.env.example`을 참고하여 프로젝트 최상위 경로에 `.env` 파일을 생성한 후 본인이 발급받은 API 정보를 입력해야 합니다.

```env
GEMINI_API_KEY=
GEMINI_MODEL=
TMAP_API_KEY=
```

실제 `.env` 파일은 `.gitignore`에 등록되어 있으며 GitHub에 업로드되지 않습니다.

## 실행 방법

### 1. 저장소 다운로드

GitHub에서 저장소를 Clone하거나 ZIP으로 다운로드합니다.

### 2. 필요한 패키지 설치

프로젝트 폴더에서 다음 명령어를 실행합니다.

```bash
pip install -r requirements.txt
```

### 3. 환경변수 설정

`.env.example`을 참고하여 `.env` 파일을 생성하고 필요한 API 정보를 입력합니다.

### 4. RoadPath 실행

```bash
python app.py
```

## API Key 및 보안

실제 API Key는 공개 저장소에 포함하지 않습니다.

- `.env`: 실제 실행에 사용하는 환경변수 파일
- `.env.example`: 필요한 환경변수 항목을 확인하기 위한 예제 파일
- `.gitignore`: `.env`가 Git에 포함되지 않도록 설정

RoadPath를 직접 실행하려면 사용자가 본인의 Gemini API Key와 TMAP API Key를 별도로 설정해야 합니다.

## 대회 기간 개발 내용

- AI Agent 기반 경로 탐색 Workflow
- Gemini와 TMAP Tool 연동
- 기본 경로 및 교통정보 분석
- 출발지 주변 도로 분기 탐색
- 정체구간 진입 전 도로 분기 탐색
- 추가 경로 탐색 전략 판단
- 추가 경로 순차 탐색 및 Feedback 구조
- Python 기반 경로 평가
- 예상 소요시간 우선 최종 경로 선택
- 지도 기반 경로 시각화 및 사용자 인터페이스

## 정보 출처 및 외부 서비스

- Google Gemini API
- SK telecom TMAP API
- OpenStreetMap

외부 API와 데이터는 각 서비스의 이용정책에 따라 사용합니다.

## RoadPath

RoadPath는 단순히 여러 경로를 나열하는 것이 아니라,

**기본 경로 조회 → 첫 경로 분석 → 추가 탐색 계획 → Tool 실행 → 결과 평가 → 필요 시 재탐색 → 최종 경로 결정**

과정을 하나의 AI Agent Workflow로 수행하는 것을 목표로 합니다.
