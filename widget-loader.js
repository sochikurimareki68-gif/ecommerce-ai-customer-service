/**
 * 优眠小助手 · 挂件加载器
 * ========================
 * 宿主页接入方式（一行 script）：
 *
 *   <script
 *     src="https://youmian-customer-service.onrender.com/widget-loader.js"
 *     data-widget-url="https://youmian-customer-service.onrender.com/?widget=1"
 *     data-primary-color="#2e5b4c"
 *     async
 *     defer></script>
 *
 * 可选属性（全部有默认值，不传即可）：
 *   data-widget-url  —— 聊天框 URL（必须带 ?widget=1）
 *   data-primary-color —— 气泡按钮主色（hex）
 *   data-bottom      —— 距底部距离（px，默认 24）
 *   data-right       —— 距右侧距离（px，默认 24）
 *   data-width       —— 展开后宽度（px，默认 380）
 *   data-height      —— 展开后高度（px，默认 600）
 *   data-title       —— 气泡 tooltip 文字
 *
 * 设计要点：
 *   · 单文件、零依赖、无全局污染（IIFE 包裹，仅挂 __YOUmianWidgetLoaded 标记）
 *   · iframe 隔离——宿主页 CSS/JS 不会污染聊天内容
 *   · 收起时销毁 iframe，不浪费内存
 *   · 移动端自适应（窄屏自动全屏展开）
 */
(function () {
  // 防重复注入
  if (window.__YOUmianWidgetLoaded) return;
  window.__YOUmianWidgetLoaded = true;

  // ===== 读取配置 =====
  var scriptTag = document.currentScript || document.querySelector('script[src*="widget-loader"]');
  var defaults = {
    widgetUrl:   'https://youmian-customer-service.onrender.com/?widget=1',
    primaryColor:'#2e5b4c',
    bottom:      24,
    right:       24,
    width:       380,
    height:      600,
    title:       '优眠小助手 · 在线咨询',
  };
  var cfg = {};
  if (scriptTag) {
    cfg.widgetUrl    = scriptTag.getAttribute('data-widget-url')    || defaults.widgetUrl;
    cfg.primaryColor = scriptTag.getAttribute('data-primary-color') || defaults.primaryColor;
    cfg.bottom       = parseInt(scriptTag.getAttribute('data-bottom'))  || defaults.bottom;
    cfg.right        = parseInt(scriptTag.getAttribute('data-right'))   || defaults.right;
    cfg.width        = parseInt(scriptTag.getAttribute('data-width'))   || defaults.width;
    cfg.height       = parseInt(scriptTag.getAttribute('data-height'))  || defaults.height;
    cfg.title        = scriptTag.getAttribute('data-title')         || defaults.title;
  } else {
    cfg = defaults;
  }

  // ===== 移动端自适应 =====
  var isMobile = window.matchMedia('(max-width: 480px)').matches;
  if (isMobile) {
    cfg.width  = Math.min(cfg.width, window.innerWidth - 16);
    cfg.height = window.innerHeight - 120;
    cfg.right  = 8;
    cfg.bottom = 16;
  }

  // ===== 注入样式 =====
  var style = document.createElement('style');
  style.textContent = [
    '.yew-widget-btn{',
    '  position:fixed;z-index:2147483647;',
    '  bottom:'+cfg.bottom+'px;right:'+cfg.right+'px;',
    '  width:56px;height:56px;border-radius:50%;',
    '  background:'+cfg.primaryColor+';',
    '  box-shadow:0 4px 16px rgba(0,0,0,.25);',
    '  cursor:pointer;border:none;',
    '  display:flex;align-items:center;justify-content:center;',
    '  transition:transform .15s ease, box-shadow .15s ease;',
    '}',
    '.yew-widget-btn:hover{transform:scale(1.06);box-shadow:0 6px 20px rgba(0,0,0,.32);}',
    '.yew-widget-btn svg{width:28px;height:28px;fill:#fff;}',
    '.yew-widget-frame{',
    '  position:fixed;z-index:2147483646;',
    '  bottom:'+(cfg.bottom+68)+'px;right:'+cfg.right+'px;',
    '  width:'+cfg.width+'px;height:'+cfg.height+'px;',
    '  border-radius:12px;overflow:hidden;',
    '  box-shadow:0 8px 32px rgba(0,0,0,.22);',
    '  background:#fff;display:none;',
    '}',
    '.yew-widget-frame iframe{width:100%;height:100%;border:0;display:block;}',
    '@media(max-width:480px){',
    '  .yew-widget-frame{',
    '    bottom:0;left:0;right:0;width:100%;height:100%;',
    '    border-radius:0;',
    '  }',
    '}'
  ].join('\n');
  document.head.appendChild(style);

  // ===== 气泡按钮（内嵌 SVG 客服图标）=====
  var btn = document.createElement('button');
  btn.className = 'yew-widget-btn';
  btn.title = cfg.title;
  btn.setAttribute('aria-label', cfg.title);
  btn.innerHTML = '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">' +
    '<path d="M20 2H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2h3l4 4 4-4h5a2 2 0 0 0 2-2V4a2 2 0 0 0-2-2zm-9 10a1 1 0 1 1 0-2 1 1 0 0 1 0 2zm4 0a1 1 0 1 1 0-2 1 1 0 0 1 0 2zm-8 0a1 1 0 1 1 0-2 1 1 0 0 1 0 2z"/>' +
    '</svg>';

  // ===== 弹窗容器 =====
  var frame = document.createElement('div');
  frame.className = 'yew-widget-frame';

  var open = false;
  function toggle() {
    open = !open;
    if (open) {
      frame.style.display = 'block';
      // 懒创建 iframe——每次展开新建，确保状态干净；收起时销毁省内存
      if (!frame.querySelector('iframe')) {
        var iframe = document.createElement('iframe');
        iframe.src = cfg.widgetUrl;
        iframe.title = '优眠小助手';
        iframe.setAttribute('allow', 'microphone; clipboard-read; clipboard-write');
        iframe.setAttribute('sandbox', 'allow-scripts allow-forms allow-same-origin allow-popups allow-modals allow-pointer-lock');
        iframe.setAttribute('referrerpolicy', 'no-referrer');
        frame.appendChild(iframe);
      }
    } else {
      frame.style.display = 'none';
      // 销毁 iframe 释放内存；下次展开重新创建
      frame.innerHTML = '';
    }
  }

  btn.addEventListener('click', toggle);

  document.body.appendChild(frame);
  document.body.appendChild(btn);
})();
