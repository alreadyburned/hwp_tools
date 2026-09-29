// hwp_tools viewer: runs inside the preview webview. Selecting paragraphs, copy / paste
// (Ctrl+C / Ctrl+V), undo (Ctrl+Z) and the margin panel; the extension does the actual edits.
(() => {
  'use strict';
  const vscode = acquireVsCodeApi();
  const infoEl = document.getElementById('hwp-doc-info');
  const info = infoEl ? JSON.parse(infoEl.textContent) : { editable: false, sections: [] };
  const initial = window.__hwpInitial || {};
  const editable = !!info.editable;
  const state = vscode.getState() || {};
  if (editable) document.body.classList.add('hwp-editable');

  // ---- selection: a range of body paragraphs [anchor, focus] ---------------------------
  let anchor = null;
  let focus = null;
  const index = (addr) => Number(String(addr).slice(1));
  const selected = () => {
    if (anchor === null) return [];
    const out = [];
    for (let i = Math.min(anchor, focus); i <= Math.max(anchor, focus); i++) out.push(`p${i}`);
    return out;
  };
  const elementsOf = (addr) => Array.from(document.querySelectorAll(`[data-addr="${addr}"]`));
  const clean = (s) => s.replace(/ /g, ' ');
  // Tables become tab-separated rows (pastes into spreadsheets too); a paragraph that only holds
  // a table adds no empty line of its own.
  const textOf = (el) => el.tagName === 'TABLE'
    ? Array.from(el.rows).map((row) => Array.from(row.cells).map((c) => clean(c.innerText).replace(/\s+/g, ' ').trim()).join('\t')).join('\n')
    : clean(el.innerText).trimEnd();
  const selectedText = () => selected().map((a) => {
    const parts = elementsOf(a).map(textOf).filter((t) => t.trim());
    return parts.length ? parts.join('\n') : '';
  }).join('\n');

  const status = document.createElement('span');
  const setStatus = (text, kind) => {
    status.textContent = text;
    status.className = kind || '';
  };
  const describeSelection = () => {
    const sel = selected();
    if (!editable) return '읽기 전용 (이 문서는 화면과 문단 대응을 확인하지 못함)';
    if (!sel.length) return '문단을 클릭해 선택 (Shift+클릭: 범위)';
    return sel.length === 1 ? `${sel[0]} 선택` : `${sel[0]}–${sel[sel.length - 1]} 선택 (${sel.length}개)`;
  };
  const saveState = () => vscode.setState({ ...vscode.getState(), sel: anchor === null ? null : [anchor, focus] });
  const paint = (scroll) => {
    document.querySelectorAll('.hwp-sel').forEach((e) => e.classList.remove('hwp-sel'));
    const sel = selected();
    sel.forEach((a) => elementsOf(a).forEach((e) => e.classList.add('hwp-sel')));
    setStatus(describeSelection());
    if (scroll && sel.length) {
      const el = elementsOf(sel[0])[0];
      const r = el && el.getBoundingClientRect();
      if (r && (r.top < 0 || r.bottom > window.innerHeight)) el.scrollIntoView({ block: 'center' });
    }
  };
  const select = (first, last) => {
    anchor = first;
    focus = last;
    saveState();
    paint(false);
  };

  document.addEventListener('click', (e) => {
    if (!editable || e.target.closest('#hwp-bar, #hwp-margins')) return;
    const el = e.target.closest('[data-addr]');
    if (!el) return;
    const i = index(el.dataset.addr);
    if (e.shiftKey && anchor !== null) select(anchor, i);
    else select(i, i);
  });

  // ---- clipboard and undo ---------------------------------------------------------------
  const copy = () => {
    const sel = selected();
    if (sel.length) {
      vscode.postMessage({ type: 'copy', addresses: sel, text: selectedText() });
      return;
    }
    const text = String(window.getSelection() || '');
    if (text) vscode.postMessage({ type: 'copyText', text });
  };
  const paste = () => {
    if (!editable) return;
    const sel = selected();
    setStatus('붙여넣는 중', 'busy');
    vscode.postMessage({ type: 'paste', after: sel.length ? sel[sel.length - 1] : null });
  };
  const undo = () => {
    setStatus('되돌리는 중', 'busy');
    vscode.postMessage({ type: 'undo' });
  };
  // The browser copy (VS Code dispatches one after Ctrl+C) must carry the same text.
  document.addEventListener('copy', (e) => {
    if (!selected().length) return;
    e.clipboardData.setData('text/plain', selectedText());
    e.preventDefault();
  });
  document.addEventListener('keydown', (e) => {
    if (e.target.closest && e.target.closest('#hwp-margins')) return; // typing in the margin form
    const mod = e.ctrlKey || e.metaKey;
    const key = e.key.toLowerCase();
    if (mod && !e.shiftKey && key === 'c') copy();
    else if (mod && !e.shiftKey && key === 'v') paste();
    else if (mod && !e.shiftKey && key === 'z') undo();
    else if (key === 'escape') {
      anchor = focus = null;
      saveState();
      paint(false);
      panel.classList.remove('open');
    } else return;
    e.preventDefault();
  });

  // ---- toolbar ---------------------------------------------------------------------------
  const bar = document.createElement('div');
  bar.id = 'hwp-bar';
  const button = (label, title, fn, enabled = true) => {
    const b = document.createElement('button');
    b.textContent = label;
    b.title = title;
    b.disabled = !enabled;
    b.addEventListener('click', fn);
    bar.appendChild(b);
    return b;
  };
  bar.appendChild(status);
  button('복사', '선택한 문단 복사 (Ctrl+C)', copy, editable);
  button('붙여넣기', '선택한 문단 뒤에 붙여넣기, 선택이 없으면 문서 끝 (Ctrl+V)', paste, editable);
  button('되돌리기', '이 뷰어에서 한 마지막 편집 되돌리기 (Ctrl+Z)', undo);
  button('여백', '쪽 여백 조정', () => togglePanel(), editable);
  button('새로고침', '다시 그리기', () => vscode.postMessage({ type: 'refresh' }));
  button('한컴오피스에서 열기', '한컴오피스(또는 뷰어)로 열기', () => vscode.postMessage({ type: 'openExternal' }));
  document.body.appendChild(bar);

  // ---- margin panel ----------------------------------------------------------------------
  const FIELDS = [['top', '위'], ['bottom', '아래'], ['left', '왼쪽'], ['right', '오른쪽'], ['header', '머리말'], ['footer', '꼬리말']];
  const panel = document.createElement('div');
  panel.id = 'hwp-margins';
  const sections = info.sections || [];
  panel.innerHTML = `
    <h4>쪽 여백 (mm)</h4>
    <div class="grid">${FIELDS.map(([k, label]) =>
      `<label for="hwp-m-${k}">${label}</label><input id="hwp-m-${k}" type="number" min="0" max="150" step="0.5">`).join('')}</div>
    <div class="row"><label for="hwp-m-section">적용</label>
      <select id="hwp-m-section">
        <option value="all">모든 구역</option>
        ${sections.map((s) => `<option value="${s.section}">구역 ${s.section + 1} (${s.width_mm}×${s.height_mm}mm)</option>`).join('')}
      </select></div>
    <div class="note">머리말·꼬리말은 본문 위·아래에 따로 잡히는 영역의 높이입니다.</div>
    <div class="actions"><button id="hwp-m-cancel">취소</button><button id="hwp-m-apply" class="primary">적용</button></div>`;
  document.body.appendChild(panel);
  const sectionSelect = panel.querySelector('#hwp-m-section');
  const fill = () => {
    const pick = sectionSelect.value === 'all' ? sections[0] : sections[Number(sectionSelect.value)];
    if (!pick) return;
    for (const [k] of FIELDS) panel.querySelector(`#hwp-m-${k}`).value = pick.margins[k];
  };
  const togglePanel = () => {
    if (panel.classList.toggle('open')) {
      if (sections.length === 1) sectionSelect.value = '0';
      fill();
      panel.querySelector('#hwp-m-top').focus();
    }
  };
  sectionSelect.addEventListener('change', fill);
  panel.querySelector('#hwp-m-cancel').addEventListener('click', () => panel.classList.remove('open'));
  panel.querySelector('#hwp-m-apply').addEventListener('click', () => {
    const margins = {};
    for (const [k] of FIELDS) {
      const v = panel.querySelector(`#hwp-m-${k}`).value;
      if (v !== '') margins[k] = Number(v);
    }
    panel.classList.remove('open');
    setStatus('여백 바꾸는 중', 'busy');
    vscode.postMessage({ type: 'margins', section: sectionSelect.value === 'all' ? null : Number(sectionSelect.value), margins });
  });
  panel.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') panel.querySelector('#hwp-m-apply').click();
    if (e.key === 'Escape') panel.classList.remove('open');
  });

  // ---- messages from the extension and restoring state --------------------------------------
  window.addEventListener('message', (e) => {
    const msg = e.data || {};
    if (msg.type === 'status') setStatus(msg.text, msg.error ? 'error' : '');
  });
  const scroller = document.querySelector('.hwpx-viewer-scroll');
  const scrollTarget = scroller && scroller.scrollHeight > scroller.clientHeight ? scroller : document.scrollingElement;
  const onScroll = () => vscode.setState({ ...vscode.getState(), scroll: scrollTarget.scrollTop });
  (scroller || window).addEventListener('scroll', onScroll, { passive: true });
  window.addEventListener('scroll', onScroll, { passive: true });
  if (typeof state.scroll === 'number') scrollTarget.scrollTop = state.scroll;

  const pick = initial.select && initial.select.length ? initial.select : null;
  if (editable && pick) {
    select(index(pick[0]), index(pick[pick.length - 1]));
    paint(true);
  } else if (editable && state.sel && state.sel[1] < info.paragraphs) {
    anchor = state.sel[0];
    focus = state.sel[1];
    paint(false);
  } else {
    paint(false);
  }
  if (initial.message) setStatus(initial.message, initial.error ? 'error' : '');
})();
