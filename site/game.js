// HorsPC : bande-annonce des pages de jeu. Aucune requête vers YouTube avant le clic :
// la façade (image + bouton) est remplacée par l'iframe youtube-nocookie.com au clic seulement.
"use strict";

(() => {
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
