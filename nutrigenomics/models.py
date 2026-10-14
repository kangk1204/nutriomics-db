from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Resource(Base):
    __tablename__ = "resource"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    layer: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    priority: Mapped[str | None] = mapped_column(String(8))
    source_url: Mapped[str | None] = mapped_column(Text)
    verification: Mapped[str | None] = mapped_column(Text)
    use_conditions: Mapped[str | None] = mapped_column(Text)
    role_and_limits: Mapped[str | None] = mapped_column(Text)
    release: Mapped[str | None] = mapped_column(String(80))
    sha256: Mapped[str | None] = mapped_column(String(64))
    local_path: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[dict | None] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ResearchStudy(Base):
    __tablename__ = "research_study"

    id: Mapped[str] = mapped_column(String(16), primary_key=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    pubmed_id: Mapped[str | None] = mapped_column(String(32), index=True)
    independent_study: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    metadata_json: Mapped[dict | None] = mapped_column(JSON)
    series: Mapped[list["GeoSeries"]] = relationship(back_populates="study")


class GeoSeries(Base):
    __tablename__ = "geo_series"

    accession: Mapped[str] = mapped_column(String(24), primary_key=True)
    study_id: Mapped[str] = mapped_column(ForeignKey("research_study.id"), index=True)
    parent_accession: Mapped[str | None] = mapped_column(String(24), index=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    assay_type: Mapped[str | None] = mapped_column(Text)
    platform_ids: Mapped[list | None] = mapped_column(JSON)
    organism: Mapped[str | None] = mapped_column(String(120))
    pubmed_id: Mapped[str | None] = mapped_column(String(32), index=True)
    sample_count: Mapped[int | None] = mapped_column(Integer)
    metadata_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    assay_data_downloaded: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    metadata_json: Mapped[dict | None] = mapped_column(JSON)
    study: Mapped[ResearchStudy] = relationship(back_populates="series")
    samples: Mapped[list["GeoSample"]] = relationship(back_populates="series", cascade="all, delete-orphan")
    effects: Mapped[list["StudyEffect"]] = relationship(back_populates="series")


class GeoSample(Base):
    __tablename__ = "geo_sample"
    __table_args__ = (UniqueConstraint("series_accession", "sample_accession"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    series_accession: Mapped[str] = mapped_column(ForeignKey("geo_series.accession"), index=True)
    sample_accession: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    title: Mapped[str | None] = mapped_column(Text)
    characteristics: Mapped[list | None] = mapped_column(JSON)
    metadata_json: Mapped[dict | None] = mapped_column(JSON)
    series: Mapped[GeoSeries] = relationship(back_populates="samples")


class AnalysisRun(Base):
    __tablename__ = "analysis_run"
    __table_args__ = (UniqueConstraint("series_accession", "analysis_version"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    series_accession: Mapped[str] = mapped_column(ForeignKey("geo_series.accession"), index=True)
    analysis_version: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="completed", nullable=False)
    started_at: Mapped[str | None] = mapped_column(String(80))
    source_script_path: Mapped[str | None] = mapped_column(Text)
    source_script_sha256: Mapped[str | None] = mapped_column(String(64))
    environment_lock_path: Mapped[str | None] = mapped_column(Text)
    environment_lock_sha256: Mapped[str | None] = mapped_column(String(64))
    metadata_json: Mapped[dict | None] = mapped_column(JSON)
    input_manifest_json: Mapped[list | None] = mapped_column(JSON)
    output_manifest_json: Mapped[list | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class Food(Base):
    __tablename__ = "food"
    __table_args__ = (UniqueConstraint("source_id", "source_code"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("resource.id"), index=True)
    source_code: Mapped[str] = mapped_column(String(120), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    name_en: Mapped[str | None] = mapped_column(Text)
    aliases: Mapped[list | None] = mapped_column(JSON)
    food_group: Mapped[str | None] = mapped_column(Text)
    edible_state: Mapped[str | None] = mapped_column(String(80))
    preparation_state: Mapped[str | None] = mapped_column(String(120))
    active_release: Mapped[str | None] = mapped_column(String(80))
    composition_mode: Mapped[str] = mapped_column(String(24), default="direct", nullable=False)
    metadata_json: Mapped[dict | None] = mapped_column(JSON)
    compositions: Mapped[list["CompositionMeasurement"]] = relationship(
        back_populates="food", cascade="all, delete-orphan"
    )
    compound_measurements: Mapped[list["FoodCompoundMeasurement"]] = relationship(
        back_populates="food", cascade="all, delete-orphan"
    )
    recipes: Mapped[list["Recipe"]] = relationship(back_populates="food", cascade="all, delete-orphan")


class Component(Base):
    __tablename__ = "component"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    component_key: Mapped[str] = mapped_column(String(160), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    name_en: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(40), default="nutrient", nullable=False)
    canonical_unit: Mapped[str | None] = mapped_column(String(40))
    standard_identifier: Mapped[str | None] = mapped_column(String(160), index=True)
    inchi_key: Mapped[str | None] = mapped_column(String(40), index=True)
    aliases: Mapped[list | None] = mapped_column(JSON)
    metadata_json: Mapped[dict | None] = mapped_column(JSON)


class CompositionMeasurement(Base):
    __tablename__ = "composition_measurement"
    __table_args__ = (
        UniqueConstraint("food_id", "component_id", "source_id", "source_ref", "release"),
        CheckConstraint("amount_per_100g IS NULL OR amount_per_100g >= 0", name="nonnegative_amount"),
        CheckConstraint(
            "status NOT IN ('measured', 'measured_zero') OR (amount_per_100g IS NOT NULL AND unit IS NOT NULL)",
            name="quantified_measurement_has_value_and_unit",
        ),
        CheckConstraint("status != 'measured_zero' OR amount_per_100g = 0", name="measured_zero_is_zero"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    food_id: Mapped[int] = mapped_column(ForeignKey("food.id"), index=True)
    component_id: Mapped[int] = mapped_column(ForeignKey("component.id"), index=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("resource.id"), index=True)
    source_ref: Mapped[str] = mapped_column(String(160), nullable=False)
    release: Mapped[str | None] = mapped_column(String(80))
    amount_per_100g: Mapped[float | None] = mapped_column(Float)
    unit: Mapped[str | None] = mapped_column(String(40))
    basis: Mapped[str] = mapped_column(String(80), default="edible_portion_100g", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="measured", nullable=False)
    n_observations: Mapped[int | None] = mapped_column(Integer)
    standard_error: Mapped[float | None] = mapped_column(Float)
    minimum: Mapped[float | None] = mapped_column(Float)
    maximum: Mapped[float | None] = mapped_column(Float)
    metadata_json: Mapped[dict | None] = mapped_column(JSON)
    food: Mapped[Food] = relationship(back_populates="compositions")
    component: Mapped[Component] = relationship()


class FoodCompoundMeasurement(Base):
    """Source-reported food compound abundance with an explicit, retained basis."""

    __tablename__ = "food_compound_measurement"
    __table_args__ = (
        UniqueConstraint("food_id", "component_id", "source_id", "source_ref", "release"),
        CheckConstraint(
            "amount_per_100g IS NULL OR amount_per_100g >= 0",
            name="nonnegative_compound_amount",
        ),
        CheckConstraint(
            "status != 'quantified' OR "
            "(amount_per_100g IS NOT NULL AND unit IS NOT NULL AND basis = 'source_100g')",
            name="quantified_compound_has_source_100g_basis",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    food_id: Mapped[int] = mapped_column(ForeignKey("food.id"), index=True)
    component_id: Mapped[int] = mapped_column(ForeignKey("component.id"), index=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("resource.id"), index=True)
    source_ref: Mapped[str] = mapped_column(String(160), nullable=False)
    release: Mapped[str | None] = mapped_column(String(80))
    amount_per_100g: Mapped[float | None] = mapped_column(Float)
    unit: Mapped[str | None] = mapped_column(String(40))
    basis: Mapped[str | None] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    minimum: Mapped[float | None] = mapped_column(Float)
    maximum: Mapped[float | None] = mapped_column(Float)
    metadata_json: Mapped[dict | None] = mapped_column(JSON)
    food: Mapped[Food] = relationship(back_populates="compound_measurements")
    component: Mapped[Component] = relationship()
    resource: Mapped[Resource] = relationship()


class Recipe(Base):
    __tablename__ = "recipe"
    __table_args__ = (UniqueConstraint("food_id", "source_ref", "release"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    food_id: Mapped[int] = mapped_column(ForeignKey("food.id"), index=True)
    source_ref: Mapped[str] = mapped_column(String(160), nullable=False)
    release: Mapped[str | None] = mapped_column(String(80))
    yield_weight_g: Mapped[float] = mapped_column(Float, nullable=False)
    source_url: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[dict | None] = mapped_column(JSON)
    food: Mapped[Food] = relationship(back_populates="recipes")
    ingredients: Mapped[list["RecipeIngredient"]] = relationship(
        back_populates="recipe", cascade="all, delete-orphan"
    )


class RecipeIngredient(Base):
    __tablename__ = "recipe_ingredient"
    __table_args__ = (
        CheckConstraint("amount_g > 0", name="positive_recipe_amount"),
        CheckConstraint("retention_factor > 0 AND retention_factor <= 2", name="valid_retention"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    recipe_id: Mapped[int] = mapped_column(ForeignKey("recipe.id"), index=True)
    food_id: Mapped[int] = mapped_column(ForeignKey("food.id"), index=True)
    amount_g: Mapped[float] = mapped_column(Float, nullable=False)
    retention_factor: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    metadata_json: Mapped[dict | None] = mapped_column(JSON)
    recipe: Mapped[Recipe] = relationship(back_populates="ingredients")
    food: Mapped[Food] = relationship()


class KnowledgeLink(Base):
    __tablename__ = "knowledge_link"
    __table_args__ = (
        UniqueConstraint("source", "source_ref", "subject_type", "subject_id", "relation", "target_type", "target_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    source_ref: Mapped[str] = mapped_column(String(160), nullable=False)
    subject_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    subject_id: Mapped[str] = mapped_column(String(180), nullable=False, index=True)
    relation: Mapped[str] = mapped_column(String(80), nullable=False)
    target_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    target_id: Mapped[str] = mapped_column(String(180), nullable=False, index=True)
    evidence_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    organism: Mapped[str | None] = mapped_column(String(120))
    tissue: Mapped[str | None] = mapped_column(String(160))
    dose: Mapped[str | None] = mapped_column(String(160))
    duration: Mapped[str | None] = mapped_column(String(160))
    pubmed_id: Mapped[str | None] = mapped_column(String(32), index=True)
    resource_id: Mapped[str | None] = mapped_column(ForeignKey("resource.id"))
    metadata_json: Mapped[dict | None] = mapped_column(JSON)


class StudyEffect(Base):
    __tablename__ = "study_effect"
    __table_args__ = (
        UniqueConstraint("series_accession", "feature_type", "feature_id", "contrast", "analysis_version"),
        Index(
            "ix_study_effect_series_type_q_id",
            "series_accession",
            "feature_type",
            "q_value",
            "id",
        ),
        Index("ix_study_effect_context_selector", "series_accession", "analysis_version",
              "contrast", "feature_type", "feature_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    series_accession: Mapped[str] = mapped_column(ForeignKey("geo_series.accession"), index=True)
    feature_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    feature_id: Mapped[str] = mapped_column(String(180), nullable=False, index=True)
    feature_label: Mapped[str | None] = mapped_column(Text)
    contrast: Mapped[str] = mapped_column(String(240), nullable=False)
    tissue: Mapped[str | None] = mapped_column(String(160))
    timepoint: Mapped[str | None] = mapped_column(String(120))
    intervention: Mapped[str | None] = mapped_column(Text)
    dose: Mapped[str | None] = mapped_column(String(160))
    duration: Mapped[str | None] = mapped_column(String(160))
    effect_estimate: Mapped[float | None] = mapped_column(Float)
    standard_error: Mapped[float | None] = mapped_column(Float)
    p_value: Mapped[float | None] = mapped_column(Float)
    q_value: Mapped[float | None] = mapped_column(Float)
    effect_unit: Mapped[str | None] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(32), default="measured", nullable=False)
    n_people: Mapped[int | None] = mapped_column(Integer)
    n_samples: Mapped[int | None] = mapped_column(Integer)
    analysis_version: Mapped[str] = mapped_column(String(80), nullable=False)
    source_file: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[dict | None] = mapped_column(JSON)
    series: Mapped[GeoSeries] = relationship(back_populates="effects")


class MealRecord(Base):
    __tablename__ = "meal_record"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    consumed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    note: Mapped[str | None] = mapped_column(Text)
    calculation_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    items: Mapped[list["MealItem"]] = relationship(back_populates="meal", cascade="all, delete-orphan")


class MealItem(Base):
    __tablename__ = "meal_item"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    meal_id: Mapped[int] = mapped_column(ForeignKey("meal_record.id"), index=True)
    food_id: Mapped[int] = mapped_column(ForeignKey("food.id"), index=True)
    amount_g: Mapped[float] = mapped_column(Float, nullable=False)
    name_snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    preparation_snapshot: Mapped[str | None] = mapped_column(String(120))
    meal: Mapped[MealRecord] = relationship(back_populates="items")
    food: Mapped[Food] = relationship()


# Register the additive bridge with the same metadata and migration boundary.
from .bridge_models import (  # noqa: E402,F401
    BridgeSource, EvidenceAssertion, Exposure, InterventionArm, StudyContrast,
)

