import json
from pathlib import Path

import httpx
import pytest
import respx
import yaml

from horspc import build
from horspc.covers import IMAGE_URL
from horspc.http import HttpClient
from horspc.igdb import API_URL, TOKEN_URL, IgdbClient
from horspc.models import DataFile
from tests.conftest import FIXTURES, NOW, load_fixture
from tests.test_build import GAMES

WEBP = b"RIFF\x1a\x00\x00\x00WEBPVP8 \x0e\x00\x00\x00nouvelle-image"
OLD_WEBP = b"RIFF\x1a\x00\x00\x00WEBPVP8 \x0e\x00\x00\x00ancienne-image"
ASTRO_URL = IMAGE_URL.format(image_id="coba3k")


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    (tmp_path / "games.yaml").write_text(yaml.safe_dump(GAMES), encoding="utf-8")
    (tmp_path / "prices.yaml").write_text("[]", encoding="utf-8")
    return tmp_path


def set_previous(workdir: Path, image_id: str | None, with_file: bool = True) -> None:
    raw = json.loads((FIXTURES / "data_previous.json").read_text(encoding="utf-8"))
    raw["games"][0]["cover_image_id"] = image_id
    raw["games"][0]["cover_url"] = None if image_id is None else "covers/astro-bot.webp"
    (workdir / "data.json").write_text(json.dumps(raw), encoding="utf-8")
    if with_file:
        (workdir / "covers").mkdir(exist_ok=True)
        (workdir / "covers" / "astro-bot.webp").write_bytes(OLD_WEBP)


@pytest.fixture
def api():
    with respx.mock(assert_all_called=False) as router:
        router.post(TOKEN_URL).respond(json=load_fixture("igdb_token.json"))
        for endpoint, fixture in [
            ("platforms", "igdb_platforms.json"),
            ("release_date_regions", "igdb_release_regions.json"),
            ("release_date_statuses", "igdb_release_statuses.json"),
            ("games", "igdb_games.json"),
        ]:
            router.post(f"{API_URL}/{endpoint}").respond(json=load_fixture(fixture))
        yield router


def full_build(workdir: Path, http: HttpClient) -> dict:
    build.run(
        now=NOW,
        igdb=IgdbClient("id", "secret", http),
        http=http,
        games_path=workdir / "games.yaml",
        prices_path=workdir / "prices.yaml",
        output_path=workdir / "data.json",
    )
    return json.loads((workdir / "data.json").read_text(encoding="utf-8"))


def offline_build(workdir: Path) -> dict:
    build.run(
        now=NOW,
        igdb=None,
        games_path=workdir / "games.yaml",
        prices_path=workdir / "prices.yaml",
        output_path=workdir / "data.json",
    )
    return json.loads((workdir / "data.json").read_text(encoding="utf-8"))


def cover_file(workdir: Path) -> Path:
    return workdir / "covers" / "astro-bot.webp"


def test_premier_build_telecharge_en_webp(workdir: Path, http: HttpClient, api) -> None:
    route = api.get(ASTRO_URL).respond(content=WEBP, headers={"Content-Type": "image/webp"})
    data = full_build(workdir, http)
    astro, saros = data["games"]
    assert data["schema_version"] == 4
    assert (astro["cover_url"], astro["cover_image_id"]) == ("covers/astro-bot.webp", "coba3k")
    assert (saros["cover_url"], saros["cover_image_id"]) == (None, None)
    assert cover_file(workdir).read_bytes() == WEBP
    assert route.call_count == 1
    assert "images.igdb.com" not in (workdir / "data.json").read_text(encoding="utf-8")


def test_meme_image_id_pas_de_retelechargement(workdir: Path, http: HttpClient, api) -> None:
    set_previous(workdir, "coba3k")
    route = api.get(ASTRO_URL).respond(content=WEBP)
    full_build(workdir, http)
    assert route.call_count == 0
    assert cover_file(workdir).read_bytes() == OLD_WEBP


def test_image_id_change_retelechargement(workdir: Path, http: HttpClient, api) -> None:
    set_previous(workdir, "ancien")
    route = api.get(ASTRO_URL).respond(content=WEBP)
    data = full_build(workdir, http)
    assert route.call_count == 1
    assert cover_file(workdir).read_bytes() == WEBP
    assert data["games"][0]["cover_image_id"] == "coba3k"


def test_fichier_manquant_retelechargement(workdir: Path, http: HttpClient, api) -> None:
    set_previous(workdir, "coba3k", with_file=False)
    route = api.get(ASTRO_URL).respond(content=WEBP)
    full_build(workdir, http)
    assert route.call_count == 1


@pytest.mark.parametrize(
    "response",
    [httpx.Response(404), httpx.Response(200, content=b"\xff\xd8\xff\xe0 du JPEG")],
)
def test_echec_garde_l_ancienne_jaquette(
    workdir: Path, http: HttpClient, api, response: httpx.Response
) -> None:
    set_previous(workdir, "ancien")
    api.get(ASTRO_URL).mock(return_value=response)
    data = full_build(workdir, http)
    assert data["games"][0]["cover_image_id"] == "ancien"
    assert cover_file(workdir).read_bytes() == OLD_WEBP


def test_plus_de_cover_igdb_supprime_le_fichier(workdir: Path, http: HttpClient, api) -> None:
    set_previous(workdir, "coba3k")
    games = load_fixture("igdb_games.json")
    for game in games:
        game.pop("cover", None)
    api.post(f"{API_URL}/games").respond(json=games)
    data = full_build(workdir, http)
    assert data["games"][0]["cover_url"] is None
    assert not cover_file(workdir).exists()


def test_jaquette_orpheline_supprimee(workdir: Path, http: HttpClient, api) -> None:
    api.get(ASTRO_URL).respond(content=WEBP)
    (workdir / "covers").mkdir()
    (workdir / "covers" / "jeu-retire.webp").write_bytes(OLD_WEBP)
    full_build(workdir, http)
    assert not (workdir / "covers" / "jeu-retire.webp").exists()


def test_offline_reutilise_le_fichier_present(workdir: Path) -> None:
    set_previous(workdir, "coba3k")
    with respx.mock:  # aucun appel réseau autorisé
        data = offline_build(workdir)
    assert data["games"][0]["cover_url"] == "covers/astro-bot.webp"
    assert cover_file(workdir).read_bytes() == OLD_WEBP


def test_offline_fichier_absent_cover_null(workdir: Path) -> None:
    set_previous(workdir, "coba3k", with_file=False)
    data = offline_build(workdir)
    assert (data["games"][0]["cover_url"], data["games"][0]["cover_image_id"]) == (None, None)


def test_offline_ne_supprime_rien(workdir: Path) -> None:
    set_previous(workdir, None)
    (workdir / "covers" / "jeu-retire.webp").write_bytes(OLD_WEBP)
    offline_build(workdir)
    assert cover_file(workdir).exists()
    assert (workdir / "covers" / "jeu-retire.webp").exists()


def test_build_refuse_n_ecrit_aucune_jaquette(
    workdir: Path, http: HttpClient, api, monkeypatch: pytest.MonkeyPatch
) -> None:
    api.get(ASTRO_URL).respond(content=WEBP)

    def refuse(*args, **kwargs):
        return DataFile.model_validate({})

    monkeypatch.setattr(build, "build_data", refuse)
    with pytest.raises(Exception, match="validation error"):
        full_build(workdir, http)
    assert not cover_file(workdir).exists()
    assert not (workdir / "data.json").exists()


def test_cover_url_coherente_avec_l_image_id() -> None:
    raw = json.loads((FIXTURES / "data_previous.json").read_text(encoding="utf-8"))
    raw["games"][0]["cover_url"] = "https://images.igdb.com/igdb/image/upload/t_cover_big/x.webp"
    with pytest.raises(ValueError, match="cover_url"):
        DataFile.model_validate(raw)
    raw["games"][0]["cover_url"] = "covers/saros.webp"
    with pytest.raises(ValueError, match="cover_url"):
        DataFile.model_validate(raw)


def test_fixture_previous_valide() -> None:
    DataFile.model_validate_json((FIXTURES / "data_previous.json").read_bytes())


# --- Captures d'écran ------------------------------------------------------------------------


def test_captures_seulement_les_nouvelles(tmp_path: Path, http: HttpClient) -> None:
    from horspc.covers import SCREEN_URL, apply_screens, fetch_screens

    shots = tmp_path / "screens" / "astro-bot"
    shots.mkdir(parents=True)
    (shots / "ancienne.webp").write_bytes(OLD_WEBP)
    (shots / "gardee.webp").write_bytes(OLD_WEBP)
    with respx.mock:
        new = respx.get(SCREEN_URL.format(image_id="nouvelle")).respond(content=WEBP)
        kept = respx.get(SCREEN_URL.format(image_id="gardee")).respond(content=WEBP)
        update = fetch_screens(http, tmp_path, "astro-bot", ["gardee", "nouvelle"])
    assert update.image_ids == ("gardee", "nouvelle")
    assert (new.call_count, kept.call_count) == (1, 0)
    apply_screens(tmp_path, [update])
    assert sorted(p.name for p in shots.iterdir()) == ["gardee.webp", "nouvelle.webp"]
    assert (shots / "gardee.webp").read_bytes() == OLD_WEBP
