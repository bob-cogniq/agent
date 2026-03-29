import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse

from cogniq.api.schemas import EventResponse
from cogniq.auth.dependencies import get_current_user
from cogniq.auth.models import User
from cogniq.dependencies import get_db, get_issue_repository
from cogniq.domain.issue import Event
from cogniq.domain.enums import EventType
from cogniq.registry.repository import IssueRepository

logger = logging.getLogger(__name__)
router = APIRouter()


def _event_to_response(e: Event) -> EventResponse:
    return EventResponse(
        id=e.event_id,
        runId=e.run_id,
        type=e.type,
        payload=e.payload,
        occurredAt=e.occurred_at.isoformat(),
    )


@router.get("/issues/{issue_id}/events", response_model=list[EventResponse])
async def list_events(
    issue_id: str,
    user: User = Depends(get_current_user),
    repo: IssueRepository = Depends(get_issue_repository),
):
    events = await repo.list_events(issue_id)
    return [_event_to_response(e) for e in events]


@router.post("/issues/{issue_id}/events", response_model=EventResponse, status_code=201)
async def add_event(
    issue_id: str,
    body: dict[str, Any],
    user: User = Depends(get_current_user),
    repo: IssueRepository = Depends(get_issue_repository),
):
    event = Event(
        run_id=body.get("run_id"),
        type=body.get("type", ""),
        payload=body.get("payload", {}),
    )
    await repo.add_event(issue_id, event)
    return _event_to_response(event)


@router.get("/events/stream")
async def event_stream(
    request: Request,
    issue_id: str | None = Query(None),
    project_id: str | None = Query(None),
    db=Depends(get_db),
):
    """SSE stream using MongoDB Change Streams."""

    async def generate():
        try:
            pipeline = []
            if issue_id:
                pipeline.append({"$match": {"fullDocument._id": issue_id}})

            async with db["issues"].watch(
                pipeline,
                full_document="updateLookup",
            ) as stream:
                async for change in stream:
                    if await request.is_disconnected():
                        break

                    full_doc = change.get("fullDocument")
                    if not full_doc:
                        continue

                    # Filter by project if specified
                    if project_id and full_doc.get("project_id") != project_id:
                        continue

                    events = full_doc.get("events", [])
                    if events:
                        latest = events[-1]
                        data = {
                            "issueId": full_doc["_id"],
                            "event": {
                                "type": latest.get("type", ""),
                                "payload": latest.get("payload", {}),
                                "occurredAt": latest.get("occurred_at", ""),
                            },
                            "status": full_doc.get("status", ""),
                        }
                        yield f"data: {json.dumps(data, default=str)}\n\n"

        except asyncio.CancelledError:
            logger.debug("SSE stream cancelled")
        except Exception as e:
            logger.error("SSE stream error: %s", e)
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
