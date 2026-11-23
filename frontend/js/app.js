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
    cbz: 'CBZ —— 漫画容器，图片原字节直嵌。文字书不能转 CBZ。'
  };
  const TEXT_ONLY = new Set(['txt', 'fb2', 'docx']);
  const COMIC_ONLY_TARGETS = new Set(['cbz']);

  let queue = [];            // [{path,name,size}]
  let target = 'pdf';
  let jobId = null;

  const fmtBtns = $$('#fmts .fmt');
  const fmtNote = $('#fmtNote');

  function updateFmtState() {
    const exts = new Set(queue.map(f => (f.name.split('.').pop() || '').toLowerCase()));
    const allText = queue.length > 0 && [...exts].every(e => TEXT_ONLY.has(e));
    fmtBtns.forEach(b => {
      if (b.dataset.f === 'azw3') return;                      // 永久禁用
      const off = allText && COMIC_ONLY_TARGETS.has(b.dataset.f);
      b.disabled = off;
      b.title = off ? '这些文件是文字书，不能转成漫画容器' : '';
    });
    if (allText && COMIC_ONLY_TARGETS.has(target)) {
      target = 'epub';
      fmtBtns.forEach(x => x.classList.toggle('on', x.dataset.f === 'epub'));
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
      return `<div class="file-row">
        <div class="fx">${esc(ext)}</div>
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
  }

  async function addFiles(fileList) {
    const files = [...fileList];
    if (!files.length) return;
    toast(`正在上传 ${files.length} 个文件…`);
    try {
      const r = await API.upload(files);
      queue.push(...r.files);
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

  function logRow(name, status, meta, metaWarn) {
    const [cls, ico] = ST_ICON[status] || ST_ICON.pending;
    return `<div class="log-row">
      <span class="st ${cls}">${ico}</span>
      <div class="log-main">
        <div class="log-name">${esc(name)}</div>
        <div class="log-meta${metaWarn ? ' warn' : ''}">${esc(meta)}</div>
      </div>
    </div>`;
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
      const r = await API.createJob(queue, target, optFor());
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
        $('#progLog').innerHTML = s.items.map(it =>
          logRow(`${it.name} → ${s.target.toUpperCase()}`, it.status, itemMeta(it), it.status === 'lossy')
        ).join('');
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

  /* ================= 书库页 ================= */
  let libPath = '';
  let libItems = [];
  let libJobId = null;

  async function loadLibrary(path = '') {
    const host = $('#flist');
    host.innerHTML = '<div class="empty">加载中…</div>';
    try {
      const r = await API.library(path);
      libPath = r.path;
      libItems = r.items;
      renderCrumbs(r.crumbs);
      renderLibrary();
      // 格式下拉按实际出现过的格式生成
      const fmts = [...new Set(r.items.filter(i => !i.is_dir && i.fmt).map(i => i.fmt))].sort();
      $('#libFilter').innerHTML = '<option value="">全部格式</option>' +
        fmts.map(f => `<option value="${esc(f)}">${esc(f.toUpperCase())}</option>`).join('');
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
    const rows = libItems.filter(it =>
      (!kw || it.name.toLowerCase().includes(kw)) &&
      (!f || (it.fmt === f))
    );
    if (!rows.length) {
      $('#flist').innerHTML = '<div class="empty">这个目录里没有匹配的文件</div>';
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

  $('#libSearch').addEventListener('input', renderLibrary);
  $('#libFilter').addEventListener('change', renderLibrary);
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
          logRow(`${it.name} → ${s.target.toUpperCase()}`, it.status, itemMeta(it), it.status === 'lossy')
        ).join('');
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

  async function loadSettings() {
    try {
      settingsCache = await API.settings();
      fillSettings(settingsCache);
      loadCaps();
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

  $('#clearHist').addEventListener('click', async () => {
    if (!confirm('清空全部转换记录？')) return;
    try {
      const r = await API.clearHistory();
      toast(`已清空 ${r.deleted} 条`);
    } catch (e) {
      toast('清空失败：' + (e.message || e), 'fail');
    }
  });

  /* ================= 启动 ================= */
  (async () => {
    // 书库根的绝对路径：让后端告诉我们（下载/转换都需要绝对路径）
    try {
      const s = await API.settings();
      libRootPath = s.library_root || '';
    } catch { }
    updateFmtState();
    renderQueue();
    navigate(location.hash.slice(1) || 'convert');
  })();

})();
