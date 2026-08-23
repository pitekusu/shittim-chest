(function () {
  "use strict";
  var storageKey = "shittim-records-theme-v1";
  var theme;
  try {
    var stored = window.localStorage.getItem(storageKey);
    if (stored === "light" || stored === "dark") theme = stored;
  } catch {
    // Storage can be unavailable in hardened browsers; the OS preference remains usable.
  }
  if (!theme) {
    theme = window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }
  document.documentElement.dataset.theme = theme;
  document.documentElement.style.colorScheme = theme;
  var themeColor = document.querySelector('meta[name="theme-color"]');
  if (themeColor) themeColor.content = theme === "dark" ? "#071724" : "#f5fbff";
})();
