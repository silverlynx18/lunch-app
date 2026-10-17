"""Comprehensive test suite for Lunch App.

Tests cover:
1. Recommendation engine scoring (walking radius, price filter, dietary tags, vote aggregation, veto).
2. Two-tier caching proxy (Tier 1 RAM, Tier 2 SQLite, upstream call avoidance, dollar savings metrics).
3. Voting quorum and session lifecycle (quorum enforcement, vote casting, winner finalization).
4. Full FastAPI integration endpoints.
"""

from datetime import datetime, timezone
import os
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# Setup test DB environment before imports
TEST_DB_URL = "sqlite:///:memory:"
test_engine = create_engine(
    TEST_DB_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)

import database
database.engine = test_engine
database.SessionLocal = TestingSessionLocal
from database import Base, get_db

from api import app
from cache_proxy import MockPlacesProvider, TwoTierCachingProxy, calculate_haversine_distance, global_cache_proxy
from models import (
    MemberVoteRead,
    PreferenceFilter,
    RestaurantModel,
    RestaurantRead,
)
from recommendation_engine import (
    RecommendationEngine,
    score_dietary_match,
    score_price_level,
    score_vote_consensus,
    score_walking_distance,
)


@pytest.fixture(autouse=True)
def setup_and_teardown_db():
    """Create fresh tables for every test."""
    Base.metadata.create_all(bind=test_engine)
    global_cache_proxy.tier1_cache.clear()
    global_cache_proxy.provider = MockPlacesProvider()
    global_cache_proxy.stats = {
        "tier1_hits": 0,
        "tier2_hits": 0,
        "nearby_searches_upstream": 0,
        "place_details_calls_upstream": 0,
        "place_details_calls_saved": 0,
    }
    yield
    Base.metadata.drop_all(bind=test_engine)


@pytest.fixture
def db_session():
    """Provide isolated db session for tests."""
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client(db_session):
    """FastAPI test client with DB override."""
    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


# ============================================================================
# 1. Recommendation Engine Unit Tests
# ============================================================================

def test_score_walking_distance():
    """Test walking distance scores and penalties."""
    pref = PreferenceFilter(max_walking_distance_m=1000, max_walking_minutes=15)
    office_lat, office_lon = 37.7749, -122.4194

    # 1. Very close venue (~100m away)
    close_venue = RestaurantRead(
        id="v1",
        name="Close Cafe",
        latitude=37.7755,
        longitude=-122.4190,
        rating=4.5,
        price_level=1,
    )
    score, dist_m, walk_mins, reasons = score_walking_distance(close_venue, office_lat, office_lon, pref)
    assert score == 25.0
    assert dist_m < 300
    assert walk_mins >= 1

    # 2. Far venue (>1500m away, exceeds 1000m max)
    far_venue = RestaurantRead(
        id="v2",
        name="Far Away Dining",
        latitude=37.8000,
        longitude=-122.4100,
        rating=4.5,
        price_level=1,
    )
    score_far, dist_m_far, _, reasons_far = score_walking_distance(far_venue, office_lat, office_lon, pref)
    assert dist_m_far > 1000
    assert score_far < 5.0
    assert any("Exceeds" in r for r in reasons_far)


def test_score_price_level():
    """Test budget matching scores."""
    pref = PreferenceFilter(price_levels=[1, 2])

    cheap_venue = RestaurantRead(id="p1", name="Cheap Eats", latitude=0, longitude=0, price_level=1)
    moderate_venue = RestaurantRead(id="p2", name="Mid Eats", latitude=0, longitude=0, price_level=2)
    expensive_venue = RestaurantRead(id="p3", name="Fancy Bistro", latitude=0, longitude=0, price_level=3)
    ultra_venue = RestaurantRead(id="p4", name="Ultra Fancy", latitude=0, longitude=0, price_level=4)

    s1, _ = score_price_level(cheap_venue, pref)
    s2, _ = score_price_level(moderate_venue, pref)
    s3, _ = score_price_level(expensive_venue, pref)
    s4, _ = score_price_level(ultra_venue, pref)

    assert s1 == 15.0
    assert s2 == 15.0
    assert s3 == 7.0  # Slightly above
    assert s4 == 2.0  # Above team budget


def test_score_dietary_match():
    """Test dietary tags scoring."""
    pref = PreferenceFilter(dietary_tags=["vegetarian", "gluten-free"])

    fully_matching = RestaurantRead(
        id="d1", name="Healthy Bowls", latitude=0, longitude=0,
        dietary_tags=["vegetarian", "gluten-free", "vegan"]
    )
    partially_matching = RestaurantRead(
        id="d2", name="Pizza Place", latitude=0, longitude=0,
        dietary_tags=["vegetarian"]
    )
    non_matching = RestaurantRead(
        id="d3", name="Steakhouse", latitude=0, longitude=0,
        dietary_tags=[]
    )

    s_full, r_full = score_dietary_match(fully_matching, pref)
    s_part, r_part = score_dietary_match(partially_matching, pref)
    s_none, r_none = score_dietary_match(non_matching, pref)

    assert s_full == 10.0
    assert any("Fully" in r for r in r_full)
    assert s_part == 5.0
    assert s_none == 0.0


def test_score_vote_consensus_and_veto():
    """Test vote aggregation, participation bonus, and veto penalty."""
    r_id = "test_rest"

    # Positive consensus with multiple voters
    votes = [
        MemberVoteRead(id="1", session_id="s1", member_name="Alice", restaurant_id=r_id, vote_score=5.0, created_at=datetime.now(timezone.utc)),
        MemberVoteRead(id="2", session_id="s1", member_name="Bob", restaurant_id=r_id, vote_score=4.5, created_at=datetime.now(timezone.utc)),
        MemberVoteRead(id="3", session_id="s1", member_name="Charlie", restaurant_id=r_id, vote_score=4.5, created_at=datetime.now(timezone.utc)),
    ]
    score, reasons = score_vote_consensus(r_id, votes)
    assert score > 30.0
    assert any("Strong team consensus" in r for r in reasons)

    # Veto test: One voter gives 1.0
    veto_votes = [
        MemberVoteRead(id="1", session_id="s1", member_name="Alice", restaurant_id=r_id, vote_score=5.0, created_at=datetime.now(timezone.utc)),
        MemberVoteRead(id="2", session_id="s1", member_name="Bob", restaurant_id=r_id, vote_score=1.0, created_at=datetime.now(timezone.utc)),
    ]
    veto_score, veto_reasons = score_vote_consensus(r_id, veto_votes)
    assert any("Veto warning" in r for r in veto_reasons)
    # Ensure penalty reduced the score significantly
    assert veto_score < score


def test_recommendation_engine_ranking():
    """Test complete recommendation engine ranking output."""
    engine = RecommendationEngine()
    pref = PreferenceFilter(
        max_walking_distance_m=1200,
        price_levels=[1, 2],
        dietary_tags=["vegetarian"],
    )

    r1 = RestaurantRead(
        id="r1", name="Awesome Burrito", latitude=37.7760, longitude=-122.4180,
        rating=4.8, user_ratings_total=300, price_level=1,
        dietary_tags=["vegetarian", "vegan"], is_open_now=True
    )
    r2 = RestaurantRead(
        id="r2", name="Far Steakhouse", latitude=37.7990, longitude=-122.4010,
        rating=4.2, user_ratings_total=80, price_level=4,
        dietary_tags=[], is_open_now=True
    )

    votes = [
        MemberVoteRead(id="v1", session_id="s", member_name="Owen", restaurant_id="r1", vote_score=5.0, created_at=datetime.now(timezone.utc)),
        MemberVoteRead(id="v2", session_id="s", member_name="Lisa", restaurant_id="r1", vote_score=4.5, created_at=datetime.now(timezone.utc)),
    ]

    results = engine.evaluate(candidates=[r1, r2], votes=votes, preferences=pref)
    assert len(results) == 2
    assert results[0].restaurant.id == "r1"
    assert results[0].total_score > results[1].total_score
    assert results[0].vote_score > results[1].vote_score
    assert results[0].distance_score > results[1].distance_score


# ============================================================================
# 2. Two-Tier Caching Proxy Tests (Solving Places API Cost Compromise)
# ============================================================================

def test_two_tier_caching_proxy_behavior(db_session):
    """Verify Tier 1 RAM hit, Tier 2 SQLite hit, and upstream call savings."""
    mock_provider = MockPlacesProvider()
    proxy = TwoTierCachingProxy(upstream_provider=mock_provider)

    place_id = "place_mission_burrito"

    # Step 1: Nearby search should populate Tier 2 DB with basic info without fetching expensive details
    nearby = proxy.search_nearby(lat=37.7749, lon=-122.4194, radius_m=2000, db=db_session)
    assert len(nearby) > 0
    assert proxy.stats["nearby_searches_upstream"] == 1
    assert proxy.stats["place_details_calls_upstream"] == 0  # 0 expensive calls made!

    # Verify place in DB currently has details_cached=False and description=None
    db_place = db_session.query(RestaurantModel).filter(RestaurantModel.id == place_id).first()
    assert db_place is not None
    assert db_place.details_cached is False
    assert db_place.description is None

    # Step 2: First explicit request for place details triggers upstream call
    enriched = proxy.get_enriched_details(place_id=place_id, db=db_session)
    assert enriched.details_cached is True
    assert enriched.description is not None
    assert "burritos" in enriched.description.lower()
    assert proxy.stats["place_details_calls_upstream"] == 1
    assert proxy.stats["place_details_calls_saved"] == 0

    # Step 3: Second request for place details should hit Tier 1 memory cache (0 upstream calls)
    cached_tier1 = proxy.get_enriched_details(place_id=place_id, db=db_session)
    assert cached_tier1.description == enriched.description
    assert proxy.stats["place_details_calls_upstream"] == 1  # Still 1!
    assert proxy.stats["tier1_hits"] == 1
    assert proxy.stats["place_details_calls_saved"] == 1

    # Step 4: Clear Tier 1 RAM cache and request again -> should hit Tier 2 SQLite persistent cache
    proxy.tier1_cache.clear()
    assert proxy.tier1_cache.size() == 0

    cached_tier2 = proxy.get_enriched_details(place_id=place_id, db=db_session)
    assert cached_tier2.description == enriched.description
    assert proxy.stats["place_details_calls_upstream"] == 1  # Still 1!
    assert proxy.stats["tier2_hits"] == 1
    assert proxy.stats["place_details_calls_saved"] == 2

    # Step 5: Check cost metrics
    stats = proxy.get_stats()
    assert stats["place_details_calls_saved"] == 2
    assert stats["estimated_dollars_saved"] > 0.0


# ============================================================================
# 3. Quorum & Session Lifecycle Tests
# ============================================================================

def test_quorum_verification():
    """Test quorum checking with distinct voters."""
    engine = RecommendationEngine()
    quorum_needed = 3

    votes_1 = [
        MemberVoteRead(id="1", session_id="s", member_name="Alice", restaurant_id="r1", vote_score=5.0, created_at=datetime.now(timezone.utc)),
    ]
    met, count, _ = engine.check_quorum(votes_1, quorum_needed)
    assert met is False
    assert count == 1

    # Duplicate vote by same member shouldn't count as 2 distinct voters
    votes_2 = votes_1 + [
        MemberVoteRead(id="2", session_id="s", member_name="Alice", restaurant_id="r2", vote_score=4.0, created_at=datetime.now(timezone.utc)),
        MemberVoteRead(id="3", session_id="s", member_name="Bob", restaurant_id="r1", vote_score=4.0, created_at=datetime.now(timezone.utc)),
    ]
    met, count, _ = engine.check_quorum(votes_2, quorum_needed)
    assert met is False
    assert count == 2

    # 3 distinct members meet quorum
    votes_3 = votes_2 + [
        MemberVoteRead(id="4", session_id="s", member_name="Charlie", restaurant_id="r1", vote_score=5.0, created_at=datetime.now(timezone.utc)),
    ]
    met, count, _ = engine.check_quorum(votes_3, quorum_needed)
    assert met is True
    assert count == 3


# ============================================================================
# 4. FastAPI Endpoints Integration Tests
# ============================================================================

def test_api_full_flow(client):
    """End-to-end integration test: search -> session -> vote -> recommendations -> decide."""
    # 1. Search places
    search_resp = client.get("/places/search?lat=37.7749&lon=-122.4194&radius_m=2000")
    assert search_resp.status_code == 200
    places = search_resp.json()
    assert len(places) >= 2
    cand_ids = [p["id"] for p in places[:3]]

    # 2. Inspect a place and verify 2-tier cache
    p_id = cand_ids[0]
    detail_resp = client.get(f"/places/{p_id}")
    assert detail_resp.status_code == 200
    detail_data = detail_resp.json()
    assert detail_data["details_cached"] is True
    assert detail_data["description"] is not None

    # Check cache stats endpoint
    cache_stats = client.get("/cache/stats").json()
    assert "tier1_cache_size" in cache_stats
    assert cache_stats["place_details_calls_upstream"] >= 1

    # 3. Create a lunch voting session with quorum = 3
    create_payload = {
        "title": "Engineering Team Lunch",
        "creator_name": "Owen",
        "office_lat": 37.7749,
        "office_lon": -122.4194,
        "quorum_needed": 3,
        "candidate_ids": cand_ids,
        "preference_filters": {
            "max_walking_minutes": 15,
            "price_levels": [1, 2],
            "dietary_tags": ["vegetarian"],
            "min_rating": 4.0,
        },
    }
    session_resp = client.post("/sessions", json=create_payload)
    assert session_resp.status_code == 201
    session_data = session_resp.json()
    session_id = session_data["id"]
    assert session_data["status"] == "active"
    assert session_data["quorum_needed"] == 3

    # 4. Cast votes
    # Voter 1: Owen loves cand_ids[0]
    v1_resp = client.post(
        f"/sessions/{session_id}/vote",
        json={"member_name": "Owen", "restaurant_id": cand_ids[0], "vote_score": 5.0, "comment": "Great burritos!"},
    )
    assert v1_resp.status_code == 200

    # Voter 2: Lisa also votes for cand_ids[0]
    v2_resp = client.post(
        f"/sessions/{session_id}/vote",
        json={"member_name": "Lisa", "restaurant_id": cand_ids[0], "vote_score": 4.5, "comment": "Love the options"},
    )
    assert v2_resp.status_code == 200

    # 5. Try deciding before quorum is reached -> Should fail with 400 Bad Request
    decide_fail = client.post(f"/sessions/{session_id}/decide")
    assert decide_fail.status_code == 400
    assert "Quorum not met" in decide_fail.json()["detail"]

    # 6. Voter 3 casts vote -> Quorum reached
    v3_resp = client.post(
        f"/sessions/{session_id}/vote",
        json={"member_name": "Ralph", "restaurant_id": cand_ids[0], "vote_score": 4.0},
    )
    assert v3_resp.status_code == 200

    # 7. Check recommendations endpoint
    rec_resp = client.get(f"/sessions/{session_id}/recommendations")
    assert rec_resp.status_code == 200
    recs = rec_resp.json()
    assert len(recs) == len(cand_ids)
    assert recs[0]["restaurant"]["id"] == cand_ids[0]
    assert recs[0]["vote_score"] > 0
    assert len(recs[0]["reasons"]) > 0

    # 8. Finalize / decide winning venue
    decide_resp = client.post(f"/sessions/{session_id}/decide")
    assert decide_resp.status_code == 200
    decision = decide_resp.json()
    assert decision["status"] == "decided"
    assert decision["quorum_met"] is True
    assert decision["total_voters"] == 3
    assert decision["winning_restaurant"]["id"] == cand_ids[0]

    # Verify session status is updated
    updated_session = client.get(f"/sessions/{session_id}").json()
    assert updated_session["status"] == "decided"
    assert updated_session["winning_restaurant_id"] == cand_ids[0]

    # 9. Voting on decided session is rejected
    late_vote = client.post(
        f"/sessions/{session_id}/vote",
        json={"member_name": "LateComer", "restaurant_id": cand_ids[0], "vote_score": 5.0},
    )
    assert late_vote.status_code == 400


def test_force_decide_without_quorum(client):
    """Test deciding a session with force=True even when quorum is not met."""
    create_payload = {
        "title": "Quick Lunch Session",
        "creator_name": "Owen",
        "quorum_needed": 5,
        "candidate_ids": ["place_golden_ramen", "place_green_leaf"],
    }
    session = client.post("/sessions", json=create_payload).json()
    s_id = session["id"]

    # Cast 1 vote (far below quorum of 5)
    client.post(
        f"/sessions/{s_id}/vote",
        json={"member_name": "SoloVoter", "restaurant_id": "place_golden_ramen", "vote_score": 5.0},
    )

    # Force decide
    force_resp = client.post(f"/sessions/{s_id}/decide?force=true")
    assert force_resp.status_code == 200
    data = force_resp.json()
    assert data["status"] == "decided"
    assert data["quorum_met"] is False
    assert data["total_voters"] == 1
    assert data["winning_restaurant"]["id"] == "place_golden_ramen"


def test_lru_cache_eviction():
    """Verify Tier-1 LRU cache eviction when capacity limit is reached."""
    from cache_proxy import Tier1MemoryCache

    lru = Tier1MemoryCache(maxsize=3, ttl_seconds=3600)
    r1 = RestaurantRead(id="r1", name="R1", latitude=0, longitude=0)
    r2 = RestaurantRead(id="r2", name="R2", latitude=0, longitude=0)
    r3 = RestaurantRead(id="r3", name="R3", latitude=0, longitude=0)
    r4 = RestaurantRead(id="r4", name="R4", latitude=0, longitude=0)

    lru.set("r1", r1)
    lru.set("r2", r2)
    lru.set("r3", r3)
    assert lru.size() == 3

    # Access r1 to mark it recently used
    assert lru.get("r1") is not None

    # Insert r4, which should evict r2 (the least recently used)
    lru.set("r4", r4)
    assert lru.size() == 3
    assert lru.get("r1") is not None
    assert lru.get("r2") is None  # Evicted!
    assert lru.get("r3") is not None
    assert lru.get("r4") is not None


def test_keyword_search_and_auto_candidate_population(client):
    """Test searching with keyword filter and auto-populating session candidates."""
    # Search for ramen
    resp = client.get("/places/search?lat=37.7749&lon=-122.4194&radius_m=3000&keyword=ramen")
    assert resp.status_code == 200
    places = resp.json()
    assert len(places) >= 1
    assert any("ramen" in p["name"].lower() for p in places)

    # Create session with empty candidate_ids -> auto-populates nearby candidates
    auto_session = client.post(
        "/sessions",
        json={"title": "Auto Candidates Poll", "creator_name": "Owen", "quorum_needed": 2, "candidate_ids": []},
    ).json()
    assert len(auto_session["candidate_ids"]) > 0
