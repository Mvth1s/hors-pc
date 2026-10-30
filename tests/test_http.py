import httpx
import pytest
import respx

from horspc.http import USER_AGENT, HttpClient
from horspc.igdb import HOST_INTERVALS


@respx.mock
def test_user_agent_identifiable(http: HttpClient) -> None:
    route = respx.get("https://example.org/").respond(200)
    http.request("GET", "https://example.org/")
    assert route.calls.last.request.headers["User-Agent"] == USER_AGENT
    assert "github.com/Mvth1s/hors-pc" in USER_AGENT


@respx.mock
def test_retry_sur_503_puis_succes(http: HttpClient, sleeps: list[float]) -> None:
    route = respx.get("https://example.org/").mock(
        side_effect=[httpx.Response(503), httpx.Response(200, text="ok")]
    )
    assert http.request("GET", "https://example.org/").text == "ok"
    assert route.call_count == 2
    assert 2.0 in sleeps


@respx.mock
def test_retry_after_respecte(http: HttpClient, sleeps: list[float]) -> None:
    respx.get("https://example.org/").mock(
        side_effect=[httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200)]
    )
    http.request("GET", "https://example.org/")
    assert 7.0 in sleeps


@respx.mock
def test_echec_definitif_apres_retries(http: HttpClient) -> None:
    route = respx.get("https://example.org/").respond(500)
    with pytest.raises(httpx.HTTPStatusError):
        http.request("GET", "https://example.org/")
    assert route.call_count == 4


@respx.mock
def test_erreur_reseau_relancee(http: HttpClient) -> None:
    respx.get("https://example.org/").mock(side_effect=httpx.ConnectTimeout("timeout"))
    with pytest.raises(httpx.ConnectTimeout):
        http.request("GET", "https://example.org/")


@respx.mock
def test_pas_de_retry_sur_404(http: HttpClient) -> None:
    route = respx.get("https://example.org/").respond(404)
    with pytest.raises(httpx.HTTPStatusError):
        http.request("GET", "https://example.org/")
    assert route.call_count == 1


@respx.mock
def test_une_requete_par_seconde_par_hote() -> None:
    sleeps: list[float] = []
    now = [100.0]
    respx.get("https://a.example/").respond(200)
    respx.get("https://b.example/").respond(200)
    with HttpClient(sleep=sleeps.append, clock=lambda: now[0]) as client:
        client.request("GET", "https://a.example/")
        now[0] += 0.25
        client.request("GET", "https://b.example/")
        client.request("GET", "https://a.example/")
    assert sleeps == [pytest.approx(0.75)]


@respx.mock
def test_intervalle_propre_a_api_igdb() -> None:
    """api.igdb.com à 4 req/s ; images.igdb.com et les autres hôtes restent à 1 req/s."""
    sleeps: list[float] = []
    respx.get("https://api.igdb.com/v4").respond(200)
    respx.get("https://images.igdb.com/x.webp").respond(200)
    with HttpClient(
        sleep=sleeps.append, clock=lambda: 100.0, host_intervals=HOST_INTERVALS
    ) as client:
        for _ in range(2):
            client.request("GET", "https://api.igdb.com/v4")
            client.request("GET", "https://images.igdb.com/x.webp")
    assert sleeps == [pytest.approx(0.25), pytest.approx(1.0)]
