"""Data models for Lunch App: Pydantic schemas and SQLAlchemy SQLite models."""

from __future__ import annotations
from datetime import datetime, timezone
import json
import uuid
from typing import Any, List, Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import relationship

from database import Base


# ==========================================
# SQLAlchemy SQLite Models
# ==========================================

class RestaurantModel(Base):
    """SQLite model for cached restaurant details and rich metadata."""
    __tablename__ = "restaurants"

    id = Column(String(128), primary_key=True, index=True)
    name = Column(String(255), nullable=False, index=True)
    address = Column(String(512), nullable=True)
    latitude = Column(Float, nullable=False)
    longitude = Column(Float, nullable=False)
    rating = Column(Float, default=0.0)
    user_ratings_total = Column(Integer, default=0)
    price_level = Column(Integer, default=2)  # 1: $, 2: $$, 3: $$$, 4: $$$$
    walking_distance_m = Column(Integer, default=0)
    walking_time_mins = Column(Integer, default=0)
    maps_url = Column(String(512), nullable=True)
    photo_urls = Column(Text, default="[]")  # JSON encoded list of URLs
    description = Column(Text, nullable=True)  # Editorial summary / rich description
    dietary_tags = Column(Text, default="[]")  # JSON encoded list of tags
    cuisines = Column(Text, default="[]")  # JSON encoded list of cuisines
    is_open_now = Column(Boolean, default=True)
    details_cached = Column(Boolean, default=False)  # True if expensive Tier-2 details populated
    cached_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    cache_expiry = Column(DateTime, nullable=True)

    def to_pydantic(self) -> RestaurantRead:
        """Convert ORM model to Pydantic RestaurantRead."""
        photos = json.loads(self.photo_urls) if self.photo_urls else []
        dietary = json.loads(self.dietary_tags) if self.dietary_tags else []
        cuisine_list = json.loads(self.cuisines) if self.cuisines else []

        return RestaurantRead(
            id=self.id,
            name=self.name,
            address=self.address,
            latitude=self.latitude,
            longitude=self.longitude,
            rating=self.rating or 0.0,
            user_ratings_total=self.user_ratings_total or 0,
            price_level=self.price_level or 2,
            walking_distance_m=self.walking_distance_m or 0,
            walking_time_mins=self.walking_time_mins or 0,
            maps_url=self.maps_url or "",
            photo_urls=photos,
            description=self.description,
            dietary_tags=dietary,
            cuisines=cuisine_list,
            is_open_now=self.is_open_now,
            details_cached=self.details_cached,
            cached_at=self.cached_at,
        )


class LunchSessionModel(Base):
    """SQLite model for group dining voting sessions."""
    __tablename__ = "lunch_sessions"

    id = Column(String(64), primary_key=True, default=lambda: str(uuid.uuid4()))
    title = Column(String(255), nullable=False)
    creator_name = Column(String(128), nullable=False)
    status = Column(String(32), default="active")  # active, decided, expired
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    expires_at = Column(DateTime, nullable=True)
    office_lat = Column(Float, default=37.7749)  # Default center / office anchor
    office_lon = Column(Float, default=-122.4194)
    quorum_needed = Column(Integer, default=3)
    candidate_ids = Column(Text, default="[]")  # JSON encoded list of restaurant IDs
    winning_restaurant_id = Column(String(128), ForeignKey("restaurants.id"), nullable=True)
    preference_filters = Column(Text, default="{}")  # JSON encoded PreferenceFilter

    votes = relationship("MemberVoteModel", back_populates="session", cascade="all, delete-orphan")


class MemberVoteModel(Base):
    """SQLite model for individual team member votes."""
    __tablename__ = "member_votes"

    id = Column(String(64), primary_key=True, default=lambda: str(uuid.uuid4()))
    session_id = Column(String(64), ForeignKey("lunch_sessions.id"), nullable=False, index=True)
    member_name = Column(String(128), nullable=False)
    restaurant_id = Column(String(128), ForeignKey("restaurants.id"), nullable=False, index=True)
    vote_score = Column(Float, nullable=False, default=3.0)  # 1.0 to 5.0 scale
    comment = Column(Text, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    session = relationship("LunchSessionModel", back_populates="votes")


class PreferenceFilterModel(Base):
    """SQLite model for saved or session-bound team preference filters."""
    __tablename__ = "preference_filters"

    id = Column(String(64), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(128), nullable=True)
    max_walking_minutes = Column(Integer, default=15)
    max_walking_distance_m = Column(Integer, default=1200)
    price_levels = Column(Text, default="[1, 2]")  # JSON list e.g. [1, 2]
    dietary_tags = Column(Text, default="[]")  # JSON list e.g. ["vegetarian"]
    min_rating = Column(Float, default=3.5)
    require_open_now = Column(Boolean, default=True)


# ==========================================
# Pydantic Schemas
# ==========================================

class PreferenceFilter(BaseModel):
    """Pydantic model for filtering dining choices."""
    max_walking_minutes: Optional[int] = Field(default=15, description="Max walking time in minutes")
    max_walking_distance_m: Optional[int] = Field(default=1200, description="Max walking distance in meters")
    price_levels: Optional[List[int]] = Field(default_factory=lambda: [1, 2], description="Allowed price levels (1-4)")
    dietary_tags: Optional[List[str]] = Field(default_factory=list, description="Required dietary tags (e.g. vegetarian, halal)")
    min_rating: Optional[float] = Field(default=3.5, description="Minimum Google rating required")
    require_open_now: bool = Field(default=True, description="Only consider places open now")

    @field_validator("dietary_tags", mode="before")
    @classmethod
    def normalize_dietary_tags(cls, v: Any) -> List[str]:
        if isinstance(v, list):
            return [t.lower().strip() for t in v if isinstance(t, str)]
        return []


class RestaurantBase(BaseModel):
    """Base Pydantic attributes for a restaurant/place."""
    name: str
    address: Optional[str] = None
    latitude: float
    longitude: float
    rating: float = Field(default=0.0, ge=0.0, le=5.0)
    user_ratings_total: int = Field(default=0, ge=0)
    price_level: int = Field(default=2, ge=1, le=4)
    walking_distance_m: int = Field(default=0, ge=0)
    walking_time_mins: int = Field(default=0, ge=0)
    maps_url: Optional[str] = ""
    photo_urls: List[str] = Field(default_factory=list)
    description: Optional[str] = Field(default=None, description="Rich editorial summary from Tier-2 details cache")
    dietary_tags: List[str] = Field(default_factory=list)
    cuisines: List[str] = Field(default_factory=list)
    is_open_now: bool = True
    details_cached: bool = Field(default=False, description="Whether expensive details are cached")


class RestaurantCreate(RestaurantBase):
    id: str


class RestaurantRead(RestaurantBase):
    id: str
    cached_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class MemberVoteCreate(BaseModel):
    """Payload to cast a vote."""
    member_name: str = Field(..., min_length=1, description="Name or identifier of the voting team member")
    restaurant_id: str = Field(..., description="Target place / restaurant ID")
    vote_score: float = Field(..., ge=1.0, le=5.0, description="Score between 1.0 (strongly dislike) and 5.0 (love it)")
    comment: Optional[str] = Field(default=None, max_length=256, description="Optional note or rationale")


class MemberVoteRead(BaseModel):
    id: str
    session_id: str
    member_name: str
    restaurant_id: str
    vote_score: float
    comment: Optional[str] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class LunchSessionCreate(BaseModel):
    """Payload to initialize a new lunch poll."""
    title: str = Field(..., min_length=2, max_length=200, description="Session title, e.g. 'Friday Team Lunch'")
    creator_name: str = Field(..., min_length=1, max_length=100, description="Name of initiator")
    office_lat: float = Field(default=37.7749, description="Office latitude anchor for walking calculation")
    office_lon: float = Field(default=-122.4194, description="Office longitude anchor")
    quorum_needed: int = Field(default=3, ge=1, description="Minimum distinct voters needed to declare winner")
    candidate_ids: List[str] = Field(default_factory=list, description="Initial restaurant place IDs in poll")
    preference_filters: Optional[PreferenceFilter] = None
    duration_minutes: Optional[int] = Field(default=60, description="Poll duration before expiry")


class LunchSessionRead(BaseModel):
    id: str
    title: str
    creator_name: str
    status: str
    created_at: datetime
    expires_at: Optional[datetime] = None
    office_lat: float
    office_lon: float
    quorum_needed: int
    candidate_ids: List[str]
    winning_restaurant_id: Optional[str] = None
    preference_filters: PreferenceFilter

    model_config = ConfigDict(from_attributes=True)


class RecommendationResult(BaseModel):
    """Scored venue with component breakdown for transparency."""
    restaurant: RestaurantRead
    total_score: float
    vote_score: float
    distance_score: float
    price_score: float
    dietary_score: float
    rating_score: float
    reasons: List[str]


class SessionDecision(BaseModel):
    """Outcome of deciding a lunch session."""
    session_id: str
    status: str
    quorum_met: bool
    total_voters: int
    quorum_needed: int
    winning_restaurant: Optional[RestaurantRead] = None
    ranked_recommendations: List[RecommendationResult] = Field(default_factory=list)
