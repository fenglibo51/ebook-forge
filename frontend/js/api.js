/* ==================== API 封装 ==================== */
const API = {
  async get(path) {
    const res = await fetch(`/api${path}`);
    if (!res.ok) throw new Error(`GET ${path}: ${res.status}`);
    return res.json();
  },

  async post(path, body) {
    const res = await fetch(`/api${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      const err = await res.text();
      throw new Error(`POST ${path}: ${res.status} — ${err}`);
    }
    return res.json();
  },

  async put(path, body) {
    const res = await fetch(`/api${path}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) throw new Error(`PUT ${path}: ${res.status}`);
    return res.json();
  },

  async delete(path) {
    const res = await fetch(`/api${path}`, { method: "DELETE" });
    if (!res.ok) throw new Error(`DELETE ${path}: ${res.status}`);
    return res.json();
  },

  async postForm(path, formData) {
    const res = await fetch(`/api${path}`, {
      method: "POST",
      body: formData,
    });
    if (!res.ok) throw new Error(`POST ${path}: ${res.status}`);
    return res.json();
  },

  clearHistory() {
    return this.delete("/history/clear");
  },

  // 工具库：书库
  browse(subdir, filterExt) {
    const params = new URLSearchParams();
    if (subdir) params.set("subdir", subdir);
    if (filterExt) params.set("filter_ext", filterExt);
    return this.get(`/library/browse?${params}`);
  },

  // 递归列出目录下所有文件（书库「全选」用）
  listFiles(subdir, filterExt) {
    const params = new URLSearchParams();
    if (subdir) params.set("subdir", subdir);
    if (filterExt) params.set("filter_ext", filterExt);
    return this.get(`/library/files?${params}`);
  },

  fileInfo(path) {
    return this.get(`/library/info?path=${encodeURIComponent(path)}`);
  },

  libraryStats() {
    return this.get("/library/stats");
  },

  // 转换
  getStatus() {
    return this.get("/convert/status");
  },

  startConvert(req) {
    return this.post("/convert/start", req);
  },

  batchConvert(req) {
    return this.post("/convert/batch", req);
  },

  getTask(taskId) {
    return this.get(`/convert/task/${taskId}`);
  },

  listTasks(limit = 50) {
    return this.get(`/convert/tasks?limit=${limit}`);
  },

  retryTask(taskId) {
    return this.post(`/convert/retry/${taskId}`, {});
  },

  retryAllFailed() {
    return this.get("/convert/retry-failed");
  },

  supportedTargets(ext) {
    return this.get(`/convert/supported-targets/${ext}`);
  },

  // 完整转换矩阵（源格式 → 可用目标格式），批量转换前做兼容性校验
  getMatrix() {
    return this.get("/convert/matrix");
  },

  // 历史
  getHistory(status, limit = 50) {
    const params = new URLSearchParams();
    if (status) params.set("status", status);
    params.set("limit", limit);
    return this.get(`/history?${params}`);
  },

  clearHistory() {
    return this.delete("/history/clear");
  },
};
