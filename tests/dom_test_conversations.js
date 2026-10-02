/* ==========================================================================
   会话列表「不跳动」回归测试（Node，无需浏览器）

   原理：给 chat.js 装一个最小 DOM 桩，记录每个元素被写入 innerHTML 的次数。
   连续两次用完全相同的数据渲染会话列表时，第二次必须**一次写入都没有**
   （这正是"每 2 秒轮询就整块重建 → 列表上下跳动"的根因）。

   运行：node tests/dom_test_conversations.js
   ========================================================================== */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.dirname(__dirname);
const writes = {};          // id -> 写入次数

function makeElement(id) {
  const node = {
    id,
    _innerHTML: '',
    textContent: '',
    value: '',
    style: { setProperty() {}, display: '' },
    className: '',
    children: [],
    firstChild: null,
    files: [],
    dataset: {},
    classList: {
      _set: new Set(),
      add(...names) { names.forEach((n) => this._set.add(n)); },
      remove(...names) { names.forEach((n) => this._set.delete(n)); },
      toggle(name, force) { if (force) this._set.add(name); else this._set.delete(name); },
      contains(name) { return this._set.has(name); },
    },
    setAttribute() {},
    getAttribute() { return null; },
    removeAttribute() {},
    addEventListener() {},
    removeEventListener() {},
    appendChild(child) { this.children.push(child); return child; },
    replaceChild() {},
    removeChild() {},
    insertAdjacentHTML() {},
    closest() { return null; },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    focus() {},
    scrollTo() {},
    getBoundingClientRect() { return { left: 0, top: 0, width: 100, height: 20 }; },
  };
  Object.defineProperty(node, 'innerHTML', {
    get() { return this._innerHTML; },
    set(value) {
      this._innerHTML = String(value);
      writes[id] = (writes[id] || 0) + 1;
    },
  });
  return node;
}

const elements = {};
function getElement(id) {
  if (!elements[id]) elements[id] = makeElement(id);
  return elements[id];
}

// ---- 最小 DOM / 浏览器环境 ----
const documentStub = {
  readyState: 'complete',
  hidden: false,
  body: makeElement('body'),
  getElementById: getElement,
  createElement: (tag) => makeElement(tag),
  querySelector: () => makeElement('query'),
  querySelectorAll: () => [],
  addEventListener() {},
  removeEventListener() {},
  dispatchEvent() {},
};

const windowStub = {
  document: documentStub,
  location: { search: '', hash: '' },
  innerWidth: 1400,
  innerHeight: 900,
  addEventListener() {},
  removeEventListener() {},
  setTimeout,
  clearTimeout,
  setInterval: () => 0,
  clearInterval() {},
  requestAnimationFrame: (fn) => setTimeout(fn, 0),
  localStorage: {
    _data: {},
    getItem(key) { return this._data[key] === undefined ? null : this._data[key]; },
    setItem(key, value) { this._data[key] = String(value); },
    removeItem(key) { delete this._data[key]; },
  },
  __BOOT__: {
    ui: { theme: 'auto', animation: 'full', poll_interval_ms: 2000, message_bubbles: true,
          show_avatar: true, lightbox: true, compact: false },
    limits: { max_image_mb: 6, max_file_mb: 8, message_max_length: 4000, page_size: 200 },
    auth_required: false,
  },
  __URL_TOKEN__: '',
  fetch: () => new Promise(() => {}),      // 不触发网络
  EventSource: function () { this.close = () => {}; },
};

const sandbox = {
  window: windowStub,
  document: documentStub,
  console,
  setTimeout,
  clearTimeout,
  setInterval: () => 0,
  clearInterval() {},
  Promise,
  JSON,
  Math,
  Date,
  Object,
  Array,
  String,
  Number,
  Boolean,
  Error,
  RegExp,
  parseInt,
  parseFloat,
  isNaN,
  encodeURIComponent,
  decodeURIComponent,
  FileReader: function () { this.readAsDataURL = () => {}; },
};
sandbox.globalThis = sandbox;
vm.createContext(sandbox);

// 按页面加载顺序执行前端脚本
for (const file of ['util.js', 'effects.js', 'chat.js']) {
  const code = fs.readFileSync(path.join(ROOT, 'web', 'static', 'js', file), 'utf8');
  vm.runInContext(code, sandbox, { filename: file });
}

const Chat = sandbox.window.Chat;
if (!Chat) {
  console.error('[FAIL] chat.js 没有导出 Chat 对象');
  process.exit(1);
}

// ---- 构造两组数据 ----
const baseConversations = [
  { key: 'bot1:group:G1', type: 'group', group_openid: 'G1', name: '测试群',
    last_time: '2026-10-01 20:00:00', last_content: '你好', unread: 0, message_count: 3 },
  { key: 'bot1:private:U1', type: 'private', openid: 'U1', name: '用户 A1B2C3',
    last_time: '2026-10-01 19:59:00', last_content: '在吗', unread: 2, message_count: 1, named: false },
  { key: 'bot1:private:U2', type: 'private', openid: 'U2', name: '老王',
    last_time: '2026-10-01 19:50:00', last_content: '图片', unread: 0, message_count: 5, named: true },
];

const results = [];
function check(name, ok, detail) {
  results.push({ name, ok });
  console.log(`  ${ok ? '[OK]' : '[FAIL]'} ${name}${ok ? '' : '  ' + (detail || '')}`);
}

function countWrites() {
  return ['groupList', 'privateList', 'groupCount', 'privateCount', 'botPicker']
    .reduce((sum, id) => sum + (writes[id] || 0), 0);
}

console.log('=' .repeat(64));
console.log('  会话列表渲染行为测试（防"上下跳动"）');
console.log('='.repeat(64));

// 1) 首次渲染：应当写入
Chat.renderConversations(baseConversations);
const afterFirst = countWrites();
check('首次渲染会写入列表', afterFirst > 0, `writes=${afterFirst}`);

// 2) 同样的数据再渲染一次（模拟轮询）：不允许有任何 DOM 写入
const beforeSecond = countWrites();
Chat.renderConversations(JSON.parse(JSON.stringify(baseConversations)));
const afterSecond = countWrites();
check('数据未变时不再重绘（不会跳动）', afterSecond === beforeSecond,
      `writes 从 ${beforeSecond} 变成 ${afterSecond}`);

// 3) 连续 5 次轮询同样数据，写入次数必须完全不变
for (let i = 0; i < 5; i++) {
  Chat.renderConversations(JSON.parse(JSON.stringify(baseConversations)));
}
check('连续 5 次轮询都不重绘', countWrites() === beforeSecond,
      `writes=${countWrites()}`);

// 4) 只有预览文本变化时才重绘一次
const changed = JSON.parse(JSON.stringify(baseConversations));
changed[1].last_content = '新消息来了';
changed[1].unread = 3;
Chat.renderConversations(changed);
check('内容真的变化时才重绘', countWrites() > beforeSecond,
      `writes=${countWrites()}`);

// 5) 已出现过的会话不应重复播放入场动画（.conv-settled）
const listHtml = elements['privateList']._innerHTML;
check('会话项带 data-key（供状态跟踪）', listHtml.indexOf('data-key=') >= 0, listHtml.slice(0, 80));
check('未命名用户会提示可双击命名', listHtml.indexOf('QQ 无昵称') >= 0, listHtml.slice(0, 160));

// 6) 底部统计与机器人下拉框也不应反复写入
const footBefore = (writes['botSummary'] || 0) + (writes['footStat'] || 0);
const pickerBefore = writes['botPicker'] || 0;
const boots = [{ id: 'bot1', name: '机器人 1', online: true, configured: true }];
Chat.bots = boots;
Chat.renderConversations(JSON.parse(JSON.stringify(changed)));
check('机器人下拉框只在内容变化时重建', (writes['botPicker'] || 0) === pickerBefore,
      `writes=${writes['botPicker'] || 0} before=${pickerBefore}`);

console.log('');
const failed = results.filter((item) => !item.ok);
console.log('='.repeat(64));
if (failed.length) {
  console.log(`  失败 ${failed.length} 项 / 共 ${results.length} 项`);
  process.exit(1);
}
console.log(`  全部通过（${results.length} 项）`);
process.exit(0);
