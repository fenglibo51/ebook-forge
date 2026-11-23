/* ==========================================================================
   EbookForge · 前端交互（接后端）
   纯原生，无框架。所有数据走 API，不写死在 HTML 里。
   ========================================================================== */
(function () {
  'use strict';

  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
  const fmtSize = (n) => {
    if (!n) return '—';
    if (n > 1073741824) return (n / 1073741824).toFixed(2) + ' GB';
    if (n > 1048576) return (n / 1048576).toFixed(1) + ' MB';
    if (n > 1024) return (n / 1024).toFixed(0) + ' KB';
    return n + ' B';
  };
  const fmtTime = (ts) => {
    const d = new Date(ts * 1000);
    const p = (x) => String(x).padStart(2, '0');
    return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
  };
  const toast = (msg, kind) => {
    let host = $('#toastHost');
    if (!host) {
      host = document.createElement('div');
      host.id = 'toastHost';
      host.style.cssText = 'position:fixed;right:24px;bottom:24px;display:flex;flex-direction:column;gap:8px;z-index:99';
      document.body.appendChild(host);
    }
    const el = document.createElement('div');
    const color = kind === 'fail' ? 'var(--fail)' : kind === 'warn' ? 'var(--warn)' : 'var(--ink)';
    el.style.cssText = `background:var(--toast-bg);border:1px solid var(--line);border-left:3px solid ${color};
      border-radius:10px;padding:11px 16px;font-size:13.5px;box-shadow:var(--shadow);
      max-width:420px;line-height:1.5;color:var(--ink)`;
    el.textContent = msg;
    host.appendChild(el);
    setTimeout(() => el.remove(), kind === 'fail' ? 7000 : 3800);
  };

  /* ================= 主题（深色 / 浅色） ================= */
  const THEME_KEY = 'ebookforge:theme';
  const ICON_MOON = 'M21 12.8A9 9 0 1111.2 3a7 7 0 009.8 9.8z';
  const ICON_SUN = 'M12 4V2M12 22v-2M4 12H2M22 12h-2M5.6 5.6L4.2 4.2M19.8 19.8l-1.4-1.4M18.4 5.6l1.4-1.4M6.6 17.4l-1.4 1.4';

  function applyTheme(dark) {
    document.documentElement.dataset.theme = dark ? 'dark' : '';
    const icon = $('#themeIcon'), label = $('#themeLabel'), btn = $('#themeToggle');
    if (!icon || !label) return;
    // 按钮上写的是「点一下会变成什么」，不是当前状态
    icon.innerHTML = dark
      ? `<circle cx="12" cy="12" r="4"/><path d="${ICON_SUN}"/>`
      : `<path d="${ICON_MOON}"/>`;
    label.textContent = dark ? '浅色模式' : '深色模式';
    btn.title = dark ? '切到浅色' : '切到深色';
  }

  (function initTheme() {
    const saved = localStorage.getItem(THEME_KEY);
    if (saved === 'dark' || saved === 'light') {
      applyTheme(saved === 'dark');
    } else {
      // 没手动选过就跟随系统
      applyTheme(window.matchMedia('(prefers-color-scheme: dark)').matches);
    }
    const btn = $('#themeToggle');
    if (btn) btn.addEventListener('click', () => {
      const dark = document.documentElement.dataset.theme !== 'dark';
      applyTheme(dark);
      localStorage.setItem(THEME_KEY, dark ? 'dark' : 'light');
    });
    // 用户没手动选过时，系统主题变了也跟着变
    window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', e => {
      if (!localStorage.getItem(THEME_KEY)) applyTheme(e.matches);
    });
  })();

  /* ================= 侧边栏 ================= */
  const sb = $('#sidebar'), tg = $('#toggle'), SB_KEY = 'ebookforge:sidebar-collapsed';
  const applyCollapsed = (v) => {
    sb.classList.toggle('collapsed', v);
    tg.title = v ? '展开侧边栏' : '收起侧边栏';
  };
  applyCollapsed(localStorage.getItem(SB_KEY) === '1');
  tg.addEventListener('click', () => {
    const next = !sb.classList.contains('collapsed');
    applyCollapsed(next);
    localStorage.setItem(SB_KEY, next ? '1' : '0');
  });

  /* ================= 页面切换 ================= */
  const navItems = $$('.nav-item'), pages = $$('.page');
  let currentPage = '';
  function navigate(id) {
    if (!document.getElementById('page-' + id)) id = 'convert';
    if (id === currentPage) return;
    currentPage = id;
    navItems.forEach(n => n.classList.toggle('active', n.dataset.page === id));
    pages.forEach(p => p.classList.toggle('active', p.id === 'page-' + id));
    if (location.hash.slice(1) !== id) history.replaceState(null, '', '#' + id);
    window.scrollTo(0, 0);
    if (id === 'library') loadLibrary(libPath);
    if (id === 'history') loadHistory();
    if (id === 'settings') loadSettings();
  }
  navItems.forEach(n => n.addEventListener('click', (e) => { e.preventDefault(); navigate(n.dataset.page); }));
  window.addEventListener('hashchange', () => navigate(location.hash.slice(1) || 'convert'));

  /* ================= 引擎状态 ================= */
  (async () => {
    try {
      const h = await API.health();
      $('#engineDot').className = 'dot ' + (h.font_ok ? 'ok' : 'warn');
      $('#engineLabel').textContent = h.font_ok ? '引擎就绪' : '字体缺失';
      $('#engineStatus').title = h.font_ok
        ? `v${h.version} · 渲染 ${h.render_dpi}dpi · 兜底${h.render_fallback ? '开' : '关'}`
        : h.font;
    } catch (e) {
      $('#engineDot').className = 'dot fail';
      $('#engineLabel').textContent = '连不上后端';
      $('#engineStatus').title = String(e.message || e);
    }
  })();

  /* ================= 转换页 ================= */
  const NOTES = {
    epub: 'EPUB —— 文字书转 EPUB 是容器重排，正文段落无损；漫画转 EPUB 是图片打包。',
    mobi: 'MOBI —— 输出用不压缩模式，体积大一点但结构最稳。',
    pdf: 'PDF —— 文字书会走排版引擎（A5 单栏、首行缩进 2 字符、中文禁则）；漫画按图片直嵌。',
    txt: 'TXT —— 只留文字，排版、图片、章节层级都会丢。',
    docx: 'DOCX —— 暂不支持嵌图，图片会跳过并在结果里报数量。',
    fb2: 'FB2 —— XML 容器，正文和插图都能留住。',
    cbz: 'CBZ —— 漫画容器，图片原字节直嵌。文字书不能转 CBZ。',
    zip: 'ZIP —— 和 CBZ 是同一个容器，只是扩展名不同；漫画打包用。'
  };
  // 格式矩阵（和引擎保持一致）
  const TEXT_SOURCES = new Set(['txt', 'fb2', 'docx']);          // 确定是文字书
  const COMIC_SOURCES = new Set(['cbz', 'cb7', 'cbc', 'cbr', 'zip']);  // 确定是漫画包
  const TEXT_TARGETS = new Set(['txt', 'fb2', 'docx', 'epub', 'mobi', 'pdf']);
  const COMIC_TARGETS = new Set(['cbz', 'zip', 'epub', 'pdf']);
  // epub / mobi / pdf 两栖（可能是漫画也可能是小说），由后端判断，前端不预设

  let queue = [];            // [{path,name,size}]
  let target = 'pdf';
  let jobId = null;

  const fmtBtns = $$('#fmts .fmt');
  const fmtNote = $('#fmtNote');

  function updateFmtState() {
    const exts = [...new Set(queue.map(f => (f.name.split('.').pop() || '').toLowerCase()))];
    const allText = exts.length > 0 && exts.every(e => TEXT_SOURCES.has(e));
    const allComic = exts.length > 0 && exts.every(e => COMIC_SOURCES.has(e));

    fmtBtns.forEach(b => {
      const t = b.dataset.f;
      let off = false, why = '';
      if (allText && !TEXT_TARGETS.has(t)) {
        off = true; why = '队列里都是文字书（' + exts.join('/') + '），不能转成漫画容器';
      } else if (allComic && !COMIC_TARGETS.has(t)) {
        off = true; why = '队列里都是漫画包（' + exts.join('/') + '），不能转成文字格式';
      }
      b.disabled = off;
      b.title = why;
    });

    // 当前选中的目标如果被禁用，自动挪到一个能用的
    const cur = fmtBtns.find(x => x.dataset.f === target);
    if (cur && cur.disabled) {
      const fallback = [...fmtBtns].find(x => !x.disabled);
      if (fallback) {
        target = fallback.dataset.f;
        fmtBtns.forEach(x => x.classList.toggle('on', x === fallback));
      }
    }
    fmtNote.textContent = NOTES[target] || '';
    updateGoState();
  }

  function renderQueue() {
    const host = $('#queue');
    host.innerHTML = queue.length ? queue.map((f, i) => `
      <div class="q-row">
        <span class="qn">${esc(f.name)}</span>
        <span class="arrow">→</span>
        <span class="tag">${esc(target.toUpperCase())}</span>
        <button class="rm" data-i="${i}" title="移除" style="color:var(--ink-3);width:22px;height:22px;border-radius:6px;flex:0 0 22px">✕</button>
      </div>`).join('') : '<div class="empty" style="padding:18px 0;font-size:13px">还没有文件</div>';
    $$('#queue .rm').forEach(b => b.addEventListener('click', () => {
      queue.splice(+b.dataset.i, 1);
      renderFiles(); renderQueue(); updateFmtState();
    }));
    $('#queueCount').textContent = queue.length + ' 个';
    $('#queueSize').textContent = queue.length
      ? '共 ' + fmtSize(queue.reduce((a, f) => a + (f.size || 0), 0)) : '';
  }

  function renderFiles() {
    const host = $('#file-list');
    host.innerHTML = queue.map((f, i) => {
      const ext = (f.name.split('.').pop() || '').toUpperCase();
      const badge = f.source === 'server'
        ? '<span class="src-badge sv">服务器</span>'
        : '<span class="src-badge up">上传</span>';
      return `<div class="file-row">
        <div class="fx">${esc(ext)}</div>
        ${badge}
        <div class="fname">${esc(f.name)}</div>
        <div class="fmeta">${fmtSize(f.size)}</div>
        <button class="rm" data-i="${i}" title="移除">✕</button>
      </div>`;
    }).join('');
    $$('#file-list .rm').forEach(b => b.addEventListener('click', () => {
      queue.splice(+b.dataset.i, 1);
      renderFiles(); renderQueue(); updateFmtState();
    }));
  }

  function updateGoState() {
    $('#go').disabled = !queue.length || !target || !!jobId;
    const hasServer = queue.some(f => f.source === 'server');
    const opts = $('#jobOpts');
    if (opts) opts.style.display = hasServer ? '' : 'none';
    const on = $('#outMode button.on');
    const custom = on && on.dataset.val === 'custom';
    const hint = $('#outModeHint');
    if (hint) hint.textContent = custom
      ? '输出到设置页里填的输出目录；没填则退回源文件同目录。'
      : '转换结果和源文件放在一起，重名自动加 _1、_2。';
  }

  async function addFiles(fileList) {
    const files = [...fileList];
    if (!files.length) return;
    toast(`正在上传 ${files.length} 个文件…`);
    try {
      const r = await API.upload(files);
      queue.push(...r.files.map(f => ({ ...f, source: 'upload' })));
      renderFiles(); renderQueue(); updateFmtState();
      toast(`已加入 ${r.files.length} 个文件`);
    } catch (e) {
      toast('上传失败：' + (e.message || e), 'fail');
    }
  }

  fmtBtns.forEach(b => b.addEventListener('click', () => {
    if (b.disabled) return;
    target = b.dataset.f;
    fmtBtns.forEach(x => x.classList.toggle('on', x === b));
    updateFmtState(); renderQueue();
  }));

  const drop = $('#drop');
  ['dragenter', 'dragover'].forEach(ev => drop.addEventListener(ev, e => {
    e.preventDefault(); drop.classList.add('over');
  }));
  ['dragleave', 'drop'].forEach(ev => drop.addEventListener(ev, e => {
    e.preventDefault(); drop.classList.remove('over');
  }));
  drop.addEventListener('drop', e => addFiles(e.dataTransfer.files));
  $('#pick').addEventListener('click', () => $('#file-input').click());
  $('#file-input').addEventListener('change', e => {
    addFiles(e.target.files);
    e.target.value = '';
  });

  /* -------- 任务轮询（转换页 + 书库页共用） -------- */
  async function pollJob(id, onUpdate) {
    for (;;) {
      await new Promise(r => setTimeout(r, 700));
      let st;
      try {
        st = await API.job(id);
      } catch (e) {
        onUpdate(null, e);
        return null;
      }
      onUpdate(st);
      if (st.status === 'done') return st;
    }
  }

  const ST_ICON = { ok: ['ok', '✓'], lossy: ['warn', '!'], fail: ['fail', '✕'], running: ['run', '⋯'], pending: ['run', '·'] };

  function logRow(name, status, meta, metaWarn, out) {
    const [cls, ico] = ST_ICON[status] || ST_ICON.pending;
    const btn = out
      ? `<button class="dlbtn" data-dl="${esc(out)}" title="下载转换结果">下载</button>`
      : '';
    return `<div class="log-row">
      <span class="st ${cls}">${ico}</span>
      <div class="log-main">
        <div class="log-name">${esc(name)}</div>
        <div class="log-meta${metaWarn ? ' warn' : ''}">${esc(meta)}</div>
      </div>
      ${btn}
    </div>`;
  }

  function bindDownloads(scope) {
    $$('.dlbtn[data-dl]', scope || document).forEach(b => {
      if (b.dataset.bound) return;
      b.dataset.bound = '1';
      b.addEventListener('click', () => {
        const v = b.dataset.dl;
        // 分享模式下这个值已经是完整的 /api/s/download/... 签名链接
        window.location.href = v.startsWith('/api/s/') ? v : API.downloadUrl(v);
      });
    });
  }

  function itemMeta(it) {
    if (it.status === 'fail') return it.error || '失败';
    if (it.status === 'running') return '转换中…';
    if (it.status === 'pending') return '排队中';
    const bits = [];
    if (it.pages) bits.push(it.pages + ' 页');
    if (it.out_size) bits.push(fmtSize(it.out_size));
    if (it.elapsed) bits.push(it.elapsed.toFixed(2) + ' 秒');
    if (it.status === 'lossy' && it.note) bits.push(it.note);
    return bits.join(' · ');
  }

  const optFor = (extra = {}) => {
    const s = settingsCache || {};
    return {
      page_size: s.page_size || 'a5',
      render_dpi: s.render_dpi || 200,
      render_fallback: s.render_fallback !== false,
      font_path: s.font_path || '',
      output_dir: s.output_dir || '',
      ...extra
    };
  };

  $('#go').addEventListener('click', async () => {
    if (!queue.length || jobId) return;
    try {
      const outMode = $('#outMode button.on')?.dataset.val || 'same';
      const opts = optFor({
        delete_source: $('#delSourceJob').checked,
        output_dir: outMode === 'custom' ? ((settingsCache && settingsCache.output_dir) || '') : ''
      });
      const r = await API.createJob(queue, target, opts);
      jobId = r.job_id;
      updateGoState();
      $('#progressCard').style.display = '';
      $('#progLog').innerHTML = '';
      $('#progSum').innerHTML = '';
      $('#progFill').style.width = '0%';
      const st = await pollJob(jobId, (s) => {
        if (!s) return;
        const pct = s.total ? (s.done / s.total * 100) : 0;
        $('#progFill').style.width = pct + '%';
        $('#progStats').textContent = `${s.done} / ${s.total}` + (s.elapsed ? ` · 已用 ${s.elapsed} 秒` : '');
        $('#progLog').innerHTML = s.items.map((it, idx) => {
          const done = ['ok', 'lossy'].includes(it.status);
          const dl = done
            ? (API.isShared ? API.shareDownloadUrl(s.id, idx) : it.out)
            : '';
          return logRow(`${it.name} → ${s.target.toUpperCase()}`, it.status,
                        itemMeta(it), it.status === 'lossy', dl);
        }).join('');
        bindDownloads($('#progLog'));
      });
      if (st) finishJob(st, 'convert');
    } catch (e) {
      toast('建任务失败：' + (e.message || e), 'fail');
    } finally {
      jobId = null;
      updateGoState();
    }
  });

  function finishJob(st, where) {
    const ok = st.items.filter(i => i.status === 'ok').length;
    const lossy = st.items.filter(i => i.status === 'lossy').length;
    const fail = st.items.filter(i => i.status === 'fail').length;

    if (where === 'convert') {
      $('#progSum').innerHTML = `
        <span class="ok">成功 <b>${ok}</b></span>
        <span class="warn">有损 <b>${lossy}</b></span>
        <span class="fail">失败 <b>${fail}</b></span>
        <span class="grow"></span>
        <button class="btn-ghost" id="clearDone">清空已完成</button>`;
      $('#clearDone').addEventListener('click', () => {
        const doneNames = new Set(st.items.map(i => i.name));
        queue = queue.filter(f => !doneNames.has(f.name));
        renderFiles(); renderQueue(); updateFmtState();
        $('#progressCard').style.display = 'none';
      });
      // 有损的文件单独提出来提醒（这个必须在界面上看得见）
      const lossyItems = st.items.filter(i => i.status === 'lossy');
      $('#warnSlot').innerHTML = lossyItems.length ? `
        <div class="warnbar" style="margin-top:14px">
          <svg viewBox="0 0 24 24"><path d="M12 9v4M12 17h.01"/><path d="M10.3 3.9L2.4 18a2 2 0 001.7 3h15.8a2 2 0 001.7-3L13.7 3.9a2 2 0 00-3.4 0z"/></svg>
          <div><b>${lossyItems.length} 个文件走了渲染模式（有损）。</b>
          ${lossyItems.map(i => `${esc(i.name)}：${esc(i.note || '没有无损通路')}`).join('<br>')}</div>
        </div>` : '';
    }
    toast(`完成：成功 ${ok} / 有损 ${lossy} / 失败 ${fail}`,
      fail ? 'fail' : lossy ? 'warn' : 'ok');
  }


  /* -------- 服务器目录浏览器（弹窗） -------- */
  let browsePath = '';
  let browseItems = [];
  const browseSel = new Map();          // rel路径 → {name, size}

  async function loadBrowse(path = '') {
    const host = $('#browseList');
    host.innerHTML = '<div class="empty">加载中…</div>';
    try {
      const r = await API.library(path);
      browsePath = r.path;
      browseItems = r.items;
      $('#browseCrumbs').innerHTML = r.crumbs.map((c, i) => {
        const last = i === r.crumbs.length - 1;
        const el = last ? `<span class="cur">${esc(c.name)}</span>`
                        : `<a href="#" data-p="${esc(c.path)}">${esc(c.name)}</a>`;
        return el + (last ? '' : '<span class="sep">/</span>');
      }).join('');
      $$('#browseCrumbs a').forEach(a => a.addEventListener('click', e => {
        e.preventDefault(); loadBrowse(a.dataset.p);
      }));
      const present = r.items.filter(i => !i.is_dir && i.fmt).map(i => i.fmt);
      const all = [...new Set([...allFmts, ...present])].sort();
      $('#browseFilter').innerHTML = '<option value="">全部格式</option>' +
        all.map(f => `<option value="${esc(f)}">${esc(f.toUpperCase())}</option>`).join('');
      renderBrowse();
    } catch (e) {
      host.innerHTML = `<div class="empty">${esc(e.message || e)}</div>`;
    }
  }

  function renderBrowse() {
    const f = $('#browseFilter').value;
    const rows = browseItems.filter(it => !f || it.fmt === f);
    if (!rows.length) {
      $('#browseList').innerHTML = '<div class="empty">这个目录里没有可用的文件</div>';
      return;
    }
    $('#browseList').innerHTML = rows.map(it => {
      const rel = (browsePath ? browsePath + '/' : '') + it.name;
      return `<div class="frow">
        ${it.is_dir ? '<span style="width:16px;flex:0 0 16px"></span>'
                    : `<input type="checkbox" class="bcbx" data-rel="${esc(rel)}" data-name="${esc(it.name)}" data-size="${it.size}" ${browseSel.has(rel) ? 'checked' : ''}>`}
        <div class="fx">${it.is_dir ? 'DIR' : esc((it.fmt || '?').toUpperCase())}</div>
        <div class="nm${it.is_dir ? ' dir' : ''}" data-rel="${esc(rel)}">${esc(it.name)}</div>
        <div class="sz">${it.is_dir ? '—' : fmtSize(it.size)}</div>
        <div class="tm">${fmtTime(it.mtime)}</div>
      </div>`;
    }).join('');

    $$('#browseList .nm.dir').forEach(n => n.addEventListener('click', () => loadBrowse(n.dataset.rel)));
    $$('#browseList .bcbx').forEach(b => b.addEventListener('change', () => {
      if (b.checked) browseSel.set(b.dataset.rel, { name: b.dataset.name, size: +b.dataset.size });
      else browseSel.delete(b.dataset.rel);
      $('#browseCount').textContent = browseSel.size;
      $('#browseAdd').disabled = browseSel.size === 0;
    }));
  }

  function openBrowser() {
    browseSel.clear();
    $('#browseCount').textContent = '0';
    $('#browseAdd').disabled = true;
    $('#browseModal').classList.add('show');
    loadBrowse('');
  }
  function closeBrowser() { $('#browseModal').classList.remove('show'); }

  if ($('#pickServer')) {
    $('#pickServer').addEventListener('click', openBrowser);
    $('#browseClose').addEventListener('click', closeBrowser);
    $('#browseCancel').addEventListener('click', closeBrowser);
    $('#browseBackdrop').addEventListener('click', closeBrowser);
    $('#browseFilter').addEventListener('change', renderBrowse);
    document.addEventListener('keydown', e => {
      if (e.key === 'Escape' && $('#browseModal').classList.contains('show')) closeBrowser();
    });

    $('#browseAdd').addEventListener('click', () => {
      const root = ((settingsCache && settingsCache.library_root) || '').replace(/\/+$/, '');
      let added = 0;
      for (const [rel, info] of browseSel) {
        const abs = root ? `${root}/${rel}` : rel;
        if (queue.some(f => f.path === abs)) continue;
        queue.push({ path: abs, name: info.name, size: info.size, source: 'server' });
        added++;
      }
      closeBrowser();
      renderFiles(); renderQueue(); updateFmtState();
      if (added) toast(`已加入 ${added} 个服务器文件`);
      else toast('这些文件已经在队列里了', 'warn');
    });
  }

  // 输出位置单选（独立类名，别和设置页的 .seg 全局逻辑搅在一起）
  if ($('#outMode')) {
    $$('#outMode button').forEach(b => b.addEventListener('click', () => {
      $$('#outMode button').forEach(x => x.classList.remove('on'));
      b.classList.add('on');
      updateGoState();
    }));
  }

  /* ================= 书库页 ================= */
  let libPath = '';
  let libItems = [];
  let scanCache = { fmt: '', path: '', dirs: null, truncated: false };
  let allFmts = [];        // 引擎支持的全部输入格式，筛选用，跟当前目录有什么无关
  let libJobId = null;

  async function loadLibrary(path = '') {
    const host = $('#flist');
    host.innerHTML = '<div class="empty">加载中…</div>';
    try {
      const r = await API.library(path);
      libPath = r.path;
      libItems = r.items;
      renderCrumbs(r.crumbs);
      scanCache = { fmt: '', path: '', dirs: null, truncated: false };
      if ($('#libFilter').value) applyLibFilter();
      else renderLibrary();
      // 格式下拉按实际出现过的格式生成
      const fmts = [...new Set(r.items.filter(i => !i.is_dir && i.fmt).map(i => i.fmt))].sort();
      const keepFmt = $('#libFilter').value;
      const present = r.items.filter(i => !i.is_dir && i.fmt).map(i => i.fmt);
      const all = [...new Set([...allFmts, ...present])].sort();
      $('#libFilter').innerHTML = '<option value="">全部格式</option>' +
        all.map(f => `<option value="${esc(f)}">${esc(f.toUpperCase())}</option>`).join('');
      if (keepFmt) $('#libFilter').value = keepFmt;
    } catch (e) {
      host.innerHTML = `<div class="empty">${esc(e.message || e)}</div>`;
    }
  }

  function renderCrumbs(crumbs) {
    $('#crumbs').innerHTML = crumbs.map((c, i) => {
      const last = i === crumbs.length - 1;
      const el = last
        ? `<span class="cur">${esc(c.name)}</span>`
        : `<a href="#" data-path="${esc(c.path)}">${esc(c.name)}</a>`;
      return el + (last ? '' : '<span class="sep">/</span>');
    }).join('');
    $$('#crumbs a').forEach(a => a.addEventListener('click', e => {
      e.preventDefault(); loadLibrary(a.dataset.path);
    }));
  }

  function renderLibrary() {
    const kw = ($('#libSearch').value || '').toLowerCase();
    const f = $('#libFilter').value;
    const rows = libItems.filter(it => {
      if (kw && !it.name.toLowerCase().includes(kw)) return false;
      if (!f) return true;
      if (it.is_dir) {
        // 文件夹本身没有格式 —— 看它「内部」有没有目标格式的文件
        if (!scanCache.dirs) return true;          // 扫描没成功就不过滤，别把目录藏了
        return scanCache.dirs.has(it.name);
      }
      return it.fmt === f;
    });
    if (!rows.length) {
      $('#flist').innerHTML = `<div class="empty">${
        f ? `这个目录及其子目录里没有 ${esc(f.toUpperCase())} 文件` : '这个目录里没有匹配的文件'
      }</div>`;
      refreshBatch(); return;
    }
    $('#flist').innerHTML = rows.map(it => `
      <div class="frow" data-name="${esc(it.name)}">
        ${it.is_dir ? '<span style="width:16px;flex:0 0 16px"></span>'
                    : `<input type="checkbox" class="cbx" data-name="${esc(it.name)}">`}
        <div class="fx">${it.is_dir ? 'DIR' : esc((it.fmt || '?').toUpperCase())}</div>
        <div class="nm${it.is_dir ? ' dir' : ''}" data-path="${esc(libPath)}" data-dir="${it.is_dir}">${esc(it.name)}</div>
        <div class="sz">${it.is_dir ? '—' : fmtSize(it.size)}</div>
        <div class="tm">${fmtTime(it.mtime)}</div>
      </div>`).join('');

    $$('#flist .nm.dir').forEach(n => n.addEventListener('click', () => {
      const cur = n.dataset.path;
      const next = (cur ? cur + '/' : '') + n.textContent.trim();
      loadLibrary(next);
    }));
    $$('#flist .cbx').forEach(b => b.addEventListener('change', refreshBatch));
    refreshBatch();
  }

  function selectedFiles() {
    return $$('#flist .cbx').filter(b => b.checked).map(b => {
      const name = b.dataset.name;
      const it = libItems.find(x => x.name === name) || {};
      return {
        name,
        size: it.size || 0,
        path: libRootPath + '/' + (libPath ? libPath + '/' : '') + name
      };
    });
  }
  let libRootPath = '';

  function refreshBatch() {
    const sel = selectedFiles();
    $$('#flist .frow').forEach(r => {
      const cb = r.querySelector('.cbx');
      r.classList.toggle('sel', !!(cb && cb.checked));
    });
    $('#batchCount').textContent = sel.length;
    $('#batchBar').classList.toggle('show', sel.length > 0);
    const boxes = $$('#flist .cbx');
    $('#selAll').textContent = (boxes.length && boxes.every(b => b.checked)) ? '取消全选' : '全选';
  }

  async function applyLibFilter() {
    const f = $('#libFilter').value;
    if (!f) {
      scanCache = { fmt: '', path: '', dirs: null, truncated: false };
      renderLibrary();
      return;
    }
    $('#flist').innerHTML = `<div class="empty">正在扫子目录里有没有 ${esc(f.toUpperCase())} …</div>`;
    try {
      const r = await API.scanFmt(f, libPath);
      scanCache = { fmt: f, path: libPath, dirs: new Set(r.dirs), truncated: r.truncated };
      if (r.truncated) toast(`目录太大，只扫了 ${r.scanned} 个条目就停了，筛选可能不全`, 'warn');
    } catch (e) {
      scanCache = { fmt: f, path: libPath, dirs: null, truncated: false };
      toast('扫描子目录失败：' + (e.message || e), 'fail');
    }
    renderLibrary();
  }

  $('#libSearch').addEventListener('input', renderLibrary);
  $('#libFilter').addEventListener('change', applyLibFilter);
  $('#libRefresh').addEventListener('click', () => loadLibrary(libPath));
  $('#selAll').addEventListener('click', () => {
    const boxes = $$('#flist .cbx');
    const on = boxes.every(b => b.checked);
    boxes.forEach(b => { b.checked = !on; });
    refreshBatch();
  });
  $('#clearSel').addEventListener('click', () => {
    $$('#flist .cbx').forEach(b => { b.checked = false; });
    refreshBatch();
  });

  $('#batchGo').addEventListener('click', async () => {
    const files = selectedFiles();
    const tgt = $('#batchTarget').value;
    if (!files.length) return toast('先勾选文件', 'warn');
    if (!tgt) return toast('先选目标格式', 'warn');
    if (libJobId) return;
    const delSrc = $('#delSource').checked;
    if (delSrc) {
      const ok = confirm(
        `确定要在转换成功后删除这 ${files.length} 个源文件？\n\n` +
        `删了就找不回来。转换失败的文件不会被删。`
      );
      if (!ok) return;
    }
    try {
      const r = await API.createJob(files, tgt, optFor({ delete_source: delSrc }));
      libJobId = r.job_id;
      $('#libProgress').style.display = '';
      $('#libLog').innerHTML = '';
      $('#libFill').style.width = '0%';
      const st = await pollJob(libJobId, (s) => {
        if (!s) return;
        $('#libFill').style.width = (s.total ? s.done / s.total * 100 : 0) + '%';
        $('#libStats').textContent = `${s.done} / ${s.total}` + (s.elapsed ? ` · ${s.elapsed} 秒` : '');
        $('#libLog').innerHTML = s.items.map(it =>
          logRow(`${it.name} → ${s.target.toUpperCase()}`, it.status, itemMeta(it),
                 it.status === 'lossy', it.out)
        ).join('');
        bindDownloads($('#libLog'));
      });
      if (st) {
        finishJob(st, 'library');
        loadLibrary(libPath);          // 刷新，输出文件会出现在列表里
      }
    } catch (e) {
      toast('批量转换失败：' + (e.message || e), 'fail');
    } finally {
      libJobId = null;
    }
  });

  /* ================= 记录页 ================= */
  let histItems = [];

  async function loadHistory() {
    try {
      const r = await API.history(300);
      histItems = r.items;
      $('#stTotal').textContent = r.stats.total;
      $('#stOk').textContent = r.stats.ok;
      $('#stLossy').textContent = r.stats.lossy;
      $('#stFail').textContent = r.stats.fail;
      renderHistory();
    } catch (e) {
      $('#histBody').innerHTML = `<tr><td colspan="6"><div class="empty">${esc(e.message || e)}</div></td></tr>`;
    }
  }

  function renderHistory() {
    const f = $('#histFilter').value;
    const rows = histItems.filter(h => !f || h.status === f);
    if (!rows.length) {
      $('#histBody').innerHTML = '<tr><td colspan="6"><div class="empty">还没有记录</div></td></tr>';
      return;
    }
    $('#histBody').innerHTML = rows.map(h => {
      const [cls, ico] = ST_ICON[h.status] || ST_ICON.pending;
      const ext = (h.src_name.split('.').pop() || '?').toUpperCase();
      const detail = h.status === 'fail'
        ? `<span style="color:var(--fail)">${esc(h.error || '失败')}</span>`
        : `${h.pages ? h.pages + ' 页 · ' : ''}${fmtSize(h.out_size)}`;
      const dl = h.out
        ? `<button class="icon-btn" data-dl="${esc(h.out)}" title="下载"><svg viewBox="0 0 24 24"><path d="M12 4v12M12 16l-4-4M12 16l4-4M4 20h16"/></svg></button>`
        : `<button class="icon-btn" data-retry="${esc(h.id)}" title="重试"><svg viewBox="0 0 24 24"><path d="M20 11a8 8 0 10-2.3 5.7M20 5v6h-6"/></svg></button>`;
      return `<tr>
        <td><span class="st ${cls}">${ico}</span></td>
        <td><div class="cellfile"><div class="fx">${esc(ext)}</div><span class="nm">${esc(h.src_name)}</span></div></td>
        <td><span class="tag">${esc((h.target || '').toUpperCase())}</span></td>
        <td class="mono"${h.status === 'lossy' ? ' style="color:var(--warn)"' : ''}>${detail}</td>
        <td class="mono">${esc(h.time || '')}</td>
        <td><div class="ops">${dl}</div></td>
      </tr>`;
    }).join('');

    $$('#histBody [data-dl]').forEach(b => b.addEventListener('click', () => {
      window.location.href = API.downloadUrl(b.dataset.dl);
    }));
    $$('#histBody [data-retry]').forEach(b => b.addEventListener('click', async () => {
      const h = histItems.find(x => x.id === b.dataset.retry);
      if (!h) return;
      try {
        const r = await API.createJob([{ path: h.src_path, name: h.src_name, size: h.src_size || 0 }],
          h.target, optFor());
        toast('已重新排队：' + h.src_name);
        await pollJob(r.job_id, () => { });
        loadHistory();
      } catch (e) {
        toast('重试失败：' + (e.message || e), 'fail');
      }
    }));
  }

  $('#histFilter').addEventListener('change', renderHistory);
  $('#histRefresh').addEventListener('click', loadHistory);
  $('#histClear').addEventListener('click', async () => {
    if (!confirm('清空全部转换记录？这个操作不可恢复。')) return;
    try {
      const r = await API.clearHistory();
      toast(`已清空 ${r.deleted} 条记录`);
      loadHistory();
    } catch (e) {
      toast('清空失败：' + (e.message || e), 'fail');
    }
  });

  /* ================= 设置页 ================= */
  let settingsCache = null;

  async function loadCacheStat() {
    try {
      const c = await API.cache();
      const age = c.oldest_age > 3600
        ? `最老的 ${Math.round(c.oldest_age / 3600)} 小时前`
        : c.oldest_age > 60 ? `最老的 ${Math.round(c.oldest_age / 60)} 分钟前` : '';
      $('#cacheStat').textContent = c.files
        ? `${c.files} 个文件 · ${fmtSize(c.size)}${age ? ' · ' + age : ''} · 超 ${Math.round(c.ttl / 3600)} 小时自动清理`
        : '缓存是空的';
    } catch (e) {
      const el = $('#cacheStat'); if (el) el.textContent = String(e.message || e);
    }
  }

  async function loadSettings() {
    try {
      settingsCache = await API.settings();
      fillSettings(settingsCache);
      loadCaps();
      loadCacheStat();
      refreshShareUI();
    } catch (e) {
      toast('读取设置失败：' + (e.message || e), 'fail');
    }
  }

  function fillSettings(s) {
    $$('.seg').forEach(seg => {
      const key = seg.dataset.key;
      const cur = String(s[key]);
      seg.querySelectorAll('button').forEach(b =>
        b.classList.toggle('on', String(b.dataset.val) === cur));
    });
    $('#renderFallback').checked = s.render_fallback !== false;
    $('#fontPath').value = s.font_path || '';
    $('#libRoot').value = s.library_root || '';
    $('#outputDir').value = s.output_dir || '';
  }

  async function savePatch(patch) {
    try {
      settingsCache = await API.saveSettings(patch);
      fillSettings(settingsCache);
    } catch (e) {
      toast('保存失败：' + (e.message || e), 'fail');
    }
  }

  $$('.seg').forEach(seg => {
    seg.querySelectorAll('button').forEach(btn => {
      btn.addEventListener('click', () => {
        const key = seg.dataset.key;
        let val = btn.dataset.val;
        if (key === 'font_size' || key === 'render_dpi') val = Number(val);
        savePatch({ [key]: val });
      });
    });
  });
  $('#renderFallback').addEventListener('change', e => savePatch({ render_fallback: e.target.checked }));
  $('#fontPath').addEventListener('change', e => savePatch({ font_path: e.target.value.trim() }));
  $('#libRoot').addEventListener('change', e => savePatch({ library_root: e.target.value.trim() }));
  $('#outputDir').addEventListener('change', e => savePatch({ output_dir: e.target.value.trim() }));

  $('#checkFont').addEventListener('click', async () => {
    try {
      const h = await API.health();
      $('#fontState').innerHTML = h.font_ok
        ? `<span class="dot ok"></span>可用 · ${esc(h.font.split('/').pop())}`
        : `<span class="dot fail"></span>${esc(h.font)}`;
    } catch (e) {
      $('#fontState').innerHTML = `<span class="dot fail"></span>${esc(e.message || e)}`;
    }
  });

  async function loadCaps() {
    try {
      const c = await API.capabilities();
      const rows = [
        ['漫画链', 'y', '支持', c.comic.inputs.map(x => x.toUpperCase()).join(' · ') + ' → ' + c.comic.targets.map(x => x.toUpperCase()).join('/')],
        ['文字链', 'y', '支持', c.text.inputs.map(x => x.toUpperCase()).join(' · ') + ' → ' + c.text.targets.map(x => x.toUpperCase()).join('/')],
      ];
      c.unsupported.forEach(u => rows.push([u.fmt.toUpperCase(), 'n', '不支持', u.why]));
      c.lossy.forEach(u => rows.push([u.fmt, 'p', '有损', u.why]));
      $('#capList').innerHTML = rows.map(([name, cls, label, note]) => `
        <div class="cap-row">
          <span>${esc(name)}</span>
          <span class="badge ${cls}">${label}</span>
          <span class="note">${esc(note)}</span>
        </div>`).join('');
    } catch { /* 能力表拿不到不影响用 */ }
  }

  $('#clearCache').addEventListener('click', async () => {
    if (!confirm('清空服务器上的上传副本？\n\n只删临时文件，不碰你设备上的原文件和书库。')) return;
    try {
      const r = await API.cacheClear();
      toast(`已清空 ${r.removed} 个文件，释放 ${fmtSize(r.freed)}`);
      loadCacheStat();
    } catch (e) {
      toast('清空失败：' + (e.message || e), 'fail');
    }
  });

  $('#clearHist').addEventListener('click', async () => {
    if (!confirm('清空全部转换记录？')) return;
    try {
      const r = await API.clearHistory();
      toast(`已清空 ${r.deleted} 条`);
    } catch (e) {
      toast('清空失败：' + (e.message || e), 'fail');
    }
  });


  /* ================= 分享模式（访客端） ================= */
  // URL 是 /s/<token> 时：只留转换页，其他全部拿掉
  (function applyShareMode() {
    if (!API.isShared) return;
    document.querySelectorAll('.nav-item').forEach(n => {
      if (n.dataset.page !== 'convert') n.remove();
    });
    ['library', 'history', 'settings'].forEach(id => {
      const el = document.getElementById('page-' + id);
      if (el) el.remove();
    });
    ['pickServer', 'shareBtn'].forEach(id => {
      const el = document.getElementById(id);
      if (el) el.remove();
    });
    const jo = document.getElementById('jobOpts');
    if (jo) jo.style.display = 'none';       // 访客没有输出目录/删源选项
    document.title = 'EbookForge · 格式转换';
    const lab = document.getElementById('engineLabel');
    if (lab) lab.textContent = '在线';
    // 顶部提示：这是分享链接
    const head = document.querySelector('#page-convert .page-head');
    if (head) {
      const tip = document.createElement('div');
      tip.className = 'warnbar';
      tip.style.marginTop = '0';
      tip.style.marginBottom = '20px';
      tip.innerHTML = `<svg viewBox="0 0 24 24"><path d="M12 8v5M12 16h.01"/><circle cx="12" cy="12" r="9"/></svg>
        <div>上传的文件在服务器只保留 24 小时，转换完直接下载。</div>`;
      head.parentNode.insertBefore(tip, head.nextSibling);
    }
  })();

  /* ================= 分享链接（主人端） ================= */
  function openShareModal() {
    const m = $('#shareModal');
    if (!m) return;
    m.classList.add('show');
    loadShareList();
  }
  function closeShareModal() { $('#shareModal')?.classList.remove('show'); }

  async function loadShareList() {
    const host = $('#shareList');
    if (!host) return;
    try {
      const r = await API.listShares();
      if (!r.items.length) { host.innerHTML = ''; return; }
      host.innerHTML = '<div class="hint" style="margin-bottom:6px">还在生效的链接</div>' +
        r.items.map(it => {
          const left = it.remaining === null ? '不设期限'
            : it.remaining > 3600 ? `${Math.round(it.remaining / 3600)} 小时后失效`
            : `${Math.round(it.remaining / 60)} 分钟后失效`;
          return `<div class="share-item">
            <span class="tk">${esc(it.token)}</span>
            <span class="tag">${esc(left)}</span>
            <button class="rm" data-revoke="${esc(it.token)}">撤销</button>
          </div>`;
        }).join('');
      $$('#shareList [data-revoke]').forEach(b => b.addEventListener('click', async () => {
        await API.revokeShare(b.dataset.revoke);
        toast('已撤销');
        loadShareList();
      }));
    } catch (e) {
      host.innerHTML = `<div class="hint">${esc(e.message || e)}</div>`;
    }
  }

  if ($('#shareBtn')) {
    $('#shareBtn').addEventListener('click', openShareModal);
    $('#shareClose')?.addEventListener('click', closeShareModal);
    $('#shareDone')?.addEventListener('click', closeShareModal);
    $('#shareBackdrop')?.addEventListener('click', closeShareModal);

    $('#shareCreate')?.addEventListener('click', async () => {
      try {
        const r = await API.createShare(24, '');
        const url = location.origin + r.path;
        $('#shareUrl').value = url;
        $('#shareMeta').textContent = '24 小时后自动失效。对方打开后只能转换和下载自己的结果。';
        try {
          await navigator.clipboard.writeText(url);
          toast('链接已生成并复制到剪贴板');
        } catch {
          toast('链接已生成，手动复制输入框里的地址');
        }
        loadShareList();
      } catch (e) {
        toast('生成失败：' + (e.message || e), 'fail');
      }
    });

    $('#shareCopy')?.addEventListener('click', async () => {
      const v = $('#shareUrl').value;
      if (!v) return toast('先点「生成新链接」', 'warn');
      try { await navigator.clipboard.writeText(v); toast('已复制'); }
      catch { $('#shareUrl').select(); toast('请手动复制'); }
    });
  }



  /* ================= 分享模式开关（设置页） ================= */
  let shareStatus = { enabled: false, admin_token: '', env_locked: false };

  async function refreshShareUI() {
    if (API.isShared) return;          // 分享页没有后台权限，别去戳需要令牌的接口
    try {
      shareStatus = await API.shareStatus();
    } catch { return; }
    // 设置页
    const cb = $('#shareEnabled');
    if (cb) {
      cb.checked = !!shareStatus.enabled;
      cb.disabled = !!shareStatus.env_locked;
      $('#shareHint').textContent = shareStatus.env_locked
        ? '令牌已由环境变量 EBOOKFORGE_ADMIN_TOKEN 固定，这里改不了。'
        : (shareStatus.enabled
            ? '已开启：别人的分享链接只能转换和下载自己的结果，后台接口需要密钥。'
            : '关闭状态：分享按钮不可用，后台接口不需要密码（适合纯内网自用）。');
      const box = $('#shareTokenBox');
      if (box) box.style.display = shareStatus.enabled && shareStatus.admin_token ? '' : 'none';
      const tk = $('#shareToken');
      if (tk && shareStatus.admin_token) {
        tk.value = shareStatus.admin_token;
        // 每次刷新回到打码状态：不该因为上次点过「显示」就一直明文摆着
        tk.type = 'password';
        const tg = $('#toggleShowToken');
        if (tg) tg.textContent = '显示';
      }
    }
    // 转换页的按钮
    const btn = $('#shareBtn');
    if (btn) {
      if (shareStatus.enabled) {
        btn.disabled = false;
        btn.style.opacity = '';
        btn.title = '生成一条 24 小时有效的链接，别人只能转换和下载自己的结果';
      } else {
        btn.disabled = true;
        btn.style.opacity = '.45';
        btn.title = '先在设置页开启「分享模式」';
      }
    }
  }

  function showTokenModal(token) {
    const m = $('#tokenShowModal');
    if (!m) return;
    $('#tokenShowValue').value = token;
    m.classList.add('show');
    $('#tokenShowOk').onclick = () => m.classList.remove('show');
    const changeBtn = $('#tokenShowChange');
    if (changeBtn) {
      changeBtn.onclick = () => {
        m.classList.remove('show');
        $('#newToken').value = token;      // 预填当前值，改一部分也行
        $('#tokenSetErr').textContent = '';
        $('#tokenSetModal')?.classList.add('show');
        setTimeout(() => $('#newToken')?.focus(), 60);
      };
    }
    $('#tokenShowCopy').onclick = async () => {
      try { await navigator.clipboard.writeText(token); toast('密钥已复制'); }
      catch { $('#tokenShowValue').select(); toast('请手动复制'); }
    };
  }

  if ($('#shareEnabled')) {
    const setModal = () => $('#tokenSetModal');

    $('#shareEnabled').addEventListener('change', async (e) => {
      const want = e.target.checked;
      if (want) {
        // 直接开启 + 自动随机密钥，开完弹窗告诉你。
        // 这个密钥只是「主人 vs 外人」的区分，不需要你记 ——
        // 设置页随时能查回来，所以随机就够，省一次输入。
        try {
          const r = await API.toggleShare(true, '');
          // ⚠️ 先把新密钥存上：开关一开，后台立刻要令牌，
          // 不存的话紧接着的 refreshShareUI() 就 401，弹出令牌框挡住当前弹窗。
          API.setAdminToken(r.admin_token);
          await refreshShareUI();
          showTokenModal(r.admin_token);
        } catch (err) {
          e.target.checked = false;
          toast('开启失败：' + (err.message || err), 'fail');
        }
        return;
      }
      const ok = confirm('关闭分享模式？\n\n所有已发出的分享链接会立刻失效。');
      if (!ok) { e.target.checked = true; return; }
      try {
        const r = await API.toggleShare(false);
        API.setAdminToken('');                // 免密了，本地别留着旧令牌
        toast(`已关闭，${r.revoked || 0} 条分享链接已作废`);
        await refreshShareUI();
      } catch (err) {
        e.target.checked = true;
        toast('关闭失败：' + (err.message || err), 'fail');
      }
    });

    // 取消 → 开关保持关闭
    $('#tokenSetCancel').onclick = () => {
      setModal()?.classList.remove('show');
      // 分享模式这时候已经开着了（密钥是自动生成的），开关保持勾选
      $('#shareEnabled').checked = true;
    };
    setModal()?.querySelector('.modal-backdrop').addEventListener('click', () => {
      setModal()?.classList.remove('show');
      $('#shareEnabled').checked = false;
    });

    // 随机生成一个
    $('#genToken').onclick = () => {
      const buf = new Uint8Array(12);
      crypto.getRandomValues(buf);
      $('#newToken').value = [...buf].map(b => b.toString(16).padStart(2, '0')).join('');
      $('#tokenSetErr').textContent = '';
    };

    // 确定开启
    $('#tokenSetOk').onclick = async () => {
      const v = $('#newToken').value.trim();
      if (v.length < 8) {
        $('#tokenSetErr').textContent = '至少 8 位';
        return;
      }
      try {
        const r = await API.toggleShare(true, v);
        API.setAdminToken(r.admin_token);     // 同上，先存再用
        setModal()?.classList.remove('show');
        $('#shareEnabled').checked = true;
        await refreshShareUI();
        toast('分享模式已开启');
      } catch (err) {
        $('#tokenSetErr').textContent = String(err.message || err);
      }
    };
    $('#newToken').addEventListener('keydown', e => {
      if (e.key === 'Enter') $('#tokenSetOk').click();
    });
  }


  if ($('#toggleShowToken')) {
    $('#toggleShowToken').addEventListener('click', () => {
      const inp = $('#shareToken');
      const show = inp.type === 'password';
      inp.type = show ? 'text' : 'password';
      $('#toggleShowToken').textContent = show ? '隐藏' : '显示';
    });
  }

  if ($('#copyShareToken')) {
    $('#copyShareToken').addEventListener('click', async () => {
      const v = $('#shareToken').value;
      if (!v) return;
      try { await navigator.clipboard.writeText(v); toast('密钥已复制'); }
      catch { $('#shareToken').select(); toast('请手动复制'); }
    });
  }

  /* ================= 管理令牌 ================= */
  window.addEventListener('ebf:need-token', () => {
    if (API.isShared) return;                 // 分享链接不需要令牌
    const m = $('#tokenModal');
    if (m) m.classList.add('show');
  });

  if ($('#tokenSave')) {
    const submit = async () => {
      const v = $('#tokenInput').value.trim();
      if (!v) return;
      API.setAdminToken(v);
      try {
        await API.settings();                 // 拿这个当验证：能读到设置就是对的
        $('#tokenModal').classList.remove('show');
        $('#tokenErr').textContent = '';
        location.reload();
      } catch (e) {
        API.setAdminToken('');
        $('#tokenErr').textContent = '令牌不对：' + (e.message || e);
      }
    };
    $('#tokenSave').addEventListener('click', submit);
    $('#tokenInput').addEventListener('keydown', e => { if (e.key === 'Enter') submit(); });
  }

  /* ================= 启动 ================= */
  // ⚠️ 先切页面，再拉数据。
  // 早先 navigate() 排在几个 await 后面，浏览器会先渲染 HTML 里默认的
  // 「格式转换」页，等网络回来才跳到刷新前那一页 —— 用户看到的是画面闪一下。
  // 现在切页在同步阶段完成（第一个 await 之前），首帧就是对的页面。
  navigate(location.hash.slice(1) || 'convert');

  (async () => {
    try {
      const s = await API.settings();
      settingsCache = s;
      libRootPath = s.library_root || '';
    } catch { }
    try {
      const c = await API.capabilities();
      allFmts = [...new Set([...c.comic.inputs, ...c.text.inputs])].sort();
    } catch { }
    updateFmtState();
    renderQueue();
    refreshShareUI();
  })();

})();
