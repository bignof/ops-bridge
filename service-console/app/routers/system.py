from __future__ import annotations

from fastapi import APIRouter


router = APIRouter(tags=["系统"])


@router.get("/health", summary="健康检查", description="探活端点,供网关 / 编排健康检查使用。")
async def health() -> dict:
    return {"status": "ok"}
