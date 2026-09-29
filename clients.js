// Writes the "hwp" MCP server entry and short usage rules into an AI agent's workspace config.
// No vscode dependency, so it can be tested with plain Node (tests/clients_test.js).
'use strict';

const fs = require('fs');
const path = require('path');

const RULES_FILE = 'hwp-tools.md';
const KILO_SCHEMA = 'https://app.kilo.ai/config.json';

/** Agents the extension can register with. "local" ones default to the small "basic" tool profile. */
const CLIENTS = [
  { id: 'claude', label: 'Claude Code', local: false, detail: '.mcp.json, .claude/skills/hwp-direct' },
  { id: 'copilot', label: 'GitHub Copilot', local: false, detail: 'VS Code MCP 등록 (파일을 만들지 않음)' },
  { id: 'roo', label: 'Roo Code / Zoo Code', local: true, detail: '.roo/mcp.json, .roo/rules/hwp-tools.md' },
  { id: 'kilo', label: 'Kilo Code', local: true, detail: '.kilo/kilo.jsonc, .kilo/rules/hwp-tools.md' },
  { id: 'continue', label: 'Continue', local: true, detail: '.continue/mcpServers/hwp.yaml, .continue/rules/hwp-tools.md' },
];

function serverArgs(profile) {
  return profile === 'full' ? ['-m', 'hwp_mcp'] : ['-m', 'hwp_mcp', '--profile', profile];
}

function rel(folder, file) {
  return path.relative(folder, file).split(path.sep).join('/');
}

function writeText(file, text) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, text, 'utf8');
}

/** Parse a JSON config we are about to merge into; a missing file is an empty config. */
function readJson(file) {
  if (!fs.existsSync(file)) return {};
  try {
    return JSON.parse(fs.readFileSync(file, 'utf8'));
  } catch (err) {
    throw new Error(`${file}을 읽을 수 없습니다 (JSON 오류: ${err.message}). 직접 고친 뒤 다시 실행하세요.`);
  }
}

function writeJson(file, data) {
  writeText(file, JSON.stringify(data, null, 2) + '\n');
}

function rulesText(opts) {
  return fs.readFileSync(path.join(opts.extensionPath, 'rules', `hwp-${opts.profile}.md`), 'utf8');
}

function copySkill(opts, dir) {
  fs.cpSync(path.join(opts.extensionPath, 'skill', 'hwp-direct'), dir, { recursive: true });
}

// ---------------------------------------------------------------------------
// writers: each returns { written: [paths], removed: [paths], manual?: { file, snippet } }
// opts: { folder, python, env, profile, extensionPath, version }
// ---------------------------------------------------------------------------
function claude(opts) {
  const file = path.join(opts.folder, '.mcp.json');
  const config = readJson(file);
  config.mcpServers = config.mcpServers || {};
  // Claude Code always gets the full tool set (it runs Claude models) plus the scripting skill.
  config.mcpServers.hwp = { type: 'stdio', command: opts.python, args: serverArgs('full'), env: opts.env };
  writeJson(file, config);
  const skill = path.join(opts.folder, '.claude', 'skills', 'hwp-direct');
  copySkill(opts, skill);
  return { written: [rel(opts.folder, file), rel(opts.folder, skill) + '/'], removed: [] };
}

function roo(opts) {
  // Roo Code and its forks (Zoo Code) read the project's .roo/ folder.
  const file = path.join(opts.folder, '.roo', 'mcp.json');
  const config = readJson(file);
  config.mcpServers = config.mcpServers || {};
  const previous = config.mcpServers.hwp || {}; // keep the user's alwaysAllow / disabledTools / timeout
  config.mcpServers.hwp = { ...previous, type: 'stdio', command: opts.python, args: serverArgs(opts.profile), env: opts.env };
  writeJson(file, config);
  const rules = path.join(opts.folder, '.roo', 'rules', RULES_FILE);
  writeText(rules, rulesText(opts));
  const out = { written: [rel(opts.folder, file), rel(opts.folder, rules)], removed: [] };
  const skill = path.join(opts.folder, '.roo', 'skills', 'hwp-direct');
  if (opts.profile === 'full') {
    copySkill(opts, skill); // the scripting skill needs a capable model
    out.written.push(rel(opts.folder, skill) + '/');
  } else if (fs.existsSync(path.join(skill, 'SKILL.md'))) {
    fs.rmSync(skill, { recursive: true, force: true });
    out.removed.push(rel(opts.folder, skill) + '/');
  }
  return out;
}

function kilo(opts) {
  // Kilo Code 7 (OpenCode based): one kilo.jsonc; .kilo/kilo.jsonc wins over the project root one.
  const candidates = ['.kilo/kilo.jsonc', '.kilo/kilo.json', 'kilo.jsonc', 'kilo.json'].map((f) => path.join(opts.folder, f));
  const existing = candidates.find((f) => fs.existsSync(f));
  const file = existing || candidates[0];
  const rules = path.join(opts.folder, '.kilo', 'rules', RULES_FILE);
  const rulesRef = rel(opts.folder, rules);
  const entry = { type: 'local', command: [opts.python, ...serverArgs(opts.profile)], environment: opts.env, enabled: true };
  writeText(rules, rulesText(opts));
  const out = { written: [rel(opts.folder, rules)], removed: [] };
  let config = { $schema: KILO_SCHEMA };
  if (existing) {
    try {
      config = JSON.parse(fs.readFileSync(existing, 'utf8'));
    } catch (_) {
      // JSONC with comments: rewriting it would drop them, so hand the snippet to the user instead.
      out.manual = { file: rel(opts.folder, existing), snippet: JSON.stringify({ mcp: { hwp: entry }, instructions: [rulesRef] }, null, 2) };
      return out;
    }
  }
  config.mcp = config.mcp || {};
  config.mcp.hwp = { ...(config.mcp.hwp || {}), ...entry };
  config.instructions = config.instructions || [];
  if (!config.instructions.includes(rulesRef)) config.instructions.push(rulesRef);
  writeJson(file, config);
  out.written.unshift(rel(opts.folder, file));
  return out;
}

function yamlString(s) {
  return JSON.stringify(String(s)); // a JSON string is a valid YAML double-quoted scalar
}

function continueDev(opts) {
  const file = path.join(opts.folder, '.continue', 'mcpServers', 'hwp.yaml');
  const lines = [
    'name: hwp_tools',
    `version: ${opts.version}`,
    'schema: v1',
    'mcpServers:',
    '  - name: hwp',
    '    type: stdio',
    `    command: ${yamlString(opts.python)}`,
    `    args: [${serverArgs(opts.profile).map(yamlString).join(', ')}]`,
    '    env:',
    ...Object.entries(opts.env).map(([k, v]) => `      ${k}: ${yamlString(v)}`),
  ];
  writeText(file, lines.join('\n') + '\n');
  const rules = path.join(opts.folder, '.continue', 'rules', RULES_FILE);
  const front = [
    '---',
    'name: hwp_tools',
    'description: How to read and edit Hangul documents (.hwpx/.hwp) with the hwp MCP tools',
    'alwaysApply: true',
    '---',
    '',
  ].join('\n');
  writeText(rules, front + rulesText(opts));
  return { written: [rel(opts.folder, file), rel(opts.folder, rules)], removed: [] };
}

const WRITERS = { claude, roo, kilo, continue: continueDev };

/** Write the workspace files for one client. Copilot needs none (the extension registers it with VS Code). */
function configure(id, opts) {
  const writer = WRITERS[id];
  return writer ? writer(opts) : { written: [], removed: [] };
}

module.exports = { CLIENTS, configure, serverArgs };
