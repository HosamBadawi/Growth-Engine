"""Pipeline-stage pages: Prospects / Drafts / In Sequence / Replies / Closed.

Each prospect appears on exactly one page (stage derived at read time in
engine/stages.py). Replies is the landing page whenever it is non-empty.
"""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from app.auth import require_auth
from app.routes.dashboard import templates
from db.session import new_session
from engine.stages import