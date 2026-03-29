# cogniq-server

AI 기반 소프트웨어 개발 자동화 플랫폼의 백엔드 서버.

이슈가 등록되면 AI 에이전트가 분석 → 구현 → 검증 → PR 생성까지 자동으로 수행하고,
사람은 두 번의 승인 게이트(계획 승인, PR 머지)에서만 개입한다.

```
Issue → 🤖 Plan → 🧑 Gate 1 → 🤖 Build → 🧑 Gate 2 → 🤖 Reflect
```

---

## 아키텍처

단일 FastAPI 프로세스 안에 7개 모듈이 동작하는 **Modular Monolith**.

```
cogniq-server (FastAPI, port 8000)
├── api/            # REST 엔드포인트 + SSE 실시간 스트림
├── auth/           # JWT 인증
├── domain/         # 도메인 모델 (Issue, Run, Artifact, Event)
├── registry/       # MongoDB 접근 계층 (Artifact Registry)
├── orchestrator/   # 상태 머신 + 태스크 스케줄링
├── agents/         # AI 에이전트 (Plan, Build, Reflect)
└── integrations/   # Linear, GitHub, Slack 연동
```

에이전트는 백엔드와 같은 프로세스에서 `asyncio.Task`로 실행된다.
별도 메시지 큐 없이 `asyncio.Queue + Semaphore`로 프로젝트 단위 동시성을 제어한다.

---

## 기술 스택

| 구성요소 | 기술 |
|---------|------|
| 런타임 | Python 3.12+ |
| 프레임워크 | FastAPI |
| DB | MongoDB 7 (motor async driver) |
| AI | anthropic SDK + Claude Code CLI |
| 패키지 관리 | uv |
| 컨테이너 | Docker + docker-compose |

기술 선택 근거는 [아키텍처 설계 문서](./docs/design/09-architecture.md#기술-선택-근거)에 상세히 기술되어 있다.

---

## 시작하기

### 사전 요구사항

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- Docker & Docker Compose
- Claude API Key ([console.anthropic.com](https://console.anthropic.com))

### 설치

```bash
# 의존성 설치
cd agent
uv sync

# 환경변수 설정
cp .env.example .env
# .env 파일에 아래 값 설정:
#   MONGODB_URI=mongodb://localhost:27017/cogniq?replicaSet=rs0
#   ANTHROPIC_API_KEY=sk-ant-...
#   JWT_SECRET=<랜덤 문자열>
```

### 실행

```bash
# MongoDB 기동 (Replica Set — Change Stream에 필요)
docker compose up -d mongodb

# 개발 서버 실행
uv run uvicorn cogniq.main:app --reload --port 8000

# 또는 Docker Compose로 전체 실행
docker compose up
```

### API 확인

```bash
# 헬스체크
curl http://localhost:8000/health

# OpenAPI 문서
open http://localhost:8000/docs
```

---

## 프로젝트 구조

```
agent/
├── pyproject.toml                    # 의존성 + 프로젝트 설정
├── Dockerfile
├── docker-compose.yml
├── .env.example
│
├── src/cogniq/
│   ├── main.py                       # FastAPI 앱 진입점
│   ├── config.py                     # 환경변수 기반 설정
│   ├── dependencies.py               # FastAPI 의존성 주입
│   │
│   ├── domain/                       # 도메인 모델 (외부 의존성 없음)
│   │   ├── issue.py                  # Issue, Run, Artifact, Event
│   │   ├── enums.py                  # IssueStatus, Stage, Phase, EventType
│   │   ├── verification.py           # 검증 계약 모델
│   │   └── state_machine.py          # 상태 전이 규칙
│   │
│   ├── registry/                     # Artifact Registry (MongoDB)
│   │   ├── database.py               # 연결 관리
│   │   ├── repository.py             # IssueRepository
│   │   └── metrics_repository.py     # MetricsRepository
│   │
│   ├── api/                          # HTTP 계층
│   │   ├── issues.py                 # /api/v1/issues
│   │   ├── artifacts.py              # /api/v1/issues/{id}/artifacts
│   │   ├── events.py                 # /api/v1/issues/{id}/events + SSE
│   │   ├── runs.py                   # /api/v1/issues/{id}/runs
│   │   ├── dashboard.py              # /api/v1/dashboard
│   │   ├── webhooks.py               # /api/v1/webhooks (Linear, GitHub)
│   │   └── projects.py               # /api/v1/projects
│   │
│   ├── auth/                         # 인증
│   │   ├── jwt.py                    # 토큰 생성/검증
│   │   ├── router.py                 # /api/v1/auth
│   │   └── models.py                 # User 모델
│   │
│   ├── orchestrator/                 # 오케스트레이션
│   │   ├── engine.py                 # 상태 전이 + 에이전트 디스패치
│   │   ├── scheduler.py              # asyncio Queue + Semaphore
│   │   ├── lock_manager.py           # 파일 잠금 + 실행 잠금
│   │   └── watchers.py               # Linear 이슈 감지
│   │
│   ├── agents/                       # AI 에이전트
│   │   ├── base.py                   # BaseAgent (타임아웃, 비용 추적)
│   │   ├── plan_agent.py             # Plan: 분석 + 검증 계약 + plan.md
│   │   ├── build_agent.py            # Build: 구현 + 검증 + PR
│   │   ├── reflect_agent.py          # Reflect: 메트릭 + 개선 제안
│   │   ├── claude_client.py          # Claude API 래퍼
│   │   ├── claude_code.py            # Claude Code CLI 래퍼
│   │   └── worktree.py               # Git worktree 관리
│   │
│   └── integrations/                 # 외부 연동
│       ├── linear.py                 # Linear API
│       ├── github.py                 # GitHub API
│       └── slack.py                  # Slack API
│
├── tests/
│   ├── unit/                         # 도메인 모델, 상태 머신
│   ├── integration/                  # API, MongoDB
│   └── e2e/                          # 전체 파이프라인
│
└── docs/design/                      # 설계 문서
    ├── 01-overview.md                # 프로세스 개요
    ├── 02-plan-stage.md              # Plan 단계
    ├── 03-build-stage.md             # Build 단계
    ├── 04-gates-and-reflect.md       # Gate + Reflect
    ├── 05-artifacts.md               # 아티팩트 스키마
    ├── 06-operations.md              # 운영 (동시성, 에러 복구)
    ├── 07-policies.md                # 정책 (에스컬레이션, Fast Track)
    ├── 08-artifact-registry.md       # Registry (MongoDB)
    └── 09-architecture.md            # 서비스 아키텍처
```

---

## 개발

### 테스트

```bash
# 단위 테스트
uv run pytest tests/unit/ -v

# 통합 테스트 (MongoDB 필요)
uv run pytest tests/integration/ -v

# 전체
uv run pytest
```

### 린트 & 타입 체크

```bash
uv run ruff check src/
uv run mypy src/
```

### OpenAPI → TypeScript 타입 생성

프론트엔드(`web/`)와 타입을 공유하기 위해 OpenAPI 스펙에서 TypeScript 타입을 자동 생성한다.

```bash
# 서버 실행 상태에서
npx openapi-typescript-codegen \
  --input http://localhost:8000/openapi.json \
  --output ../web/src/api/generated
```

---

## 환경변수

| 변수 | 필수 | 설명 | 기본값 |
|------|------|------|--------|
| `MONGODB_URI` | O | MongoDB 연결 URI | `mongodb://localhost:27017/cogniq?replicaSet=rs0` |
| `ANTHROPIC_API_KEY` | O | Claude API 키 | - |
| `JWT_SECRET` | O | JWT 서명 키 | - |
| `JWT_EXPIRY_MINUTES` | | 액세스 토큰 만료 | `60` |
| `LINEAR_API_KEY` | | Linear API 키 | - |
| `LINEAR_WEBHOOK_SECRET` | | Linear 웹훅 서명 검증 | - |
| `GITHUB_TOKEN` | | GitHub Personal Access Token | - |
| `GITHUB_WEBHOOK_SECRET` | | GitHub 웹훅 서명 검증 | - |
| `SLACK_BOT_TOKEN` | | Slack Bot 토큰 | - |
| `SLACK_CHANNEL` | | 알림 채널 | `#cogniq-updates` |
| `PLAN_TIMEOUT_MINUTES` | | Plan 에이전트 타임아웃 | `10` |
| `BUILD_TIMEOUT_MINUTES` | | Build 에이전트 타임아웃 | `30` |
| `BUILD_MAX_COST_USD` | | Build 이슈당 비용 상한 | `5.00` |
| `BUILD_MAX_TURNS` | | Claude Code CLI 최대 턴 | `50` |

---

## 설계 문서

프로세스 설계와 아키텍처 결정의 상세한 근거는 `docs/design/` 디렉토리에 있다.

| 문서 | 내용 |
|------|------|
| [01-overview](./docs/design/01-overview.md) | 설계 원칙, 전체 프로세스 흐름, 이슈 상태 전이 |
| [02-plan-stage](./docs/design/02-plan-stage.md) | Plan 단계, plan.md/design.md 템플릿 |
| [03-build-stage](./docs/design/03-build-stage.md) | Build 3 Phase, 검증 2계층, adversarial review |
| [04-gates-and-reflect](./docs/design/04-gates-and-reflect.md) | Gate 승인, Reflect 피드백 루프, 알림 |
| [05-artifacts](./docs/design/05-artifacts.md) | 아티팩트 스키마, 디렉토리 구조, Git 정책 |
| [06-operations](./docs/design/06-operations.md) | 동시성, 에러 복구, 멱등성, 타임아웃, 보안 |
| [07-policies](./docs/design/07-policies.md) | 에스컬레이션, Fast Track, 이슈 분해, 로드맵 |
| [08-artifact-registry](./docs/design/08-artifact-registry.md) | MongoDB Artifact Registry |
| [09-architecture](./docs/design/09-architecture.md) | 서비스 아키텍처, 기술 선택 근거 |
