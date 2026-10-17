"""Two-tier caching proxy for Google Places API.

Solves the Google Places API cost compromise:
Instead of dropping descriptions or paying for expensive Place Details calls on every
marker render, this proxy:
1. Performs lightweight nearby search to get basic coordinates and metadata.
2. Caches search results and enriched place details in a 2-tier architecture:
   - Tier 1: In-memory LRU/TTL cache for high-frequency requests & active voting sessions.
   - Tier 2: Persistent SQLite database (restaurants table) with long-term retention.
3. Performs lazy on-demand enrichment: only calls expensive upstream Place Details when
   a user interacts or inspects a restaurant, and caches the editorial description permanently.
4. Tracks upstream call savings and estimated dollar costs avoided.
"""

from __future__ import annotations
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
import json
import math
import os
import time
from typing import Any, Dict, List, Optional, Tuple
import httpx
from sqlalchemy.orm import Session

from database import SessionLocal
from models import RestaurantModel, RestaurantRead


# Google Places API pricing constants (USD)
COST_NEARBY_SEARCH = 0.032  # ~$32 per 1,000 requests
COST_PLACE_DETAILS = 0.025  # ~$25 per 1,000 requests with Atmosphere/Editorial summary


class Tier1MemoryCache:
    """Fast in-memory LRU cache with TTL expiration."""

    def __init__(self, maxsize: int = 500, ttl_seconds: int = 1800):
        self.maxsize = maxsize
        self.ttl_seconds = ttl_seconds
        self._cache: OrderedDict[str, Tuple[RestaurantRead, float]] = OrderedDict()

    def get(self, key: str) -> Optional[RestaurantRead]:
        if key not in self._cache:
            return None
        item, timestamp = self._cache[key]
        if time.time() - timestamp > self.ttl_seconds:
            del self._cache[key]
            return None
        # Move to most recently used
        self._cache.move_to_end(key)
        return item

    def set(self, key: str, value: RestaurantRead) -> None:
        if key in self._cache:
            del self._cache[key]
        elif len(self._cache) >= self.maxsize:
            self._cache.popitem(last=False)  # Evict oldest (LRU)
        self._cache[key] = (value, time.time())

    def clear(self) -> None:
        self._cache.clear()

    def size(self) -> int:
        return len(self._cache)


class UpstreamPlacesProvider:
    """Base interface for fetching places from upstream Google Places or mock."""

    def nearby_search(
        self, lat: float, lon: float, radius_m: int, keyword: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        raise NotImplementedError

    def place_details(self, place_id: str) -> Dict[str, Any]:
        raise NotImplementedError


class MockPlacesProvider(UpstreamPlacesProvider):
    """Realistic mock provider simulating upstream Google Places API."""

    def __init__(self):
        # Sample realistic restaurants around San Francisco downtown (37.7749, -122.4194)
        self.mock_db = {
            "place_mission_burrito": {
                "place_id": "place_mission_burrito",
                "name": "Mission Burrito Co.",
                "address": "780 Valencia St, San Francisco, CA",
                "latitude": 37.7610,
                "longitude": -122.4215,
                "rating": 4.7,
                "user_ratings_total": 450,
                "price_level": 1,
                "maps_url": "https://maps.google.com/?cid=place_mission_burrito",
                "photo_urls": [
                    "https://images.unsplash.com/photo-1584947897554-47b2c019d08e",
                    "https://images.unsplash.com/photo-1565299585323-38d6b0865b47"
                ],
                "description": "Celebrated neighborhood taqueria serving hefty Mission-style foil-wrapped burritos, housemade horchata, and vibrant salsa bar.",
                "dietary_tags": ["vegetarian", "halal", "vegan"],
                "cuisines": ["mexican", "burritos", "tacos"],
                "is_open_now": True,
            },
            "place_golden_ramen": {
                "place_id": "place_golden_ramen",
                "name": "Golden Ramen House",
                "address": "450 Geary St, San Francisco, CA",
                "latitude": 37.7865,
                "longitude": -122.4110,
                "rating": 4.6,
                "user_ratings_total": 620,
                "price_level": 2,
                "maps_url": "https://maps.google.com/?cid=place_golden_ramen",
                "photo_urls": [
                    "https://images.unsplash.com/photo-1569718212165-3a8278d5f624"
                ],
                "description": "Artisanal 18-hour tonkotsu broth, hand-pulled noodles, spicy black garlic oil, and rich vegetarian miso ramen options.",
                "dietary_tags": ["vegetarian"],
                "cuisines": ["japanese", "ramen", "noodles"],
                "is_open_now": True,
            },
            "place_green_leaf": {
                "place_id": "place_green_leaf",
                "name": "Green Leaf Plant Kitchen",
                "address": "1200 Market St, San Francisco, CA",
                "latitude": 37.7780,
                "longitude": -122.4160,
                "rating": 4.8,
                "user_ratings_total": 310,
                "price_level": 2,
                "maps_url": "https://maps.google.com/?cid=place_green_leaf",
                "photo_urls": [
                    "https://images.unsplash.com/photo-1512621776951-a57141f2eefd"
                ],
                "description": "100% plant-based organic bistro featuring macrobiotic bowls, gluten-free flatbreads, cold-pressed elixirs, and cashew-cream desserts.",
                "dietary_tags": ["vegan", "vegetarian", "gluten-free"],
                "cuisines": ["salads", "healthy", "vegan"],
                "is_open_now": True,
            },
            "place_trattoria_bella": {
                "place_id": "place_trattoria_bella",
                "name": "Trattoria Bella Vista",
                "address": "520 Columbus Ave, San Francisco, CA",
                "latitude": 37.7995,
                "longitude": -122.4080,
                "rating": 4.5,
                "user_ratings_total": 510,
                "price_level": 3,
                "maps_url": "https://maps.google.com/?cid=place_trattoria_bella",
                "photo_urls": [
                    "https://images.unsplash.com/photo-1555396273-367ea4eb4db5"
                ],
                "description": "Charming North Beach trattoria offering handmade fresh pasta, wood-fired Neapolitan pizza, and an extensive Chianti wine collection.",
                "dietary_tags": ["vegetarian"],
                "cuisines": ["italian", "pasta", "pizza"],
                "is_open_now": True,
            },
            "place_burgercraft": {
                "place_id": "place_burgercraft",
                "name": "BurgerCraft Artisanal",
                "address": "210 4th St, San Francisco, CA",
                "latitude": 37.7830,
                "longitude": -122.4030,
                "rating": 4.4,
                "user_ratings_total": 390,
                "price_level": 2,
                "maps_url": "https://maps.google.com/?cid=place_burgercraft",
                "photo_urls": [
                    "https://images.unsplash.com/photo-1568901346375-23c9450c58cd"
                ],
                "description": "Grass-fed smash patties, brioche buns baked daily, truffle aioli fries, and delicious Beyond Meat vegan burger options.",
                "dietary_tags": ["vegetarian", "vegan"],
                "cuisines": ["burgers", "american", "comfort food"],
                "is_open_now": True,
            },
            "place_oasis_mediterranean": {
                "place_id": "place_oasis_mediterranean",
                "name": "Oasis Mediterranean Grill",
                "address": "90 1st St, San Francisco, CA",
                "latitude": 37.7890,
                "longitude": -122.3995,
                "rating": 4.6,
                "user_ratings_total": 415,
                "price_level": 1,
                "maps_url": "https://maps.google.com/?cid=place_oasis_mediterranean",
                "photo_urls": [
                    "https://images.unsplash.com/photo-1540420773420-3366772f4999"
                ],
                "description": "Fast-casual Middle Eastern eatery famous for crisp falafel, tender chicken shawarma plates, creamy hummus, and fresh pita bread.",
                "dietary_tags": ["halal", "vegetarian", "vegan", "gluten-free"],
                "cuisines": ["mediterranean", "middle eastern", "falafel"],
                "is_open_now": True,
            },
            "place_dragon_dumpling": {
                "place_id": "place_dragon_dumpling",
                "name": "Dragon Dumpling Bistro",
                "address": "640 Jackson St, San Francisco, CA",
                "latitude": 37.7955,
                "longitude": -122.4060,
                "rating": 4.7,
                "user_ratings_total": 780,
                "price_level": 2,
                "maps_url": "https://maps.google.com/?cid=place_dragon_dumpling",
                "photo_urls": [
                    "https://images.unsplash.com/photo-1496116218417-1a781b1c416c"
                ],
                "description": "Steaming bamboo baskets of handcrafted xiao long bao soup dumplings, scallion pancakes, and hot chili oil wontons.",
                "dietary_tags": ["vegetarian"],
                "cuisines": ["chinese", "dim sum", "dumplings"],
                "is_open_now": True,
            },
        }

    def nearby_search(
        self, lat: float, lon: float, radius_m: int, keyword: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        results = []
        for place in self.mock_db.values():
            dist = calculate_haversine_distance(lat, lon, place["latitude"], place["longitude"])
            if dist <= radius_m:
                if keyword:
                    kw = keyword.lower()
                    name_match = kw in place["name"].lower()
                    cuisine_match = any(kw in c.lower() for c in place.get("cuisines", []))
                    if not (name_match or cuisine_match):
                        continue
                # In Google Nearby Search, description is NOT provided (only basic summary)
                lightweight = {
                    "place_id": place["place_id"],
                    "name": place["name"],
                    "address": place["address"],
                    "latitude": place["latitude"],
                    "longitude": place["longitude"],
                    "rating": place["rating"],
                    "user_ratings_total": place["user_ratings_total"],
                    "price_level": place["price_level"],
                    "maps_url": place["maps_url"],
                    "photo_urls": place["photo_urls"][:1],  # Only 1 thumbnail in nearby search
                    "cuisines": place["cuisines"],
                    "is_open_now": place["is_open_now"],
                    # Note: 'description' is intentionally omitted in nearby search, mirroring Google API
                }
                results.append(lightweight)
        return results

    def place_details(self, place_id: str) -> Dict[str, Any]:
        if place_id in self.mock_db:
            return dict(self.mock_db[place_id])
        raise ValueError(f"Mock place {place_id} not found")


class GooglePlacesLiveProvider(UpstreamPlacesProvider):
    """Live Google Places API client with error handling and fallback."""

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.base_url = "https://maps.googleapis.com/maps/api/place"

    def nearby_search(
        self, lat: float, lon: float, radius_m: int, keyword: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        params = {
            "location": f"{lat},{lon}",
            "radius": radius_m,
            "type": "restaurant",
            "key": self.api_key,
        }
        if keyword:
            params["keyword"] = keyword

        with httpx.Client(timeout=10.0) as client:
            resp = client.get(f"{self.base_url}/nearbysearch/json", params=params)
            resp.raise_for_status()
            data = resp.json()

        results = []
        for item in data.get("results", []):
            loc = item.get("geometry", {}).get("location", {})
            photos = []
            for p in item.get("photos", [])[:1]:
                photo_ref = p.get("photo_reference")
                if photo_ref:
                    photos.append(
                        f"{self.base_url}/photo?maxwidth=400&photo_reference={photo_ref}&key={self.api_key}"
                    )

            results.append({
                "place_id": item.get("place_id"),
                "name": item.get("name"),
                "address": item.get("vicinity"),
                "latitude": loc.get("lat", 0.0),
                "longitude": loc.get("lng", 0.0),
                "rating": item.get("rating", 0.0),
                "user_ratings_total": item.get("user_ratings_total", 0),
                "price_level": item.get("price_level", 2),
                "maps_url": f"https://www.google.com/maps/place/?q=place_id:{item.get('place_id')}",
                "photo_urls": photos,
                "cuisines": item.get("types", []),
                "is_open_now": item.get("opening_hours", {}).get("open_now", True),
            })
        return results

    def place_details(self, place_id: str) -> Dict[str, Any]:
        params = {
            "place_id": place_id,
            "fields": "place_id,name,formatted_address,geometry,rating,user_ratings_total,price_level,url,photos,editorial_summary,opening_hours,types",
            "key": self.api_key,
        }
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(f"{self.base_url}/details/json", params=params)
            resp.raise_for_status()
            data = resp.json()

        res = data.get("result", {})
        loc = res.get("geometry", {}).get("location", {})
        photos = []
        for p in res.get("photos", []):
            photo_ref = p.get("photo_reference")
            if photo_ref:
                photos.append(
                    f"{self.base_url}/photo?maxwidth=800&photo_reference={photo_ref}&key={self.api_key}"
                )

        desc = res.get("editorial_summary", {}).get("overview", "")
        return {
            "place_id": res.get("place_id", place_id),
            "name": res.get("name"),
            "address": res.get("formatted_address"),
            "latitude": loc.get("lat", 0.0),
            "longitude": loc.get("lng", 0.0),
            "rating": res.get("rating", 0.0),
            "user_ratings_total": res.get("user_ratings_total", 0),
            "price_level": res.get("price_level", 2),
            "maps_url": res.get("url") or f"https://www.google.com/maps/place/?q=place_id:{place_id}",
            "photo_urls": photos,
            "description": desc,
            "dietary_tags": [],
            "cuisines": res.get("types", []),
            "is_open_now": res.get("opening_hours", {}).get("open_now", True),
        }


def calculate_haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> int:
    """Calculate distance in meters between two coordinates using Haversine formula."""
    r = 6371000  # Earth radius in meters
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    a = (math.sin(delta_phi / 2.0) ** 2) + math.cos(phi1) * math.cos(phi2) * (math.sin(delta_lambda / 2.0) ** 2)
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return int(round(r * c))


def calculate_walking_time_mins(distance_m: int, walking_speed_m_per_min: float = 80.0) -> int:
    """Calculate walking time in minutes assuming standard ~4.8 km/h (80 m/min)."""
    return max(1, int(math.ceil(distance_m / walking_speed_m_per_min)))


class TwoTierCachingProxy:
    """Two-tier caching proxy for Google Places API."""

    def __init__(
        self,
        upstream_provider: Optional[UpstreamPlacesProvider] = None,
        tier1_ttl_seconds: int = 1800,
        tier2_ttl_days: int = 14,
    ):
        api_key = os.getenv("GOOGLE_PLACES_API_KEY")
        if upstream_provider:
            self.provider = upstream_provider
        elif api_key:
            self.provider = GooglePlacesLiveProvider(api_key=api_key)
        else:
            self.provider = MockPlacesProvider()

        self.tier1_cache = Tier1MemoryCache(maxsize=500, ttl_seconds=tier1_ttl_seconds)
        self.tier2_ttl = timedelta(days=tier2_ttl_days)

        # Cost and performance metrics
        self.stats = {
            "tier1_hits": 0,
            "tier2_hits": 0,
            "nearby_searches_upstream": 0,
            "place_details_calls_upstream": 0,
            "place_details_calls_saved": 0,
        }

    def get_stats(self) -> Dict[str, Any]:
        """Return metrics on cache hits and estimated API cost savings."""
        saved_details = self.stats["place_details_calls_saved"]
        dollars_saved = saved_details * COST_PLACE_DETAILS
        dollars_spent = (
            self.stats["nearby_searches_upstream"] * COST_NEARBY_SEARCH
            + self.stats["place_details_calls_upstream"] * COST_PLACE_DETAILS
        )

        return {
            **self.stats,
            "tier1_cache_size": self.tier1_cache.size(),
            "estimated_dollars_spent": round(dollars_spent, 4),
            "estimated_dollars_saved": round(dollars_saved, 4),
            "net_savings_percentage": round((dollars_saved / (dollars_saved + dollars_spent) * 100) if (dollars_saved + dollars_spent) > 0 else 0.0, 1),
        }

    def search_nearby(
        self,
        lat: float,
        lon: float,
        radius_m: int = 1500,
        keyword: Optional[str] = None,
        db: Optional[Session] = None,
    ) -> List[RestaurantRead]:
        """Search nearby places using lightweight search and merge with existing cache."""
        should_close_db = False
        if db is None:
            db = SessionLocal()
            should_close_db = True

        try:
            self.stats["nearby_searches_upstream"] += 1
            raw_places = self.provider.nearby_search(lat, lon, radius_m, keyword)
            restaurants: List[RestaurantRead] = []

            for p in raw_places:
                place_id = p["place_id"]
                dist = calculate_haversine_distance(lat, lon, p["latitude"], p["longitude"])
                walk_mins = calculate_walking_time_mins(dist)

                # Check Tier 1 memory cache
                cached_mem = self.tier1_cache.get(place_id)
                if cached_mem and cached_mem.details_cached:
                    self.stats["tier1_hits"] += 1
                    # Update relative distance for this search anchor
                    cached_mem.walking_distance_m = dist
                    cached_mem.walking_time_mins = walk_mins
                    restaurants.append(cached_mem)
                    continue

                # Check Tier 2 persistent SQLite cache
                db_model = db.query(RestaurantModel).filter(RestaurantModel.id == place_id).first()
                if db_model:
                    self.stats["tier2_hits"] += 1
                    db_model.walking_distance_m = dist
                    db_model.walking_time_mins = walk_mins
                    db.commit()
                    r_read = db_model.to_pydantic()
                    self.tier1_cache.set(place_id, r_read)
                    restaurants.append(r_read)
                    continue

                # Not yet in DB: Save basic record in Tier 2
                new_model = RestaurantModel(
                    id=place_id,
                    name=p["name"],
                    address=p.get("address"),
                    latitude=p["latitude"],
                    longitude=p["longitude"],
                    rating=p.get("rating", 0.0),
                    user_ratings_total=p.get("user_ratings_total", 0),
                    price_level=p.get("price_level", 2),
                    walking_distance_m=dist,
                    walking_time_mins=walk_mins,
                    maps_url=p.get("maps_url", ""),
                    photo_urls=json.dumps(p.get("photo_urls", [])),
                    description=None,  # Not fetched yet (deferred to save API costs)
                    dietary_tags=json.dumps(p.get("dietary_tags", [])),
                    cuisines=json.dumps(p.get("cuisines", [])),
                    is_open_now=p.get("is_open_now", True),
                    details_cached=False,
                    cached_at=datetime.now(timezone.utc),
                    cache_expiry=datetime.now(timezone.utc) + self.tier2_ttl,
                )
                db.add(new_model)
                db.commit()
                db.refresh(new_model)

                r_read = new_model.to_pydantic()
                self.tier1_cache.set(place_id, r_read)
                restaurants.append(r_read)

            return restaurants
        finally:
            if should_close_db:
                db.close()

    def get_enriched_details(
        self,
        place_id: str,
        anchor_lat: Optional[float] = None,
        anchor_lon: Optional[float] = None,
        db: Optional[Session] = None,
    ) -> RestaurantRead:
        """Fetch full place details including editorial description, with 2-tier caching."""
        should_close_db = False
        if db is None:
            db = SessionLocal()
            should_close_db = True

        try:
            # 1. Tier 1 In-Memory Cache Check
            cached_mem = self.tier1_cache.get(place_id)
            if cached_mem and cached_mem.details_cached:
                self.stats["tier1_hits"] += 1
                self.stats["place_details_calls_saved"] += 1
                if anchor_lat is not None and anchor_lon is not None:
                    cached_mem.walking_distance_m = calculate_haversine_distance(
                        anchor_lat, anchor_lon, cached_mem.latitude, cached_mem.longitude
                    )
                    cached_mem.walking_time_mins = calculate_walking_time_mins(cached_mem.walking_distance_m)
                return cached_mem

            # 2. Tier 2 Persistent SQLite DB Check
            db_model = db.query(RestaurantModel).filter(RestaurantModel.id == place_id).first()
            if db_model and db_model.details_cached:
                self.stats["tier2_hits"] += 1
                self.stats["place_details_calls_saved"] += 1
                if anchor_lat is not None and anchor_lon is not None:
                    db_model.walking_distance_m = calculate_haversine_distance(
                        anchor_lat, anchor_lon, db_model.latitude, db_model.longitude
                    )
                    db_model.walking_time_mins = calculate_walking_time_mins(db_model.walking_distance_m)
                    db.commit()
                r_read = db_model.to_pydantic()
                self.tier1_cache.set(place_id, r_read)
                return r_read

            # 3. Cache Miss: Make upstream Place Details API call
            self.stats["place_details_calls_upstream"] += 1
            details = self.provider.place_details(place_id)

            dist = 0
            walk_mins = 0
            if anchor_lat is not None and anchor_lon is not None:
                dist = calculate_haversine_distance(
                    anchor_lat, anchor_lon, details["latitude"], details["longitude"]
                )
                walk_mins = calculate_walking_time_mins(dist)

            if db_model:
                # Update existing lightweight record with rich details
                db_model.name = details.get("name", db_model.name)
                db_model.address = details.get("address", db_model.address)
                db_model.rating = details.get("rating", db_model.rating)
                db_model.user_ratings_total = details.get("user_ratings_total", db_model.user_ratings_total)
                db_model.price_level = details.get("price_level", db_model.price_level)
                db_model.maps_url = details.get("maps_url", db_model.maps_url)
                db_model.photo_urls = json.dumps(details.get("photo_urls", []))
                db_model.description = details.get("description", "")
                if details.get("dietary_tags"):
                    db_model.dietary_tags = json.dumps(details["dietary_tags"])
                if details.get("cuisines"):
                    db_model.cuisines = json.dumps(details["cuisines"])
                db_model.is_open_now = details.get("is_open_now", True)
                db_model.details_cached = True
                if dist > 0:
                    db_model.walking_distance_m = dist
                    db_model.walking_time_mins = walk_mins
                db_model.cached_at = datetime.now(timezone.utc)
                db.commit()
                db.refresh(db_model)
                record = db_model
            else:
                # Create brand new enriched record
                new_model = RestaurantModel(
                    id=place_id,
                    name=details["name"],
                    address=details.get("address"),
                    latitude=details["latitude"],
                    longitude=details["longitude"],
                    rating=details.get("rating", 0.0),
                    user_ratings_total=details.get("user_ratings_total", 0),
                    price_level=details.get("price_level", 2),
                    walking_distance_m=dist,
                    walking_time_mins=walk_mins,
                    maps_url=details.get("maps_url", ""),
                    photo_urls=json.dumps(details.get("photo_urls", [])),
                    description=details.get("description", ""),
                    dietary_tags=json.dumps(details.get("dietary_tags", [])),
                    cuisines=json.dumps(details.get("cuisines", [])),
                    is_open_now=details.get("is_open_now", True),
                    details_cached=True,
                    cached_at=datetime.now(timezone.utc),
                    cache_expiry=datetime.now(timezone.utc) + self.tier2_ttl,
                )
                db.add(new_model)
                db.commit()
                db.refresh(new_model)
                record = new_model

            r_read = record.to_pydantic()
            self.tier1_cache.set(place_id, r_read)
            return r_read
        finally:
            if should_close_db:
                db.close()


# Singleton instance
global_cache_proxy = TwoTierCachingProxy()
