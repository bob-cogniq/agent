import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cogniq_worker.agents.base import BaseAgent, RunResult
from cogniq_worker.agents.claude_client import ClaudeClient
from cogniq_worker.agents.claude_code import ClaudeCodeRunner
from cogniq_worker.workspace import ProjectWorkspace, WorkspaceManager
from cogniq_shared.config import settings
from cogniq_shared.domain.enums import ArtifactType, EventType, Stage
from cogniq_shared.domain.issue import Project as ProjectModel, RepoConfig
from cogniq_shared.domain.verification import VerifyResult, VerificationItemResult, DefaultsResult, PostReview, AdversarialFinding
from cogniq_shared.integrations.github import GitHubClient
from cogniq_shared.registry.repository import IssueRepository

logger = logging.getLogger(__name__)

MAX_AUTO_FIX_ATTEMPTS = 2
MAX_ADVERSARIAL_CYCLES = 2


class VerificationFailed(Exception):
    pass


class BuildAgent(BaseAgent):
    """DeveloperAgent: implements code, runs verification, creates PRs."""

    stage = Stage.BUILD

    def __init__(self, repo: IssueRepository, workspace_mgr: WorkspaceManager):
        super().__init__(
            repo=repo,
            timeout_seconds=settings.build_timeout_minutes * 60,
            max_cost_usd=settings.build_max_cost_usd,
        )
        self._workspace_mgr = workspace_mgr
        self._claude = ClaudeClient(
            cost_tracker=self._cost_tracker,
        )
        self._claude_code = ClaudeCodeRunner(
            cost_tracker=self._cost_tracker,
            max_turns=settings.build_max_turns,
        )
        self._github: GitHubClient | None = None
        self._project: ProjectModel | None = None

    async def _load_project(self, project_id: str) -> ProjectModel:
        """Load project from DB."""
        from cogniq_shared.registry.database import get_database
        db = get_database()
        project = await db["projects"].find_one({"_id": project_id})
        if not project or not project.get("repos"):
            raise RuntimeError(f"Project {project_id} has no repos configured")
        return ProjectModel(**project)

    async def run(self, issue_id: str, run_id: str) -> RunResult:
        issue = await self._repo.get(issue_id)
        if not issue:
            return RunResult(status="failed", reason="issue_not_found")

        # Load project and ensure workspace
        self._project = await self._load_project(issue.project_id)
        workspace = await self._workspace_mgr.ensure_workspace(self._project)

        primary_repo = self._project.get_primary_repo()
        if not primary_repo:
            return RunResult(status="failed", reason="no_repo_configured")

        github_token = primary_repo.github_token or settings.github_token
        self._github = GitHubClient(token=github_token) if github_token else None

        # Get plan artifacts
        plan_artifact = await self._repo.get_latest_artifact(issue_id, ArtifactType.PLAN_MD.value)
        verification_artifact = await self._repo.get_latest_artifact(issue_id, ArtifactType.VERIFICATION.value)

        plan_md = plan_artifact.content_md if plan_artifact else ""
        verification_data = verification_artifact.content if verification_artifact else {}

        if not plan_md:
            return RunResult(status="failed", reason="no_plan_md")

        # Create worktrees for all repos under work/{issue_id}/
        worktree_paths = await self._workspace_mgr.create_worktrees(
            workspace, issue_id, self._project.repos,
        )
        if not worktree_paths:
            return RunResult(status="failed", reason="worktree_creation_failed")

        branch = f"agent/{issue_id.lower().replace(' ', '-')}"
        primary_wt = worktree_paths.get(primary_repo.repo_id)

        try:
            await self._emit_event(issue_id, EventType.BUILD_PHASE_COMPLETED, run_id, {"phase": "phase1_start"})

            # Phase 1: Implementation — Claude Code runs at workspace root
            prompt = self._build_implementation_prompt(
                plan_md, issue.title, issue.description, workspace, worktree_paths,
            )
            code_result = await self._claude_code.run(
                workspace_root=workspace.root_path,
                worktree_paths=worktree_paths,
                prompt=prompt,
            )

            if not code_result.success:
                raise RuntimeError(f"Claude Code failed: {code_result.error[:500]}")

            # Commit changes in all repos that have modifications
            all_files_changed: list[str] = code_result.files_changed
            commit_msg = f"{issue_id}: {issue.title}"
            commit_hashes: dict[str, str | None] = {}
            for repo_id, wt_path in worktree_paths.items():
                h = await self._workspace_mgr.commit_worktree(wt_path, commit_msg)
                commit_hashes[repo_id] = h

            await self._emit_event(issue_id, EventType.BUILD_PHASE_COMPLETED, run_id, {
                "phase": "phase1",
                "commits": {k: v for k, v in commit_hashes.items() if v},
                "files_changed": all_files_changed,
            })

            # Phase 2: Verification (run on primary repo worktree)
            verify_result = await self._run_verification(
                primary_wt, primary_repo, verification_data, issue_id, run_id,
            )

            if verify_result.overall_status == "fail":
                for attempt in range(MAX_AUTO_FIX_ATTEMPTS):
                    logger.info("Auto-fix attempt %d/%d for %s", attempt + 1, MAX_AUTO_FIX_ATTEMPTS, issue_id)
                    failed_items = [r for r in verify_result.results if r.status == "fail"]
                    fix_prompt = self._build_fix_prompt(plan_md, failed_items)

                    fix_result = await self._claude_code.run(
                        workspace_root=workspace.root_path,
                        worktree_paths=worktree_paths,
                        prompt=fix_prompt,
                    )
                    if fix_result.success:
                        for repo_id, wt_path in worktree_paths.items():
                            await self._workspace_mgr.commit_worktree(
                                wt_path, f"{issue_id}: fix verification failures (attempt {attempt + 1})"
                            )
                        verify_result = await self._run_verification(
                            primary_wt, primary_repo, verification_data, issue_id, run_id,
                        )
                        if verify_result.overall_status == "pass":
                            break
                else:
                    if verify_result.overall_status == "fail":
                        raise VerificationFailed("Verification failed after auto-fix attempts")

            # Save verify result
            cogniq_dir = workspace.root_path / ".cogniq"
            cogniq_dir.mkdir(parents=True, exist_ok=True)
            verify_result_path = cogniq_dir / "verify-result.toml"
            self.write_toml(verify_result_path, self._verify_result_to_dict(verify_result))
            await self._push_artifact(issue_id, run_id, ArtifactType.VERIFY_RESULT, verify_result_path)
            await self._emit_event(issue_id, EventType.BUILD_PHASE_COMPLETED, run_id, {"phase": "phase2"})

            # Phase 3: Adversarial Review (on primary repo diff)
            code_diff = await self._workspace_mgr.get_diff(primary_wt, primary_repo.base_branch)
            review = await self._run_adversarial_review(
                code_diff, plan_md, primary_wt, primary_repo, verification_data, issue_id, run_id,
            )
            if review:
                verify_result.post_review = review

            await self._emit_event(issue_id, EventType.BUILD_PHASE_COMPLETED, run_id, {"phase": "phase3"})

            # Ship: Rebase + Push + PR for each repo with changes
            pr_data: dict[str, Any] = {}
            for repo_id, wt_path in worktree_paths.items():
                rc = next((r for r in self._project.repos if r.repo_id == repo_id), None)
                if not rc or not commit_hashes.get(repo_id):
                    continue

                rebase_ok = await self._workspace_mgr.rebase_worktree(wt_path, rc.base_branch)
                if not rebase_ok:
                    logger.warning("Rebase conflict for %s in %s", issue_id, repo_id)

                await self._workspace_mgr.push_worktree(wt_path, branch)

                # Create PR for each repo that has changes
                token = rc.github_token or settings.github_token
                gh = GitHubClient(token=token) if token else None
                if gh and rc.repo_url:
                    repo_name = self._extract_repo_name(rc.repo_url)
                    if repo_name:
                        pr_body = self._build_pr_body(verify_result)
                        this_pr = await gh.create_pr(
                            repo=repo_name,
                            branch=branch,
                            title=f"{issue_id}: {issue.title}",
                            body=pr_body,
                        )
                        if repo_id == primary_repo.repo_id:
                            pr_data = this_pr

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
                    "branch": branch,
                    "files_changed": all_files_changed,
                    "repos_modified": [k for k, v in commit_hashes.items() if v],
                },
                "pr": pr_data,
            }
            build_result_path = cogniq_dir / "build-result.toml"
            self.write_toml(build_result_path, build_result_data)
            await self._push_artifact(issue_id, run_id, ArtifactType.BUILD_RESULT, build_result_path)

            return RunResult(status="success", pr_url=pr_data.get("url", ""))

        except (VerificationFailed, RuntimeError):
            for wt_path in worktree_paths.values():
                rc = next((r for r in self._project.repos if worktree_paths.get(r.repo_id) == wt_path), None)
                if rc:
                    await self._workspace_mgr.rollback_worktree(wt_path, rc.base_branch)
            raise
        except Exception:
            for wt_path in worktree_paths.values():
                rc = next((r for r in self._project.repos if worktree_paths.get(r.repo_id) == wt_path), None)
                if rc:
                    await self._workspace_mgr.rollback_worktree(wt_path, rc.base_branch)
            raise

    # ── Phase 2: Verification ──

    async def _run_verification(
        self,
        wt_path: Path | None,
        repo_config: RepoConfig,
        verification_data: dict[str, Any],
        issue_id: str,
        run_id: str,
    ) -> VerifyResult:
        from cogniq_shared.domain.verification import VerificationMeta

        defaults = DefaultsResult()
        results: list[VerificationItemResult] = []

        if not wt_path:
            return VerifyResult(
                meta=VerificationMeta(issue_id=issue_id, created_by="build"),
                overall_status="fail",
                defaults=defaults,
                results=[],
            )

        # Step 1: Default verification (lint, typecheck, test, secret_scan)
        # Skip checks with empty commands
        rc = repo_config
        if rc.lint_command:
            defaults.lint = await self._run_check(wt_path, rc.lint_command, "lint")
        if rc.typecheck_command:
            defaults.typecheck = await self._run_check(wt_path, rc.typecheck_command, "typecheck")
        if rc.test_command:
            defaults.test = await self._run_check(wt_path, rc.test_command, "test")
        if rc.secret_scan_enabled:
            defaults.secret_scan = await self._run_check(wt_path, self._secret_scan_command(), "secret_scan")

        # Step 2: Issue-specific verification (from verification.toml)
        acceptance = verification_data.get("acceptance", [])
        for item in acceptance:
            method = item.get("verify_method", "manual")
            item_id = item.get("id", "?")

            if method == "test":
                test_cmd = rc.test_command
                if not test_cmd:
                    status = "skip"
                else:
                    check = await self._run_check(wt_path, test_cmd, f"test:{item_id}")
                    status = "pass" if check and check.get("status") == "pass" else "fail"
            elif method == "existence":
                status = "pass"
            elif method == "manual":
                status = "skip"
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

    async def _run_check(self, wt_path: Path, command: str, name: str) -> dict[str, Any]:
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                cwd=str(wt_path),
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
        wt_path: Path | None,
        repo_config: RepoConfig,
        verification_data: dict[str, Any],
        issue_id: str,
        run_id: str,
    ) -> PostReview | None:
        if not code_diff.strip() or not wt_path:
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
            fix_result = await self._claude_code.run(
                workspace_root=wt_path.parent.parent,  # workspace root
                worktree_paths={repo_config.repo_id: wt_path},
                prompt=fix_prompt,
            )
            if fix_result.success:
                await self._workspace_mgr.commit_worktree(
                    wt_path, f"{issue_id}: fix critical review findings (cycle {cycle + 1})"
                )
                await self._run_verification(wt_path, repo_config, verification_data, issue_id, run_id)
                new_diff = await self._workspace_mgr.get_diff(wt_path, repo_config.base_branch)
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
            findings_critical=0,
            findings_warning=len(warnings),
            findings_info=len(infos),
            findings=all_findings,
        )

    # ── Helpers ──

    def _build_implementation_prompt(
        self,
        plan_md: str,
        title: str,
        description: str,
        workspace: ProjectWorkspace,
        worktree_paths: dict[str, Path],
    ) -> str:
        # Build repo map for the prompt
        repo_lines = []
        for repo_id, wt_path in worktree_paths.items():
            rel_path = wt_path.relative_to(workspace.root_path)
            is_primary = repo_id == workspace.primary_repo_id
            label = " (primary)" if is_primary else ""
            repo_lines.append(f"- {rel_path}/{label}")

        repo_map = "\n".join(repo_lines)

        return f"""You are implementing a development task. Follow the plan exactly.

## Task: {title}
{description}

## Workspace Layout
The current working directory contains multiple repositories as worktrees:
{repo_map}

CRITICAL RULES:
- You MUST create and modify files ONLY inside the work/ directory (worktrees listed above).
- The repos/ directory contains source clones — NEVER modify files there.
- When the task says "create a file in the project root", it means the root of the appropriate worktree under work/, NOT the current working directory.
- Always use the full relative path starting with work/ when creating or editing files.
  For example: {list(worktree_paths.values())[0].relative_to(workspace.root_path)}/example.txt

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
        data: dict[str, Any] = {
            "meta": vr.meta.model_dump(),
            "overall_status": vr.overall_status,
            "defaults": vr.defaults.model_dump(),
            "results": [r.model_dump() for r in vr.results],
        }
        if vr.post_review:
            data["post_review"] = vr.post_review.model_dump()
        return data
