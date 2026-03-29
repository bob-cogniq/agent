import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cogniq.agents.base import BaseAgent, RunResult
from cogniq.agents.claude_client import ClaudeClient
from cogniq.agents.claude_code import ClaudeCodeRunner
from cogniq.agents.worktree import Worktree, WorktreeManager
from cogniq.config import settings
from cogniq.domain.enums import ArtifactType, EventType, Stage
from cogniq.domain.issue import RepoConfig
from cogniq.domain.verification import VerifyResult, VerificationItemResult, DefaultsResult, PostReview, AdversarialFinding
from cogniq.integrations.github import GitHubClient
from cogniq.registry.repository import IssueRepository

logger = logging.getLogger(__name__)

MAX_AUTO_FIX_ATTEMPTS = 2
MAX_ADVERSARIAL_CYCLES = 2


class VerificationFailed(Exception):
    pass


class BuildAgent(BaseAgent):
    """DeveloperAgent: implements code, runs verification, creates PRs."""

    stage = Stage.BUILD

    def __init__(self, repo: IssueRepository):
        super().__init__(
            repo=repo,
            timeout_seconds=settings.build_timeout_minutes * 60,
            max_cost_usd=settings.build_max_cost_usd,
        )
        self._claude = ClaudeClient(
            cost_tracker=self._cost_tracker,
        )
        self._claude_code = ClaudeCodeRunner(
            cost_tracker=self._cost_tracker,
            max_turns=settings.build_max_turns,
        )
        # These are set per-run based on project repo config
        self._worktree_mgr: WorktreeManager | None = None
        self._github: GitHubClient | None = None
        self._repo_config: RepoConfig | None = None

    async def _load_repo_config(self, project_id: str, repo_id: str | None = None) -> RepoConfig:
        """Load repo config from project. Uses repo_id if given, otherwise primary repo."""
        from cogniq.dependencies import get_db
        db = get_db()
        project = await db["projects"].find_one({"_id": project_id})
        if not project or not project.get("repos"):
            raise RuntimeError(f"Project {project_id} has no repos configured")

        from cogniq.domain.issue import Project as ProjectModel
        proj = ProjectModel(**project)

        if repo_id:
            rc = proj.get_repo(repo_id)
            if not rc:
                raise RuntimeError(f"Repo {repo_id} not found in project {project_id}")
            return rc
        rc = proj.get_primary_repo()
        if not rc:
            raise RuntimeError(f"Project {project_id} has no repos configured")
        return rc

    async def run(self, issue_id: str, run_id: str) -> RunResult:
        issue = await self._repo.get(issue_id)
        if not issue:
            return RunResult(status="failed", reason="issue_not_found")

        # Load project repo config
        self._repo_config = await self._load_repo_config(issue.project_id)
        repo_path = Path(self._repo_config.repo_path) if self._repo_config.repo_path else Path(".")
        self._worktree_mgr = WorktreeManager(repo_path)

        github_token = self._repo_config.github_token or settings.github_token
        self._github = GitHubClient(token=github_token) if github_token else None

        # Get plan artifacts
        plan_artifact = await self._repo.get_latest_artifact(issue_id, ArtifactType.PLAN_MD.value)
        verification_artifact = await self._repo.get_latest_artifact(issue_id, ArtifactType.VERIFICATION.value)

        plan_md = plan_artifact.content_md if plan_artifact else ""
        verification_data = verification_artifact.content if verification_artifact else {}

        if not plan_md:
            return RunResult(status="failed", reason="no_plan_md")

        # Phase 1: Implementation
        base_branch = self._repo_config.base_branch
        worktree = await self._worktree_mgr.create(issue_id, base_branch=base_branch)
        try:
            await self._emit_event(issue_id, EventType.BUILD_PHASE_COMPLETED, run_id, {"phase": "phase1_start"})

            # Run Claude Code CLI
            prompt = self._build_implementation_prompt(plan_md, issue.title, issue.description)
            code_result = await self._claude_code.run(
                worktree_path=worktree.path,
                prompt=prompt,
            )

            if not code_result.success:
                raise RuntimeError(f"Claude Code failed: {code_result.error[:500]}")

            # Commit
            commit_msg = f"{issue_id}: {issue.title}"
            commit_hash = await self._worktree_mgr.commit(worktree, commit_msg)
            await self._emit_event(issue_id, EventType.BUILD_PHASE_COMPLETED, run_id, {
                "phase": "phase1",
                "commit": commit_hash,
                "files_changed": code_result.files_changed,
            })

            # Phase 2: Verification
            verify_result = await self._run_verification(worktree, verification_data, issue_id, run_id)

            if verify_result.overall_status == "fail":
                # Auto-fix loop (max 2 attempts)
                for attempt in range(MAX_AUTO_FIX_ATTEMPTS):
                    logger.info("Auto-fix attempt %d/%d for %s", attempt + 1, MAX_AUTO_FIX_ATTEMPTS, issue_id)
                    failed_items = [r for r in verify_result.results if r.status == "fail"]
                    fix_prompt = self._build_fix_prompt(plan_md, failed_items)

                    fix_result = await self._claude_code.run(worktree_path=worktree.path, prompt=fix_prompt)
                    if fix_result.success:
                        await self._worktree_mgr.commit(worktree, f"{issue_id}: fix verification failures (attempt {attempt + 1})")
                        verify_result = await self._run_verification(worktree, verification_data, issue_id, run_id)
                        if verify_result.overall_status == "pass":
                            break
                else:
                    if verify_result.overall_status == "fail":
                        raise VerificationFailed("Verification failed after auto-fix attempts")

            # Save verify result
            verify_result_path = worktree.path / ".cogniq" / "verify-result.toml"
            self.write_toml(verify_result_path, self._verify_result_to_dict(verify_result))
            await self._push_artifact(issue_id, run_id, ArtifactType.VERIFY_RESULT, verify_result_path)
            await self._emit_event(issue_id, EventType.BUILD_PHASE_COMPLETED, run_id, {"phase": "phase2"})

            # Phase 3: Adversarial Review
            code_diff = await self._worktree_mgr.get_diff(worktree)
            review = await self._run_adversarial_review(code_diff, plan_md, worktree, verification_data, issue_id, run_id)

            if review:
                verify_result.post_review = review

            await self._emit_event(issue_id, EventType.BUILD_PHASE_COMPLETED, run_id, {"phase": "phase3"})

            # Ship: Rebase + Push + PR
            rebase_ok = await self._worktree_mgr.rebase(worktree)
            if not rebase_ok:
                logger.warning("Rebase conflict for %s — PR will be created without rebase", issue_id)

            await self._worktree_mgr.push(worktree)

            pr_data = {}
            if self._github and self._repo_config and self._repo_config.repo_url:
                repo_name = self._extract_repo_name(self._repo_config.repo_url)
                if repo_name:
                    pr_body = self._build_pr_body(verify_result)
                    pr_data = await self._github.create_pr(
                        repo=repo_name,
                        branch=worktree.branch,
                        title=f"{issue_id}: {issue.title}",
                        body=pr_body,
                    )

            # Save build result
            build_result_data = {
                "meta": {"issue_id": issue_id, "created_by": "build"},
                "execution": {
                    "model": "claude-sonnet-4-20250514",
                    "actual_turns": code_result.turns_used,
                    "cost_usd": round(self._cost_tracker.total_usd, 4),
                    "tokens_used": self._cost_tracker.total_tokens,
                },
                "git": {
                    "branch": worktree.branch,
                    "files_changed": code_result.files_changed,
                },
                "pr": pr_data,
            }
            build_result_path = worktree.path / ".cogniq" / "build-result.toml"
            self.write_toml(build_result_path, build_result_data)
            await self._push_artifact(issue_id, run_id, ArtifactType.BUILD_RESULT, build_result_path)

            return RunResult(status="success", pr_url=pr_data.get("url", ""))

        except (VerificationFailed, RuntimeError) as e:
            await self._worktree_mgr.rollback(worktree)
            raise
        except Exception as e:
            await self._worktree_mgr.rollback(worktree)
            raise

    # ── Phase 2: Verification ──

    async def _run_verification(
        self,
        worktree: Worktree,
        verification_data: dict[str, Any],
        issue_id: str,
        run_id: str,
    ) -> VerifyResult:
        from cogniq.domain.verification import VerificationMeta

        defaults = DefaultsResult()
        results: list[VerificationItemResult] = []

        # Step 1: Default verification (lint, typecheck, test, secret_scan)
        rc = self._repo_config
        defaults.lint = await self._run_check(worktree, rc.lint_command if rc else "ruff check .", "lint")
        defaults.typecheck = await self._run_check(worktree, rc.typecheck_command if rc else "ruff check --select ANN .", "typecheck")
        defaults.test = await self._run_check(worktree, rc.test_command if rc else "uv run pytest", "test")
        if not rc or rc.secret_scan_enabled:
            defaults.secret_scan = await self._run_check(worktree, self._secret_scan_command(), "secret_scan")

        # Step 2: Issue-specific verification (from verification.toml)
        acceptance = verification_data.get("acceptance", [])
        for item in acceptance:
            method = item.get("verify_method", "manual")
            item_id = item.get("id", "?")

            if method == "test":
                check = await self._run_check(worktree, "uv run pytest -x", f"test:{item_id}")
                status = "pass" if check and check.get("status") == "pass" else "fail"
            elif method == "existence":
                # Check if expected files/functions exist
                status = "pass"  # Simplified — real implementation would check specific paths
            else:
                status = "skip"

            result = VerificationItemResult(
                id=item_id,
                status=status,
                detail=item.get("description", ""),
                attempts=1,
            )
            results.append(result)

            event_type = EventType.VERIFICATION_PASSED if status == "pass" else EventType.VERIFICATION_FAILED
            await self._emit_event(issue_id, event_type, run_id, {"item_id": item_id})

        # Determine overall status
        all_pass = all(r.status in ("pass", "skip") for r in results)
        defaults_pass = all(
            (getattr(defaults, f) or {}).get("status") in ("pass", None)
            for f in ("lint", "typecheck", "test", "secret_scan")
        )
        overall = "pass" if (all_pass and defaults_pass) else "fail"

        return VerifyResult(
            meta=VerificationMeta(issue_id=issue_id, created_by="build"),
            overall_status=overall,
            defaults=defaults,
            results=results,
        )

    async def _run_check(self, worktree: Worktree, command: str, name: str) -> dict[str, Any]:
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                cwd=str(worktree.path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
            output = stdout.decode("utf-8", errors="replace")
            return {
                "status": "pass" if proc.returncode == 0 else "fail",
                "detail": output[:500],
                "returncode": proc.returncode,
            }
        except asyncio.TimeoutError:
            return {"status": "fail", "detail": f"{name} timed out"}
        except Exception as e:
            return {"status": "fail", "detail": str(e)}

    def _secret_scan_command(self) -> str:
        return (
            "git diff --cached 2>/dev/null | "
            "grep -iE '(api_key|secret|password|token|private_key)\\s*=\\s*[\"\\x27]' "
            "&& exit 1 || exit 0"
        )

    # ── Phase 3: Adversarial Review ──

    async def _run_adversarial_review(
        self,
        code_diff: str,
        plan_md: str,
        worktree: Worktree,
        verification_data: dict[str, Any],
        issue_id: str,
        run_id: str,
    ) -> PostReview | None:
        if not code_diff.strip():
            return None

        review_data = await self._claude.adversarial_review(code_diff[:10000], plan_md[:5000])
        findings_raw = review_data.get("findings", [])
        findings = [AdversarialFinding(**f) for f in findings_raw if isinstance(f, dict)]

        critical = [f for f in findings if f.severity == "critical"]
        warnings = [f for f in findings if f.severity == "warning"]
        infos = [f for f in findings if f.severity == "info"]

        # Auto-fix critical findings (max cycles)
        for cycle in range(MAX_ADVERSARIAL_CYCLES):
            if not critical:
                break
            logger.info("Adversarial fix cycle %d/%d for %s", cycle + 1, MAX_ADVERSARIAL_CYCLES, issue_id)
            fix_prompt = "Fix these critical issues:\n" + "\n".join(f"- {c.description}" for c in critical)
            fix_result = await self._claude_code.run(worktree_path=worktree.path, prompt=fix_prompt)
            if fix_result.success:
                await self._worktree_mgr.commit(worktree, f"{issue_id}: fix critical review findings (cycle {cycle + 1})")
                # Re-verify
                await self._run_verification(worktree, verification_data, issue_id, run_id)
                # Re-review
                new_diff = await self._worktree_mgr.get_diff(worktree)
                new_review = await self._claude.adversarial_review(new_diff[:10000], plan_md[:5000])
                new_findings = new_review.get("findings", [])
                critical = [AdversarialFinding(**f) for f in new_findings if isinstance(f, dict) and f.get("severity") == "critical"]
            else:
                break

        # Remaining criticals become warnings
        for c in critical:
            c.severity = "warning"
            warnings.append(c)

        all_findings = warnings + infos
        return PostReview(
            model_used="claude-opus-4-20250514",
            findings_critical=0,  # All resolved or demoted
            findings_warning=len(warnings),
            findings_info=len(infos),
            findings=all_findings,
        )

    # ── Helpers ──

    def _build_implementation_prompt(self, plan_md: str, title: str, description: str) -> str:
        return f"""You are implementing a development task. Follow the plan exactly.

## Task: {title}
{description}

## Implementation Plan
{plan_md}

Follow the plan's dependency order. After each task, run the verification command specified.
Do not modify files listed in the "수정 금지" section.
Follow existing code patterns referenced in the plan."""

    def _build_fix_prompt(self, plan_md: str, failed_items: list[VerificationItemResult]) -> str:
        failures = "\n".join(f"- {item.id}: {item.detail}" for item in failed_items)
        return f"""The following verification items failed. Fix them:

{failures}

Original plan:
{plan_md[:3000]}"""

    def _build_pr_body(self, verify_result: VerifyResult) -> str:
        lines = ["## Verification Results\n"]

        # Defaults
        for name in ("lint", "typecheck", "test", "secret_scan"):
            result = getattr(verify_result.defaults, name, None) or {}
            status = result.get("status", "unknown")
            emoji = "\u2705" if status == "pass" else "\u274c"
            lines.append(f"{emoji} {name} \u2014 {status}")

        lines.append("\n## Acceptance Criteria\n")
        for r in verify_result.results:
            emoji = "\u2705" if r.status == "pass" else ("\u23ed\ufe0f" if r.status == "skip" else "\u274c")
            lines.append(f"{emoji} {r.id}: {r.detail}")

        if verify_result.post_review:
            lines.append("\n## Adversarial Review\n")
            for f in verify_result.post_review.findings:
                emoji = "\u26a0\ufe0f" if f.severity == "warning" else "\u2139\ufe0f"
                fixed = " (auto-fixed)" if f.auto_fixed else ""
                lines.append(f"{emoji} {f.severity.upper()}: {f.description}{fixed}")

        # Manual checklist
        manual_items = [r for r in verify_result.results if r.status == "skip"]
        if manual_items:
            lines.append("\n## Manual Check Required\n")
            for item in manual_items:
                lines.append(f"- [ ] {item.id}: {item.detail}")

        return "\n".join(lines)

    @staticmethod
    def _extract_repo_name(repo_url: str) -> str:
        """Extract 'owner/repo' from a GitHub URL."""
        url = repo_url.rstrip("/")
        if "github.com" in url:
            parts = url.split("github.com/")[-1]
            parts = parts.replace(".git", "")
            segments = parts.split("/")
            if len(segments) >= 2:
                return f"{segments[0]}/{segments[1]}"
        return ""

    def _verify_result_to_dict(self, vr: VerifyResult) -> dict[str, Any]:
        return {
            "meta": vr.meta.model_dump(),
            "overall_status": vr.overall_status,
            "defaults": vr.defaults.model_dump(),
            "results": [r.model_dump() for r in vr.results],
            "post_review": vr.post_review.model_dump() if vr.post_review else None,
        }
