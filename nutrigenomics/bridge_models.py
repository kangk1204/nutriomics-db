"""Additive, versioned intervention bridge; large effect matrices remain in StudyEffect."""
from __future__ import annotations

from sqlalchemy import JSON, Boolean, CheckConstraint, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class BridgeSource(Base):
    __tablename__ = "ng_bridge_source"
    id: Mapped[str] = mapped_column(String(160), primary_key=True)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[str] = mapped_column(String(120), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_location: Mapped[str] = mapped_column(Text, nullable=False)
    license: Mapped[str] = mapped_column(Text, nullable=False)
    export_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    metadata_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)


class Exposure(Base):
    __tablename__ = "ng_exposure"
    __table_args__ = (
        CheckConstraint("amount IS NULL OR amount >= 0"),
        CheckConstraint("duration_days IS NULL OR duration_days >= 0"),
        CheckConstraint("(amount IS NULL AND unit IS NULL) OR (amount IS NOT NULL AND unit IS NOT NULL)"),
    )
    id: Mapped[str] = mapped_column(String(160), primary_key=True)
    study_id: Mapped[str] = mapped_column(ForeignKey("research_study.id"), index=True)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    formulation: Mapped[str] = mapped_column(Text, nullable=False)
    amount: Mapped[float | None] = mapped_column(Float)
    unit: Mapped[str | None] = mapped_column(String(40))
    dose_basis: Mapped[str] = mapped_column(Text, nullable=False)
    duration_days: Mapped[float | None] = mapped_column(Float)
    component_keys: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    food_terms: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    source_id: Mapped[str] = mapped_column(ForeignKey("ng_bridge_source.id"))
    metadata_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)


class InterventionArm(Base):
    __tablename__ = "ng_arm"
    id: Mapped[str] = mapped_column(String(160), primary_key=True)
    study_id: Mapped[str] = mapped_column(ForeignKey("research_study.id"), index=True)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    allocation: Mapped[str] = mapped_column(String(60), nullable=False)
    exposure_ids: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    n_people: Mapped[int | None] = mapped_column(Integer)
    metadata_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)


class StudyContrast(Base):
    __tablename__ = "ng_contrast"
    __table_args__ = (
        CheckConstraint("response_time_h IS NULL OR response_time_h >= 0"),
        CheckConstraint("n_people IS NULL OR n_people > 0"),
    )
    id: Mapped[str] = mapped_column(String(220), primary_key=True)
    study_id: Mapped[str] = mapped_column(ForeignKey("research_study.id"), index=True)
    series_accession: Mapped[str] = mapped_column(ForeignKey("geo_series.accession"), index=True)
    contrast: Mapped[str] = mapped_column(String(240), nullable=False, index=True)
    analysis_version: Mapped[str | None] = mapped_column(String(80))
    comparison: Mapped[str] = mapped_column(Text, nullable=False)
    design: Mapped[str] = mapped_column(String(60), nullable=False)
    tissue: Mapped[str] = mapped_column(String(160), nullable=False)
    response_time_h: Mapped[float | None] = mapped_column(Float)
    exposure_ids: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    arm_ids: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    n_people: Mapped[int | None] = mapped_column(Integer)
    n_samples: Mapped[int | None] = mapped_column(Integer)
    analysis_status: Mapped[str] = mapped_column(String(40), nullable=False)
    metadata_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)


class EvidenceAssertion(Base):
    __tablename__ = "ng_assertion"
    id: Mapped[str] = mapped_column(String(240), primary_key=True)
    study_id: Mapped[str] = mapped_column(ForeignKey("research_study.id"), index=True)
    exposure_id: Mapped[str] = mapped_column(ForeignKey("ng_exposure.id"), index=True)
    contrast_id: Mapped[str | None] = mapped_column(ForeignKey("ng_contrast.id"), index=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("ng_bridge_source.id"))
    layer: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    assertion: Mapped[str] = mapped_column(Text, nullable=False)
    review_status: Mapped[str] = mapped_column(String(40), nullable=False)
    organism: Mapped[str] = mapped_column(String(120), nullable=False)
    inference: Mapped[str] = mapped_column(String(40), nullable=False)
    limitations: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    metadata_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
