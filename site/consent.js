// HorsPC : consentement à la mesure d'audience (Google Analytics 4), conforme aux règles CNIL.
// Aucun script ni requête Google avant un clic sur « Accepter » : gtag.js est injecté ensuite
// seulement (pas de Consent Mode). Fermer le bandeau vaut refus. Le choix est redemandé après
// CONSENT_MONTHS mois.
"use strict";

(() => {
  const GA_ID = "G-TMFL6HQ430";
  const STORAGE_KEY = "horspc-consent";
  const CONSENT_MONTHS = 6;
  // Durée de vie des cookies _ga limitée à 13 mois (recommandation CNIL), en secondes.
  const COOKIE_EXPIRES = 13 * 30 * 24 * 3600;

  function readChoice() {
    try {
      const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || "null");
      if (!saved || !["granted", "denied"].includes(saved.choice)) return null;
      const expiry = new Date(saved.date);
      expiry.setMonth(expiry.getMonth() + CONSENT_MONTHS);
      return Number.isNaN(expiry.getTime()) || expiry < new Date() ? null : saved.choice;
    } catch {
      return null;
    }
  }

  function saveChoice(choice) {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify({ choice, date: new Date().toISOString() }));
    } catch {
      // Stockage indisponible : le choix ne vaut que pour cette page.
    }
  }

  function loadAnalytics() {
    if (window.gtag) return;
    const script = document.createElement("script");
    script.async = true;
    script.src = `https://www.googletagmanager.com/gtag/js?id=${GA_ID}`;
    document.head.appendChild(script);
    window.dataLayer = window.dataLayer || [];
    window.gtag = function gtag() {
      window.dataLayer.push(arguments);
    };
    window.gtag("js", new Date());
    window.gtag("config", GA_ID, {
      allow_google_signals: false,
      allow_ad_personalization_signals: false,
      cookie_expires: COOKIE_EXPIRES,
    });
  }

  /** Supprime les cookies _ga* sur l'hôte courant et ses domaines parents. */
  function deleteAnalyticsCookies() {
    const parts = window.location.hostname.split(".");
    const domains = [""];
    for (let i = 0; i < parts.length - 1; i += 1) domains.push(`; domain=.${parts.slice(i).join(".")}`);
    for (const cookie of document.cookie.split(";")) {
      const name = cookie.split("=")[0].trim();
      if (!name.startsWith("_ga")) continue;
      for (const domain of domains) {
        document.cookie = `${name}=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/${domain}`;
      }
    }
  }

  const privacyHref = (() => {
    const link = document.querySelector('a[href$="confidentialite.html"]');
    return link ? link.getAttribute("href") : "confidentialite.html";
  })();

  function closeBanner() {
    document.getElementById("consent")?.remove();
  }

  function decide(choice) {
    const previous = readChoice();
    saveChoice(choice);
    closeBanner();
    if (choice === "granted") {
      loadAnalytics();
    } else if (previous === "granted" || window.gtag) {
      // Retrait du consentement : on efface les cookies et on recharge sans GA.
      deleteAnalyticsCookies();
      window.location.reload();
    }
  }

  function openBanner() {
    if (document.getElementById("consent")) return;
    const current = readChoice();
    const status =
      current === "granted"
        ? "Vous avez actuellement accepté la mesure d'audience."
        : current === "denied"
          ? "Vous avez actuellement refusé la mesure d'audience."
          : "";
    const banner = document.createElement("section");
    banner.id = "consent";
    banner.className = "consent";
    banner.setAttribute("role", "dialog");
    banner.setAttribute("aria-labelledby", "consent-title");
    banner.innerHTML = `
      <div class="consent-inner">
        <button type="button" class="consent-close" data-consent="denied" aria-label="Fermer (refuser)">×</button>
        <h2 id="consent-title">Mesure d'audience</h2>
        <p>Avec votre accord, HorsPC utilise Google Analytics 4 pour mesurer sa fréquentation (pages vues, durée, type d'appareil), sans publicité ni personnalisation. Les données sont transmises à Google, qui peut les transférer hors de l'Union européenne, notamment aux États-Unis. Refuser n'a aucune conséquence sur l'utilisation du site. ${status}</p>
        <p class="consent-more"><a data-consent-privacy>En savoir plus et gérer vos choix</a></p>
        <div class="consent-actions">
          <button type="button" class="consent-btn" data-consent="denied">Refuser</button>
          <button type="button" class="consent-btn" data-consent="granted">Accepter</button>
        </div>
      </div>`;
    banner.querySelector("[data-consent-privacy]").setAttribute("href", `${privacyHref}#cookies`);
    banner.addEventListener("click", (event) => {
      const button = event.target.closest("[data-consent]");
      if (button) decide(button.dataset.consent);
    });
    document.body.appendChild(banner);
  }

  document.addEventListener("click", (event) => {
    const link = event.target.closest("[data-consent-open]");
    if (link) {
      event.preventDefault();
      openBanner();
    }
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && document.getElementById("consent")) decide("denied");
  });

  const choice = readChoice();
  if (choice === "granted") loadAnalytics();
  else if (choice === null) openBanner();
})();
