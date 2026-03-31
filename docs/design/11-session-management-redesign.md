# 세션 관리 재설계: Claude Agent SDK 기반 영구 세션 모델

> **상태:** 설계 완료 / 구현 대기
> **작성일:** 2026-03-31
> **관련 구현:** `cogniq_worker`, `cogniq_server/api/code_sessions.py`

---

## 1. 배경 및 현재 문제점

### 1.1 현재 아키텍처 (CLI Subprocess 방식)

```
[사용자 Continue] → POST /code-sessions/{id}/continue
                  → TaskQueue에 "continue" 태스크 등록
                  → Worker가 claude CLI subprocess 실행
                     (claude --resume {cli_session_id} -p {prompt} --output-format stream-json)
                  → stdout 전체를 한꺼번에 파싱
                  → DB에 저장
```

### 1.2 확인된 문제점

| 문제 | 원인 | 영향 |
|------|------|------|
| 동시 Continue 충돌 | 두 Worker가 같은 `cli_session_id`로 `--resume` 실행 | 세션 hang, 결과 유실 |
| 실시간성 부족 | CLI 종료 후 전체 stdout 파싱 → DB 저장 → 폴링 | 최대 5s 지연 |
| 작업 중 메시지 거부 | 실행 중에는 409 반환 (현재는 큐잉으로 개선) | UX 저하 |
| 세션 불연속 | Continue마다 새 subprocess 생성 | 컨텍스트 단절 위험 |
| 스트리밍 불가 | `--print` 모드는 출력 완료 후 일괄 반환 | 실시간 피드백 없음 |

---

## 2. 새 설계: Claude Agent SDK 영구 세션 모델

### 2.1 핵심 전환점

| 항목 | 기존 (`ClaudeCodeRunner`) | 신규 (`ClaudeSDKClient`) |
|------|--------------------------|--------------------------|
| 세션 모델 | 요청마다 subprocess 생성 | Issue당 Client 1개 영구 유지 |
| 스트리밍 | ❌ 완료 후 일괄 파싱 | ✅ `StreamEvent` 실시간 yield |
| 인터럽트 | ❌ 미지원 | ✅ `client.interrupt()` |
| 작업 중 메시지 | DB 큐에 누적 → 완료 후 처리 | `asyncio.Queue` → 순차 전달 |
| 컨텍스트 연속성 | `--resume` 의존 | SDK 내부에서 자동 유지 |

### 2.2 전체 아키텍처

```
┌─────────────────────────────────────────────────────────────┐
│  Web Frontend                                               │
│  EventSource(/api/issues/{id}/stream)  ←── SSE events      │
│  POST /continue  ──────────────────────→  API Server        │
└─────────────────────────────────────────────────────────────┘
                                                 │
                                    ┌────────────▼────────────┐
                                    │   FastAPI Server         │
                                    │   - SSE 엔드포인트       │
                                    │   - Continue 엔드포인트  │
                                    │   - MongoDB 변경 감지    │
                                    └────────────┬────────────┘
                                                 │ MongoDB Change Stream
                                    ┌────────────▼────────────┐
                                    │   Worker Process         │
                                    │                          │
                                    │  IssueSessionManager     │
                                    │  ┌──────────────────┐   │
                                    │  │ ClaudeSDKClient  │   │
                                    │  │ (영구 유지)       │   │
                                    │  └────────┬─────────┘   │
                                    │           │              │
                                    │  asyncio.Queue           │
                                    │  (pending messages)      │
                                    └────────────┬────────────┘
                                                 │
                                    ┌────────────▼────────────┐
                                    │   MongoDB                │
                                    │   - code_sessions        │
                                    │   - stream_events (신규) │
                                    └─────────────────────────┘
```

---

## 3. 세션 생명주기

### 3.1 Issue당 단일 세션 원칙

```
Issue 생성
    │
    ▼
Build 태스크 실행
    │
    ├── ClaudeSDKClient 생성 (issue당 1개)
    │
    ├── 첫 번째 query: 빌드 프롬프트
    │       │
    │       └── StreamEvent → SSE 실시간 전달
    │
    ├── [사용자 Follow-up 도착]
    │       │
    │       ├── 실행 중이면 → asyncio.Queue에 push
    │       └── 완료 중이면 → 즉시 client.query()
    │
    ├── Queue에서 순차 처리
    │
    └── Issue 완료 / 에러 → Client 종료
```

### 3.2 상태 전이

```
              ┌──────────┐
              │  IDLE    │  (Client 준비 완료, 메시지 대기)
              └────┬─────┘
                   │ message_queue.get()
              ┌────▼─────┐
              │ RUNNING  │  (Claude 실행 중, StreamEvent 발생)
              └────┬─────┘
          ┌────────┼────────┐
     완료  │        │ 인터럽트 │ 에러
     ┌────▼───┐ ┌──▼─────┐ ┌▼──────┐
     │COMPLETED│ │INTERRUPTED│ │FAILED │
     └────┬───┘ └──┬──────┘ └───────┘
          │        │ drain 후
          └────────┘
               │ queue에 다음 메시지 있으면
               └─▶ RUNNING (loop)
```

---

## 4. 컴포넌트별 설계

### 4.1 `IssueSessionManager` (Worker)

```python
class IssueSessionManager:
    """Issue당 하나의 ClaudeSDKClient를 관리하는 영구 세션 매니저."""

    def __init__(self, issue_id: str, repo: IssueRepository, broadcaster: SSEBroadcaster):
        self.issue_id = issue_id
        self.repo = repo
        self.broadcaster = broadcaster
        self.client: ClaudeSDKClient | None = None
        self.message_queue: asyncio.Queue[UserMessage] = asyncio.Queue()
        self._running = False

    async def run(self, workspace_root: Path, initial_prompt: str) -> None:
        """세션 루프 — 종료될 때까지 메시지 큐를 처리한다."""
        options = ClaudeAgentOptions(
            include_partial_messages=True,  # StreamEvent 수신
            max_turns=settings.build_max_turns,
            allowed_tools=["Write", "Edit", "Read", "Bash", "Glob", "Grep"],
        )

        async with ClaudeSDKClient(options=options, cwd=workspace_root) as self.client:
            # 첫 번째 실행 (빌드 프롬프트)
            await self._execute(initial_prompt)

            # 이후 사용자 메시지 루프
            while self._running:
                try:
                    msg = await asyncio.wait_for(self.message_queue.get(), timeout=300)
                    await self._execute(msg.prompt)
                except asyncio.TimeoutError:
                    break  # 5분 유휴 → 세션 종료

    async def _execute(self, prompt: str) -> None:
        """단일 쿼리를 실행하고 StreamEvent를 SSE로 중계한다."""
        await self.repo.update_session_status(self.issue_id, "running")
        await self.client.query(prompt)

        async for message in self.client.receive_response():
            if isinstance(message, StreamEvent):
                # 실시간으로 SSE 브로드캐스트
                await self.broadcaster.push(self.issue_id, message)
                # DB에도 점진적으로 저장 (tool_use 감지 등)
                await self._handle_stream_event(message)
            elif isinstance(message, ResultMessage):
                await self._handle_result(message)

        await self.repo.update_session_status(self.issue_id, "completed")

    async def send_message(self, prompt: str) -> None:
        """외부에서 메시지를 큐에 추가한다."""
        await self.message_queue.put(UserMessage(prompt=prompt))

    async def interrupt(self) -> None:
        """현재 실행을 인터럽트하고 drain한다."""
        if self.client:
            await self.client.interrupt()
            # drain — ResultMessage(subtype="error_during_execution") 수신
            async for msg in self.client.receive_response():
                if isinstance(msg, ResultMessage):
                    await self.repo.add_event(self.issue_id, Event(
                        type=EventType.SESSION_INTERRUPTED,
                        payload={"subtype": msg.subtype},
                    ))
```

### 4.2 실시간 스트리밍 처리

`include_partial_messages=True` 설정 시 수신되는 이벤트 타입:

```python
async def _handle_stream_event(self, message: StreamEvent) -> None:
    event = message.event

    match event.get("type"):
        case "content_block_delta":
            delta = event.get("delta", {})
            if delta.get("type") == "text_delta":
                # 텍스트 청크 — 실시간 타이핑 효과
                await self.broadcaster.push_text_chunk(delta["text"])

        case "content_block_start":
            block = event.get("content_block", {})
            if block.get("type") == "tool_use":
                # 툴 호출 시작 — UI에 "Write 실행 중..." 표시
                await self.broadcaster.push_tool_start(block["name"], block.get("input", {}))

        case "message_delta":
            usage = event.get("usage", {})
            # 토큰 사용량 업데이트
            await self.repo.update_session_tokens(self.issue_id, usage)
```

### 4.3 인터럽트 후 새 메시지 처리

```python
# 인터럽트 패턴
await client.interrupt()

# 반드시 drain (ResultMessage 소비)
async for msg in client.receive_response():
    if isinstance(msg, ResultMessage):
        # subtype == "error_during_execution"
        logger.info("Session interrupted: %s", msg.subtype)
        break

# 같은 세션 컨텍스트에서 새 쿼리 전송
await client.query("지금까지 작업된 내용 요약해줘")
async for msg in client.receive_response():
    ...
```

---

## 5. SSE 스트림 이벤트 스펙

### 5.1 이벤트 타입

```typescript
type SSEEvent =
  | { type: 'connected'; issueId: string }
  | { type: 'text_chunk'; sessionId: string; text: string }
  | { type: 'tool_start'; sessionId: string; toolName: string; input: object }
  | { type: 'tool_end'; sessionId: string; toolName: string; success: boolean }
  | { type: 'session_update'; issueId: string }   // 세션 상태 변경 (DB 갱신)
  | { type: 'session_complete'; sessionId: string; costUsd: number }
  | { type: 'session_error'; sessionId: string; error: string }
  | { type: 'heartbeat' }  // 30초마다 연결 유지
```

### 5.2 SSE Broadcaster (Server)

```python
class SSEBroadcaster:
    """issue_id별 SSE 구독자에게 이벤트를 브로드캐스트한다."""

    def __init__(self):
        # {issue_id: [asyncio.Queue, ...]}
        self._subscribers: dict[str, list[asyncio.Queue]] = defaultdict(list)

    def subscribe(self, issue_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._subscribers[issue_id].append(q)
        return q

    def unsubscribe(self, issue_id: str, q: asyncio.Queue) -> None:
        self._subscribers[issue_id].remove(q)

    async def push(self, issue_id: str, event: dict) -> None:
        for q in self._subscribers.get(issue_id, []):
            try:
                q.put_nowait(json.dumps(event))
            except asyncio.QueueFull:
                pass  # 느린 클라이언트 drop
```

> **현재 구현과의 차이:** 현재 MongoDB Change Stream 기반 SSE는 세션 상태 변경만 감지한다. 신규 설계에서는 Worker가 직접 SSEBroadcaster에 push하여 텍스트 청크 수준의 실시간 스트리밍이 가능해진다.

### 5.3 SSE 엔드포인트 수정

```python
@router.get("/issues/{issue_id}/stream")
async def stream_issue_events(issue_id: str, user: User = Depends(_auth_sse)):
    q = broadcaster.subscribe(issue_id)
    try:
        async def generator():
            yield f"data: {json.dumps({'type': 'connected', 'issueId': issue_id})}\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=30)
                    yield f"data: {event}\n\n"
                except asyncio.TimeoutError:
                    yield "data: {\"type\": \"heartbeat\"}\n\n"

        return StreamingResponse(generator(), media_type="text/event-stream",
                                  headers={"X-Accel-Buffering": "no"})
    finally:
        broadcaster.unsubscribe(issue_id, q)
```

---

## 6. Worker 프로세스 관리

### 6.1 세션 레지스트리

Worker 프로세스 내에서 `IssueSessionManager` 인스턴스를 관리하는 레지스트리:

```python
class SessionRegistry:
    """실행 중인 IssueSessionManager를 관리한다."""

    def __init__(self):
        self._sessions: dict[str, IssueSessionManager] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    async def start(self, issue_id: str, workspace_root: Path, prompt: str) -> None:
        if issue_id in self._sessions:
            raise RuntimeError(f"Session already running for issue {issue_id}")
        manager = IssueSessionManager(issue_id, ...)
        self._sessions[issue_id] = manager
        self._tasks[issue_id] = asyncio.create_task(
            manager.run(workspace_root, prompt)
        )

    async def send_message(self, issue_id: str, prompt: str) -> None:
        if manager := self._sessions.get(issue_id):
            await manager.send_message(prompt)
        else:
            raise ValueError(f"No active session for issue {issue_id}")

    async def interrupt(self, issue_id: str) -> None:
        if manager := self._sessions.get(issue_id):
            await manager.interrupt()

    def is_active(self, issue_id: str) -> bool:
        return issue_id in self._sessions and not self._tasks[issue_id].done()
```

### 6.2 Worker-Server 통신 (메시지 라우팅)

현재 TaskQueue 기반 → Worker 내부 `asyncio.Queue` 로 전환 시 Server-Worker 간 채널이 필요:

| 옵션 | 장점 | 단점 |
|------|------|------|
| **MongoDB Capped Collection** (현재 방식 확장) | 인프라 추가 없음 | 지연 ~100ms |
| **Redis Pub/Sub** | 낮은 지연, pub/sub | 인프라 추가 |
| **HTTP POST to Worker** | 단순 | Worker 포트 노출 필요 |
| **gRPC** | 양방향 스트리밍 | 복잡도 높음 |

**권장:** 단기적으로는 기존 **MongoDB TaskQueue 유지** + `IssueSessionManager`를 Worker 내 장기 실행 태스크로 변환. Worker가 TaskQueue를 폴링하면서 `continue` 메시지를 활성 세션의 `asyncio.Queue`에 라우팅.

```python
# Worker main loop 변경
while running:
    task = await task_queue.claim(worker_id)
    if not task:
        await asyncio.sleep(poll_interval)
        continue

    if task.stage in ("build", "plan"):
        # 기존 방식 유지
        result = await run_agent(task, repo, workspace_mgr)
        await task_queue.complete(task.id, result)

    elif task.stage == "continue":
        session_id = task.config["session_id"]
        prompt = task.config["prompt"]

        if registry.is_active(task.issue_id):
            # 활성 세션에 메시지 전달 (세션 내부 큐 사용)
            await registry.send_message(task.issue_id, prompt)
        else:
            # 세션 재시작 (Worker 재시작 후 복구)
            await registry.start(task.issue_id, workspace, prompt)

        await task_queue.complete(task.id, TaskResult(status="success"))
```

---

## 7. 마이그레이션 전략

### Phase 1 (완료): 기반 작업
- [x] MongoDB 큐 (`message_queue`) 도입 — 작업 중 메시지 누적
- [x] SSE 엔드포인트 (MongoDB Change Stream 기반) — 세션 상태 변경 알림
- [x] Continue API: 실행 중이면 큐잉, 동시 요청 방지

### Phase 2 (다음): SDK 전환
- [ ] `claude_agent_sdk` 패키지 설치 및 API 검증
- [ ] `ClaudeCodeRunner` → `ClaudeSDKClient` 래퍼 교체
- [ ] `IssueSessionManager` 구현
- [ ] Worker: `SessionRegistry` 도입
- [ ] SSE Broadcaster를 인메모리 Pub/Sub으로 교체 (Change Stream 의존 제거)

### Phase 3 (개선): 실시간 스트리밍
- [ ] `include_partial_messages=True` 활성화
- [ ] 텍스트 청크 SSE 이벤트 (`text_chunk`) 추가
- [ ] 툴 호출 실시간 알림 (`tool_start`, `tool_end`) 추가
- [ ] Frontend: SSE 이벤트로 메시지를 점진적으로 렌더링

### Phase 4 (선택): 인터럽트
- [ ] 인터럽트 API 엔드포인트 추가
- [ ] drain 패턴 구현
- [ ] Frontend: 인터럽트 버튼 추가

---

## 8. 의존성 및 패키지

```toml
# agent/pyproject.toml 추가
[project]
dependencies = [
    "claude-agent-sdk>=1.0.0",   # ClaudeSDKClient, StreamEvent 등
    ...
]
```

> **주의:** `claude_agent_sdk` 패키지 네임스페이스 및 정확한 API는 Anthropic 공식 문서 확인 필요. `query()` / `ClaudeAgentOptions` / `StreamEvent` / `ResultMessage` 타입은 SDK 버전에 따라 다를 수 있음.

---

## 9. 결론

Claude Desktop과 동일한 방식의 세션 관리는 `ClaudeSDKClient` 기반으로 완전히 구현 가능하다. 핵심은:

1. **Issue당 하나의 Client 인스턴스** — subprocess 재생성 없이 컨텍스트 유지
2. **`asyncio.Queue` 기반 메시지 큐** — 작업 중 메시지를 순차 처리
3. **`StreamEvent` 직접 SSE push** — 텍스트 청크 수준의 실시간 피드백
4. **`interrupt()` + drain** — 작업 중단 후 즉시 새 메시지 처리

Phase 1(현재)은 기존 CLI 기반을 유지하면서 SSE와 메시지 큐를 도입했다. Phase 2에서 SDK로 전환하면 실시간성과 안정성이 크게 향상된다.
