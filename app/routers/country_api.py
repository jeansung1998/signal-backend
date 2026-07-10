"""
app/routers/country_api.py

국가 정보 조회 API
- countries 테이블에서 국가 상세 정보 반환
- 국경선 클릭 -> 정보 카드 표시용 (SIGNAL 프론트엔드 "국가" 카테고리)

주의: 이 파일은 Supabase에 직접 httpx로 REST 호출하는 방식으로 작성했습니다.
      기존 프로젝트에 이미 공용 Supabase 클라이언트/헬퍼(예: database.py)가 있다면
      아래 SUPABASE_URL/SUPABASE_KEY 및 httpx 호출 부분을 그 헬퍼로 교체해서 쓰시면 됩니다.
"""

import os
import httpx
from fastapi import APIRouter, HTTPException
from typing import Optional

router = APIRouter(prefix="/countries", tags=["countries"])

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
}

# 앱 상단 언어 선택기와 맞춘 지원 언어 코드 (description_* 컬럼 접미사와 동일해야 함)
SUPPORTED_LANGS = {
    "ko", "en", "ja", "zh", "es", "ru", "fr", "de",
    "pt", "vi", "th", "id", "ar", "hi", "it",
}


@router.get("/{iso2}")
async def get_country(iso2: str, lang: Optional[str] = "ko"):
    """
    단일 국가 정보 조회
    - iso2: ISO 3166-1 alpha-2 코드 (예: KR, US, TH) — 대소문자 무관
    - lang: 설명 텍스트 언어 (기본 ko). 지원 안 하는 언어면 ko로 폴백
    """
    iso2 = iso2.upper()
    if lang not in SUPPORTED_LANGS:
        lang = "ko"

    url = f"{SUPABASE_URL}/rest/v1/countries"
    params = {"iso2": f"eq.{iso2}", "select": "*"}

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(url, headers=HEADERS, params=params)

    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="국가 정보 조회 실패")

    rows = resp.json()
    if not rows:
        raise HTTPException(status_code=404, detail=f"국가 코드 '{iso2}'를 찾을 수 없습니다")

    country = rows[0]

    # 요청 언어에 맞는 description 컬럼 선택, 없으면 한국어로 폴백
    desc_key = f"description_{lang}"
    description = country.get(desc_key) or country.get("description_ko")

    return {
        "iso2": country["iso2"],
        "iso3": country["iso3"],
        "name": country["name_common"],
        "name_official": country["name_official"],
        "capital": country["capital"],
        "region": country["region"],
        "subregion": country["subregion"],
        "languages": country["languages"],
        "currencies": country["currencies"],
        "area_km2": country["area_km2"],
        "population": country["population"],
        "flag_url": country["flag_url"],
        "calling_code": country["calling_code"],
        "latlng": country["latlng"],
        "borders": country["borders"],
        "description": description,
    }


@router.get("")
async def list_countries():
    """
    전체 국가 목록 (경량 — 이름/코드/좌표만) — 프론트에서 검색/자동완성 등에 사용
    """
    url = f"{SUPABASE_URL}/rest/v1/countries"
    params = {"select": "iso2,iso3,name_common,latlng,flag_url", "is_hidden": "eq.false"}

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(url, headers=HEADERS, params=params)

    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="국가 목록 조회 실패")

    return resp.json()
