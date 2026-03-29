# cogniq-server 전체 구현 계획

## Context

cogniq-server는 AI 기반 개발 자동화 플랫폼의 백엔드이다.
프론트엔드(`web/`)는 이미 완성되어 있고, `localhost:8000`에서 REST API를 기대한다.
설계 문서 9개(`docs/design/01~09`)가 프로세스, 아티팩트, 운영 정책, 아키텍처를 정의한다.
현재 백엔드 코드는 0줄이다.

이 계획은 **프론트엔드가 동작하는 백엔드**를 먼저 만들고, 점진적으로 에이전트를 붙이는 순서로 진행한다.

---

## Phase 1: 프로젝트 스캐폴딩 + DB + 설정

> 목표: `uv run uvicorn cogniq.main:app --reload`로 서버가 뜨고, MongoDB에 연결되며, `/health`와 `/docs`가 응답한다.

### 1-1. 프로젝트 초기화

| 파일 | 내용 |
|------|------|
| `agent/pyproject.toml` | 프로젝트 메타 + 의존성 (fastapi, uvicorn, motor, pydantic-settings, pyjwt, bcrypt, httpx, anthropic, gitpython, tomli-w) |
| `agent/.python-version` | `3.12` |
| `agent/.env.example` | 전체 환경변수 템플릿 |
| `agent/.gitignore` | `__pycache__`, `.env`, `.cogniq/issue.toml` 등 |

### 1-2. Docker Compose

| 파일 | 내용 |
|------|------|
| `docker-compose.yml` (루트) | mongodb (replSet rs0) + mongo-init (rs.initiate) + server (port 8000) + web (port 3000) |

MongoDB를 Replica Set으로 띄워야 Change Stream이 동작한다. `mongo-init` 서비스가 `rs.initiate()`를 실행.

### 1-3. FastAPI 앱 + 설정

| 파일 | 내용 |
|------|------|
| `src/cogniq/__init__.py` | 빈 파일 |
| `src/cogniq/main.py` | FastAPI app 생성. lifespan으로 MongoDB 연결/해제. CORS 설정 (localhost:3000). 라우터 등록. `/health` 엔드포인트. |
| `src/cogniq/config.py` | `class Settings(BaseSettings)` — 전체 환경변수를 Pydantic으로 정의. `.env` 파일 로드. |
| `src/cogniq/dependencies.py` | `get_db()`, `get_settings()`, `get_repository()` — FastAPI Depends 팩토리. |

### 1-4. MongoDB 연결

| 파일 | 내용 |
|------|------|
| `src/cogniq/registry/__init__.py` | |
| `src/cogniq/registry/database.py` | `Motor` AsyncIOMotorClient 싱글턴. `connect()`, `disconnect()`, `get_database()`. 인덱스 생성 (`ensure_indexes()`). |

**인덱스 목록** (issues 컬렉션):
```python
{ "status": 1 }
{ "team_key": 1, "status": 1 }
{ "project_id": 1, "status": 1 }
{ "updated_at": -1 }
{ "artifacts.type": 1 }
{ "runs.stage": 1, "runs.status": 1 }
{ "events.type": 1, "events.occurred_at": -1 }
```

### 1-5. 도메인 모델

| 파일 | 내용 |
|------|------|
| `src/cogniq/domain/__init__.py` | |
| `src/cogniq/domain/enums.py` | `IssueStatus`, `IssuePriority`, `Stage`, `Phase`, `RunStatus`, `EventType`, `ArtifactType`, `DocumentType`, `MemberRole` |
| `src/cogniq/domain/issue.py` | `Issue`, `Run`, `Artifact`, `Event`, `IssueSummary` — Pydantic BaseModel |
| `src/cogniq/domain/verification.py` | `AcceptanceCriteria`, `SafetyCriteria`, `Verification`, `VerifyResult` |
| `src/cogniq/domain/state_machine.py` | `StateMachine` — `can_transition(from, to) -> bool`, `ALLOWED_TRANSITIONS` dict |

**IssueStatus** (프론트엔드 enum과 매핑):

| 백엔드 (domain) | 프론트엔드 (types) | 설명 |
|-----------------|-------------------|------|
| `backlog` | `BACKLOG` | 초기 상태 |
| `in_analysis` | `IN_ANALYSIS` | Plan 실행 중 |
| `plan_complete` | `IN_ANALYSIS` | Plan 완료, Gate 1 대기 |
| `plan_approved` | `IN_ANALYSIS` | Gate 1 승인 |
| `in_progress` | `IN_PROGRESS` | Build 실행 중 |
| `in_review` | `IN_REVIEW` | PR 생성, Gate 2 대기 |
| `blocked` | `IN_PROGRESS` | Build 실패 |
| `done` | `DONE` | 머지 완료 |
| `cancelled` | `CANCELLED` | 취소 |

API 직렬화 시 프론트엔드 enum으로 변환하는 매핑 함수를 `api/schemas.py`에 둔다.

### 검증

```bash
docker compose up -d mongodb
uv sync && uv run uvicorn cogniq.main:app --reload --port 8000
curl http://localhost:8000/health          # → {"status": "ok"}
open http://localhost:8000/docs            # → Swagger UI
```

---

## Phase 2: 인증 + 사용자

> 목표: 프론트엔드의 로그인/회원가입/토큰 리프레시가 동작한다.

| 파일 | 내용 |
|------|------|
| `src/cogniq/auth/__init__.py` | |
| `src/cogniq/auth/models.py` | `User` Pydantic 모델 + `UserInDB` (hashed_password 포함). MongoDB `users` 컬렉션 사용. |
| `src/cogniq/auth/jwt.py` | `create_access_token()`, `create_refresh_token()`, `decode_token()`. PyJWT + HS256. |
| `src/cogniq/auth/password.py` | `hash_password()`, `verify_password()`. bcrypt. |
| `src/cogniq/auth/dependencies.py` | `get_current_user()` — Bearer 토큰 검증 → User 반환. FastAPI Depends. |
| `src/cogniq/auth/router.py` | 아래 3개 엔드포인트 |

**엔드포인트**:

```
POST /api/v1/auth/login
  Request:  { email: str, password: str }
  Response: { user: User, access_token: str, refresh_token: str }

POST /api/v1/auth/register
  Request:  { name: str, email: str, password: str, inviteCode: str }
  Response: { user: User, access_token: str, refresh_token: str }
  Note:     inviteCode 검증 (정규식 ^[A-Z0-9]{6,12}$). 초기에는 하드코딩 또는 환경변수.

POST /api/v1/auth/refresh
  Request:  { refresh_token: str }
  Response: { access_token: str }
```

### 검증

```bash
# 회원가입
curl -X POST http://localhost:8000/api/v1/auth/register \
  -d '{"name":"Bob","email":"bob@test.com","password":"test1234","inviteCode":"COGNIQ01"}'

# 로그인
curl -X POST http://localhost:8000/api/v1/auth/login \
  -d '{"email":"bob@test.com","password":"test1234"}'

# 프론트엔드에서 로그인 → 대시보드 진입 확인
```

---

## Phase 3: 프로젝트 + 이슈 CRUD

> 목표: 프론트엔드의 프로젝트 선택, 칸반 보드, 이슈 생성이 동작한다.

### 3-1. 프로젝트

| 파일 | 내용 |
|------|------|
| `src/cogniq/api/projects.py` | 프로젝트 CRUD |

```
GET  /api/v1/projects                       → 현재 사용자의 프로젝트 목록
POST /api/v1/projects                       → 프로젝트 생성
GET  /api/v1/projects/{project_id}          → 프로젝트 상세
PATCH /api/v1/projects/{project_id}         → 프로젝트 수정
```

MongoDB `projects` 컬렉션: `{ _id, name, slug, description, owner_id, repo_url, created_at }`

### 3-2. 이슈

| 파일 | 내용 |
|------|------|
| `src/cogniq/api/issues.py` | 이슈 CRUD + 상태 변경 |
| `src/cogniq/api/schemas.py` | API 요청/응답 Pydantic 모델. 백엔드↔프론트엔드 enum 매핑. |
| `src/cogniq/registry/repository.py` | `IssueRepository` — MongoDB issues 컬렉션 CRUD |

```
GET  /api/v1/projects/{project_id}/issues   → 이슈 목록 (필터: status, priority, 페이지네이션)
POST /api/v1/projects/{project_id}/issues   → 이슈 생성 (BACKLOG 상태)
GET  /api/v1/issues/{issue_id}              → 이슈 상세 (runs + artifacts + events + summary)
PUT  /api/v1/issues/{issue_id}              → 이슈 수정 (title, description, priority, assignee)
PUT  /api/v1/issues/{issue_id}/status       → 이슈 상태 변경 (StateMachine 검증)
```

**IssueRepository 메서드**:

```python
class IssueRepository:
    async def create(self, issue: Issue) -> Issue
    async def get(self, issue_id: str) -> Issue | None
    async def list_by_project(self, project_id: str, status: str | None, skip: int, limit: int) -> list[Issue]
    async def update(self, issue_id: str, updates: dict) -> Issue
    async def update_status(self, issue_id: str, new_status: IssueStatus) -> Issue
    async def add_run(self, issue_id: str, run: Run) -> str  # returns run_id
    async def update_run(self, issue_id: str, run_id: str, updates: dict) -> None
    async def add_artifact(self, issue_id: str, artifact: Artifact) -> str
    async def add_event(self, issue_id: str, event: Event) -> str
    async def update_summary(self, issue_id: str) -> None  # 캐시 재계산
```

### 검증

```bash
# 프론트엔드에서:
# 1. 프로젝트 생성 → 프로젝트 선택기에 표시
# 2. 칸반 보드에서 이슈 생성 → BACKLOG 컬럼에 카드 표시
# 3. 카드 드래그 → 상태 변경
```

---

## Phase 4: 파이프라인 + 문서 API

> 목표: 프론트엔드의 파이프라인 뷰어, 문서 볼트가 동작한다.

### 4-1. 파이프라인 (Runs + Artifacts 조합)

| 파일 | 내용 |
|------|------|
| `src/cogniq/api/pipelines.py` | 프론트엔드의 AgentPipeline/AgentRun 형식으로 변환하여 응답 |

```
GET /api/v1/issues/{issue_id}/pipeline      → AgentPipeline + AgentRun[] 형식 응답
POST /api/v1/issues/{issue_id}/pipeline/retry → 실패한 파이프라인 재시도 (이슈 상태 → plan_approved 또는 재build)
```

프론트엔드의 `AgentPipeline`은 설계 문서의 `Run`과 1:1이 아니다. 변환 로직:

```python
# 프론트엔드가 기대하는 구조:
AgentPipeline = {
    id: str,
    issueId: str,
    status: "PENDING" | "RUNNING" | "COMPLETED" | "FAILED",
    currentStep: "PO_AGENT" | "DESIGNER_AGENT" | "DEV_DESIGN_AGENT" | "CODE_IMPLEMENTATION",
    startedAt: str,
    completedAt: str
}

AgentRun = {
    id: str,
    pipelineId: str,
    step: AgentStep,
    status: RunStatus,
    logs: str,
    tokensUsed: int,
    costUsd: float,
    error: str | None,
    startedAt: str,
    completedAt: str
}

# 매핑:
# Plan stage의 run → PO_AGENT step (분석) + DESIGNER_AGENT step (design.md) + DEV_DESIGN_AGENT step (plan.md)
# Build stage의 run → CODE_IMPLEMENTATION step
```

### 4-2. Runs + Artifacts + Events API (Registry)

| 파일 | 내용 |
|------|------|
| `src/cogniq/api/runs.py` | Run 관련 CRUD |
| `src/cogniq/api/artifacts.py` | Artifact push/조회 |
| `src/cogniq/api/events.py` | 이벤트 기록 + 타임라인 조회 + SSE 스트림 |

```
# Runs
POST /api/v1/issues/{issue_id}/runs                     → Run 생성
PATCH /api/v1/issues/{issue_id}/runs/{run_id}            → Run 상태 업데이트
GET  /api/v1/issues/{issue_id}/runs                      → Run 목록

# Artifacts
POST /api/v1/issues/{issue_id}/artifacts                 → Artifact 추가 (TOML→JSON)
GET  /api/v1/issues/{issue_id}/artifacts                 → Artifact 목록 (filter: type)
GET  /api/v1/issues/{issue_id}/artifacts/latest           → 타입별 최신 (query: type)

# Events
POST /api/v1/issues/{issue_id}/events                    → 이벤트 기록
GET  /api/v1/issues/{issue_id}/events                    → 이벤트 타임라인
GET  /api/v1/events/stream                               → SSE 스트림 (query: issue_id, project_id)
```

**SSE 구현**: MongoDB Change Stream → FastAPI StreamingResponse

```python
@router.get("/events/stream")
async def event_stream(issue_id: str | None = None, db = Depends(get_db)):
    async def generate():
        pipeline = []
        if issue_id:
            pipeline.append({"$match": {"fullDocument._id": issue_id}})
        async with db.issues.watch(pipeline) as stream:
            async for change in stream:
                event_data = extract_latest_event(change)
                if event_data:
                    yield f"data: {json.dumps(event_data)}\n\n"
    return StreamingResponse(generate(), media_type="text/event-stream")
```

### 4-3. 문서

| 파일 | 내용 |
|------|------|
| `src/cogniq/api/documents.py` | 문서 CRUD |

```
GET  /api/v1/projects/{project_id}/documents             → 문서 목록 (filter: type, search)
POST /api/v1/projects/{project_id}/documents             → 문서 생성
GET  /api/v1/documents/{document_id}                     → 문서 상세
PUT  /api/v1/documents/{document_id}                     → 문서 수정
```

MongoDB `documents` 컬렉션: `{ _id, type, title, content, version, issue_id, project_id, created_at, updated_at }`

에이전트가 생성하는 plan.md, design.md, review.md도 이 컬렉션에 저장 (issue_id 연결).

### 4-4. 대시보드 집계

| 파일 | 내용 |
|------|------|
| `src/cogniq/api/dashboard.py` | 집계 API |
| `src/cogniq/registry/metrics_repository.py` | metrics 컬렉션 CRUD |

```
GET /api/v1/dashboard/active                              → 진행 중 이슈 + 현재 단계
GET /api/v1/dashboard/metrics                             → 기간별 성공률, 비용, 시간
GET /api/v1/dashboard/issues/{issue_id}/timeline          → 이벤트+산출물 통합 타임라인
```

### 검증

```bash
# 프론트엔드에서:
# 1. 이슈 클릭 → 파이프라인 뷰어에 단계별 상태 표시
# 2. 문서 볼트에서 문서 목록/상세 확인
# 3. SSE 스트림 연결 확인 (브라우저 DevTools Network 탭)
```

---

## Phase 5: 오케스트레이터 + 상태 머신

> 목표: 이슈 상태 전이가 규칙에 따라 동작하고, 에이전트 디스패치 준비가 완료된다.

| 파일 | 내용 |
|------|------|
| `src/cogniq/orchestrator/__init__.py` | |
| `src/cogniq/orchestrator/engine.py` | `OrchestrationEngine` — 이벤트 수신 → 상태 전이 → 후속 액션 |
| `src/cogniq/orchestrator/scheduler.py` | `TaskScheduler` — asyncio.Queue + Semaphore. 프로젝트별 워커. |
| `src/cogniq/orchestrator/lock_manager.py` | `FileLockManager` (파일 잠금), `RunLockManager` (실행 잠금) |
| `src/cogniq/orchestrator/retry.py` | `RetryPolicy` — exponential backoff, max attempts |
| `src/cogniq/orchestrator/watchers.py` | `GateWatcher` — 미승인 Gate 리마인더 (주기적 체크) |

**OrchestrationEngine 이벤트 핸들링**:

```python
class OrchestrationEngine:
    async def handle_event(self, event: str, issue_id: str, payload: dict = {}):
        match event:
            case "issue_created":
                await self._repo.create(issue)
                await self._scheduler.enqueue(issue_id, "plan", project_id)
            case "plan_completed":
                await self._transition(issue_id, IssueStatus.PLAN_COMPLETE)
                if self._is_fast_track(issue_id):
                    await self._transition(issue_id, IssueStatus.PLAN_APPROVED)
                    await self._scheduler.enqueue(issue_id, "build", project_id)
                else:
                    await self._notify_gate1(issue_id)
            case "gate1_approved":
                await self._transition(issue_id, IssueStatus.PLAN_APPROVED)
                await self._scheduler.enqueue(issue_id, "build", project_id)
            case "build_completed":
                await self._transition(issue_id, IssueStatus.IN_REVIEW)
                await self._notify_gate2(issue_id)
            case "build_failed":
                await self._transition(issue_id, IssueStatus.BLOCKED)
                await self._integrations.slack.notify_failure(issue_id, payload)
            case "gate2_merged":
                await self._transition(issue_id, IssueStatus.DONE)
```

**TaskScheduler**:

```python
class TaskScheduler:
    def __init__(self, max_concurrent_per_project: int = 1):
        self._queues: dict[str, asyncio.Queue] = {}
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._active_tasks: dict[str, asyncio.Task] = {}

    async def enqueue(self, issue_id: str, stage: str, project_id: str): ...
    async def cancel(self, issue_id: str, stage: str): ...
    async def recover_on_startup(self): ...  # running 상태 Run 재큐잉
```

**Startup 복구**: `main.py`의 lifespan에서 `scheduler.recover_on_startup()` 호출.
MongoDB에서 `runs.status == "running"`인 이슈를 찾아 재큐잉.

### 검증

```bash
# 단위 테스트
uv run pytest tests/unit/test_state_machine.py -v
uv run pytest tests/unit/test_scheduler.py -v

# 통합 테스트: 이슈 생성 → plan 스케줄링 확인 (에이전트 없이 mock)
uv run pytest tests/integration/test_orchestrator.py -v
```

---

## Phase 6: 웹훅 + 외부 연동 클라이언트

> 목표: Linear/GitHub 웹훅을 수신하고, 아웃바운드 API 호출 기반이 준비된다.

| 파일 | 내용 |
|------|------|
| `src/cogniq/api/webhooks.py` | 웹훅 수신 엔드포인트 + 서명 검증 |
| `src/cogniq/integrations/__init__.py` | |
| `src/cogniq/integrations/linear.py` | `LinearClient` — 이슈 조회, 상태 변경, 코멘트 작성 |
| `src/cogniq/integrations/github.py` | `GitHubClient` — PR 생성/업데이트, 머지 상태 감지 |
| `src/cogniq/integrations/slack.py` | `SlackClient` — 알림 전송, 리마인더 |

```
POST /api/v1/webhooks/linear    → 서명 검증 → engine.handle_event()
POST /api/v1/webhooks/github    → 서명 검증 → engine.handle_event()
```

**LinearClient**:
```python
class LinearClient:
    async def get_issue(self, issue_id: str) -> dict
    async def update_status(self, issue_id: str, status: str) -> None
    async def add_comment(self, issue_id: str, body: str) -> None
    async def create_sub_issues(self, parent_id: str, sub_issues: list[dict]) -> list[str]
```

**GitHubClient**:
```python
class GitHubClient:
    async def create_pr(self, repo: str, branch: str, title: str, body: str) -> dict
    async def update_pr(self, repo: str, pr_number: int, body: str) -> None
    async def get_pr_status(self, repo: str, pr_number: int) -> dict
```

**SlackClient**:
```python
class SlackClient:
    async def notify(self, channel: str, message: str, blocks: list | None = None) -> None
    async def notify_plan_complete(self, issue_id: str, summary: dict) -> None
    async def notify_build_failure(self, issue_id: str, postmortem: dict) -> None
    async def notify_gate_reminder(self, issue_id: str, gate: str, hours_waiting: int) -> None
```

### 검증

```bash
# Linear 웹훅 시뮬레이션
curl -X POST http://localhost:8000/api/v1/webhooks/linear \
  -H "X-Linear-Signature: ..." \
  -d '{"type":"Issue","action":"create","data":{"id":"...","title":"test"}}'
```

---

## Phase 7: BaseAgent + Claude 클라이언트

> 목표: 에이전트 공통 기반 (타임아웃, 비용 추적, Registry push, 에러 처리)이 완성된다.

| 파일 | 내용 |
|------|------|
| `src/cogniq/agents/__init__.py` | |
| `src/cogniq/agents/base.py` | `BaseAgent` — 타임아웃, 비용 추적, Registry push, postmortem 생성 |
| `src/cogniq/agents/claude_client.py` | `ClaudeClient` — anthropic SDK 래퍼. async. 토큰/비용 추적. |
| `src/cogniq/agents/claude_code.py` | `ClaudeCodeRunner` — CLI subprocess 래퍼. stdout 파싱. 턴 카운트. |
| `src/cogniq/agents/worktree.py` | `WorktreeManager` — git worktree add/remove, rebase, conflict 감지 |

**BaseAgent 핵심**:

```python
class BaseAgent(ABC):
    stage: Stage  # 서브클래스가 정의

    async def execute(self, issue_id: str) -> RunResult:
        run_id = await self._repo.add_run(issue_id, stage=self.stage)
        await self._emit_event(issue_id, f"{self.stage}_started", run_id)
        try:
            async with asyncio.timeout(self._timeout):
                result = await self.run(issue_id, run_id)
            await self._repo.complete_run(issue_id, run_id)
            await self._emit_event(issue_id, f"{self.stage}_completed", run_id)
            return result
        except TimeoutError:
            await self._create_postmortem(issue_id, run_id, category="timeout")
            raise
        except CostLimitExceeded:
            await self._create_postmortem(issue_id, run_id, category="cost_limit")
            raise
        except Exception as e:
            await self._create_postmortem(issue_id, run_id, category="permanent", error=str(e))
            raise

    async def _push_artifact(self, issue_id, run_id, artifact_type, file_path):
        """fire-and-forget — 실패해도 에이전트 중단 안 함."""
        try:
            content = self._parse_file(file_path)
            await self._repo.add_artifact(issue_id, Artifact(...))
        except Exception as e:
            logger.warning(f"Registry push failed: {e}")

    @abstractmethod
    async def run(self, issue_id: str, run_id: str) -> RunResult: ...
```

**CostTracker**:
```python
class CostTracker:
    def __init__(self, max_usd: float):
        self.total_usd = 0.0
        self.total_tokens = 0
        self._max_usd = max_usd

    def add(self, input_tokens: int, output_tokens: int, model: str):
        cost = calculate_cost(input_tokens, output_tokens, model)
        self.total_usd += cost
        self.total_tokens += input_tokens + output_tokens
        if self.total_usd > self._max_usd:
            raise CostLimitExceeded(self.total_usd, self._max_usd)
```

### 검증

```bash
uv run pytest tests/unit/test_base_agent.py -v
uv run pytest tests/unit/test_cost_tracker.py -v
uv run pytest tests/unit/test_worktree.py -v
```

---

## Phase 8: Plan Agent

> 목표: 이슈가 생성되면 자동으로 Plan이 실행되어 analysis.toml, verification.toml, plan.md가 생성된다.

| 파일 | 내용 |
|------|------|
| `src/cogniq/agents/plan_agent.py` | `PlanAgent(BaseAgent)` |

**PlanAgent.run() 흐름**:

```python
async def run(self, issue_id: str, run_id: str) -> RunResult:
    # 1. 이슈 데이터 수집
    issue_data = await self._linear.get_issue(issue_id)
    issue_toml = build_issue_toml(issue_data)
    write_local(".cogniq/issue.toml", issue_toml)
    await self._push_artifact(issue_id, run_id, "issue_snapshot", path)

    # 2. 중단 조건 확인
    if not self._validate_issue(issue_data):
        await self._linear.add_comment(issue_id, "Plan 중단: 요구사항 불충분")
        return RunResult(status="failed", reason="invalid_issue")

    # 3. 코드베이스 분석 (Claude API)
    analysis = await self._claude.analyze_codebase(issue_data, repo_path)
    analysis_toml = build_analysis_toml(analysis)
    write_local(".cogniq/analysis.toml", analysis_toml)
    await self._push_artifact(issue_id, run_id, "analysis", path)

    # 4. 검증 항목 정의
    verification = await self._claude.define_verification(issue_data, analysis)
    verification_toml = build_verification_toml(verification)
    write_local(".cogniq/verification.toml", verification_toml)
    await self._push_artifact(issue_id, run_id, "verification", path)

    # 5. plan.md 생성
    plan_md = await self._claude.generate_plan(issue_data, analysis)
    write_local(".cogniq/plan.md", plan_md)
    await self._push_artifact(issue_id, run_id, "plan_md", path)

    # 6. design.md (조건부)
    if analysis.design_required:
        design_md = await self._claude.generate_design(issue_data, analysis)
        write_local(".cogniq/design.md", design_md)
        await self._push_artifact(issue_id, run_id, "design_md", path)

    # 7. 분해 판단
    if analysis.should_split:
        sub_issues = await self._linear.create_sub_issues(issue_id, analysis.sub_issues)

    # 8. 안전 플래그
    if analysis.safety_flags:
        await self._linear.add_comment(issue_id, f"⚠️ 안전 플래그: {analysis.safety_flags}")

    return RunResult(status="success")
```

### 검증

```bash
# 이슈 생성 → Plan 자동 실행 → MongoDB에 artifacts 확인
curl -X POST http://localhost:8000/api/v1/projects/{id}/issues \
  -d '{"title":"프로필 수정","description":"WHEN... THEN..."}'

# MongoDB에서 확인
mongosh --eval "db.issues.findOne({title:'프로필 수정'})"

# 프론트엔드 파이프라인 뷰어에서 PO_AGENT 단계 확인
```

---

## Phase 9: Build Agent

> 목표: Plan 승인 후 자동으로 코드 구현 → 검증 → PR 생성이 실행된다.

| 파일 | 내용 |
|------|------|
| `src/cogniq/agents/build_agent.py` | `BuildAgent(BaseAgent)` |

**BuildAgent.run() 흐름**:

```python
async def run(self, issue_id: str, run_id: str) -> RunResult:
    issue = await self._repo.get(issue_id)
    plan_md = self._get_latest_artifact(issue, "plan_md")
    verification = self._get_latest_artifact(issue, "verification")

    # Phase 1: 구현
    await self._emit_event(issue_id, "build_phase_completed", run_id, {"phase": "phase1_start"})
    worktree = await self._worktree.create(issue_id)
    try:
        result = await self._claude_code.run(
            worktree_path=worktree.path,
            prompt=self._build_prompt(plan_md, issue),
            max_turns=self._config.max_turns,
        )
        await self._worktree.commit(worktree, f"{issue.identifier}: {issue.title}")
        await self._emit_event(issue_id, "build_phase_completed", run_id, {"phase": "phase1"})

        # Phase 2: 검증
        verify_result = await self._run_verification(worktree, verification)
        await self._push_artifact(issue_id, run_id, "verify_result", verify_result_path)

        for item in verify_result.results:
            event_type = "verification_passed" if item.status == "pass" else "verification_failed"
            await self._emit_event(issue_id, event_type, run_id, {"item_id": item.id})

        if verify_result.overall_status == "fail":
            # 자동 수정 (최대 2회)
            for attempt in range(2):
                fix_result = await self._auto_fix(worktree, verify_result)
                if fix_result.overall_status == "pass":
                    break
            else:
                raise VerificationFailed(verify_result)

        await self._emit_event(issue_id, "build_phase_completed", run_id, {"phase": "phase2"})

        # Phase 3: Adversarial Review
        review = await self._adversarial_review(worktree, issue)
        if review.has_critical:
            for cycle in range(2):  # 무한루프 방지
                await self._auto_fix_critical(worktree, review)
                verify_result = await self._run_verification(worktree, verification)
                review = await self._adversarial_review(worktree, issue)
                if not review.has_critical:
                    break
            # 남은 CRITICAL → WARNING으로 전환

        await self._emit_event(issue_id, "build_phase_completed", run_id, {"phase": "phase3"})

        # Ship: PR 생성
        await self._worktree.rebase(worktree)
        pr = await self._github.create_pr(
            repo=issue.repo_url,
            branch=worktree.branch,
            title=f"{issue.identifier}: {issue.title}",
            body=self._build_pr_body(verify_result, review),
        )

        build_result = BuildResult(pr=pr, ...)
        await self._push_artifact(issue_id, run_id, "build_result", build_result_path)

        return RunResult(status="success", pr_url=pr.url)

    except Exception:
        await self._worktree.rollback(worktree)
        raise
    finally:
        # worktree는 유지 (디버깅용). 성공 시에만 정리 옵션.
        pass
```

**_run_verification** (Phase 2):

```python
async def _run_verification(self, worktree, verification) -> VerifyResult:
    results = []

    # Step 1: 기본 검증 (build-defaults)
    defaults = {}
    defaults["lint"] = await self._run_command(worktree, "ruff check .")
    defaults["typecheck"] = await self._run_command(worktree, "ruff check --select ANN .")
    defaults["test"] = await self._run_command(worktree, "uv run pytest")
    defaults["secret_scan"] = await self._run_secret_scan(worktree)

    # Step 2: 이슈별 검증 (verification.toml)
    for item in verification.acceptance:
        if item.verify_method == "test":
            result = await self._verify_test(worktree, item)
        elif item.verify_method == "existence":
            result = await self._verify_existence(worktree, item)
        else:  # manual
            result = VerificationItemResult(id=item.id, status="skip", detail="PR 체크리스트")
        results.append(result)

    return VerifyResult(defaults=defaults, results=results, ...)
```

### 검증

```bash
# Gate 1 승인 → Build 자동 실행 → PR 생성 확인
curl -X PUT http://localhost:8000/api/v1/issues/{id}/status \
  -d '{"status":"plan_approved"}'

# GitHub에서 PR 확인
# 프론트엔드 파이프라인 뷰어에서 CODE_IMPLEMENTATION 단계 확인
```

---

## Phase 10: Reflect Agent + 마무리

> 목표: 주기적 회고, Gate 리마인더, 전체 E2E 흐름이 동작한다.

### 10-1. Reflect Agent

| 파일 | 내용 |
|------|------|
| `src/cogniq/agents/reflect_agent.py` | `ReflectAgent(BaseAgent)` |
| `src/cogniq/registry/metrics_repository.py` | `MetricsRepository` — metrics 컬렉션 CRUD + aggregation |

```python
class ReflectAgent(BaseAgent):
    stage = Stage.REFLECT

    async def run(self, issue_id: str, run_id: str) -> RunResult:
        # issue_id는 None — Reflect는 이슈 단위가 아님
        completed_issues = await self._repo.list_completed_since(self._last_run)

        metrics = self._calculate_metrics(completed_issues)
        trend = await self._metrics_repo.get_trend(weeks=4)
        suggestions = self._generate_suggestions(metrics, trend)

        retro = RetrospectiveReport(metrics=metrics, trend=trend, suggestions=suggestions)
        await self._metrics_repo.save(retro)

        return RunResult(status="success")
```

### 10-2. Gate 리마인더

`orchestrator/watchers.py`에서 주기적으로 미승인 Gate를 체크:

```python
class GateWatcher:
    async def check_pending_gates(self):
        """main.py의 lifespan에서 주기적 태스크로 실행."""
        pending = await self._repo.find_pending_gates()
        for issue in pending:
            hours = (now() - issue.last_event_at).total_seconds() / 3600
            if hours > config.gate1_reminder_hours:
                await self._slack.notify_gate_reminder(issue.id, "gate1", hours)
```

### 10-3. 테스트 정비

| 파일 | 내용 |
|------|------|
| `tests/conftest.py` | MongoDB testcontainer, FastAPI TestClient, mock fixtures |
| `tests/unit/test_state_machine.py` | 모든 상태 전이 조합 테스트 |
| `tests/unit/test_cost_tracker.py` | 비용 계산 + 상한 초과 |
| `tests/unit/test_verification.py` | 검증 항목 파싱 + 결과 생성 |
| `tests/integration/test_api_issues.py` | 이슈 CRUD API |
| `tests/integration/test_api_auth.py` | 인증 흐름 |
| `tests/integration/test_orchestrator.py` | 상태 전이 + 스케줄링 |
| `tests/integration/test_plan_agent.py` | Plan 에이전트 (Claude mock) |
| `tests/integration/test_build_agent.py` | Build 에이전트 (Claude Code mock) |
| `tests/e2e/test_full_pipeline.py` | 이슈 생성 → Plan → Gate 1 → Build → PR 전체 |

---

## 전체 타임라인

```
Phase 1:  프로젝트 스캐폴딩 + DB + 설정          ██░░░░░░░░  1주
Phase 2:  인증 + 사용자                          ███░░░░░░░  3일
Phase 3:  프로젝트 + 이슈 CRUD                   ████░░░░░░  1주
Phase 4:  파이프라인 + 문서 + SSE                 █████░░░░░  1주
Phase 5:  오케스트레이터 + 상태 머신               ██████░░░░  1주
Phase 6:  웹훅 + 외부 연동 클라이언트              ███████░░░  3일
Phase 7:  BaseAgent + Claude 클라이언트            ████████░░  1주
Phase 8:  Plan Agent                             █████████░  1주
Phase 9:  Build Agent                            ██████████  2주
Phase 10: Reflect + 리마인더 + 테스트 정비         ██████████  1주
                                                 ─────────
                                                 총 ~10주
```

### 의존성 그래프

```
Phase 1 (스캐폴딩)
  ├── Phase 2 (인증)
  │     └── Phase 3 (프로젝트+이슈)
  │           └── Phase 4 (파이프라인+문서+SSE)
  │
  ├── Phase 5 (오케스트레이터)
  │     └── Phase 6 (웹훅+연동)
  │
  └── Phase 7 (BaseAgent)
        ├── Phase 8 (Plan Agent)
        │     └── Phase 9 (Build Agent)
        └── Phase 10 (Reflect)

※ Phase 5~6과 Phase 2~4는 병렬 진행 가능
```

---

## 각 Phase 완료 조건

| Phase | 완료 조건 |
|-------|----------|
| 1 | `uvicorn` 기동 + `/health` 응답 + MongoDB 연결 |
| 2 | 프론트엔드에서 회원가입 → 로그인 → 토큰 리프레시 동작 |
| 3 | 프론트엔드 칸반 보드에서 이슈 CRUD + 상태 변경 동작 |
| 4 | 파이프라인 뷰어 + 문서 볼트 + SSE 실시간 이벤트 동작 |
| 5 | 이슈 생성 → plan 스케줄링 + 상태 전이 규칙 위반 시 에러 |
| 6 | Linear 웹훅 → engine.handle_event 호출 + Slack 알림 전송 |
| 7 | BaseAgent mock으로 타임아웃/비용/Registry push 동작 확인 |
| 8 | 이슈 → Plan 자동 실행 → analysis + verification + plan.md 생성 |
| 9 | Gate 1 승인 → Build 자동 실행 → 검증 → PR 생성 |
| 10 | 주간 회고 실행 + Gate 리마인더 + E2E 테스트 통과 |
