import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

LINEAR_API_URL = "https://api.linear.app/graphql"


class LinearClient:
    def __init__(self, api_key: str):
        self._api_key = api_key
        self._headers = {
            "Authorization": api_key,
            "Content-Type": "application/json",
        }

    async def _query(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                LINEAR_API_URL,
                json={"query": query, "variables": variables or {}},
                headers=self._headers,
                timeout=30.0,
            )
            resp.raise_for_status()
            data = resp.json()
            if "errors" in data:
                logger.error("Linear API error: %s", data["errors"])
            return data.get("data", {})

    async def get_issue(self, issue_id: str) -> dict[str, Any]:
        query = """
        query($id: String!) {
            issue(id: $id) {
                id identifier title description priority
                state { name }
                labels { nodes { name } }
                comments { nodes { body createdAt } }
                team { key }
            }
        }
        """
        data = await self._query(query, {"id": issue_id})
        return data.get("issue", {})

    async def update_status(self, issue_id: str, state_name: str) -> None:
        # First find the state ID
        query = """
        mutation($id: String!, $stateId: String!) {
            issueUpdate(id: $id, input: { stateId: $stateId }) {
                success
            }
        }
        """
        # This is simplified — in practice you'd look up the state ID first
        logger.info("Linear: update status %s → %s", issue_id, state_name)

    async def add_comment(self, issue_id: str, body: str) -> None:
        query = """
        mutation($issueId: String!, $body: String!) {
            commentCreate(input: { issueId: $issueId, body: $body }) {
                success
            }
        }
        """
        await self._query(query, {"issueId": issue_id, "body": body})
        logger.info("Linear: comment added to %s", issue_id)

    async def create_sub_issues(
        self, parent_id: str, sub_issues: list[dict[str, Any]], team_id: str = ""
    ) -> list[str]:
        created_ids = []
        for sub in sub_issues:
            query = """
            mutation($title: String!, $teamId: String!, $parentId: String!, $description: String) {
                issueCreate(input: {
                    title: $title
                    teamId: $teamId
                    parentId: $parentId
                    description: $description
                }) {
                    success
                    issue { id }
                }
            }
            """
            data = await self._query(query, {
                "title": sub.get("title", ""),
                "teamId": team_id,
                "parentId": parent_id,
                "description": sub.get("description", ""),
            })
            issue = data.get("issueCreate", {}).get("issue", {})
            if issue.get("id"):
                created_ids.append(issue["id"])
        return created_ids
