/* ==================== 转换记录页 ==================== */
const HistoryPage = {
  filterStatus: "",
  refreshTimer: null,

  init() {
    this.bindEvents();
    this.load();
    this.startAutoRefresh();
  },

  startAutoRefresh() {
    this.stopAutoRefresh();
    this.refreshTimer = setInterval(() => this.load(), 3000);
  },

  stopAutoRefresh() {
    if (this.refreshTimer) {
      clearInterval(this.refreshTimer);
      this.refreshTimer = null;
    }
  },

  bindEvents() {
    document.getElementById("btn-refresh-history").addEventListener("click", () => this.load());
    document.getElementById("history-filter").addEventListener("change", (e) => {
      this.filterStatus = e.target.value;
      this.load();
    });
    document.getElementById("btn-clear-history").addEventListener("click", async () => {
      if (!confirm("确定清除所有转换记录？此操作不可恢复。")) return;
      try {
        await API.clearHistory();
        Toast.success("历史已清除");
        this.load();
      } catch (err) {
        Toast.error("清除失败: " + err.message);
      }
    });
  },

  async load() {
    const tbody = document.getElementById("history-body");
    tbody.innerHTML = '<tr><td colspan="5"><div class="loading"><span class="spinner"></span> 加载中...</div></td></tr>';

    try {
      const data = await API.getHistory(this.filterStatus || null);

      document.getElementById("hist-total").textContent = data.stats.total;
      document.getElementById("hist-ok").textContent = data.stats.success;
      document.getElementById("hist-fail").textContent = data.stats.failed;
      document.getElementById("hist-run").textContent = data.stats.running;

      if (data.tasks.length === 0) {
        tbody.innerHTML = '<tr><td colspan="5"><div class="loading">暂无转换记录</div></td></tr>';
        return;
      }

      tbody.innerHTML = data.tasks.map(task => {
        const statusClass = "hist-" + task.status;
        const icons = { success: "✅", failed: "❌", running: "🔄", pending: "⏳" };
        const fn = (task.source_path || "").split("/").pop() || task.task_id;
        const time = new Date(task.created_at).toLocaleString();

        let actionHtml = "";
        if (task.status === "failed") {
          actionHtml = '<button class="btn btn-sm" onclick="HistoryPage.retry(\'' + task.task_id + '\')">重试</button>';
        } else if (task.status === "success") {
            const outPath = task.output_path || "";
          if (outPath.startsWith("/temp/")) {
            const fname = encodeURIComponent(outPath.split("/").pop());
            actionHtml = '<button class="btn btn-sm" onclick="HistoryPage.downloadFile(\'' + fname + '\')">📥 下载</button>';
          } else {
            actionHtml = '<span style="font-size:11px;color:var(--text-muted)">' + this.formatDuration(task.created_at, task.completed_at) + '</span>';
          }
        }

        return '<tr>' +
          '<td><span class="hist-status ' + statusClass + '">' + (icons[task.status] || "•") + " " + this.getStatusText(task.status) + '</span></td>' +
          '<td style="max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="' + (task.source_path || "") + '">' + fn + '</td>' +
          '<td>→ ' + (task.target_format ? task.target_format.toUpperCase() : "?") + '</td>' +
          '<td style="font-size:12px;color:var(--text-muted)">' + time + '</td>' +
          '<td>' + actionHtml + '</td>' +
        '</tr>';
      }).join("");

    } catch (err) {
      tbody.innerHTML = '<tr><td colspan="5"><div class="loading" style="color:var(--error)">加载失败: ' + err.message + '</div></td></tr>';
    }
  },

  async downloadFile(filename) {
    try {
      const resp = await fetch("/api/convert/download/" + filename, { method: "HEAD" });
      if (!resp.ok) {
        Toast.error("文件已过期或已被清理");
        return;
      }
      window.open("/api/convert/download/" + filename, "_blank");
    } catch (e) {
      Toast.error("下载失败: " + e.message);
    }
  },

  async retry(taskId) {
    try {
      const result = await API.retryTask(taskId);
      Toast.success("已重试");
      this.load();
    } catch (err) {
      Toast.error("重试失败: " + err.message);
    }
  },

  getStatusText(status) {
    const map = { success: "成功", failed: "失败", running: "运行中", pending: "排队中" };
    return map[status] || status;
  },

  formatDuration(start, end) {
    if (!start || !end) return "";
    const s = new Date(start);
    const e = new Date(end);
    const sec = Math.round((e - s) / 1000);
    if (sec < 60) return sec + "s";
    return Math.floor(sec / 60) + "m" + (sec % 60) + "s";
  },
};
