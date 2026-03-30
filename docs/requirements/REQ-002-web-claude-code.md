# REQ-002: 웹 기반 Claude Code 뷰어

## 개요

프로젝트의 메인 브랜치에 연결된 Claude Code 세션을 웹 대시보드에서 실시간으로 조회하고 상호작용할 수 있는 기능을 추가한다.

---

## 배경

현재 cogniq의 Build Agent는 Claude Code CLI를 통해 코드를 작성하지만, 그 과정이 서버 로그에서만 확인 가능하다. 사용자가 웹 대시보드에서 Claude Code가 어떤 파일을 보고, 어떤 코드를 작성하는지 실시간으로 볼 수 있어야 한다.

이 기능은 Claude Desktop 앱의 "Code" 섹션과 유사한 UX를 웹에서 제공하는 것을 목표로 한다.

---

## 용어 정의

| 용어 | 설명 |
|------|------|
| **Claude Code 세션** | Claude Code CLI가 프로젝트 디렉토리에서 실행되는 하나의 인스턴스 |
| **메시지** | Claude Code 세션 내의 하나의 대화 턴 (user prompt, assistant response, tool use, tool result) |
| **프로젝트 루트** | 프로젝트에 연결된 GitHub 레포의 로컬 클론 경로 |
| **라이브 세션** | 현재 실행 중인 Build Agent의 Claude Code 세션 |
| **히스토리 세션** | 이미 완료된 이전 Build의 Claude Code 세션 기록 |

---

## 기능 요구사항

### FR-001: Claude Code 세션 목록 조회

사용자는 특정 이슈의 Claude Code 실행 이력을 목록으로 확인할 수 있다.

- 이슈 상세 페이지에 "Code" 탭 추가
- 각 세션: 시작 시간, 종료 시간, 상태 (running/completed/failed), 턴 수, 비용 표시
- 최신 세션이 상단에 위치

### FR-002: Claude Code 세션 상세 조회 (히스토리)

사용자는 완료된 Claude Code 세션의 전체 대화 내용을 확인할 수 있다.

- 메시지 타입별 시각적 구분:
  - **user** (프롬프트): 회색 배경
  - **assistant** (응답): 흰색 배경, 마크다운 렌더링
  - **tool_use** (도구 호출): 코드 블록 스타일, 접기/펼치기
  - **tool_result** (도구 결과): 코드 블록, 접기/펼치기
- 파일 diff가 포함된 tool_result는 diff 뷰어로 렌더링
- 각 메시지의 토큰 사용량 표시

### FR-003: 라이브 세션 스트리밍

사용자는 현재 실행 중인 Claude Code 세션을 실시간으로 볼 수 있다.

- 이슈 상태가 `in_progress`일 때 자동으로 라이브 모드 활성화
- 새 메시지가 생성될 때마다 자동 스크롤
- assistant 응답이 스트리밍될 때 타이핑 애니메이션 효과
- 현재 실행 중인 tool_use 표시 (스피너 + 도구 이름)
- 연결 상태 인디케이터 (Connected / Reconnecting / Disconnected)

### FR-004: 파일 트리 뷰어

사용자는 Claude Code가 작업 중인 프로젝트의 파일 구조를 확인할 수 있다.

- 프로젝트 루트의 파일 트리 표시
- Claude Code가 수정한 파일 하이라이트 (변경 표시)
- 파일 클릭 시 내용 미리보기 (읽기 전용)
- 현재 worktree 기준 (main 브랜치 + agent 변경사항)

### FR-005: 코드 Diff 뷰어

사용자는 Claude Code가 생성/수정한 코드의 변경사항을 확인할 수 있다.

- 이슈별 전체 diff 요약 (변경된 파일 수, 추가/삭제 줄 수)
- 파일별 unified diff 뷰 (GitHub PR 스타일)
- 변경 전/후 side-by-side 뷰 토글
- 구문 하이라이팅 (언어 자동 감지)

---

## 비기능 요구사항

### NFR-001: 실시간 지연

라이브 세션에서 서버 → 클라이언트 메시지 전달 지연은 **2초 이내**여야 한다.

### NFR-002: 세션 데이터 보존

완료된 세션의 전체 대화 기록은 **MongoDB에 영구 저장**되어야 한다.

### NFR-003: 대용량 세션 처리

50턴 이상의 대형 세션도 웹에서 **스크롤 가능한 가상화 리스트**로 렌더링되어야 한다.

### NFR-004: 모바일 대응

Code 뷰어는 모바일 화면에서도 읽기 가능해야 한다 (읽기 전용, 스크롤).

---

## 기술 설계 방향

### 백엔드

| 항목 | 방식 |
|------|------|
| 라이브 스트리밍 | SSE (Server-Sent Events) — 기존 events API 패턴 확장 |
| 세션 저장 | MongoDB `code_sessions` 컬렉션 |
| 데이터 소스 | Claude Code CLI `--output-format stream-json` 출력 파싱 |
| 파일 트리 | `git ls-tree` + `git diff --stat` |
| Diff | `git diff` 출력 파싱 |

### 프론트엔드

| 항목 | 방식 |
|------|------|
| 메시지 렌더링 | React 컴포넌트 + react-markdown |
| Diff 뷰어 | react-diff-viewer 또는 monaco-diff |
| 파일 트리 | 커스텀 트리 컴포넌트 |
| 코드 하이라이팅 | shiki 또는 prism |
| 가상 스크롤 | @tanstack/virtual |
| SSE 연결 | EventSource API + React Query |

### API 엔드포인트 (신규)

```
GET  /api/issues/{issue_id}/code-sessions                    → 세션 목록
GET  /api/issues/{issue_id}/code-sessions/{session_id}       → 세션 상세 (메시지 포함)
GET  /api/issues/{issue_id}/code-sessions/live               → SSE 라이브 스트림
GET  /api/issues/{issue_id}/code-sessions/{session_id}/diff  → 변경 diff
GET  /api/issues/{issue_id}/code-sessions/{session_id}/files → 파일 트리
GET  /api/issues/{issue_id}/code-sessions/{session_id}/files/{path} → 파일 내용
```

---

## 데이터 모델

### CodeSession (MongoDB: code_sessions)

```json
{
  "_id": "session_id",
  "issue_id": "issue_id",
  "run_id": "run_id",
  "status": "running | completed | failed",
  "model": "sonnet | opus",
  "started_at": "2026-03-31T00:00:00Z",
  "completed_at": "2026-03-31T00:05:00Z",
  "total_turns": 15,
  "total_tokens": { "input": 50000, "output": 12000 },
  "total_cost_usd": 0.08,
  "messages": [
    {
      "turn": 1,
      "role": "user",
      "content": "...",
      "timestamp": "...",
      "tokens": { "input": 1200, "output": 0 }
    },
    {
      "turn": 1,
      "role": "assistant",
      "content": [
        { "type": "text", "text": "..." },
        { "type": "tool_use", "name": "Write", "input": { "file_path": "...", "content": "..." } }
      ],
      "timestamp": "...",
      "tokens": { "input": 0, "output": 800 }
    },
    {
      "turn": 1,
      "role": "tool_result",
      "tool_use_id": "...",
      "content": "File written successfully",
      "timestamp": "..."
    }
  ],
  "changed_files": [
    { "path": "src/main.py", "additions": 15, "deletions": 3, "status": "modified" },
    { "path": "test.txt", "additions": 1, "deletions": 0, "status": "added" }
  ]
}
```

---

## 화면 와이어프레임 (텍스트)

```
┌─────────────────────────────────────────────────────────────────────┐
│ Issue: 이슈를 잘 처리하는지 테스트                    [IN_PROGRESS] │
├─────────────────────────────────────────────────────────────────────┤
│ [Overview] [Plan] [Verification] [Code ●] [Result] [History]       │
├─────────────────┬───────────────────────────────────────────────────┤
│ Files  (3)      │  🟢 Live Session — 2m 30s elapsed                │
│ ─────────────── │  ─────────────────────────────────────────────── │
│ 📁 src/         │  👤 User (Turn 1)                                │
│   📄 main.py *  │  "test.txt 파일을 만들고 Hello Repo!! 작성해줘"  │
│ 📄 test.txt +   │                                                   │
│ 📄 README.md    │  🤖 Assistant (Turn 1)                           │
│                  │  파일을 생성하겠습니다.                           │
│                  │  ┌─ Write: test.txt ─────────────────────────┐   │
│                  │  │ Hello Repo!!                               │   │
│                  │  └──────────────────────────────────────────┘   │
│                  │                                                   │
│                  │  ✅ File written successfully                     │
│                  │                                                   │
│                  │  🤖 Assistant (Turn 2)                           │
│                  │  커밋하고 푸시하겠습니다.                         │
│                  │  ┌─ Bash: git add ... ───────────────────────┐   │
│                  │  │ ▶ Running...                               │   │
│                  │  └──────────────────────────────────────────┘   │
├─────────────────┴───────────────────────────────────────────────────┤
│ Diff: +2 files, +15 additions, -3 deletions          [View Diff]  │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Given-When-Then 테스트 케이스

### TC-001: 세션 목록 조회

```gherkin
Feature: Claude Code 세션 목록 조회

  Scenario: 이슈에 Code 세션이 있을 때 목록을 표시한다
    Given 이슈 "642d461f0829"에 완료된 Code 세션 2개가 존재한다
      And 사용자가 로그인되어 있다
    When 이슈 상세 페이지에서 "Code" 탭을 클릭한다
    Then 세션 목록이 최신순으로 표시된다
      And 각 세션에 시작 시간, 상태, 턴 수, 비용이 표시된다

  Scenario: Code 세션이 없을 때 빈 상태를 표시한다
    Given 이슈 "abc123"에 Code 세션이 없다
    When 이슈 상세 페이지에서 "Code" 탭을 클릭한다
    Then "아직 Code 세션이 없습니다" 메시지가 표시된다
```

### TC-002: 히스토리 세션 상세 조회

```gherkin
Feature: 완료된 Claude Code 세션 상세 조회

  Scenario: 완료된 세션의 전체 대화를 볼 수 있다
    Given 완료된 Code 세션 "session_001"이 15턴으로 구성되어 있다
    When 세션 목록에서 "session_001"을 클릭한다
    Then 15턴의 전체 메시지가 시간순으로 표시된다
      And user 메시지는 회색 배경으로 표시된다
      And assistant 메시지는 마크다운으로 렌더링된다

  Scenario: tool_use 메시지를 접기/펼치기 할 수 있다
    Given Code 세션에 Write 도구 호출이 포함되어 있다
    When 도구 호출 블록의 헤더를 클릭한다
    Then 도구의 입력 파라미터가 접기/펼치기 된다
      And 펼쳤을 때 코드 내용에 구문 하이라이팅이 적용된다

  Scenario: 대용량 세션도 부드럽게 스크롤된다
    Given Code 세션이 50턴 이상이다
    When 세션 상세를 열고 스크롤한다
    Then 가상 스크롤이 적용되어 렌더링 지연 없이 스크롤된다
```

### TC-003: 라이브 세션 스트리밍

```gherkin
Feature: 실행 중인 Claude Code 세션 실시간 조회

  Scenario: Build 실행 중일 때 라이브 세션이 자동 표시된다
    Given 이슈 "642d461f0829"의 상태가 "in_progress"이다
      And Build Agent가 Claude Code를 실행 중이다
    When 이슈 상세 페이지에서 "Code" 탭을 클릭한다
    Then "🟢 Live Session" 인디케이터가 표시된다
      And 경과 시간이 실시간으로 업데이트된다
      And SSE 연결이 자동으로 수립된다

  Scenario: 새 메시지가 실시간으로 표시된다
    Given 라이브 세션에 연결되어 있다
    When Claude Code가 새 assistant 메시지를 생성한다
    Then 2초 이내에 화면에 메시지가 나타난다
      And 자동으로 최하단으로 스크롤된다

  Scenario: 도구 실행 중 상태가 표시된다
    Given 라이브 세션에 연결되어 있다
    When Claude Code가 Bash 도구를 호출한다
    Then "▶ Running: Bash" 스피너가 표시된다
    When 도구 실행이 완료된다
    Then 스피너가 사라지고 결과가 표시된다

  Scenario: 세션이 완료되면 라이브 모드가 종료된다
    Given 라이브 세션에 연결되어 있다
    When Build Agent가 완료/실패한다
    Then "세션 완료" 또는 "세션 실패" 배너가 표시된다
      And SSE 연결이 자동으로 종료된다
      And 히스토리 모드로 전환된다

  Scenario: SSE 연결이 끊겼을 때 자동 재연결한다
    Given 라이브 세션에 연결되어 있다
    When 네트워크 연결이 일시적으로 끊긴다
    Then "Reconnecting..." 인디케이터가 표시된다
    When 네트워크가 복구된다
    Then 자동으로 재연결되고 놓친 메시지가 보충된다
```

### TC-004: 파일 트리 뷰어

```gherkin
Feature: 프로젝트 파일 트리 표시

  Scenario: 프로젝트 파일 구조를 확인할 수 있다
    Given Code 세션이 존재한다
      And 프로젝트에 10개의 파일이 있다
    When "Code" 탭의 왼쪽 파일 트리를 본다
    Then 프로젝트 루트부터 파일/폴더가 트리 형태로 표시된다
      And 폴더는 접기/펼치기가 가능하다

  Scenario: 변경된 파일이 하이라이트된다
    Given Claude Code가 "src/main.py"를 수정하고 "test.txt"를 생성했다
    When 파일 트리를 본다
    Then "src/main.py" 옆에 수정 표시(*)가 노란색으로 표시된다
      And "test.txt" 옆에 추가 표시(+)가 초록색으로 표시된다

  Scenario: 파일 내용을 미리보기할 수 있다
    Given 파일 트리에 "src/main.py"가 표시되어 있다
    When "src/main.py"를 클릭한다
    Then 오른쪽에 파일 내용이 읽기 전용으로 표시된다
      And 구문 하이라이팅이 적용된다
```

### TC-005: 코드 Diff 뷰어

```gherkin
Feature: 코드 변경사항 Diff 표시

  Scenario: 전체 변경 요약을 확인할 수 있다
    Given Code 세션에서 3개 파일이 변경되었다 (+25줄, -8줄)
    When "View Diff" 버튼을 클릭한다
    Then "3 files changed, 25 insertions, 8 deletions" 요약이 표시된다
      And 변경된 파일 목록이 표시된다

  Scenario: 파일별 unified diff를 확인할 수 있다
    Given "src/main.py"에 변경사항이 있다
    When diff 뷰어에서 "src/main.py"를 선택한다
    Then GitHub PR 스타일의 unified diff가 표시된다
      And 추가된 줄은 초록색, 삭제된 줄은 빨간색으로 표시된다
      And 줄 번호가 표시된다

  Scenario: side-by-side 뷰로 전환할 수 있다
    Given unified diff가 표시되어 있다
    When "Side by Side" 토글을 클릭한다
    Then 변경 전(왼쪽)과 변경 후(오른쪽)가 나란히 표시된다
```

### TC-006: API 엔드포인트

```gherkin
Feature: Code Session API

  Scenario: 세션 목록 조회 API
    Given 이슈 "642d461f0829"에 Code 세션 2개가 존재한다
    When GET /api/issues/642d461f0829/code-sessions 요청을 보낸다
    Then 200 OK와 함께 세션 배열이 반환된다
      And 각 세션에 id, status, started_at, total_turns, total_cost_usd가 포함된다

  Scenario: 세션 상세 조회 API
    Given Code 세션 "session_001"이 존재한다
    When GET /api/issues/642d461f0829/code-sessions/session_001 요청을 보낸다
    Then 200 OK와 함께 세션 상세가 반환된다
      And messages 배열에 전체 대화 내용이 포함된다

  Scenario: 라이브 스트림 API
    Given 이슈의 Build Agent가 실행 중이다
    When GET /api/issues/642d461f0829/code-sessions/live 요청을 SSE로 보낸다
    Then Content-Type: text/event-stream으로 응답한다
      And 새 메시지마다 SSE 이벤트가 전송된다

  Scenario: 실행 중이 아닐 때 라이브 스트림 요청
    Given 이슈의 상태가 "backlog"이다
    When GET /api/issues/642d461f0829/code-sessions/live 요청을 보낸다
    Then 404 Not Found와 함께 "No active session" 메시지가 반환된다

  Scenario: Diff 조회 API
    Given Code 세션 "session_001"에서 파일 변경이 있었다
    When GET /api/issues/642d461f0829/code-sessions/session_001/diff 요청을 보낸다
    Then 200 OK와 함께 파일별 diff가 반환된다
      And 각 파일에 path, status, additions, deletions, patch가 포함된다

  Scenario: 인증 없이 접근 시
    Given 인증 토큰이 없다
    When GET /api/issues/{id}/code-sessions 요청을 보낸다
    Then 401 Unauthorized가 반환된다
```

### TC-007: 에러 처리

```gherkin
Feature: Code 뷰어 에러 처리

  Scenario: SSE 연결 실패 시 폴링으로 폴백한다
    Given 브라우저가 SSE를 지원하지 않는다
    When "Code" 탭을 열고 라이브 세션을 본다
    Then 3초 간격 폴링으로 자동 전환된다
      And 사용자에게 "실시간 연결 불가, 자동 새로고침 모드" 알림이 표시된다

  Scenario: 세션 데이터 로드 실패 시
    Given 서버가 500 에러를 반환한다
    When "Code" 탭을 연다
    Then "세션을 불러올 수 없습니다. 다시 시도해주세요." 메시지가 표시된다
      And "다시 시도" 버튼이 표시된다
```

---

## 프론트엔드 구현 위치

### 기존 이슈 상세 페이지 탭 구조

`web/src/components/dashboard/issue/issue-detail-page.tsx`의 기존 탭:

```
[Overview] [Analysis*] [Plan*] [Verification*] [Result*] [History]
                                                 ↑ 여기에 Code 탭 추가
```

*는 조건부 탭 (artifact 존재 시에만 표시)

### Code 탭 추가 위치

```tsx
// issue-detail-page.tsx 기존 탭 목록 (263~272줄)
<TabsList variant="line" className="h-9">
  <TabsTrigger value="overview">{t('tabs.overview')}</TabsTrigger>
  {hasAnalysis && <TabsTrigger value="analysis">...</TabsTrigger>}
  {hasPlan && <TabsTrigger value="plan">...</TabsTrigger>}
  {hasVerification && <TabsTrigger value="verification">...</TabsTrigger>}
  {hasCode && <TabsTrigger value="code">Code</TabsTrigger>}        // ← 추가
  {hasResult && <TabsTrigger value="result">...</TabsTrigger>}
  <TabsTrigger value="history">{t('tabs.history')}</TabsTrigger>
</TabsList>
```

**탭 표시 조건:**
```tsx
// Code 세션이 존재하거나, 이슈가 in_progress 상태일 때 표시
const hasCode = codeSessions.length > 0
  || issue.status === 'IN_PROGRESS'
  || issue.status === 'IN_ANALYSIS'
```

**탭 내용:**
```tsx
<TabsContent value="code" className="h-full m-0">
  <CodeViewer issueId={issue.id} issueStatus={issue.status} />
</TabsContent>
```

### Code 탭 내부 레이아웃

```
┌──────────────────────────────────────────────────────────────────┐
│ [Code] 탭 클릭 시                                                │
├──────────────────────────────────────────────────────────────────┤
│                                                                  │
│  ┌─ 세션 선택기 (드롭다운) ─────────────────────────────────────┐ │
│  │ 🟢 Session #3 (진행 중) — 2026-03-31 00:44   ▼             │ │
│  └──────────────────────────────────────────────────────────────┘ │
│                                                                  │
│  ┌─ 메시지 영역 (스크롤) ──────────────────────────────────────┐ │
│  │                                                              │ │
│  │  👤 User (Turn 1)                                            │ │
│  │  ┌──────────────────────────────────────────────────────┐   │ │
│  │  │ test.txt 파일을 만들고 Hello Repo!! 작성해줘          │   │ │
│  │  └──────────────────────────────────────────────────────┘   │ │
│  │                                                              │ │
│  │  🤖 Assistant (Turn 1)                                      │ │
│  │  파일을 생성하겠습니다.                                      │ │
│  │                                                              │ │
│  │  ┌─ 🔧 Write ──────────────────────────────────────────┐   │ │
│  │  │ file: test.txt                                        │   │ │
│  │  │ ┌──────────────────────────────────────────────────┐ │   │ │
│  │  │ │ Hello Repo!!                                      │ │   │ │
│  │  │ └──────────────────────────────────────────────────┘ │   │ │
│  │  └──────────────────────────────────────────────────────┘   │ │
│  │                                                              │ │
│  │  ✅ File written successfully                                │ │
│  │                                                              │ │
│  │  🤖 Assistant (Turn 2)                                      │ │
│  │  커밋하고 푸시하겠습니다.                                    │ │
│  │  ┌─ 🔧 Bash ───────────────────────────── ▶ Running ───┐   │ │
│  │  │ git add test.txt && git commit -m "Add test.txt"      │   │ │
│  │  └──────────────────────────────────────────────────────┘   │ │
│  │                                                              │ │
│  └──────────────────────────────────────────────────────────────┘ │
│                                                                  │
│  ┌─ 하단 요약바 ───────────────────────────────────────────────┐ │
│  │ 📊 Turns: 5  │  🎯 Tokens: 12,340  │  💰 $0.08  │  Files: 2 │ │
│  └──────────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────┘
```

---

## 구현 우선순위

| 단계 | 기능 | 복잡도 |
|------|------|--------|
| **Phase 1** | 세션 저장 + 목록/상세 API + Code 탭 (FR-001, FR-002) | 중간 |
| **Phase 2** | 라이브 SSE 스트리밍 (FR-003) | 높음 |
| **Phase 3** | 파일 트리 + Diff 뷰어 (FR-004, FR-005) | 중간 |

### Phase 1 변경 파일

**백엔드 (agent/):**

| 파일 | 변경 | 설명 |
|------|------|------|
| `packages/shared/src/cogniq_shared/domain/code_session.py` | 새 파일 | CodeSession, CodeMessage Pydantic 모델 |
| `packages/shared/src/cogniq_shared/registry/code_session_repository.py` | 새 파일 | MongoDB `code_sessions` 컬렉션 CRUD |
| `packages/server/src/cogniq_server/api/code_sessions.py` | 새 파일 | API 라우터 (목록, 상세, 라이브, diff) |
| `packages/server/src/cogniq_server/main.py` | 수정 | code_sessions 라우터 등록 |
| `packages/worker/src/cogniq_worker/agents/claude_code.py` | 수정 | stream-json 출력을 CodeSession으로 저장 |
| `packages/worker/src/cogniq_worker/agents/build_agent.py` | 수정 | CodeSession 생성/완료 호출 추가 |

**프론트엔드 (web/):**

| 파일 | 변경 | 설명 |
|------|------|------|
| `src/components/dashboard/issue/issue-detail-page.tsx` | **수정** | Code 탭 추가 (TabsTrigger + TabsContent) |
| `src/components/dashboard/issue/code-viewer.tsx` | 새 파일 | Code 탭 메인 컴포넌트 (세션 선택 + 메시지 목록) |
| `src/components/dashboard/issue/code-message.tsx` | 새 파일 | 개별 메시지 렌더링 (user/assistant/tool_use/tool_result) |
| `src/components/dashboard/issue/code-tool-block.tsx` | 새 파일 | tool_use/tool_result 접기/펼치기 블록 |
| `src/components/dashboard/issue/code-session-selector.tsx` | 새 파일 | 세션 드롭다운 선택기 |
| `src/components/dashboard/issue/code-summary-bar.tsx` | 새 파일 | 하단 요약 (턴, 토큰, 비용, 파일 수) |
| `src/services/code-sessions.ts` | 새 파일 | API 서비스 레이어 |
| `src/hooks/useCodeSessions.ts` | 새 파일 | React Query 훅 (목록, 상세, 라이브) |
| `src/i18n/locales/ko/dashboard.json` | 수정 | Code 탭 관련 번역 키 추가 |
| `src/i18n/locales/en/dashboard.json` | 수정 | Code 탭 관련 번역 키 추가 |

### Phase 2 추가 변경 파일

| 파일 | 변경 | 설명 |
|------|------|------|
| `packages/server/src/cogniq_server/api/code_sessions.py` | 수정 | SSE 라이브 스트림 엔드포인트 추가 |
| `packages/worker/src/cogniq_worker/agents/claude_code.py` | 수정 | MongoDB Change Stream으로 실시간 메시지 push |
| `src/components/dashboard/issue/code-viewer.tsx` | 수정 | SSE 연결 + 자동 스크롤 + 연결 상태 인디케이터 |
| `src/components/dashboard/issue/code-live-indicator.tsx` | 새 파일 | 라이브 연결 상태 표시 컴포넌트 |

### Phase 3 추가 변경 파일

| 파일 | 변경 | 설명 |
|------|------|------|
| `packages/server/src/cogniq_server/api/code_sessions.py` | 수정 | diff, files, file content 엔드포인트 추가 |
| `src/components/dashboard/issue/code-file-tree.tsx` | 새 파일 | 파일 트리 컴포넌트 |
| `src/components/dashboard/issue/code-diff-viewer.tsx` | 새 파일 | Diff 뷰어 (unified + side-by-side) |
| `src/components/dashboard/issue/code-file-preview.tsx` | 새 파일 | 파일 내용 미리보기 (구문 하이라이팅) |
