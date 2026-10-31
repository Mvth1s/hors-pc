"""Client HTTP partagé : User-Agent identifiable, timeouts, retries et limite de débit par hôte."""

import logging
import time
from collections.abc import Callable
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from horspc import __version__

log = logging.getLogger(__name__)

USER_AGENT = f"HorsPC/{__version__} (+https://github.com/Mvth1s/hors-pc)"
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


class HttpClient:
    """Enveloppe httpx : au plus une requête par `min_interval` secondes vers un même hôte.

    `host_intervals` fixe un intervalle propre à certains hôtes (api.igdb.com : 4 req/s).
    """

    def __init__(
        self,
        *,
        timeout: float = 10.0,
        max_retries: int = 3,
        backoff: float = 2.0,
        min_interval: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        retry_statuses: frozenset[int] = RETRY_STATUSES,
        host_intervals: dict[str, float] | None = None,
    ) -> None:
        self._client = httpx.Client(
            timeout=httpx.Timeout(timeout),
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
        )
        self._max_retries = max_retries
        self._backoff = backoff
        self._min_interval = min_interval
        self._host_intervals = host_intervals or {}
        self._sleep = sleep
        self._clock = clock
        self._last_request: dict[str, float] = {}
        self._retry_statuses = retry_statuses

    def __enter__(self) -> "HttpClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """Envoie la requête avec retries ; lève httpx.HTTPError en cas d'échec définitif."""
        parsed = httpx.URL(url)
        host = parsed.host
        safe_url = parsed.copy_with(query=None)  # pour les logs, sans paramètres
        for attempt in range(self._max_retries + 1):
            self._throttle(host)
            try:
                response = self._client.request(method, url, **kwargs)
            except httpx.TransportError as exc:
                if attempt == self._max_retries:
                    raise
                delay = self._backoff * 2**attempt
                log.warning("%s %s : %s, nouvel essai dans %.0f s", method, safe_url, exc, delay)
                self._sleep(delay)
                continue
            if response.status_code in self._retry_statuses and attempt < self._max_retries:
                delay = _retry_after(response) or self._backoff * 2**attempt
                log.warning(
                    "%s %s : HTTP %d, nouvel essai dans %.0f s",
                    method,
                    safe_url,
                    response.status_code,
                    delay,
                )
                self._sleep(delay)
                continue
            response.raise_for_status()
            return response
        raise AssertionError("unreachable")

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("POST", url, **kwargs)

    def _throttle(self, host: str) -> None:
        last = self._last_request.get(host)
        if last is not None:
            interval = self._host_intervals.get(host, self._min_interval)
            wait = interval - (self._clock() - last)
            if wait > 0:
                self._sleep(wait)
        self._last_request[host] = self._clock()


def _retry_after(response: httpx.Response) -> float | None:
    """Délai demandé par l'en-tête Retry-After (secondes ou date HTTP), plafonné à 60 s."""
    value = response.headers.get("Retry-After")
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            seconds = parsedate_to_datetime(value).timestamp() - time.time()
        except (TypeError, ValueError):
            return None
    return min(max(seconds, 0.0), 60.0)
