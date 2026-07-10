"""
countries 테이블 시딩 스크립트 (v2)
- REST Countries 라이브 API 대신, GitHub에 정적으로 호스팅된 안정적인 오픈 데이터셋 사용
  (라이브 API가 v3.1 -> v5로 변경되며 계속 깨지는 문제를 회피)
- 소스 1: mledoze/countries (이름/수도/언어/통화/면적/좌표/접경국/일부 언어 번역)
- 소스 2: samayo/country-json (인구, 국가명 기준 매칭)
- 국기 이미지: flagcdn.com CDN URL 규칙으로 직접 생성 (별도 fetch 불필요)
- radio_ingest.py와 동일 패턴: httpx로 Supabase REST API 직접 호출

사용법:
    pip install httpx python-dotenv --break-system-packages
    .env 파일에 SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY 설정 필요 (radio_ingest.py와 동일)
    python country_seed.py
"""

import httpx
import os
import time
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "resolution=merge-duplicates",  # upsert
}

COUNTRIES_URL = "https://raw.githubusercontent.com/mledoze/countries/master/countries.json"
POPULATION_URL = "https://raw.githubusercontent.com/samayo/country-json/master/src/country-by-population.json"


def fetch_json(url: str):
    with httpx.Client(timeout=30, follow_redirects=True) as client:
        resp = client.get(url)
        resp.raise_for_status()
        return resp.json()


def build_population_map(pop_list: list[dict]) -> dict:
    return {p["country"].strip().lower(): p["population"] for p in pop_list if p.get("country")}


def find_population(country: dict, pop_map: dict):
    candidates = [country["name"]["common"], country["name"]["official"]] + country.get("altSpellings", [])
    for cand in candidates:
        key = cand.strip().lower()
        if key in pop_map:
            return pop_map[key]
    return None


def transform(country: dict, pop_map: dict) -> dict:
    iso2 = country.get("cca2")
    idd = country.get("idd", {})
    root = idd.get("root", "") or ""
    suffixes = idd.get("suffixes", [])
    calling_code = (root + suffixes[0]) if root and suffixes else (root or None)

    return {
        "iso2": iso2,
        "iso3": country.get("cca3"),
        "name_common": country["name"].get("common"),
        "name_official": country["name"].get("official"),
        "capital": (country.get("capital") or [None])[0],
        "region": country.get("region"),
        "subregion": country.get("subregion"),
        "languages": list(country.get("languages", {}).values()) or None,
        "currencies": country.get("currencies") or None,
        "area_km2": country.get("area"),
        "population": find_population(country, pop_map),
        "flag_url": f"https://flagcdn.com/{iso2.lower()}.svg" if iso2 else None,
        "calling_code": calling_code,
        "timezones": None,  # 이 데이터셋엔 시간대 정보 없음. 필요시 추후 별도 보강
        "latlng": country.get("latlng") or None,
        "borders": country.get("borders") or None,
    }


def upsert_batch(rows: list[dict]):
    url = f"{SUPABASE_URL}/rest/v1/countries"
    with httpx.Client(timeout=30) as client:
        resp = client.post(url, headers=HEADERS, json=rows)
        if resp.status_code not in (200, 201):
            print(f"  실패 ({resp.status_code}): {resp.text[:300]}")
        else:
            print(f"  {len(rows)}개국 upsert 성공")


def main():
    print("mledoze/countries 데이터 가져오는 중...")
    countries = fetch_json(COUNTRIES_URL)
    print(f"총 {len(countries)}개국 확보")

    print("인구 데이터 가져오는 중...")
    pop_map = build_population_map(fetch_json(POPULATION_URL))

    rows = [transform(c, pop_map) for c in countries if c.get("cca2")]

    no_pop = sum(1 for r in rows if r["population"] is None)
    print(f"인구 데이터 매칭 안 된 나라: {no_pop}개국 (수동 보강 필요)")

    batch_size = 50
    for i in range(0, len(rows), batch_size):
        batch = rows[i : i + batch_size]
        print(f"배치 {i // batch_size + 1} 처리 중 ({len(batch)}개국)...")
        upsert_batch(batch)
        time.sleep(0.5)

    print("완료. Supabase countries 테이블 확인해주세요.")


if __name__ == "__main__":
    main()
