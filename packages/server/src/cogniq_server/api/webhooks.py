import hashlib
import hmac
import logging
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request

from cogniq_shared.config import settings

logger = logging.getLogger(__name__)
router = APIRouter()


def _verify_signature(payload: bytes, signature: str, secret: str) -> bool:
    if not secret:
        return True  # Skip verification if no secret configured
    expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


@router.post("/linear")
async def linear_webhook(
    request: Request,
    x_linear_signature: str | None = Header(None),
):
    body = await request.body()

    if settings.linear_webhook_secret and x_linear_signature:
        if not _verify_signature(body, x_linear_signature, settings.linear_webhook_secret):
            raise HTTPException(status_code=401, detail="Invalid signature")

    payload = await request.json()
    action = payload.get("action", "")
    data = payload.get("data", {})

    logger.info("Linear webhook: action=%s, issue=%s", action, data.get("id", ""))

    # Dispatch to orchestrator engine
    try:
        from cogniq_server.dependencies import get_engine
        engine = get_engine()
        issue_id = data.get("id", "")
        if action == "create" and issue_id:
            await engine.handle_event("issue_created", issue_id, data)
        elif action == "update" and issue_id:
            state = data.get("state", {}).get("name", "")
            if state.lower() in ("approved", "plan approved"):
                await engine.handle_event("gate1_approved", issue_id, data)
    except RuntimeError:
        pass  # Engine not initialized
    except Exception as e:
        logger.warning("Linear webhook engine dispatch failed: %s", e)

    return {"received": True, "action": action}


@router.post("/github")
async def github_webhook(
    request: Request,
    x_hub_signature_256: str | None = Header(None),
):
    body = await request.body()

    if settings.github_webhook_secret and x_hub_signature_256:
        sig = x_hub_signature_256.replace("sha256=", "")
        if not _verify_signature(body, sig, settings.github_webhook_secret):
            raise HTTPException(status_code=401, detail="Invalid signature")

    payload = await request.json()
    event_type = request.headers.get("X-GitHub-Event", "")

    logger.info("GitHub webhook: event=%s, action=%s", event_type, payload.get("action", ""))

    # Dispatch to orchestrator engine
    try:
        from cogniq_server.dependencies import get_engine
        engine = get_engine()
        if event_type == "pull_request" and payload.get("action") == "closed":
            pr = payload.get("pull_request", {})
            if pr.get("merged"):
                # Extract issue ID from PR title (e.g. "COG-42: ...")
                title = pr.get("title", "")
                issue_id = title.split(":")[0].strip() if ":" in title else ""
                if issue_id:
                    await engine.handle_event("gate2_merged", issue_id, {
                        "pr_url": pr.get("html_url", ""),
                        "merged_by": pr.get("merged_by", {}).get("login", ""),
                    })
    except RuntimeError:
        pass
    except Exception as e:
        logger.warning("GitHub webhook engine dispatch failed: %s", e)

    return {"received": True, "event": event_type}
