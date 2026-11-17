from fastapi import APIRouter

from app.routers import accounts, dashboard, entries, netting, quarantine, settle, verify

api = APIRouter(prefix="/api")
api.include_router(dashboard.router)
api.include_router(accounts.router)
api.include_router(entries.router)
api.include_router(settle.router)
api.include_router(quarantine.router)
api.include_router(verify.router)
api.include_router(netting.router)
