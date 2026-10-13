"""Authenticated routes for the shared manual task board."""

from typing import Any, Literal
from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field
from src.pixel_agents.office import OfficeManager, office_action


class OfficeActionRequest(BaseModel):
    action: Literal["create", "assign", "complete", "rank"]
    arguments: dict[str, Any] = Field(default_factory=dict)


def register_pixel_office_routes(app: FastAPI, read_auth, write_auth) -> None:
    """Mount read and mutation routes with the normal local/settings guards."""

    @app.get("/api/pixel-office", dependencies=[Depends(read_auth)])
    def dashboard():
        return OfficeManager().dashboard()

    @app.post("/api/pixel-office/actions", dependencies=[Depends(write_auth)])
    def update(payload: OfficeActionRequest):
        try:
            return office_action(OfficeManager(), payload.action, payload.arguments)
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
