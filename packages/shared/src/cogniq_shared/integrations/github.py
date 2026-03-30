import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

GITHUB_API_URL = "https://api.github.com"


class GitHubClient:
    def __init__(self, token: str):
        self._token = token
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def _request(self, method: str, path: str, json: dict | None = None, params: dict | None = None) -> dict[str, Any]:
        async with httpx.AsyncClient() as client:
            resp = await client.request(
                method,
                f"{GITHUB_API_URL}{path}",
                json=json,
                params=params,
                headers=self._headers,
                timeout=30.0,
            )
            resp.raise_for_status()
            return resp.json() if resp.content else {}

    async def _request_list(self, method: str, path: str, params: dict | None = None) -> list[dict[str, Any]]:
        """Request that returns a JSON array."""
        async with httpx.AsyncClient() as client:
            resp = await client.request(
                method,
                f"{GITHUB_API_URL}{path}",
                params=params,
                headers=self._headers,
                timeout=30.0,
            )
            resp.raise_for_status()
            return resp.json() if resp.content else []

    async def get_authenticated_user(self) -> dict[str, Any]:
        """Validate token and get user info. GET /user."""
        return await self._request("GET", "/user")

    async def list_repos(self, page: int = 1, per_page: int = 30) -> list[dict[str, Any]]:
        """List repos accessible to the authenticated user."""
        return await self._request_list("GET", "/user/repos", params={
            "sort": "updated",
            "affiliation": "owner,collaborator,organization_member",
            "per_page": per_page,
            "page": page,
        })

    async def search_repos(self, query: str, page: int = 1, per_page: int = 30) -> list[dict[str, Any]]:
        """Search repos by name."""
        data = await self._request("GET", "/search/repositories", params={
            "q": f"{query} in:name",
            "sort": "updated",
            "per_page": per_page,
            "page": page,
        })
        return data.get("items", [])

    async def create_pr(
        self,
        repo: str,
        branch: str,
        title: str,
        body: str,
        base: str = "main",
    ) -> dict[str, Any]:
        """Create a pull request. repo format: 'owner/repo'."""
        data = await self._request("POST", f"/repos/{repo}/pulls", json={
            "title": title,
            "body": body,
            "head": branch,
            "base": base,
        })
        logger.info("GitHub: PR created #%s on %s", data.get("number"), repo)
        return {
            "url": data.get("html_url", ""),
            "number": data.get("number", 0),
            "title": title,
        }

    async def update_pr(self, repo: str, pr_number: int, body: str) -> None:
        await self._request("PATCH", f"/repos/{repo}/pulls/{pr_number}", json={
            "body": body,
        })
        logger.info("GitHub: PR #%d updated on %s", pr_number, repo)

    async def get_pr_status(self, repo: str, pr_number: int) -> dict[str, Any]:
        data = await self._request("GET", f"/repos/{repo}/pulls/{pr_number}")
        return {
            "state": data.get("state", ""),
            "merged": data.get("merged", False),
            "mergeable": data.get("mergeable"),
            "title": data.get("title", ""),
        }

    async def merge_pr(self, repo: str, pr_number: int, method: str = "squash") -> bool:
        try:
            await self._request("PUT", f"/repos/{repo}/pulls/{pr_number}/merge", json={
                "merge_method": method,
            })
            logger.info("GitHub: PR #%d merged on %s", pr_number, repo)
            return True
        except httpx.HTTPStatusError as e:
            logger.error("GitHub: merge failed for PR #%d: %s", pr_number, e)
            return False
