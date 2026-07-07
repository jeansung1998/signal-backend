"""
Bulk text translation for app-wide UI language switching.

Separate from the chat-message translation in ws.py/translation.py:
this endpoint lets the frontend send a batch of UI strings (button
labels, menu items, screen titles, etc.) and get them all translated
into the selected language in one call, so switching the whole app's
language doesn't mean one API call per label.

Not meant for translating external/user content like radio station
names, place names, or chat messages — those either come from
external sources (Radio Browser, `places`) and should stay as-is, or
already go through the separate chat translation path.
"""
from fastapi import APIRouter
from pydantic import BaseModel

from app.translation import translate_text

router = APIRouter()


class TranslateBatchRequest(BaseModel):
    texts: list[str]
    target_lang: str


@router.post("/translate/batch")
async def translate_batch(payload: TranslateBatchRequest):
    """
    e.g. POST /translate/batch
         {"texts": ["즐겨찾기", "설정", "라디오"], "target_lang": "ja"}
      -> {"translations": ["お気に入り", "設定", "ラジオ"]}

    Order of `translations` matches the order of `texts` so the
    frontend can zip them back onto the same UI-string keys.
    """
    translations = []
    for text in payload.texts:
        translations.append(await translate_text(text, payload.target_lang))
    return {"translations": translations}
