# Sources de prix : cadre légal et analyse par site

Relevés du **25/10/2026**, faits avec le User-Agent du projet
(`HorsPC/0.1 (+https://github.com/Mvth1s/hors-pc)`), au plus une requête par seconde et par hôte,
sans aucun contournement. L'état de chaque source est piloté par `data/sources.config.yaml`
(`enabled` + raison) : une source se désactive en une ligne.

## Cadre légal

- **Base** : exception de fouille de textes et de données (directive (UE) 2019/790, art. 4 ;
  CPI art. L. 122-5-3, III), applicable sauf opposition du titulaire des droits.
- **Forme de l'opposition** : CPI art. R. 122-28 (décret n° 2022-928 du 23 juin 2022, en vigueur
  depuis le 25 juin 2022) : l'opposition « n'a pas à être motivée et peut être exprimée par tout
  moyen », notamment par des procédés lisibles par machine (métadonnées) « et par le recours à des
  conditions générales d'utilisation d'un site internet ».
- **Règles appliquées par HorsPC** :
  1. un site est **exclu** s'il a exprimé une opposition : robots.txt (Disallow sur les chemins
     utiles, réserve TDM), fichier `/.well-known/tdmrep.json`, en-tête ou meta `tdm-reservation`,
     ou CGU interdisant l'extraction ou les robots ;
  2. un site est **exclu** s'il bloque nos requêtes (400, 403, 429, captcha, défi anti-bot, ou
     robots.txt non vérifiable : tout code autre que 200, 404 ou 410) : aucun
     contournement (pas de proxy, pas de changement d'User-Agent, pas de navigateur headless pour
     passer un défi, pas de résolution de captcha) ;
  3. robots.txt est relu à chaque run et interprété selon la RFC 9309 (jokers `*` et `$`, règle la
     plus longue). `urllib.robotparser` n'est pas utilisé : il ignore les jokers et autorisait à tort
     `/recherche*` chez Auchan ;
  4. seuls des faits sont publiés (prix, marchand, vendeur, date du relevé, lien) : aucun texte,
     image ou description n'est repris.

## Sources retenues

### E.Leclerc (`www.e.leclerc`)

- **Éditeur** : L COMMERCE, SNC, 26 quai Marcel Boyer, 94200 Ivry-sur-Seine, RCS Créteil 882 736 077
  (mentions légales).
- **robots.txt** : groupe `User-agent: *` (L2) ; `Disallow: /recherche` (L3), `Disallow: */offers`
  (L6) ; aucune règle sur `/fp/` (fiches produit).
- **TDMRep** : `/.well-known/tdmrep.json` absent (404), pas d'en-tête ni de meta `tdm-reservation`.
- **CGU / CGV** : les pages légales ne s'affichent qu'en JavaScript (lues rendues). Les CGV (art. 16,
  propriété intellectuelle) interdisent la « modification, représentation et reproduction » des
  textes, marques, images et représentations graphiques ; **aucune clause sur l'extraction, les
  robots ou la fouille**. Les prix et la date d'un relevé ne relèvent pas de ces éléments.
- **Réponse** : 200, fiche réelle, sans défi.
- **Données** : JSON-LD `Product` avec `gtin`, `Offer` (prix, `EUR`, `InStock`, `NewCondition`,
  `seller: E.Leclerc`) et `AggregateOffer` pour la place de marché.
- **Découverte par catégories** (depuis le 25/10/2026) : la recherche interne est interdite
  (`Disallow: /recherche`, L3), mais les pages catégories de jeux par console sont listées dans le
  sitemap de navigation (`/sitemap/sitemap-navigation-1.xml`, via `/sitemap/sitemap-index.xml`) :
  `/cat/jeux-ps5`, `/cat/jeux-switch`, `/cat/jeux-switch-2`, `/cat/jeux-xbox-series`.
  - **robots.txt** : groupe `User-agent: *` (L2) ; aucune règle ne vise `/cat/…` ni `?page=N`.
    `Disallow: /catalogue` (L5) ne concerne que les chemins commençant par `/catalogue`, et
    `Disallow: /lp/*page=` (L24) que `/lp/`. Le tri et les filtres sont interdits
    (`/*property_facet`, `/*sortOrder=`, `/*sortKey=`, `/*availability_status=`…, L14 à L23) :
    aucun de ces paramètres n'est utilisé.
  - **Rendu** : HTML complet côté serveur (environ 850 Ko par page) ; chaque carte donne le nom du
    produit (attribut `title`) et l'EAN, en fin de lien `/fp/<slug>-<EAN>`. Correspondance stricte
    sur ce nom (plateforme, édition standard, code in a box, éditeurs IGDB), puis `gtin13`, offre
    et vendeur vérifiés sur la fiche ; fiche physique avec offre préférée.
  - **Pagination** : `?page=N`, 40 cartes par page, jusqu'à une page vide. Relevé du 25/10/2026 :
    51 pages (PS5), 57 (Switch), 8 (Switch 2), 22 (Xbox Series).
  - **Pagination instable** : certaines pages répètent la précédente (Switch 2 : pages 3 et 4
    identiques), si bien que des produits n'apparaissent pas ; le paramètre `xrf` des liens du site
    n'y change rien et le tri est interdit. Mesure : 20 des 23 fiches déjà connues par EAN
    figurent dans les catégories, les 3 absentes étant en rupture. Un produit manqué est recherché
    à nouveau 14 jours plus tard ; il est aussi rattrapé par Pixmania puis Leclerc par EAN.
  - **Volume** : une catégorie n'est lue que si une de ses éditions attend une découverte, au plus
    une fois par run. Au pire 138 pages, soit environ 2 min 20 à 1 requête par seconde et 115 Mo,
    plus le sitemap de navigation (2,2 Mo) ; un run où tout est déjà trouvé ne lit aucune
    catégorie.
- **Découverte par EAN** : `/fp/<slug>-<EAN>` redirige vers la fiche quel que soit le slug : la
  fiche s'ouvre par l'EAN (`/fp/x-<EAN>`), pour les EAN venus de Pixmania.
  **Correction (25/10/2026)** : une première version de ce document indiquait que les sitemaps
  produits donnaient titre et EAN. C'est faux : les 78 sitemaps produits (environ 9 Mo chacun) ne
  contiennent que des URL `/fp/<EAN>`, sans titre.
- **Rupture** : une fiche sans offre en ligne est conservée et relevée à chaque run ; le site
  affiche « En rupture chez E.Leclerc, constaté le … » tant qu'aucune autre offre n'existe.
- **Vendeurs tiers** : une fiche qui n'a que des offres de la place de marché (`AggregateOffer`
  sans `Offer` de l'enseigne) est écartée.

### Pixmania (`www.pixmania.com`)

- **Exploitant** : PIXMANIA GROUP, SAS, 22 rue Curnonsky, 75017 Paris, RCS Paris 910 199 835
  (CGU, art. 2) : établi dans l'UE.
- **Vendeurs** : place de marché réservée aux professionnels (CGU, définitions : « Vendeur(s)
  désigne(nt) tout(s) professionnel(s) qui propose(nt) à la vente des Produits sur la Marketplace »).
- **robots.txt** : `User-Agent: *` (L67), `Disallow: */search/*` (L68) ; fiches produit autorisées.
- **CGU** (mise à jour du 03/04/2026) : aucune clause sur l'extraction, les robots ou la fouille.
- **TDMRep** : absent.
- **Réponse** : 200, fiche réelle.
- **Données** : JSON-LD `Product` avec `gtin13`, `AggregateOffer` (prix, `EUR`, `NewCondition`,
  disponibilité, frais de port, retour sous 14 jours en France) ; vendeur réel dans le HTML.
- **Découverte** : sitemaps produits (`products-N.xml`, slugs lisibles), correspondance stricte
  titre + plateforme + édition standard, puis `gtin13` lu sur la fiche. **Source des EAN** du
  catalogue depuis la désactivation de Boulanger. Les noms de produits omettent parfois la
  plateforme (« Sony Interactive Entertainment SAROS ») : celle du slug de l'URL est alors
  retenue si elle est unique. Les fiches « [Code de téléchargement] » (codes envoyés sans boîte
  par un vendeur de la place de marché) ne sont jamais rattachées.
- **Limite** : les EAN de Pixmania sont souvent ceux d'imports et ne correspondent pas toujours à
  ceux de Leclerc (Mario Kart World : `0045496312329` contre `0045496312336`). Depuis la
  découverte par catégories, Leclerc ne dépend plus de ces EAN ; un jeu absent des catégories
  (pagination instable, rupture) et vendu sous un autre EAN chez Pixmania peut encore n'avoir que
  le prix Pixmania.

## Découverte du catalogue (même sources, mêmes règles)

Depuis le 30/10/2026, le catalogue est aussi découvert automatiquement dans les rayons des sources
actives ci-dessus (`horspc discover`, voir le README). **Aucune nouvelle source** : seuls les
chemins déjà lus pour le relevé des prix sont utilisés, avec le même `Fetcher` (robots.txt relu,
1 requête par seconde et par hôte, source arrêtée au premier blocage, aucun contournement) :

| Source | Chemins lus | Volume par build complet (30/10/2026) |
|---|---|---|
| E.Leclerc | sitemap de navigation, puis `/cat/jeux-ps5`, `jeux-switch`, `jeux-switch-2`, `jeux-xbox-series` (`?page=N`, sans tri ni filtre) | environ 138 pages, 1 930 produits (nom, EAN, URL), environ 2 min 30 |
| Pixmania | index et sitemaps produits (`feeds.pixmania.com`) | quelques fichiers, 194 160 URL, dont 5 598 citant une seule console cible |

Les pages catégories Leclerc et les sitemaps Pixmania sont désormais lus à chaque build complet,
et non plus seulement quand une édition attend une découverte. Ils sont lus une seule fois par
run et partagés avec le relevé. Aucune fiche produit n'est ouverte pour identifier un jeu ; les
fiches des jeux retenus sont ensuite relevées comme les autres. Build complet mesuré le
30/10/2026 en régime établi : 318 requêtes vers Leclerc, 114 vers Pixmania, 6,4 min au total. Une absence de rayon n'est comptée que si toutes les sources ont été lues sans blocage.

Hôtes vérifiés et non retenus pour les titres français : `query.wikidata.org` (`Disallow:
/sparql`) et `www.wikidata.org` (`Disallow: /w/`, donc `api.php` et `rest.php` ; seul
`/wiki/Special:EntityData/*.` est autorisé), relevés le 30/10/2026. Voie conforme à étudier
(page Wikipédia liée par IGDB, puis QID, puis `Special:EntityData`) : issue #8.

## Sources désactivées

### Boulanger (`www.boulanger.com`) : désactivé, bloque les IP de GitHub Actions

- **Constat (25/10/2026)** : le diagnostic `scrape_dry_run` lancé depuis GitHub Actions reçoit
  **HTTP 400** sur robots.txt, sur la page catégorie et sur la fiche, alors que les mêmes URL
  répondent 200 depuis une connexion résidentielle. C'est un filtrage des IP de GitHub Actions :
  conformément à la règle 2, la source est désactivée (`enabled: false`) et n'est pas contournée.
- L'analyse juridique ci-dessous reste valable si la source redevient accessible.

- **robots.txt** : `User-agent: *` (L8), `Allow: /` (L10), `Disallow: /resultats*` (recherche) ;
  fiches `/ref/<id>` et pages catégories `/c/...` autorisées.
- **TDMRep** : absent.
- **CGV** (version du 11/09/2026), article 9 : « Tous les textes, commentaires, ouvrages, illustrations,
  images et photographies, marques et bases de données reproduits et intégrés sur le site
  Boulanger.com sont protégés […]. A ce titre […] seule l'utilisation pour un usage privé est
  autorisée. Toute reproduction totale ou partielle du site Boulanger.com est strictement interdite. »
- **Interprétation retenue** : l'article 9 vise la reproduction du site et de ses éléments protégés ;
  il n'exprime pas d'opposition à la fouille de textes et de données au sens de l'art. R. 122-28 et
  ne mentionne ni extraction ni robots. HorsPC ne reproduit aucun élément du site : il publie
  seulement le prix, le marchand, le vendeur, la date du relevé et le lien, des faits non protégés.
  Source désactivable en une ligne si Boulanger exprime une opposition.
- **Réponse** : 200, fiche réelle.
- **Données** : JSON-LD `Offer` (prix, `EUR`, disponibilité, état) et `gtin13` ; vendeur absent du
  JSON-LD mais affiché dans le HTML (« vendu par boulanger » ou vendeur de la place de marché).
- **Découverte** : pages catégories `/c/jeu-playstation-5`, `/c/jeu-switch` (Switch et Switch 2) et
  `/c/jeu-xbox-one` (Xbox, dont Series), paginées par `?numPage=N` (40 produits par page), rendues
  côté serveur. Chaque carte donne le nom, l'éditeur, l'état (`neuf`, reconditionné…) et l'EAN
  (dans l'URL de l'image `/Boulanger/<EAN>_h_f_l_…`), puis l'EAN est vérifié par le `gtin13` de la
  fiche. C'était la source principale des EAN du catalogue avant sa désactivation.

## Sources exclues

| Site | Motif | Preuve (25/10/2026) |
|---|---|---|
| PlayStation Store | Opposition | Conditions d'utilisation du site, § 10 (« Vous ne devez pas mener […] toute opération d'exploitation de textes ou de données », « robot, bot, spider, scraper ») |
| Nintendo (eShop) | Opposition | robots.txt de nintendo.com : « web scraping, machine learning, or any form of text or data mining […] is strictly prohibited », réserve au titre de l'art. 4 (3) de la directive 2019/790 |
| leDénicheur | Opposition | CGU du 23/03/2026, art. 4.3 b et d (extraction ; « Utiliser un robot, notamment d'exploration (spider) ») |
| Amazon | Opposition | Conditions d'utilisation et de vente (MAJ 28/11/2025), § 3 : « vous ne devez pas utiliser de robot d'aspiration de données, ou tout autre outil similaire de collecte ou d'extraction de données » |
| Auchan | Opposition | CGU (MAJ 15/11/2024), art. 8 : « L'usage de systèmes automatisés (scripts, robots d'indexation, logiciels…) visant à extraire des données du Site […] (Webscraping) est strictement interdit » ; robots.txt L33 `Disallow: /recherche*` |
| Carrefour | Opposition | CGV, art. 11.2 (« l'extraction répétée et systématique d'éléments du Site ») ; CGU place de marché (« pratiques telles que le scrapping ») |
| eBay | Opposition, et occasion | Contrat d'utilisation (« utiliser tout type de robots informatiques, scraper ») ; robots.txt L6 et L19. API Browse écartée : occasion majoritaire et risque d'adhésion au programme d'affiliation eBay Partner Network |
| Microsoft / Xbox Store | Opposition | Contrat de services Microsoft (MAJ 30/09/2026) : « en procédant à un "scraping" inadmissible » |
| Rakuten France | Non vérifiable | Pages de CGU en 403 depuis notre User-Agent ; robots.txt servi en HTML |
| Cdiscount | Bloqué | 403 Cloudflare (« Accès non autorisé ») |
| Fnac, Darty | Bloqués | 403 Akamai (« FNAC DARTY - Maintenance ») |
| Cultura | Bloqué | 403 DataDome (captcha) |
| Micromania | Bloqué | Défi Imperva/Incapsula (page vide de 212 octets) |
| Intermarché, King Jouet, Maxi Toys | Bloqués | 403 DataDome |
| Super U | Bloqué | 403 Cloudflare |
| Smyths Toys | Bloqué | Défi Imperva (« Pardon Our Interruption ») |
| Gamecash | Bloqué | 403 |
| JouéClub | Hors périmètre | Sitemaps (113 389 produits) : aucun jeu PS5, Switch ou Xbox, seulement des produits dérivés |
| La Grande Récré | Hors périmètre | Sitemap (18 466 produits) : aucun produit PS5, Switch ou Xbox |
| LDLC, Rue du Commerce | Hors périmètre | Aucun jeu de console dans les sitemaps échantillonnés ; recherche interdite (`Disallow: /recherche/`) |
| Easy Cash | Hors périmètre | Surtout de l'occasion ; recherche interdite (`Disallow: *q=*`) |

## Contacts officiels pour une demande d'autorisation

Coordonnées reprises des pages légales des enseignes, avec leur source. Ces pages renvoient 403
ou ne s'affichent qu'en JavaScript pour un outil automatisé : elles ont été lues telles
qu'indexées par un moteur de recherche le 25/10/2026, sans contournement. **À vérifier sur la
page avant tout envoi** ; adresser le courrier au service juridique de la société.

| Enseigne | Société et adresse | Canal | Source |
|---|---|---|---|
| Boulanger | Boulanger SA, siège à Lesquin (Nord), RCS Lille B 347 384 570 | Formulaire de contact du site | [Mentions légales](https://www.boulanger.com/evenement/infos-legales), [contact](https://www.boulanger.com/evenement/contact) |
| Micromania | Micromania SAS, 955 route des Lucioles, 06560 Valbonne, RCS Grasse (le numéro diffère selon les documents du site) | Courrier au siège | [CGV](https://www.micromania.fr/cgv.html) |
| Cdiscount | Cdiscount SA, 120-126 quai de Bacalan, 33000 Bordeaux, RCS Bordeaux 424 059 822 | Courrier au service juridique, à cette adresse | [Mentions légales (PDF)](https://www.cdiscount.com/resources/imagesok/cgv/ml.pdf) |
| Fnac | Fnac Direct SA, Le Flavia, Ivry-sur-Seine, RCS Créteil B 377 853 536 | Courrier au siège | [Mentions légales](https://www.fnac.com/entreprise/mentions_legales.aspx), [CGV de la place de marché](https://www4.fnac.com/Help/cgv-fnac-marketplace) |
| Darty | Établissements Darty et Fils SAS, ZAC Port d'Ivry, 9 rue des Bateaux-Lavoirs, 94200 Ivry-sur-Seine, RCS Créteil B 542 086 616 (d'autres documents du site indiquent Bondy, RCS Bobigny) | Courrier au siège | [Informations légales](https://www.darty.com/achat/informations/informations_legales.html) |
| Cultura | SOCULTUR SAS, 17 rue Archimède, 33691 Mérignac Cedex, RCS Bordeaux 519 780 795 | Courrier au siège | [CGV de cultura.com](https://www.cultura.com/mentions-legales/cultura-cgv/cgv-cultura-com-en-vigueur-du-22-juillet-2025-au-31-aout-2025.html) |

## Audit mensuel

Le workflow [`sources-audit.yml`](../.github/workflows/sources-audit.yml) (le 1er de chaque mois,
ou à la demande) contrôle chaque enseigne de ce document, listée dans
[`data/sources.audit.yaml`](../data/sources.audit.yaml), depuis les IP de GitHub Actions :

- robots.txt : statut HTTP, empreinte des règles qui s'appliquent à HorsPC, verdict sur nos chemins
  (catégories, fiches, recherche) ;
- TDMRep : `/.well-known/tdmrep.json` (seulement s'il s'agit de JSON), en-tête et meta
  `tdm-reservation` de l'accueil ;
- accueil et fiche connue : statut HTTP, page de défi ; pour les sites qui s'opposent à la fouille,
  aucune fiche n'est demandée ;
- CGU et CGV : empreinte des paragraphes qui parlent d'extraction, de robots, d'aspiration, de
  fouille ou de reproduction. Un changement signifie « à relire », jamais une interprétation ;
  une page illisible un mois donné garde sa dernière empreinte ;
- sources actives : JSON-LD toujours lisible sur une fiche.

L'état de référence est [`data/sources.audit.json`](../data/sources.audit.json), commité par le bot.
Un changement ouvre l'issue « Audit des sources : changements à relire » (label `sources-audit`,
une seule ouverte à la fois) et envoie un message Discord. **Aucune source n'est activée ni
désactivée automatiquement** : la décision reste humaine, dans `data/sources.config.yaml`.

Mêmes règles que le relevé : User-Agent identifiable, 1 requête par seconde par hôte, aucun
contournement ; une page interdite par robots.txt n'est pas demandée, et une enseigne qui bloque
n'est plus sollicitée pendant l'audit (environ 100 requêtes au total).

Limite : les CGU d'E.Leclerc, d'eBay, de LDLC et de Rue du Commerce ne s'affichent qu'en
JavaScript ; leur texte n'est pas lisible sans navigateur et leurs changements ne sont pas
détectés (aucune clause trouvée).

## Demande d'autorisation (modèle de mail)

Pour les sites bloqués (Micromania, Fnac, Darty, Cdiscount, Cultura, Boulanger), à envoyer aux
contacts officiels ci-dessus :

> Objet : Demande d'autorisation de relevé de prix automatisé, projet non commercial HorsPC
>
> Bonjour,
>
> Je publie HorsPC (https://mvth1s.github.io/hors-pc/), un catalogue gratuit et non commercial des
> jeux PS5, Switch et Xbox absents du PC, qui indique pour chaque jeu le prix le plus bas relevé chez
> les enseignes françaises, avec un lien direct vers leur fiche produit. Le site n'affiche aucune
> publicité, ne perçoit aucune commission et n'utilise aucun lien d'affiliation.
>
> Je souhaiterais être autorisé à consulter automatiquement les fiches produit de votre site pour
> une vingtaine de jeux, dans les conditions suivantes :
> - une requête par semaine et par jeu, au plus une requête par seconde ;
> - un User-Agent identifiable : `HorsPC/0.1 (+https://github.com/Mvth1s/hors-pc)` ;
> - respect de votre robots.txt ;
> - seuls le prix, la disponibilité, le vendeur et le lien vers votre fiche sont publiés, avec la
>   date du relevé ; aucun texte ni aucune image de votre site n'est repris.
>
> Si vous préférez, je peux utiliser un flux ou une API que vous mettriez à disposition, ou une
> plage d'adresses IP déterminée. Vous pouvez à tout moment me demander d'arrêter.
>
> Contact : Mvth1s, mag.d3v+horspc@pm.me
>
> Je vous remercie par avance de votre réponse.
>
> Mvth1s
