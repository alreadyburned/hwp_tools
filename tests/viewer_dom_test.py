"""Run the viewer's webview script (media/viewer.js) in a headless Chrome/Edge on a real rendered
document: click / shift-click selection, Ctrl+C / Ctrl+V / Ctrl+Z, Escape, the margin panel, and
restoring a selection after a redraw. The messages the script sends to the extension are recorded
and checked.

Run:  <venv python> tests/viewer_dom_test.py [workdir]      (PYTHONPATH=server)
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import warnings
from pathlib import Path

from hwp_mcp import viewer
from hwp_mcp.api import _find_browser

warnings.filterwarnings("ignore")
ROOT = Path(__file__).resolve().parent.parent

# Stands in for VS Code: records messages and keeps the webview state.
STUB = """<script>
window.__sent = [];
window.__state = %s;
window.acquireVsCodeApi = () => ({
  postMessage: (m) => window.__sent.push(m),
  getState: () => window.__state,
  setState: (s) => { window.__state = s; },
});
window.__hwpInitial = %s;
</script>"""

# The scenario: runs after viewer.js, writes what happened into <pre id="result">.
SCENARIO = """<script>
(async () => {
  const r = {};
  const $ = (s) => document.querySelector(s);
  const click = (addr, shift) => $(`[data-addr="${addr}"]`).dispatchEvent(new MouseEvent('click', { bubbles: true, shiftKey: !!shift }));
  const key = (k, ctrl) => document.dispatchEvent(new KeyboardEvent('keydown', { key: k, ctrlKey: !!ctrl, bubbles: true }));
  const sel = () => Array.from(document.querySelectorAll('.hwp-sel')).map((e) => e.dataset.addr);
  r.status0 = $('#hwp-status') ? $('#hwp-status').textContent : document.querySelector('#hwp-bar span').textContent;
  click('p5'); click('p7', true);
  r.selected = sel();
  r.statusSel = document.querySelector('#hwp-bar span').textContent;
  key('c', true);
  key('v', true);
  click('p2');
  key('v', true);
  key('z', true);
  key('Escape');
  r.afterEscape = sel();
  key('v', true);
  document.querySelectorAll('#hwp-bar button').forEach((b) => { if (b.textContent === '여백') b.click(); });
  r.panelOpen = $('#hwp-margins').classList.contains('open');
  r.prefilled = $('#hwp-m-left').value + '/' + $('#hwp-m-top').value;
  $('#hwp-m-left').value = '18';
  $('#hwp-m-top').value = '';
  $('#hwp-m-apply').click();
  r.panelClosed = !$('#hwp-margins').classList.contains('open');
  window.postMessage({ type: 'status', text: '테스트 상태', error: true }, '*');
  await new Promise((ok) => setTimeout(ok, 50));
  r.statusMsg = document.querySelector('#hwp-bar span').textContent + '|' + document.querySelector('#hwp-bar span').className;
  r.sent = window.__sent;
  r.state = window.__state;
  const out = document.createElement('pre');
  out.id = 'result';
  out.textContent = JSON.stringify(r);
  document.body.appendChild(out);
})();
</script>"""


def run_page(html: str, work: str, name: str, browser: str) -> dict:
    page = Path(work) / f"{name}.html"
    page.write_text(html, encoding="utf-8")
    out = subprocess.run([browser, "--headless=new", "--disable-gpu", "--virtual-time-budget=3000",
                          f"--user-data-dir={Path(work) / 'profile'}", "--dump-dom", page.as_uri()],
                         capture_output=True, timeout=120)
    dom = out.stdout.decode("utf-8", "replace")
    m = re.search(r'<pre id="result">(.*?)</pre>', dom, re.S)
    assert m, dom[-2000:]
    return json.loads(m.group(1).replace("&quot;", '"').replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&"))


def page_html(state: dict, initial: dict, scenario: bool) -> str:
    html = viewer.render(str(ROOT / "samples" / "샘플_사업보고서.hwpx"))
    ui = (STUB % (json.dumps(state), json.dumps(initial))
          + f"<style>{(ROOT / 'media' / 'viewer.css').read_text(encoding='utf-8')}</style>"
          + f"<script>{(ROOT / 'media' / 'viewer.js').read_text(encoding='utf-8')}</script>"
          + (SCENARIO if scenario else RESTORE))
    return html.replace("</body>", ui + "</body>")


RESTORE = """<script>
const out = document.createElement('pre');
out.id = 'result';
out.textContent = JSON.stringify({ selected: Array.from(document.querySelectorAll('.hwp-sel')).map((e) => e.dataset.addr),
  status: document.querySelector('#hwp-bar span').textContent });
document.body.appendChild(out);
</script>"""


def main() -> None:
    work = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="hwpviewerdom-")
    os.makedirs(work, exist_ok=True)
    browser = _find_browser()
    if browser is None:
        print("SKIPPED: no Chrome or Edge found")
        return
    r = run_page(page_html({}, {}, True), work, "scenario", browser)
    print(json.dumps(r, ensure_ascii=False, indent=1)[:1500])
    assert r["selected"] == ["p5", "p6", "p7", "p7"], "p5-p7 selected; p7's table is highlighted too"
    assert r["statusSel"] == "p5–p7 선택 (3개)", r["statusSel"]
    sent = r["sent"]
    assert sent[0]["type"] == "copy" and sent[0]["addresses"] == ["p5", "p6", "p7"], sent[0]
    lines = sent[0]["text"].split("\n")
    assert lines[:4] == ["분기별 실적", "(단위: 억 원)", "구분\t1분기\t2분기\t상반기 계\t전년 대비",
                         "매출\t300\t360\t660\t+18.4%"], lines
    assert lines[-1] == "비고\t클라우드 부문 매출 비중 31% → 38%로 확대" and len(lines) == 7, lines
    assert sent[1] == {"type": "paste", "after": "p7"}, "paste goes after the last selected paragraph"
    assert sent[2] == {"type": "paste", "after": "p2"}
    assert sent[3] == {"type": "undo"}
    assert r["afterEscape"] == [] and sent[4] == {"type": "paste", "after": None}, "no selection: paste at the end"
    assert r["panelOpen"] and r["prefilled"] == "25/20", r["prefilled"]
    assert r["panelClosed"] and sent[5] == {"type": "margins", "section": 0, "margins": {
        "bottom": 15, "right": 25, "header": 15, "footer": 15, "left": 18}}, sent[5]
    assert r["statusMsg"] == "테스트 상태|error", r["statusMsg"]
    assert r["state"]["sel"] is None, "Escape cleared the saved selection"

    # after a paste the extension redraws with the inserted paragraphs selected
    r = run_page(page_html({}, {"select": ["p3", "p4"], "message": "붙여넣었습니다: 문단 2개"}, False), work, "initial", browser)
    assert r["selected"] == ["p3", "p4"] and r["status"] == "붙여넣었습니다: 문단 2개", r
    # a plain redraw (file changed elsewhere) keeps the selection saved in the webview state
    r = run_page(page_html({"sel": [9, 10]}, {}, False), work, "restore", browser)
    assert r["selected"] == ["p9", "p10"], r
    print("\nALL VIEWER DOM CHECKS PASSED ->", work)


if __name__ == "__main__":
    main()
