"""Calm Avito catalog client shared by every search.

Includes automatic proxy rotation every 3 requests and instant recovery
on IP blocks or captchas without waiting 10 minutes.
"""

from __future__ import annotations

import os
import random
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlencode

from curl_cffi.requests.exceptions import (
    ConnectionError as CurlConnectionError,
    IncompleteRead,
    ProxyError as CurlProxyError,
    Timeout as CurlTimeout,
)

import main
from proxy_rotator import FreeProxyRotator

DEFAULT_DELAY_SECONDS = 30.0
MAX_POW_IN_A_ROW = 3
MAX_SESSION_RESETS_PER_PAGE = 8
MAX_OPEN_SESSION_ATTEMPTS = 15
FULL_PAGE_SIZE = 50
ROTATE_IP_EVERY_REQUESTS = 3

TRANSPORT_ERRORS = (IncompleteRead, CurlConnectionError, CurlTimeout, CurlProxyError)


@dataclass(frozen=True)
class Page:
    number: int
    items: tuple[dict[str, Any], ...]
    total_count: int
    items_on_page: int

    @property
    def is_last(self) -> bool:
        # Do not treat a transient empty page as end-of-results when Avito
        # reports that listings still exist. The caller validates that state.
        return (
            self.total_count > 0 and self.number * FULL_PAGE_SIZE >= self.total_count
        ) or (self.total_count == 0 and self.items_on_page == 0 and not self.items)


def items_url(params: dict[str, str], page: int) -> str:
    query = [(key, str(value)) for key, value in params.items()]
    query += [("p", str(page)), ("updateListOnly", "true")]
    return f"{main.ITEMS_URL}?{urlencode(query)}"


def response_kind(response: Any) -> str:
    status = response.status_code
    if status == 200:
        return "ok"
    if status == 439 or (status == 429 and main.response_has_pow_challenge(response)):
        return "pow"
    if main.is_firewall_captcha_dispatcher_response(response) or (
        status == 429 and main.is_geetest_firewall_html(response.text)
    ):
        return "captcha"
    return f"refused({status})"


class _ChallengeFromCookie:
    """Looks like the JSON 439 answer for main.run_pow_verification."""

    def __init__(self, challenge: str) -> None:
        self._challenge = challenge

    def json(self) -> dict[str, str]:
        return {"pow_challenge": self._challenge}


def pow_source(response: Any, session: Any) -> Any:
    """The 439 answer itself, or its challenge from the cookie."""
    if main.response_has_pow_challenge(response):
        return response
    challenge = session.cookies.get("pow_challenge") if session is not None else None
    return _ChallengeFromCookie(challenge) if challenge else response


class CalmClient:
    """One Avito session at a time, rotating proxy IP every 3 requests or on captcha."""

    def __init__(
        self,
        *,
        delay: float = DEFAULT_DELAY_SECONDS,
        rotate_every: int = ROTATE_IP_EVERY_REQUESTS,
        use_free_proxies: bool = True,
        log: Callable[..., None] | None = None,
    ) -> None:
        for variable in main.PROXY_ENVIRONMENT_VARIABLES:
            os.environ.pop(variable, None)
        self.delay = delay
        self.log = log or (lambda kind, **fields: None)
        self.session: Any | None = None
        self.referer: str | None = None
        self.last_request = 0.0
        self.sessions_opened = 0

        self.proxy_rotator: FreeProxyRotator | None = (
            FreeProxyRotator(rotate_every=rotate_every, log=self.log)
            if use_free_proxies
            else None
        )

    def _close_session(self) -> None:
        if self.session is not None:
            try:
                self.session.close()
            except Exception:
                pass
            self.session = None

    def _pause(self) -> None:
        wait = self.delay * random.uniform(0.7, 1.3) - (time.monotonic() - self.last_request)
        if wait > 0:
            time.sleep(wait)

    def _get(self, url: str, *, document: bool) -> tuple[str, Any]:
        page_headers = {**main.REQUEST_HEADERS, "Referer": self.referer or main.REFERER}
        headers = main.DOCUMENT_REQUEST_HEADERS if document else page_headers
        for _ in range(MAX_POW_IN_A_ROW + 1):
            self._pause()
            response = main.get_with_qrator_recovery(
                self.session,
                url,
                session_headers=headers,
                request_headers=None if document else main.ITEMS_REQUEST_HEADERS,
                timeout_seconds=main.DOCUMENT_REQUEST_TIMEOUT_SECONDS,
                context="calm-client",
                stream_response_body=True,
            )
            self.last_request = time.monotonic()
            kind = response_kind(response)
            if kind != "pow":
                return kind, response
            main.run_pow_verification(self.session, pow_source(response, self.session))
        return "pow-loop", None

    def _open_session(self, catalog_url: str) -> None:
        """Open sessions until one loads the catalog page, rotating proxies instead of waiting."""
        for attempt in range(MAX_OPEN_SESSION_ATTEMPTS):
            if self.proxy_rotator:
                self.proxy_rotator.ensure_proxy_available()
                proxies = self.proxy_rotator.get_proxies_dict()
            else:
                proxies = None

            self.session = main.requests.Session(
                impersonate=main.HTTP_IMPERSONATE_PROFILE,
                proxies=proxies,
                trust_env=False,
            )
            main.set_session_headers(self.session, {**main.REQUEST_HEADERS, "Referer": catalog_url})
            self.referer = catalog_url

            try:
                kind, _ = self._get(catalog_url, document=True)
            except TRANSPORT_ERRORS as exc:
                self._close_session()
                if self.proxy_rotator:
                    self.proxy_rotator.mark_dead_and_rotate(reason=type(exc).__name__)
                    # Сбрасываем таймер паузы, чтобы сразу проверить следующий прокси без 30-секундного ожидания
                    self.last_request = 0.0
                    continue
                raise

            if kind == "ok":
                self.sessions_opened += 1
                return

            self._close_session()

            # Если прокси получил капчу или отказ — не ждем 10 минут, а сразу меняем IP
            if self.proxy_rotator:
                self.proxy_rotator.mark_dead_and_rotate(reason=f"blocked({kind})")
                self.last_request = 0.0
                if (attempt + 1) % 5 == 0:
                    self.proxy_rotator.refresh_pool(initial=False)
                continue

            # Если прокси принудительно отключены, делаем короткую передышку вместо 10 минут
            time.sleep(10)

        raise RuntimeError("Не удалось открыть сессию после перебора прокси; повторю в следующем цикле")

    def _tick_request_counter(self) -> None:
        """Меняет IP каждые 3 успешных запроса."""
        if self.proxy_rotator and self.proxy_rotator.register_request_and_check_rotation():
            self._close_session()

    def get_json(self, url: str, *, catalog_url: str, context: str = "request") -> dict[str, Any]:
        """GET a same-origin JSON endpoint, replacing the session and proxy after captchas."""
        for _ in range(MAX_SESSION_RESETS_PER_PAGE + 1):
            if self.session is None:
                self._open_session(catalog_url)
            self.referer = catalog_url
            try:
                kind, response = self._get(url, document=False)
            except TRANSPORT_ERRORS as exc:
                self.log("TRANSPORT", context=context, error=type(exc).__name__)
                if self.proxy_rotator:
                    self.proxy_rotator.mark_dead_and_rotate(reason=type(exc).__name__)
                self._close_session()
                self.last_request = 0.0
                continue
            if kind == "ok":
                payload = response.json()
                if not isinstance(payload, dict):
                    raise RuntimeError(f"{context}: answer is not a JSON object")
                self._tick_request_counter()
                return payload
            self.log("SESSION-RESET", reason=kind, context=context)
            if self.proxy_rotator:
                self.proxy_rotator.mark_dead_and_rotate(reason=kind)
            self._close_session()
            self.last_request = 0.0
        raise RuntimeError(f"{context}: no answer after {MAX_SESSION_RESETS_PER_PAGE} new sessions")

    def get_document(self, url: str, *, catalog_url: str) -> str | None:
        """HTML of a page such as a listing; None if Avito would not show it."""
        for _ in range(MAX_SESSION_RESETS_PER_PAGE + 1):
            if self.session is None:
                self._open_session(catalog_url)
            try:
                kind, response = self._get(url, document=True)
            except TRANSPORT_ERRORS as exc:
                self.log("TRANSPORT", context="item page", error=type(exc).__name__)
                if self.proxy_rotator:
                    self.proxy_rotator.mark_dead_and_rotate(reason=type(exc).__name__)
                self._close_session()
                self.last_request = 0.0
                continue
            if kind == "ok":
                self._tick_request_counter()
                return response.text
            if kind.startswith("refused"):
                self._tick_request_counter()
                return None  # removed listing (404) and the like
            self.log("SESSION-RESET", reason=kind, context="item page")
            if self.proxy_rotator:
                self.proxy_rotator.mark_dead_and_rotate(reason=kind)
            self._close_session()
            self.last_request = 0.0
        return None

    def page_payload(self, *, catalog_url: str, params: dict[str, str], number: int) -> dict[str, Any]:
        """The full items answer: listings, counters, filters, category tree."""
        return self.get_json(
            items_url(params, number), catalog_url=catalog_url, context=f"page {number}"
        )

    def page(self, *, catalog_url: str, params: dict[str, str], number: int) -> Page:
        """Fetch one catalog page of listings."""
        payload = self.page_payload(catalog_url=catalog_url, params=params, number=number)
        return page_from_payload(payload, number)


def page_from_payload(payload: dict[str, Any], number: int) -> Page:
    catalog = payload.get("catalog")
    if not isinstance(catalog, dict) or not isinstance(catalog.get("items"), list):
        raise ValueError("Ответ не содержит каталог объявлений; это не пустая выдача")
    items = catalog["items"]

    def counter(name: str) -> int:
        value = payload.get(name)
        if type(value) is not int or value < 0:
            raise ValueError(f"Ответ Авито не содержит корректный счётчик {name}")
        return value

    total_count = counter("totalCount")
    items_on_page = counter("itemsOnPage")
    parsed_items = tuple(
        item for item in items
        if isinstance(item, dict) and item.get("type", "item") == "item"
    )
    if items_on_page > 0 and not parsed_items:
        raise ValueError("Авито сообщил объявления на странице, но не вернул их список")
    if total_count > 0 and number == 1 and not parsed_items:
        raise ValueError(f"Авито сообщает {total_count} объявлений, но первая страница пуста")

    return Page(
        number=number,
        items=parsed_items,
        total_count=total_count,
        items_on_page=items_on_page,
    )
