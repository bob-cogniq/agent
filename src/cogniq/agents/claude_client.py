"""Claude client using Claude Code CLI (subprocess).

All Claude calls go through the `claude` CLI, which uses the OAuth token
configured via `claude setup-token`. No separate API key needed.
"""

import asyncio
import json
import logging
from typing import Any

from cogniq.agents.base import CostTracker

logger = logging.getLogger(__name__)


class ClaudeClient:
    """Wrapper around Claude Code CLI for structured AI calls."""

    def __init__(self, cost_tracker: CostTracker, model: str = "sonnet"):
        self._cost_tracker = cost_tracker
        self._model = model

    async def message(
        self,
        system: str,
        user_prompt: str,
        model: str | None = None,
    ) -> str:
        """Send a prompt via Claude Code CLI.

        Combines system + user prompt into a single -p argument.
        NOTE: --system-prompt with long -p causes empty results in Claude CLI,
        so we merge everything into one prompt string.
        """
        model = model or self._model
        combined = f"[INSTRUCTIONS]\n{system}\n\n[INPUT]\n{user_prompt}"

        cmd = [
            "claude",
            "-p", combined,
            "--model", model,
            "--output-format", "json",
            "--max-turns", "10",
            "--no-session-persistence",
        ]

        logger.info("Claude CLI: model=%s combined_len=%d", model, len(combined))

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=180)

        if proc.returncode != 0:
            error = stderr.decode("utf-8", errors="replace")[:500]
            logger.error("Claude CLI stderr: %s", error)
            raise RuntimeError(f"Claude CLI failed (rc={proc.returncode}): {error}")

        raw = stdout.decode("utf-8", errors="replace")
        logger.info("Claude CLI raw output len: %d", len(raw))
        result = self._parse_response(raw)
        logger.info("Claude CLI parsed len: %d", len(result))
        return result

    def _parse_response(self, raw: str) -> str:
        """Parse Claude CLI JSON output and track cost."""
        text_parts: list[str] = []

        for line in raw.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                msg_type = data.get("type", "")

                if msg_type == "result":
                    text_parts.append(data.get("result", ""))
                    # Track cost
                    usage = data.get("usage", {})
                    if usage:
                        self._cost_tracker.add(
                            usage.get("input_tokens", 0),
                            usage.get("output_tokens", 0),
                            data.get("model", ""),
                        )
                    cost_usd = data.get("cost_usd", 0)
                    if cost_usd:
                        logger.info(
                            "Claude CLI: tokens=%d cost=$%.4f",
                            usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
                            cost_usd,
                        )
                elif msg_type == "assistant":
                    # Streaming message content
                    content = data.get("message", {}).get("content", [])
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text_parts.append(block.get("text", ""))

            except json.JSONDecodeError:
                # Plain text line
                text_parts.append(line)

        return "\n".join(text_parts)

    async def analyze_codebase(self, issue_data: dict[str, Any], codebase_context: str) -> dict[str, Any]:
        """Analyze codebase for an issue. Returns structured analysis."""
        system = """You are a senior software architect analyzing a codebase for a development task.
Return ONLY a JSON object (no markdown, no explanation) with this structure:
{
    "business": { "background": "...", "feasibility": "...", "completion_criteria": ["..."] },
    "development": {
        "scope": "...",
        "changed_files": ["..."],
        "stages": [{"order": 1, "description": "...", "complexity": "low|medium|high"}],
        "estimated_complexity": "simple|normal|complex"
    },
    "design": { "required": false, "trigger": "", "trigger_detail": "" },
    "safety": { "flags": [], "notes": "" },
    "decompose": { "should_split": false, "strategy": "", "sub_issues": [] }
}"""

        prompt = f"Issue:\n{issue_data.get('title', '')}\n{issue_data.get('description', '')}\n\nCodebase context:\n{codebase_context}"
        response = await self.message(system, prompt)
        return self._extract_json(response, fallback={"raw_response": response})

    async def define_verification(self, issue_data: dict[str, Any], analysis: dict[str, Any]) -> dict[str, Any]:
        """Define verification criteria for an issue."""
        system = """You are defining testable verification criteria for a development task.
Return ONLY a JSON object (no markdown, no explanation) with:
{
    "acceptance": [
        {"id": "AC-1", "description": "...", "verify_method": "test|existence|manual", "expected": "..."}
    ],
    "safety": []
}
Only include safety items if the analysis indicates safety flags."""

        prompt = f"Issue:\n{issue_data.get('description', '')}\n\nAnalysis:\n{json.dumps(analysis, ensure_ascii=False, default=str)}"
        response = await self.message(system, prompt)
        return self._extract_json(response, fallback={"acceptance": [], "safety": []})

    async def generate_plan(self, issue_data: dict[str, Any], analysis: dict[str, Any]) -> str:
        """Generate plan.md content."""
        system = """You are generating a concise implementation plan (plan.md) for a developer agent.
Rules:
- 150 lines or less
- Self-contained: this document + codebase is all the developer needs
- Specific: file names, function names, conditions — no vague adjectives
- Include: goal, files to modify, files NOT to modify, tasks in dependency order
- Each task has a verification command
- Write in Korean"""

        # Strip raw_response and meta to keep prompt short
        analysis_summary = self._summarize_analysis(analysis)
        prompt = f"Issue:\n{issue_data.get('title', '')}\n{issue_data.get('description', '')}\n\nAnalysis:\n{analysis_summary}"
        return await self.message(system, prompt)

    async def generate_design(self, issue_data: dict[str, Any], analysis: dict[str, Any]) -> str:
        """Generate design.md content for UI changes."""
        system = """You are generating a UI/UX design document (design.md) for a developer agent.
Rules:
- 100 lines or less
- Focus on behavior, not appearance
- Define all component states (default/loading/error/empty/success)
- List existing components to reuse
- Include accessibility requirements
- Write in Korean"""

        analysis_summary = self._summarize_analysis(analysis)
        prompt = f"Issue:\n{issue_data.get('title', '')}\n{issue_data.get('description', '')}\n\nAnalysis:\n{analysis_summary}"
        return await self.message(system, prompt)

    @staticmethod
    def _summarize_analysis(analysis: dict[str, Any]) -> str:
        """Extract key fields from analysis, dropping raw_response and meta to keep prompts short."""
        summary = {}
        for key in ("business", "development", "design", "safety", "decompose"):
            if key in analysis:
                summary[key] = analysis[key]
        return json.dumps(summary, ensure_ascii=False, default=str)[:3000]

    async def adversarial_review(self, code_diff: str, plan_md: str) -> dict[str, Any]:
        """Review code for issues the plan didn't anticipate."""
        system = """You are performing an adversarial code review. Find issues the original plan missed.
Return ONLY a JSON object (no markdown, no explanation):
{
    "findings": [
        {"severity": "critical|warning|info", "description": "...", "suggestion": "..."}
    ]
}
Focus on: edge cases, error handling, performance issues, security vulnerabilities."""

        prompt = f"Plan:\n{plan_md[:5000]}\n\nCode diff:\n{code_diff[:10000]}"
        response = await self.message(system, prompt, model="opus")
        return self._extract_json(response, fallback={"findings": []})

    @staticmethod
    def _extract_json(text: str, fallback: dict[str, Any]) -> dict[str, Any]:
        """Extract first JSON object from text, stripping markdown code fences."""
        # Strip markdown code fences (```json ... ```)
        cleaned = text
        if "```" in cleaned:
            import re
            match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", cleaned, re.DOTALL)
            if match:
                cleaned = match.group(1)

        try:
            start = cleaned.find("{")
            end = cleaned.rfind("}") + 1
            if start >= 0 and end > start:
                return json.loads(cleaned[start:end])
        except json.JSONDecodeError:
            logger.warning("Failed to parse JSON from Claude response (len=%d)", len(text))
        return fallback
