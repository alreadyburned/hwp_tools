// hwp_tools VS Code extension.
//  - keeps a private Python environment (~/.hwp-mcp/venv) with the bundled hwp_mcp server
//  - registers that server with the AI agents chosen in hwpMcp.clients: VS Code's MCP support
//    (Copilot), and workspace config files for Claude Code, Roo/Zoo Code, Kilo Code, Continue
//  - shows an approximate preview of .hwpx/.hwp files
'use strict';

const vscode = require('vscode');
const cp = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');
const clients = require('./clients');

const SERVER_LABEL = 'HWP 문서 편집 (hwp_tools)';
const SERVER_ENV = { PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1' };

let log;

function envRoot() {
  return path.join(os.homedir(), '.hwp-mcp');
}

function venvPython() {
  return process.platform === 'win32'
    ? path.join(envRoot(), 'venv', 'Scripts', 'python.exe')
    : path.join(envRoot(), 'venv', 'bin', 'python');
}

function run(command, args, options = {}) {
  return new Promise((resolve) => {
    log.appendLine(`$ ${command} ${args.join(' ')}`);
    let child;
    try {
      child = cp.spawn(command, args, { windowsHide: true, env: { ...process.env, ...SERVER_ENV }, ...options });
    } catch (err) {
      resolve({ code: -1, stdout: '', stderr: String(err) });
      return;
    }
    const out = [];
    const errOut = [];
    child.stdout.on('data', (d) => out.push(d));
    child.stderr.on('data', (d) => {
      errOut.push(d);
      if (options.logStderr) log.append(d.toString());
    });
    child.on('error', (err) => resolve({ code: -1, stdout: '', stderr: String(err) }));
    child.on('close', (code) =>
      resolve({ code, stdout: Buffer.concat(out).toString('utf8'), stderr: Buffer.concat(errOut).toString('utf8') })
    );
  });
}

async function runChecked(command, args, options) {
  const r = await run(command, args, { logStderr: true, ...options });
  if (r.code !== 0) {
    throw new Error(`${path.basename(command)} ${args.slice(0, 3).join(' ')} failed (exit ${r.code}): ${r.stderr.slice(-1500)}`);
  }
  return r;
}

async function findBasePython() {
  const configured = vscode.workspace.getConfiguration('hwpMcp').get('pythonPath');
  const candidates = configured
    ? [[configured, []]]
    : process.platform === 'win32'
      ? [['py', ['-3']], ['python', []], ['python3', []]]
      : [['python3', []], ['python', []]];
  for (const [cmd, pre] of candidates) {
    const r = await run(cmd, [...pre, '-c', 'import sys; print(sys.version_info >= (3, 10))']);
    if (r.code === 0 && r.stdout.trim() === 'True') return { cmd, pre };
  }
  throw new Error('Python 3.10 이상을 찾을 수 없습니다. Python을 설치하거나 설정 hwpMcp.pythonPath를 지정하세요.');
}

/** Cross-process lock (several VS Code windows share ~/.hwp-mcp). */
async function withInstallLock(fn) {
  const lock = path.join(envRoot(), 'install.lock');
  fs.mkdirSync(envRoot(), { recursive: true });
  for (;;) {
    try {
      fs.mkdirSync(lock);
      break;
    } catch (err) {
      if (err.code !== 'EEXIST') throw err;
      let age = 0;
      try {
        age = Date.now() - fs.statSync(lock).mtimeMs;
      } catch (_) {
        continue; // released meanwhile
      }
      if (age > 15 * 60 * 1000) {
        fs.rmSync(lock, { recursive: true, force: true }); // stale
        continue;
      }
      await new Promise((resolve) => setTimeout(resolve, 2000));
    }
  }
  try {
    return await fn();
  } finally {
    fs.rmSync(lock, { recursive: true, force: true });
  }
}

let ensuring = null;

/** Make sure ~/.hwp-mcp/venv exists and has this extension's server version installed. */
function ensureEnvironment(context, force = false) {
  if (ensuring) {
    // A forced reinstall must not be satisfied by an ordinary check that is already running.
    return force ? ensuring.catch(() => {}).then(() => ensureEnvironment(context, true)) : ensuring;
  }
  const version = context.extension.packageJSON.version;
  const marker = path.join(envRoot(), 'installed-version');
  const py = venvPython();
  const ready = () =>
    fs.existsSync(py) && fs.existsSync(marker) && fs.readFileSync(marker, 'utf8').trim() === version;
  ensuring = (async () => {
    if (!force && ready()) return py;
    await withInstallLock(async () => {
      if (!force && ready()) return; // another window finished while we waited
      await vscode.window.withProgress(
        { location: vscode.ProgressLocation.Notification, title: 'hwp_tools: Python 환경 준비 중' },
        async (progress) => {
          if (force && fs.existsSync(path.join(envRoot(), 'venv'))) {
            fs.rmSync(path.join(envRoot(), 'venv'), { recursive: true, force: true });
          }
          if (!fs.existsSync(py)) {
            progress.report({ message: '가상환경 생성…' });
            const base = await findBasePython();
            await runChecked(base.cmd, [...base.pre, '-m', 'venv', path.join(envRoot(), 'venv')]);
          }
          progress.report({ message: 'python-hwpx / mcp 설치…' });
          // Build from a temporary copy so pip does not write build files into the extension folder.
          const src = fs.mkdtempSync(path.join(os.tmpdir(), 'hwp-mcp-'));
          try {
            fs.cpSync(path.join(context.extensionPath, 'server'), src, { recursive: true });
            await runChecked(py, ['-m', 'pip', 'install', '--disable-pip-version-check', '--upgrade', src]);
          } finally {
            fs.rmSync(src, { recursive: true, force: true });
          }
          fs.writeFileSync(marker, version);
        }
      );
    });
    log.appendLine(`Python environment ready: ${py}`);
    return py;
  })().finally(() => {
    ensuring = null;
  });
  return ensuring;
}

// ---------------------------------------------------------------------------
// agent registration
// ---------------------------------------------------------------------------
function settings() {
  return vscode.workspace.getConfiguration('hwpMcp');
}

function enabledClients() {
  return settings().get('clients', ['claude']).filter((id) => clients.CLIENTS.some((c) => c.id === id));
}

/** Tool profile for a client: Claude Code and Copilot run large models; the others often local ones. */
function profileFor(id) {
  const client = clients.CLIENTS.find((c) => c.id === id);
  return client && client.local ? settings().get('localAgentProfile', 'basic') : 'full';
}

function registerMcpProvider(context) {
  if (!vscode.lm || typeof vscode.lm.registerMcpServerDefinitionProvider !== 'function') {
    log.appendLine('This VS Code has no MCP provider API; skipping VS Code MCP registration.');
    return;
  }
  const version = context.extension.packageJSON.version;
  const changed = new vscode.EventEmitter();
  context.subscriptions.push(
    changed,
    vscode.workspace.onDidChangeConfiguration((e) => {
      if (e.affectsConfiguration('hwpMcp.clients')) changed.fire();
    }),
    vscode.lm.registerMcpServerDefinitionProvider('hwpMcp.server', {
      onDidChangeMcpServerDefinitions: changed.event,
      // Copilot (VS Code agent mode) only when chosen in hwpMcp.clients.
      provideMcpServerDefinitions: async () =>
        enabledClients().includes('copilot')
          ? [new vscode.McpStdioServerDefinition(SERVER_LABEL, venvPython(), clients.serverArgs('full'), SERVER_ENV, version)]
          : [],
      resolveMcpServerDefinition: async (server) => {
        server.command = await ensureEnvironment(context);
        return server;
      },
    })
  );
}

async function pickWorkspaceFolder() {
  const folders = vscode.workspace.workspaceFolders || [];
  if (folders.length === 0) {
    vscode.window.showErrorMessage('hwp_tools: 먼저 폴더(워크스페이스)를 여세요.');
    return undefined;
  }
  if (folders.length === 1) return folders[0];
  return vscode.window.showWorkspaceFolderPick({ placeHolder: 'AI 에이전트 설정 파일을 만들 폴더' });
}

/** Write the workspace config of each client in ``ids`` and report what was written. */
async function applyClients(context, ids, folder) {
  const python = await ensureEnvironment(context);
  const lines = [];
  for (const id of ids) {
    const client = clients.CLIENTS.find((c) => c.id === id);
    const profile = profileFor(id);
    const result = clients.configure(id, {
      folder: folder.uri.fsPath, python, env: SERVER_ENV, profile,
      extensionPath: context.extensionPath, version: context.extension.packageJSON.version,
    });
    const files = result.written.length ? result.written.join(', ') : 'VS Code MCP 목록';
    lines.push(`${client.label} (${profile}): ${files}` + (result.removed.length ? `; 삭제 ${result.removed.join(', ')}` : ''));
    log.appendLine(`configured ${id} (${profile}): ${JSON.stringify(result)}`);
    if (result.manual) {
      // A config with comments is left alone; the user pastes the entry.
      await vscode.env.clipboard.writeText(result.manual.snippet);
      const doc = await vscode.workspace.openTextDocument(path.join(folder.uri.fsPath, result.manual.file));
      await vscode.window.showTextDocument(doc);
      vscode.window.showWarningMessage(
        `hwp_tools: ${result.manual.file}에 주석이 있어 직접 고치지 않았습니다. ` +
          '클립보드에 복사한 "mcp"와 "instructions" 항목을 붙여 넣으세요.'
      );
    }
  }
  vscode.window.showInformationMessage(
    `hwp_tools 등록: ${lines.join(' / ')}. 각 에이전트에서 MCP 서버 목록을 새로 고치거나 VS Code 창을 다시 로드하세요.`
  );
}

async function configureClients(context) {
  const enabled = new Set(enabledClients());
  const items = clients.CLIENTS.map((c) => ({
    id: c.id,
    label: c.label,
    description: c.local ? `도구 ${settings().get('localAgentProfile', 'basic')}` : '도구 full',
    detail: c.detail,
    picked: enabled.has(c.id),
  }));
  const picked = await vscode.window.showQuickPick(items, {
    canPickMany: true,
    title: 'hwp_tools: HWP 도구를 쓸 AI 에이전트',
    placeHolder: '선택한 에이전트의 설정 파일을 워크스페이스에 만듭니다 (선택 해제해도 기존 파일은 지우지 않음)',
  });
  if (!picked) return;
  const ids = picked.map((p) => p.id);
  await settings().update('clients', ids, vscode.ConfigurationTarget.Global);
  const withFiles = ids.filter((id) => id !== 'copilot');
  if (withFiles.length === 0) {
    vscode.window.showInformationMessage(`hwp_tools: ${ids.includes('copilot') ? 'Copilot에 등록했습니다.' : '등록한 에이전트가 없습니다.'}`);
    return;
  }
  const folder = await pickWorkspaceFolder();
  if (!folder) return;
  await applyClients(context, ids, folder);
}

async function configureClaudeCode(context) {
  const folder = await pickWorkspaceFolder();
  if (!folder) return;
  await applyClients(context, ['claude'], folder);
}

/** Re-write the local agents' configs when their tool profile changes. */
async function onProfileChanged(context) {
  const targets = enabledClients().filter((id) => (clients.CLIENTS.find((c) => c.id === id) || {}).local);
  if (targets.length === 0) return;
  const choice = await vscode.window.showInformationMessage(
    `hwp_tools: 로컬 에이전트 도구 구성이 "${settings().get('localAgentProfile')}"(으)로 바뀌었습니다. 설정 파일을 다시 만들까요?`,
    '다시 만들기'
  );
  if (!choice) return;
  const folder = await pickWorkspaceFolder();
  if (folder) await applyClients(context, targets, folder);
}

// ---------------------------------------------------------------------------
// semantic search (optional embeddings)
// ---------------------------------------------------------------------------
function embeddingSettings() {
  const s = settings();
  return {
    provider: s.get('embedding.provider', 'none'),
    ollama_url: s.get('embedding.ollamaUrl', 'http://localhost:11434'),
    ollama_model: s.get('embedding.ollamaModel', 'bge-m3'),
  };
}

/** The server re-reads ~/.hwp-mcp/settings.json on every search, so no agent config needs rewriting. */
function writeServerSettings() {
  const file = path.join(envRoot(), 'settings.json');
  let current = {};
  try {
    current = JSON.parse(fs.readFileSync(file, 'utf8'));
  } catch (_) {
    // missing or unreadable: start fresh
  }
  current.embedding = embeddingSettings();
  fs.mkdirSync(envRoot(), { recursive: true });
  fs.writeFileSync(file, JSON.stringify(current, null, 2) + '\n', 'utf8');
}

/** Run `python -m hwp_mcp.embed <cmd>`, passing "PROGRESS <pct> <msg>" lines to ``onProgress``. */
function runEmbed(python, cmd, onProgress) {
  return new Promise((resolve) => {
    const child = cp.spawn(python, ['-m', 'hwp_mcp.embed', cmd], { windowsHide: true, env: { ...process.env, ...SERVER_ENV } });
    let last = '';
    let buf = '';
    const onData = (d) => {
      buf += d.toString('utf8');
      const lines = buf.split(/\r?\n/);
      buf = lines.pop();
      for (const line of lines) {
        const m = /^PROGRESS (\d+) (.*)$/.exec(line);
        if (m) onProgress(Number(m[1]), m[2]);
        else if (line.trim()) {
          last = line.trim();
          log.appendLine(line);
        }
      }
    };
    child.stdout.on('data', onData);
    child.stderr.on('data', (d) => log.append(d.toString('utf8')));
    child.on('error', (err) => resolve({ ok: false, message: String(err) }));
    child.on('close', (code) => {
      if (buf.trim()) last = buf.trim();
      resolve({ ok: code === 0, message: last.replace(/^ERROR /, '') });
    });
  });
}

/** Install the runtime and download the model for the chosen provider (no-op when off or ready). */
async function prepareEmbedding(context, { force = false } = {}) {
  writeServerSettings();
  const conf = embeddingSettings();
  if (conf.provider === 'none') return;
  const python = await ensureEnvironment(context);
  if (!force) {
    const status = await runEmbed(python, 'status', () => {});
    if (status.ok && status.message.startsWith('ready')) return;
  }
  const label = conf.provider === 'ollama' ? `Ollama ${conf.ollama_model}` : '내장 모델 multilingual-e5-small (약 135MB)';
  const result = await withInstallLock(() =>
    vscode.window.withProgress(
      { location: vscode.ProgressLocation.Notification, title: `hwp_tools: 의미 검색 준비 — ${label}` },
      async (progress) => {
        let done = 0;
        return runEmbed(python, 'prepare', (pct, msg) => {
          progress.report({ message: msg, increment: Math.max(0, pct - done) });
          done = Math.max(done, pct);
        });
      }
    )
  );
  if (!result.ok) throw new Error(`의미 검색 준비 실패: ${result.message}`);
  vscode.window.showInformationMessage(`hwp_tools: ${result.message}. hwp_search가 뜻이 비슷한 문단도 찾습니다.`);
}

function reportEmbeddingError(err) {
  log.appendLine(String(err.stack || err));
  vscode.window.showErrorMessage(`hwp_tools: ${err.message}`, '로그 보기').then((choice) => {
    if (choice) log.show();
  });
}

// ---------------------------------------------------------------------------
// viewer: preview + editing (paste from another document, margins, undo)
// ---------------------------------------------------------------------------
function escapeHtml(s) {
  return String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

const TOOLBAR = `
<div id="hwpmcp-bar" style="position:fixed;right:12px;bottom:12px;z-index:9999;display:flex;gap:6px;align-items:center;
  font:12px system-ui,sans-serif;background:rgba(30,30,30,.85);color:#fff;padding:6px 10px;border-radius:6px">
  <button onclick="hwpmcp('refresh')">새로고침</button>
  <button onclick="hwpmcp('openExternal')">한컴오피스에서 열기</button>
</div>
<script>const __vs = acquireVsCodeApi(); function hwpmcp(type){ __vs.postMessage({type}); }</script>`;

function messageHtml(title, body) {
  return `<!doctype html><html><body style="font:14px system-ui,sans-serif;padding:24px">
    <h3>${escapeHtml(title)}</h3><pre style="white-space:pre-wrap">${escapeHtml(body)}</pre>${TOOLBAR}</body></html>`;
}

/** What was last copied in any viewer: pasting it into another viewer keeps its formatting. */
let hwpClipboard = null; // { path, addresses, text }
const UNDO_DEPTH = 20;
const undoStacks = new Map(); // file path -> [Buffer] of the file before each viewer edit

function sameText(a, b) {
  const norm = (s) => String(s || '').replace(/\r\n?/g, '\n').replace(/\u00a0/g, ' ').trim();
  return norm(a) === norm(b);
}

/** Run `python -m hwp_mcp.viewer apply` with one edit; resolves to its JSON result. */
function runViewerOp(python, op) {
  return new Promise((resolve) => {
    const child = cp.spawn(python, ['-m', 'hwp_mcp.viewer', 'apply'], { windowsHide: true, env: { ...process.env, ...SERVER_ENV } });
    const out = [];
    const err = [];
    child.stdout.on('data', (d) => out.push(d));
    child.stderr.on('data', (d) => err.push(d));
    child.on('error', (e) => resolve({ ok: false, message: String(e) }));
    child.on('close', () => {
      const text = Buffer.concat(out).toString('utf8').trim().split(/\r?\n/).pop() || '';
      try {
        resolve(JSON.parse(text));
      } catch (_) {
        const stderr = Buffer.concat(err).toString('utf8');
        log.appendLine(stderr);
        resolve({ ok: false, message: `편집 실패: ${stderr.slice(-500) || text}` });
      }
    });
    child.stdin.end(JSON.stringify(op));
  });
}

function writeFileAtomic(file, data) {
  const tmp = path.join(path.dirname(file), `.~hwpview-${process.pid}-${Date.now()}.tmp`);
  fs.writeFileSync(tmp, data);
  try {
    fs.renameSync(tmp, file);
  } catch (err) {
    fs.rmSync(tmp, { force: true });
    throw new Error(err.code === 'EPERM' || err.code === 'EBUSY' ? '파일이 다른 프로그램에서 열려 있어 쓸 수 없습니다.' : err.message);
  }
}

class PreviewProvider {
  constructor(context) {
    this.context = context;
  }

  openCustomDocument(uri) {
    return { uri, dispose() {} };
  }

  async resolveCustomEditor(document, panel) {
    const uri = document.uri;
    const file = uri.fsPath;
    const media = path.join(this.context.extensionPath, 'media');
    panel.webview.options = { enableScripts: true };
    let timer;
    let quietUntil = 0; // our own save also fires the file watcher: skip that render
    let busy = false;

    const render = async (initial = {}) => {
      try {
        const py = await ensureEnvironment(this.context);
        const r = await run(py, ['-m', 'hwp_mcp.viewer', 'render', file]);
        if (r.code !== 0) throw new Error(r.stderr.slice(-2000) || `exit ${r.code}`);
        const ui =
          `<style>${fs.readFileSync(path.join(media, 'viewer.css'), 'utf8')}</style>` +
          `<script>window.__hwpInitial = ${JSON.stringify(initial).replace(/</g, '\\u003c')};</script>` +
          `<script>${fs.readFileSync(path.join(media, 'viewer.js'), 'utf8')}</script>`;
        panel.webview.html = r.stdout.includes('</body>') ? r.stdout.replace('</body>', () => `${ui}</body>`) : r.stdout + ui;
      } catch (err) {
        panel.webview.html = messageHtml('미리보기를 만들 수 없습니다', String(err.message || err));
      }
    };
    const status = (text, error = false) => panel.webview.postMessage({ type: 'status', text, error });

    /** Apply one edit, keeping the previous bytes for undo, then redraw with the result selected. */
    const edit = async (op) => {
      if (busy) return status('이전 작업이 끝나기를 기다리는 중입니다.', true);
      busy = true;
      try {
        const python = await ensureEnvironment(this.context);
        const before = fs.readFileSync(file);
        const result = await runViewerOp(python, { ...op, path: file });
        if (!result.ok) return status(result.message, true);
        const stack = undoStacks.get(file) || [];
        stack.push(before);
        undoStacks.set(file, stack.slice(-UNDO_DEPTH));
        quietUntil = Date.now() + 2000;
        await render({ select: result.inserted, message: result.message });
      } catch (err) {
        status(String(err.message || err), true);
      } finally {
        busy = false;
      }
    };

    const handlers = {
      refresh: () => render(),
      openExternal: () => vscode.env.openExternal(uri),
      copy: async (msg) => {
        hwpClipboard = { path: file, addresses: msg.addresses, text: msg.text };
        await vscode.env.clipboard.writeText(msg.text);
        status(`복사했습니다: 문단 ${msg.addresses.length}개 — 다른 한글 문서 뷰어에서 Ctrl+V`);
      },
      copyText: async (msg) => {
        hwpClipboard = null;
        await vscode.env.clipboard.writeText(msg.text);
        status('텍스트를 복사했습니다.');
      },
      paste: async (msg) => {
        const text = await vscode.env.clipboard.readText();
        const rich = hwpClipboard && sameText(text, hwpClipboard.text) && fs.existsSync(hwpClipboard.path);
        if (rich) {
          return edit({ op: 'paste_paragraphs', after: msg.after, source: hwpClipboard.path, addresses: hwpClipboard.addresses });
        }
        if (!text.trim()) return status('클립보드에 붙여넣을 텍스트가 없습니다.', true);
        return edit({ op: 'paste_text', after: msg.after, text });
      },
      margins: (msg) => edit({ op: 'margins', section: msg.section, margins: msg.margins }),
      undo: async () => {
        const stack = undoStacks.get(file) || [];
        if (!stack.length) return status('이 뷰어에서 되돌릴 편집이 없습니다.', true);
        try {
          writeFileAtomic(file, stack[stack.length - 1]);
          stack.pop();
          quietUntil = Date.now() + 2000;
          await render({ message: `되돌렸습니다 (남은 단계 ${stack.length})` });
        } catch (err) {
          status(String(err.message || err), true);
        }
      },
    };
    panel.webview.html = messageHtml('불러오는 중…', file);
    panel.webview.onDidReceiveMessage((msg) => {
      const handler = handlers[msg && msg.type];
      if (handler) Promise.resolve(handler(msg)).catch((err) => status(String(err.message || err), true));
    });
    const watcher = vscode.workspace.createFileSystemWatcher(
      new vscode.RelativePattern(vscode.Uri.file(path.dirname(file)), path.basename(file))
    );
    const onChange = () => {
      if (Date.now() < quietUntil) return;
      clearTimeout(timer);
      timer = setTimeout(() => render(), 400);
    };
    watcher.onDidChange(onChange);
    watcher.onDidCreate(onChange);
    panel.onDidDispose(() => {
      clearTimeout(timer);
      watcher.dispose();
    });
    await render();
  }
}

// ---------------------------------------------------------------------------
function activate(context) {
  log = vscode.window.createOutputChannel('hwp_tools');
  context.subscriptions.push(log);

  registerMcpProvider(context);

  context.subscriptions.push(
    vscode.window.registerCustomEditorProvider('hwpMcp.preview', new PreviewProvider(context), {
      webviewOptions: { retainContextWhenHidden: false },
      supportsMultipleEditorsPerDocument: true,
    }),
    vscode.commands.registerCommand('hwpMcp.configureClients', () =>
      configureClients(context).catch((err) => vscode.window.showErrorMessage(`hwp_tools: ${err.message}`))
    ),
    vscode.commands.registerCommand('hwpMcp.configureClaudeCode', () =>
      configureClaudeCode(context).catch((err) => vscode.window.showErrorMessage(`hwp_tools: ${err.message}`))
    ),
    vscode.commands.registerCommand('hwpMcp.prepareEmbedding', () =>
      prepareEmbedding(context, { force: true }).catch(reportEmbeddingError)
    ),
    vscode.workspace.onDidChangeConfiguration((e) => {
      if (e.affectsConfiguration('hwpMcp.localAgentProfile')) {
        onProfileChanged(context).catch((err) => vscode.window.showErrorMessage(`hwp_tools: ${err.message}`));
      }
      if (e.affectsConfiguration('hwpMcp.embedding')) prepareEmbedding(context).catch(reportEmbeddingError);
    }),
    vscode.commands.registerCommand('hwpMcp.reinstall', () =>
      ensureEnvironment(context, true).then(
        () => {
          vscode.window.showInformationMessage('hwp_tools: Python 환경을 다시 설치했습니다.');
          return prepareEmbedding(context).catch(reportEmbeddingError); // the new venv lacks the embedding runtime
        },
        (err) => vscode.window.showErrorMessage(`hwp_tools: ${err.message}`)
      )
    ),
    vscode.commands.registerCommand('hwpMcp.openInHancom', (uri) => {
      const target = uri || (vscode.window.activeTextEditor && vscode.window.activeTextEditor.document.uri);
      if (target) vscode.env.openExternal(target);
    }),
    vscode.commands.registerCommand('hwpMcp.showLog', () => log.show())
  );

  // Prepare the environment (and the embedding model, if semantic search is on) in the background
  // so the first tool call is fast.
  ensureEnvironment(context).then(
    () => prepareEmbedding(context).catch(reportEmbeddingError),
    (err) => {
      log.appendLine(String(err.stack || err));
      vscode.window.showErrorMessage(`hwp_tools: Python 환경 준비 실패 — ${err.message}`, '로그 보기').then((choice) => {
        if (choice) log.show();
      });
    }
  );
}

function deactivate() {}

module.exports = { activate, deactivate };
