"""FastAPI application for Lunch App group dining decision coordinator."""

from __future__ import annotations
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import json
from typing import Any, Dict, List, Optional
from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

from cache_proxy import TwoTierCachingProxy, global_cache_proxy
from database import get_db, init_db
from models import (
    LunchSessionCreate,
    LunchSessionModel,
    LunchSessionRead,
    MemberVoteCreate,
    MemberVoteModel,
    MemberVoteRead,
    PreferenceFilter,
    RecommendationResult,
    RestaurantModel,
    RestaurantRead,
    SessionDecision,
)
from recommendation_engine import RecommendationEngine


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize SQLite database schema
    init_db()
    yield


app = FastAPI(
    title="Lunch App - Group Dining Decision Coordinator",
    description=(
        "Solves the Google Places API cost compromise via a 2-tier caching proxy. "
        "Coordinates team dining decisions with multi-criteria recommendation scoring and quorum voting."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

# Enable CORS for frontend clients
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

engine = RecommendationEngine()


# ==========================================
# Health & Status Endpoints
# ==========================================

@app.get("/", tags=["Health"])
def root():
    return {
        "app": "Lunch App - Group Dining Decision Coordinator",
        "status": "healthy",
        "description": "2-tier caching proxy + group dining quorum coordinator",
    }


# ==========================================
# Places & 2-Tier Caching Proxy Endpoints
# ==========================================

@app.get("/places/search", response_model=List[RestaurantRead], tags=["Places"])
def search_places(
    lat: float = Query(default=37.7749, description="Search anchor latitude"),
    lon: float = Query(default=-122.4194, description="Search anchor longitude"),
    radius_m: int = Query(default=1500, description="Search radius in meters"),
    keyword: Optional[str] = Query(default=None, description="Search keyword e.g. ramen, vegan, tacos"),
    db: Session = Depends(get_db),
):
    """Lightweight nearby search with automatic 2-tier caching.

    Performs lightweight nearby search, stores/updates Tier 2 persistent SQLite cache,
    and returns places with basic metadata (without incurring expensive Place Details calls).
    """
    return global_cache_proxy.search_nearby(
        lat=lat, lon=lon, radius_m=radius_m, keyword=keyword, db=db
    )


@app.get("/places/{place_id}", response_model=RestaurantRead, tags=["Places"])
def get_place_details(
    place_id: str,
    anchor_lat: Optional[float] = Query(default=None, description="Office latitude anchor for distance"),
    anchor_lon: Optional[float] = Query(default=None, description="Office longitude anchor for distance"),
    db: Session = Depends(get_db),
):
    """Get rich place details including editorial description via 2-tier cache.

    If cached in Tier 1 (RAM) or Tier 2 (SQLite), returns immediately at $0 API cost.
    Only if never fetched before does it call the expensive upstream Place Details API,
    caching the result permanently.
    """
    try:
        return global_cache_proxy.get_enriched_details(
            place_id=place_id,
            anchor_lat=anchor_lat,
            anchor_lon=anchor_lon,
            db=db,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Restaurant '{place_id}' not found: {str(exc)}",
        )


@app.get("/cache/stats", tags=["Cache Proxy"])
def get_cache_statistics():
    """Retrieve 2-tier caching proxy metrics and API cost savings."""
    return global_cache_proxy.get_stats()


@app.post("/cache/clear", tags=["Cache Proxy"])
def clear_memory_cache():
    """Clear Tier-1 in-memory cache (Tier-2 SQLite persistent cache remains intact)."""
    size_before = global_cache_proxy.tier1_cache.size()
    global_cache_proxy.tier1_cache.clear()
    return {
        "status": "cleared",
        "tier1_cleared_items": size_before,
        "message": "Tier-1 RAM cache cleared. Tier-2 SQLite persistent cache preserved.",
    }


# ==========================================
# Lunch Session & Voting Endpoints
# ==========================================

@app.post("/sessions", response_model=LunchSessionRead, status_code=status.HTTP_201_CREATED, tags=["Sessions"])
def create_session(
    payload: LunchSessionCreate,
    db: Session = Depends(get_db),
):
    """Create a new lunch voting poll for team coordination."""
    # If no candidate places specified, automatically search nearby
    candidate_ids = payload.candidate_ids
    if not candidate_ids:
        nearby = global_cache_proxy.search_nearby(
            lat=payload.office_lat,
            lon=payload.office_lon,
            radius_m=payload.preference_filters.max_walking_distance_m if payload.preference_filters else 1500,
            db=db,
        )
        candidate_ids = [p.id for p in nearby]

    pref_dict = (
        payload.preference_filters.model_dump()
        if payload.preference_filters
        else PreferenceFilter().model_dump()
    )

    now = datetime.now(timezone.utc)
    expires = now + timedelta(minutes=payload.duration_minutes or 60)

    session_model = LunchSessionModel(
        title=payload.title,
        creator_name=payload.creator_name,
        status="active",
        created_at=now,
        expires_at=expires,
        office_lat=payload.office_lat,
        office_lon=payload.office_lon,
        quorum_needed=payload.quorum_needed,
        candidate_ids=json.dumps(candidate_ids),
        preference_filters=json.dumps(pref_dict),
    )
    db.add(session_model)
    db.commit()
    db.refresh(session_model)

    return LunchSessionRead(
        id=session_model.id,
        title=session_model.title,
        creator_name=session_model.creator_name,
        status=session_model.status,
        created_at=session_model.created_at,
        expires_at=session_model.expires_at,
        office_lat=session_model.office_lat,
        office_lon=session_model.office_lon,
        quorum_needed=session_model.quorum_needed,
        candidate_ids=json.loads(session_model.candidate_ids),
        winning_restaurant_id=session_model.winning_restaurant_id,
        preference_filters=PreferenceFilter(**json.loads(session_model.preference_filters)),
    )


@app.get("/sessions", response_model=List[LunchSessionRead], tags=["Sessions"])
def list_sessions(db: Session = Depends(get_db)):
    """List all lunch sessions."""
    sessions = db.query(LunchSessionModel).order_by(LunchSessionModel.created_at.desc()).all()
    results = []
    for s in sessions:
        results.append(
            LunchSessionRead(
                id=s.id,
                title=s.title,
                creator_name=s.creator_name,
                status=s.status,
                created_at=s.created_at,
                expires_at=s.expires_at,
                office_lat=s.office_lat,
                office_lon=s.office_lon,
                quorum_needed=s.quorum_needed,
                candidate_ids=json.loads(s.candidate_ids) if s.candidate_ids else [],
                winning_restaurant_id=s.winning_restaurant_id,
                preference_filters=PreferenceFilter(**json.loads(s.preference_filters or "{}")),
            )
        )
    return results


@app.get("/sessions/{session_id}", response_model=LunchSessionRead, tags=["Sessions"])
def get_session(session_id: str, db: Session = Depends(get_db)):
    """Get details for a specific session."""
    session = db.query(LunchSessionModel).filter(LunchSessionModel.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Lunch session not found")

    return LunchSessionRead(
        id=session.id,
        title=session.title,
        creator_name=session.creator_name,
        status=session.status,
        created_at=session.created_at,
        expires_at=session.expires_at,
        office_lat=session.office_lat,
        office_lon=session.office_lon,
        quorum_needed=session.quorum_needed,
        candidate_ids=json.loads(session.candidate_ids) if session.candidate_ids else [],
        winning_restaurant_id=session.winning_restaurant_id,
        preference_filters=PreferenceFilter(**json.loads(session.preference_filters or "{}")),
    )


@app.post("/sessions/{session_id}/vote", response_model=MemberVoteRead, tags=["Voting"])
def cast_vote(
    session_id: str,
    payload: MemberVoteCreate,
    db: Session = Depends(get_db),
):
    """Cast or update a team member's vote in a lunch session."""
    session = db.query(LunchSessionModel).filter(LunchSessionModel.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Lunch session not found")

    if session.status != "active":
        raise HTTPException(
            status_code=400,
            detail=f"Cannot vote on session in status '{session.status}'",
        )

    # Check if member already voted for this restaurant in this session
    existing_vote = (
        db.query(MemberVoteModel)
        .filter(
            MemberVoteModel.session_id == session_id,
            MemberVoteModel.member_name == payload.member_name,
            MemberVoteModel.restaurant_id == payload.restaurant_id,
        )
        .first()
    )

    if existing_vote:
        existing_vote.vote_score = payload.vote_score
        existing_vote.comment = payload.comment
        existing_vote.created_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(existing_vote)
        vote_obj = existing_vote
    else:
        new_vote = MemberVoteModel(
            session_id=session_id,
            member_name=payload.member_name,
            restaurant_id=payload.restaurant_id,
            vote_score=payload.vote_score,
            comment=payload.comment,
            created_at=datetime.now(timezone.utc),
        )
        db.add(new_vote)
        db.commit()
        db.refresh(new_vote)
        vote_obj = new_vote

    return MemberVoteRead(
        id=vote_obj.id,
        session_id=vote_obj.session_id,
        member_name=vote_obj.member_name,
        restaurant_id=vote_obj.restaurant_id,
        vote_score=vote_obj.vote_score,
        comment=vote_obj.comment,
        created_at=vote_obj.created_at,
    )


@app.get("/sessions/{session_id}/votes", response_model=List[MemberVoteRead], tags=["Voting"])
def get_session_votes(session_id: str, db: Session = Depends(get_db)):
    """Retrieve all votes cast in this lunch session."""
    session = db.query(LunchSessionModel).filter(LunchSessionModel.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Lunch session not found")

    votes = db.query(MemberVoteModel).filter(MemberVoteModel.session_id == session_id).all()
    return [
        MemberVoteRead(
            id=v.id,
            session_id=v.session_id,
            member_name=v.member_name,
            restaurant_id=v.restaurant_id,
            vote_score=v.vote_score,
            comment=v.comment,
            created_at=v.created_at,
        )
        for v in votes
    ]


@app.get("/sessions/{session_id}/recommendations", response_model=List[RecommendationResult], tags=["Recommendations"])
def get_session_recommendations(session_id: str, db: Session = Depends(get_db)):
    """Compute and rank candidate restaurants based on votes, walking distance, budget, and dietary preferences."""
    session = db.query(LunchSessionModel).filter(LunchSessionModel.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Lunch session not found")

    candidate_ids = json.loads(session.candidate_ids) if session.candidate_ids else []
    if not candidate_ids:
        return []

    # Retrieve candidate restaurant objects (using 2-tier cache)
    candidates: List[RestaurantRead] = []
    for cid in candidate_ids:
        try:
            cand = global_cache_proxy.get_enriched_details(
                place_id=cid,
                anchor_lat=session.office_lat,
                anchor_lon=session.office_lon,
                db=db,
            )
            candidates.append(cand)
        except Exception:
            # Fallback to DB model if upstream error
            cand_db = db.query(RestaurantModel).filter(RestaurantModel.id == cid).first()
            if cand_db:
                candidates.append(cand_db.to_pydantic())

    votes = db.query(MemberVoteModel).filter(MemberVoteModel.session_id == session_id).all()
    vote_reads = [
        MemberVoteRead(
            id=v.id,
            session_id=v.session_id,
            member_name=v.member_name,
            restaurant_id=v.restaurant_id,
            vote_score=v.vote_score,
            comment=v.comment,
            created_at=v.created_at,
        )
        for v in votes
    ]

    preferences = PreferenceFilter(**json.loads(session.preference_filters or "{}"))
    return engine.evaluate(
        candidates=candidates,
        votes=vote_reads,
        preferences=preferences,
        office_lat=session.office_lat,
        office_lon=session.office_lon,
    )


@app.post("/sessions/{session_id}/decide", response_model=SessionDecision, tags=["Sessions"])
def decide_session(
    session_id: str,
    force: bool = Query(default=False, description="Force decision even if quorum is not met"),
    db: Session = Depends(get_db),
):
    """Determine winning venue and close the voting session.

    Enforces quorum verification unless 'force=True' is provided.
    """
    session = db.query(LunchSessionModel).filter(LunchSessionModel.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Lunch session not found")

    votes = db.query(MemberVoteModel).filter(MemberVoteModel.session_id == session_id).all()
    vote_reads = [
        MemberVoteRead(
            id=v.id,
            session_id=v.session_id,
            member_name=v.member_name,
            restaurant_id=v.restaurant_id,
            vote_score=v.vote_score,
            comment=v.comment,
            created_at=v.created_at,
        )
        for v in votes
    ]

    quorum_met, voter_count, _ = engine.check_quorum(
        votes=vote_reads,
        quorum_needed=session.quorum_needed,
    )

    if not quorum_met and not force:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Quorum not met: {voter_count} distinct voters participating, {session.quorum_needed} required.",
        )

    # Compute recommendations
    recommendations = get_session_recommendations(session_id=session_id, db=db)
    if not recommendations:
        raise HTTPException(status_code=400, detail="No candidate restaurants available to decide.")

    winner_result = recommendations[0]
    winner = winner_result.restaurant

    session.status = "decided"
    session.winning_restaurant_id = winner.id
    db.commit()

    return SessionDecision(
        session_id=session.id,
        status="decided",
        quorum_met=quorum_met,
        total_voters=voter_count,
        quorum_needed=session.quorum_needed,
        winning_restaurant=winner,
        ranked_recommendations=recommendations,
    )
