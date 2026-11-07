/* ==========================================================================
   版本显示与「检查更新」的前端行为测试（Node + DOM 桩）

   钉住三件事：
     1. 版本号来自后端启动数据（`BOOT.version_display`），**不许在前端写死**；
     2. `Admin.showVersion()` 会把版本写进侧栏 `#appVersion` 与关于页 `#aboutVersion`；
     3. `Admin.loadUpdateStatus()` 会把「当前版本 / 最新版本 / 状态」渲染进 `#updateGrid`，
        更新进行中会显示进度，装好待重启时会明确提示重启。

   运行：`node tests/dom_test_update.js`
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

function makeElement(id) {
  const listeners = {};
  const node = {
    id, style: {}, dataset: {}, children: [], _innerHTML: '', textContent: '',
    value: '', checked: false, disabled: false, href: '',
    _listeners: listeners,
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    appendChild(child) { this.children.push(child); return child; },
    removeChild() {}, insertBefore(child) { this.children.push(child); return child; },
    insertAdjacentHTML(pos, html) { this._innerHTML += String(html); },
    setAttribute(k, v) { this.dataset[k] = v; },
    getAttribute(k) { return Object.prototype.hasOwnProperty.call(this.dataset, k) ? this.dataset[k] : null; },
    removeAttribute(k) { delete this.dataset[k]; },
    addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
    removeEventListener() {},
    focus() {}, blur() {}, click() {},
    querySelector() { return makeElement('q'); },
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

// 每次 fetch 返回哪份数据由 currentPayload / checkPayload 决定（按 URL 区分）
let currentPayload = {};
let checkPayload = {};
let lastUrl = '';
const requestUrls = [];

// 版本号从 core/version.py 读，不在测试里写死（发版改版本是常态，写死会天天飘）
const VERSION = (function () {
  const source = fs.readFileSync(path.join(ROOT, 'core/version.py'), 'utf8');
  const match = source.match(/^__version__\s*=\s*"([^"]+)"/m);
  if (!match) throw new Error('core/version.py 里找不到 __version__');
  return match[1];
})();
// 一个"比当前新"的版本，用来模拟"发现新版本"的场景
const NEWER = (function () {
  const parts = VERSION.split('.');
  parts[0] = String(Number(parts[0]) + 1);
  return parts.join('.');
})();
const SHOW = 'v' + VERSION;
const SHOW_NEWER = 'v' + NEWER;

const BOOT = {
  ui: {}, limits: {}, auth_required: true,
  version: VERSION, version_display: SHOW,
  repo: 'L-web666/QQBotMerged', repo_url: 'https://github.com/L-web666/QQBotMerged',
};

const windowStub = {
  document: documentStub, location: { search: '', hash: '' },
  innerWidth: 1400, innerHeight: 900,
  addEventListener() {}, removeEventListener() {},
  setTimeout: (fn) => { return 0; },      // 不真的排程，避免轮询把测试带飞
  clearTimeout() {}, setInterval: () => 0, clearInterval() {},
  localStorage: { _d: {}, getItem(k) { return this._d[k] ?? null; },
                  setItem(k, v) { this._d[k] = String(v); }, removeItem(k) { delete this._d[k]; } },
  __BOOT__: BOOT,
  __URL_TOKEN__: 'TEST-TOKEN',
  fetch: (url) => {
    lastUrl = String(url);
    requestUrls.push(String(url));
    const body = String(url).indexOf('/update/check') >= 0 ? checkPayload : currentPayload;
    return Promise.resolve({
      ok: true, status: 200,
      json: () => Promise.resolve(body),
      text: () => Promise.resolve(JSON.stringify(body)),
    });
  },
  EventSource: function () { this.close = () => {}; },
  confirm: () => true,
};

const sandbox = {
  window: windowStub, document: documentStub, console,
  setTimeout: windowStub.setTimeout, clearTimeout: windowStub.clearTimeout,
  setInterval: () => 0, clearInterval() {},
  Promise, JSON, Math, Date, Object, Array, String, Number, Boolean, Error, RegExp,
  parseInt, parseFloat, isNaN, encodeURIComponent, decodeURIComponent,
  fetch: windowStub.fetch, EventSource: windowStub.EventSource,
  localStorage: windowStub.localStorage, location: windowStub.location,
  __BOOT__: BOOT, __URL_TOKEN__: 'TEST-TOKEN', confirm: windowStub.confirm,
};
sandbox.globalThis = sandbox;
vm.createContext(sandbox);

vm.runInContext(fs.readFileSync(path.join(ROOT, 'web/static/js/util.js'), 'utf8'), sandbox);
vm.runInContext(fs.readFileSync(path.join(ROOT, 'web/static/js/admin.js'), 'utf8'), sandbox);

const Admin = sandbox.window.Admin;

function main() {
  console.log('='.repeat(66));
  console.log('  版本显示与检查更新（前端）');
  console.log('='.repeat(66));

  console.log('\n[1] 版本号来自后端启动数据');
  check('Admin 已加载', !!Admin, String(Admin));
  check('暴露 showVersion', typeof Admin.showVersion === 'function', '');
  Admin.showVersion();
  check('侧栏版本 = ' + SHOW, el('appVersion').textContent === SHOW,
        el('appVersion').textContent);
  check('关于页版本 = ' + SHOW, el('aboutVersion').textContent === SHOW,
        el('aboutVersion').textContent);
  check('关于页仓库链接已填', String(el('aboutRepo').href).indexOf('github.com') >= 0,
        el('aboutRepo').href);

  console.log('\n[2] 启动数据缺失时不能崩（降级显示 v--）');
  windowStub.__BOOT__ = {};
  sandbox.__BOOT__ = {};
  const utilBoot = sandbox.window.Util.BOOT;
  // Util.BOOT 是加载时读的，这里模拟"没有启动数据"的场景：临时改 Util.BOOT
  sandbox.window.Util.BOOT = {};
  Admin.showVersion();
  check('没有版本数据时显示 v--', el('appVersion').textContent === 'v--',
        el('appVersion').textContent);
  sandbox.window.Util.BOOT = utilBoot;    // 还原
  windowStub.__BOOT__ = BOOT;
  sandbox.__BOOT__ = BOOT;
  Admin.showVersion();

  console.log('\n[3] 渲染更新状态（当前版本 / 最新版本 / 状态）');
  currentPayload = {
    stage: 'idle', message: '', progress: 0, enabled: true,
    current: VERSION, current_display: SHOW,
    repo: 'L-web666/QQBotMerged', pending_restart: false, last_install: {},
    busy: false,
  };
  Admin.loadUpdateStatus().then(function () {
    const html = el('updateGrid').innerHTML;
    check('请求打到了 /api/admin/update/status',
          lastUrl.indexOf('/api/admin/update/status') >= 0, lastUrl);
    check('显示当前版本', html.indexOf(SHOW) >= 0, html.slice(0, 120));
    check('显示状态"就绪"', html.indexOf('就绪') >= 0, html.slice(0, 200));
    check('状态里带更新源', html.indexOf('L-web666') >= 0, html.slice(0, 200));

    console.log('\n[4] 更新进行中显示进度 + 每秒轮询');
    currentPayload = {
      stage: 'downloading', message: '正在下载 0.3/0.5MB…', progress: 60,
      enabled: true, current_display: SHOW, repo: 'L-web666/QQBotMerged',
      pending_restart: false, last_install: {}, busy: true,
    };
    return Admin.loadUpdateStatus();
  }).then(function () {
    const html = el('updateGrid').innerHTML;
    check('显示"正在下载"', html.indexOf('正在下载') >= 0, html.slice(0, 200));
    check('提示里带进度百分比', el('updateHint').innerHTML.indexOf('%') >= 0,
          el('updateHint').innerHTML.slice(0, 120));
    check('更新中时"立即更新"按钮隐藏或禁用',
          el('updateInstall').style.display === 'none' || el('updateInstall').disabled === true,
          JSON.stringify(el('updateInstall').style));

    console.log('\n[5] 装好待重启时明确提示');
    currentPayload = {
      stage: 'done', message: '已更新到 ' + SHOW_NEWER, progress: 100, enabled: true,
      current_display: SHOW, repo: 'L-web666/QQBotMerged',
      pending_restart: true,
      last_install: { to_version: NEWER, from_version: VERSION }, busy: false,
    };
    return Admin.loadUpdateStatus();
  }).then(function () {
    const hint = el('updateHint').innerHTML;
    check('提示需要重新启动', hint.indexOf('启动') >= 0, hint.slice(0, 160));
    check('提示里有备份位置', hint.indexOf('backup') >= 0, hint.slice(0, 200));
    const html = el('updateGrid').innerHTML;
    check('状态里标出待重启', html.indexOf('待重启') >= 0, html.slice(0, 250));

    console.log('\n[6] 自动更新被关闭时给出说明');
    currentPayload = {
      stage: 'idle', message: '', progress: 0, enabled: false,
      current_display: SHOW, repo: 'L-web666/QQBotMerged',
      pending_restart: false, last_install: {}, busy: false,
    };
    return Admin.loadUpdateStatus();
  }).then(function () {
    check('关闭时提示 update.enabled',
          el('updateHint').innerHTML.indexOf('update.enabled') >= 0,
          el('updateHint').innerHTML.slice(0, 150));
    check('关闭时"立即更新"按钮不显示',
          el('updateInstall').style.display === 'none', el('updateInstall').style.display);

    // ---- [7] 回归：点「检查更新」后必须显示最新版本 ----
    // 曾经的 bug：前端读 `updateLatest.version`，而后端返回的是 `latest` /
    // `latest_display`，于是「最新版本」永远显示"未检查"。
    console.log('\n[7] 点「检查更新」后「最新版本」不能还是"未检查"');
    currentPayload = {
      stage: 'idle', message: '', progress: 0, enabled: true,
      current_display: SHOW, repo: 'L-web666/QQBotMerged',
      pending_restart: false, last_install: {}, busy: false,
    };
    // 故意**只给 `latest`、不给 `latest_display`**：这样「最新版本」只能靠
    // `latestVersion()` 的回退逻辑画出来。如果有人把字段名又写成 `version`，
    // 这条断言就会红（当初的 bug 正是读了不存在的 `version` 字段）。
    checkPayload = {
      success: true, enabled: true,
      current: VERSION, current_display: SHOW,
      latest: NEWER,
      has_update: true, tag: 'v' + NEWER,
      published_at: '2026-11-07T00:00:00Z',
      url: 'https://github.com/L-web666/QQBotMerged/releases/tag/v' + NEWER,
      repo: 'L-web666/QQBotMerged', notes: '', error: '',
    };
    el('updateGrid').innerHTML = '';
    return new Promise(function (resolve) {
      Admin.checkUpdate();
      // checkUpdate 内部是两条异步链（check → loadUpdateStatus），等它们走完
      setTimeout(resolve, 120);
    });
  }).then(function () {
    const html = el('updateGrid').innerHTML;
    check('请求打到 /api/admin/update/check',
          requestUrls.some((u) => u.indexOf('/api/admin/update/check') >= 0),
          requestUrls.join(' | '));
    check('「最新版本」显示 ' + SHOW_NEWER + '（不再显示未检查）',
          html.indexOf(SHOW_NEWER) >= 0 && html.indexOf('未检查') < 0, html.slice(0, 240));
    check('提示里出现"发现新版本 ' + SHOW_NEWER + '"',
          el('updateHint').innerHTML.indexOf('发现新版本') >= 0 &&
          el('updateHint').innerHTML.indexOf(SHOW_NEWER) >= 0,
          el('updateHint').innerHTML.slice(0, 160));
    check('"立即更新"按钮出现',
          el('updateInstall').style.display !== 'none', el('updateInstall').style.display);

    // ---- [8] 刷新页面后，用服务端缓存把「最新版本」补回来 ----
    console.log('\n[8] 刷新页面后（前端内存清空）仍能显示最新版本');
    Admin.updateLatest = null;              // 模拟刷新后前端状态丢失
    el('updateGrid').innerHTML = '';
    currentPayload = {
      stage: 'idle', message: '', progress: 0, enabled: true,
      current_display: SHOW, repo: 'L-web666/QQBotMerged',
      pending_restart: false, last_install: {}, busy: false,
      last_check: { latest: NEWER, latest_display: SHOW_NEWER,
                    has_update: true, published_at: '2026-11-07', tag: 'v' + NEWER },
    };
    return Admin.loadUpdateStatus();
  }).then(function () {
    const html = el('updateGrid').innerHTML;
    check('刷新后「最新版本」仍显示 ' + SHOW_NEWER + '（用服务端缓存）',
          html.indexOf(SHOW_NEWER) >= 0 && html.indexOf('未检查') < 0, html.slice(0, 240));
    check('刷新后仍提示有可安装的新版本',
          el('updateInstall').style.display !== 'none', el('updateInstall').style.display);

    console.log('\n' + '='.repeat(66));
    console.log('  通过 ' + PASSED.length + ' 项，失败 ' + FAILED.length + ' 项');
    FAILED.forEach(function (item) { console.log('   [FAIL] ' + item[0] + '  ' + item[1]); });
    console.log('='.repeat(66));
    process.exit(FAILED.length ? 1 : 0);
  }).catch(function (error) {
    console.log('  [FAIL] 测试过程抛异常：' + (error && error.stack ? error.stack : error));
    process.exit(1);
  });
}

main();
