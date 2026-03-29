import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

SLACK_API_URL = "https://slack.com/api/chat.postMessage"


class SlackClient:
    def __init__(self, bot_token: str, default_channel: str = "#cogniq-updates"):
        self._token = bot_token
        self._default_channel = default_channel
        self._headers = {
            "Authorization": f"Bearer {bot_token}",
            "Content-Type": "application/json",
        }

    async def notify(self, event: str, issue_id: str, payload: dict[str, Any] | None = None) -> None:
        """Route event to appropriate notification method."""
        payload = payload or {}
        match event:
            case "plan_complete":
                await self._send(self._format_plan_complete(issue_id, payload))
            case "build_complete":
                await self._send(self._format_build_complete(issue_id, payload))
            case "build_failed":
                await self._send(self._format_build_failed(issue_id, payload))
            case "gate1_reminder":
                await self._send(self._format_gate_reminder(issue_id, "Gate 1", payload))
            case "gate2_reminder":
                await self._send(self._format_gate_reminder(issue_id, "Gate 2", payload))
            case _:
                logger.debug("No notification template for event: %s", event)

    async def send_message(self, channel: str, text: str, blocks: list | None = None) -> None:
        await self._send(text, channel, blocks)

    async def _send(self, text: str, channel: str | None = None, blocks: list | None = None) -> None:
        if not self._token:
            logger.debug("Slack: no token configured, skipping notification")
            return

        body: dict[str, Any] = {
            "channel": channel or self._default_channel,
            "text": text,
        }
        if blocks:
            body["blocks"] = blocks

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(SLACK_API_URL, json=body, headers=self._headers, timeout=10.0)
                data = resp.json()
                if not data.get("ok"):
                    logger.warning("Slack API error: %s", data.get("error"))
        except Exception as e:
            logger.warning("Slack notification failed: %s", e)

    def _format_plan_complete(self, issue_id: str, payload: dict) -> str:
        count = payload.get("artifact_count", "?")
        return f":robot_face: *{issue_id}* Plan \uc644\ub8cc \u2014 \uc2b9\uc778 \ub300\uae30\n\u251c\u2500 \uc0b0\ucd9c\ubb3c: {count}\uac1c\n\u2514\u2500 Gate 1 \uc2b9\uc778\uc744 \uae30\ub2e4\ub9bd\ub2c8\ub2e4"

    def _format_build_complete(self, issue_id: str, payload: dict) -> str:
        pr_url = payload.get("pr_url", "")
        return f":white_check_mark: *{issue_id}* Build \uc644\ub8cc\n\u251c\u2500 PR: {pr_url}\n\u2514\u2500 Gate 2 \ub9ac\ubdf0\ub97c \uae30\ub2e4\ub9bd\ub2c8\ub2e4"

    def _format_build_failed(self, issue_id: str, payload: dict) -> str:
        error = payload.get("error", "unknown")
        return f":red_circle: *{issue_id}* Build \uc2e4\ud328\n\u251c\u2500 \uc6d0\uc778: {error}\n\u2514\u2500 \ud655\uc778\uc774 \ud544\uc694\ud569\ub2c8\ub2e4"

    def _format_gate_reminder(self, issue_id: str, gate: str, payload: dict) -> str:
        hours = payload.get("hours_waiting", "?")
        return f":bell: *{issue_id}* {gate} \ub9ac\ub9c8\uc778\ub354\n\u2514\u2500 {hours}\uc2dc\uac04 \ub300\uae30 \uc911"
