from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query

from cogniq.api.schemas import CreateDocumentRequest, DocumentResponse, UpdateDocumentRequest
from cogniq.auth.dependencies import get_current_user
from cogniq.auth.models import User
from cogniq.dependencies import get_db, verify_team_member
from cogniq.domain.issue import Document

router = APIRouter()


@router.get("/teams/{team_id}/projects/{project_id}/documents", response_model=list[DocumentResponse])
async def list_documents(
    team_id: str,
    project_id: str,
    type: str | None = Query(None),
    search: str | None = Query(None),
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    await verify_team_member(team_id, user.id, db)
    query: dict = {"project_id": project_id}
    if type:
        query["type"] = type
    if search:
        query["title"] = {"$regex": search, "$options": "i"}

    cursor = db["documents"].find(query).sort("updated_at", -1)
    return [DocumentResponse.from_db(doc) async for doc in cursor]


@router.post("/teams/{team_id}/projects/{project_id}/documents", response_model=DocumentResponse, status_code=201)
async def create_document(
    team_id: str,
    project_id: str,
    req: CreateDocumentRequest,
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    await verify_team_member(team_id, user.id, db)
    doc = Document(
        type=req.type,
        title=req.title,
        content=req.content,
        issue_id=req.issueId,
        project_id=project_id,
    )
    await db["documents"].insert_one(doc.model_dump(by_alias=True))
    result = await db["documents"].find_one({"_id": doc.id})
    return DocumentResponse.from_db(result)


@router.get("/documents/{document_id}", response_model=DocumentResponse)
async def get_document(
    document_id: str,
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    doc = await db["documents"].find_one({"_id": document_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    return DocumentResponse.from_db(doc)


@router.put("/documents/{document_id}", response_model=DocumentResponse)
async def update_document(
    document_id: str,
    req: UpdateDocumentRequest,
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    updates: dict = {}
    if req.title is not None:
        updates["title"] = req.title
    if req.content is not None:
        updates["content"] = req.content
        updates["version"] = 1  # Will be incremented below
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    updates["updated_at"] = datetime.now(timezone.utc)

    # Increment version if content changed
    if req.content is not None:
        await db["documents"].update_one({"_id": document_id}, {"$inc": {"version": 1}})

    result = await db["documents"].find_one_and_update(
        {"_id": document_id},
        {"$set": {k: v for k, v in updates.items() if k != "version"}},
        return_document=True,
    )
    if not result:
        raise HTTPException(status_code=404, detail="Document not found")
    return DocumentResponse.from_db(result)
