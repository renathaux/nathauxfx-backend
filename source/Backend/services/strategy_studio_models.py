"""SQLAlchemy models for Strategy Studio persistence.

Kept isolated from the legacy monolithic models module so Stage 1 stays
self-contained and cannot accidentally change existing trading models.
"""
from __future__ import annotations

from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, JSON, String

from db import Base


class SavedStrategy(Base):
    __tablename__ = "saved_strategies"

    strategy_id = Column(String(64), primary_key=True)
    owner_id = Column(String(100), nullable=False, index=True)
    name = Column(String(120), nullable=False)
    schema_version = Column(Integer, nullable=False, default=1)
    definition_json = Column(JSON, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("ix_saved_strategy_owner_updated", "owner_id", "updated_at"),
    )


class StrategyStudioSelection(Base):
    __tablename__ = "strategy_studio_selection"

    owner_id = Column(String(100), primary_key=True)
    strategy_id = Column(
        String(64),
        ForeignKey("saved_strategies.strategy_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    activated_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)
