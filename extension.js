// hwp_tools VS Code extension.
//  - keeps a private Python environment (~/.hwp-mcp/venv) with the bundled hwp_mcp server
//  - registers that server with VS Code's MCP support (Copilot / agent mode)
//  - writes .mcp.json so Claude Code can use it
//  - shows an approximate preview of .hwpx/.hwp files
'use strict';

const vscode = require('vscode');
const cp = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

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
// MCP registration
// ---------------------------------------------------------------------------
function serverConfig() {
  return { type: 'stdio', command: venvPython(), args: ['-m', 'hwp_mcp'], env: SERVER_ENV };
}

function registerMcpProvider(context) {
  if (!vscode.lm || typeof vscode.lm.registerMcpServerDefinitionProvider !== 'function') {
    log.appendLine('This VS Code has no MCP provider API; skipping VS Code MCP registration.');
    return;
  }
  const version = context.extension.packageJSON.version;
  context.subscriptions.push(
    vscode.lm.registerMcpServerDefinitionProvider('hwpMcp.server', {
      provideMcpServerDefinitions: async () => [
        new vscode.McpStdioServerDefinition(SERVER_LABEL, venvPython(), ['-m', 'hwp_mcp'], SERVER_ENV, version),
      ],
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
  return vscode.window.showWorkspaceFolderPick({ placeHolder: '.mcp.json을 만들 폴더' });
}

async function configureClaudeCode(context) {
  const folder = await pickWorkspaceFolder();
  if (!folder) return;
  await ensureEnvironment(context);
  const file = path.join(folder.uri.fsPath, '.mcp.json');
  let config = {};
  if (fs.existsSync(file)) {
    try {
      config = JSON.parse(fs.readFileSync(file, 'utf8'));
    } catch (err) {
      vscode.window.showErrorMessage(`hwp_tools: ${file}을 읽을 수 없습니다 (JSON 오류). 직접 고친 뒤 다시 실행하세요.`);
      return;
    }
  }
  config.mcpServers = config.mcpServers || {};
  config.mcpServers.hwp = serverConfig();
  fs.writeFileSync(file, JSON.stringify(config, null, 2) + '\n', 'utf8');
  // The "hwp-direct" skill: scripted editing with hwp_mcp.api for what the MCP tools do not cover.
  const skillDir = path.join(folder.uri.fsPath, '.claude', 'skills', 'hwp-direct');
  fs.cpSync(path.join(context.extensionPath, 'skill', 'hwp-direct'), skillDir, { recursive: true });
  vscode.window.showInformationMessage(
    `hwp_tools: .mcp.json에 "hwp" 서버를, .claude/skills/hwp-direct에 스킬을 설치했습니다. ` +
      'Claude Code를 다시 시작한 뒤 /mcp에서 확인하세요.'
  );
}

// ---------------------------------------------------------------------------
// preview
// ---------------------------------------------------------------------------
function escapeHtml(s) {
  return String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

const TOOLBAR = `
<div id="hwpmcp-bar" style="position:fixed;right:12px;bottom:12px;z-index:9999;display:flex;gap:6px;align-items:center;
  font:12px system-ui,sans-serif;background:rgba(30,30,30,.85);color:#fff;padding:6px 10px;border-radius:6px">
  <span title="레이아웃·테두리·그림·자동 번호는 한컴오피스와 다를 수 있습니다">근사 미리보기</span>
  <button onclick="hwpmcp('refresh')">새로고침</button>
  <button onclick="hwpmcp('openExternal')">한컴오피스에서 열기</button>
</div>
<script>const __vs = acquireVsCodeApi(); function hwpmcp(type){ __vs.postMessage({type}); }</script>`;

function messageHtml(title, body) {
  return `<!doctype html><html><body style="font:14px system-ui,sans-serif;padding:24px">
    <h3>${escapeHtml(title)}</h3><pre style="white-space:pre-wrap">${escapeHtml(body)}</pre>${TOOLBAR}</body></html>`;
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
    panel.webview.options = { enableScripts: true };
    let timer;
    const render = async () => {
      try {
        const py = await ensureEnvironment(this.context);
        const r = await run(py, ['-m', 'hwp_mcp.preview', uri.fsPath]);
        if (r.code !== 0) throw new Error(r.stderr.slice(-2000) || `exit ${r.code}`);
        const html = r.stdout.includes('</body>') ? r.stdout.replace('</body>', `${TOOLBAR}</body>`) : r.stdout + TOOLBAR;
        panel.webview.html = html;
      } catch (err) {
        panel.webview.html = messageHtml('미리보기를 만들 수 없습니다', String(err.message || err));
      }
    };
    panel.webview.html = messageHtml('불러오는 중…', uri.fsPath);
    panel.webview.onDidReceiveMessage((msg) => {
      if (msg.type === 'refresh') render();
      if (msg.type === 'openExternal') vscode.env.openExternal(uri);
    });
    const watcher = vscode.workspace.createFileSystemWatcher(
      new vscode.RelativePattern(vscode.Uri.file(path.dirname(uri.fsPath)), path.basename(uri.fsPath))
    );
    const onChange = () => {
      clearTimeout(timer);
      timer = setTimeout(render, 400);
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
    vscode.commands.registerCommand('hwpMcp.configureClaudeCode', () =>
      configureClaudeCode(context).catch((err) => vscode.window.showErrorMessage(`hwp_tools: ${err.message}`))
    ),
    vscode.commands.registerCommand('hwpMcp.reinstall', () =>
      ensureEnvironment(context, true).then(
        () => vscode.window.showInformationMessage('hwp_tools: Python 환경을 다시 설치했습니다.'),
        (err) => vscode.window.showErrorMessage(`hwp_tools: ${err.message}`)
      )
    ),
    vscode.commands.registerCommand('hwpMcp.openInHancom', (uri) => {
      const target = uri || (vscode.window.activeTextEditor && vscode.window.activeTextEditor.document.uri);
      if (target) vscode.env.openExternal(target);
    }),
    vscode.commands.registerCommand('hwpMcp.showLog', () => log.show())
  );

  // Prepare the environment in the background so the first tool call is fast.
  ensureEnvironment(context).catch((err) => {
    log.appendLine(String(err.stack || err));
    vscode.window.showErrorMessage(`hwp_tools: Python 환경 준비 실패 — ${err.message}`, '로그 보기').then((choice) => {
      if (choice) log.show();
    });
  });
}

function deactivate() {}

module.exports = { activate, deactivate };
