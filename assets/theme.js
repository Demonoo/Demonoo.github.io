/* DMEDIA · 明暗主题切换（light / dark）
 * 约定：<html data-theme="light|dark">，页面 <head> 内已有一小段内联脚本先按
 * localStorage / 系统偏好把 data-theme 写死，避免首屏闪烁（FOUC）。
 * 本脚本只负责：
 *   1) 给 #theme-toggle 按钮绑定点击切换
 *   2) 切换后把偏好写入 localStorage（跨页面共享）
 *   3) 派发 themechange 事件，供依赖 JS 重绘的图（如 trends 页气泡图）刷新颜色
 */
(function () {
  'use strict';
  var KEY = 'dmedia-theme';

  function current() {
    return document.documentElement.getAttribute('data-theme') === 'dark' ? 'dark' : 'light';
  }
  function apply(t) {
    document.documentElement.setAttribute('data-theme', t === 'dark' ? 'dark' : 'light');
  }
  function toggle() {
    var next = current() === 'dark' ? 'light' : 'dark';
    apply(next);
    try { localStorage.setItem(KEY, next); } catch (e) { /* 隐私模式等场景忽略 */ }
    document.dispatchEvent(new CustomEvent('themechange', { detail: { theme: next } }));
  }
  function ready(fn) {
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', fn);
    } else {
      fn();
    }
  }
  ready(function () {
    var btn = document.getElementById('theme-toggle');
    if (btn) btn.addEventListener('click', toggle);
  });

  window.DMEDIA_THEME = { current: current, apply: apply, toggle: toggle };
})();
