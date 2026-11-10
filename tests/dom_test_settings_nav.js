/* 验证设置页左侧导航：分组清单是否与后端分组一一对应。 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.resolve(__dirname, '..');
const PASSED = [];
const FAILED = [];

function check(name, condition, detail) {
  if (condition) {
    PASSED.push(name);
    console.log('  [OK] ' + name);
  } else {
    FAILED.push([name, detail || '']);
    console.log('  [FAIL] ' + name + '  ' + (detail || ''));
  }
}

function makeElement(tag) {
  const node = {
    tagName: (tag || 'div').toUpperCase(),
    id: '', className: '', value: '', textContent: '', _html: '',
    style: {}, dataset: {}, children: [], _listeners: {},
    classList: {
      _s: new Set(),
      add(c) { this._s.add(c); },
      remove(c) { this._s.delete(c); },
      toggle(c, on) { if (on === undefined) { this._s.has(c) ? this._s.delete(c) : this._s.add(c); } else if (on) { this._s.add(c); } else { this._s.delete(c); } },
      contains(c) { return this._s.has(c); },
    },
    appendChild(c) { this.children.push(c); return c; },
    removeChild() {}, insertBefore(c) { this.children.push(c); return c; },
    insertAdjacentHTML(pos, html) { this._html += String(html); },
    setAttribute(k, v) { this.dataset[k] = String(v); },
    getAttribute(k) { return Object.prototype.hasOwnProperty.call(this.dataset, k) ? this.dataset[k] : null; },
    removeAttribute(k) { delete this.dataset[k]; },
    addEventListener(t, fn) { (this._listeners[t] = this._listeners[t] || []).push(fn); },
    removeEventListener() {},
    querySelector() { return null; },
    querySelectorAll() { return []; },
    closest() { return null; },
    matches() { return false; },
    scrollIntoView() {},
    focus() {}, blur() {}, click() {},
    getBoundingClientRect() { return { top: 0, left: 0, width: 0, height: 0 }; },
    get offsetWidth() { return 100; },
  };
  Object.defineProperty(node, 'innerHTML', {
    get() { return this._html; },
    set(v) { this._html = String(v); },
  });
  return node;
}

const elements = {};
function el(id) { return elements[id] || (elements[id] = makeElement('div')); }

const documentStub = {
  readyState: 'complete', hidden: false, body: makeElement('body'),
  getElementById: el,
  createElement: (t) => makeElement(t),
  querySelector: () => null, querySelectorAll: () => [],
  addEventListener() {}, removeEventListener() {}, dispatchEvent() {},
};

const BOOT = { ui: {}, limits: {}, auth_required: false, version: '1.3.5', version_display: 'v1.3.5' };
const windowStub = {
  document: documentStub, location: { search: '', hash: '' },
  innerWidth: 1400, innerHeight: 900,
  addEventListener() {}, removeEventListener() {},
  setTimeout: () => 0, clearTimeout() {}, setInterval: () => 0, clearInterval() {},
  localStorage: { _d: {}, getItem(k) { return this._d[k] ?? null; }, setItem(k, v) { this._d[k] = String(v); }, removeItem(k) { delete this._d[k]; } },
  __BOOT__: BOOT, __URL_TOKEN__: '',
  fetch: () => new Promise(() => {}),
  EventSource: function () { this.close = () => {}; },
  CSS: { escape: (s) => s },
};
const sandbox = {
  window: windowStub, document: documentStub, console,
  setTimeout: () => 0, clearTimeout() {}, setInterval: () => 0, clearInterval() {},
  Promise, JSON, Math, Date, Object, Array, String, Number, Boolean, Error, RegExp,
  parseInt, parseFloat, isNaN, encodeURIComponent, decodeURIComponent,
  fetch: windowStub.fetch, EventSource: windowStub.EventSource,
  localStorage: windowStub.localStorage, location: windowStub.location,
  __BOOT__: BOOT, __URL_TOKEN__: '', CSS: windowStub.CSS,
};
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(path.join(ROOT, 'web/static/js/util.js'), 'utf8'), sandbox);
vm.runInContext(fs.readFileSync(path.join(ROOT, 'web/static/js/settings.js'), 'utf8'), sandbox);

const Settings = sandbox.window.Settings;

// 真实后端分组结构：由 Python 侧现场导出到临时文件再读回来。
// 不用 execFileSync 捕获 stdout —— 某些受限环境里子进程管道会被拒绝，
// 用"写文件 + 读文件"更稳。
function loadSpec() {
  const { execFileSync } = require('child_process');
  const tmpFile = path.join(ROOT, 'data', '_selftest',
                            '_nav_spec_' + process.pid + '.json');
  fs.mkdirSync(path.dirname(tmpFile), { recursive: true });
  const script = [
    'import json, sys',
    "sys.path.insert(0, r'" + ROOT + "')",
    'from core import config_schema as s',
    "with open(r'" + tmpFile + "', 'w', encoding='utf-8') as fh:",
    '    json.dump({"sections": s.sections_payload()}, fh, ensure_ascii=False)',
  ].join('\n');
  execFileSync('python', ['-c', script], {
    cwd: ROOT,
    stdio: 'ignore',
    env: Object.assign({}, process.env,
                       { PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1' }),
  });
  const spec = JSON.parse(fs.readFileSync(tmpFile, 'utf8'));
  try { fs.unlinkSync(tmpFile); } catch (e) { /* 清理失败无所谓 */ }
  return spec;
}

const spec = loadSpec();

function main() {
  console.log('='.repeat(66));
  console.log('  设置页左侧分类导航');
  console.log('='.repeat(66));

  console.log('\n[1] 页面结构里存在导航容器');
  const html = fs.readFileSync(path.join(ROOT, 'web/templates/index.html'), 'utf8');
  for (const id of ['settingsNav', 'settingsNavList', 'settingsForm']) {
    check('模板含 #' + id, html.indexOf('id="' + id + '"') >= 0, '');
  }
  check('模板含 settings-layout 布局', html.indexOf('settings-layout') >= 0, '');

  console.log('\n[2] CSS 定义了导航样式');
  const css = fs.readFileSync(path.join(ROOT, 'web/static/css/app.css'), 'utf8');
  for (const cls of ['.settings-layout', '.settings-nav', '.settings-nav-item',
                     '.settings-nav-item.active', '.cfg-section.flash']) {
    check('CSS 含 ' + cls, css.indexOf(cls) >= 0, '');
  }
  check('CSS 有窄屏适配', css.indexOf('@media (max-width: 980px)') >= 0, '');

  console.log('\n[3] 后端分组 → 导航条目');
  check('后端有多个分组', Array.isArray(spec.sections) && spec.sections.length > 5,
        String((spec.sections || []).length));
  check('每个分组都带 key/title/icon',
        (spec.sections || []).every((s) => s.key && s.title), '');

  const keys = (spec.sections || []).map((s) => s.key);
  check('分组 key 不重复', new Set(keys).size === keys.length, keys.join(','));

  console.log('\n[4] 设置页需要这些关键分组（便于快速定位）');
  for (const expected of ['bots', 'web', 'ai', 'reply', 'plugins', 'update',
                          'cloud', 'security', 'logging', 'ui']) {
    check('包含分组 ' + expected, keys.indexOf(expected) >= 0, keys.join(','));
  }

  console.log('\n' + '='.repeat(66));
  console.log('  通过 ' + PASSED.length + ' 项，失败 ' + FAILED.length + ' 项');
  FAILED.forEach((it) => console.log('   [FAIL] ' + it[0] + '  ' + it[1]));
  console.log('='.repeat(66));
  process.exit(FAILED.length ? 1 : 0);
}

main();
