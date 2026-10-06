/* ==========================================================================
   消息渲染细节测试（Node + 最小 DOM 桩，无需浏览器）

   覆盖用户反馈过的两个具体问题：
   1. 收到图片/表情包时**重复显示两张**（同一张图来自附件、image_url、表情图片三个来源）；
   2. 发送文件时出现**空消息**。

   运行：node tests/dom_test_messages.js
   ========================================================================== */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.dirname(__dirname);

function makeElement(id) {
  const node = {
    id, _innerHTML: '', textContent: '', value: '', className: '', children: [], files: [],
    style: { setProperty() {}, display: '' },
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    setAttribute() {}, getAttribute() { return null; }, addEventListener() {},
    appendChild(c) { this.children.push(c); return c; }, replaceChild() {}, removeChild() {},
    insertAdjacentHTML() {}, closest() { return null; }, querySelector() { return null; },
    querySelectorAll() { return []; }, focus() {}, scrollTo() {},
    getBoundingClientRect() { return { left: 0, top: 0, width: 10, height: 10 }; },
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
};
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
for (const file of ['util.js', 'effects.js', 'chat.js']) {
  vm.runInContext(fs.readFileSync(path.join(ROOT, 'web', 'static', 'js', file), 'utf8'),
                  sandbox, { filename: file });
}
// 每次都重新读取源码（避免 node 的模块缓存让我们测到旧版本）
delete require.cache[__filename];

const Chat = sandbox.window.Chat;

const results = [];
function check(name, ok, detail) {
  results.push(ok);
  console.log(`  ${ok ? '[OK]' : '[FAIL]'} ${name}${ok ? '' : '  ' + (detail || '')}`);
}

console.log('='.repeat(66));
console.log('  消息渲染测试（图片去重 / 文件消息不为空）');
console.log('='.repeat(66));

// ---------------------------------------------------------------- 图片去重
// 真实情况：同一张图既在 attachments（远端 + 本地留存各一份），又是表情标记里的图
const sameImage = {
  direction: 'in', type: 'private', username: '用户',
  content: '<faceType=4 faceId="0" ext="xxx">',
  content_display: '',
  attachments: [
    { url: 'https://multimedia.nt.qq.com.cn/download?appid=1&fileid=FILE_A&rkey=abc',
      local_url: '/media/pic_A.png', content_type: 'image/png', file_name: 'pic_A.png' },
  ],
  image_url: '/media/pic_A.png',
  content_images: ['https://multimedia.nt.qq.com.cn/download?appid=1&fileid=FILE_A&rkey=zzz'],
  msg_id: 'M1',
};
const keys = Chat.imageKeys(sameImage);
check('同一张图的三种来源被合并成一张', keys.length === 1, JSON.stringify(keys));
check('优先使用本地留存地址', keys[0] === '/media/pic_A.png', JSON.stringify(keys));

const html = Chat.messageHtmlForTest(sameImage, {});
const imgCount = (html.match(/<img /g) || []).length;
check('气泡里只渲染一张图', imgCount === 1, `img=${imgCount}`);

// 两张不同的图必须都保留
const twoImages = {
  direction: 'in', type: 'private', content: '', content_display: '',
  attachments: [
    { url: 'https://x.qq.com/download?fileid=FILE_1', local_url: '/media/a.png', content_type: 'image/png' },
    { url: 'https://x.qq.com/download?fileid=FILE_2', local_url: '/media/b.png', content_type: 'image/jpeg' },
  ],
  image_url: '', content_images: [], msg_id: 'M2',
};
check('两张不同的图都保留', Chat.imageKeys(twoImages).length === 2,
      JSON.stringify(Chat.imageKeys(twoImages)));

// 只有远端地址、没有本地留存时也要能显示（并按 fileid 去重不同的 rkey）
const remoteOnly = {
  direction: 'in', type: 'private', content: '', content_display: '',
  attachments: [
    { url: 'https://x.qq.com/download?fileid=FILE_9&rkey=r1', content_type: 'image/png' },
  ],
  image_url: 'https://x.qq.com/download?fileid=FILE_9&rkey=r2',
  content_images: [], msg_id: 'M3',
};
check('只有远端地址时按 fileid 去重', Chat.imageKeys(remoteOnly).length === 1,
      JSON.stringify(Chat.imageKeys(remoteOnly)));

// ---------------------------------------------------------------- 文件消息
const fileMessage = {
  direction: 'out', type: 'private', content: '📎 report.zip', content_display: '📎 report.zip',
  attachments: [
    { url: '', local_url: '/media/report.zip', file_name: 'report.zip', content_type: '' },
  ],
  image_url: '', content_images: [], msg_id: 'M4',
};
const fileHtml = Chat.messageHtmlForTest(fileMessage, {});
check('发送文件的消息不再是空的', fileHtml.indexOf('report.zip') >= 0, fileHtml.slice(0, 160));
check('文件以可下载链接呈现', fileHtml.indexOf('download') >= 0, fileHtml.slice(0, 200));
check('文件不会被误当成图片渲染', (fileHtml.match(/<img /g) || []).length === 0, '');

// 图片 + 文字：两者都要显示
const imageWithText = {
  direction: 'out', type: 'private', content: '看这张', content_display: '看这张',
  attachments: [], image_url: '/media/shot.png', content_images: [], msg_id: 'M5',
};
const mixedHtml = Chat.messageHtmlForTest(imageWithText, {});
check('图片消息同时显示图片与文字',
      mixedHtml.indexOf('<img ') >= 0 && mixedHtml.indexOf('看这张') >= 0, mixedHtml.slice(0, 160));

// 空消息（既无文字也无图片/附件）也不应崩
const emptyMessage = { direction: 'in', type: 'private', content: '', content_display: '',
                       attachments: [], image_url: '', content_images: [], msg_id: 'M6' };
const emptyHtml = Chat.messageHtmlForTest(emptyMessage, {});
check('真正的空消息会给出占位提示而不是白屏',
      emptyHtml.indexOf('空消息') >= 0, emptyHtml.slice(0, 160));

// 后端曾把"非图片文件"的本地地址写进 image_url，页面于是显示"图片加载失败"
const fileWithBadImageUrl = {
  direction: 'out', type: 'private', content: '📎 msedge.exe', content_display: '📎 msedge.exe',
  attachments: [{ local_url: '/media/msedge.exe', file_name: 'msedge.exe', content_type: '' }],
  image_url: '/media/msedge.exe',           // 就是被写坏的那种数据
  content_images: [], msg_id: 'M7',
};
const badHtml = Chat.messageHtmlForTest(fileWithBadImageUrl, {});
check('image_url 指向文件时不会被渲染成图片', (badHtml.match(/<img /g) || []).length === 0,
      badHtml.slice(0, 200));
check('这个文件仍然以可下载链接呈现', badHtml.indexOf('msedge.exe') >= 0 &&
      badHtml.indexOf('download') >= 0, badHtml.slice(0, 220));
check('imageKeys 不会把 .exe 当成图片',
      Chat.imageKeys(fileWithBadImageUrl).length === 0,
      JSON.stringify(Chat.imageKeys(fileWithBadImageUrl)));

// .py 同理（用户实际发过 gui_v1.6.0.py）
const pyFile = {
  direction: 'out', type: 'private', content: '📎 gui_v1.6.0.py', content_display: '📎 gui_v1.6.0.py',
  attachments: [{ local_url: '/media/gui_v1.6.0.py', file_name: 'gui_v1.6.0.py' }],
  image_url: '/media/gui_v1.6.0.py', content_images: [], msg_id: 'M8',
};
check('.py 文件也不会被当成图片',
      (Chat.messageHtmlForTest(pyFile, {}).match(/<img /g) || []).length === 0,
      Chat.messageHtmlForTest(pyFile, {}).slice(0, 200));

// 没有扩展名的远端图片链接仍要能显示（QQ 的临时链接）
const noExtImage = {
  direction: 'in', type: 'private', content: '', content_display: '',
  attachments: [], image_url: 'https://multimedia.nt.qq.com.cn/download?fileid=Z1',
  content_images: [], msg_id: 'M9',
};
check('没有扩展名的图片链接仍然按图片显示',
      Chat.imageKeys(noExtImage).length === 1, JSON.stringify(Chat.imageKeys(noExtImage)));

// ---------------------------------------------------------------- @ 标记渲染（只读展示）
const atMessage = {
  direction: 'out', type: 'group',
  content: '<qqbot-at-user id="MEMBER_1" /> 早上好 <qqbot-at-everyone />',
  content_display: '', attachments: [], image_url: '', content_images: [], msg_id: 'M10',
};
const atHtml = Chat.messageHtmlForTest(atMessage, {});
check('@某人 标记渲染成可读的 @标签（不显示原始标记）',
      atHtml.indexOf('class="mention"') >= 0 && atHtml.indexOf('qqbot-at-user') < 0,
      atHtml.slice(0, 220));
check('@全体成员 标记渲染成 @全体成员',
      atHtml.indexOf('@全体成员') >= 0, atHtml.slice(0, 220));

const legacyAt = {
  direction: 'in', type: 'group', content: '<@MEMBER_2> 在吗 <@all>',
  content_display: '', attachments: [], image_url: '', content_images: [], msg_id: 'M11',
};
const legacyHtml = Chat.messageHtmlForTest(legacyAt, {});
check('旧的 <@openid> 也渲染成 @标签',
      legacyHtml.indexOf('class="mention"') >= 0 && legacyHtml.indexOf('&lt;@') < 0,
      legacyHtml.slice(0, 220));
check('旧的 <@all> 渲染成 @全体成员',
      legacyHtml.indexOf('@全体成员') >= 0, legacyHtml.slice(0, 220));

// 点击图片消息时不应该再有"转发图片到其他会话"，也不该再出现 @ 发送入口
const fsSource = fs.readFileSync(path.join(ROOT, 'web', 'static', 'js', 'chat.js'), 'utf8');
check('消息操作框里没有"转发图片到其他会话"',
      fsSource.indexOf('转发图片到其他会话') < 0 && fsSource.indexOf('forwardImage') < 0, '');
check('已移除群 @ 提及发送入口（mentionPicker / insertMention）',
      fsSource.indexOf('mentionPicker') < 0 && fsSource.indexOf('insertMention') < 0, '');

console.log('');
console.log('='.repeat(66));
const failed = results.filter((ok) => !ok).length;
if (failed) {
  console.log(`  失败 ${failed} 项 / 共 ${results.length} 项`);
  process.exit(1);
}
console.log(`  全部通过（${results.length} 项）`);
process.exit(0);
