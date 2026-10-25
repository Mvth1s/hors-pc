// HorsPC : pages de jeu. Retour au catalogue avec history.back() si possible ; badge « À venir »
// expiré côté client ; bande-annonce sans requête vers YouTube avant le clic (façade, puis iframe).
"use strict";

(() => {
  // Badge « À venir » retiré si la date est passée depuis le dernier build.
  const today = new Date().toLocaleDateString("sv"); // AAAA-MM-JJ
  for (const badge of document.querySelectorAll(".status.soon[data-release]")) {
    if (badge.dataset.release <= today) badge.remove();
  }

  // Retour : history.back() si l'on vient du catalogue de ce site (filtres et défilement
  // retrouvés), sinon le lien mène simplement au catalogue.
  const back = document.querySelector("[data-back]");
  back?.addEventListener("click", (event) => {
    const catalogue = new URL(back.getAttribute("href"), window.location.href);
    let from;
    try { from = new URL(document.referrer); } catch { return; }
    const sameCatalogue = from.origin === catalogue.origin &&
      [catalogue.pathname, `${catalogue.pathname}index.html`].includes(from.pathname);
    if (sameCatalogue && window.history.length > 1) {
      event.preventDefault();
      window.history.back();
    }
  });

  const YOUTUBE_ID = /^[A-Za-z0-9_-]{11}$/;

  for (const facade of document.querySelectorAll(".trailer[data-youtube-id]")) {
    const id = facade.dataset.youtubeId;
    const button = facade.querySelector(".trailer-play");
    if (!button || !YOUTUBE_ID.test(id)) continue;
    button.addEventListener("click", () => {
      const iframe = document.createElement("iframe");
      iframe.src = `https://www.youtube-nocookie.com/embed/${id}?autoplay=1&rel=0`;
      iframe.title = `Bande-annonce de ${facade.dataset.title || "ce jeu"}`;
      iframe.allow = "autoplay; encrypted-media; picture-in-picture; fullscreen";
      iframe.allowFullscreen = true;
      iframe.referrerPolicy = "strict-origin-when-cross-origin";
      iframe.width = "940";
      iframe.height = "529";
      facade.replaceChildren(iframe);
      facade.classList.add("is-playing");
      iframe.focus();
    });
  }
})();
