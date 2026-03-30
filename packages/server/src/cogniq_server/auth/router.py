import re
import logging

import jwt as pyjwt
from fastapi import APIRouter, Depends, HTTPException, status

from cogniq_server.api.schemas import (
    AuthResponse,
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    TokenResponse,
    UserResponse,
)
from cogniq_server.auth.jwt import create_access_token, create_refresh_token, decode_token
from cogniq_server.auth.models import UserInDB
from cogniq_server.auth.password import hash_password, verify_password
from cogniq_shared.config import settings
from cogniq_server.dependencies import get_db

logger = logging.getLogger(__name__)
router = APIRouter()

INVITE_CODE_PATTERN = re.compile(r"^[A-Z0-9]{6,12}$")


@router.post("/register", response_model=AuthResponse)
async def register(req: RegisterRequest, db=Depends(get_db)):
    # Validate invite code
    if not INVITE_CODE_PATTERN.match(req.inviteCode):
        raise HTTPException(status_code=400, detail="Invalid invite code format")
    if req.inviteCode not in settings.invite_code_list:
        raise HTTPException(status_code=400, detail="Invalid invite code")

    # Check duplicate email
    existing = await db["users"].find_one({"email": req.email})
    if existing:
        raise HTTPException(status_code=409, detail="Email already registered")

    # Create user
    user = UserInDB(
        name=req.name,
        email=req.email,
        hashed_password=hash_password(req.password),
    )
    await db["users"].insert_one(user.model_dump(by_alias=True))

    access_token = create_access_token(user.id)
    refresh_token = create_refresh_token(user.id)

    return AuthResponse(
        user=UserResponse(id=user.id, name=user.name, email=user.email),
        access_token=access_token,
        refresh_token=refresh_token,
    )


@router.post("/login", response_model=AuthResponse)
async def login(req: LoginRequest, db=Depends(get_db)):
    user_doc = await db["users"].find_one({"email": req.email})
    if not user_doc:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    user = UserInDB.model_validate(user_doc)
    if not verify_password(req.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    access_token = create_access_token(user.id)
    refresh_token = create_refresh_token(user.id)

    return AuthResponse(
        user=UserResponse(id=user.id, name=user.name, email=user.email),
        access_token=access_token,
        refresh_token=refresh_token,
    )


@router.post("/refresh", response_model=TokenResponse)
async def refresh(req: RefreshRequest):
    try:
        payload = decode_token(req.refresh_token)
        if payload.get("type") != "refresh":
            raise HTTPException(status_code=401, detail="Invalid token type")
        user_id = payload.get("sub")
        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid token")
    except pyjwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Refresh token expired")
    except pyjwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid refresh token")

    new_access = create_access_token(user_id)
    return TokenResponse(access_token=new_access)
