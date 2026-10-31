/* ==========================================================================
   设置页「添加机器人」重复插入的回归测试

   现象：在设置页点一次「＋ 添加机器人」，有时会出现 2 个甚至更多编辑框。
   根因：`render()` 每次都会调用 `bindFormEvents()`，而 `#settingsForm` 元素本身
   不会被替换（只改 innerHTML），于是在同一个元素上反复 addEventListener，
   **监听器累积**：渲染 N 次后点一次就插入 N 个编辑框。

   本测试用能真实记录监听器的 DOM 桩，验证：
     · 多次 render 之后，click 监听器只注册 1 个；
     · 点一次「添加机器人」只插入 1 个编辑框；
     · 在已绑定状态下再次 render，不会重复绑定。

   运行：`node tests/dom_test_settings.js`
   ========================================================================== */

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

/* ---------------- 能记录监听器的最小 DOM 桩 ---------------- */
function makeElement(id) {
  const listeners = {};
  let inserted = 0;
  const node = {
    id, style: {}, dataset: {}, children: [], _innerHTML: '', textContent: '',
    value: '', checked: false,
    _listeners: listeners,
    _inserted: () => inserted,
    _countOf(type) { return (listeners[type] || []).length; },
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    appendChild(child) { this.children.push(child); return child; },
    removeChild() {}, insertBefore(child) { this.children.push(child); return child; },
    insertAdjacentHTML() { inserted += 1; },
    setAttribute(k, v) { this.dataset[k] = v; },
    getAttribute(k) { return Object.prototype.hasOwnProperty.call(this.dataset, k) ? this.dataset[k] : null; },
    removeAttribute(k) { delete this.dataset[k]; },
    addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
    removeEventListener() {},
    focus() {}, blur() {}, click() {},
    querySelector(sel) {
      // 只实现测试用得到的这一个容器：插件/机器人/任务的插入目标
      if (sel === '[data-botsbox]' || sel === '[data-tasksbox]' || sel === '[data-cmdsbox]') {
        return (node._boxes = node._boxes || {})[sel] ||
          (node._boxes[sel] = makeElement(sel));
      }
      return null;
    },
    querySelectorAll() { return []; },
    matches() { return false; },
    scrollIntoView() {}, closest() { return null; }, contains() { return false; },
    getBoundingClientRect() { return { top: 0, left: 0, width: 0, height: 0 }; },
  };
  Object.defineProperty(node, 'innerHTML', {
    get() { return this._innerHTML; },
    set(v) { this._innerHTML = String(v); },
  });
  return node;
}

const elements = {};
function el(id) { return elements[id] || (elements[id] = makeElement(id)); }

const documentStub = {
  readyState: 'complete', hidden: false, body: makeElement('body'),
  getElementById: el,
  createElement: (tag) => makeElement(tag),
  querySelector: () => makeElement('q'), querySelectorAll: () => [],
  addEventListener() {}, removeEventListener() {}, dispatchEvent() {},
};

const windowStub = {
  document: documentStub, location: { search: '', hash: '' },
  innerWidth: 1400, innerHeight: 900,
  addEventListener() {}, removeEventListener() {},
  setTimeout, clearTimeout, setInterval: () => 0, clearInterval() {},
  localStorage: { _d: {}, getItem(k) { return this._d[k] ?? null; },
                  setItem(k, v) { this._d[k] = String(v); }, removeItem(k) { delete this._d[k]; } },
  __BOOT__: { ui: {}, limits: {}, auth_required: false },
  __URL_TOKEN__: '',
  fetch: () => new Promise(() => {}),
  EventSource: function () { this.close = () => {}; },
};

const sandbox = {
  window: windowStub, document: documentStub, console,
  setTimeout, clearTimeout, setInterval: () => 0, clearInterval() {},
  Promise, JSON, Math, Date, Object, Array, String, Number, Boolean, Error, RegExp,
  parseInt, parseFloat, isNaN, encodeURIComponent, decodeURIComponent,
  fetch: windowStub.fetch, EventSource: windowStub.EventSource,
  localStorage: windowStub.localStorage, location: windowStub.location,
  __BOOT__: windowStub.__BOOT__, __URL_TOKEN__: '',
};
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(path.join(ROOT, 'web/static/js/util.js'), 'utf8'), sandbox);

/* ---------------- 载入 settings.js ---------------- */
const settingsSrc = fs.readFileSync(path.join(ROOT, 'web/static/js/settings.js'), 'utf8');
vm.runInContext(settingsSrc, sandbox);

const Settings = sandbox.window.Settings;

function main() {
  console.log('='.repeat(66));
  console.log('  设置页「添加机器人」不应重复插入');
  console.log('='.repeat(66));

  const form = el('settingsForm');

  console.log('\n[1] settings.js 正确加载');
  check('window.Settings 存在', !!Settings, String(Settings));
  check('导出 render / load', typeof Settings.render === 'function'
        || typeof Settings.load === 'function', Object.keys(Settings || {}).join(','));

  // 准备一份最小 spec，让 render 有东西可画
  Settings.spec = {
    config: {}, defaults: {}, fields: {}, sections: [], section_labels: {},
    restart_required: [], env_overrides: [], env_unknown: [], editable: [],
    active_bot: 'bot1', bots: [],
  };
  el('settingsSearch').value = '';

  console.log('\n[2] 多次 render 之后，click 监听器只能有 1 个');
  const render = Settings.render;
  if (typeof render !== 'function') {
    check('render 可调用', false, 'settings.js 没有导出 render');
  } else {
    for (let i = 0; i < 5; i += 1) render();
    const clickCount = form._countOf('click');
    check('渲染 5 次后 click 监听器仍为 1 个', clickCount === 1, '实际 ' + clickCount + ' 个');

    console.log('\n[3] 点一次「添加机器人」只插入 1 个编辑框');
    // 插入目标是 form 里的 [data-botsbox] 容器，不是 form 本身
    const botsBox = form.querySelector('[data-botsbox]');
    const before = botsBox._inserted();
    const handlers = form._listeners['click'] || [];
    const fakeEvent = {
      target: {
        matches: (sel) => sel === '[data-addbot]',
        getAttribute: () => null,
      },
    };
    handlers.forEach((fn) => fn(fakeEvent));
    const added = botsBox._inserted() - before;
    check('只插入 1 个机器人编辑框', added === 1, '实际插入 ' + added + ' 个');

    console.log('\n[4] 再渲染几次后点击，仍然只插入 1 个');
    for (let i = 0; i < 3; i += 1) render();
    const before2 = botsBox._inserted();
    (form._listeners['click'] || []).forEach((fn) => fn(fakeEvent));
    const added2 = botsBox._inserted() - before2;
    check('反复渲染后点击仍只插入 1 个', added2 === 1, '实际插入 ' + added2 + ' 个');
  }

  console.log('\n'.repeat(0));
  console.log('='.repeat(66));
  console.log('  通过 ' + PASSED.length + ' 项，失败 ' + FAILED.length + ' 项');
  FAILED.forEach(([name, detail]) => console.log('   [FAIL] ' + name + '  ' + detail));
  console.log('='.repeat(66));
  return FAILED.length ? 1 : 0;
}

process.exit(main());
