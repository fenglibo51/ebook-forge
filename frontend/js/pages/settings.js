/* ==================== 设置页 ==================== */
const SettingsPage = {
  init() {
    this.loadTempStats();
    this.loadStatus();

    document.querySelectorAll(".theme-option").forEach(btn => {
      btn.addEventListener("click", () => {
        const theme = btn.dataset.theme;
        document.querySelectorAll(".theme-option").forEach(b => b.classList.remove("active"));
        btn.classList.add("active");
        document.documentElement.setAttribute("data-theme", theme);
        localStorage.setItem("ebookforge-theme", theme);
        const isDark = theme === "dark";
        document.getElementById("theme-icon").textContent = isDark ? "🌙" : "☀️";
        document.getElementById("theme-label").textContent = isDark ? "深色模式" : "浅色模式";
      });
    });

    document.getElementById("btn-temp-clear").addEventListener("click", async () => {
      if (!confirm("确定清空临时文件？")) return;
      try {
        const result = await API.delete("/convert/temp-clear");
        Toast.success(`已删除 ${result.deleted} 个文件`);
        this.loadTempStats();
      } catch (err) {
        Toast.error(`清理失败: ${err.message}`);
      }
    });
  },

  async loadTempStats() {
    try {
      const stats = await API.get("/convert/temp-stats");
      const el = document.getElementById("temp-stats-info");
      if (stats.file_count === 0) {
        el.textContent = "暂无临时文件";
      } else {
        const totalMB = (stats.total_size / 1024 / 1024).toFixed(1);
        el.textContent = `${stats.file_count} 个文件, ${totalMB} MB`;
      }
    } catch (err) {
      document.getElementById("temp-stats-info").textContent = `加载失败: ${err.message}`;
    }
  },

  async loadStatus() {
    try {
      const status = await API.getStatus();
      const calibreEl = document.getElementById("about-calibre");
      if (status.calibre_available) {
        calibreEl.innerHTML = 'Calibre: <span style="color:var(--success)">✅ 可用</span>';
        document.querySelector("#calibre-status .dot").className = "dot ok";
        document.querySelector("#calibre-status .status-text").textContent = "Calibre 就绪";
      } else {
        calibreEl.innerHTML = 'Calibre: <span style="color:var(--error)">❌ 不可用</span>';
        document.querySelector("#calibre-status .dot").className = "dot err";
        document.querySelector("#calibre-status .status-text").textContent = "Calibre 异常";
      }
      document.getElementById("about-book-path").textContent = `书库路径: ${status.book_path || "—"}`;
    } catch (err) {
      document.getElementById("about-calibre").textContent = `Calibre: 检测失败 (${err.message})`;
    }
  },
};
