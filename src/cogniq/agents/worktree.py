import asyncio
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class Worktree:
    path: Path
    branch: str
    issue_id: str
    base_branch: str = "main"


class WorktreeManager:
    """Manages Git worktrees for isolated agent work."""

    def __init__(self, repo_path: Path, worktrees_dir: Path | None = None):
        self._repo_path = repo_path
        self._worktrees_dir = worktrees_dir or repo_path / ".worktrees"
        self._worktrees_dir.mkdir(parents=True, exist_ok=True)

    async def create(self, issue_id: str, base_branch: str = "main") -> Worktree:
        """Create a new worktree with a dedicated branch."""
        branch = f"agent/{issue_id.lower().replace(' ', '-')}"
        worktree_path = self._worktrees_dir / branch.replace("/", "-")

        # Clean up if exists
        if worktree_path.exists():
            await self.remove(Worktree(path=worktree_path, branch=branch, issue_id=issue_id))

        # Create branch and worktree
        await self._git("fetch", "origin", base_branch)
        await self._git("worktree", "add", "-b", branch, str(worktree_path), f"origin/{base_branch}")

        logger.info("Worktree created: %s → %s", branch, worktree_path)
        return Worktree(path=worktree_path, branch=branch, issue_id=issue_id, base_branch=base_branch)

    async def commit(self, worktree: Worktree, message: str) -> str | None:
        """Stage all changes and commit. Returns commit hash or None if nothing to commit."""
        # Check for changes
        status = await self._git_in(worktree.path, "status", "--porcelain")
        if not status.strip():
            logger.info("No changes to commit in %s", worktree.branch)
            return None

        await self._git_in(worktree.path, "add", "-A")
        await self._git_in(worktree.path, "commit", "-m", message)

        # Get commit hash
        commit_hash = await self._git_in(worktree.path, "rev-parse", "HEAD")
        logger.info("Committed %s: %s", commit_hash[:7], message)
        return commit_hash.strip()

    async def push(self, worktree: Worktree) -> None:
        """Push the worktree branch to origin."""
        await self._git_in(worktree.path, "push", "-u", "origin", worktree.branch)
        logger.info("Pushed %s", worktree.branch)

    async def rebase(self, worktree: Worktree) -> bool:
        """Rebase worktree branch onto latest base branch. Returns True if successful."""
        await self._git_in(worktree.path, "fetch", "origin", worktree.base_branch)
        try:
            await self._git_in(worktree.path, "rebase", f"origin/{worktree.base_branch}")
            logger.info("Rebase successful: %s", worktree.branch)
            return True
        except RuntimeError as e:
            logger.warning("Rebase conflict in %s: %s", worktree.branch, e)
            # Abort rebase on conflict
            try:
                await self._git_in(worktree.path, "rebase", "--abort")
            except RuntimeError:
                pass
            return False

    async def get_diff(self, worktree: Worktree) -> str:
        """Get diff of all changes since base branch."""
        return await self._git_in(worktree.path, "diff", f"origin/{worktree.base_branch}...HEAD")

    async def rollback(self, worktree: Worktree) -> None:
        """Reset worktree to base branch state."""
        await self._git_in(worktree.path, "reset", "--hard", f"origin/{worktree.base_branch}")
        logger.info("Rolled back %s", worktree.branch)

    async def remove(self, worktree: Worktree) -> None:
        """Remove worktree and optionally its branch."""
        try:
            await self._git("worktree", "remove", str(worktree.path), "--force")
        except RuntimeError:
            # Manual cleanup if git worktree remove fails
            if worktree.path.exists():
                shutil.rmtree(worktree.path, ignore_errors=True)

        # Delete branch
        try:
            await self._git("branch", "-D", worktree.branch)
        except RuntimeError:
            pass

        logger.info("Removed worktree: %s", worktree.branch)

    async def _git(self, *args: str) -> str:
        return await self._run_command("git", *args, cwd=self._repo_path)

    async def _git_in(self, path: Path, *args: str) -> str:
        return await self._run_command("git", *args, cwd=path)

    @staticmethod
    async def _run_command(cmd: str, *args: str, cwd: Path) -> str:
        proc = await asyncio.create_subprocess_exec(
            cmd, *args,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            error = stderr.decode("utf-8", errors="replace")
            raise RuntimeError(f"Command failed: {cmd} {' '.join(args)}\n{error}")
        return stdout.decode("utf-8", errors="replace")
