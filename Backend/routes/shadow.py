"""Retired Strategy V2 shadow route compatibility module.

The V2 shadow API was removed. Keeping an empty router allows the legacy
application bootstrap import to remain harmless until the broader V1-era
bootstrap is cleaned up.
"""
from fastapi import APIRouter

router = APIRouter()
