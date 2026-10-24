// HorsPC : thème clair/sombre commun à toutes les pages (prefers-color-scheme + bouton).
// Le choix explicite est mémorisé en localStorage sous la clé THEME_KEY.
"use strict";

(() => {
  const THEME_KEY = "horspc-theme";
  const darkQuery = window.matchMedia("(prefers-color-scheme: dark)");

  function storedTheme() {
    try {
      const t = localStorage.getItem(THEME_KEY);
      return t === "light" || t === "dark" ? t : null;
    } catch {
      return null;
    }
  }

  function setClasses(theme, explicit) {
    const body = document.body;
    body.classList.toggle("dark", explicit && theme === "dark");
    body.classList.toggle("light", explicit && theme === "light");
    body.classList.toggle("is-dark", theme === "dark");
    const button = document.getElementById("theme-toggle");
    if (button) {
      button.setAttribute(
        "aria-label",
        theme === "dark" ? "Passer en thème clair" : "Passer en thème sombre",
      );
    }
  }

  function applyTheme() {
    const stored = storedTheme();
    setClasses(stored || (darkQuery.matches ? "dark" : "light"), stored !== null);
  }

  function toggleTheme() {
    const next = document.body.classList.contains("is-dark") ? "light" : "dark";
    try {
      localStorage.setItem(THEME_KEY, next);
    } catch {
      // Stockage indisponible (navigation privée) : le choix ne vaut que pour cette page.
    }
    setClasses(next, true);
  }

  applyTheme();
  const button = document.getElementById("theme-toggle");
  if (button) button.addEventListener("click", toggleTheme);
  darkQuery.addEventListener("change", applyTheme);
})();
