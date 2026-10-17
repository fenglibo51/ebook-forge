/* ==================== 书库浏览页 ==================== */
const LibraryPage = {
  currentPath: "",
  filterExt: "",
  selected: new Set(),
  allItems: [],
  currentTasks: [],
  libRunningLogged: new Set(),

  init() {
    this.bindEvents();
    this.load("");
  },

  bindEvents() {
    document.getElementById("btn-refresh").addEventListener("click", () => this.load(this.currentPath));

    // 全选
    document.getElementById("btn-select-all").addEventListener("click", async () => {
      const btn = document.getElementById("btn-select-all");

      // 已经全选过 → 再点一次就是「取消全选」
      if (btn.dataset.allSelected === "1") {
        this.selected.clear();
        btn.dataset.allSelected = "0";
        btn.textContent = "☑️ 全选";
        this.renderItems();
        this.updateBatchBar();
        return;
      }

      // 当前目录下的文件直接选中
      this.allItems.filter(it => !it.is_dir).forEach(f => this.selected.add(f.path));

      // 若当前目录还有子目录（漫画书库常见：comics/<系列>/<卷>.pdf），
      // 递归收集子目录里的文件 —— 否则在这一层点「全选」永远是空的
      if (this.allItems.some(it => it.is_dir)) {
        const oldText = btn.textContent;
        btn.disabled = true;
        btn.textContent = "⏳ 扫描中...";
        try {
          const data = await API.listFiles(this.currentPath || "", this.filterExt || null);
          data.files.forEach(f => this.selected.add(f.path));
          Toast.success(`已选中 ${this.selected.size} 个文件`);
        } catch (e) {
          Toast.error(`扫描失败: ${e.message}`);
        } finally {
          btn.disabled = false;
          btn.textContent = oldText;
        }
      }

      btn.dataset.allSelected = "1";
      btn.textContent = "☐ 取消全选";
      this.renderItems();
      this.updateBatchBar();
    });

    document.getElementById("library-search").addEventListener("input", (e) => {
      this.renderItems(e.target.value);
    });

    document.getElementById("library-filter").addEventListener("change", (e) => {
      this.filterExt = e.target.value;
      this.load(this.currentPath);
    });

    // 批量操作
    document.getElementById("btn-batch-convert").addEventListener("click", () => this.batchConvert());
    document.getElementById("btn-batch-rename").addEventListener("click", () => this.openRenameModal());
    document.getElementById("btn-clear-selection").addEventListener("click", () => this.clearSelection());

    // 进度关闭按钮
    document.getElementById("btn-lib-close-progress").addEventListener("click", () => {
      document.getElementById("library-progress").style.display = "none";
    });

    // 重命名弹窗
    document.querySelector("#rename-modal .modal-close").addEventListener("click", () => this.closeRenameModal());
    document.querySelector("#rename-modal .modal-backdrop").addEventListener("click", () => this.closeRenameModal());
    document.getElementById("btn-rename-cancel").addEventListener("click", () => this.closeRenameModal());
    document.getElementById("btn-rename-confirm").addEventListener("click", () => this.confirmRename());

    document.querySelectorAll("#rename-modal .preset-btn").forEach(btn => {
      btn.addEventListener("click", () => {
        document.querySelectorAll("#rename-modal .preset-btn").forEach(b => b.classList.remove("active"));
        btn.classList.add("active");
        document.getElementById("rename-rule").value = btn.dataset.rule;
        this.previewRename();
      });
    });

    document.getElementById("rename-rule").addEventListener("input", () => this.previewRename());
  },

  async load(subdir) {
    this.currentPath = subdir || "";
    const grid = document.getElementById("library-grid");
    grid.innerHTML = '<div class="loading"><span class="spinner"></span> 加载中...</div>';

    try {
      const data = await API.browse(this.currentPath, this.filterExt);
      this.allItems = data.items;

      // 面包屑
      const bc = document.getElementById("library-breadcrumb");
      let crumbs = [{ name: "Book", path: "" }];
      if (this.currentPath) {
        const parts = this.currentPath.split("/");
        let p = "";
        for (const part of parts) {
          p = p ? `${p}/${part}` : part;
          crumbs.push({ name: part, path: p });
        }
      }
      bc.innerHTML = crumbs.map((c, i) =>
        `<span class="crumb-item" data-path="${c.path}" ${i === crumbs.length - 1 ? 'style="pointer-events:none;color:var(--text-primary)"' : ""}>${c.name}</span>`
      ).join("");
      bc.querySelectorAll(".crumb-item").forEach(el => {
        if (el.style.pointerEvents !== "none") {
          el.addEventListener("click", () => this.load(el.dataset.path));
        }
      });

      this.renderItems();
    } catch (err) {
      grid.innerHTML = `<div class="loading" style="color:var(--error)">加载失败: ${err.message}</div>`;
    }
  },

  renderItems(searchText) {
    const grid = document.getElementById("library-grid");
    let items = this.allItems;

    if (searchText) {
      const q = searchText.toLowerCase();
      items = items.filter(it => it.name.toLowerCase().includes(q));
    }

    grid.innerHTML = items.map(item => {
      const icon = item.is_dir ? "📁" : this.getFormatIcon(item.ext);
      const isSelected = this.selected.has(item.path);
      const meta = item.is_dir ? "目录" : `${item.ext?.toUpperCase() || "?"} · ${this.formatSize(item.size)}`;
      if (item.is_dir) {
        return `<div class="lib-item dir-item" data-path="${item.path}" data-is-dir="true">
        <span class="item-icon">📁</span>
        <div class="item-info">
          <div class="item-name">${item.name}</div>
          <div class="item-meta">目录</div>
        </div>
        <div class="item-actions">
          <button class="btn btn-sm btn-ghost open-dir" title="打开目录">📂</button>
        </div>
      </div>`;
      }
      return `<div class="lib-item ${isSelected ? "selected" : ""}" data-path="${item.path}" data-is-dir="false">
        <input type="checkbox" class="item-checkbox" ${isSelected ? "checked" : ""}>
        <span class="item-icon">${icon}</span>
        <div class="item-info">
          <div class="item-name">${item.name}</div>
          <div class="item-meta">${meta}</div>
        </div>
        <div class="item-actions"></div>
      </div>`;
    }).join("");

    // 绑定事件
    grid.querySelectorAll(".lib-item").forEach(el => {
      const path = el.dataset.path;
      const isDir = el.dataset.isDir === "true";
      const checkbox = el.querySelector(".item-checkbox");

      if (isDir) {
        // 点击、双击、📂 按钮都打开目录
        el.addEventListener("click", (e) => {
          const btn = el.querySelector(".open-dir");
          if (btn && btn.contains(e.target)) {
            e.stopPropagation();
            this.load(path);
            return;
          }
          this.load(path);
        });
      } else {
        checkbox.addEventListener("change", () => {
          if (checkbox.checked) this.selected.add(path);
          else this.selected.delete(path);
          el.classList.toggle("selected", checkbox.checked);
          this.updateBatchBar();
        });
        // 点击行也切换
        el.addEventListener("click", (e) => {
          if (e.target.tagName !== "BUTTON" && e.target.tagName !== "INPUT") {
            checkbox.checked = !checkbox.checked;
            checkbox.dispatchEvent(new Event("change"));
          }
        });
      }
    });
  },

  updateBatchBar() {
    const bar = document.getElementById("batch-bar");
    const count = this.selected.size;

    // 选中被清空时（点取消选择、切换目录），全选按钮恢复初始文案
    if (count === 0) {
      const allBtn = document.getElementById("btn-select-all");
      if (allBtn && allBtn.dataset.allSelected === "1") {
        allBtn.dataset.allSelected = "0";
        allBtn.textContent = "☑️ 全选";
      }
    }
    if (count > 0) {
      bar.style.display = "flex";
      document.getElementById("batch-count").textContent = count;
    } else {
      bar.style.display = "none";
    }
  },

  clearSelection() {
    this.selected.clear();
    this.renderItems();
    this.updateBatchBar();
  },

  async batchConvert() {
    const formatEl = document.getElementById("batch-format");
    const format = formatEl.value;
    if (!format) {
      Toast.error("请先选择目标格式（下拉框现在还是「转换格式...」占位提示）");
      formatEl.focus();
      return;
    }
    if (this.selected.size === 0) { Toast.error("未选择文件"); return; }

    const keepSrc = document.getElementById("batch-keep-source")?.checked ?? true;
    const n = this.selected.size;

    // 取格式支持矩阵：提前判断哪些选中文件转不了目标格式（否则要等转完才看到一堆失败）
    let matrix = {};
    try {
      const m = await API.getMatrix();
      matrix = m.matrix || {};
    } catch (e) {
      console.warn("获取格式矩阵失败，跳过兼容性校验", e);
    }
    const hasMatrix = Object.keys(matrix).length > 0;

    // 按格式统计选中文件
    const extCount = {};
    this.selected.forEach(p => {
      const mm = p.match(/\.([^./\\]+)$/);
      const ext = mm ? mm[1].toLowerCase() : "未知";
      extCount[ext] = (extCount[ext] || 0) + 1;
    });

    const rows = Object.entries(extCount)
      .map(([ext, cnt]) => ({
        ext, cnt,
        ok: !hasMatrix || (matrix[ext] || []).includes(format),
      }))
      .sort((a, b) => b.cnt - a.cnt);

    const unsupportedRows = rows.filter(r => !r.ok);
    const unsupportedCount = unsupportedRows.reduce((s, r) => s + r.cnt, 0);
    const convertibleCount = n - unsupportedCount;

    const fmtLines = rows
      .map(r => `  · ${r.ext.toUpperCase()}：${r.cnt} 个${r.ok ? "" : "   ⚠️ 不支持"}`)
      .join("\n");

    // 转换前统一确认：数量 + 格式构成 + 目标格式 + 是否保留原文件
    let msg = "确认执行批量转换？\n\n";
    msg += `文件数量：${n} 个\n`;
    msg += fmtLines + "\n\n";
    msg += `目标格式：${format.toUpperCase()}\n`;
    msg += keepSrc
      ? "保留原文件：是\n"
      : "保留原文件：否（转换成功后将删除源文件，不可恢复）\n";

    if (unsupportedCount > 0) {
      msg += `\n⚠️ 其中 ${unsupportedCount} 个文件不支持转为 ${format.toUpperCase()}：\n`;
      msg += unsupportedRows.map(r => `     ${r.ext.toUpperCase()}（${r.cnt} 个）`).join("\n") + "\n";
      msg += `   这些会被自动跳过，实际只转换 ${convertibleCount} 个。\n`;
    }
    if (n > 20 && convertibleCount > 0) {
      msg += "\n⚠️ 数量较多，转换会持续较长时间，期间机器负载会升高。";
    }
    msg += "\n\n确定继续吗？";
    if (!confirm(msg)) return;

    if (convertibleCount === 0) {
      Toast.error(`选中的文件都不支持转为 ${format.toUpperCase()}`);
      return;
    }

    // 只提交支持该目标格式的文件
    const files = [];
    this.selected.forEach(p => {
      const mm = p.match(/\.([^./\\]+)$/);
      const ext = mm ? mm[1].toLowerCase() : "";
      if (!hasMatrix || (matrix[ext] || []).includes(format)) {
        files.push({
          source_path: p,
          target_format: format,
          keep_source: keepSrc,
          use_original_name: true,
        });
      }
    });

    // 显示进度区域
    const area = document.getElementById("library-progress");
    area.style.display = "block";
    const log = document.getElementById("lib-progress-log");
    log.innerHTML = "";
    document.getElementById("lib-progress-summary").style.display = "none";
    document.getElementById("lib-progress-fill").style.width = "0%";

    files.forEach(f => {
      const fn = f.source_path.split("/").pop();
      this.addLibLog("pending", `${fn} 等待中...`);
    });

    try {
      const result = await API.batchConvert({ files });
      const taskIds = result.task_ids || [];
      this.currentTasks = taskIds;
      this.libRunningLogged = new Set();
      this.addLibLog("running", `已启动 ${taskIds.length} 个转换任务`);
      this.pollLibraryTasks(taskIds, files);
    } catch (err) {
      this.addLibLog("error", `批量转换失败: ${err.message}`);
      Toast.error(`批量转换失败: ${err.message}`);
    }
  },

  pollLibraryTasks(taskIds, files) {
    if (taskIds.length === 0) return;
    let success = 0, failed = 0;
    let completed = new Set();
    const total = taskIds.length;
    const startTime = Date.now();

    const check = async () => {
      for (const tid of taskIds) {
        if (completed.has(tid)) continue;
        try {
          const task = await API.getTask(tid);
          const fn = task.source_path?.split("/").pop() || tid;
          if (task.status === "success" || task.status === "failed") {
            completed.add(tid);
            if (task.status === "success") {
              this.addLibLog("success", `${fn} → ${task.target_format?.toUpperCase()} 完成`);
              success++;
            } else {
              this.addLibLog("error", `${fn} 失败`, task.error);
              failed++;
            }
          } else if (task.status === "running" && !this.libRunningLogged.has(tid)) {
            this.libRunningLogged.add(tid);
            this.addLibLog("running", `${fn} 转换中...`);
          }
        } catch (e) {}
      }

      const done = completed.size;
      const pct = total > 0 ? Math.round((done / total) * 100) : 0;
      document.getElementById("lib-progress-stats").textContent = `${done}/${total}`;
      document.getElementById("lib-progress-fill").style.width = `${pct}%`;

      if (done >= total) {
        const elapsed = Math.round((Date.now() - startTime) / 1000);
        document.getElementById("lib-progress-stats").textContent = `${done}/${total} 完成`;
        document.getElementById("lib-progress-fill").style.width = "100%";
        document.getElementById("lib-summary-ok").textContent = `成功: ${success}`;
        document.getElementById("lib-summary-fail").textContent = `失败: ${failed}`;
        document.getElementById("lib-summary-time").textContent = `用时: ${elapsed}s`;
        document.getElementById("lib-progress-summary").style.display = "flex";
        document.getElementById("btn-lib-retry").onclick = () => this.retryLibraryFailed();

        this.currentTasks = [];
        this.selected.clear();
        this.updateBatchBar();

        const msg = failed > 0 ? `转换完成: ${success} 成功, ${failed} 失败` : `✅ ${success} 个文件转换完成`;
        if (failed > 0) Toast.error(msg); else Toast.success(msg);
        return;
      }
      setTimeout(check, 2000);
    };
    check();
  },

  addLibLog(type, msg, detail) {
    const log = document.getElementById("lib-progress-log");
    const time = new Date().toLocaleTimeString();
    const icons = { success: "✅", error: "❌", running: "🔄", pending: "⏳" };
    const html = `<div class="log-item">
      <span class="log-icon">${icons[type] || "•"}</span>
      <span class="log-time">${time}</span>
      <div>
        <div class="log-msg">${msg}</div>
        ${detail ? `<div class="log-error">⚠️ ${this.escapeHtml(detail)}</div>` : ""}
      </div>
    </div>`;
    log.insertAdjacentHTML("beforeend", html);
    log.scrollTop = log.scrollHeight;
  },

  async retryLibraryFailed() {
    try {
      const result = await API.retryAllFailed();
      if (result.task_ids.length === 0) { Toast.info("没有失败任务"); return; }
      this.currentTasks = result.task_ids;
      Toast.info(`正在重试 ${result.task_ids.length} 个...`);
      this.pollLibraryTasks(result.task_ids);
    } catch (err) {
      Toast.error(`重试失败: ${err.message}`);
    }
  },

  escapeHtml(str) {
    const div = document.createElement("div");
    div.textContent = str;
    return div.innerHTML;
  },

  openRenameModal() {
    if (this.selected.size === 0) { Toast.error("请选择文件"); return; }
    document.getElementById("rename-modal").style.display = "flex";
    this.previewRename();
  },

  closeRenameModal() {
    document.getElementById("rename-modal").style.display = "none";
  },

  async previewRename() {
    const preview = document.getElementById("rename-preview");
    preview.innerHTML = '<div class="loading"><span class="spinner"></span> 计算中...</div>';

    const rule = document.getElementById("rename-rule").value;
    const files = Array.from(this.selected);

    preview.innerHTML = files.map(path => {
      const name = path.split("/").pop();
      return `<div style="padding:4px 0;font-size:12px;border-bottom:1px solid var(--border-light)">
        <div style="color:var(--text-secondary)">${name}</div>
        <div style="color:var(--accent)">→ ${this.applyRule(rule, name)}</div>
      </div>`;
    }).join("");
  },

  applyRule(rule, filename) {
    const ext = filename.split(".").pop() || "";
    const name = filename.slice(0, -(ext.length + 1));
    const title = name.replace(/[\(（].*[\)）]/g, "").replace(/[-_]\s*[^-_]+$/, "").trim();
    const author = name.includes("-") ? name.split("-").pop()?.trim() : "未知";
    return rule
      .replace("{title}", title || name)
      .replace("{author}", author)
      .replace("{ext}", ext);
  },

  async confirmRename() {
    const rule = document.getElementById("rename-rule").value;
    const files = Array.from(this.selected);
    const items = files.map(path => {
      const name = path.split("/").pop();
      return { path, new_name: this.applyRule(rule, name) };
    });

    try {
      const result = await API.post("/library/batch-rename", { items });
      Toast.success(`已重命名 ${result.results?.length || 0} 个文件`);
      this.closeRenameModal();
      this.selected.clear();
      this.load(this.currentPath);
    } catch (err) {
      Toast.error(`重命名失败: ${err.message}`);
    }
  },

  getFormatIcon(ext) {
    const icons = {
      epub: "📖", mobi: "📱", azw3: "📱", pdf: "📕", txt: "📄", docx: "📝", fb2: "📘",
      cbz: "🗂️", cbr: "🗂️", cb7: "🗂️", cbc: "🗂️", zip: "🗜️",
    };
    return icons[ext?.toLowerCase()] || "📄";
  },

  formatSize(bytes) {
    if (!bytes) return "";
    if (bytes < 1024) return `${bytes}B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)}KB`;
    return `${(bytes / 1024 / 1024).toFixed(1)}MB`;
  },
};
