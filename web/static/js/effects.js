/* ==========================================================================
   视觉动效：按钮涟漪、会话列表错峰入场、图片渐显、平滑滚动、主题与动画强度
   ========================================================================== */
(function (global) {
  'use strict';
  var U = global.Util;

  /* ---------------- 主题与动画强度 ---------------- */
  function applyUiPrefs() {
    var ui = (U.BOOT.ui || {});
    document.body.setAttribute('data-theme', ui.theme || 'auto');
    document.body.setAttribute('data-anim', ui.animation || 'full');
    document.body.setAttribute('data-compact', ui.compact ? '1' : '0');
  }

  function cycleTheme() {
    var order = ['auto', 'light', 'dark'];
    var current = document.body.getAttribute('data-theme') || 'auto';
    var next = order[(order.indexOf(current) + 1) % order.length];
    document.body.setAttribute('data-theme', next);
    try { localStorage.setItem('qbm-theme', next); } catch (e) { /* 忽略隐私模式限制 */ }
    U.toast('主题：' + ({ auto: '跟随系统', light: '浅色', dark: '深色' })[next], 'ok', 1800);
  }

  function restoreTheme() {
    try {
      var saved = localStorage.getItem('qbm-theme');
      if (saved) document.body.setAttribute('data-theme', saved);
    } catch (e) { /* 忽略 */ }
  }

  /* ---------------- 涟漪反馈 ---------------- */
  function attachRipple(root) {
    (root || document).addEventListener('pointerdown', function (event) {
      var target = event.target.closest('.btn, .icon-btn, .nav-item, .tool-btn, .ghost-btn');
      if (!target || document.body.getAttribute('data-anim') === 'off') return;
      var rect = target.getBoundingClientRect();
      var ripple = document.createElement('span');
      ripple.className = 'ripple';
      ripple.style.left = (event.clientX - rect.left) + 'px';
      ripple.style.top = (event.clientY - rect.top) + 'px';
      target.appendChild(ripple);
      setTimeout(function () { if (ripple.parentNode) ripple.parentNode.removeChild(ripple); }, 560);
    }, { passive: true });
  }

  /* ---------------- 列表错峰 ---------------- */
  function stagger(container, selector) {
    if (!container) return;
    var items = container.querySelectorAll(selector || '.conv-item');
    for (var i = 0; i < items.length; i++) {
      items[i].style.setProperty('--i', Math.min(i, 12));
    }
  }

  /* ---------------- 图片渐显 ---------------- */
  function fadeImages(root) {
    if (!root) return;
    var images = root.querySelectorAll('img:not(.loaded)');
    for (var i = 0; i < images.length; i++) {
      (function (img) {
        if (img.complete && img.naturalWidth) {
          img.classList.add('loaded');
        } else {
          img.addEventListener('load', function () { img.classList.add('loaded'); });
          img.addEventListener('error', function () { img.classList.add('loaded'); });
        }
      })(images[i]);
    }
  }

  /* ---------------- 平滑滚动 ---------------- */
  function scrollToBottom(node) {
    if (!node) return;
    try {
      node.scrollTo({ top: node.scrollHeight, behavior: 'smooth' });
    } catch (e) {
      node.scrollTop = node.scrollHeight;
    }
  }

  function nearBottom(node, threshold) {
    if (!node) return true;
    return node.scrollHeight - node.scrollTop - node.clientHeight < (threshold || 120);
  }

  /* ---------------- 本地存储（会话/页面记忆） ---------------- */
  function remember(key, value) {
    try { localStorage.setItem('qbm-' + key, value); } catch (e) { /* 忽略 */ }
  }
  function recall(key) {
    try { return localStorage.getItem('qbm-' + key); } catch (e) { return null; }
  }

  global.Effects = {
    applyUiPrefs: applyUiPrefs,
    cycleTheme: cycleTheme,
    restoreTheme: restoreTheme,
    attachRipple: attachRipple,
    stagger: stagger,
    fadeImages: fadeImages,
    scrollToBottom: scrollToBottom,
    nearBottom: nearBottom,
    remember: remember,
    recall: recall
  };
  // 防止整个对象被别人替换掉（对象内部字段照旧可改，见 util.js 的 protectGlobal）
  if (typeof global.__qbmProtect === 'function') global.__qbmProtect('Effects', global.Effects);
})(window);
