"""Retired V1 strategy diagnostics route compatibility module."""
from fastapi import APIRouter

# Keep an empty router so the legacy application bootstrap import remains safe.
router = APIRouter()
