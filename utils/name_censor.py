"""Utilities for detecting profanity using the Bad Words API.

This module wraps the APILayer Bad Words API so application code can check
for profanity and react using simple boolean semantics. The main entry point
is :func:`contains_profanity`, which returns ``True`` when the supplied text
contains profanity and ``False`` otherwise.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Tuple

import requests
from requests import Response, Session
from requests.exceptions import RequestException

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 5.0
_DEFAULT_URL = "https://api.apilayer.com/bad_words"
_RATE_LIMIT_HEADERS = {
    "x-ratelimit-limit-day": "limit_day",
    "x-ratelimit-remaining-day": "remaining_day",
    "x-ratelimit-limit-month": "limit_month",
    "x-ratelimit-remaining-month": "remaining_month",
}


def _parse_timeout(value: Optional[str]) -> float:
    if not value:
        return _DEFAULT_TIMEOUT
    try:
        parsed = float(value)
        if parsed <= 0:
            raise ValueError
        return parsed
    except ValueError:
        logger.warning("Invalid BAD_WORDS_API_TIMEOUT value '%s'; falling back to %.1fs", value, _DEFAULT_TIMEOUT)
        return _DEFAULT_TIMEOUT


_CONFIGURED_TIMEOUT = _parse_timeout(os.getenv("BAD_WORDS_API_TIMEOUT"))
_CONFIGURED_URL = os.getenv("BAD_WORDS_API_URL", _DEFAULT_URL)
_CONFIGURED_KEY = os.getenv("BAD_WORDS_API_KEY")
_SESSION: Optional[Session] = None


class BadWordsAPIError(RuntimeError):
    """Raised when the Bad Words API cannot be accessed or returns an error."""

    def __init__(self, message: str, *, status_code: Optional[int] = None, response: Optional[Response] = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response = response


@dataclass(frozen=True)
class RateLimitInfo:
    """Represents the rate limit metadata returned by the API."""

    limit_day: Optional[int] = None
    remaining_day: Optional[int] = None
    limit_month: Optional[int] = None
    remaining_month: Optional[int] = None


@dataclass(frozen=True)
class ProfanityCheckResult:
    """Structured representation of a profanity check result."""

    contains_profanity: bool
    original_text: str
    censored_text: Optional[str] = None
    bad_words: Tuple[str, ...] = field(default_factory=tuple)
    raw_response: Mapping[str, Any] = field(default_factory=dict)
    rate_limit: Optional[RateLimitInfo] = None


def _get_session() -> Session:
    global _SESSION
    if _SESSION is None:
        _SESSION = requests.Session()
    return _SESSION


def _resolve_api_key(override: Optional[str]) -> str:
    key = override or _CONFIGURED_KEY
    if not key:
        raise BadWordsAPIError("Bad Words API key is not configured. Set BAD_WORDS_API_KEY.")
    return key


def _extract_rate_limit_info(response: Response) -> Optional[RateLimitInfo]:
    header_values: Dict[str, Optional[int]] = {}
    has_value = False
    for header_name, field_name in _RATE_LIMIT_HEADERS.items():
        raw = response.headers.get(header_name)
        if raw is None:
            header_values[field_name] = None
            continue
        try:
            header_values[field_name] = int(raw)
            has_value = True
        except (TypeError, ValueError):
            logger.debug("Unable to parse rate limit header %s=%s", header_name, raw)
            header_values[field_name] = None
    if not has_value:
        return None
    return RateLimitInfo(
        limit_day=header_values["limit_day"],
        remaining_day=header_values["remaining_day"],
        limit_month=header_values["limit_month"],
        remaining_month=header_values["remaining_month"],
    )


def _extract_bad_words(payload: Mapping[str, Any]) -> Tuple[str, ...]:
    candidates: Tuple[str, ...] = tuple()

    bad_words = payload.get("bad_words")
    if isinstance(bad_words, list):
        extracted = [word for word in bad_words if isinstance(word, str)]
        if extracted:
            candidates = tuple(extracted)

    if candidates:
        return candidates

    bad_words_list = payload.get("bad_words_list")
    if isinstance(bad_words_list, list):
        extracted = []
        for entry in bad_words_list:
            if isinstance(entry, dict):
                original = entry.get("original")
                if isinstance(original, str):
                    extracted.append(original)
                    continue
                word = entry.get("word")
                if isinstance(word, str):
                    extracted.append(word)
            elif isinstance(entry, str):
                extracted.append(entry)
        if extracted:
            candidates = tuple(extracted)

    return candidates


def _determine_profanity(payload: Mapping[str, Any], censored_text: Optional[str], original_text: str) -> bool:
    total = payload.get("bad_words_total")
    if isinstance(total, int):
        return total > 0

    for key in ("has_profanity", "contains_profanity", "contains_bad_words", "profanity"):
        flag = payload.get(key)
        if isinstance(flag, bool):
            return flag

    bad_words = _extract_bad_words(payload)
    if bad_words:
        return True

    if isinstance(censored_text, str) and censored_text != original_text:
        return True

    return False


def check_text(
    text: str,
    *,
    api_key: Optional[str] = None,
    censor_character: Optional[str] = None,
    timeout: Optional[float] = None,
    session: Optional[Session] = None,
    api_url: Optional[str] = None,
) -> ProfanityCheckResult:
    """Call the Bad Words API and return a structured result.

    Args:
        text: Text to check for profanity.
        api_key: Override the API key for this call. Defaults to ``BAD_WORDS_API_KEY``.
        censor_character: Optional censor character passed to the API's ``censor_character`` query parameter.
        timeout: Socket timeout for the request. Defaults to ``BAD_WORDS_API_TIMEOUT`` environment variable.
        session: Optional :class:`requests.Session` to reuse connections.
        api_url: Override the API URL. Defaults to ``BAD_WORDS_API_URL``.

    Raises:
        BadWordsAPIError: If the API cannot be reached or returns an error.

    Returns:
        A :class:`ProfanityCheckResult` instance describing the outcome.
    """
    if not text:
        return ProfanityCheckResult(
            contains_profanity=False,
            original_text=text,
            censored_text=None,
            bad_words=tuple(),
            raw_response={},
            rate_limit=None,
        )

    resolved_key = _resolve_api_key(api_key)
    resolved_timeout = timeout if timeout is not None else _CONFIGURED_TIMEOUT
    resolved_url = api_url or _CONFIGURED_URL

    params = {}
    if censor_character is not None:
        params["censor_character"] = censor_character

    request_session = session or _get_session()
    headers = {"apikey": resolved_key}

    try:
        response = request_session.post(
            resolved_url,
            params=params,
            data={"body": text},
            headers=headers,
            timeout=resolved_timeout,
        )
    except RequestException as exc:
        raise BadWordsAPIError(f"Failed to reach Bad Words API at {resolved_url}") from exc

    if response.status_code == 429:
        raise BadWordsAPIError("Bad Words API rate limit exceeded", status_code=response.status_code, response=response)

    if not response.ok:
        message = f"Bad Words API returned {response.status_code}"
        try:
            error_body = response.json()
            error_message = error_body.get("message")
            if error_message:
                message = f"{message}: {error_message}"
        except ValueError:
            error_message = response.text
            if error_message:
                message = f"{message}: {error_message}"
        raise BadWordsAPIError(message, status_code=response.status_code, response=response)

    try:
        payload = response.json()
    except ValueError as exc:
        raise BadWordsAPIError("Bad Words API returned invalid JSON payload", response=response) from exc

    if not isinstance(payload, Mapping):
        raise BadWordsAPIError("Bad Words API returned unexpected payload type", response=response)

    censored_text = None
    for key in ("censored_content", "censored", "result"):
        value = payload.get(key)
        if isinstance(value, str):
            censored_text = value
            break

    bad_words = _extract_bad_words(payload)
    contains_profanity = _determine_profanity(payload, censored_text, text)
    rate_limit = _extract_rate_limit_info(response)

    return ProfanityCheckResult(
        contains_profanity=contains_profanity,
        original_text=text,
        censored_text=censored_text,
        bad_words=bad_words,
        raw_response=dict(payload),
        rate_limit=rate_limit,
    )


def contains_profanity(
    text: str,
    *,
    api_key: Optional[str] = None,
    censor_character: Optional[str] = None,
    timeout: Optional[float] = None,
    session: Optional[Session] = None,
    api_url: Optional[str] = None,
    fail_open: bool = False,
) -> bool:
    """Convenience wrapper that returns ``True`` when profanity is detected.

    Args:
        text: Text to check for profanity.
        api_key: Optional API key override.
        censor_character: Optional censor character to forward to the API.
        timeout: Optional request timeout override.
        session: Optional reusable :class:`requests.Session`.
        api_url: Optional Bad Words API URL override.
        fail_open: When ``True``, errors contacting the API are logged and ``False``
            is returned. When ``False`` (default), errors raise :class:`BadWordsAPIError`.

    Returns:
        ``True`` if profanity was detected, ``False`` otherwise.
    """
    try:
        result = check_text(
            text,
            api_key=api_key,
            censor_character=censor_character,
            timeout=timeout,
            session=session,
            api_url=api_url,
        )
        return result.contains_profanity
    except BadWordsAPIError:
        if fail_open:
            logger.warning("Bad Words API unavailable; treating '%s' as clean due to fail_open=True", text)
            return False
        raise