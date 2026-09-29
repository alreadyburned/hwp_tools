// Check the agent config writers (clients.js) on a scratch workspace.
//
// Run with any Node, e.g. VS Code's own:  ELECTRON_RUN_AS_NODE=1 "<VS Code>/Code.exe" tests/clients_test.js | cat
'use strict';

const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { CLIENTS, configure, serverArgs } = require('../clients');

const root = path.resolve(__dirname, '..');
const folder = fs.mkdtempSync(path.join(os.tmpdir(), 'hwp-clients-'));
const python = 'C:\\Users\\me\\.hwp-mcp\\venv\\Scripts\\python.exe';
const env = { PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1' };
const opts = (profile) => ({ folder, python, env, profile, extensionPath: root, version: '9.9.9' });
const read = (f) => fs.readFileSync(path.join(folder, f), 'utf8');
const json = (f) => JSON.parse(read(f));
const exists = (f) => fs.existsSync(path.join(folder, f));
const put = (f, text) => {
  fs.mkdirSync(path.dirname(path.join(folder, f)), { recursive: true });
  fs.writeFileSync(path.join(folder, f), text);
};
const rules = (profile) => fs.readFileSync(path.join(root, 'rules', `hwp-${profile}.md`), 'utf8');

assert.deepStrictEqual(CLIENTS.map((c) => c.id), ['claude', 'copilot', 'roo', 'kilo', 'continue']);
assert.deepStrictEqual(serverArgs('full'), ['-m', 'hwp_mcp']);
assert.deepStrictEqual(serverArgs('basic'), ['-m', 'hwp_mcp', '--profile', 'basic']);

// Claude Code: full tools, other servers kept, skill installed
put('.mcp.json', JSON.stringify({ mcpServers: { other: { command: 'x' } } }));
let r = configure('claude', opts('basic'));
assert.deepStrictEqual(json('.mcp.json').mcpServers.other, { command: 'x' });
assert.deepStrictEqual(json('.mcp.json').mcpServers.hwp.args, ['-m', 'hwp_mcp'], 'Claude Code always gets full');
assert.ok(exists('.claude/skills/hwp-direct/SKILL.md'));
console.log('claude:', r.written);

// Copilot: nothing written
assert.deepStrictEqual(configure('copilot', opts('full')), { written: [], removed: [] });

// Roo / Zoo: user settings on the hwp entry survive; skill only with the full profile
put('.roo/mcp.json', JSON.stringify({ mcpServers: { other: { command: 'y' }, hwp: { alwaysAllow: ['hwp_search'], timeout: 120 } } }));
r = configure('roo', opts('basic'));
let hwp = json('.roo/mcp.json').mcpServers.hwp;
assert.deepStrictEqual(hwp.args, ['-m', 'hwp_mcp', '--profile', 'basic']);
assert.deepStrictEqual(hwp.alwaysAllow, ['hwp_search']);
assert.strictEqual(hwp.timeout, 120);
assert.strictEqual(hwp.command, python);
assert.ok(json('.roo/mcp.json').mcpServers.other);
assert.strictEqual(read('.roo/rules/hwp-tools.md'), rules('basic'));
assert.ok(!exists('.roo/skills/hwp-direct'));
r = configure('roo', opts('full'));
assert.ok(exists('.roo/skills/hwp-direct/SKILL.md') && r.written.includes('.roo/skills/hwp-direct/'));
assert.strictEqual(read('.roo/rules/hwp-tools.md'), rules('full'));
r = configure('roo', opts('basic'));
assert.ok(!exists('.roo/skills/hwp-direct') && r.removed.includes('.roo/skills/hwp-direct/'), 'switching back removes the skill');
console.log('roo:', r);

// Kilo: new .kilo/kilo.jsonc; rerun does not duplicate the instructions entry
r = configure('kilo', opts('basic'));
configure('kilo', opts('basic'));
let kilo = json('.kilo/kilo.jsonc');
assert.strictEqual(kilo.$schema, 'https://app.kilo.ai/config.json');
assert.deepStrictEqual(kilo.mcp.hwp, { type: 'local', command: [python, '-m', 'hwp_mcp', '--profile', 'basic'], environment: env, enabled: true });
assert.deepStrictEqual(kilo.instructions, ['.kilo/rules/hwp-tools.md']);
assert.strictEqual(read('.kilo/rules/hwp-tools.md'), rules('basic'));
console.log('kilo new:', r.written);
// existing plain-JSON config: other keys kept, merged in place
put('.kilo/kilo.jsonc', JSON.stringify({ model: 'ollama/qwen3', instructions: ['AGENTS.md'], mcp: { hwp: { timeout: 30000 } } }));
configure('kilo', opts('full'));
kilo = json('.kilo/kilo.jsonc');
assert.strictEqual(kilo.model, 'ollama/qwen3');
assert.deepStrictEqual(kilo.instructions, ['AGENTS.md', '.kilo/rules/hwp-tools.md']);
assert.strictEqual(kilo.mcp.hwp.timeout, 30000);
assert.deepStrictEqual(kilo.mcp.hwp.command, [python, '-m', 'hwp_mcp']);
// a config with comments is not rewritten: the snippet comes back for the user to paste
fs.rmSync(path.join(folder, '.kilo'), { recursive: true });
const commented = '{\n  // my settings\n  "model": "ollama/qwen3",\n}\n';
put('kilo.jsonc', commented);
r = configure('kilo', opts('basic'));
assert.strictEqual(read('kilo.jsonc'), commented);
assert.strictEqual(r.manual.file, 'kilo.jsonc');
const snippet = JSON.parse(r.manual.snippet);
assert.deepStrictEqual(snippet.mcp.hwp.command, [python, '-m', 'hwp_mcp', '--profile', 'basic']);
assert.ok(exists('.kilo/rules/hwp-tools.md'));
console.log('kilo commented ->', r.manual.file);

// Continue: YAML server block and an always-applied rule
r = configure('continue', opts('basic'));
const yaml = read('.continue/mcpServers/hwp.yaml');
assert.ok(yaml.includes('schema: v1') && yaml.includes('version: 9.9.9'));
assert.ok(yaml.includes(`command: ${JSON.stringify(python)}`), yaml);
assert.ok(yaml.includes('args: ["-m", "hwp_mcp", "--profile", "basic"]'), yaml);
assert.ok(read('.continue/rules/hwp-tools.md').startsWith('---\nname: hwp_tools\n'));
assert.ok(read('.continue/rules/hwp-tools.md').endsWith(rules('basic')));
console.log('continue:', r.written);

// broken JSON is reported, not overwritten
put('.roo/mcp.json', '{ broken');
assert.throws(() => configure('roo', opts('basic')), /JSON 오류/);
assert.strictEqual(read('.roo/mcp.json'), '{ broken');

console.log(`\nALL CLIENT CHECKS PASSED -> ${folder}`);
