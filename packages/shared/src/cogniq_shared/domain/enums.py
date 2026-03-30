from enum import StrEnum


class IssueStatus(StrEnum):
    BACKLOG = "backlog"
    IN_ANALYSIS = "in_analysis"
    PLAN_COMPLETE = "plan_complete"
    PLAN_APPROVED = "plan_approved"
    IN_PROGRESS = "in_progress"
    IN_REVIEW = "in_review"
    BLOCKED = "blocked"
    DONE = "done"
    CANCELLED = "cancelled"


class IssuePriority(StrEnum):
    URGENT = "urgent"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class Stage(StrEnum):
    PLAN = "plan"
    BUILD = "build"
    REFLECT = "reflect"


class Phase(StrEnum):
    PHASE1 = "phase1"
    PHASE2 = "phase2"
    PHASE3 = "phase3"
    SHIP = "ship"


class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"
    SKIPPED = "skipped"


class EventType(StrEnum):
    PLAN_STARTED = "plan_started"
    PLAN_COMPLETED = "plan_completed"
    PLAN_FAILED = "plan_failed"
    GATE1_APPROVED = "gate1_approved"
    GATE1_REJECTED = "gate1_rejected"
    BUILD_STARTED = "build_started"
    BUILD_PHASE_COMPLETED = "build_phase_completed"
    VERIFICATION_PASSED = "verification_passed"
    VERIFICATION_FAILED = "verification_failed"
    BUILD_COMPLETED = "build_completed"
    BUILD_FAILED = "build_failed"
    GATE2_MERGED = "gate2_merged"
    CONFLICT_DETECTED = "conflict_detected"
    ESCALATION = "escalation"
    COST_WARNING = "cost_warning"


class ArtifactType(StrEnum):
    ISSUE_SNAPSHOT = "issue_snapshot"
    ANALYSIS = "analysis"
    VERIFICATION = "verification"
    PLAN_MD = "plan_md"
    DESIGN_MD = "design_md"
    BUILD_RESULT = "build_result"
    VERIFY_RESULT = "verify_result"
    REVIEW_MD = "review_md"
    POSTMORTEM = "postmortem"
    RETRO = "retro"
    TREND = "trend"


class DocumentType(StrEnum):
    REQUIREMENTS = "requirements"
    DESIGN = "design"
    PLAN = "plan"
    REVIEW = "review"
    POSTMORTEM = "postmortem"
    CUSTOM = "custom"


class MemberRole(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"
    VIEWER = "viewer"


# Backend status is passed directly to frontend (no mapping layer).
# Frontend uses the same IssueStatus values as-is.
#
# Pipeline columns the frontend renders:
#   backlog → in_analysis → plan_complete → plan_approved →
#   in_progress → in_review → done
#   (blocked: shown as sub-state of in_progress)
#   (cancelled: filtered or separate view)
