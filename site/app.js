// HorsPC : rendu du catalogue à partir de data.json (vanilla, sans dépendance).
// Le thème clair/sombre est géré par theme.js, commun à toutes les pages.
"use strict";

// Au-delà de ce nombre de jours depuis generated_at, une bannière d'alerte s'affiche.
// Seuil distinct de STALE_ISSUE_DAYS (8 j) dans .github/workflows/update.yml.
const STALE_BANNER_DAYS = 14;

const DAY_MS = 86400000;

// Filtres console : « Switch 2 » couvre aussi les jeux Switch (rétrocompatibles).
const PLATFORM_FILTERS = {
  ps5: { label: "PS5", platforms: ["ps5"] },
  switch2: { label: "Switch 2", platforms: ["switch", "switch2"] },
};
const CONSOLES = {
  ps5: { label: "PS5", cls: "ps" },
  switch: { label: "Switch", cls: "nin" },
  switch2: { label: "Switch 2", cls: "nin" },
};
const STATUSES = {
  none: { label: "Exclusif console", filter: "Exclusif", cls: "status ex" },
  announced: { label: "Portage PC annoncé", filter: "Portage annoncé", cls: "status port" },
};
const FORMATS = { physical: "Physique", digital: "Démat" };
const STOPWORDS = new Set(["the", "of", "a", "an", "le", "la", "les", "de", "du", "des", "et"]);

const state = { platform: "all", status: "all", sort: "price" };
let games = [];

const $ = (id) => document.getElementById(id);

function esc(value) {
  return String(value).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);
}

function safeUrl(url) {
  return /^https?:\/\//i.test(url) ? url : "#";
}

/** "2026-07-18" -> "18/07/2026", sans passer par Date (pas de décalage de fuseau). */
function frDate(iso) {
  const [y, m, d] = iso.slice(0, 10).split("-");
  return `${d}/${m}/${y}`;
}

function frDateTime(iso) {
  return new Date(iso).toLocaleDateString("fr-FR", { timeZone: "Europe/Paris" });
}

const euroFmt = new Intl.NumberFormat("fr-FR", { style: "currency", currency: "EUR" });
const intFmt = new Intl.NumberFormat("fr-FR");

function euros(cents) {
  return euroFmt.format(cents / 100);
}

/** Prix mis en avant : euros en grand, centimes réduits. */
function bigPrice(cents) {
  const whole = intFmt.format(Math.floor(cents / 100));
  const rest = String(cents % 100).padStart(2, "0");
  return `${whole}<small>,${rest}&nbsp;€</small>`;
}

function initials(title) {
  const main = title.includes(":") ? title.split(":").pop() : title;
  const words = main.split(/[\s\-]+/).filter((w) => w && !STOPWORDS.has(w.toLowerCase()));
  if (words.length === 1) return words[0].slice(0, 2).toUpperCase();
  return words.slice(0, 2).map((w) => w[0]).join("").toUpperCase();
}

function comparePrice(g) {
  const o = g.offer;
  return o ? o.price_cents + (o.shipping_cents || 0) : null;
}

/** Tri avec les valeurs absentes toujours en fin de liste. */
function byKey(key, desc) {
  return (a, b) => {
    const ka = key(a);
    const kb = key(b);
    if (ka === null && kb === null) return a.title.localeCompare(b.title, "fr");
    if (ka === null) return 1;
    if (kb === null) return -1;
    if (ka === kb) return a.title.localeCompare(b.title, "fr");
    return (ka < kb ? -1 : 1) * (desc ? -1 : 1);
  };
}

const SORTS = {
  price: byKey(comparePrice, false),
  discount: byKey((g) => (g.offer ? g.offer.discount_pct : null), true),
  release: byKey((g) => g.release_date, true),
};

function visibleGames() {
  return games
    .filter(
      (g) =>
        state.platform === "all" ||
        g.platforms.some((p) => PLATFORM_FILTERS[state.platform].platforms.includes(p)),
    )
    .filter((g) => state.status === "all" || g.pc_status === state.status)
    .sort(SORTS[state.sort]);
}

function shippingText(offer) {
  if (offer.shipping_cents === null) return "";
  return offer.shipping_cents === 0 ? " · port offert" : ` · + ${euros(offer.shipping_cents)} de port`;
}

// Dimensions de t_cover_big (IGDB), pour réserver la place avant le chargement.
const COVER_WIDTH = 264;
const COVER_HEIGHT = 352;

/** Jaquette locale si disponible, sinon visuel provisoire à initiales. */
function coverHtml(g, consoles) {
  if (g.cover_url && /^covers\/[a-z0-9-]+\.webp$/.test(g.cover_url)) {
    return `
      <div class="cover has-img">
        <img src="${esc(g.cover_url)}" width="${COVER_WIDTH}" height="${COVER_HEIGHT}" loading="lazy" decoding="async" alt="Jaquette de ${esc(g.title)}">
        <span class="covers">${consoles}</span>
      </div>`;
  }
  return `
      <div class="cover cv-${g._color}" role="img" aria-label="Visuel provisoire de ${esc(g.title)}">
        <span class="covers">${consoles}</span>
        <span class="initials" aria-hidden="true">${esc(initials(g.title))}</span>
        <span class="cover-tag">Visuel provisoire</span>
      </div>`;
}

function cardHtml(g) {
  const status = STATUSES[g.pc_status];
  const consoles = g.platforms
    .map((p) => `<span class="console ${CONSOLES[p].cls}">${CONSOLES[p].label}</span>`)
    .join("");
  const release = g.release_date ? `Sortie le ${esc(frDate(g.release_date))}` : "Date de sortie inconnue";
  const o = g.offer;

  let price;
  if (o) {
    const launch = g.launch_price_cents
      ? `<span class="was">Lancement <s>${euros(g.launch_price_cents)}</s></span>`
      : "";
    const discount = o.discount_pct > 0 ? `<span class="discount">-${o.discount_pct}&nbsp;%</span>` : "";
    const where = `${esc(o.merchant)}, relevé le ${esc(frDate(o.observed_at))}${shippingText(o)}`;
    const source = o.stale
      ? `<p class="source old"><span class="warn-dot" aria-hidden="true"></span><span>${where}. Prix à vérifier.</span></p>`
      : `<p class="source">${where}</p>`;
    price = `
      <div class="price-block">
        ${launch}
        <div class="price-row">
          <span class="price">${bigPrice(o.price_cents)}</span>
          ${discount}
        </div>
      </div>
      ${source}
      <a class="cta" href="${esc(safeUrl(o.url))}" rel="noopener nofollow" target="_blank">Voir l'offre<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M7 17 17 7M8 7h9v9"></path></svg></a>`;
  } else {
    price = `<div class="price-block"><p class="no-price">Pas encore de prix relevé.</p></div>`;
  }

  return `
    <article class="card">
      ${coverHtml(g, consoles)}
      <div class="body">
        <span class="${status.cls}">${status.label}</span>
        <h2 class="title">${esc(g.title)}</h2>
        <p class="sub"><span>${release}</span><span>${o ? FORMATS[o.format] : ""}</span></p>
        ${price}
      </div>
    </article>`;
}

function emptyHtml() {
  const pills = [];
  if (state.platform !== "all") pills.push(PLATFORM_FILTERS[state.platform].label);
  if (state.status !== "all") pills.push(STATUSES[state.status].filter);
  return `
    <div class="empty">
      <div class="empty-icon"><svg width="44" height="44" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 8h12a4 4 0 0 1 4 4v1.5a3.5 3.5 0 0 1-6.2 2.2L14.5 14h-5l-1.3 1.7A3.5 3.5 0 0 1 2 13.5V12a4 4 0 0 1 4-4z"></path><path d="M7 11v3M5.5 12.5h3M16 11.5h.01M18 13.5h.01"></path></svg></div>
      <h2>Aucun jeu ne correspond à ces filtres</h2>
      <div class="active-filters">${pills.map((p) => `<span class="pill">${esc(p)}</span>`).join("")}</div>
      <p>Aucune exclusivité de cette sélection n'est dans le catalogue pour l'instant. Essaie une autre console ou retire le filtre de statut.</p>
      <button class="btn" type="button" data-reset>Réinitialiser les filtres</button>
    </div>`;
}

function render() {
  const list = visibleGames();
  $("count").textContent = `${list.length} ${list.length > 1 ? "jeux" : "jeu"}`;
  $("grid").innerHTML = list.length ? list.map(cardHtml).join("") : emptyHtml();
  for (const btn of document.querySelectorAll("[data-platform]")) {
    btn.setAttribute("aria-pressed", String(btn.dataset.platform === state.platform));
  }
  for (const btn of document.querySelectorAll("[data-status]")) {
    btn.setAttribute("aria-pressed", String(btn.dataset.status === state.status));
  }
  $("sort").value = state.sort;
}

function renderFreshness(generatedAt) {
  const days = Math.floor((Date.now() - Date.parse(generatedAt)) / DAY_MS);
  const date = frDateTime(generatedAt);
  $("updated-date").textContent = date;
  $("updated").hidden = false;
  const stale = days > STALE_BANNER_DAYS;
  $("live").classList.toggle("old", stale);
  $("stale-banner").hidden = !stale;
  if (stale) {
    $("stale-text").innerHTML =
      `<strong>Données anciennes : dernière mise à jour le ${esc(date)}, il y a ${days} jours.</strong> ` +
      "Les prix et les stocks ont pu changer, vérifie chez le marchand avant d'acheter.";
  }
}

// --- Initialisation --------------------------------------------------------------

function bindEvents() {
  $("platform-chips").addEventListener("click", (e) => {
    const btn = e.target.closest("[data-platform]");
    if (btn) { state.platform = btn.dataset.platform; render(); }
  });
  $("status-chips").addEventListener("click", (e) => {
    const btn = e.target.closest("[data-status]");
    if (btn) { state.status = btn.dataset.status; render(); }
  });
  $("sort").addEventListener("change", (e) => { state.sort = e.target.value; render(); });
  $("grid").addEventListener("click", (e) => {
    if (e.target.closest("[data-reset]")) {
      state.platform = "all";
      state.status = "all";
      render();
    }
  });
}

async function init() {
  bindEvents();
  try {
    const response = await fetch("data.json", { cache: "no-cache" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    // Les jeux déjà sortis sur PC ne sont pas affichés.
    games = data.games
      .map((g, i) => ({ ...g, _color: i % 8 }))
      .filter((g) => g.pc_status !== "released");
    renderFreshness(data.generated_at);
    render();
  } catch (err) {
    $("count").textContent = "0 jeu";
    $("grid").innerHTML = `<p class="error">Impossible de charger le catalogue (${esc(err.message)}).</p>`;
  }
}

init();
