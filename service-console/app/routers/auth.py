"""鉴权路由:单 admin 登录签发 JWT + 当前会话回显。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.auth import issue_token, require_session, verify_login


router = APIRouter(tags=["鉴权"])


class LoginReq(BaseModel):
    username: str
    password: str


@router.post("/auth/login", summary="单 admin 登录", description="校验 env 凭据,成功返回 JWT(放入 Authorization: Bearer)。")
async def login(req: LoginReq) -> dict:
    if not verify_login(req.username, req.password):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")
    return {"token": issue_token(req.username)}


@router.get("/auth/me", summary="当前会话", description="校验 Bearer JWT,回显当前会话 sub。")
async def me(sub: str = Depends(require_session)) -> dict:
    return {"user": sub}
