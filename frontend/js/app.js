/* ==================== 主应用初始化 ==================== */
(function() {
  document.addEventListener("DOMContentLoaded", () => {
    // 按顺序初始化各页面
    ConvertPage.init();
    LibraryPage.init();
    HistoryPage.init();
    SettingsPage.init();
  });
})();
