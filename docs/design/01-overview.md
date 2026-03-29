# 01. 프로세스 설계 개요

## 설계 원칙

1. **"사람이 승인해야 하는 순간"을 기준으로 단계를 나눈다.**
2. **Plan이 검증 계약을 정의하고, Build가 이를 통과시킨다.**
3. **아티팩트는 TOML 형식으로 통일한다.**

---

## 사람의 승인이 필요한 상황

### 반드시 사람이 판단해야 하는 것 (자동화 불가)

| 상황 | 이유 | 예시 |
|------|------|------|
| **무엇을 만들 것인가** | 사업적 판단, 우선순위 | "이 기능을 지금 만들어야 하나?" |
| **요구사항 해석의 모호함** | 의도를 아는 건 작성자뿐 | "편리하게 → 구체적으로 뭘 의미?" |
| **트레이드오프 선택** | 정답 없는 판단 | "속도 vs 정확도" |
| **외부 이해관계자 영향** | 조직 컨텍스트 | "다른 팀 일정에 영향?" |
| **코드 머지 최종 승인** | 프로덕션 책임 | PR approve → merge |
| **배포 결정** | 장애 리스크 | "금요일 오후에 배포?" |

### 안전상 사람이 확인해야 하는 것 (조건부 자동화)

| 상황 | 리스크 | 자동화 조건 |
|------|--------|------------|
| **DB 스키마 변경** | 데이터 유실 | 테스트 + 롤백 계획 |
| **인증/권한 변경** | 보안 취약점 | 보안 테스트 자동화 |
| **결제/과금 로직** | 금전적 손실 | 샌드박스 테스트 |
| **데이터 삭제** | 비가역적 | dry-run 확인 |
| **외부 API 변경** | 하위 호환성 | API 버전 관리 |
| **새 의존성 도입** | 라이선스/보안 | 허용 목록 기반 |

### AI가 자율 처리 가능한 것

요구사항 분석, 코드 구현, 테스트, 린트, PR 생성, 문서 생성, 상태 업데이트, 메트릭 수집

---

## 전체 프로세스 흐름

```mermaid
flowchart LR
    A["🤖 Plan"] --> B{"🧑 Gate 1<br/>계획 승인"}
    B --> C["🤖 Build"]
    C --> D{"🧑 Gate 2<br/>PR 리뷰"}
    D --> E["🤖 Reflect"]

    A -.- A1["검증 항목 정의"]
    B -.- B1["사람 확인"]
    C -.- C1["구현 + 검증 통과"]
    D -.- D1["사람 확인"]
    E -.- E1["자동 분석"]
```

---

## 이슈 상태 전이

```mermaid
stateDiagram-v2
    [*] --> Backlog

    Backlog --> InAnalysis : Plan 시작
    InAnalysis --> Backlog : Plan 실패 (코멘트)
    InAnalysis --> PlanComplete : Plan 완료

    PlanComplete --> PlanApproved : 🧑 Gate 1 승인
    PlanComplete --> Backlog : 🧑 Gate 1 반려 (코멘트)

    PlanApproved --> InProgress : Build 시작
    InProgress --> InReview : Build 성공 (PR 생성)
    InProgress --> Blocked : Build 실패

    Blocked --> PlanApproved : 사람: Build 재시도
    Blocked --> Backlog : 사람: Plan부터 재시도

    InReview --> Done : 🧑 Gate 2 머지
    InReview --> InProgress : 🧑 Gate 2 반려 (수정)

    Done --> [*]
```

---

## 전체 흐름 상세

```mermaid
flowchart TD
    Issue["📋 이슈 생성"] --> Plan

    subgraph Plan["🤖 Plan"]
        P1["분석<br/>(비즈니스 + 개발 + 디자인)"]
        P2["verification.toml 작성<br/>(검증 계약서)"]
        P3["분해<br/>(하위 이슈 생성, 필요 시)"]
        P4["안전 플래그 ⚠️"]
        P1 --> P2 --> P3 --> P4
    end

    Plan --> PlanOut["📝 Plan 완료<br/>+ verification.toml"]
    PlanOut --> Gate1

    Gate1{"🧑 Gate 1<br/>분석 + 검증 항목<br/>확인/수정 + 승인"}

    Gate1 --> Build

    subgraph Build["🤖 Build"]
        B1["Phase 1: 구현<br/>(Claude Code)"]
        B2["Phase 2: 검증<br/>(verification.toml 통과)"]
        B3["Phase 3: 구현 후 리뷰<br/>(adversarial)"]
        B4["PR 생성"]
        B1 --> B2 --> B3 --> B4
    end

    Build --> BuildOut["📝 Build 완료<br/>+ verify-result.toml"]
    BuildOut --> Gate2

    Gate2{"🧑 Gate 2<br/>manual 항목 확인<br/>+ WARNING 판단<br/>+ Merge"}

    Gate2 --> Done["✅ 완료"]
    Done --> Reflect

    subgraph Reflect["🤖 Reflect (비동기)"]
        R1["성과 분석"]
        R2["검증 통과율 트렌드"]
        R3["개선 제안"]
    end
```

---

다음 문서: [02. Plan 단계](./02-plan-stage.md)
