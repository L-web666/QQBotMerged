/* ==========================================================================
   全局命名空间保护测试（Node + DOM 桩）

   目的：`window.Util / Effects / Chat / Settings / Admin / Groups / App` 不能被
   "整个替换掉"（别的脚本、控制台、被注入的页面都可能这么干），
   但**对象内部的字段还得能正常改**（`Chat.byKey = {}` 这类写法到处都是，
   所以这里不能用 Object.freeze —— 一旦冻结，这些赋值全部失效）。

   运行：`node tests/dom_test_globals.js`
   ========================================================================== */

'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.resolve(__dirname, '..');

function makeElement(id) {
  const node = {
    id, style: {}, dataset: {}, children: [], _innerHTML: '', textContent: '',
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    appendChild(child) { this.children.push(child); return child; },
    removeChild() {}, insertBefore(child) { this.children.push(child); return child; },
    setAttribute() {}, getAttribute() { return null; }, removeAttribute() {},
    addEventListener() {}, removeEventListener() {}, focus() {}, blur() {}, click() {},
    querySelector() { return null; }, querySelectorAll() { return []; },
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
const documentStub = {
  readyState: 'complete', hidden: false, body: makeElement('body'),
  getElementById: (id) => (elements[id] || (elements[id] = makeElement(id))),
  createElement: (tag) => makeElement(tag),
  querySelector: () => makeElement('q'), querySelectorAll: () => [],
  addEventListener() {}, removeEventListener() {}, dispatchEvent() {},
};

const windowStub = {
  document: documentStub, location: { search: '', hash: '' },
  innerWidth: 1400, innerHeight: 900, addEventListener() {}, removeEventListener() {},
  setTimeout, clearTimeout, setInterval: () => 0, clearInterval() {},
  localStorage: { _d: {}, getItem(k) { return this._d[k] ?? null; },
                  setItem(k, v) { this._d[k] = String(v); }, removeItem(k) { delete this._d[k]; } },
  __BOOT__: { ui: { lightbox: true }, limits: {}, auth_required: false },
  __URL_TOKEN__: '',
  fetch: () => new Promise(() => {}),
  EventSource: function () { this.close = () => {}; },
};

const sandbox = {
  window: windowStub, document: documentStub, console,
  setTimeout, clearTimeout, setInterval: () => 0, clearInterval() {},
  Promise, JSON, Math, Date, Object, Array, String, Number, Boolean, Error, RegExp,
  parseInt, parseFloat, isNaN, encodeURIComponent, decodeURIComponent,
  FileReader: function () { this.readAsDataURL = () => {}; },
  // app.js 一加载就会 boot()：这些浏览器 API 必须在沙箱里也存在
  fetch: windowStub.fetch, EventSource: windowStub.EventSource,
  localStorage: windowStub.localStorage, location: windowStub.location,
  __BOOT__: windowStub.__BOOT__, __URL_TOKEN__: '',
};
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
for (const file of ['util.js', 'effects.js', 'chat.js', 'settings.js', 'admin.js',
                    'groups.js', 'app.js']) {
  vm.runInContext(fs.readFileSync(path.join(ROOT, 'web', 'static', 'js', file), 'utf8'),
                  sandbox, { filename: file });
}

const results = [];
function check(name, ok, detail) {
  results.push(ok);
  console.log(`  ${ok ? '[OK]' : '[FAIL]'} ${name}${ok ? '' : '  ' + (detail || '')}`);
}

console.log('='.repeat(66));
console.log('  全局命名空间保护测试');
console.log('='.repeat(66));

const names = ['Util', 'Effects', 'Chat', 'Settings', 'Admin', 'Groups', 'App'];
for (const name of names) {
  const original = sandbox.window[name];
  check(`window.${name} 已经挂上`, !!original && typeof original === 'object', typeof original);
  // 试着整体替换（浏览器里非严格模式会静默失败，严格模式会抛错：两种都算"替换失败"）
  let replaced = false;
  try {
    sandbox.window[name] = { hacked: true };
  } catch (e) { /* 抛错也算保护成功 */ }
  check(`window.${name} 不能被整体替换`, sandbox.window[name] === original,
        '被替换成了 ' + JSON.stringify(sandbox.window[name]));
  // delete 也不该生效
  try { delete sandbox.window[name]; } catch (e) { /* 同上 */ }
  check(`window.${name} 不能被 delete 掉`, sandbox.window[name] === original, '');
  replaced = sandbox.window[name] !== original;
  if (replaced) { console.log(`    （${name} 保护失败）`); }
}

// 内部字段必须仍然可改：这正是不能用 Object.freeze 的原因
sandbox.window.Chat.byKey = { A: 1 };
check('对象内部字段仍然可以正常赋值（没有用 Object.freeze）',
      sandbox.window.Chat.byKey && sandbox.window.Chat.byKey.A === 1,
      JSON.stringify(sandbox.window.Chat.byKey));
sandbox.window.Admin.pluginChanges = {};
check('Admin.pluginChanges = {} 这类整字段赋值没被挡住',
      sandbox.window.Admin.pluginChanges !== undefined, '');

check('util.js 暴露了 protectGlobal 助手（其它文件靠它）',
      typeof sandbox.window.Util.protectGlobal === 'function'
      && typeof sandbox.window.__qbmProtect === 'function', '');

const failed = results.filter((ok) => !ok).length;
console.log('\n' + '='.repeat(66));
console.log(`  通过 ${results.length - failed} 项，失败 ${failed} 项`);
console.log('='.repeat(66));
process.exit(failed ? 1 : 0);
