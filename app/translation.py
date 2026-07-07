"""
Language detection + Google Translate integration for real-time chat.

Approach (per Ryker's decision, July 2026):
  - The default target language is derived from the receiver's stored
    nationality (`users.country_code`) via COUNTRY_TO_LANGUAGE.
  - Falls back to English if the country isn't mapped or country_code
    is missing (most existing test accounts have no country_code yet).
  - A user can override "translate incoming messages into ___" at any
    time via a button in the app — this takes priority over the
    country-based default. Overrides are in-memory only for now
    (reset on server restart); worth persisting to `users` later if
    this sees heavy use.
"""
import os
import httpx

GOOGLE_TRANSLATE_URL = "https://translation.googleapis.com/language/translate/v2"
GOOGLE_TRANSLATE_API_KEY = os.environ.get("GOOGLE_TRANSLATE_API_KEY", "")

# ISO 3166-1 alpha-2 country code -> primary language for Google Translate.
# Not exhaustive — unmapped countries (or missing country_code) fall
# back to English. Add more entries here as SIGNAL's user base grows.
COUNTRY_TO_LANGUAGE = {
    "KR": "ko",
    "JP": "ja",
    "CN": "zh-CN", "TW": "zh-TW", "HK": "zh-TW",
    "US": "en", "GB": "en", "AU": "en", "CA": "en", "NZ": "en", "IE": "en",
    "SG": "en", "IN": "en", "PH": "en",
    "FR": "fr", "BE": "fr",
    "DE": "de", "AT": "de", "CH": "de",
    "ES": "es", "MX": "es", "AR": "es", "CO": "es", "CL": "es", "PE": "es",
    "IT": "it",
    "PT": "pt", "BR": "pt",
    "NL": "nl",
    "SE": "sv",
    "NO": "no",
    "DK": "da",
    "FI": "fi",
    "PL": "pl",
    "RU": "ru",
    "TR": "tr",
    "GR": "el",
    "TH": "th",
    "VN": "vi",
    "ID": "id",
    "MY": "ms",
    "AE": "ar", "SA": "ar", "EG": "ar",
    "IL": "he",
    "UA": "uk",
    "CZ": "cs",
    "RO": "ro",
    "HU": "hu",
}

# user_id -> language code override, set via the "choose recipient's
# language" button in the app. Takes priority over country_code lookup.
_user_language_overrides: dict[str, str] = {}


def set_user_language_override(user_id: str, lang_code: str) -> None:
    _user_language_overrides[user_id] = lang_code


def clear_user_language_override(user_id: str) -> None:
    _user_language_overrides.pop(user_id, None)


def resolve_target_language(user_id: str, country_code: str | None) -> str:
    """
    Decides what language incoming messages should be translated into
    for this user: manual override first, then country_code mapping,
    then English as the final fallback.
    """
    if user_id in _user_language_overrides:
        return _user_language_overrides[user_id]
    if country_code:
        return COUNTRY_TO_LANGUAGE.get(country_code.upper(), "en")
    return "en"


async def translate_text(text: str, target_lang: str) -> str:
    """
    Translates `text` into `target_lang` via Google Translate.
    Source language is auto-detected (we don't know the sender's
    language for certain either, so auto-detect is the simplest
    correct choice). Falls back to the original text if the API key
    isn't configured or the call fails, so a translate outage never
    blocks the chat itself.
    """
    if not GOOGLE_TRANSLATE_API_KEY:
        return text
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            res = await client.post(
                GOOGLE_TRANSLATE_URL,
                params={"key": GOOGLE_TRANSLATE_API_KEY},
                json={"q": text, "target": target_lang, "format": "text"},
            )
            res.raise_for_status()
            data = res.json()
            return data["data"]["translations"][0]["translatedText"]
    except (httpx.HTTPError, KeyError, IndexError):
        return text
