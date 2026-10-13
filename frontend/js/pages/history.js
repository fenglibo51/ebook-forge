/* ==================== 转换记录页 ==================== */
const HistoryPage = {
  filterStatus: "",
  refreshTimer: null,
  _lastSnapshot: null,
  _hasActive: false,

  init() {
    this.bindEvents();
    this.load(false);
    this.startAutoRefresh();
  },

  startAutoRefresh(delay = 3000) {
    this.stopAutoRefresh();
    this.refreshTimer = setTimeout(async () => {
      // 只在转换记录页可见时刷新
      const page = document.getElementById("page-history");
      if (page && page.classList.contains("active")) {
        await this.load(true);
      }
      // 有进行中任务时 3 秒刷新，全部结束则降频，避免无谓空转
      this.startAutoRefresh(this._hasActive ? 3000 : 15000);
    }, delay);
  },

  stopAutoRefresh() {
    if (this.refreshTimer) {
      clearTimeout(this.refreshTimer);
      this.refreshTimer = null;
    }
  },

  bindEvents() {
    document.getElementById("btn-refresh-history").addEventListener("click", () => this.load(true));
    document.getElementById("history-filter").addEventListener("change", (e) => {
      this.filterStatus = e.target.value;
      this.load(true);
    });
    document.getElementById("btn-clear-history").addEventListener("click", async () => {
      if (!confirm("确定清除所有转换记录？此操作不可恢复。")) return;
      try {
        await API.clearHistory();
        Toast.success("历史已清除");
        this.load(true);
      } catch (err) {
        Toast.error("清除失败: " + err.message);
      }
    });
    // 切入本页时立即刷新一次（静默，不闪）
    window.addEventListener("hermes:pagechange", (e) => {
      if (e.detail && e.detail.page === "history") this.load(true);
    });
  },

  async load(silent = true) {
    const tbody = document.getElementById("history-body");
    // 仅首次加载显示骨架屏；后续静默刷新，避免整表重建导致闪动
    if (!silent) {
      tbody.innerHTML = '<tr><td colspan="5"><div class="loading"><span class="spinner"></span> 加载中...</div></td></tr>';
      this._lastSnapshot = null;
    }

    try {
      const data = await API.getHistory(this.filterStatus || null);

      document.getElementById("hist-total").textContent = data.stats.total;
      document.getElementById("hist-ok").textContent = data.stats.success;
      document.getElementById("hist-fail").textContent = data.stats.failed;
      document.getElementById("hist-run").textContent = data.stats.running;

      // 记录是否有进行中任务，供轮询调速使用
      this._hasActive = data.tasks.some(t => t.status === "running" || t.status === "pending");

      // 数据快照对比：无变化则完全不动 DOM（消除每 3 秒闪动）
      const snapshot = this.filterStatus + "|" + data.tasks.map(t =>
        [t.task_id, t.status, t.output_path || "", t.completed_at || ""].join("~")
      ).join("|");
      if (snapshot === this._lastSnapshot) return;
      this._lastSnapshot = snapshot;

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
      if (!silent) {
        tbody.innerHTML = '<tr><td colspan="5"><div class="loading" style="color:var(--error)">加载失败: ' + err.message + '</div></td></tr>';
      }
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
