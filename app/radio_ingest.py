"""
radio_ingest.py — Radio Browser 방송국을 Supabase `radio_stations` 테이블에 적재.

3단계 좌표 배치:
    1차 — Radio Browser가 준 실좌표(geo_lat/geo_long)가 있으면 그대로 사용.
    2차 — 실좌표는 없지만 station의 `state` 필드(도시/지역명 텍스트)가
          GeoNames 도시 목록과 매칭되면 그 도시 좌표 사용.
    3차 — 그마저 없으면 국가 수도 좌표를 중심으로, 국가 면적에 비례한
          반경 안에서 임의 배치.
    공통 — 어느 단계든 좌표가 겹치면(같은 지점에 이미 다른 방송국이
          배치돼 있으면) 10km 간격 나선형으로 밀어낸다.

전체 데이터를 3단계로 나눠 "전수 조사 → 해당분 배치 → 남은 것만 다음 단계"
순으로 진행한다 (국가별로 단계를 도는 게 아니라, 전체 방송국을 대상으로
1차를 먼저 다 끝내고, 아직 안 끝난 것들만 2차, 그 나머지만 3차로 넘어감).

실행:
    cd signal-backend
    pip install httpx python-dotenv global-land-mask numpy --break-system-packages
    python -m app.radio_ingest

필요 환경변수 (.env):
    SUPABASE_URL
    SUPABASE_SERVICE_ROLE_KEY
"""
from __future__ import annotations

import io
import math
import os
import random
import re
import time
import zipfile
from dataclasses import dataclass, field

import httpx
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

RADIO_BROWSER_HOST = "https://de1.api.radio-browser.info"
USER_AGENT = "Signal-App/0.1 (prototype)"

GEONAMES_CITIES_URL = "https://download.geonames.org/export/dump/cities15000.zip"
GEONAMES_COUNTRYINFO_URL = "https://download.geonames.org/export/dump/countryInfo.txt"

SPIRAL_STEP_KM = 10.0  # 겹칠 때 밀어내는 간격

# --------------------------------------------------------------------------
# 육지/바다 판정 (바다 위에 마커가 찍히는 문제 방지)
# --------------------------------------------------------------------------
try:
    from global_land_mask import globe as _land_globe

    def is_on_land(lat: float, lng: float) -> bool:
        # 경도는 -180~180 범위로 정규화 (지터로 인해 살짝 벗어날 수 있음)
        lng_norm = ((lng + 180) % 360) - 180
        lat_c = max(-90.0, min(90.0, lat))
        return bool(_land_globe.is_land(lat_c, lng_norm))

except ImportError:  # pragma: no cover
    print("⚠️ global-land-mask 미설치 — 육지 판정 없이 진행합니다. "
          "'pip install global-land-mask --break-system-packages' 권장.")

    def is_on_land(lat: float, lng: float) -> bool:  # type: ignore
        return True


# --------------------------------------------------------------------------
# 공용 지오메트리 헬퍼
# --------------------------------------------------------------------------

def km_to_deg_lat(km: float) -> float:
    return km / 111.0


def km_to_deg_lng(km: float, at_lat: float) -> float:
    return km / (111.0 * max(0.1, math.cos(math.radians(at_lat))))


def spiral_offset(base_lat: float, base_lng: float, index: int) -> tuple[float, float]:
    """
    같은 지점에 index번째로 배치되는 항목을 10km 나선형으로 밀어낸 좌표를 반환.
    index=0이면 원래 좌표 그대로.
    """
    if index == 0:
        return base_lat, base_lng
    radius_km = SPIRAL_STEP_KM * math.sqrt(index)
    angle = index * 137.5  # 골든 앵글 — 균등하게 퍼지도록
    rad = math.radians(angle)
    dlat = km_to_deg_lat(radius_km) * math.sin(rad)
    dlng = km_to_deg_lng(radius_km, base_lat) * math.cos(rad)
    return base_lat + dlat, base_lng + dlng


class OccupancyTracker:
    """
    같은 좌표(소수 4자리로 반올림해 동일 지점 취급)에 몇 개가 이미
    배치됐는지 세어, 겹칠 때마다 나선형으로 밀어낼 인덱스를 내준다.
    """

    def __init__(self) -> None:
        self._counts: dict[tuple[float, float], int] = {}

    def place(self, lat: float, lng: float) -> tuple[float, float]:
        key = (round(lat, 4), round(lng, 4))
        idx = self._counts.get(key, 0)
        self._counts[key] = idx + 1
        return spiral_offset(lat, lng, idx)


# --------------------------------------------------------------------------
# GeoNames 데이터 로딩 (2차: 도시 매칭 / 3차: 국가 수도+면적)
# --------------------------------------------------------------------------

@dataclass
class CountryInfo:
    iso2: str
    capital: str
    area_km2: float | None
    capital_lat: float | None = None
    capital_lng: float | None = None


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


@dataclass
class CityPool:
    """국가별 실제 도시 목록 — 인구 비례 가중 샘플링용 (3차 배치를 도시 중심으로
    자연스럽게 분포시키기 위함)."""
    lats: list = field(default_factory=list)
    lngs: list = field(default_factory=list)
    weights: list = field(default_factory=list)  # 인구 (누적합 아님, 그냥 가중치)


def load_geonames_cities(
    client: httpx.Client,
) -> tuple[dict[str, list[tuple[str, float, float, bool]]], dict[str, CityPool]]:
    """
    반환값 두 가지:
      1) name_index: countrycode -> [(정규화된 도시명, lat, lng, is_capital), ...]
         → 2차(도시명 매칭)에 사용, alt name 포함해서 중복 등록.
      2) city_pool:  countrycode -> CityPool(인구 가중치 포함)
         → 3차(국가 임의배치)에서 "인구 많은 도시 근처에 더 몰리게" 가중 샘플링용.
         alt name 중복 없이 실제 도시 1개당 1건만 포함.

    cities15000.txt (인구 15,000명 이상) 사용 — 약 26,000개 도시, 수도 대부분 포함.
    """
    print("GeoNames cities15000 다운로드 중...")
    resp = client.get(GEONAMES_CITIES_URL, timeout=60)
    resp.raise_for_status()
    zf = zipfile.ZipFile(io.BytesIO(resp.content))
    raw = zf.read("cities15000.txt").decode("utf-8")

    name_index: dict[str, list[tuple[str, float, float, bool]]] = {}
    city_pool: dict[str, CityPool] = {}

    for line in raw.splitlines():
        cols = line.split("\t")
        if len(cols) < 15:
            continue
        name = cols[1]
        alt_names = cols[3]
        lat = float(cols[4])
        lng = float(cols[5])
        feature_code = cols[7]  # PPLC = capital
        country_code = cols[8]
        try:
            population = float(cols[14]) if cols[14] else 0.0
        except ValueError:
            population = 0.0
        is_capital = feature_code == "PPLC"

        names_to_index = {name, *alt_names.split(",")} if alt_names else {name}
        entries = name_index.setdefault(country_code, [])
        for n in names_to_index:
            n = n.strip()
            if n:
                entries.append((_norm(n), lat, lng, is_capital))

        pool = city_pool.setdefault(country_code, CityPool())
        pool.lats.append(lat)
        pool.lngs.append(lng)
        # 인구 0(데이터 누락)인 도시도 완전히 배제하지 않도록 최소 가중치 부여
        pool.weights.append(max(population, 500.0))

    print(f"  {sum(len(v) for v in name_index.values())}개 도시명 로드 완료 ({len(name_index)}개국)")
    return name_index, city_pool


def load_country_info(client: httpx.Client) -> dict[str, CountryInfo]:
    """
    countrycode -> CountryInfo(수도, 면적). 수도 좌표는 cities 목록에서 채워넣는다.
    """
    print("GeoNames countryInfo 다운로드 중...")
    resp = client.get(GEONAMES_COUNTRYINFO_URL, timeout=30)
    resp.raise_for_status()

    result: dict[str, CountryInfo] = {}
    for line in resp.text.splitlines():
        if not line or line.startswith("#"):
            continue
        cols = line.split("\t")
        if len(cols) < 8:
            continue
        iso2 = cols[0]
        area = cols[6]
        capital = cols[5]
        try:
            area_km2 = float(area) if area else None
        except ValueError:
            area_km2 = None
        result[iso2] = CountryInfo(iso2=iso2, capital=capital, area_km2=area_km2)
    print(f"  {len(result)}개국 정보 로드 완료")
    return result


def fill_capital_coords(countries: dict[str, CountryInfo], cities_by_country) -> None:
    for iso2, info in countries.items():
        entries = cities_by_country.get(iso2, [])
        capital_entry = next((e for e in entries if e[3]), None)  # is_capital
        if capital_entry:
            info.capital_lat, info.capital_lng = capital_entry[1], capital_entry[2]
        elif entries:
            # 수도 정보가 없으면 그 나라에서 제일 먼저 나온 도시로 대체
            info.capital_lat, info.capital_lng = entries[0][1], entries[0][2]


def match_city(cities_by_country, countrycode: str, state_text: str) -> tuple[float, float] | None:
    if not state_text:
        return None
    entries = cities_by_country.get(countrycode.upper())
    if not entries:
        return None
    target = _norm(state_text)
    if not target:
        return None
    # 완전 일치 우선
    for name, lat, lng, _ in entries:
        if name == target:
            return lat, lng
    # 부분 일치 (도시명이 state 텍스트에 포함되는 경우, 예: "Seoul, South Korea")
    for name, lat, lng, _ in entries:
        if len(name) >= 4 and name in target:
            return lat, lng
    return None


def area_to_radius_km(area_km2: float | None) -> float:
    """
    국가 면적을 반지름(km)으로 환산 — 원 면적 공식 역산 후 여유를 좀 둠.
    최소 30km(작은 나라도 마커가 한 점에 안 몰리게), 최대 600km(러시아 등
    초대형 국가가 화면을 너무 뒤덮지 않게) 캡. city_pool이 아예 없는 극히
    예외적인 경우(초소형 섬나라 등)의 최후 수단으로만 쓰인다.
    """
    if not area_km2 or area_km2 <= 0:
        return 80.0
    radius = math.sqrt(area_km2 / math.pi) * 0.5
    return max(30.0, min(600.0, radius))


def _weighted_pick(rng: random.Random, weights: list[float]) -> int:
    total = sum(weights)
    r = rng.uniform(0, total)
    upto = 0.0
    for i, w in enumerate(weights):
        upto += w
        if upto >= r:
            return i
    return len(weights) - 1


def natural_point_in_country(
    cc: str,
    info: CountryInfo | None,
    city_pool: dict,
    rng: random.Random,
) -> tuple[float, float]:
    """
    수도 중심의 균일한 원판에 뿌리면 실제 방송국 분포와 다르게 '인위적인
    동심원'처럼 보이는 문제가 있었다. 대신 그 나라의 실제 도시(인구 15,000명
    이상, GeoNames)를 인구수 가중치로 하나 뽑고, 그 도시 근처(반경 2~15km,
    도시 인구가 클수록 더 넓게 흩어지도록)에 살짝 지터를 줘서 배치한다.
    → 인구 많은 도시 주변에 자연스럽게 더 몰리는, 실제 방송국 분포와 비슷한
    모양이 된다.
    """
    pool = city_pool.get(cc)
    if pool and pool.lats:
        idx = _weighted_pick(rng, pool.weights)
        city_lat, city_lng, pop = pool.lats[idx], pool.lngs[idx], pool.weights[idx]
        # 인구가 많은 도시일수록 조금 더 넓게 퍼뜨림 (2km~15km)
        max_jitter_km = min(15.0, 2.0 + math.log10(max(pop, 10.0)))

        # 지터를 주다 보면 해안 도시는 확률적으로 바다로 튈 수 있다. 반경을
        # 점점 줄여가며 육지에 떨어질 때까지 재시도하고, 그래도 안 되면
        # 지터 없이 도시 좌표 그대로(거의 항상 육지) 사용한다.
        for attempt_radius in (max_jitter_km, max_jitter_km * 0.5, max_jitter_km * 0.2, 0.0):
            r = attempt_radius * math.sqrt(rng.random()) if attempt_radius > 0 else 0.0
            theta = rng.uniform(0, 2 * math.pi)
            dlat = km_to_deg_lat(r) * math.sin(theta)
            dlng = km_to_deg_lng(r, city_lat) * math.cos(theta)
            cand_lat, cand_lng = city_lat + dlat, city_lng + dlng
            if attempt_radius == 0.0 or is_on_land(cand_lat, cand_lng):
                return cand_lat, cand_lng
        return city_lat, city_lng  # 안전망

    # city_pool에 아예 도시가 없는 극히 드문 경우(초소형 국가/영토) — 예전
    # 방식(수도 중심 원판)으로 대체. info가 없으면 그것도 최후의 수단으로 처리.
    if info is None or info.capital_lat is None or info.capital_lng is None:
        return rng.uniform(-50, 50), rng.uniform(-170, 170)
    radius_km = area_to_radius_km(info.area_km2 if info else None)
    for attempt_radius in (radius_km, radius_km * 0.5, radius_km * 0.2, 0.0):
        r = attempt_radius * math.sqrt(rng.random()) if attempt_radius > 0 else 0.0
        theta = rng.uniform(0, 2 * math.pi)
        dlat = km_to_deg_lat(r) * math.sin(theta)
        dlng = km_to_deg_lng(r, info.capital_lat) * math.cos(theta)
        cand_lat, cand_lng = info.capital_lat + dlat, info.capital_lng + dlng
        if attempt_radius == 0.0 or is_on_land(cand_lat, cand_lng):
            return cand_lat, cand_lng
    return info.capital_lat, info.capital_lng


# --------------------------------------------------------------------------
# Radio Browser에서 전체 방송국 수집
# --------------------------------------------------------------------------

def fetch_all_stations(client: httpx.Client) -> list[dict]:
    print("Radio Browser에서 전체 방송국 목록 가져오는 중...")
    resp = client.get(
        f"{RADIO_BROWSER_HOST}/json/stations",
        params={"hidebroken": "true", "order": "clickcount", "reverse": "true", "limit": 100000},
        timeout=120,
    )
    resp.raise_for_status()
    stations = resp.json()
    print(f"  {len(stations)}개 방송국 수신")
    if not stations:
        raise RuntimeError(
            "Radio Browser가 빈 목록을 반환했습니다. API가 일시적으로 죽었거나 "
            "요청이 막혔을 수 있습니다 — 잠시 후 다시 시도해보세요."
        )

    # stationuuid 중복 제거 (Radio Browser가 같은 uuid를 중복으로 줄 때가 있음 —
    # 이전 버전에서 이것 때문에 배치 upsert가 통째로 실패해 KR/JP/US/CN이
    # 통째로 누락됐던 버그가 있었음. 여기서 한 번 더 방어.)
    seen: set[str] = set()
    deduped = []
    for s in stations:
        uuid = s.get("stationuuid")
        if not uuid or uuid in seen:
            continue
        seen.add(uuid)
        deduped.append(s)
    print(f"  중복 제거 후 {len(deduped)}개")
    return deduped


# --------------------------------------------------------------------------
# Supabase REST(PostgREST) 직접 호출로 upsert
# --------------------------------------------------------------------------

def supabase_upsert(client: httpx.Client, rows: list[dict], batch_size: int = 200) -> None:
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }
    url = f"{SUPABASE_URL}/rest/v1/radio_stations?on_conflict=stationuuid"

    for i in range(0, len(rows), batch_size):
        batch = rows[i : i + batch_size]
        resp = client.post(url, json=batch, headers=headers, timeout=30)
        if resp.status_code >= 300:
            print(f"  ⚠️ upsert 실패 (rows {i}-{i+len(batch)}): {resp.status_code} {resp.text[:300]}")
        else:
            print(f"  ✓ {i + len(batch)}/{len(rows)} 적재")


# --------------------------------------------------------------------------
# 메인 파이프라인
# --------------------------------------------------------------------------

def build_row(
    s: dict,
    lat: float,
    lng: float,
    geo_source: str,
) -> dict:
    return {
        "stationuuid": s["stationuuid"],
        "name": s.get("name", "")[:200],
        "url": s.get("url_resolved") or s.get("url") or "",
        "favicon": s.get("favicon") or None,
        "tags": s.get("tags", ""),
        "country": s.get("country", ""),
        "countrycode": (s.get("countrycode") or "").upper(),
        "votes": s.get("votes", 0),
        "geo_lat": lat,
        "geo_long": lng,
        "is_synthetic_geo": geo_source != "exact",
        "geo_source": geo_source,  # 'exact' | 'city' | 'country'
        "is_hidden": False,
    }


def main() -> None:
    rng = random.Random(42)  # 재실행해도 같은 임의배치가 나오도록 시드 고정

    with httpx.Client(headers={"User-Agent": USER_AGENT}, follow_redirects=True) as client:
        stations = fetch_all_stations(client)
        cities_by_country, city_pool = load_geonames_cities(client)
        countries = load_country_info(client)
        fill_capital_coords(countries, cities_by_country)

        occupancy = OccupancyTracker()
        rows: list[dict] = []

        remaining: list[dict] = []

        # ---- 1차: 실좌표 있는 것 ----
        print("\n[1차] 실좌표 있는 방송국 배치 중...")
        stage1_count = 0
        stage1_rejected_water = 0
        for s in stations:
            lat, lng = s.get("geo_lat"), s.get("geo_long")
            if lat is None or lng is None or (lat == 0 and lng == 0):
                remaining.append(s)
                continue
            lat, lng = float(lat), float(lng)
            if not is_on_land(lat, lng):
                # Radio Browser 실좌표가 바다 위(부정확한 제출 데이터) → 못 믿을
                # 좌표로 보고 2차/3차(도시 매칭/국가 임의배치)로 넘긴다.
                stage1_rejected_water += 1
                remaining.append(s)
                continue
            plat, plng = occupancy.place(lat, lng)
            rows.append(build_row(s, plat, plng, "exact"))
            stage1_count += 1
        print(
            f"  1차 완료: {stage1_count}개 "
            f"(바다 위라 거부: {stage1_rejected_water}개, "
            f"실좌표 없음: {len(remaining) - stage1_rejected_water}개 → 2차로 넘김)"
        )

        # ---- 2차: 국가+도시명 매칭 ----
        print("\n[2차] 도시명 매칭 배치 중...")
        still_remaining: list[dict] = []
        stage2_count = 0
        for s in remaining:
            cc = (s.get("countrycode") or "").upper()
            state = s.get("state") or ""
            match = match_city(cities_by_country, cc, state) if cc else None
            if match is None:
                still_remaining.append(s)
                continue
            plat, plng = occupancy.place(match[0], match[1])
            rows.append(build_row(s, plat, plng, "city"))
            stage2_count += 1
        print(f"  2차 완료: {stage2_count}개 (도시 매칭 안 된 {len(still_remaining)}개는 3차로 넘김)")

        # ---- 3차: 국가만 있고 도시 정보 없음 → 국가 내 임의 배치 ----
        print("\n[3차] 국가 기준 임의 배치 중...")
        stage3_count = 0
        skipped = 0
        for s in still_remaining:
            cc = (s.get("countrycode") or "").upper()
            if not cc:
                skipped += 1
                continue
            info = countries.get(cc)
            if info is None and cc not in city_pool:
                skipped += 1
                continue
            lat, lng = natural_point_in_country(cc, info, city_pool, rng)
            plat, plng = occupancy.place(lat, lng)
            rows.append(build_row(s, plat, plng, "country"))
            stage3_count += 1
        print(f"  3차 완료: {stage3_count}개 (국가 정보 자체가 없어서 스킵: {skipped}개)")

        print(f"\n총 {len(rows)}개 행 upsert 시작 (1차 {stage1_count} / 2차 {stage2_count} / 3차 {stage3_count})")
        supabase_upsert(client, rows)

    print("\n완료.")


if __name__ == "__main__":
    main()
