// Smoke-test extension.js against a stubbed "vscode" module: Copilot opt-in, the agent
// registration command and the profile-change prompt. A temporary home directory with a fake
// ready environment keeps it away from the real ~/.hwp-mcp.
//
// Run with any Node, e.g. VS Code's own:  ELECTRON_RUN_AS_NODE=1 "<VS Code>/Code.exe" tests/extension_smoke_test.js | cat
'use strict';

const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');
const Module = require('module');

const root = path.resolve(__dirname, '..');
const pkg = JSON.parse(fs.readFileSync(path.join(root, 'package.json'), 'utf8'));
const home = fs.mkdtempSync(path.join(os.tmpdir(), 'hwp-home-'));
process.env.USERPROFILE = home;
process.env.HOME = home;
const venvPy = process.platform === 'win32' ? path.join(home, '.hwp-mcp', 'venv', 'Scripts', 'python.exe') : path.join(home, '.hwp-mcp', 'venv', 'bin', 'python');
fs.mkdirSync(path.dirname(venvPy), { recursive: true });
fs.writeFileSync(venvPy, '');
fs.writeFileSync(path.join(home, '.hwp-mcp', 'installed-version'), pkg.version); // environment counts as ready
const workspace = fs.mkdtempSync(path.join(os.tmpdir(), 'hwp-ws-'));

// ---- vscode stub -------------------------------------------------------------
const config = { clients: ['claude'], localAgentProfile: 'basic' };
const configListeners = [];
const commands = {};
const messages = [];
const answers = { quickPick: null, info: undefined };
let provider = null;

class EventEmitter {
  constructor() { this.listeners = []; this.event = (fn) => this.listeners.push(fn); }
  fire() { this.listeners.forEach((fn) => fn()); }
  dispose() {}
}
const disposable = { dispose() {} };
const vscode = {
  EventEmitter,
  ConfigurationTarget: { Global: 1 },
  ProgressLocation: { Notification: 15 },
  McpStdioServerDefinition: class { constructor(label, command, args, env, version) { Object.assign(this, { label, command, args, env, version }); } },
  lm: { registerMcpServerDefinitionProvider: (id, p) => { provider = p; return disposable; } },
  commands: { registerCommand: (id, fn) => { commands[id] = fn; return disposable; } },
  env: { clipboard: { writeText: async () => {} }, openExternal: () => {} },
  workspace: {
    workspaceFolders: [{ uri: { fsPath: workspace } }],
    getConfiguration: () => ({
      get: (key, dflt) => (key in config ? config[key] : dflt),
      update: async (key, value) => {
        config[key] = value;
        // like VS Code, a section matches its sub-keys ("hwpMcp.embedding" covers "hwpMcp.embedding.provider")
        const changed = `hwpMcp.${key}`;
        configListeners.forEach((fn) => fn({ affectsConfiguration: (k) => changed === k || changed.startsWith(k + '.') }));
      },
    }),
    onDidChangeConfiguration: (fn) => { configListeners.push(fn); return disposable; },
    openTextDocument: async () => ({}),
    createFileSystemWatcher: () => ({ onDidChange() {}, onDidCreate() {}, dispose() {} }),
  },
  window: {
    createOutputChannel: () => ({ appendLine: (l) => process.env.HWP_TEST_LOG && console.log('[log]', l), append: (l) => process.env.HWP_TEST_LOG && console.log('[log]', String(l).trim()), show() {}, dispose() {} }),
    showInformationMessage: async (msg) => { messages.push(msg); return answers.info; },
    showWarningMessage: async (msg) => { messages.push(msg); },
    showErrorMessage: async (msg) => { messages.push('ERROR ' + msg); },
    showQuickPick: async (items) => { answers.items = items; return answers.quickPick && items.filter((i) => answers.quickPick.includes(i.id)); },
    showWorkspaceFolderPick: async () => vscode.workspace.workspaceFolders[0],
    showTextDocument: async () => {},
    withProgress: async (_opts, fn) => fn({ report() {} }),
    registerCustomEditorProvider: () => disposable,
  },
};
const originalLoad = Module._load;
Module._load = function (request, ...rest) {
  return request === 'vscode' ? vscode : originalLoad.call(this, request, ...rest);
};

// ---- semantic search setup ------------------------------------------------------
// Needs a real Python with numpy: HWP_TEST_VENV=<venv dir>. Its interpreter stands in for the
// extension's venv, and a fake Ollama server answers the model pull.
async function checkEmbedding() {
  const testVenv = process.env.HWP_TEST_VENV;
  if (!testVenv) {
    console.log('(semantic search setup skipped: set HWP_TEST_VENV to a venv with numpy)');
    return;
  }
  fs.copyFileSync(path.join(testVenv, 'Scripts', 'python.exe'), venvPy);
  fs.copyFileSync(path.join(testVenv, 'pyvenv.cfg'), path.join(home, '.hwp-mcp', 'venv', 'pyvenv.cfg'));
  process.env.PYTHONPATH = [path.join(root, 'server'), path.join(testVenv, 'Lib', 'site-packages')].join(path.delimiter);
  const http = require('http');
  let pulled = false;
  const server = http.createServer((req, res) => {
    let body = '';
    req.on('data', (d) => (body += d));
    req.on('end', () => {
      if (req.url === '/api/tags') return res.end(JSON.stringify({ models: pulled ? [{ name: 'bge-m3:latest' }] : [] }));
      if (req.url === '/api/pull') {
        pulled = true;
        return res.end([0, 60, 100].map((c) => JSON.stringify({ status: 'pulling', total: 100, completed: c })).join('\n') + '\n');
      }
      if (req.url === '/api/embed' && pulled) return res.end(JSON.stringify({ embeddings: JSON.parse(body).input.map(() => [1, 0, 0]) }));
      res.statusCode = 404;
      res.end('{}');
    });
  });
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  const url = `http://127.0.0.1:${server.address().port}`;
  const before = messages.length;
  const cfg = vscode.workspace.getConfiguration('hwpMcp');
  config['embedding.ollamaUrl'] = url;
  await cfg.update('embedding.provider', 'ollama'); // triggers prepareEmbedding
  for (let i = 0; i < 600 && messages.length === before; i++) await new Promise((r) => setTimeout(r, 100));
  server.close();
  const written = JSON.parse(fs.readFileSync(path.join(home, '.hwp-mcp', 'settings.json'), 'utf8'));
  assert.deepStrictEqual(written.embedding, { provider: 'ollama', ollama_url: url, ollama_model: 'bge-m3' });
  const msg = messages[messages.length - 1];
  assert.ok(pulled && msg.includes('Semantic search ready: ollama:bge-m3'), messages.slice(before).join('\n'));
  console.log(msg);
}

// ---- run -----------------------------------------------------------------------
(async () => {
  const ext = require('../extension');
  const context = { extensionPath: root, extension: { packageJSON: pkg }, subscriptions: [] };
  ext.activate(context);
  await new Promise((r) => setTimeout(r, 50));
  for (const id of pkg.contributes.commands.map((c) => c.command)) assert.ok(commands[id], `command ${id} registered`);

  // Copilot is opt-in
  assert.deepStrictEqual(await provider.provideMcpServerDefinitions(), [], 'default: Claude only, no Copilot server');
  let fired = 0;
  provider.onDidChangeMcpServerDefinitions(() => fired++);

  // pick Copilot, Roo and Continue in the registration command
  answers.quickPick = ['copilot', 'roo', 'continue'];
  await commands['hwpMcp.configureClients']();
  assert.deepStrictEqual(answers.items.filter((i) => i.picked).map((i) => i.id), ['claude'], 'the quick pick starts from the setting');
  assert.deepStrictEqual(config.clients, ['copilot', 'roo', 'continue']);
  assert.strictEqual(fired, 1, 'Copilot list refreshed when the setting changes');
  const defs = await provider.provideMcpServerDefinitions();
  assert.strictEqual(defs.length, 1);
  assert.deepStrictEqual(defs[0].args, ['-m', 'hwp_mcp']);
  const roo = JSON.parse(fs.readFileSync(path.join(workspace, '.roo', 'mcp.json'), 'utf8'));
  assert.strictEqual(roo.mcpServers.hwp.command, venvPy);
  assert.deepStrictEqual(roo.mcpServers.hwp.args, ['-m', 'hwp_mcp', '--profile', 'basic']);
  assert.ok(fs.existsSync(path.join(workspace, '.continue', 'mcpServers', 'hwp.yaml')));
  assert.ok(!fs.existsSync(path.join(workspace, '.mcp.json')), 'Claude Code was not picked');
  console.log(messages[messages.length - 1]);

  // the Claude Code command still works on its own
  await commands['hwpMcp.configureClaudeCode']();
  assert.ok(fs.existsSync(path.join(workspace, '.claude', 'skills', 'hwp-direct', 'SKILL.md')));

  // switching local agents to the full profile offers to rewrite their configs
  answers.info = '다시 만들기';
  await vscode.workspace.getConfiguration('hwpMcp').update('localAgentProfile', 'full');
  await new Promise((r) => setTimeout(r, 50));
  const roo2 = JSON.parse(fs.readFileSync(path.join(workspace, '.roo', 'mcp.json'), 'utf8'));
  assert.deepStrictEqual(roo2.mcpServers.hwp.args, ['-m', 'hwp_mcp']);
  assert.ok(fs.existsSync(path.join(workspace, '.roo', 'skills', 'hwp-direct', 'SKILL.md')));
  assert.ok(fs.readFileSync(path.join(workspace, '.continue', 'mcpServers', 'hwp.yaml'), 'utf8').includes('args: ["-m", "hwp_mcp"]'));
  assert.ok(!messages.some((m) => m.startsWith('ERROR')), messages.join('\n'));
  console.log(messages[messages.length - 1]);

  await checkEmbedding();
  console.log(`\nEXTENSION SMOKE TEST PASSED -> ${workspace}`);
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
