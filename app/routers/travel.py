"""
Travel / flight-arc feature.

Given two places (from the `places` table), this computes:
  - great-circle distance (km)
  - a rough flight-time estimate
  - an array of intermediate lat/lng points along the great-circle path,
    for animating a flight arc on the 3D globe.

`places` lookup/search also lives here since the travel feature is the
main consumer of that table right now.
"""
import math

from fastapi import APIRouter, HTTPException, Query

from app.database import get_supabase

router = APIRouter()

AVG_FLIGHT_SPEED_KMH = 800.0  # rough cruise speed average across aircraft types
GROUND_OVERHEAD_HOURS = 0.5   # taxi/takeoff/landing/climb overhead, rough estimate


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _to_vec(lat_rad: float, lng_rad: float):
    return (
        math.cos(lat_rad) * math.cos(lng_rad),
        math.cos(lat_rad) * math.sin(lng_rad),
        math.sin(lat_rad),
    )


def _to_latlng(v):
    x, y, z = v
    lat = math.asin(max(-1.0, min(1.0, z)))
    lng = math.atan2(y, x)
    return math.degrees(lat), math.degrees(lng)


# 두 지점이 지구 반대편(대척점)에 아주 가까우면 대권경로가 수학적으로
# 무한히 많아져서(어느 자오선으로 가도 최단거리가 같음) sin(omega)가
# 0에 가까워져 slerp 계산이 불안정해진다 (좌표가 튀거나 원을 그림).
# 이 경우를 감지해서, 위도 방향으로 아주 살짝(0.05도) 틀어 특이점을
# 피한다 — 항공 노선 표시 목적상 경로가 살짝 어긋나는 건 시각적으로
# 문제되지 않는다.
_ANTIPODAL_DOT_THRESHOLD = -0.9999  # 약 179.2도 이상 벌어지면 근접 대척점으로 간주


def great_circle_arc(lat1: float, lng1: float, lat2: float, lng2: float, num_points: int = 64):
    """
    Spherical linear interpolation (slerp) between two lat/lng points,
    returning `num_points` points along the great-circle path between
    them. Used to draw/animate a flight arc on the 3D globe.

    Longitude values in the returned points are "unwrapped" (continuous,
    not clamped to -180..180) so that a route crossing the antimeridian
    (±180°) doesn't jump discontinuously — the frontend should draw these
    points as-is, in order, without re-wrapping them into -180..180.
    """
    v1 = _to_vec(math.radians(lat1), math.radians(lng1))
    v2 = _to_vec(math.radians(lat2), math.radians(lng2))

    dot = max(-1.0, min(1.0, sum(a * b for a, b in zip(v1, v2))))

    if dot < _ANTIPODAL_DOT_THRESHOLD:
        # 근접 대척점: lat2를 살짝 틀어서 특이점을 피한다.
        lat2 = lat2 + (0.05 if lat2 <= 0 else -0.05)
        v2 = _to_vec(math.radians(lat2), math.radians(lng2))
        dot = max(-1.0, min(1.0, sum(a * b for a, b in zip(v1, v2))))

    omega = math.acos(dot)

    if omega < 1e-9:
        # Same point (or numerically identical) — nothing to interpolate.
        return [(lat1, lng1)] * num_points

    raw_points = []
    for i in range(num_points):
        t = i / (num_points - 1)
        a = math.sin((1 - t) * omega) / math.sin(omega)
        b = math.sin(t * omega) / math.sin(omega)
        vx = a * v1[0] + b * v2[0]
        vy = a * v1[1] + b * v2[1]
        vz = a * v1[2] + b * v2[2]
        raw_points.append(_to_latlng((vx, vy, vz)))

    return _unwrap_longitudes(raw_points)


def _unwrap_longitudes(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """
    연속된 점들의 경도가 -180/180 경계를 넘을 때 갑자기 튀지 않도록,
    이전 점과의 차이가 180도를 넘으면 ±360도를 더해 연속된 값으로 만든다.
    (예: 179.9 -> -179.8 로 이어지던 것을 179.9 -> 180.2 로 이어지게 함)
    프론트엔드는 이 값을 그대로 순서대로 선으로 이으면 되고, 다시
    -180..180으로 wrap하면 안 된다.
    """
    if not points:
        return points

    unwrapped = [points[0]]
    prev_lng = points[0][1]
    for lat, lng in points[1:]:
        diff = lng - prev_lng
        if diff > 180:
            lng -= 360
        elif diff < -180:
            lng += 360
        unwrapped.append((lat, lng))
        prev_lng = lng
    return unwrapped


def _get_place(sb, place_id: int) -> dict:
    res = sb.table("places").select("*").eq("id", place_id).single().execute()
    if not res.data:
        raise HTTPException(status_code=404, detail=f"Place {place_id} not found")
    return res.data


@router.get("/places/search")
def search_places(q: str = Query(..., min_length=1), limit: int = 10):
    """
    Autocomplete-style search over `places` by city or country name.
    e.g. /places/search?q=seoul
    """
    sb = get_supabase()
    res = (
        sb.table("places")
        .select("*")
        .or_(f"city.ilike.%{q}%,city_en.ilike.%{q}%,country.ilike.%{q}%")
        .limit(limit)
        .execute()
    )
    return res.data


@router.get("/places/{place_id}")
def get_place(place_id: int):
    sb = get_supabase()
    return _get_place(sb, place_id)


@router.get("/travel/route")
def travel_route(from_id: int, to_id: int, arc_points: int = 64):
    """
    Distance, rough flight-time estimate, and a great-circle arc
    (list of lat/lng points) between two places in the `places` table.

    e.g. /travel/route?from_id=12&to_id=87
    """
    sb = get_supabase()
    origin = _get_place(sb, from_id)
    dest = _get_place(sb, to_id)

    distance_km = haversine_km(origin["lat"], origin["lng"], dest["lat"], dest["lng"])
    duration_hours = distance_km / AVG_FLIGHT_SPEED_KMH + GROUND_OVERHEAD_HOURS

    arc = great_circle_arc(origin["lat"], origin["lng"], dest["lat"], dest["lng"], num_points=arc_points)

    return {
        "from": origin,
        "to": dest,
        "distance_km": round(distance_km, 1),
        "duration_hours": round(duration_hours, 2),
        "arc": [{"lat": lat, "lng": lng} for lat, lng in arc],
    }
