# cogniq 서비스 아키텍처 설계

## Context

cogniq는 AI 기반 소프트웨어 개발 자동화 플랫폼이다.
Issue → Plan(AI) → Gate 1(사람) → Build(AI) → Gate 2(사람) → Reflect(AI) 파이프라인을 실행한다.

현재 상태:
- `web/` — React 19 프론트엔드 완성 (localhost:8000 백엔드 기대, JWT 인증, Zustand + React Query)
- `agent/` — 설계 문서 8개만 존재, 코드 없음
- 설계 문서에서 MongoDB 기반 Artifact Registry, 검증 계약 패턴, 동시성/에러복구/멱등성 정책이 정의됨

이 문서는 설계 문서를 기반으로 **실제 구현을 위한 서비스 아키텍처**를 정의한다.

---

## 아키텍처 결정: Modular Monolith

### 왜 마이크로서비스가 아닌가

| 관점 | 마이크로서비스 | 모듈러 모놀리스 |
|------|-------------|--------------|
| 현재 코드 | 0줄. 서비스 분리는 경계를 모를 때 위험 | 모듈 경계만 정의. 추후 필요 시 분리 |
| 프론트엔드 | `api.ts`가 단일 `localhost:8000` 기대 | 하나의 FastAPI가 모든 엔드포인트 서빙 |
| 데이터 | 모든 컴포넌트가 같은 MongoDB + 같은 도메인 모델 공유 | 네트워크 hop 없이 직접 접근 |
| Registry | 설계상 사이드카 (fire-and-forget) | 같은 프로세스 내 try/except로 충분 |
| 동시성 | 프로젝트 단위 잠금 — 수평 확장 불필요 | `asyncio.Semaphore`로 해결 |
| 운영 복잡도 | 서비스 메시, 서비스 간 인증, 배포 조율 | 단일 컨테이너 배포 |

**결론**: 단일 FastAPI 프로세스 + 7개 내부 모듈. 모듈 간 인터페이스를 깨끗하게 유지하여, 스케일 필요 시 모듈 단위로 서비스 분리 가능.

---

## 서비스 구성도

```
┌─────────────────────────────────────────────────────────────────┐
│                        Docker Compose                           │
│                                                                 │
│  ┌──────────────────────────────────────────────┐  ┌─────────┐ │
│  │           cogniq-server (FastAPI)             │  │         │ │
│  │           port 8000                           │  │ MongoDB │ │
│  │                                               │  │  :27017 │ │
│  │  ┌─────────┐ ┌────────────┐ ┌─────────────┐  │  │         │ │
│  │  │   api   │ │  registry  │ │orchestrator │  │  │         │ │
│  │  │ (REST)  │ │ (MongoDB)  │ │(state+sched)│  │  │         │ │
│  │  └────┬────┘ └──────┬─────┘ └──────┬──────┘  │  │         │ │
│  │       │              │              │         │  │         │ │
│  │  ┌────┴────┐ ┌──────┴─────┐ ┌──────┴──────┐  │  │         │ │
│  │  │  auth   │ │   domain   │ │   agents    │  │  │         │ │
│  │  │ (JWT)   │ │  (models)  │ │(plan/build/ │  │  │         │ │
│  │  └─────────┘ └────────────┘ │  reflect)   │  │  │         │ │
│  │                             └──────┬──────┘  │  │         │ │
│  │                            ┌───────┴───────┐  │  │         │ │
│  │                            │ integrations  │  │  │         │ │
│  │                            │(linear/github/│  │  │         │ │
│  │                            │    slack)     │  │  │         │ │
│  │                            └───────────────┘  │  │         │ │
│  └──────────────────────────────────────────────┘  └─────────┘ │
│                                                                 │
│  ┌──────────────────────┐                                       │
│  │    cogniq-web (Vite) │  ← 개발 시에만. 프로덕션은 정적 빌드   │
│  │    port 3000         │                                       │
│  └──────────────────────┘                                       │
└─────────────────────────────────────────────────────────────────┘

외부 시스템:
  ← Linear API (이슈 가져오기, 상태 업데이트, 코멘트)
  ← GitHub API (PR 생성, 머지)
  ← Slack API (알림)
  ← Claude API / Claude Code CLI (에이전트 실행)
```

---

## 모듈 구조

```
agent/
├── pyproject.toml
├── Dockerfile
├── docker-compose.yml
├── alembic/                          # MongoDB 마이그레이션 (mongomigrate 또는 스크립트)
│
├── src/cogniq/
│   ├── __init__.py
│   ├── main.py                       # FastAPI app + lifespan (startup/shutdown)
│   ├── config.py                     # Settings (Pydantic BaseSettings, 환경변수)
│   ├── dependencies.py               # FastAPI 의존성 주입 (DB, services)
│   │
│   ├── domain/                       # 도메인 모델 — 외부 의존성 없음
│   │   ├── __init__.py
│   │   ├── issue.py                  # Issue, Run, Artifact, Event 모델 (Pydantic)
│   │   ├── enums.py                  # IssueStatus, Stage, Phase, EventType 등
│   │   ├── verification.py           # Verification, AcceptanceCriteria 모델
│   │   └── state_machine.py          # 상태 전이 규칙 (allowed_transitions)
│   │
│   ├── registry/                     # Artifact Registry — MongoDB 접근 계층
│   │   ├── __init__.py
│   │   ├── repository.py             # IssueRepository (CRUD + aggregation)
│   │   ├── metrics_repository.py     # MetricsRepository (Reflect 집계)
│   │   └── database.py               # MongoDB 연결 관리 (motor async client)
│   │
│   ├── api/                          # HTTP 계층 — FastAPI 라우터
│   │   ├── __init__.py
│   │   ├── router.py                 # 라우터 통합 등록
│   │   ├── issues.py                 # /api/v1/issues — CRUD + 상태 업데이트
│   │   ├── artifacts.py              # /api/v1/issues/{id}/artifacts
│   │   ├── events.py                 # /api/v1/issues/{id}/events + SSE stream
│   │   ├── runs.py                   # /api/v1/issues/{id}/runs
│   │   ├── dashboard.py              # /api/v1/dashboard — 집계, 메트릭
│   │   ├── webhooks.py               # /api/v1/webhooks — Linear/GitHub 인바운드
│   │   ├── projects.py               # /api/v1/projects — 프론트엔드용
│   │   └── schemas.py                # API 요청/응답 스키마 (Pydantic)
│   │
│   ├── auth/                         # 인증/인가
│   │   ├── __init__.py
│   │   ├── jwt.py                    # JWT 토큰 생성/검증
│   │   ├── router.py                 # /api/v1/auth — login, register, refresh
│   │   ├── models.py                 # User 모델
│   │   └── dependencies.py           # get_current_user 의존성
│   │
│   ├── orchestrator/                 # 오케스트레이션 — 상태 머신 + 스케줄링
│   │   ├── __init__.py
│   │   ├── engine.py                 # OrchestrationEngine (상태 전이 + 에이전트 디스패치)
│   │   ├── scheduler.py              # TaskScheduler (asyncio.Queue + Semaphore)
│   │   ├── lock_manager.py           # FileLockManager (locks.toml), RunLockManager (run.lock)
│   │   ├── retry.py                  # 재시도 정책 (exponential backoff)
│   │   └── watchers.py               # LinearWatcher (폴링/웹훅으로 이슈 감지)
│   │
│   ├── agents/                       # AI 에이전트
│   │   ├── __init__.py
│   │   ├── base.py                   # BaseAgent (공통: 타임아웃, 비용 추적, Registry push)
│   │   ├── plan_agent.py             # ProductOwnerAgent (분석 + 검증 계약 + plan.md)
│   │   ├── build_agent.py            # DeveloperAgent (구현 + 검증 + adversarial)
│   │   ├── reflect_agent.py          # ReflectAgent (메트릭 분석 + 제안)
│   │   ├── claude_client.py          # Claude API 래퍼 (비용/토큰 추적 포함)
│   │   ├── claude_code.py            # Claude Code CLI 래퍼 (subprocess)
│   │   └── worktree.py               # Git worktree 관리 (생성, 정리, rebase)
│   │
│   └── integrations/                 # 외부 시스템 연동
│       ├── __init__.py
│       ├── linear.py                 # LinearClient (이슈 조회, 상태 변경, 코멘트)
│       ├── github.py                 # GitHubClient (PR 생성/업데이트, 머지)
│       └── slack.py                  # SlackClient (알림, 리마인더)
│
└── tests/
    ├── conftest.py                   # pytest 픽스처 (MongoDB testcontainer 등)
    ├── unit/
    │   ├── test_state_machine.py
    │   ├── test_repository.py
    │   └── test_verification.py
    ├── integration/
    │   ├── test_api_issues.py
    │   ├── test_orchestrator.py
    │   └── test_agents.py
    └── e2e/
        └── test_full_pipeline.py
```

---

## 모듈별 책임과 의존성

```
의존성 방향 (안쪽이 상위):

                    ┌──────────┐
                    │  domain  │  ← 순수 Python. 외부 의존성 0.
                    └────┬─────┘
                         │
              ┌──────────┼──────────┐
              │          │          │
         ┌────┴───┐ ┌───┴────┐ ┌───┴──────────┐
         │registry│ │  auth  │ │integrations  │
         │(mongo) │ │ (jwt)  │ │(linear/gh/sl)│
         └────┬───┘ └───┬────┘ └───┬──────────┘
              │          │          │
              └──────────┼──────────┘
                         │
                  ┌──────┴──────┐
                  │orchestrator │  ← registry + integrations 사용
                  └──────┬──────┘
                         │
                  ┌──────┴──────┐
                  │   agents    │  ← orchestrator + registry + integrations 사용
                  └──────┬──────┘
                         │
                  ┌──────┴──────┐
                  │     api     │  ← 모든 모듈 사용 (HTTP 진입점)
                  └─────────────┘
```

### 모듈별 상세

| 모듈 | 책임 | 주요 인터페이스 |
|------|------|--------------|
| **domain** | Issue/Run/Artifact/Event 모델 정의. 상태 전이 규칙. 비즈니스 룰 검증. | `IssueStatus`, `StateMachine.can_transition()`, `Verification` |
| **registry** | MongoDB 읽기/쓰기. 이슈 문서 CRUD + aggregation. | `IssueRepository.add_artifact()`, `.add_event()`, `.update_status()` |
| **auth** | JWT 발급/검증. User CRUD. | `create_token()`, `get_current_user()` |
| **orchestrator** | 상태 머신 실행. 에이전트 디스패치. 파일 잠금. 재시도. | `engine.handle_event()`, `scheduler.enqueue()`, `lock_manager.acquire()` |
| **agents** | AI 에이전트 실행. Claude API/CLI 호출. Worktree 관리. | `PlanAgent.run()`, `BuildAgent.run()`, `ReflectAgent.run()` |
| **integrations** | Linear/GitHub/Slack HTTP 클라이언트. | `linear.update_status()`, `github.create_pr()`, `slack.notify()` |
| **api** | FastAPI 라우터. 요청 검증. 응답 직렬화. | REST 엔드포인트 + SSE 스트림 |

---

## 통신 패턴

### 1. 프론트엔드 ↔ 백엔드: REST + SSE

```
React (Axios)  ──HTTP──▶  FastAPI (api/)
               ◀─SSE───   /events/stream
```

- 기존 프론트엔드의 JWT Bearer 인증 유지
- React Query로 REST 폴링 + SSE로 실시간 업데이트

### 2. 오케스트레이터 → 에이전트: asyncio in-process

```python
# orchestrator/scheduler.py

class TaskScheduler:
    """프로젝트 단위 동시성 제어 + 에이전트 디스패치."""

    def __init__(self, max_concurrent_per_project: int = 1):
        self._queues: dict[str, asyncio.Queue] = {}       # project_id별 큐
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._active_tasks: dict[str, asyncio.Task] = {}  # run_id → Task

    async def enqueue(self, issue_id: str, stage: str, project_id: str):
        """에이전트 작업을 프로젝트 큐에 추가."""
        queue = self._get_or_create_queue(project_id)
        await queue.put((issue_id, stage))

    async def _worker(self, project_id: str):
        """프로젝트별 워커. Semaphore로 동시 실행 1개 제한."""
        queue = self._queues[project_id]
        semaphore = self._semaphores[project_id]
        while True:
            issue_id, stage = await queue.get()
            async with semaphore:
                task = asyncio.create_task(self._run_agent(issue_id, stage))
                self._active_tasks[f"{issue_id}:{stage}"] = task
                await task
```

메시지 큐 도입하지 않는 이유:
- 에이전트 작업이 10~30분 — 일반 MQ의 visibility timeout과 맞지 않음
- 프로젝트 단위 잠금이 핵심 — in-process Semaphore로 충분
- 장애 복구는 MongoDB 상태 기반 (startup 시 `running` 상태 Run 재큐잉)

### 3. 에이전트 → Registry: 동일 프로세스 직접 호출

```python
# agents/base.py

class BaseAgent:
    def __init__(self, repo: IssueRepository, ...):
        self._repo = repo

    async def _push_artifact(self, issue_id, run_id, artifact_type, file_path):
        """Registry push — 실패해도 에이전트 중단 안 함."""
        try:
            content = self._parse_file(file_path)  # TOML → dict, MD → str
            await self._repo.add_artifact(issue_id, run_id, artifact_type, content)
        except Exception as e:
            logger.warning(f"Registry push failed: {e}")
            await self._queue_for_retry(issue_id, run_id, artifact_type, file_path)

    async def _emit_event(self, issue_id, event_type, run_id=None, payload=None):
        """이벤트 기록 — 실패해도 에이전트 중단 안 함."""
        try:
            await self._repo.add_event(issue_id, event_type, run_id, payload)
        except Exception as e:
            logger.warning(f"Event emit failed: {e}")
```

### 4. 외부 웹훅 인바운드

```
Linear  ──POST──▶  /api/v1/webhooks/linear   → orchestrator.handle_event()
GitHub  ──POST──▶  /api/v1/webhooks/github    → orchestrator.handle_event()
```

- Linear: 이슈 생성/업데이트 → Plan 트리거
- GitHub: PR 머지 → 이슈 상태 Done 전이

---

## 데이터 흐름 상세

### 이슈 전체 생명주기

```
1. Linear 웹훅: 새 이슈 감지
   → webhooks.py → orchestrator.engine.handle_event("issue_created")
   → engine: 이슈 문서 생성 (MongoDB)
   → engine: scheduler.enqueue(issue_id, "plan")

2. Plan 에이전트 실행
   → scheduler: Semaphore 획득 → PlanAgent.run()
   → PlanAgent:
     a. Linear에서 이슈 상세 가져오기 → issue.toml 저장
     b. 코드베이스 탐색 → analysis.toml 저장
     c. 검증 항목 정의 → verification.toml 저장
     d. 구현 계획 작성 → plan.md 저장
     e. (조건부) UI 설계 → design.md 저장
     f. 각 단계마다 Registry push + 이벤트 emit
   → engine: 상태 전이 → Plan Complete
   → Slack 알림: "COG-42 Plan 완료 — 승인 대기"

3. Gate 1: 사람 승인
   → (Linear에서 상태 변경 or 대시보드 승인 버튼)
   → webhooks.py 또는 api/issues.py → engine.handle_event("gate1_approved")
   → engine: 상태 전이 → Plan Approved
   → engine: scheduler.enqueue(issue_id, "build")

4. Build 에이전트 실행
   → scheduler: Semaphore 획득 → BuildAgent.run()
   → BuildAgent:
     Phase 1: Worktree 생성 → Claude Code CLI로 구현 → 커밋
     Phase 2: 기본 검증 (lint/test/typecheck/secret_scan) → 이슈별 검증 (verification.toml)
     Phase 3: Adversarial review (다른 모델)
     Ship: PR 생성 (GitHub)
   → engine: 상태 전이 → In Review
   → Slack 알림: "COG-42 PR 생성 — 리뷰 대기"

5. Gate 2: 사람 머지
   → GitHub 웹훅: PR merged
   → webhooks.py → engine.handle_event("gate2_merged")
   → engine: 상태 전이 → Done
   → Linear 상태 업데이트

6. Reflect (비동기, 주기적)
   → scheduler: 주간 cron 또는 N개 이슈 완료 시
   → ReflectAgent: Registry에서 데이터 조회 → 분석 → metrics 컬렉션 저장
```

---

## 기술 선택 근거

### 핵심 제약: "에이전트가 뭘 호출하는가"

이 프로젝트에서 기술 선택을 좌우하는 가장 큰 제약은 에이전트의 외부 의존성이다:

```
Plan Agent  → Claude API (Anthropic SDK)
Build Agent → Claude Code CLI (subprocess)
             → Git 조작 (worktree, rebase, commit)
             → 테스트/린트 실행 (pytest, ruff 등 subprocess)
```

### 언어: Python

**서비스 코드의 ~60%가 에이전트**(agents/ + orchestrator/)이며, 이 영역에서 Python 생태계가 압도적이다.

| 기준 | Python | TypeScript | Go |
|------|--------|-----------|-----|
| **Claude API SDK** | ✅ 공식. async. 가장 성숙 | ✅ 공식. 잘 관리됨 | ❌ 비공식만 존재 |
| **Claude Code CLI 연동** | ✅ subprocess + 결과 파싱 자연스러움 | ⚠️ child_process 가능 | ✅ os/exec |
| **TOML 파싱/생성** | ✅ tomllib(내장) + tomli-w | ⚠️ 서드파티 | ⚠️ 서드파티 |
| **Git 조작** | ✅ gitpython (성숙) | ⚠️ simple-git (제한적) | ✅ go-git |
| **AI 에이전트 레퍼런스** | ✅ claude-agent-sdk, computer-use 전부 Python | ⚠️ 존재하나 적음 | ❌ 거의 없음 |

**TypeScript를 선택하지 않는 이유**: 프론트엔드와 타입 공유가 가능하다는 장점이 있으나, (1) 에이전트 생태계가 Python 대비 얇고 (2) Anthropic 공식 레퍼런스가 전부 Python이라 문제 해결 시 참고 자원이 부족하다. 타입 공유는 FastAPI의 OpenAPI 스펙 → `openapi-typescript-codegen`으로 80% 커버 가능.

**Go/Kotlin을 선택하지 않는 이유**: Claude SDK가 비공식이라 장기 유지보수 리스크. 에이전트 생태계 사실상 부재.

**Python의 단점과 대응**:

| 단점 | 대응 |
|------|------|
| 타입 안전성 약함 | Pydantic v2 런타임 검증 + mypy/pyright 정적 분석 |
| 프론트엔드 타입 공유 불가 | OpenAPI → TS 코드젠 자동화 |
| 런타임 성능 | 병목이 Claude API 응답 대기(초~분)이므로 Python 속도는 무관 |
| 패키지 관리 혼란 | uv로 통일 |

### 프레임워크: FastAPI

에이전트가 같은 프로세스에서 `asyncio.create_task`로 실행되어야 하므로, **async가 네이티브인 프레임워크**가 필수.

| 기준 | FastAPI | Django | Flask |
|------|---------|--------|-------|
| **async 네이티브** | ✅ | ⚠️ ORM이 동기 | ❌ |
| **장시간 백그라운드 태스크** | ✅ asyncio.create_task | ❌ Celery 필요 | ❌ Celery 필요 |
| **Pydantic 통합** | ✅ 도메인 모델 = API 스키마 | ❌ 별도 시리얼라이저 | ❌ |
| **OpenAPI 자동 생성** | ✅ | ⚠️ 추가 패키지 | ⚠️ 추가 패키지 |
| **SSE (실시간)** | ✅ StreamingResponse | ⚠️ 불편 | ⚠️ 불편 |
| **MongoDB** | ✅ motor(async) 직접 사용 | ❌ ORM이 SQL 전제 | ✅ |

Django/Flask는 장시간 태스크에 Celery 같은 외부 큐가 필요한데, "메시지 큐 없이 in-process"로 결정했으므로 맞지 않음.

### DB: MongoDB

[08-artifact-registry.md](../docs/design/08-artifact-registry.md)에서 결정. 요약:
- 이슈 하나 = 문서 하나 (runs, artifacts, events 내장). JOIN 불필요.
- artifact type마다 스키마가 다름 → RDB에서는 전부 JSONB에 넣게 됨.
- append-only 쓰기 패턴 → 트랜잭션 불필요.
- Change Stream → SSE로 실시간 스트림 직결.

### 메시지 큐: 사용하지 않음

| 도입 시 | 미도입 시 (현재) |
|---------|--------------|
| Redis/RabbitMQ 인프라 추가 | MongoDB만 운영 |
| 에이전트 10~30분 실행 → visibility timeout 관리 복잡 | asyncio.Semaphore로 프로젝트 단위 동시성 제어 |
| 워커 프로세스 별도 관리 | 단일 프로세스 |
| 장애 복구: MQ acknowledgment | 장애 복구: MongoDB 상태 기반 (startup 시 running Run 재큐잉) |

추후 멀티노드 확장 필요 시 `scheduler.py`만 Redis 기반(arq 등)으로 교체하면 됨.

---

## 기술 스택

| 구성요소 | 선택 | 선택 이유 |
|---------|------|---------|
| **런타임** | Python 3.12+ | Claude SDK 네이티브. 에이전트 생태계 최적. Anthropic 레퍼런스 전부 Python. |
| **웹 프레임워크** | FastAPI | async 네이티브 → in-process 에이전트 실행 필수. OpenAPI 자동 생성. Pydantic 통합. |
| **DB** | MongoDB 7 (motor async driver) | 이슈 중심 문서 모델. 유연한 스키마. Change Stream → SSE. |
| **인증** | PyJWT + bcrypt | 프론트엔드의 기존 JWT Bearer 흐름 유지. |
| **AI** | anthropic SDK (async) + Claude Code CLI (subprocess) | Plan은 API 직접 호출, Build는 CLI로 코드 생성. |
| **Git** | gitpython + subprocess | worktree 관리, rebase, commit. subprocess는 CLI 호출에도 재사용. |
| **HTTP 클라이언트** | httpx (async) | Linear/GitHub/Slack 아웃바운드 통신. async 네이티브. |
| **태스크 스케줄링** | asyncio (Queue + Semaphore) | MQ 불필요. 프로세스 내 동시성 제어. 추후 arq로 교체 가능. |
| **타입 안전성 보강** | Pydantic v2 + mypy | 런타임 검증 + 정적 분석으로 Python 타입 단점 보완. |
| **프론트엔드 타입 공유** | openapi-typescript-codegen | FastAPI OpenAPI 스펙 → TypeScript 타입/클라이언트 자동 생성. |
| **설정 관리** | pydantic-settings | 환경변수 → 타입 안전 설정 객체. `.env` 파일 지원. |
| **테스트** | pytest + pytest-asyncio + testcontainers | 단위/통합 테스트. MongoDB testcontainer로 격리된 DB 테스트. |
| **패키지 관리** | uv | pip/poetry 대비 10~100x 빠름. pyproject.toml 기반. lockfile 지원. |
| **컨테이너** | Docker + docker-compose | 로컬 개발 환경 통일. 프로덕션 동일 이미지. |

---

## 배포

### 로컬 개발

```yaml
# docker-compose.yml
services:
  mongodb:
    image: mongo:7
    ports: ["27017:27017"]
    volumes: [mongo-data:/data/db]
    command: ["--replSet", "rs0"]  # Change Stream에 필요

  server:
    build: ./agent
    ports: ["8000:8000"]
    env_file: .env
    depends_on: [mongodb]
    volumes:
      - ./agent/src:/app/src         # 핫 리로드
      - ~/.ssh:/root/.ssh:ro         # Git SSH 키
      - /var/run/docker.sock:/var/run/docker.sock  # worktree용

  web:
    build: ./web
    ports: ["3000:3000"]
    depends_on: [server]
```

### 프로덕션

```
                     ┌──────────────┐
   사용자 ──HTTPS──▶ │  Nginx/ALB   │
                     │  (TLS 종단)   │
                     └──────┬───────┘
                            │
                     ┌──────┴───────┐
                     │ cogniq-server│
                     │ (uvicorn x N)│
                     └──────┬───────┘
                            │
                     ┌──────┴───────┐
                     │ MongoDB Atlas│
                     │ (Replica Set)│
                     └──────────────┘
```

- MongoDB Atlas: 관리형. Replica Set 필수 (Change Stream).
- uvicorn workers: 에이전트 작업은 단일 워커에서만 실행 (Semaphore 공유). 다중 워커 시 scheduler를 리더 선출 패턴으로 분리.
- 정적 파일: `web/` 빌드 결과를 Nginx에서 서빙. 또는 S3 + CloudFront.

---

## 핵심 인터페이스

### OrchestrationEngine

```python
class OrchestrationEngine:
    """이슈 생명주기의 중앙 제어."""

    async def handle_event(self, event: str, issue_id: str, payload: dict = {}):
        """외부 이벤트(웹훅, API 호출) → 상태 전이 + 후속 액션 디스패치."""
        match event:
            case "issue_created":
                await self._create_issue(issue_id, payload)
                await self._scheduler.enqueue(issue_id, "plan")
            case "gate1_approved":
                await self._transition(issue_id, IssueStatus.PLAN_APPROVED)
                await self._scheduler.enqueue(issue_id, "build")
            case "gate2_merged":
                await self._transition(issue_id, IssueStatus.DONE)
                await self._integrations.linear.update_status(issue_id, "Done")
            case "build_failed":
                await self._transition(issue_id, IssueStatus.BLOCKED)
                await self._integrations.slack.notify_failure(issue_id, payload)
```

### BaseAgent

```python
class BaseAgent(ABC):
    """모든 에이전트의 공통 기반."""

    def __init__(self, repo: IssueRepository, config: AgentConfig):
        self._repo = repo
        self._config = config
        self._cost_tracker = CostTracker(max_usd=config.max_cost_usd)
        self._timeout = config.timeout_minutes * 60

    async def execute(self, issue_id: str) -> RunResult:
        """타임아웃, 비용 제한, Registry push를 감싸는 실행 래퍼."""
        run_id = await self._repo.add_run(issue_id, stage=self.stage)
        try:
            async with asyncio.timeout(self._timeout):
                result = await self.run(issue_id, run_id)
            await self._repo.complete_run(issue_id, run_id)
            return result
        except TimeoutError:
            await self._handle_timeout(issue_id, run_id)
        except CostLimitExceeded:
            await self._handle_cost_limit(issue_id, run_id)
        except Exception as e:
            await self._handle_failure(issue_id, run_id, e)

    @abstractmethod
    async def run(self, issue_id: str, run_id: str) -> RunResult:
        """서브클래스가 구현하는 실제 에이전트 로직."""
        ...
```

---

## 구현 순서

### Phase 1: Foundation (1~2주)

| 작업 | 파일 |
|------|------|
| pyproject.toml + 의존성 | `agent/pyproject.toml` |
| Docker Compose | `docker-compose.yml` |
| FastAPI 골격 + lifespan | `src/cogniq/main.py` |
| 설정 관리 | `src/cogniq/config.py` |
| MongoDB 연결 | `src/cogniq/registry/database.py` |
| 도메인 모델 (Issue, Run, Artifact, Event, Enums) | `src/cogniq/domain/` |
| IssueRepository (MongoDB CRUD) | `src/cogniq/registry/repository.py` |
| Auth (JWT + User) | `src/cogniq/auth/` |
| Issues API (CRUD + 상태) | `src/cogniq/api/issues.py` |
| Projects API (프론트엔드용) | `src/cogniq/api/projects.py` |

### Phase 2: Registry + Dashboard (1주)

| 작업 | 파일 |
|------|------|
| Artifacts API | `src/cogniq/api/artifacts.py` |
| Events API + SSE stream | `src/cogniq/api/events.py` |
| Runs API | `src/cogniq/api/runs.py` |
| Dashboard API (집계) | `src/cogniq/api/dashboard.py` |
| MetricsRepository | `src/cogniq/registry/metrics_repository.py` |

### Phase 3: Orchestrator (1~2주)

| 작업 | 파일 |
|------|------|
| StateMachine (상태 전이 규칙) | `src/cogniq/domain/state_machine.py` |
| OrchestrationEngine | `src/cogniq/orchestrator/engine.py` |
| TaskScheduler (asyncio Queue + Semaphore) | `src/cogniq/orchestrator/scheduler.py` |
| LockManager (파일 잠금 + 실행 잠금) | `src/cogniq/orchestrator/lock_manager.py` |
| Webhook 수신 (Linear, GitHub) | `src/cogniq/api/webhooks.py` |

### Phase 4: Plan Agent (1~2주)

| 작업 | 파일 |
|------|------|
| BaseAgent (타임아웃, 비용, Registry push) | `src/cogniq/agents/base.py` |
| Claude API 래퍼 | `src/cogniq/agents/claude_client.py` |
| PlanAgent (분석 + 검증 계약 + plan.md) | `src/cogniq/agents/plan_agent.py` |
| LinearClient (이슈 조회, 코멘트) | `src/cogniq/integrations/linear.py` |

### Phase 5: Build Agent (2주)

| 작업 | 파일 |
|------|------|
| Claude Code CLI 래퍼 | `src/cogniq/agents/claude_code.py` |
| Worktree 관리 | `src/cogniq/agents/worktree.py` |
| BuildAgent (5 Phase) | `src/cogniq/agents/build_agent.py` |
| GitHubClient (PR 생성) | `src/cogniq/integrations/github.py` |

### Phase 6: Integrations + Reflect (1~2주)

| 작업 | 파일 |
|------|------|
| SlackClient (알림) | `src/cogniq/integrations/slack.py` |
| ReflectAgent | `src/cogniq/agents/reflect_agent.py` |
| Gate 리마인더 (cron) | `src/cogniq/orchestrator/watchers.py` |

---

## 검증 방법

### 로컬 E2E 테스트

```bash
# 1. 인프라 기동
docker compose up -d mongodb
uv run uvicorn cogniq.main:app --reload

# 2. API 헬스체크
curl http://localhost:8000/health

# 3. 이슈 생성 → Plan 트리거 확인
curl -X POST http://localhost:8000/api/v1/issues \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"issue_id": "COG-42", "title": "test", "team_key": "COG"}'

# 4. SSE 스트림으로 이벤트 수신 확인
curl -N http://localhost:8000/api/v1/events/stream?issue_id=COG-42

# 5. 프론트엔드 연동 확인
cd web && npm run dev  # localhost:3000 → localhost:8000 API 호출
```

### 단위 테스트

```bash
uv run pytest tests/unit/ -v           # 도메인 모델, 상태 머신
uv run pytest tests/integration/ -v    # MongoDB 연동, API 엔드포인트
```
