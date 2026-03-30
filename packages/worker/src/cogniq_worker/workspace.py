"""Workspace manager — sets up per-project work folders with cloned repos."""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from cogniq_shared.domain.issue import Project, RepoConfig

logger = logging.getLogger(__name__)


@dataclass
class ProjectWorkspace:
    """Represents a ready-to-use project workspace."""

    project_id: str
    root_path: Path  # CLI cwd — parent of repos/ and work/
    repos: dict[str, Path] = field(default_factory=dict)  # repo_id → clone path
    primary_repo_id: str | None = None

    def repo_path(self, repo_id: str) -> Path | None:
        return self.repos.get(repo_id)

    def work_dir(self, issue_id: str) -> Path:
        """Return the issue-level work directory under work/."""
        return self.root_path / "work" / _sanitize(issue_id)


def _sanitize(value: str) -> str:
    return value.lower().replace(" ", "-").replace("/", "-")


class WorkspaceManager:
    """Manages per-project work folders with cloned repos.

    Directory layout::

        {base_dir}/{project_id}/
        ├── .workspace.json
        ├── repos/{repo_id}/        ← git clones
        ├── work/{issue_id}/        ← worktrees per issue
        └── .cogniq/
    """

    def __init__(self, base_dir: Path):
        self._base_dir = base_dir
        self._base_dir.mkdir(parents=True, exist_ok=True)

    # ── Public API ──

    async def ensure_workspace(self, project: Project) -> ProjectWorkspace:
        """Get or create workspace for a project. Clones missing repos, fetches existing ones."""
        project_dir = self._base_dir / project.id
        existing = self._load_workspace_meta(project_dir)

        if existing:
            # Workspace exists — fetch all repos to stay current
            ws = self._build_workspace(project, project_dir)
            await self._fetch_all(ws, project)
            self._save_workspace_meta(project_dir, project)
            return ws

        # First time — create structure and clone repos
        return await self.setup_project(project)

    async def setup_project(self, project: Project) -> ProjectWorkspace:
        """Create workspace from scratch: dirs + clone all repos."""
        project_dir = self._base_dir / project.id
        repos_dir = project_dir / "repos"
        work_dir = project_dir / "work"
        cogniq_dir = project_dir / ".cogniq"

        for d in (repos_dir, work_dir, cogniq_dir):
            d.mkdir(parents=True, exist_ok=True)

        ws = self._build_workspace(project, project_dir)

        # Clone all repos in parallel
        tasks = []
        for rc in project.repos:
            clone_dir = repos_dir / rc.repo_id
            tasks.append(self._clone_repo(rc, clone_dir))

        results = await asyncio.gather(*tasks, return_exceptions=True)
        for rc, result in zip(project.repos, results):
            if isinstance(result, Exception):
                logger.error("Failed to clone %s: %s", rc.repo_url, result)
            else:
                ws.repos[rc.repo_id] = repos_dir / rc.repo_id

        self._save_workspace_meta(project_dir, project)
        logger.info(
            "Workspace ready: project=%s repos=%d path=%s",
            project.id,
            len(ws.repos),
            project_dir,
        )
        return ws

    async def get_workspace(self, project_id: str) -> ProjectWorkspace | None:
        """Load an existing workspace without network calls."""
        project_dir = self._base_dir / project_id
        meta = self._load_workspace_meta(project_dir)
        if not meta:
            return None

        repos: dict[str, Path] = {}
        repos_dir = project_dir / "repos"
        if repos_dir.exists():
            for entry in repos_dir.iterdir():
                if entry.is_dir() and (entry / ".git").exists():
                    repos[entry.name] = entry

        return ProjectWorkspace(
            project_id=project_id,
            root_path=project_dir,
            repos=repos,
            primary_repo_id=meta.get("primary_repo_id"),
        )

    async def cleanup_issue(self, workspace: ProjectWorkspace, issue_id: str) -> None:
        """Remove all worktrees for an issue."""
        work_dir = workspace.work_dir(issue_id)
        if work_dir.exists():
            # Remove git worktrees properly first
            for repo_dir in work_dir.iterdir():
                if repo_dir.is_dir():
                    repo_id = repo_dir.name
                    source_repo = workspace.repos.get(repo_id)
                    if source_repo:
                        try:
                            await self._git(source_repo, "worktree", "remove", str(repo_dir), "--force")
                        except RuntimeError:
                            pass
            # Then clean up the directory
            import shutil

            shutil.rmtree(work_dir, ignore_errors=True)
            logger.info("Cleaned up work dir: %s", work_dir)

    # ── Worktree Management ──

    async def create_worktrees(
        self,
        workspace: ProjectWorkspace,
        issue_id: str,
        repo_configs: list[RepoConfig],
    ) -> dict[str, Path]:
        """Create worktrees for specified repos under work/{issue_id}/."""
        work_dir = workspace.work_dir(issue_id)
        work_dir.mkdir(parents=True, exist_ok=True)

        result: dict[str, Path] = {}
        branch = f"agent/{_sanitize(issue_id)}"

        for rc in repo_configs:
            source_repo = workspace.repos.get(rc.repo_id)
            if not source_repo:
                logger.warning("Repo %s not cloned, skipping worktree", rc.repo_id)
                continue

            wt_path = work_dir / rc.repo_id
            base_branch = rc.base_branch

            # Clean up if exists
            if wt_path.exists():
                try:
                    await self._git(source_repo, "worktree", "remove", str(wt_path), "--force")
                except RuntimeError:
                    import shutil

                    shutil.rmtree(wt_path, ignore_errors=True)

            # Fetch and create worktree
            await self._git(source_repo, "fetch", "origin", base_branch)

            # Delete branch if it already exists
            try:
                await self._git(source_repo, "branch", "-D", branch)
            except RuntimeError:
                pass

            await self._git(
                source_repo, "worktree", "add", "-b", branch, str(wt_path), f"origin/{base_branch}"
            )
            result[rc.repo_id] = wt_path
            logger.info("Worktree created: %s → %s", branch, wt_path)

            # Install dependencies if setup_command is configured
            if rc.setup_command:
                await self._install_deps(wt_path, rc.setup_command)
            else:
                await self._auto_install_deps(wt_path)

        return result

    async def _install_deps(self, wt_path: Path, command: str) -> None:
        """Run explicit setup command in worktree."""
        try:
            logger.info("Installing deps in %s: %s", wt_path.name, command)
            await self._run_shell(command, cwd=wt_path, timeout=300)
            logger.info("Deps installed: %s", wt_path.name)
        except Exception as e:
            logger.warning("Dep install failed in %s: %s", wt_path.name, e)

    async def _auto_install_deps(self, wt_path: Path) -> None:
        """Auto-detect and install dependencies based on project files."""
        if (wt_path / "pyproject.toml").exists() and (wt_path / "uv.lock").exists():
            await self._install_deps(wt_path, "uv sync")
        elif (wt_path / "pyproject.toml").exists():
            await self._install_deps(wt_path, "uv sync")
        elif (wt_path / "package-lock.json").exists():
            await self._install_deps(wt_path, "npm ci")
        elif (wt_path / "package.json").exists():
            await self._install_deps(wt_path, "npm install")

    @staticmethod
    async def _run_shell(command: str, cwd: Path, timeout: int = 300) -> str:
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        if proc.returncode != 0:
            error = stderr.decode("utf-8", errors="replace")
            raise RuntimeError(f"Command failed ({command}): {error[:500]}")
        return stdout.decode("utf-8", errors="replace")

    async def commit_worktree(self, wt_path: Path, message: str) -> str | None:
        """Stage all and commit in a worktree. Returns commit hash or None."""
        status = await self._git(wt_path, "status", "--porcelain")
        if not status.strip():
            return None
        await self._git(wt_path, "add", "-A")
        await self._git(wt_path, "commit", "-m", message)
        commit_hash = await self._git(wt_path, "rev-parse", "HEAD")
        return commit_hash.strip()

    async def push_worktree(self, wt_path: Path, branch: str) -> None:
        await self._git(wt_path, "push", "-u", "origin", branch)

    async def rebase_worktree(self, wt_path: Path, base_branch: str) -> bool:
        await self._git(wt_path, "fetch", "origin", base_branch)
        try:
            await self._git(wt_path, "rebase", f"origin/{base_branch}")
            return True
        except RuntimeError:
            try:
                await self._git(wt_path, "rebase", "--abort")
            except RuntimeError:
                pass
            return False

    async def get_diff(self, wt_path: Path, base_branch: str) -> str:
        return await self._git(wt_path, "diff", f"origin/{base_branch}...HEAD")

    async def rollback_worktree(self, wt_path: Path, base_branch: str) -> None:
        await self._git(wt_path, "reset", "--hard", f"origin/{base_branch}")

    # ── Internal ──

    def _build_workspace(self, project: Project, project_dir: Path) -> ProjectWorkspace:
        repos_dir = project_dir / "repos"
        repos: dict[str, Path] = {}
        primary_id: str | None = None

        for rc in project.repos:
            clone_path = repos_dir / rc.repo_id
            if clone_path.exists():
                repos[rc.repo_id] = clone_path
            if rc.is_primary:
                primary_id = rc.repo_id

        if not primary_id and project.repos:
            primary_id = project.repos[0].repo_id

        return ProjectWorkspace(
            project_id=project.id,
            root_path=project_dir,
            repos=repos,
            primary_repo_id=primary_id,
        )

    async def _clone_repo(self, rc: RepoConfig, target_dir: Path) -> None:
        if target_dir.exists() and (target_dir / ".git").exists():
            logger.info("Repo already cloned: %s", target_dir)
            return

        target_dir.mkdir(parents=True, exist_ok=True)

        clone_url = rc.repo_url
        if rc.github_token and "github.com" in clone_url:
            clone_url = clone_url.replace("https://", f"https://x-access-token:{rc.github_token}@")

        await self._run_command(
            "git", "clone", "--depth=1", clone_url, str(target_dir)
        )
        # Unshallow so worktrees work properly
        await self._git(target_dir, "fetch", "--unshallow")
        logger.info("Cloned %s → %s", rc.repo_url, target_dir)

    async def _fetch_all(self, ws: ProjectWorkspace, project: Project) -> None:
        """Fetch origin for all cloned repos."""
        tasks = []
        for rc in project.repos:
            repo_path = ws.repos.get(rc.repo_id)
            if repo_path and (repo_path / ".git").exists():
                tasks.append(self._git(repo_path, "fetch", "origin"))
            elif self._base_dir:
                # Repo not cloned yet — clone it
                clone_dir = ws.root_path / "repos" / rc.repo_id
                tasks.append(self._clone_repo(rc, clone_dir))

        results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, Exception):
                logger.warning("Fetch/clone error: %s", r)

    def _save_workspace_meta(self, project_dir: Path, project: Project) -> None:
        meta = {
            "project_id": project.id,
            "repos": [
                {
                    "repo_id": rc.repo_id,
                    "repo_url": rc.repo_url,
                    "base_branch": rc.base_branch,
                    "is_primary": rc.is_primary,
                }
                for rc in project.repos
            ],
            "primary_repo_id": (project.get_primary_repo() or project.repos[0]).repo_id if project.repos else None,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        meta_path = project_dir / ".workspace.json"
        meta_path.write_text(json.dumps(meta, indent=2))

    def _load_workspace_meta(self, project_dir: Path) -> dict | None:
        meta_path = project_dir / ".workspace.json"
        if not meta_path.exists():
            return None
        try:
            return json.loads(meta_path.read_text())
        except (json.JSONDecodeError, OSError):
            return None

    @staticmethod
    async def _git(repo_path: Path, *args: str) -> str:
        return await WorkspaceManager._run_command("git", *args, cwd=repo_path)

    @staticmethod
    async def _run_command(cmd: str, *args: str, cwd: Path | None = None) -> str:
        proc = await asyncio.create_subprocess_exec(
            cmd,
            *args,
            cwd=str(cwd) if cwd else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            error = stderr.decode("utf-8", errors="replace")
            raise RuntimeError(f"Command failed: {cmd} {' '.join(args)}\n{error}")
        return stdout.decode("utf-8", errors="replace")
