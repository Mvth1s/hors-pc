// HorsPC : date de mise à jour du catalogue dans le pied de page des pages légales
// (lue dans data.json). Sur le catalogue, app.js s'en charge avec le data.json qu'il charge.
"use strict";

(() => {
  const line = document.querySelector("[data-foot-updated]");
  if (!line || document.querySelector('script[src="app.js"]')) return;
  fetch("data.json", { cache: "no-cache" })
    .then((response) => (response.ok ? response.json() : Promise.reject(response.status)))
    .then((data) => {
      line.querySelector("[data-foot-date]").textContent = new Date(
        data.generated_at,
      ).toLocaleDateString("fr-FR", { timeZone: "Europe/Paris" });
      line.hidden = false;
    })
    .catch(() => {
      // Sans data.json, la ligne reste masquée.
    });
})();
