/* EbookForge service worker
 *
 * 只做两件事：
 *   ① 让「添加到主屏」装得像个应用（配合 manifest.json）
 *   ② 静态资源（CSS/JS/图片）走缓存，第二次打开快
 *
 * 不做离线转换 —— 转换要调服务器，没网本来就用不了，装作能用反而误导。
 * /api/* 一律直连网络，绝不缓存：缓存了任务进度和文件列表只会显示过期数据。
 */

const CACHE = 'ebf-v1';

// 只需要壳，页面本身走网络（避免缓存到旧 HTML 导致更新后看不到新功能）
const SHELL = [
  '/manifest.json',
  '/css/variables.css',
  '/css/layout.css',
  '/css/components.css',
  '/css/pages.css',
  '/css/modal.css',
  '/js/api.js',
  '/js/app.js',
  '/img/logo-h.png',
  '/img/logo-h-dark.png',
  '/img/logo-icon.png',
  '/img/logo-icon-dark.png',
  '/img/icon-192.png',
  '/img/icon-512.png',
  '/img/favicon.ico',
];

self.addEventListener('install', (e) => {
  e.waitUntil(
    caches.open(CACHE)
      .then(c => c.addAll(SHELL))
      .catch(() => {})          // 个别资源 404 不该让整个安装失败
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;

  const url = new URL(req.url);
  if (url.origin !== location.origin) return;      // 跨域不碰
  if (url.pathname.startsWith('/api/')) return;    // 接口永远直连

  // 静态资源：缓存优先，后台顺便更新
  const isStatic = /\.(css|js|png|ico|svg|woff2?)$/.test(url.pathname);
  if (isStatic) {
    e.respondWith(
      caches.match(req).then(hit => {
        const fetching = fetch(req).then(resp => {
          if (resp && resp.ok) {
            const copy = resp.clone();
            caches.open(CACHE).then(c => c.put(req, copy)).catch(() => {});
          }
          return resp;
        }).catch(() => hit);
        return hit || fetching;
      })
    );
    return;
  }

  // HTML：网络优先，断网时才退回缓存
  e.respondWith(
    fetch(req).catch(() => caches.match(req).then(hit => hit || caches.match('/')))
  );
});
