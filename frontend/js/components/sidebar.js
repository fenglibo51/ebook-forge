/* ==================== 侧边栏 ==================== */
(function() {
  document.addEventListener("DOMContentLoaded", () => {
    const navItems = document.querySelectorAll(".nav-item");
    const pages = document.querySelectorAll(".page");

    function navigate(pageId) {
      // 更新导航高亮
      navItems.forEach(item => {
        const isActive = item.dataset.page === pageId;
        item.classList.toggle("active", isActive);
      });

      // 切换页面
      pages.forEach(page => {
        const isActive = page.id === `page-${pageId}`;
        page.classList.toggle("active", isActive);
      });

      // 更新 URL hash
      window.location.hash = pageId;

      // 通知各页面：切换后可按需即时刷新（如转换记录页）
      window.dispatchEvent(new CustomEvent("hermes:pagechange", { detail: { page: pageId } }));
    }

    navItems.forEach(item => {
      item.addEventListener("click", (e) => {
        e.preventDefault();
        navigate(item.dataset.page);
      });
    });

    // 从 hash 初始化
    const hash = window.location.hash.replace("#", "") || "convert";
    navigate(hash);

    // ===== 侧边栏收放 =====
    const sidebar = document.getElementById("sidebar");
    const toggleBtn = document.getElementById("btn-toggle-sidebar");
    const STORAGE_KEY = "ebookforge:sidebar-collapsed";

    function applyCollapsed(collapsed) {
      sidebar.classList.toggle("collapsed", collapsed);
      toggleBtn.textContent = collapsed ? "»" : "«";
      toggleBtn.title = collapsed ? "展开侧边栏" : "收起侧边栏";
    }

    if (sidebar && toggleBtn) {
      // 恢复上次状态（刷新后保持）
      applyCollapsed(localStorage.getItem(STORAGE_KEY) === "1");
      toggleBtn.addEventListener("click", () => {
        const next = !sidebar.classList.contains("collapsed");
        applyCollapsed(next);
        localStorage.setItem(STORAGE_KEY, next ? "1" : "0");
      });
    }

    // 暴露导航函数供全局使用
    window.navigateTo = navigate;
  });
})();
