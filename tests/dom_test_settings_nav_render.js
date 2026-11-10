/* 真实渲染验证：用真实 spec 跑 render()，检查导航条目与卡片一一对应。 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.resolve(__dirname, '..');
const PASSED = [];
const FAILED = [];

function check(name, condition, detail) {
  if (condition) { PASSED.push(name); console.log('  [OK] ' + name); }
  else { FAILED.push([name, detail || '']); console.log('  [FAIL] ' + name + '  ' + (detail || '')); }
}

/* ---------- 能记录结构的 DOM 桩 ---------- */
function makeElement(tag) {
  const node = {
    tagName: (tag || 'div').toUpperCase(), id: '', className: '', value: '',
    textContent: '', _html: '', style: {}, dataset: {}, children: [],
    _listeners: {}, _classes: new Set(),
    classList: {
      add(c) { node._classes.add(c); }, remove(c) { node._classes.delete(c); },
      toggle(c, on) { if (on) node._classes.add(c); else node._classes.delete(c); },
      contains(c) { return node._classes.has(c); },
    },
    appendChild(c) { node.children.push(c); return c; },
    removeChild() {}, insertBefore(c) { node.children.push(c); return c; },
    insertAdjacentHTML(pos, html) { node._html += String(html); },
    setAttribute(k, v) { node.dataset[k] = String(v); },
    getAttribute(k) { return Object.prototype.hasOwnProperty.call(node.dataset, k) ? node.dataset[k] : null; },
    removeAttribute(k) { delete node.dataset[k]; },
    addEventListener(t, fn) { (node._listeners[t] = node._listeners[t] || []).push(fn); },
    removeEventListener() {},
    /* 关键：settings.js 的 renderNav() 靠 querySelectorAll('.cfg-section')
       收集卡片。桩必须真的从 innerHTML 里解析出这些卡片，否则导航永远是空的。 */
    querySelectorAll(sel) {
      if (sel === '.cfg-section') {
        const out = [];
        const re = /<div class="card cfg-section" data-section="([^"]*)" data-title="([^"]*)" data-icon="([^"]*)" data-perbot="([^"]*)">/g;
        let m;
        while ((m = re.exec(node._html)) !== null) {
          const card = makeElement('div');
          card.setAttribute('data-section', m[1]);
          card.setAttribute('data-title', m[2]);
          card.setAttribute('data-icon', m[3]);
          card.setAttribute('data-perbot', m[4]);
          out.push(card);
        }
        return out;
      }
      if (sel === '.field') return [];
      return [];
    },
    closest() { return null; }, matches() { return false; },
    scrollIntoView() {}, focus() {}, blur() {}, click() {},
    getBoundingClientRect() { return { top: 0, left: 0, width: 0, height: 0 }; },
    get offsetWidth() { return 100; },
  };
  Object.defineProperty(node, 'innerHTML', {
    get() { return node._html; }, set(v) { node._html = String(v); },
  });
  return node;
}

const elements = {};
function el(id) { return elements[id] || (elements[id] = makeElement('div')); }

// 解析 form.innerHTML 里生成的卡片（{key,title,icon,perbot}）
function parseCards(html) {
  const out = [];
  const re = /<div class="card cfg-section" data-section="([^"]*)" data-title="([^"]*)" data-icon="([^"]*)" data-perbot="([^"]*)">/g;
  let m;
  while ((m = re.exec(html)) !== null) {
    out.push({ key: m[1], title: m[2], icon: m[3], perBot: m[4] === '1' });
  }
  return out;
}

// 解析 navList.innerHTML 里生成的导航条目
function parseNav(html) {
  const out = [];
  const re = /data-goto="([^"]*)"[^>]*>\s*<span class="nav-ico">([^<]*)<\/span>\s*<span class="nav-text">([^<]*)<\/span>/g;
  let m;
  while ((m = re.exec(html)) !== null) {
    out.push({ key: m[1], icon: m[2], title: m[3] });
  }
  return out;
}

const documentStub = {
  readyState: 'complete', hidden: false, body: makeElement('body'),
  getElementById: el,
  createElement: (t) => makeElement(t),
  querySelector: () => null, querySelectorAll: () => [],
  addEventListener() {}, removeEventListener() {}, dispatchEvent() {},
};

const BOOT = { ui: {}, limits: {}, auth_required: true,
               version: '1.3.5', version_display: 'v1.3.5' };
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

function loadSpec() {
  const { execFileSync } = require('child_process');
  const tmp = path.join(ROOT, 'data', '_selftest', '_navr_' + process.pid + '.json');
  fs.mkdirSync(path.dirname(tmp), { recursive: true });
  const script = [
    'import json, sys',
    "sys.path.insert(0, r'" + ROOT + "')",
    'from core import config_schema as s',
    "with open(r'" + tmp + "', 'w', encoding='utf-8') as fh:",
    '    json.dump({"sections": s.sections_payload()}, fh, ensure_ascii=False)',
  ].join('\n');
  execFileSync('python', ['-c', script], {
    cwd: ROOT, stdio: 'ignore',
    env: Object.assign({}, process.env, { PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1' }),
  });
  const data = JSON.parse(fs.readFileSync(tmp, 'utf8'));
  try { fs.unlinkSync(tmp); } catch (e) {}
  return data;
}

const spec = loadSpec();
const Settings = sandbox.window.Settings;

function main() {
  console.log('='.repeat(66));
  console.log('  设置页左侧导航：真实渲染验证');
  console.log('='.repeat(66));

  // 构造后端返回的 spec（与 /api/admin/config 结构一致）
  const fields = {};
  for (const section of spec.sections) {
    for (const key of section.fields) {
      fields[key] = { type: 'str', label: key.split('.').pop(), hint: '' };
    }
  }
  Settings.spec = {
    sections: spec.sections,
    fields: fields,
    config: {}, defaults: {}, bot_config: {},
    path: 'config.json', active_bot: 'bot1',
    runtime_web: { url: 'http://127.0.0.1:8897', host: '127.0.0.1', port: 8897 },
  };
  el('settingsSearch').value = '';

  console.log('\n[1] render() 生成的卡片');
  Settings.render();
  const cards = parseCards(el('settingsForm').innerHTML);
  check('生成了卡片', cards.length > 0, String(cards.length));
  check('卡片数 = 后端分组数', cards.length === spec.sections.length,
        cards.length + ' vs ' + spec.sections.length);

  console.log('\n[2] 导航条目与卡片一一对应');
  const nav = parseNav(el('settingsNavList').innerHTML);
  check('生成了导航条目', nav.length > 0, String(nav.length));
  check('导航条目数 = 卡片数', nav.length === cards.length,
        nav.length + ' vs ' + cards.length);
  const cardKeys = cards.map((c) => c.key).join(',');
  const navKeys = nav.map((n) => n.key).join(',');
  check('导航顺序与卡片顺序完全一致', cardKeys === navKeys,
        '\n     卡片: ' + cardKeys + '\n     导航: ' + navKeys);

  console.log('\n[3] 导航显示了图标与标题');
  const byKey = {};
  for (const item of nav) byKey[item.key] = item;
  check('机器人账号分组有图标', !!byKey['bots'] && !!byKey['bots'].icon,
        JSON.stringify(byKey['bots']));
  check('机器人账号标题正确', byKey['bots'] && byKey['bots'].title === '机器人账号',
        byKey['bots'] && byKey['bots'].title);
  check('自动更新分组存在', !!byKey['update'], Object.keys(byKey).join(','));
  check('云同步分组存在', !!byKey['cloud'], '');

  console.log('\n[4] 卡片带上了导航需要的属性');
  check('卡片有 data-title', cards.every((c) => c.title), '');
  check('卡片有 data-icon', cards.every((c) => c.icon), '');
  check('卡片有 data-perbot',
        cards.every((c) => c.perBot === true || c.perBot === false), '');

  console.log('\n[5] 搜索过滤后导航同步收缩');
  el('settingsSearch').value = 'token';
  Settings.render();
  const filteredCards = parseCards(el('settingsForm').innerHTML);
  const filteredNav = parseNav(el('settingsNavList').innerHTML);
  check('过滤后卡片变少', filteredCards.length < cards.length,
        filteredCards.length + ' / ' + cards.length);
  check('过滤后导航与卡片仍一一对应',
        filteredCards.map((c) => c.key).join(',') === filteredNav.map((n) => n.key).join(','),
        '');

  console.log('\n[6] 没有匹配项时导航给出提示');
  el('settingsSearch').value = 'zzz-not-exist-zzz';
  Settings.render();
  check('显示无可配置项提示',
        el('settingsNavList').innerHTML.indexOf('settings-nav-empty') >= 0,
        el('settingsNavList').innerHTML.slice(0, 80));

  console.log('\n' + '='.repeat(66));
  console.log('  通过 ' + PASSED.length + ' 项，失败 ' + FAILED.length + ' 项');
  FAILED.forEach((it) => console.log('   [FAIL] ' + it[0] + '  ' + it[1]));
  console.log('='.repeat(66));
  process.exit(FAILED.length ? 1 : 0);
}

main();
