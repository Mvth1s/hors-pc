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
  2. un site est **exclu** s'il bloque nos requêtes (403, 429, captcha, défi anti-bot) : aucun
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
- **Découverte** : la recherche interne est interdite (L3), mais `/fp/<slug>-<EAN>` redirige vers la
  fiche quel que soit le slug : la fiche s'ouvre par l'EAN (`/fp/x-<EAN>`).
  **Correction (25/10/2026)** : une première version de ce document indiquait que les sitemaps
  produits donnaient titre et EAN. C'est faux : les 78 sitemaps produits (environ 9 Mo chacun) ne
  contiennent que des URL `/fp/<EAN>`, sans titre. Ils ne permettent donc pas de trouver un jeu par
  son titre. L'EAN vient de Boulanger (pages catégories, EAN confirmé par le `gtin13` de la fiche)
  ou de Pixmania (fiche trouvée par les sitemaps, `gtin13`), puis la fiche Leclerc est ouverte par
  cet EAN et son `gtin13` est vérifié.

### Boulanger (`www.boulanger.com`) : retenu par interprétation

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
  fiche. C'est la source principale des EAN du catalogue.

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
- **Découverte** : sitemaps produits (`products-N.xml`, slugs lisibles), puis EAN vérifié.

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

## Demande d'autorisation (modèle de mail)

Pour les sites bloqués (Micromania, Fnac, Darty, Cdiscount, Cultura), à envoyer au service juridique
ou au contact presse/partenariats :

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
> Contact : Mathis Aguado (Mvth1s), mag.d3v+github@pm.me
>
> Je vous remercie par avance de votre réponse.
>
> Mathis Aguado
