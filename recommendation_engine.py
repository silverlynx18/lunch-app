"""Recommendation scoring algorithm for group dining decision coordination.

Balances multiple criteria:
1. Team consensus & vote score (up to 40 pts)
2. Walking distance & time from office anchor (up to 25 pts)
3. Budget / price level compatibility (up to 15 pts)
4. Dietary tags & restriction satisfaction (up to 10 pts)
5. Google rating & community review confidence (up to 10 pts)

Outputs ranked recommendations with transparent score breakdowns and reasons.
"""

from __future__ import annotations
from typing import Dict, List, Optional, Tuple
from cache_proxy import calculate_haversine_distance, calculate_walking_time_mins
from models import (
    MemberVoteRead,
    PreferenceFilter,
    RecommendationResult,
    RestaurantRead,
)


def score_vote_consensus(
    restaurant_id: str,
    votes: List[MemberVoteRead],
) -> Tuple[float, List[str]]:
    """Score team vote aggregation (0.0 to 40.0 points)."""
    place_votes = [v for v in votes if v.restaurant_id == restaurant_id]
    reasons: List[str] = []

    if not place_votes:
        # If no votes cast at all in the entire session, return neutral base
        if not votes:
            return 20.0, ["Session voting in progress (neutral 20.0/40)"]
        # If other candidates have received votes but this one received none:
        return 0.0, ["No team votes cast for this venue (0.0/40)"]

    scores = [v.vote_score for v in place_votes]
    avg_score = sum(scores) / len(scores)

    # Base consensus score: (avg / 5.0) * 35
    base_vote_pts = (avg_score / 5.0) * 35.0

    # Popularity bonus: up to +5 points for more supporters
    num_voters = len(place_votes)
    popularity_bonus = min(5.0, (num_voters - 1) * 1.5) if num_voters > 1 else 0.0

    total_vote_score = min(40.0, base_vote_pts + popularity_bonus)

    # Check for vetoes / strong negative votes (1.0 or 1.5)
    vetoes = [v for v in place_votes if v.vote_score <= 1.5]
    if vetoes:
        total_vote_score = max(0.0, total_vote_score - 10.0)
        reasons.append(
            f"Veto warning: {len(vetoes)} member(s) rated <= 1.5 ({', '.join(v.member_name for v in vetoes)})"
        )
    else:
        if avg_score >= 4.0:
            reasons.append(
                f"Strong team consensus ({avg_score:.1f}/5.0 avg across {num_voters} votes)"
            )
        else:
            reasons.append(
                f"Team rating {avg_score:.1f}/5.0 across {num_voters} vote(s)"
            )

    return round(total_vote_score, 2), reasons


def score_walking_distance(
    restaurant: RestaurantRead,
    office_lat: float,
    office_lon: float,
    filter_pref: PreferenceFilter,
) -> Tuple[float, int, int, List[str]]:
    """Score walking distance/radius (0.0 to 25.0 points)."""
    dist_m = calculate_haversine_distance(
        office_lat, office_lon, restaurant.latitude, restaurant.longitude
    )
    walk_mins = calculate_walking_time_mins(dist_m)
    reasons: List[str] = []

    max_dist = filter_pref.max_walking_distance_m or 1200
    if filter_pref.max_walking_minutes:
        max_dist = min(max_dist, filter_pref.max_walking_minutes * 80)

    if dist_m <= 300:
        # Very close: full 25 points
        dist_score = 25.0
        reasons.append(f"Very close walking distance ({walk_mins} min, {dist_m}m)")
    elif dist_m <= max_dist:
        # Linear decay from 25.0 down to 5.0 points
        ratio = (dist_m - 300) / max(1, (max_dist - 300))
        dist_score = 25.0 - (20.0 * ratio)
        reasons.append(f"Within walking radius ({walk_mins} min, {dist_m}m)")
    else:
        # Outside walking range
        excess = dist_m - max_dist
        dist_score = max(0.0, 5.0 - (excess / 150.0))
        reasons.append(f"Exceeds target walking limit ({walk_mins} min, {dist_m}m > {max_dist}m)")

    return round(dist_score, 2), dist_m, walk_mins, reasons


def score_price_level(
    restaurant: RestaurantRead,
    filter_pref: PreferenceFilter,
) -> Tuple[float, List[str]]:
    """Score budget match (0.0 to 15.0 points)."""
    reasons: List[str] = []
    allowed = filter_pref.price_levels or [1, 2]

    if restaurant.price_level in allowed:
        reasons.append(f"Matches target price level ({'$' * restaurant.price_level})")
        return 15.0, reasons

    max_allowed = max(allowed) if allowed else 2
    if restaurant.price_level == max_allowed + 1:
        reasons.append(f"Slightly above target price level ({'$' * restaurant.price_level})")
        return 7.0, reasons

    reasons.append(f"Above team budget ({'$' * restaurant.price_level})")
    return 2.0, reasons


def score_dietary_match(
    restaurant: RestaurantRead,
    filter_pref: PreferenceFilter,
) -> Tuple[float, List[str]]:
    """Score dietary compatibility (0.0 to 10.0 points)."""
    required_tags = filter_pref.dietary_tags or []
    if not required_tags:
        return 10.0, ["No dietary constraints required"]

    restaurant_tags = set(t.lower() for t in restaurant.dietary_tags)
    matches = [tag for tag in required_tags if tag.lower() in restaurant_tags]

    reasons: List[str] = []
    if len(matches) == len(required_tags):
        reasons.append(f"Fully satisfies dietary needs: {', '.join(matches)}")
        return 10.0, reasons
    elif len(matches) > 0:
        pct = len(matches) / len(required_tags)
        reasons.append(f"Partially satisfies dietary needs ({', '.join(matches)})")
        return round(10.0 * pct, 2), reasons
    else:
        reasons.append(f"Missing required dietary options: {', '.join(required_tags)}")
        return 0.0, reasons


def score_rating_quality(
    restaurant: RestaurantRead,
    filter_pref: PreferenceFilter,
) -> Tuple[float, List[str]]:
    """Score Google rating and review count confidence (0.0 to 10.0 points)."""
    reasons: List[str] = []
    rating = restaurant.rating or 0.0

    # Base rating score: up to 8.0 points
    base_score = (rating / 5.0) * 8.0

    # Review volume confidence: up to 2.0 points
    reviews = restaurant.user_ratings_total or 0
    if reviews >= 200:
        conf = 2.0
    elif reviews >= 50:
        conf = 1.0
    else:
        conf = 0.5

    total_rating_score = min(10.0, base_score + conf)

    min_rating = filter_pref.min_rating or 3.5
    if rating < min_rating:
        total_rating_score = max(0.0, total_rating_score - 3.0)
        reasons.append(f"Below min rating threshold ({rating:.1f} < {min_rating})")
    else:
        reasons.append(f"Rating {rating:.1f}★ ({reviews} reviews)")

    return round(total_rating_score, 2), reasons


class RecommendationEngine:
    """Coordinates and evaluates candidate dining options for group voting sessions."""

    def evaluate(
        self,
        candidates: List[RestaurantRead],
        votes: List[MemberVoteRead],
        preferences: Optional[PreferenceFilter] = None,
        office_lat: float = 37.7749,
        office_lon: float = -122.4194,
    ) -> List[RecommendationResult]:
        """Rank candidates with full transparent score breakdown."""
        filter_pref = preferences or PreferenceFilter()
        results: List[RecommendationResult] = []

        for restaurant in candidates:
            all_reasons: List[str] = []

            # 1. Vote score
            vote_pts, vote_reasons = score_vote_consensus(restaurant.id, votes)
            all_reasons.extend(vote_reasons)

            # 2. Distance score
            dist_pts, dist_m, walk_mins, dist_reasons = score_walking_distance(
                restaurant, office_lat, office_lon, filter_pref
            )
            all_reasons.extend(dist_reasons)

            # 3. Price score
            price_pts, price_reasons = score_price_level(restaurant, filter_pref)
            all_reasons.extend(price_reasons)

            # 4. Dietary score
            dietary_pts, dietary_reasons = score_dietary_match(restaurant, filter_pref)
            all_reasons.extend(dietary_reasons)

            # 5. Rating score
            rating_pts, rating_reasons = score_rating_quality(restaurant, filter_pref)
            all_reasons.extend(rating_reasons)

            # Open now check
            penalty = 0.0
            if filter_pref.require_open_now and not restaurant.is_open_now:
                penalty = 25.0
                all_reasons.append("Currently closed (-25 penalty)")

            total = max(0.0, round(vote_pts + dist_pts + price_pts + dietary_pts + rating_pts - penalty, 2))

            # Update restaurant object with accurate walking distance
            updated_restaurant = restaurant.model_copy(
                update={"walking_distance_m": dist_m, "walking_time_mins": walk_mins}
            )

            results.append(
                RecommendationResult(
                    restaurant=updated_restaurant,
                    total_score=total,
                    vote_score=vote_pts,
                    distance_score=dist_pts,
                    price_score=price_pts,
                    dietary_score=dietary_pts,
                    rating_score=rating_pts,
                    reasons=all_reasons,
                )
            )

        # Sort descending by total score
        results.sort(key=lambda r: r.total_score, reverse=True)
        return results

    def check_quorum(
        self,
        votes: List[MemberVoteRead],
        quorum_needed: int,
    ) -> Tuple[bool, int, List[str]]:
        """Verify whether distinct voters meet or exceed session quorum."""
        distinct_voters = sorted(list(set(v.member_name for v in votes)))
        quorum_met = len(distinct_voters) >= quorum_needed
        return quorum_met, len(distinct_voters), distinct_voters
