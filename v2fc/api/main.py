"""
Orchestrateur V2FC — API REST (ARCHITECTURE.md §9) : authentification,
gestion des endpoints (vCenter/ESXi/FusionCompute), inventaire, plans,
relais et jobs de migration (§5, ajoutés le 17/09/2026 — docs/dev/JOURNAL.md,
socle L2 demandé explicitement une fois L1 validé deux fois
sur plateforme réelle).
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import get_settings
from .routers import auth, endpoints, jobs, plans, relays, reports, runner, users

app = FastAPI(title="V2FC — orchestrateur", version="0.1.0")

settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(endpoints.router)
app.include_router(plans.router)
app.include_router(relays.router)
app.include_router(jobs.router)
app.include_router(runner.router)
app.include_router(users.router)
app.include_router(reports.router)


@app.get("/api/v1/health")
async def health() -> dict:
    return {"status": "ok"}
