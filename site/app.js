// HorsPC : filtres, tris et bannière d'ancienneté sur le catalogue rendu au build (index.html).
// Sans JS, toutes les fiches restent lisibles ; le thème est géré par theme.js.
"use strict";

// Au-delà de ce nombre de jours depuis generated_at, une bannière d'alerte s'affiche.
// Seuil distinct de STALE_ISSUE_DAYS (8 j) dans .github/workflows/update.yml.
const STALE_BANNER_DAYS = 14;

const DAY_MS = 86400000;

// Filtres console : « Switch 2 » couvre aussi les jeux Switch (rétrocompatibles).
const PLATFORM_FILTERS = {
  ps5: { label: "PS5", platforms: ["ps5"] },
  switch2: { label: "Switch 2", platforms: ["switch", "switch2"] },
  xbox_series: { label: "Xbox Series", platforms: ["xbox_series"] },
};
const STATUSES = {
  none: { filter: "Exclusif" },
  announced: { filter: "Portage annoncé" },
};

const state = { platform: "all", status: "all", sort: "price" };

const $ = (id) => document.getElementById(id);
const cards = () => [...$("grid").querySelectorAll("article.card")];

function esc(value) {
  return String(value).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);
}

/** Valeur de tri d'une fiche, null si absente (placée en fin de liste). */
function sortValue(card, key) {
  const raw = card.dataset[key];
  if (!raw) return null;
  return key === "release" ? raw : Number(raw);
}

function byKey(key, desc) {
  return (a, b) => {
    const ka = sortValue(a, key);
    const kb = sortValue(b, key);
    const titles = a.dataset.title.localeCompare(b.dataset.title, "fr");
    if (ka === null && kb === null) return titles;
    if (ka === null) return 1;
    if (kb === null) return -1;
    if (ka === kb) return titles;
    return (ka < kb ? -1 : 1) * (desc ? -1 : 1);
  };
}

const SORTS = {
  price: byKey("price", false),
  discount: byKey("discount", true),
  release: byKey("release", true),
};

function matches(card) {
  const platforms = card.dataset.platforms.split(" ");
  const platformOk =
    state.platform === "all" ||
    platforms.some((p) => PLATFORM_FILTERS[state.platform].platforms.includes(p));
  return platformOk && (state.status === "all" || card.dataset.status === state.status);
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
  const grid = $("grid");
  const all = cards().sort(SORTS[state.sort]);
  let visible = 0;
  for (const card of all) {
    card.hidden = !matches(card);
    if (!card.hidden) visible += 1;
    grid.appendChild(card); // réordonne sans recréer les fiches
  }
  grid.querySelector(".empty")?.remove();
  if (visible === 0) grid.insertAdjacentHTML("beforeend", emptyHtml());
  $("count").textContent = `${visible} ${visible > 1 ? "jeux" : "jeu"}`;
  for (const btn of document.querySelectorAll("[data-platform]")) {
    btn.setAttribute("aria-pressed", String(btn.dataset.platform === state.platform));
  }
  for (const btn of document.querySelectorAll("[data-status]")) {
    btn.setAttribute("aria-pressed", String(btn.dataset.status === state.status));
  }
  $("sort").value = state.sort;
  syncUrl();
}

/** Filtres et tri dans l'URL (sans nouvelle entrée d'historique) : le retour depuis une page de
 * jeu les retrouve, même si le navigateur recharge le catalogue. */
function syncUrl() {
  const params = new URLSearchParams();
  if (state.platform !== "all") params.set("console", state.platform);
  if (state.status !== "all") params.set("statut", state.status);
  if (state.sort !== "price") params.set("tri", state.sort);
  const query = params.toString();
  const url = window.location.pathname + (query ? `?${query}` : "") + window.location.hash;
  if (url !== window.location.pathname + window.location.search + window.location.hash) {
    window.history.replaceState(window.history.state, "", url);
  }
}

function renderFreshness() {
  const generatedAt = $("updated")?.dataset.generatedAt;
  if (!generatedAt) return;
  const days = Math.floor((Date.now() - Date.parse(generatedAt)) / DAY_MS);
  const stale = days > STALE_BANNER_DAYS;
  $("live").classList.toggle("old", stale);
  $("stale-banner").hidden = !stale;
  if (stale) {
    const date = $("updated-date").textContent;
    $("stale-text").innerHTML =
      `<strong>Données anciennes : dernière mise à jour le ${esc(date)}, il y a ${days} jours.</strong> ` +
      "Les prix et les stocks ont pu changer, vérifie chez le marchand avant d'acheter.";
  }
}

/** Retire « À venir » des jeux sortis depuis le dernier build (date locale du visiteur). */
function expireUpcoming() {
  const today = new Date().toLocaleDateString("sv"); // AAAA-MM-JJ
  for (const badge of document.querySelectorAll(".status.soon[data-release]")) {
    const release = badge.dataset.release;
    if (release > today) continue;
    const sub = badge.closest("article")?.querySelector(".sub span");
    if (sub) sub.textContent = `Sortie le ${release.split("-").reverse().join("/")}`;
    badge.remove();
  }
}

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

function init() {
  // Liens du pied de page (./?console=ps5…) et retour depuis une page de jeu : l'URL porte
  // les filtres et le tri.
  const params = new URLSearchParams(window.location.search);
  const consoleParam = params.get("console") ?? "";
  const statusParam = params.get("statut") ?? "";
  const sortParam = params.get("tri") ?? "";
  if (Object.hasOwn(PLATFORM_FILTERS, consoleParam)) state.platform = consoleParam;
  if (Object.hasOwn(STATUSES, statusParam)) state.status = statusParam;
  if (Object.hasOwn(SORTS, sortParam)) state.sort = sortParam;
  bindEvents();
  expireUpcoming();
  renderFreshness();
  render();
}

init();
