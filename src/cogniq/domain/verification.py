from pydantic import BaseModel


class AcceptanceCriteria(BaseModel):
    id: str
    description: str
    verify_method: str  # test | existence | manual
    expected: str = ""


class SafetyCriteria(BaseModel):
    id: str
    description: str
    verify_method: str = "manual"
    flag: str  # db_schema | auth | payment | deletion
    note: str = ""


class VerificationMeta(BaseModel):
    issue_id: str
    created_by: str = "plan"
    created_at: str = ""


class Verification(BaseModel):
    meta: VerificationMeta
    acceptance: list[AcceptanceCriteria] = []
    safety: list[SafetyCriteria] = []


class VerificationItemResult(BaseModel):
    id: str
    status: str  # pass | fail | skip
    detail: str = ""
    attempts: int = 1


class DefaultsResult(BaseModel):
    lint: dict | None = None
    typecheck: dict | None = None
    test: dict | None = None
    secret_scan: dict | None = None


class AdversarialFinding(BaseModel):
    severity: str  # critical | warning | info
    description: str
    suggestion: str = ""
    auto_fixed: bool = False


class PostReview(BaseModel):
    model_used: str = ""
    findings_critical: int = 0
    findings_warning: int = 0
    findings_info: int = 0
    findings: list[AdversarialFinding] = []


class VerifyResult(BaseModel):
    meta: VerificationMeta
    overall_status: str = "pending"  # pass | fail | partial
    defaults: DefaultsResult = DefaultsResult()
    results: list[VerificationItemResult] = []
    post_review: PostReview | None = None
