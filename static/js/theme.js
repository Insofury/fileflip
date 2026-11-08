// Applies the saved theme before first paint (no flash of the wrong theme).
(function () {
  try {
    var theme = localStorage.getItem("fileflip:theme");
    if (theme === "light" || theme === "dark") document.documentElement.setAttribute("data-theme", theme);
  } catch (e) {
    /* storage unavailable */
  }
})();
