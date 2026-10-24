# HorsPC

Catalogue web des exclusivités **PS5, Switch et Switch 2 jamais sorties sur PC**, avec le prix le plus bas relevé en France. Le site est statique (HTML, CSS, JS vanilla) et lit un unique `site/data.json`, régénéré chaque semaine par GitHub Actions puis publié sur GitHub Pages.

## Architecture

```
data/
  games.yaml            liste curée des jeux (source de vérité)
  prices.manual.yaml    relevés de prix manuels (historique)
src/horspc/
  __main__.py           CLI : uv run python -m horspc build [--offline]
  models.py             modèles Pydantic v2 (entrées YAML, sortie data.json, liste des marchands)
  http.py               client HTTP : User-Agent, timeouts, retries, 1 requête/s max par hôte
  igdb.py               client IGDB v4 : date de sortie, fiche IGDB, statut PC, id de jaquette
  covers.py             jaquettes IGDB téléchargées en WebP dans site/covers/
  prices/               Protocol PriceProvider, provider manuel, règle de sélection
  build.py              fusion, validation, écriture de site/data.json
  render.py             rendu statique : fiches dans index.html, pages de jeux, SEO, sitemap.xml
site/                   index.html, mentions-legales.html, confidentialite.html, methodologie.html,
                        style.css, theme.js (thème, toutes pages), app.js (catalogue), fonts/,
                        data.json et covers/ (générés, versionnés)
tests/                  pytest, appels réseau mockés avec respx (fixtures IGDB figées)
docs/maquette/          maquette de référence du front (non publiée)
.github/workflows/      ci.yml (ruff + pytest), update.yml (build hebdo + Pages)
```

Flux de données : `games.yaml` + `prices.manual.yaml` + IGDB → `build.py` → validation Pydantic → `site/data.json` → `app.js`.

## Lancer en local

Prérequis : [uv](https://docs.astral.sh/uv/). Python 3.12 est installé par uv si besoin.

```sh
uv sync
cp .env.example .env          # puis renseigner IGDB_CLIENT_ID et IGDB_CLIENT_SECRET
uv run python -m horspc build # régénère site/data.json
python3 -m http.server -d site 8000   # puis http://localhost:8000
```

Les identifiants IGDB sont ceux d'une application Twitch (<https://dev.twitch.tv/console/apps>). Sans identifiants, `uv run python -m horspc build --offline` reprend les champs IGDB et le `generated_at` du `data.json` existant (ou l'heure courante s'il n'existe pas).

Codes de sortie du build : `0` OK, `1` validation refusée (entrées ou sortie) ou `data.json` précédent présent mais mal formé (JSON invalide, `schema_version` différent du schéma courant, ou contenu refusé par le modèle ; seul un fichier absent est toléré) ; dans les deux cas `data.json` n'est pas modifié, `2` identifiants IGDB manquants.

Tests et lint :

```sh
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

## Ajouter un jeu

**Règle : une entrée de `games.yaml` = une référence achetable = un prix.** Une édition distincte (par exemple une « Nintendo Switch 2 Edition ») est une autre entrée, pas une plateforme de plus. Zelda Tears of the Kingdom et Pokémon Écarlate sont donc listés en `[switch]` seulement.

```yaml
- slug: astro-bot                 # kebab-case, unique
  title: Astro Bot
  platforms: [ps5]                # ps5 | switch | switch2
  igdb_id: 255340                 # optionnel : sinon recherche IGDB par titre et plateforme
  launch_price: 69.99             # € TTC, ou null
  launch_price_source: https://…  # obligatoire si launch_price est renseigné
  pc_override:                    # optionnel, prioritaire sur IGDB
    status: none                  # none | announced | released
    source_url: https://…
    checked_at: 2026-10-24
  release_date_override:          # optionnel, prioritaire sur IGDB (date future admise)
    date: 2026-04-30
    source_url: https://…
```

Sans `igdb_id`, le build journalise l'identifiant trouvé (`IGDB : astro-bot → 255340`) et signale les correspondances approximatives : il est conseillé d'épingler alors l'`igdb_id`. Un jeu dont le `pc_status` passe à `released` reste dans `data.json` mais n'est plus affiché ; on peut ensuite le retirer de `games.yaml`.

### Trouver des candidats

`uv run python -m horspc candidates [--limit 50] [--min-ratings 20] [--recent-limit 20] [--include-legacy] [--output data/candidates.yaml]` interroge IGDB et écrit un YAML au format de `games.yaml`, **à curer à la main** (le fichier est ignoré par git ; la commande refuse d'écrire dans `games.yaml`).

- Jeux sur PS5, Switch ou Switch 2, de type Main Game, Remake ou Remaster (ids lus via `/game_types`), hors éditions (`version_parent`), absents de `games.yaml`.
- Exclusion PC avec la même logique que `pc_status`, appliquée à la fiche, à ses fiches liées (`remasters`, `remakes`, `ports`, `expanded_games`, `standalone_expansions`) et à ses éditions, ainsi qu'au jeu d'origine (`parent_game`) pour un Remaster ou un Port (même jeu), mais pas pour un Remake (autre jeu) : seul un jeu **released** sur PC est écarté ; un portage annoncé reste candidat, signalé en commentaire.
- Première sortie sur PS5, Switch ou Switch 2 (les originaux PS4, Wii U, GBA… ressortis sur ces consoles sont écartés, sauf `--include-legacy`). `platforms` reprend les consoles de cette première sortie.
- Deux sections : les plus notés (`total_rating_count`, seuil `--min-ratings`), puis « Sorties récentes » des 365 derniers jours triées par `hypes`, sans seuil (le champ `follows` n'est plus renseigné par IGDB).

## Ajouter un prix

**Pourquoi les prix sont manuels.** Aucune extraction automatique : les comparateurs (leDénicheur) et les stores officiels (PlayStation, Nintendo) l'interdisent dans leurs CGU ou leur robots.txt, et idealo bloque les automates. Pas d'affiliation non plus : les commissions rendraient le projet commercial, ce qui sort du cadre gratuit non commercial de l'API IGDB et contredirait la mention « ne touche aucune commission » du site.

Ajouter une observation à `data/prices.manual.yaml`. Les anciennes observations restent : elles forment l'historique.

`uv run python -m horspc price add` guide la saisie : jeu (liste numérotée ou slug, complétion avec Tab), marchand (liste blanche), format (déduit du marchand), prix, port (vide = inconnu), vente par l'enseigne (refus explicite sinon) et URL. `observed_at` vaut la date du jour. Le relevé est validé avec les mêmes modèles que le build et ajouté en fin de fichier, commentaires et ordre préservés (ruamel.yaml) ; rien n'est écrit si la validation échoue.

`uv run python -m horspc stale` liste les jeux à relever : sans aucun relevé, ou dont l'offre retenue est `stale` (aucun relevé de moins de 30 jours). Ils sont triés du plus ancien dernier relevé au plus récent (sans relevé en tête), avec la date et l'URL du dernier relevé et le dernier relevé de chaque marchand.

```yaml
- slug: astro-bot
  price: 53.30              # € TTC, article seul
  shipping: null            # frais de port en € ; 0 = offerts ; null = inconnus
  format: physical          # physical | digital
  condition: new
  merchant: amazon          # identifiant de la liste blanche ci-dessous
  sold_by_merchant: true    # vendu par l'enseigne elle-même, pas par un vendeur tiers
  url: https://www.amazon.fr/dp/…
  observed_at: 2026-10-20
```

Marchands admis (`MERCHANTS` dans `src/horspc/models.py`), le domaine de `url` devant correspondre :

| Format | Identifiants |
|---|---|
| physical | `amazon`, `cdiscount` (ecommerce) ; `fnac`, `darty`, `boulanger`, `cultura` (culture_tech) ; `micromania` (specialiste) ; `leclerc`, `auchan` (grande_distribution) |
| digital (officiel uniquement) | `playstation-store`, `nintendo-eshop` (store_officiel) |

Chaque marchand a une `category` (`grande_distribution`, `specialiste`, `culture_tech`, `ecommerce`, `store_officiel`).

**Règle : un relevé n'est valable que si le prix est accessible partout en France en livraison à domicile** (pas de prix de drive, de retrait ou de magasin). Enseignes écartées : Intermarché et Super U (prix du magasin choisi, livraison selon le magasin), Carrefour (aucune fiche de jeu vendue par Carrefour lui-même trouvée), Lidl (pas de jeux PS5/Switch en ligne).

Sont refusés à la validation, avec un message explicite : les marketplaces (`rakuten`, `ebay`), les revendeurs de clés (Instant Gaming, Eneba, G2A, Kinguin, CDKeys, Gamivo, K4G, HRK, etc.), toute observation avec `sold_by_merchant: false`, un marchand inconnu, une URL hors du domaine du marchand, et une date future.

### Règle de sélection

1. Pour chaque couple (marchand, format), seul le relevé le plus récent compte.
2. Parmi les relevés de **moins de 30 jours** : le **physique neuf** le moins cher ; à défaut, le **démat officiel** le moins cher.
3. La comparaison porte sur `price + shipping` quand le port est connu, sinon sur `price` seul.
4. Sans relevé récent : le relevé le plus récent, marqué `stale: true` (« Prix à vérifier » sur le site).
5. Égalité : relevé le plus récent, puis ordre alphabétique du marchand.

`discount_pct` = `round(100 × (1 − price / launch_price))`, port exclu ; `null` sans prix de lancement. Le badge n'est affiché que si la réduction est positive.

## Schéma de `site/data.json` (v3)

Montants en centimes (entiers), dates ISO 8601, horodatages UTC (`Z`). Le formatage (`53,30 €`, `JJ/MM/AAAA`) est fait par le front. Le modèle de référence est `DataFile` dans `src/horspc/models.py`.

```json
{
  "schema_version": 3,
  "generated_at": "2026-10-26T06:17:42Z",
  "currency": "EUR",
  "games": [
    {
      "slug": "astro-bot",
      "title": "Astro Bot",
      "platforms": ["ps5"],
      "release_date": "2024-09-06",
      "release_date_source": "igdb",
      "launch_price_cents": 6999,
      "igdb_id": 255340,
      "igdb_url": "https://www.igdb.com/games/astro-bot",
      "igdb_fetched_at": "2026-10-26T06:17:40Z",
      "cover_url": "covers/astro-bot.webp",
      "cover_image_id": "coba3k",
      "pc_status": "none",
      "pc_status_source": "igdb",
      "offer": {
        "price_cents": 5330,
        "shipping_cents": null,
        "discount_pct": 24,
        "format": "physical",
        "condition": "new",
        "merchant": "Amazon",
        "url": "https://www.amazon.fr/dp/…",
        "observed_at": "2026-07-18",
        "source": "manual",
        "stale": true
      }
    }
  ]
}
```

- `release_date` : plus ancienne sortie sur l'une des plateformes du jeu, parmi les dates IGDB en région `europe` ou `worldwide` et au statut « Full Release » ou sans statut (accès anticipé, Advanced Access, alpha, bêta, etc. sont écartés). Les ids de région et de statut sont lus par leur nom via `/release_date_regions` et `/release_date_statuses`. Si aucune date ne passe ces filtres, le build journalise un WARNING (slug, nombre de dates écartées par motif) : ajouter alors un `release_date_override`.
- `release_date_source` : `igdb`, `manual` (`release_date_override`) ou `null` si `release_date` est `null`. Ajouté en v2.
- `pc_status` : déduit des `release_dates` PC d'IGDB (toutes régions), les lignes au statut Cancelled étant ignorées. `released` si une ligne Full Release, Early Access, Advanced Access, Offline ou sans statut a une date passée ; `announced` pour toute autre ligne retenue (date future ou absente, ou statut Alpha, Beta…) ; `none` si aucune ligne PC n'est retenue, même si le PC figure dans les plateformes du jeu. `pc_status_source` vaut `manual` quand `pc_override` est utilisé.
- L'identifiant de la plateforme PC n'est pas codé en dur : il est lu via l'endpoint `platforms` d'IGDB (slug `win`).
- Si IGDB est injoignable, les champs IGDB (dont `igdb_fetched_at`) sont repris du `data.json` précédent ; le build n'échoue pas.
- `cover_url` : chemin relatif `covers/<slug>.webp`, ou `null` sans jaquette ; `cover_image_id` : `image_id` IGDB de la jaquette présente sur disque. Ajoutés en v3. Le site ne charge jamais d'image depuis `images.igdb.com`.

### Jaquettes

- Au build complet, `covers.py` récupère l'`image_id` de la cover IGDB de chaque jeu et télécharge `t_cover_big` (264 × 352) **directement en WebP** : IGDB sert ce format quand on demande l'extension `.webp`, aucune conversion ni dépendance n'est nécessaire. Le contenu est vérifié (en-tête `RIFF…WEBP`).
- Le fichier n'est retéléchargé que si l'`image_id` a changé par rapport au `data.json` précédent, ou s'il manque. En cas d'échec, l'ancien fichier est conservé ; si IGDB n'a plus de cover, le fichier est supprimé, ainsi que les jaquettes de jeux retirés du catalogue.
- Les fichiers ne sont écrits qu'après validation et écriture de `data.json` : un build refusé ne touche pas `site/covers/`.
- En `--offline` (ou si IGDB est indisponible), les fichiers déjà présents sont réutilisés tels quels ; rien n'est téléchargé ni supprimé.
- Le front affiche la jaquette (`width`/`height`, `loading="lazy"`) et retombe sur le visuel à initiales, avec la mention « Visuel provisoire », si `cover_url` est `null`.

## Mise à jour automatique et seuils de fraîcheur

`.github/workflows/update.yml` se comporte différemment selon l'événement :

| Événement | Build | Commit de `data.json` et `site/covers/` | Déploiement Pages | Job `freshness` |
|---|---|---|---|---|
| `schedule` (**lundi 06:17 UTC**) et `workflow_dispatch` | complet, avec IGDB (secrets) | oui, s'il a changé | oui | oui |
| `push` sur `main` touchant `site/`, `src/` ou `data/` | `--offline` (sans IGDB ni secrets) | **jamais** | oui, avec le `data.json` ainsi généré | non |

Sur un push, le site est donc redéployé avec les prix et le code à jour, mais les champs IGDB viennent du `data.json` versionné. Ce `data.json` régénéré n'existe que dans l'artefact déployé : le dépôt n'est modifié que par le build planifié ou manuel. Le build `--offline` reprend aussi le `generated_at` du `data.json` versionné : un push ne rajeunit donc pas la date « Catalogue mis à jour le » et ne masque pas la bannière d'ancienneté, qui reflètent toujours le dernier build complet. Seul `offer.stale` est recalculé avec l'heure courante, si bien qu'un relevé peut passer en « Prix à vérifier » après un push.

Deux garde-fous vérifient qu'un push ne produit aucun commit du bot :

- `tests/test_workflows.py` évalue les conditions `if:` de `update.yml` pour chaque événement. Il vérifie que, sur un push, aucune étape exécutée ne contient `git commit` ou `git push`, que le seul build lancé est `--offline` sans secret, et que `freshness` ne tourne pas.
- Dans le workflow, l'étape « Vérifie qu'un push ne produit aucun commit » échoue si `HEAD` diffère du commit poussé.

Si la validation échoue, rien n'est commité ni déployé.

Deux seuils, volontairement distincts :

| Constante | Valeur | Où | Effet |
|---|---|---|---|
| `STALE_ISSUE_DAYS` | **8 jours** | `env` de `.github/workflows/update.yml` | ouvre une issue « Données obsolètes » (label `stale-data`, une seule à la fois) |
| `STALE_BANNER_DAYS` | **14 jours** | en tête de `site/app.js` | affiche une bannière d'alerte sous l'en-tête du site |

Le job d'alerte tourne même quand le build échoue (sur `schedule` et `workflow_dispatch` uniquement). Il ne peut pas, en revanche, détecter un cron désactivé par GitHub (après 60 jours sans activité sur le dépôt) : le commit hebdomadaire de `data.json` maintient normalement le dépôt actif, et la bannière du site reste le garde-fou indépendant d'Actions.

### Configuration du dépôt GitHub

1. Secrets `IGDB_CLIENT_ID` et `IGDB_CLIENT_SECRET` (Settings > Secrets and variables > Actions).
2. Source GitHub Pages sur « GitHub Actions » :

   ```sh
   gh api -X POST repos/Mvth1s/hors-pc/pages -f build_type=workflow
   # si Pages est déjà activé avec une autre source :
   gh api -X PUT repos/Mvth1s/hors-pc/pages -f build_type=workflow
   ```

## SEO et rendu statique

Le build (complet ou `--offline`) écrit le catalogue en HTML : le site est **entièrement lisible sans JavaScript** ; `app.js` ne gère plus que les filtres, les tris et la bannière d'ancienneté sur les fiches déjà présentes, et plus aucune page ne charge `data.json`.

- `index.html` reste le gabarit : le build ne remplace que les zones `<!--horspc:head-->`, `updated`, `count` et `grid`. Les pages racine reçoivent leur zone `head` et la date du catalogue dans le pied de page.
- Une page par jeu : `site/jeux/<slug>/index.html` (titre, plateformes, date, statut PC, offre retenue et date de relevé, jaquette, lien vers le catalogue), construite sur le gabarit des pages légales. Les pages des jeux retirés sont supprimées.
- Sur chaque page : `<link rel="canonical">` absolu, meta description, Open Graph et Twitter card (jaquette, ou `site/og-image.png` en 1200 × 630), `lang="fr"`, un seul `h1`.
- JSON-LD : `WebSite`, `Person` et `ItemList` sur l'accueil ; `VideoGame` sur chaque page de jeu, avec une `Offer` (prix, EUR, `priceValidUntil` = date du relevé + 30 jours, `InStock`, `NewCondition`) seulement si l'offre n'est pas `stale` : au-delà de 30 jours, le prix n'est plus garanti et n'est plus publié. Le stock n'est pas suivi : `InStock` signifie « offre disponible au moment du relevé ».
- `site/sitemap.xml` : accueil, pages racine et pages de jeux. `lastmod` réel : date du catalogue pour l'accueil, date « Dernière mise à jour » affichée pour les pages statiques, et pour une page de jeu la date du build où son contenu (`<main>`) a changé pour la dernière fois.
- Vérification Google Search Console : balise `google-site-verification` dans le `<head>` d'`index.html` uniquement (un test garantit qu'elle ne disparaît pas).

**Pas de `robots.txt` dans `site/`.** Les robots ne lisent `robots.txt` qu'à la racine du domaine (`https://mvth1s.github.io/robots.txt`), jamais dans un sous-chemin comme `/hors-pc/`. Deux options pour en avoir un :

1. **Domaine personnel** : configurer un domaine pour GitHub Pages ; le site est alors servi à la racine, `site/robots.txt` y serait lu (avec `Sitemap: https://<domaine>/sitemap.xml`). Les URL absolues (`SITE_URL` dans `src/horspc/render.py`) sont à mettre à jour.
2. **Dépôt `Mvth1s.github.io`** : il sert la racine `https://mvth1s.github.io/` ; y placer un `robots.txt` contenant `Sitemap: https://mvth1s.github.io/hors-pc/sitemap.xml`.

En attendant, déclarer `https://mvth1s.github.io/hors-pc/sitemap.xml` directement dans Search Console.

## Pages légales

`site/faq.html` (questions en `<details>`, JSON-LD `FAQPage`), `site/mentions-legales.html`, `site/confidentialite.html` et `site/methodologie.html` sont statiques et liées depuis le pied de page de toutes les pages. `tests/test_site.py` vérifie qu'aucune page ne charge de ressource externe, que le pied de page lie les trois pages et qu'elles sont datées. **TODO bloquant** : `test_adresse_de_contact_renseignee` échoue tant que l'adresse de contact de l'éditeur n'est pas renseignée dans `mentions-legales.html`.

## Crédits

- Dates de sortie, statut PC et jaquettes : [IGDB.com](https://www.igdb.com), attribution visible dans le pied de page du site. Les jaquettes et noms de jeux appartiennent à leurs éditeurs respectifs ; une demande de retrait peut être faite via une issue GitHub (lien dans le pied de page).
- Polices auto-hébergées (aucun appel à Google Fonts) : Bricolage Grotesque et IBM Plex Sans, sous licence SIL Open Font License 1.1 (`site/fonts/OFL-*.txt`).
- HorsPC n'est affilié à aucun constructeur, éditeur ni marchand.
