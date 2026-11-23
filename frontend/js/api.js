/* ==========================================================================
   API 封装。所有请求都从这里走，换后端只改这个文件。
   ========================================================================== */
const API = (() => {
  const base = '';

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
    health: () => get('/api/health'),
    capabilities: () => get('/api/capabilities'),

    upload(files) {
      const fd = new FormData();
      [...files].forEach(f => fd.append('files', f, f.name));
      return req('/api/upload', { method: 'POST', body: fd });
    },

    scanFmt(fmt, path = '') {
      return get('/api/scan?fmt=' + encodeURIComponent(fmt) +
                 (path ? '&path=' + encodeURIComponent(path) : ''));
    },

    library(path = '') {
      return get('/api/library' + (path ? '?path=' + encodeURIComponent(path) : ''));
    },

    createJob(files, target, options) {
      return postJSON('/api/jobs', { files, target, options });
    },
    job(id) { return get('/api/jobs/' + id); },

    history(limit = 200) { return get('/api/history?limit=' + limit); },
    clearHistory() { return del('/api/history'); },

    cache() { return get('/api/cache'); },
    cacheClear() { return del('/api/cache'); },

    settings() { return get('/api/settings'); },
    saveSettings(patch) { return putJSON('/api/settings', patch); },

    downloadUrl(path) { return '/api/download?path=' + encodeURIComponent(path); },
  };
})();
