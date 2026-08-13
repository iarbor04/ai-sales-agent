/* Чат для сайта. Ставится одной строкой:
     <script src="https://ВАШ-АДРЕС/widget.js" async></script>

   Ничего не тянет со стороны: ни шрифтов, ни библиотек. Стили в теневом
   дереве, поэтому вёрстка сайта на виджет не влияет и наоборот. */
(function () {
  "use strict";
  if (window.__aiSalesWidget) return;
  window.__aiSalesWidget = true;

  var BASE = document.currentScript
    ? document.currentScript.src.replace(/\/widget\.js.*$/, "")
    : "";
  var KEY = "ai-sales-visitor";
  var token = null, lastId = 0, timer = null, opened = false, cfg = {};

  var host = document.createElement("div");
  host.style.cssText = "position:fixed;right:20px;bottom:20px;z-index:2147483000";
  var root = host.attachShadow ? host.attachShadow({ mode: "open" }) : host;

  root.innerHTML =
    '<style>' +
    ':host,*{box-sizing:border-box}' +
    '.b{width:58px;height:58px;border-radius:50%;border:none;cursor:pointer;' +
    'background:var(--c,#0a7c47);color:#fff;box-shadow:0 6px 20px rgba(0,0,0,.22);' +
    'display:grid;place-items:center;transition:transform .15s}' +
    '.b:hover{transform:scale(1.06)}' +
    '.b svg{width:26px;height:26px}' +
    '.p{position:absolute;right:0;bottom:72px;width:340px;max-width:calc(100vw - 40px);' +
    'height:480px;max-height:calc(100vh - 120px);background:#fff;border-radius:16px;' +
    'box-shadow:0 16px 48px rgba(16,24,40,.24);display:none;flex-direction:column;' +
    'overflow:hidden;font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;' +
    'color:#0d1117}' +
    '.p.on{display:flex}' +
    '.h{padding:14px 16px;background:var(--c,#0a7c47);color:#fff;font-weight:600;' +
    'display:flex;align-items:center;justify-content:space-between;gap:8px}' +
    '.h small{display:block;font-weight:400;opacity:.85;font-size:12px}' +
    '.x{background:none;border:none;color:#fff;font-size:20px;cursor:pointer;line-height:1;padding:0}' +
    '.m{flex:1;overflow-y:auto;padding:14px;background:#f5f7f9;display:flex;' +
    'flex-direction:column;gap:8px}' +
    '.i,.o{max-width:82%;padding:8px 12px;border-radius:14px;white-space:pre-wrap;' +
    'word-break:break-word}' +
    '.i{background:#fff;border:1px solid #e5e8ec;border-bottom-left-radius:4px;align-self:flex-start}' +
    '.o{background:var(--c,#0a7c47);color:#fff;border-bottom-right-radius:4px;align-self:flex-end}' +
    '.f{display:flex;gap:8px;padding:10px;border-top:1px solid #e5e8ec;background:#fff}' +
    '.f textarea{flex:1;resize:none;border:1px solid #e5e8ec;border-radius:20px;' +
    'padding:9px 13px;font:inherit;outline:none;max-height:90px}' +
    '.f textarea:focus{border-color:var(--c,#0a7c47)}' +
    '.f button{width:38px;height:38px;flex:0 0 38px;border-radius:50%;border:none;' +
    'background:var(--c,#0a7c47);color:#fff;cursor:pointer;display:grid;place-items:center}' +
    '.f svg{width:16px;height:16px}' +
    '</style>' +
    '<button class="b" aria-label="Чат">' +
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" ' +
    'stroke-linecap="round" stroke-linejoin="round">' +
    '<path d="M21 12a8 8 0 0 1-11.6 7.1L3 21l1.9-6.4A8 8 0 1 1 21 12z"/></svg></button>' +
    '<div class="p"><div class="h"><span><span class="t">Чат</span>' +
    '<small class="s">Обычно отвечаем сразу</small></span>' +
    '<button class="x" aria-label="Закрыть">&times;</button></div>' +
    '<div class="m"></div>' +
    '<form class="f"><textarea rows="1" placeholder="Ваш вопрос…"></textarea>' +
    '<button type="submit" aria-label="Отправить">' +
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
    'stroke-linecap="round" stroke-linejoin="round">' +
    '<path d="M22 2 11 13"/><path d="M22 2 15 22l-4-9-9-4 20-7z"/></svg>' +
    '</button></form></div>';

  var bubble = root.querySelector(".b"),
      panel = root.querySelector(".p"),
      list = root.querySelector(".m"),
      form = root.querySelector(".f"),
      box = root.querySelector("textarea");

  function api(path, body) {
    return fetch(BASE + "/api/widget/" + path, {
      method: body ? "POST" : "GET",
      headers: { "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : undefined
    }).then(function (r) { return r.json(); });
  }

  function add(text, mine) {
    if (!text) return;
    var el = document.createElement("div");
    el.className = mine ? "o" : "i";
    el.textContent = text;
    list.appendChild(el);
    list.scrollTop = list.scrollHeight;
  }

  function pull() {
    if (!token) return;
    api("poll?token=" + encodeURIComponent(token) + "&after=" + lastId)
      .then(function (data) {
        (data.messages || []).forEach(function (m) {
          lastId = Math.max(lastId, m.id);
          // своё сообщение уже нарисовано сразу при отправке
          if (m.direction === "out") add(m.text, false);
        });
      })
      .catch(function () {});
  }

  function open() {
    panel.classList.add("on");
    opened = true;
    box.focus();
    if (!timer) timer = setInterval(pull, 3000);
    if (!token) {
      api("start", {}).then(function (data) {
        token = data.token;
        cfg = data;
        localStorage.setItem(KEY, token);
        root.querySelector(".t").textContent = data.title || "Чат";
        if (data.color) host.style.setProperty("--c", data.color);
        if (data.greeting) add(data.greeting, false);
      });
    }
  }

  bubble.addEventListener("click", function () {
    opened ? (panel.classList.remove("on"), (opened = false)) : open();
  });
  root.querySelector(".x").addEventListener("click", function () {
    panel.classList.remove("on");
    opened = false;
  });

  box.addEventListener("input", function () {
    box.style.height = "auto";
    box.style.height = Math.min(box.scrollHeight, 90) + "px";
  });
  box.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      form.dispatchEvent(new Event("submit", { cancelable: true }));
    }
  });

  form.addEventListener("submit", function (e) {
    e.preventDefault();
    var text = box.value.trim();
    if (!text || !token) return;
    add(text, true);
    box.value = "";
    box.style.height = "auto";
    api("send", { token: token, text: text }).then(function () {
      setTimeout(pull, 900);
    });
  });

  try {
    var saved = localStorage.getItem(KEY);
    if (saved) token = saved;
  } catch (e) {}

  document.addEventListener("DOMContentLoaded", function () {
    document.body.appendChild(host);
  });
  if (document.readyState !== "loading") document.body.appendChild(host);
})();
