/* ==========================================================================
   API 封装。所有请求都从这里走，换后端只改这个文件。

   分享模式：URL 是 /s/<token> 时自动切到受限接口，
   带 token + 会话 id，只能碰自己上传的文件和自己任务的产物。
   ========================================================================== */
const API = (() => {
  const base = '';

  /* ---------- 分享上下文 ---------- */
  const SHARE = (() => {
    const m = location.pathname.match(/^\/s\/([^/]+)/);
    if (!m) return null;
    const token = m[1];
    const key = 'ebookforge:sid:' + token;
    let sid = localStorage.getItem(key);
    if (!sid) {
      sid = (crypto.randomUUID ? crypto.randomUUID() : Math.random().toString(36).slice(2) + Date.now().toString(36))
        .replace(/-/g, '').slice(0, 20);
      localStorage.setItem(key, sid);
    }
    return { token, sid };
  })();

  const SQ = () => SHARE
    ? `t=${encodeURIComponent(SHARE.token)}&sid=${encodeURIComponent(SHARE.sid)}`
    : '';

  /* ---------- 请求 ---------- */
  async function req(path, opts = {}) {
    const res = await fetch(base + path, opts);
    const ct = res.headers.get('content-type') || '';
    const body = ct.includes('application/json') ? await res.json() : await res.text();
    if (!res.ok) {
      const msg = (body && body.detail) || body || `HTTP ${res.status}`;
      throw new Error(typeof msg === 'string' ? msg : JSON.stringify(msg));
    }
    return body;
  }

  const get = (p) => req(p);
  const postJSON = (p, body) => req(p, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  const putJSON = (p, body) => req(p, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  const del = (p) => req(p, { method: 'DELETE' });

  return {
    /* 分享模式标记，app.js 用 */
    share: SHARE,
    get isShared() { return !!SHARE; },

    health: () => get('/api/health'),
    capabilities: () => get('/api/capabilities'),

    upload(files) {
      const fd = new FormData();
      [...files].forEach(f => fd.append('files', f, f.name));
      const url = SHARE ? `/api/s/upload?${SQ()}` : '/api/upload';
      return req(url, { method: 'POST', body: fd });
    },

    library(path = '') {
      if (SHARE) return Promise.reject(new Error('分享链接不能浏览服务器目录'));
      return get('/api/library' + (path ? '?path=' + encodeURIComponent(path) : ''));
    },

    scanFmt(fmt, path = '') {
      if (SHARE) return Promise.reject(new Error('分享链接不能浏览服务器目录'));
      return get('/api/scan?fmt=' + encodeURIComponent(fmt) +
                 (path ? '&path=' + encodeURIComponent(path) : ''));
    },

    createJob(files, target, options) {
      const url = SHARE ? `/api/s/jobs?${SQ()}` : '/api/jobs';
      return postJSON(url, { files, target, options });
    },

    job(id) {
      const url = SHARE ? `/api/s/jobs/${id}?${SQ()}` : '/api/jobs/' + id;
      return get(url);
    },

    history(limit = 200) {
      if (SHARE) return Promise.reject(new Error('分享链接看不到转换记录'));
      return get('/api/history?limit=' + limit);
    },
    clearHistory() {
      if (SHARE) return Promise.reject(new Error('分享链接没有这个权限'));
      return del('/api/history');
    },

    cache() {
      if (SHARE) return Promise.reject(new Error('分享链接没有这个权限'));
      return get('/api/cache');
    },
    cacheClear() {
      if (SHARE) return Promise.reject(new Error('分享链接没有这个权限'));
      return del('/api/cache');
    },

    settings() {
      if (SHARE) return Promise.reject(new Error('分享链接不能改设置'));
      return get('/api/settings');
    },
    saveSettings(patch) {
      if (SHARE) return Promise.reject(new Error('分享链接不能改设置'));
      return putJSON('/api/settings', patch);
    },

    /* 主人端：管理分享链接 */
    createShare(ttlHours = 24, note = '') {
      return postJSON('/api/shares', { ttl_hours: ttlHours, note });
    },
    listShares() { return get('/api/shares'); },
    revokeShare(token) { return del('/api/shares/' + encodeURIComponent(token)); },

    /* 下载 */
    downloadUrl(path) {
      if (SHARE) return '';        // 分享模式不给路径式下载
      return '/api/download?path=' + encodeURIComponent(path);
    },
    shareDownloadUrl(jobId, index) {
      return `/api/s/download/${jobId}/${index}?${SQ()}`;
    },
  };
})();
