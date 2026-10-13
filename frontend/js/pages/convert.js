/* ==================== 格式转换页 ==================== */
const ConvertPage = {
  uploadedFiles: [],
  targetFormat: "epub",
  namingRule: "{title}.{author}.{ext}",
  keepSource: false,
  isRunning: false,
  pollTimer: null,
  currentTasks: [],
  runningLogged: new Set(),

  init() {
    this.bindEvents();
    // 恢复上次未完成的任务
    this.resumePendingTasks();
  },

  async resumePendingTasks() {
    try {
      const data = await API.listTasks(30);
      if (!data || data.length === 0) return;

      const running = data.filter(t => t.status === "running" || t.status === "pending");
      const completed = data.filter(t => t.status === "success" || t.status === "failed");

      // 显示进度区域
      const area = document.getElementById("progress-area");
      area.style.display = "block";
      document.getElementById("progress-log").innerHTML = "";
      document.getElementById("progress-summary").style.display = "none";

      // 先渲染已完成的任务
      let successCount = 0, failedCount = 0;
      completed.reverse().forEach(task => {
        const fn = (task.source_path || "").split("/").pop() || task.task_id;
        if (task.status === "success") {
          this.addLog("success", `${fn} → ${(task.target_format || "").toUpperCase()} 完成`);
          successCount++;
        } else {
          this.addLog("error", `${fn} 转换失败`, task.error);
          failedCount++;
        }
      });

      // 渲染等待中的任务
      running.forEach(task => {
        const fn = (task.source_path || "").split("/").pop() || task.task_id;
        this.addLog("pending", `${fn} 等待中...`);
      });

      const total = data.length;
      const done = completed.length;
      this.updateProgress(done, total);

      // 如果完成数 > 0，显示摘要
      if (completed.length > 0) {
        document.getElementById("summary-success").textContent = `成功: ${successCount}`;
        document.getElementById("summary-failed").textContent = `失败: ${failedCount}`;
        document.getElementById("progress-summary").style.display = "flex";
        document.getElementById("btn-retry-failed").onclick = () => this.retryFailed();
      }

      // 如果有运行中的任务，启动轮询
      if (running.length > 0) {
        this.currentTasks = running.map(t => t.task_id);
        this.runningLogged = new Set();
        this.isRunning = true;

        const btn = document.getElementById("btn-start-convert");
        btn.disabled = true;
        btn.textContent = "⏳ 恢复中...";

        this.pollTasks();
      }
    } catch (e) {
      // 静默失败
    }
  },

  bindEvents() {
    // 目标格式
    document.querySelectorAll(".format-btn").forEach(btn => {
      btn.addEventListener("click", () => {
        document.querySelectorAll(".format-btn").forEach(b => b.classList.remove("active"));
        btn.classList.add("active");
        this.targetFormat = btn.dataset.format;
        // 选 CBZ 时才显示清晰度（DPI）选项
        const dpiGroup = document.getElementById("dpi-group");
        if (dpiGroup) dpiGroup.style.display = this.targetFormat === "cbz" ? "block" : "none";
        this.updateQueue();
      });
    });

    // 上传区域
    const uploadZone = document.getElementById("upload-zone");
    const fileInput = document.getElementById("file-input");

    uploadZone.addEventListener("click", () => fileInput.click());

    uploadZone.addEventListener("dragover", (e) => {
      e.preventDefault();
      uploadZone.classList.add("dragover");
    });
    uploadZone.addEventListener("dragleave", () => uploadZone.classList.remove("dragover"));
    uploadZone.addEventListener("drop", (e) => {
      e.preventDefault();
      uploadZone.classList.remove("dragover");
      this.handleFiles(e.dataTransfer.files);
    });

    fileInput.addEventListener("change", () => {
      if (fileInput.files.length) {
        this.handleFiles(fileInput.files);
        fileInput.value = "";
      }
    });

    // 转换按钮
    document.getElementById("btn-start-convert").addEventListener("click", () => this.startConvert());

    // 从书库选择目录按钮
    document.getElementById("btn-pick-target").addEventListener("click", () => this.openDirPicker());

    // 清除本地日志按钮
    document.getElementById("btn-clear-local-log").addEventListener("click", () => {
      const area = document.getElementById("progress-area");
      area.style.display = "none";
      document.getElementById("progress-log").innerHTML = "";
      document.getElementById("progress-summary").style.display = "none";
      document.getElementById("progress-fill").style.width = "0%";
      this.currentTasks = [];
      this.isRunning = false;
      if (this.pollTimer) {
        clearTimeout(this.pollTimer);
        this.pollTimer = null;
      }
      const btn = document.getElementById("btn-start-convert");
      btn.disabled = false;
      btn.textContent = "🚀 开始转换";
    });
  },

  openDirPicker() {
    // 弹窗选择目录
    const modal = document.getElementById("dir-picker-modal");
    modal.style.display = "flex";
    this.loadDirs("", (path) => {
      document.getElementById("upload-target").value = path;
      modal.style.display = "none";
    });
  },

  async loadDirs(subdir, onSelect) {
    const list = document.getElementById("dir-picker-list");
    list.innerHTML = '<div class="loading">加载中...</div>';
    try {
      const data = await API.browse(subdir);
      const bc = document.getElementById("dir-picker-breadcrumb");
      let crumbs = [{ name: "Book", path: "" }];
      if (subdir) {
        const parts = subdir.split("/");
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
          el.addEventListener("click", () => this.loadDirs(el.dataset.path, onSelect));
        }
      });

      const dirs = data.items.filter(it => it.is_dir);
      list.innerHTML = dirs.map(d =>
        `<div class="picker-item" data-path="${d.path}">
          <span>📁</span>
          <span class="item-name">${d.name}</span>
          <span class="item-size">目录</span>
        </div>`
      ).join("");
      if (dirs.length === 0) {
        list.innerHTML = '<div class="loading">此目录下没有子目录</div>';
      }

      // 选中的目录填入输入框
      list.querySelectorAll(".picker-item").forEach(el => {
        el.addEventListener("click", () => {
          const path = el.dataset.path;
          const btn = document.getElementById("dir-picker-select");
          btn.dataset.path = path;
          btn.style.display = "inline-flex";
          document.getElementById("dir-picker-current").textContent = `已选: /${path}/`;
        });
      });

      // 双击进入
      list.querySelectorAll(".picker-item").forEach(el => {
        el.addEventListener("dblclick", () => this.loadDirs(el.dataset.path, onSelect));
      });

    } catch (err) {
      list.innerHTML = `<div class="loading" style="color:var(--error)">加载失败: ${err.message}</div>`;
    }
  },

  handleFiles(files) {
    for (const f of files) {
      this.uploadedFiles.push({ name: f.name, size: f.size, file: f });
    }
    this.renderUploadList();
    this.updateQueue();
  },

  renderUploadList() {
    const list = document.getElementById("upload-list");
    if (this.uploadedFiles.length === 0) {
      list.innerHTML = "";
      return;
    }
    list.innerHTML = this.uploadedFiles.map((f, i) =>
      `<div class="upload-item">
        <span>${this.getFormatIcon(f.name.split('.').pop())}</span>
        <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${f.name}</span>
        <span style="color:var(--text-muted);font-size:11px;flex-shrink:0">${this.formatSize(f.size)}</span>
        <button class="btn btn-sm btn-ghost" onclick="ConvertPage.removeFile(${i})" style="color:var(--error);flex-shrink:0">✕</button>
      </div>`
    ).join("");
  },

  removeFile(idx) {
    this.uploadedFiles.splice(idx, 1);
    this.renderUploadList();
    this.updateQueue();
  },

  updateQueue() {
    const total = this.uploadedFiles.length;
    document.getElementById("queue-count").textContent = total;
    const list = document.getElementById("queue-list");
    const btn = document.getElementById("btn-start-convert");

    if (total === 0) {
      btn.disabled = true;
      btn.textContent = "🚀 开始转换";
      list.innerHTML = '<div style="color:var(--text-muted);font-size:12px;padding:8px">还没有选择文件</div>';
      return;
    }

    btn.disabled = false;
    list.innerHTML = this.uploadedFiles.slice(0, 10).map(f =>
      `<div class="queue-item">
        <span>📄</span>
        <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${f.name}</span>
        <span style="font-size:11px;color:var(--text-muted);flex-shrink:0">→ ${this.targetFormat.toUpperCase()}</span>
      </div>`
    ).join("");
    if (total > 10) {
      list.innerHTML += `<div style="font-size:11px;color:var(--text-muted);padding:4px 8px">...还有 ${total - 10} 个文件</div>`;
    }
  },

  async startConvert() {
    if (this.isRunning || this.uploadedFiles.length === 0) return;
    if (!this.targetFormat) { Toast.error("请选择目标格式"); return; }

    this.isRunning = true;
    const btn = document.getElementById("btn-start-convert");
    btn.disabled = true;
    btn.textContent = "⏳ 上传中...";

    // 显示进度区（不清理已有内容，新任务叠加）
    const area = document.getElementById("progress-area");
    area.style.display = "block";
    document.getElementById("progress-summary").style.display = "none";

    // 获取目标目录
    const target = document.getElementById("upload-target").value.trim() || "temp";
    this.uploadTarget = target;

    // 逐个上传文件
    const filePaths = [];
    for (const item of this.uploadedFiles) {
      const formData = new FormData();
      formData.append("file", item.file);
      formData.append("target_dir", target);
      try {
        btn.textContent = `⏳ 上传 ${item.name}...`;
        const uploadResult = await API.postForm("/convert/upload", formData);
        filePaths.push(uploadResult.path);
        this.addLog("running", `${item.name} 上传成功`);
      } catch (err) {
        this.addLog("error", `${item.name} 上传失败: ${err.message}`);
        filePaths.push(null);
      }
    }

    btn.textContent = "⏳ 转换中...";

    // 构建转换请求
    const isCbz = this.targetFormat === "cbz";
    const dpiEl = document.getElementById("cbz-dpi");
    const fmtEl = document.getElementById("cbz-format");
    const cbzDpi = (isCbz && dpiEl) ? parseInt(dpiEl.value, 10) : 0;
    const cbzFmt = (isCbz && fmtEl) ? fmtEl.value : "";
    const convertFiles = [];
    for (let i = 0; i < this.uploadedFiles.length; i++) {
      if (filePaths[i] === null) continue;
      const item = {
        source_path: filePaths[i],
        target_format: this.targetFormat,
        keep_source: true,
        use_original_name: true,
        target_dir: target,
      };
      if (cbzDpi) item.dpi = cbzDpi;
      if (cbzFmt) item.img_format = cbzFmt;
      convertFiles.push(item);
    }

    if (convertFiles.length === 0) {
      this.isRunning = false;
      btn.disabled = false;
      btn.textContent = "🚀 开始转换";
      return;
    }

    try {
      const batchResult = await API.batchConvert({ files: convertFiles });
      const newIds = batchResult.task_ids || [];

      // 追加到已有任务列表，不替换
      const existingIds = this.currentTasks || [];
      this.currentTasks = [...existingIds, ...newIds];

      // 为新任务添加等待日志
      newIds.forEach((id, i) => {
        if (i < convertFiles.length) {
          const fn = convertFiles[i]?.source_path?.split("/").pop() || "文件";
          this.addLog("pending", `${fn} 等待中...`);
        }
      });

      this.updateProgress(0, this.currentTasks.length);
      if (!this.isRunning) {
        this.isRunning = true;
        this.runningLogged = new Set();
        this.pollTasks();
      }
    } catch (err) {
      this.finishWithError(`启动转换失败: ${err.message}`);
    }
  },

  pollTasks() {
    if (this.currentTasks.length === 0) return;

    let success = 0;
    let failed = 0;
    let completed = new Set();
    const startTime = Date.now();
    const total = this.currentTasks.length;

    const check = async () => {
      for (const tid of this.currentTasks) {
        if (completed.has(tid)) continue;
        try {
          const task = await API.getTask(tid);
          if (task.status === "success" || task.status === "failed") {
            completed.add(tid);
            const fn = task.source_path?.split("/").pop() || "文件";
            if (task.status === "success") {
              this.addLog("success", `${fn} → ${this.targetFormat.toUpperCase()} 完成`);
              success++;
            } else {
              this.addLog("error", `${fn} 转换失败`, task.error);
              failed++;
            }
          } else if (task.status === "running" && !this.runningLogged.has(tid)) {
            this.runningLogged.add(tid);
            const fn = task.source_path?.split("/").pop() || "文件";
            this.addLog("running", `${fn} 转换中...`);
          }
        } catch (e) {}
      }

      this.updateProgress(completed.size, total);

      if (completed.size >= total) {
        const elapsed = Math.round((Date.now() - startTime) / 1000);
        document.getElementById("progress-stats").textContent = `${completed.size}/${total} 完成`;
        document.getElementById("progress-fill").style.width = "100%";

        const target = this.uploadTarget || "temp";
        let downloadHtml = "";

        // 如果是 temp 模式，展示下载链接
        if (target === "temp") {
          const dlLinks = [];
          for (const tid of this.currentTasks) {
            try {
              const task = await API.getTask(tid);
              if (task.status === "success" && task.output_path) {
                const fname = task.output_path.split("/").pop();
                dlLinks.push(fname);
              }
            } catch (e) {}
          }
          if (dlLinks.length > 0) {
            const items = dlLinks.map(f => `<button class="btn btn-sm" style="margin:2px" onclick="ConvertPage.downloadTempFile('${encodeURIComponent(f)}')">📥 ${f}</button>`).join("");
            downloadHtml = `<div style="margin-top:8px;font-size:12px"><strong>下载文件:</strong> ${items}</div>`;
          }
        }

        document.getElementById("summary-success").textContent = `成功: ${success}`;
        document.getElementById("summary-failed").textContent = `失败: ${failed}`;
        document.getElementById("summary-time").textContent = `用时: ${elapsed}s`;
        document.getElementById("progress-summary").style.display = "flex";
        document.getElementById("btn-retry-failed").onclick = () => this.retryFailed();

        // 如果有下载链接，追加到进度摘要
        if (downloadHtml) {
          const summary = document.getElementById("progress-summary");
          const wrapper = document.createElement("div");
          wrapper.style.width = "100%";
          wrapper.innerHTML = downloadHtml;
          summary.appendChild(wrapper);
        }

        this.isRunning = false;
        const btn = document.getElementById("btn-start-convert");
        btn.disabled = false;
        btn.textContent = "🚀 开始转换";

        // 转换完清空上传列表
        this.uploadedFiles = [];
        this.renderUploadList();
        this.updateQueue();

        // 复用上面的 target，不重复声明 const
        const dirLabel = target === "temp" ? "临时目录" : `/${target}/`;
        const msg = failed > 0
          ? `转换完成: ${success} 成功, ${failed} 失败`
          : `✅ 已完成，文件已保存到 ${dirLabel}`;
        if (failed > 0) Toast.error(msg);
        else Toast.success(msg);

        return;
      }

      this.pollTimer = setTimeout(check, 2000);
    };

    check();
  },

  updateProgress(done, total) {
    const pct = total > 0 ? Math.round((done / total) * 100) : 0;
    document.getElementById("progress-stats").textContent = `${done}/${total}`;
    document.getElementById("progress-fill").style.width = `${pct}%`;
  },

  addLog(type, msg, detail) {
    const log = document.getElementById("progress-log");
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

  finishWithError(msg) {
    this.isRunning = false;
    const btn = document.getElementById("btn-start-convert");
    btn.disabled = false;
    btn.textContent = "🚀 开始转换";
    this.addLog("error", msg);
    Toast.error(msg);
  },

  async retryFailed() {
    try {
      const result = await API.retryAllFailed();
      if (result.task_ids.length === 0) {
        Toast.info("没有失败任务可重试");
        return;
      }
      this.currentTasks = result.task_ids;
      Toast.info(`正在重试 ${result.task_ids.length} 个任务...`);
      this.pollTasks();
    } catch (err) {
      Toast.error(`重试失败: ${err.message}`);
    }
  },

  async downloadTempFile(fname) {
    try {
      const resp = await fetch("/api/convert/download/" + fname, { method: "HEAD" });
      if (!resp.ok) {
        Toast.error("文件已过期或已被清理");
        return;
      }
      window.open("/api/convert/download/" + fname, "_blank");
    } catch (e) {
      Toast.error("下载失败: " + e.message);
    }
  },

  getFormatIcon(ext) {
    const icons = {
      epub: "📖", mobi: "📱", azw3: "📱", pdf: "📕", txt: "📄", docx: "📝", fb2: "📘",
      cbz: "🗂️", cbr: "🗂️", zip: "🗜️",
    };
    return icons[ext?.toLowerCase()] || "📄";
  },

  formatSize(bytes) {
    if (!bytes) return "";
    if (bytes < 1024) return `${bytes}B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)}KB`;
    return `${(bytes / 1024 / 1024).toFixed(1)}MB`;
  },

  escapeHtml(str) {
    const div = document.createElement("div");
    div.textContent = str;
    return div.innerHTML;
  },
};
