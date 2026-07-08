"""
Pydantic request/response schemas.
Kept separate from the DB schema (schema.sql) on purpose — the API
contract and the storage layout are allowed to drift slightly.
"""
from datetime import datetime
from pydantic import BaseModel


class UserProfile(BaseModel):
    id: str
    nickname: str
    photo_url: str | None = None
    intro: str | None = None
    greeting_message: str | None = None
    country_code: str | None = None


class UserProfileUpdate(BaseModel):
    nickname: str | None = None
    photo_url: str | None = None
    intro: str | None = None
    greeting_message: str | None = None
    country_code: str | None = None


class PresenceUpdate(BaseModel):
    user_id: str
    city: str
    lat: float
    lng: float


class PresenceCity(BaseModel):
    city: str
    country: str
    lat: float
    lng: float
    online_count: int


class MatchRequestCreate(BaseModel):
    from_user_id: str
    to_user_id: str


class MatchRequestResponse(BaseModel):
    id: str
    from_user_id: str
    to_user_id: str
    status: str  # pending | accepted | rejected
    created_at: datetime
