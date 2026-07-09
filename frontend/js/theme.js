/* ==================== 主题切换 ==================== */
(function() {
  const html = document.documentElement;
  const saved = localStorage.getItem("ebookforge-theme") || "dark";
  html.setAttribute("data-theme", saved);

  function updateUI() {
    const isDark = html.getAttribute("data-theme") === "dark";
    document.getElementById("theme-icon").textContent = isDark ? "🌙" : "☀️";
    document.getElementById("theme-label").textContent = isDark ? "深色模式" : "浅色模式";
  }

  function toggle() {
    const current = html.getAttribute("data-theme");
    const next = current === "dark" ? "light" : "dark";
    html.setAttribute("data-theme", next);
    localStorage.setItem("ebookforge-theme", next);
    updateUI();
  }

  document.addEventListener("DOMContentLoaded", () => {
    updateUI();
    document.getElementById("theme-toggle").addEventListener("click", toggle);
  });
})();
