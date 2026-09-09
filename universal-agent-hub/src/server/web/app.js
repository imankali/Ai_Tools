/* Universal Agent Hub — منطق رابط کاربری (بدون build، بدون وابستگی).
 *
 * ساختار:
 *   Hub      : لایه‌ی ارتباط (REST + WebSocket، reconnect، session)
 *   ui*      : رندر هر نما (chat / tools / keys / settings)
 *   i18n     : انگلیسی/فارسی با تنظیم dir خودکار
 *
 * نکات ایمنی: کلید API هرگز در این مرورگر/اپ ذخیره نمی‌شود؛ فقط توکن دسترسی
 * سرور در localStorage می‌ماند. همه‌ی متن‌های مدل قبل از درج در DOM escape می‌شوند.
 */

/* ------------------------------------------------------------------ ابزارهای کوچک */
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
const esc = (value) =>
  String(value ?? "").replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[ch]);
const fmtTime = (ts) => {
  if (!ts) return "";
  const date = new Date(Number(ts) * 1000);
  return Number.isNaN(date.getTime()) ? "" : date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
};
const ms = (value) => (Number.isFinite(Number(value)) ? `${Number(value) < 1000 ? Math.round(value) + " ms" : (value / 1000).toFixed(1) + " s"}` : "");

function toast(message, kind = "") {
  const node = $("#toast");
  node.textContent = message;
  node.className = `toast ${kind}`;
  node.hidden = false;
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => (node.hidden = true), kind === "err" ? 5200 : 2600);
}

/* ------------------------------------------------------------------ پل بومی (Android / iOS) */

/**
 * ارسال رویداد به پوسته‌ی native (اپ اندروید/iOS) — در مرورگر بی‌اثر است.
 *
 * اندروید: `window.HubNative.notify(json)` (JavascriptInterface)
 * iOS:     `window.webkit.messageHandlers.HubNative.postMessage(json)`
 *
 * فقط برای اعلان/لرزش استفاده می‌شود؛ هیچ قدرتی برای اجرای کد روی دستگاه نمی‌دهد.
 */
function nativeEvent(title, body, urgent) {
  const payload = JSON.stringify({ title: title, body: String(body || "").slice(0, 400), urgent: !!urgent });
  const bridge = (typeof window !== "undefined" && (window.HubNative || (window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.HubNative))) || null;
  if (!bridge) return false;
  try {
    if (typeof bridge.notify === "function") bridge.notify(payload);
    else if (typeof bridge.postMessage === "function") bridge.postMessage(payload);
    if (urgent && typeof bridge.vibrate === "function") bridge.vibrate("0,60,80,40");
    return true;
  } catch (_) {
    return false; // پل بومی اختیاری است؛ هرگز UI را نمی‌شکند
  }
}

/**
 * مصرف پارامترهای لینک بومی/QR: `?server=https://host:8765&token=…&session=…`
 *
 * اپ‌های native و صفحه‌ی «اسکن QR» همین‌ها را می‌دهند. بعد از خواندن، آدرس با
 * `history.replaceState` پاک می‌شود تا توکن در تاریخچه‌ی مرورگر نماند.
 */
function hydrateFromLocation() {
  const params = new URLSearchParams(location.search);
  if (!params.has("server") && !params.has("token") && !params.has("session")) return;
  const server = (params.get("server") || "").trim().replace(/\/+$/, "");
  if (server) localStorage.setItem("hub.url", server);
  if (params.has("token")) localStorage.setItem("hub.token", (params.get("token") || "").trim());
  const session = (params.get("session") || "").trim();
  if (session) localStorage.setItem("hub.session", session);
  history.replaceState(null, "", location.pathname + location.hash);
}

/* ------------------------------------------------------------------ ترجمه */
const I18N = {
  en: {
    app_name: "Agent Hub",
    connecting: "connecting…",
    connected: "live",
    offline: "offline",
    reconnecting: "reconnecting…",
    lang_title: "زبان / English",
    theme_title: "theme",
    ask_placeholder: "Ask the agent to do something on this machine…",
    send_title: "send (enter), shift+enter = new line",
    stop: "Stop",
    running: "running…",
    reset_context: "Reset",
    search_tools: "search tools",
    reload: "Reload",
    arguments: "Arguments (JSON)",
    run_tool: "Run tool",
    keys_title: "Model API keys",
    keys_help: "Keys live on the machine running the agent (never in this app). The app only ever sees a masked form.",
    import_env: "Import from .env",
    key_name: "Name",
    key_profile: "Agent profile",
    key_value: "API key",
    key_value_help: "Leave empty to keep the stored key.",
    key_base: "Base URL",
    key_model: "Model",
    key_fallbacks: "Fallback models (comma separated)",
    activate_now: "Activate now",
    save_key: "Save key",
    delete_key: "Delete",
    connection: "Connection",
    server_url: "Server URL",
    access_token: "Access token",
    reconnect: "Reconnect",
    forget: "Forget",
    connection_help: "Only needed when you open this page from another device. The token is stored in this browser only.",
    agent_settings: "Agent",
    temperature: "Temperature",
    max_iterations: "Max iterations",
    model_override: "Model override",
    system_note: "Extra system note",
    confirmation_mode: "Ask before sensitive actions",
    parallel_tools: "Run independent tools in parallel",
    apply: "Apply",
    new_session: "New session",
    allowed_tools: "Tools allowed for this session",
    safety_title: "Safety policy",
    server_info: "Server",
    tab_chat: "Chat",
    tab_tools: "Tools",
    tab_reports: "Reports",
    tab_keys: "Keys",
    tab_settings: "Settings",
    approve: "Allow",
    deny: "Deny",
    approval_title: "Needs your approval",
    send_failed: "request failed",
    reports_window: "Window",
    reports_refresh: "Refresh",
    reports_note: "Add a note",
    reports_next: "What it plans next",
    reports_tools: "Busiest tools",
    reports_plans: "Open plans in memory",
    reports_empty: "Nothing recorded yet — run the agent and come back.",
    reports_tokens: "tokens",
    reports_denied: "denied",
    reports_blocked: "blocked",
    reports_saved: "saved to memory",
    reports_note_prompt: "Note for the agent's long-term memory:",
    reports_runs: "runs",
    reports_warnings: "Warnings",
    reports_no_warnings: "No warnings.",
    need_key: "No API key configured — open the Keys tab and add one.",
    empty_tools: "no tool",
    use_all: "all tools from profile",
    active: "active",
    copied: "copied",
  },
  fa: {
    app_name: "هاب ایجنت",
    connecting: "در حال اتصال…",
    connected: "زنده",
    offline: "آفلاین",
    reconnecting: "تلاش برای اتصال دوباره…",
    lang_title: "Language / فارسی",
    theme_title: "پوسته",
    ask_placeholder: "از ایجنت بخواه کاری روی این دستگاه انجام دهد…",
    send_title: "ارسال (enter)، shift+enter برای خط جدید",
    stop: "توقف",
    running: "در حال اجرا…",
    reset_context: "پاک کردن",
    search_tools: "جست‌وجوی ابزار",
    reload: "بازخوانی",
    arguments: "پارامترها (JSON)",
    run_tool: "اجرای ابزار",
    keys_title: "کلیدهای API مدل",
    keys_help: "کلیدها فقط روی همان ماشین می‌مانند و هرگز به این اپ فرستاده نمی‌شوند؛ شما تنها شکل ماسک‌شده را می‌بینید.",
    import_env: "برداشت از .env",
    key_name: "نام",
    key_profile: "پروفایل ایجنت",
    key_value: "کلید API",
    key_value_help: "اگر خالی بگذارید، کلید ذخیره‌شده می‌ماند.",
    key_base: "Base URL",
    key_model: "مدل",
    key_fallbacks: "مدل‌های جایگزین (با کاما)",
    activate_now: "همین حالا فعال شود",
    save_key: "ذخیره کلید",
    delete_key: "حذف",
    connection: "اتصال",
    server_url: "آدرس سرور",
    access_token: "توکن دسترسی",
    reconnect: "اتصال دوباره",
    forget: "فراموش کردن",
    connection_help: "فقط وقتی لازم است که این صفحه را از دستگاه دیگری باز کنید. توکن فقط در همین مرورگر می‌ماند.",
    agent_settings: "ایجنت",
    temperature: "دما (temperature)",
    max_iterations: "حداکثر دورها",
    model_override: "تغییر مدل",
    system_note: "یادداشت سیستمی اضافی",
    confirmation_mode: "قبل از کارهای حساس بپرسد",
    parallel_tools: "اجرای موازی ابزارهای مستقل",
    apply: "اعمال",
    new_session: "نشست تازه",
    allowed_tools: "ابزارهای مجاز این نشست",
    safety_title: "سیاست ایمنی",
    server_info: "سرور",
    tab_chat: "گفت‌وگو",
    tab_tools: "ابزارها",
    tab_reports: "گزارش‌ها",
    tab_keys: "کلیدها",
    tab_settings: "تنظیمات",
    reports_window: "بازه",
    reports_refresh: "به‌روز کردن",
    reports_note: "یادداشت",
    reports_next: "قدم بعدی",
    reports_tools: "پرتکرارترین ابزارها",
    reports_plans: "برنامه‌های باز در حافظه",
    reports_empty: "هنوز چیزی ثبت نشده — ایجنت را اجرا کنید و برگردید.",
    reports_tokens: "توکن",
    reports_denied: "ردشده",
    reports_blocked: "بلوکه",
    reports_saved: "در حافظه ذخیره شد",
    reports_note_prompt: "یادداشت برای حافظه‌ی بلندمدت ایجنت:",
    reports_runs: "اجرا",
    reports_warnings: "هشدارها",
    reports_no_warnings: "هشداری نیست.",
    approve: "اجازه بده",
    deny: "رد کن",
    approval_title: "نیاز به تأیید شما",
    send_failed: "ارسال ناموفق بود",
    need_key: "کلید API تنظیم نشده — از تب «کلیدها» اضافه کنید.",
    empty_tools: "بدون ابزار",
    use_all: "همه ابزارهای پروفایل",
    active: "فعال",
    copied: "کپی شد",
  },
};

const storedLang = localStorage.getItem("hub.lang");
const state = {
  lang: storedLang === "fa" || storedLang === "en" ? storedLang : (navigator.language || "").toLowerCase().startsWith("fa") ? "fa" : "en",
  dict: {},
  session: localStorage.getItem("hub.session") || "",
  profiles: [],
  tools: [],
  categories: [],
  status: null,
  keys: [],
  activeKey: "",
  pending: new Map(),
  busy: false,
  ws: null,
  wsAttempts: 0,
  history: [],
  streaming: null,
  filters: { q: "", category: "" },
  selectedTool: "",
  promptedToken: false,
  queued: null,
};

function t(key) {
  return (state.dict[key] ?? I18N.en[key] ?? key).toString();
}

function applyLang() {
  state.dict = I18N[state.lang] || I18N.en;
  document.documentElement.lang = state.lang;
  document.documentElement.dir = state.lang === "fa" ? "rtl" : "ltr";
  $("#lang-btn").textContent = state.lang === "fa" ? "EN" : "FA";
  $$("[data-i18n]").forEach((node) => (node.textContent = t(node.dataset.i18n)));
  $$("[data-i18n-placeholder]").forEach((node) => (node.placeholder = t(node.dataset.i18nPlaceholder)));
  $$("[data-i18n-title]").forEach((node) => (node.title = t(node.dataset.i18nTitle)));
}

/* ------------------------------------------------------------------ لایه‌ی ارتباط */
const Hub = {
  get base() {
    const saved = (localStorage.getItem("hub.url") || "").trim().replace(/\/+$/, "");
    return saved || "";
  },
  get token() {
    return (localStorage.getItem("hub.token") || "").trim();
  },
  headers(extra = {}) {
    const headers = { "Content-Type": "application/json", ...extra };
    if (this.token) headers["Authorization"] = `Bearer ${this.token}`;
    if (state.session) headers["X-Agent-Session"] = state.session;
    return headers;
  },
  async request(path, { method = "GET", body, retry = true } = {}) {
    if (!this.base && location.protocol === "file:") throw new Error("open the app from the server URL");
    const response = await fetch(`${this.base}${path}`, { method, headers: this.headers(), body: body === undefined ? undefined : JSON.stringify(body) });
    const text = await response.text();
    let data = null;
    try {
      data = text ? JSON.parse(text) : null;
    } catch {
      data = { raw: text };
    }
    if (response.status === 401 && retry && !state.promptedToken) {
      const token = window.prompt(this.base ? "Access token for this server:" : "Access token:");
      if (token) {
        localStorage.setItem("hub.token", token.trim());
        state.promptedToken = true;
        return this.request(path, { method, body, retry: false });
      }
    }
    if (!response.ok) {
      const error = new Error((data && (data.error || data.message)) || `HTTP ${response.status}`);
      error.code = data && data.error_code;
      error.status = response.status;
      error.payload = data;
      throw error;
    }
    return data;
  },
  /* ------------------------------------------------------------ WebSocket */
  connect() {
    if (this._closed) return;
    const scheme = location.protocol === "https:" ? "wss" : "ws";
    const origin = this.base ? this.base.replace(/^http/, "ws") : `${scheme}://${location.host}`;
    const params = new URLSearchParams();
    if (this.token) params.set("token", this.token);
    if (state.session) params.set("session", state.session);
    const url = `${origin}/ws?${params.toString()}`;
    setConn("connecting");
    let socket;
    try {
      socket = new WebSocket(url);
    } catch (error) {
      setConn("offline");
      scheduleReconnect();
      return;
    }
    state.ws = socket;
    socket.onopen = () => {
      state.wsAttempts = 0;
      setConn("connected");
      this.send("subscribe", {});
      if (state.queued) {
        const queued = state.queued;
        state.queued = null;
        this.run(queued);
      }
    };
    socket.onclose = () => {
      setConn("offline");
      scheduleReconnect();
    };
    socket.onerror = () => socket.close();
    socket.onmessage = (event) => {
      let message;
      try {
        message = JSON.parse(event.data);
      } catch {
        return;
      }
      Hub.onMessage(message);
    };
  },
  send(kind, payload = {}) {
    const socket = state.ws;
    if (socket && socket.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify({ kind, payload }));
      return true;
    }
    return false;
  },
  run(prompt) {
    if (this.send("run", { prompt, session_id: state.session })) {
      startRun(prompt);
      return true;
    }
    state.queued = prompt;
    return false;
  },
  onMessage(message) {
    const kind = message.kind;
    const payload = message.payload || {};
    if (kind === "hello") {
      if (payload.session && payload.session.session_id) setSession(payload.session.session_id);
      state.status = state.status || {};
      renderHello(payload);
      (payload.recent_events || []).forEach((event) => uiEvent(event, true));
      (payload.pending_approvals || []).forEach(uiApproval);
      return;
    }
    if (kind === "result") return uiResult(payload);
    // پیام‌های event/approval_request تخت‌اند (کل message همان ساختار /events است)
    if (kind === "event") return uiEvent(message);
    if (kind === "approval_request") return uiApproval(message);
    if (kind === "accepted") return setRunStatus(payload.prompt ? `running: ${String(payload.prompt).slice(0, 60)}` : t("running"));
    if (kind === "error") {
      finishRun();
      const error = new Error(payload.error || "server error");
      if (payload.error_code === "missing_api_key") {
        toast(t("need_key"), "err");
        showView("keys");
      } else {
        toast(`${t("send_failed")}: ${error.message}`, "err");
      }
      uiError(error);
      return;
    }
    if (kind === "ack") {
      if (payload.session) renderSession(payload.session);
      if (payload.cancelled === false) toast("nothing is running");
      return;
    }
  },
};

function scheduleReconnect() {
  state.wsAttempts = Math.min(state.wsAttempts + 1, 6);
  const delay = Math.min(15000, 700 * 2 ** state.wsAttempts);
  setConn("reconnecting");
  clearTimeout(Hub._timer);
  Hub._timer = setTimeout(() => Hub.connect(), delay);
}

function setConn(kind) {
  const node = $("#conn-state");
  node.className = `conn ${kind === "connected" ? "on" : kind === "reconnecting" || kind === "connecting" ? "warn" : "off"}`;
  node.textContent = t(kind === "connected" ? "connected" : kind === "reconnecting" ? "reconnecting" : kind === "connecting" ? "connecting" : "offline");
  $("#conn-badge").textContent = kind;
}

/* ------------------------------------------------------------------ چت */
function messagesEl() {
  return $("#messages");
}

function scrollDown() {
  const box = messagesEl();
  box.scrollTop = box.scrollHeight;
}

function addMessage(role, html, opts = {}) {
  const node = document.createElement("div");
  node.className = `msg ${role}${opts.cls ? ` ${opts.cls}` : ""}`;
  const label = document.createElement("span");
  label.className = "msg-role";
  label.textContent = opts.label || role;
  const body = document.createElement("div");
  body.className = "msg-body";
  if (opts.raw) body.textContent = html;
  else body.innerHTML = markdown(html);
  node.append(label, body);
  messagesEl().append(node);
  scrollDown();
  return node;
}

/** رندر ساده‌ی Markdown (اول escape، بعد تبدیل) — از تزریق HTML جلوگیری می‌کند. */
function markdown(source) {
  const text = String(source ?? "");
  const blocks = [];
  let working = text.replace(/```([^\n]*)\n?([\s\S]*?)```/g, (_all, lang, code) => {
    blocks.push(`<pre><code data-lang="${esc(lang.trim())}">${esc(code.replace(/\n$/, ""))}</code></pre>`);
    return `\u0000${blocks.length - 1}\u0000`;
  });
  working = esc(working)
    .replace(/`([^`\n]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
    .replace(/^(#{1,3})\s+(.*)$/gm, (_m, hashes, title) => `<h${hashes.length + 1}>${title}</h${hashes.length + 1}>`)
    .replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>')
    .replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g, '$1<a href="$2" target="_blank" rel="noopener noreferrer">$2</a>');
  return working.replace(/\u0000(\d+)\u0000/g, (_m, index) => blocks[Number(index)]);
}

function startRun(prompt) {
  state.busy = true;
  state.streaming = addMessage("assistant", "", { label: "agent" });
  state.streaming.classList.add("typing");
  state.streaming.querySelector(".msg-body").innerHTML = '<span class="muted">…</span>';
  $("#send-btn").disabled = true;
  setRunStatus(t("running"));
  $("#run-status").hidden = false;
  addMessage("user", prompt, { raw: true, label: "you" });
}

function setRunStatus(text) {
  $("#run-status-text").textContent = text;
}

function finishRun() {
  state.busy = false;
  $("#send-btn").disabled = false;
  $("#run-status").hidden = true;
  if (state.streaming) state.streaming.classList.remove("typing");
  state.streaming = null;
}

function uiEvent(event, replay = false) {
  const box = messagesEl();
  const kind = String(event.event || event.kind || "event");
  const payload = event.payload || {};
  const isTool = kind.startsWith("tool.") || kind.startsWith("safety.");
  const node = document.createElement("details");
  node.className = "event";
  const tone = kind.endsWith("failed") || kind.includes("blocked") ? "evt-bad" : kind.endsWith("completed") || kind.includes("approved") ? "evt-ok" : kind.includes("requested") ? "evt-warn" : "";
  const summary = document.createElement("summary");
  const tool = payload.tool || payload.name || "";
  summary.innerHTML = `<span class="evt-name ${tone}">${esc(kind)}${tool ? ` · ${esc(tool)}` : ""}</span><span class="muted">${esc(ms(payload.duration_ms))} ${esc(fmtTime(event.at))}</span>`;
  const pre = document.createElement("pre");
  pre.textContent = JSON.stringify(payload, null, 1).slice(0, 4000);
  node.append(summary, pre);
  if (!isTool && replay) return;
  if (isTool || kind.includes("error") || kind.includes("fallback")) {
    box.append(node);
    scrollDown();
  }
  const chip = $("#tool-chip");
  if (kind === "tool.completed") chip.textContent = `tools +${payload.tool || ""}`;
}

function uiError(error) {
  const node = addMessage("error", String(error && error.message ? error.message : error), { raw: true, label: "error" });
  if (error && error.code) {
    const meta = document.createElement("div");
    meta.className = "msg-meta";
    meta.innerHTML = `<span class="chip bad">${esc(error.code)}</span>`;
    node.append(meta);
  }
}

function uiResult(result) {
  finishRun();
  if (state.streaming) state.streaming.remove();
  const text = result.text || (result.error ? "" : "(no answer)");
  const node = addMessage(result.ok === false ? "error" : "assistant", text || result.error || "(empty)", { raw: true, label: "agent" });
  const meta = document.createElement("div");
  meta.className = "msg-meta";
  const bits = [];
  if (result.iterations) bits.push(`${result.iterations} iter`);
  if (result.duration_ms) bits.push(ms(result.duration_ms));
  if (result.usage && result.usage.total_tokens) bits.push(`${result.usage.total_tokens} tok`);
  if (result.model) bits.push(result.model);
  if (result.session_id) bits.push(result.session_id);
  meta.innerHTML = bits.map((bit) => `<span class="chip">${esc(bit)}</span>`).join("");
  node.append(meta);
  const calls = result.tool_calls || [];
  if (calls.length) {
    const list = document.createElement("div");
    list.className = "msg-meta";
    list.innerHTML = calls
      .map((call) => `<span class="chip ${call.succeeded === false ? "bad" : "ok"}">${esc(call.tool || call.name || "tool")}</span>`)
      .join("");
    node.append(list);
  }
  loadHistory().catch(() => {});
  refreshStatus().catch(() => {});
  // پاسخ تمام شد ولی صفحه باز نبود → اعلان بومی
  if (document.visibilityState === "hidden") {
    nativeEvent("Agent Hub", result.ok === false ? result.error || "failed" : (result.text || "").slice(0, 160), false);
  }
}

function uiApproval(request) {
  if (!request || !request.request_id || state.pending.has(request.request_id)) return;
  state.pending.set(request.request_id, request);
  const card = document.createElement("div");
  card.className = "approval";
  card.dataset.request = request.request_id;
  const detail = request.details && Object.keys(request.details).length ? JSON.stringify(request.details, null, 1) : "";
  card.innerHTML = `
    <h4>⚠️ ${esc(t("approval_title"))} <span class="risk ${esc(request.risk || "medium")}">${esc(request.risk || "medium")}</span></h4>
    <p><strong>${esc(request.tool || "")}</strong> ${esc(request.action || "")}</p>
    <p>${esc(request.summary || "")}</p>
    ${detail ? `<pre>${esc(detail)}</pre>` : ""}
    <div class="row">
      <button class="btn tiny" data-answer="deny">${esc(t("deny"))}</button>
      <button class="btn tiny primary" data-answer="allow">${esc(t("approve"))}</button>
    </div>`;
  card.querySelectorAll("[data-answer]").forEach((button) => {
    button.addEventListener("click", () => answerApproval(request.request_id, button.dataset.answer === "allow"));
  });
  $("#approvals").append(card);
  scrollDown();
  if (navigator.vibrate) navigator.vibrate(60);
  // اگر گوشی در پس‌زمینه است، کاربر با اعلان بومی و لرزش خبر می‌شود
  nativeEvent(t("approval_title") + " · " + (request.tool || ""), request.summary || "", true);
}

async function answerApproval(requestId, approved) {
  state.pending.delete(requestId);
  const card = $(`.approval[data-request="${requestId}"]`);
  if (card) card.remove();
  const sent = Hub.send("approve", { request_id: requestId, approved });
  if (!sent) {
    try {
      await Hub.request(`/api/sessions/${encodeURIComponent(state.session)}/approvals`, { method: "POST", body: { request_id: requestId, approved } });
    } catch (error) {
      toast(error.message, "err");
    }
  }
}

/* ------------------------------------------------------------------ نماها */
function showView(name) {
  $$(".view").forEach((view) => view.classList.toggle("is-active", view.id === `view-${name}`));
  $$(".tab").forEach((tab) => tab.classList.toggle("is-active", tab.dataset.view === name));
  if (name === "tools") renderTools();
  if (name === "reports") loadReports().catch((error) => toast(error.message, "err"));
  if (name === "keys") loadKeys().catch((error) => toast(error.message, "err"));
  if (name === "settings") refreshStatus().catch(() => {});
  if (name === "chat") scrollDown();
}

function setSession(id) {
  state.session = id || "";
  if (id) localStorage.setItem("hub.session", id);
  else localStorage.removeItem("hub.session");
  renderSession({ session_id: id });
}

function renderSession(session) {
  if (!session) return;
  $("#session-chip").textContent = `session ${(session.session_id || "—").slice(-6)}`;
  if (session.model) $("#model-chip").textContent = session.model;
  if (session.active_tools) $("#tool-chip").textContent = `tools ${session.active_tools.length}`;
  if (session.profile) $("#profile-select").value = session.profile;
  const select = $("#session-select");
  if (session.session_id && ![...select.options].some((option) => option.value === session.session_id)) {
    select.append(new Option(session.session_id.slice(-6), session.session_id));
  }
  select.value = session.session_id || select.value;
  if (session.busy !== undefined) $("#run-status").hidden = !session.busy;
}

function renderHello(payload) {
  renderSession(payload.session);
  $("#ready-badge").textContent = payload.ready ? "ready" : "no key";
  $("#ready-badge").className = `chip ${payload.ready ? "ok" : "warn"}`;
  if (!payload.ready) toast(t("need_key"), "err");
}

async function refreshStatus() {
  const status = await Hub.request("/api/status");
  state.status = status;
  renderSession(status.session);
  $("#ready-badge").textContent = status.ready ? "ready" : "no key";
  $("#ready-badge").className = `chip ${status.ready ? "ok" : "warn"}`;
  $("#server-info").textContent = JSON.stringify({ version: status.version, config: status.config, keystore: status.keystore, capabilities: status.capabilities }, null, 2);
  renderSafety(status.safety);
  renderProfiles(status.profiles || []);
  renderSessionList(status.sessions || []);
  const config = status.config || {};
  $("#set-temperature").value = config.temperature ?? "";
  $("#set-iterations").value = config.max_tool_iterations ?? "";
  $("#set-model").value = config.model_name ?? "";
  $("#set-confirmation").checked = config.enable_confirmation !== false;
  $("#set-parallel").checked = config.parallel_tool_calls === true;
  $("#set-note").value = (status.session && status.session.system_note) || "";
  if (status.tools === undefined) loadTools().catch(() => {});
}

async function loadHistory() {
  if (!state.session) return;
  const data = await Hub.request(`/api/sessions/${encodeURIComponent(state.session)}/history?limit=14`);
  const box = messagesEl();
  box.innerHTML = "";
  (data.messages || []).forEach((message) => {
    if (message.role === "user") addMessage("user", message.content, { raw: true, label: "you" });
    else if (message.role === "assistant") addMessage("assistant", message.content || "(tool call)", { raw: !message.content, label: "agent" });
    else if (message.role === "tool") uiEvent({ event: "tool.completed", payload: { tool: message.name, result: String(message.content).slice(0, 400) } });
  });
}

function renderProfiles(names) {
  const selects = [$("#profile-select"), $("#key-profile")];
  selects.forEach((select) => {
    const current = select.value;
    select.innerHTML = "";
    (names || []).forEach((name) => select.append(new Option(name, name)));
    if (!names.includes("generalist")) select.prepend(new Option("generalist", "generalist"));
    select.value = current && names.includes(current) ? current : names.includes("generalist") ? "generalist" : names[0] || "";
  });
}

function renderSessionList(sessions) {
  const select = $("#session-select");
  const keep = select.value;
  select.innerHTML = "";
  select.append(new Option("＋ new", ""));
  (sessions || []).forEach((item) => select.append(new Option(`${(item.session_id || "").slice(-6)} · ${item.profile || ""}${item.busy ? " ●" : ""}`, item.session_id)));
  select.value = keep && sessions.some((item) => item.session_id === keep) ? keep : state.session || "";
}

function renderSafety(safety) {
  const box = $("#safety-summary");
  if (!safety) {
    box.innerHTML = '<div><b>—</b><span>open a session to load the policy</span></div>';
    return;
  }
  const card = (label, value) => `<div><b>${esc(String(value))}</b><span>${esc(label)}</span></div>`;
  box.innerHTML =
    card(safety.policy || "policy", safety.allow_shell ? "shell allowed" : "shell blocked") +
    card("confirm", safety.confirmation_enabled ? "on" : "off") +
    card("channels", safety.has_confirmation_channel ? "ready" : "none") +
    card("protected dirs", (safety.protected_system_dirs || []).length) +
    card("block rules", (safety.critical_rules || []).length) +
    card("confirm rules", (safety.confirm_rules || []).length) +
    `<div class="list"><span>allowed directories</span><br />${esc((safety.allowed_directories || []).join(", ") || "unrestricted")}</div>`;
}

/* ------------------------------------------------------------------ ابزارها */
async function loadTools() {
  const data = await Hub.request("/api/tools");
  state.tools = data.tools || [];
  state.categories = [...new Set(state.tools.map((tool) => tool.category || "other"))].sort();
  renderTools();
}

function renderTools() {
  const list = $("#tool-list");
  const cats = $("#tool-categories");
  cats.innerHTML = "";
  const allChip = document.createElement("button");
  allChip.textContent = `all (${state.tools.length})`;
  allChip.className = state.filters.category ? "" : "is-active";
  allChip.addEventListener("click", () => {
    state.filters.category = "";
    renderTools();
  });
  cats.append(allChip);
  state.categories.forEach((category) => {
    const chip = document.createElement("button");
    chip.textContent = `${category} (${state.tools.filter((tool) => (tool.category || "other") === category).length})`;
    chip.className = state.filters.category === category ? "is-active" : "";
    chip.addEventListener("click", () => {
      state.filters.category = category;
      renderTools();
    });
    cats.append(chip);
  });

  const needle = state.filters.q.toLowerCase();
  const filtered = state.tools.filter(
    (tool) =>
      (!state.filters.category || (tool.category || "other") === state.filters.category) &&
      (!needle || `${tool.name} ${tool.description}`.toLowerCase().includes(needle))
  );
  list.innerHTML = "";
  if (!filtered.length) {
    list.innerHTML = `<li class="muted small" style="padding:12px">${esc(t("empty_tools"))}</li>`;
    return;
  }
  filtered.forEach((tool) => {
    const item = document.createElement("li");
    item.className = `tool-item${state.selectedTool === tool.name ? " is-active" : ""}`;
    item.innerHTML = `
      <div class="grow">
        <div class="tool-name">${esc(tool.name)}</div>
        <div class="tool-desc">${esc(tool.description || "")}</div>
        <div class="badges">
          <span class="chip">${esc(tool.category || "other")}</span>
          <span class="risk ${esc(tool.risk_level || "low")}">${esc(tool.risk_level || "low")}</span>
          ${tool.requires_confirmation ? '<span class="chip warn">asks</span>' : ""}
        </div>
      </div>`;
    item.addEventListener("click", () => openTool(tool.name));
    list.append(item);
  });
  renderPicker();
}

async function openTool(name) {
  state.selectedTool = name;
  renderTools();
  const panel = $("#tool-detail");
  panel.classList.remove("hidden");
  $("#tool-detail-name").textContent = name;
  $("#tool-detail-desc").textContent = "";
  $("#tool-result").textContent = "";
  try {
    const data = await Hub.request(`/api/tools/${encodeURIComponent(name)}`);
    const schema = (data.schema && data.schema.function) || data.schema || {};
    $("#tool-detail-desc").textContent = schema.description || "";
    $("#tool-detail-schema").textContent = JSON.stringify(schema.parameters || {}, null, 2);
    const sample = {};
    Object.entries((schema.parameters && schema.parameters.properties) || {}).forEach(([key, spec]) => {
      sample[key] = spec.default !== undefined ? spec.default : spec.type === "integer" || spec.type === "number" ? 0 : spec.type === "boolean" ? false : spec.type === "array" ? [] : "";
    });
    $("#tool-args").value = JSON.stringify(sample, null, 2);
    panel.dataset.name = name;
    panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
  } catch (error) {
    toast(error.message, "err");
  }
}

function renderPicker() {
  const box = $("#tool-picker");
  if (!box || !state.tools.length) return;
  const selected = state.status && state.status.session && state.status.session.tools ? new Set(state.status.session.tools) : null;
  box.innerHTML = "";
  state.tools.forEach((tool) => {
    const label = document.createElement("label");
    const input = document.createElement("input");
    input.type = "checkbox";
    input.value = tool.name;
    input.checked = !selected || selected.has(tool.name);
    input.addEventListener("change", () => {
      const names = $$("#tool-picker input:checked").map((node) => node.value);
      label.dataset.touched = "1";
      $("#tools-count").textContent = `${names.length}/${state.tools.length}`;
      $("#tools-count").dataset.pending = "1";
      state.tools.forEach((other) => {
        const node = $(`#tool-picker input[value="${other.name}"]`);
        if (node) node.closest("label").classList.toggle("muted", !node.checked);
      });
      box.dataset.names = names.join(",");
    });
    label.append(input, Object.assign(document.createElement("span"), { className: "mono", textContent: tool.name }));
    box.append(label);
  });
  $("#tools-count").textContent = selected ? `${selected.size}/${state.tools.length}` : t("use_all");
}

/* ------------------------------------------------------------------ کلیدها */
/* ------------------------------------------------------------ گزارش‌ها */
async function loadReports() {
  const picker = $("#report-days");
  const days = picker ? picker.value : "7";
  const data = await Hub.request(`/api/reports?days=${encodeURIComponent(days)}`);
  renderReports(data || {});
}

function renderReports(data) {
  const report = data.report || {};
  const runs = report.runs || {};
  const safety = report.safety || {};
  const memory = report.memory || {};
  const cells = [
    [t("reports_runs"), runs.total || 0],
    ["ok", runs.ok || 0],
    ["failed", runs.failed || 0],
    [t("reports_tokens"), runs.tokens || 0],
    ["time", runs.duration_human || "0s"],
    [t("reports_denied"), safety.denied || 0],
    [t("reports_blocked"), safety.blocked || 0],
    ["memory", memory.records || 0],
  ];
  const stats = $("#report-stats");
  if (stats) {
    stats.innerHTML = cells
      .map(([label, value]) => `<div class="stat"><span>${esc(String(label))}</span><strong>${esc(String(value))}</strong></div>`)
      .join("");
  }
  const list = (id, items, empty) => {
    const node = $(id);
    if (!node) return;
    const rows = (items || []).map((item) => `<li dir="auto">${esc(String(item))}</li>`);
    node.innerHTML = rows.length ? rows.join("") : `<li class="muted small">${esc(empty)}</li>`;
  };
  list("#report-next", report.next_actions, t("reports_empty"));
  list("#report-warnings", report.warnings, t("reports_no_warnings"));
  list(
    "#report-plans",
    (report.plans || []).map((plan) => `${plan.content} (${plan.age_days}d)`),
    t("reports_empty")
  );
  const tools = $("#report-tools");
  if (tools) {
    const rows = report.tools || [];
    tools.innerHTML = rows.length
      ? rows
          .map(
            (row) => `<div class="report-row"><span class="mono">${esc(row.tool)}</span><span>${esc(String(row.calls || 0))} calls</span><span>${esc(String(row.failures || 0))} fail</span><span>${esc(String(row.denied || 0))} denied</span><span>${esc(String(row.blocked || 0))} blocked</span></div>`
          )
          .join("")
      : `<div class="muted small">${esc(t("reports_empty"))}</div>`;
  }
  const text = $("#report-text");
  if (text) text.textContent = data.text || "";
}

async function addMemoryNote() {
  const content = window.prompt(t("reports_note_prompt"));
  if (!content || !content.trim()) return;
  const state = $("#reports-note-state");
  try {
    await Hub.request("/api/memory", { method: "POST", body: { content: content.trim(), kind: "note" } });
    if (state) state.textContent = t("reports_saved");
    loadReports().catch(() => {});
  } catch (error) {
    toast(error.message, "err");
  }
}

async function loadKeys() {
  const data = await Hub.request("/api/keys");
  state.keys = data.profiles || [];
  state.activeKey = (data.status && data.status.active) || "";
  renderKeys();
}

function renderKeys() {
  const list = $("#key-list");
  list.innerHTML = "";
  if (!state.keys.length) {
    list.innerHTML = `<li class="muted small" style="padding:10px">${esc(t("need_key"))}</li>`;
    return;
  }
  state.keys.forEach((record) => {
    const item = document.createElement("li");
    item.className = "key-item";
    item.innerHTML = `
      <div class="grow">
        <div class="tool-name">${esc(record.name)} ${record.name === state.activeKey ? `<span class="chip ok">${esc(t("active"))}</span>` : ""}</div>
        <div class="tool-desc mono">${esc(record.masked_key || "—")}</div>
        <div class="badges">
          ${record.model ? `<span class="chip">${esc(record.model)}</span>` : ""}
          ${record.base_url ? `<span class="chip">${esc(String(record.base_url).replace(/^https?:\/\//, "").slice(0, 26))}</span>` : ""}
          ${record.profile ? `<span class="chip">${esc(record.profile)}</span>` : ""}
          ${(record.fallbacks || []).length ? `<span class="chip">${record.fallbacks.length} fallbacks</span>` : ""}
        </div>
      </div>
      <div class="row">
        <button class="btn tiny" data-act="use">use</button>
        <button class="btn tiny ghost" data-act="edit">edit</button>
      </div>`;
    item.querySelector('[data-act="use"]').addEventListener("click", async () => {
      try {
        await Hub.request(`/api/keys/${encodeURIComponent(record.name)}/activate`, { method: "POST" });
        toast(`${t("active")}: ${record.name}`, "ok");
        await loadKeys();
        await refreshStatus();
      } catch (error) {
        toast(error.message, "err");
      }
    });
    item.querySelector('[data-act="edit"]').addEventListener("click", () => {
      $("#key-name").value = record.name;
      $("#key-value").value = "";
      $("#key-base").value = record.base_url || "";
      $("#key-model").value = record.model || "";
      $("#key-fallbacks").value = (record.fallbacks || []).join(", ");
      $("#key-profile").value = record.profile || $("#key-profile").value;
      $("#key-activate").checked = true;
    });
    list.append(item);
  });
}

/* ------------------------------------------------------------------ راه‌اندازی */
async function loadHistorySafe() {
  try {
    await loadHistory();
  } catch {
    /* session خالی است */
  }
}

function bindUI() {
  $$(".tab").forEach((tab) => tab.addEventListener("click", () => showView(tab.dataset.view)));

  $("#lang-btn").addEventListener("click", () => {
    state.lang = state.lang === "fa" ? "en" : "fa";
    localStorage.setItem("hub.lang", state.lang);
    applyLang();
  });
  $("#theme-btn").addEventListener("click", () => {
    const next = document.documentElement.dataset.theme === "light" ? "dark" : "light";
    document.documentElement.dataset.theme = next;
    localStorage.setItem("hub.theme", next);
    const meta = $('meta[name="theme-color"]');
    if (meta) meta.content = next === "light" ? "#f4f6fb" : "#0b1020";
  });

  const prompt = $("#prompt");
  prompt.addEventListener("input", () => {
    prompt.style.height = "auto";
    prompt.style.height = `${Math.min(prompt.scrollHeight, window.innerHeight * 0.4)}px`;
  });
  prompt.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      $("#composer").requestSubmit();
    }
  });
  $("#composer").addEventListener("submit", (event) => {
    event.preventDefault();
    const text = prompt.value.trim();
    if (!text || state.busy) return;
    prompt.value = "";
    prompt.style.height = "auto";
    if (!Hub.run(text)) toast(t("connecting"), "err");
  });
  $("#stop-btn").addEventListener("click", () => {
    Hub.send("cancel", {});
    setRunStatus("stopping…");
  });
  $("#reset-btn").addEventListener("click", async () => {
    try {
      await Hub.request(`/api/sessions/${encodeURIComponent(state.session)}/reset`, { method: "POST" });
      messagesEl().innerHTML = "";
      toast("context cleared", "ok");
    } catch (error) {
      toast(error.message, "err");
    }
  });

  $("#profile-select").addEventListener("change", async (event) => {
    try {
      await Hub.request(`/api/sessions/${encodeURIComponent(state.session)}`, { method: "PATCH", body: { profile: event.target.value } });
      toast(`profile: ${event.target.value}`, "ok");
      await refreshStatus();
    } catch (error) {
      toast(error.message, "err");
    }
  });
  $("#session-select").addEventListener("change", (event) => {
    const id = event.target.value;
    if (!id) return Hub.connect();
    setSession(id);
    Hub.send("subscribe", {});
    loadHistorySafe();
  });

  $("#tool-search").addEventListener("input", (event) => {
    state.filters.q = event.target.value;
    renderTools();
  });
  $("#tools-reload").addEventListener("click", () => loadTools().then(() => toast("tools reloaded", "ok")));
  const days = $("#report-days");
  if (days) days.addEventListener("change", () => loadReports().catch((error) => toast(error.message, "err")));
  const reloadReports = $("#reports-reload");
  if (reloadReports) reloadReports.addEventListener("click", () => loadReports().catch((error) => toast(error.message, "err")));
  const note = $("#reports-note");
  if (note) note.addEventListener("click", () => addMemoryNote());
  $("#tool-detail-close").addEventListener("click", () => $("#tool-detail").classList.add("hidden"));
  $("#tool-run").addEventListener("click", async () => {
    const name = $("#tool-detail").dataset.name;
    let args = {};
    try {
      args = JSON.parse($("#tool-args").value || "{}");
    } catch (error) {
      toast(`bad JSON: ${error.message}`, "err");
      return;
    }
    $("#tool-run-note").textContent = "running…";
    $("#tool-result").textContent = "";
    try {
      const result = await Hub.request(`/api/tools/${encodeURIComponent(name)}/invoke`, { method: "POST", body: { tool: name, arguments: args, session_id: state.session } });
      $("#tool-result").textContent = JSON.stringify(result, null, 2).slice(0, 20000);
      $("#tool-run-note").textContent = result.success ? `ok · ${ms(result.duration_ms)}` : `failed · ${result.error_code || ""}`;
    } catch (error) {
      $("#tool-run-note").textContent = "failed";
      $("#tool-result").textContent = JSON.stringify(error.payload || { error: error.message }, null, 2);
    }
  });

  $("#key-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const body = {
      name: $("#key-name").value.trim(),
      api_key: $("#key-value").value.trim() || undefined,
      base_url: $("#key-base").value.trim() || undefined,
      model: $("#key-model").value.trim() || undefined,
      fallbacks: $("#key-fallbacks").value.split(",").map((item) => item.trim()).filter(Boolean),
      profile: $("#key-profile").value || undefined,
      activate: $("#key-activate").checked,
    };
    try {
      await Hub.request("/api/keys", { method: "POST", body });
      $("#key-value").value = "";
      toast("saved", "ok");
      await loadKeys();
      await refreshStatus();
    } catch (error) {
      toast(error.message, "err");
    }
  });
  $("#key-delete").addEventListener("click", async () => {
    const name = $("#key-name").value.trim();
    if (!name || !window.confirm(`delete key profile '${name}'?`)) return;
    try {
      await Hub.request(`/api/keys/${encodeURIComponent(name)}`, { method: "DELETE" });
      toast("deleted", "ok");
      await loadKeys();
    } catch (error) {
      toast(error.message, "err");
    }
  });
  $("#keys-import").addEventListener("click", async () => {
    try {
      await Hub.request("/api/keys/import-env", { method: "POST" });
      toast("imported", "ok");
      await loadKeys();
      await refreshStatus();
    } catch (error) {
      toast(error.message, "err");
    }
  });

  $("#connect-btn").addEventListener("click", () => {
    localStorage.setItem("hub.url", $("#server-url").value.trim().replace(/\/+$/, ""));
    localStorage.setItem("hub.token", $("#access-token").value.trim());
    location.reload();
  });
  $("#forget-btn").addEventListener("click", () => {
    localStorage.removeItem("hub.url");
    localStorage.removeItem("hub.token");
    location.reload();
  });
  $("#settings-save").addEventListener("click", async () => {
    const names = $("#tool-picker").dataset.names ? $("#tool-picker").dataset.names.split(",").filter(Boolean) : null;
    const body = {
      temperature: $("#set-temperature").value === "" ? null : Number($("#set-temperature").value),
      max_tool_iterations: $("#set-iterations").value === "" ? null : Number($("#set-iterations").value),
      model_name: $("#set-model").value.trim() || null,
      system_note: $("#set-note").value.trim(),
      enable_confirmation: $("#set-confirmation").checked,
      parallel_tool_calls: $("#set-parallel").checked,
    };
    if (names && state.tools.length && names.length < state.tools.length) body.tools = names;
    try {
      const result = await Hub.request(`/api/sessions/${encodeURIComponent(state.session)}`, { method: "PATCH", body });
      toast(`applied: ${Object.keys(result.changed || {}).join(", ") || "—"}`, "ok");
      delete $("#tool-picker").dataset.names;
      await refreshStatus();
    } catch (error) {
      toast(error.message, "err");
    }
  });
  $("#new-session").addEventListener("click", async () => {
    const data = await Hub.request("/api/sessions", { method: "POST", body: {} });
    setSession(data.session_id);
    Hub.send("subscribe", {});
    messagesEl().innerHTML = "";
    toast(`session ${String(data.session_id).slice(-6)}`, "ok");
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") $("#tool-detail").classList.add("hidden");
    if ((event.ctrlKey || event.metaKey) && event.key === "Enter") $("#composer").requestSubmit();
  });
  window.addEventListener("online", () => Hub.connect());
  window.addEventListener("focus", () => {
    if (!state.ws || state.ws.readyState !== WebSocket.OPEN) Hub.connect();
  });
}

async function boot() {
  hydrateFromLocation(); // لینک بومی/QR زودتر از هر درخواست خوانده می‌شود
  document.documentElement.dataset.theme = localStorage.getItem("hub.theme") || "dark";
  applyLang();
  bindUI();
  $("#server-url").value = Hub.base || "";
  $("#access-token").value = Hub.token || "";
  showView(localStorage.getItem("hub.view") || "chat");
  $$(".tab").forEach((tab) => tab.addEventListener("click", () => localStorage.setItem("hub.view", tab.dataset.view)));
  if ("serviceWorker" in navigator && location.protocol !== "file:") {
    navigator.serviceWorker.register("sw.js").catch(() => {});
  }
  try {
    await refreshStatus();
    await loadTools();
    await loadHistorySafe();
  } catch (error) {
    toast(error.message, "err");
  }
  if (!state.session) {
    try {
      const created = await Hub.request("/api/sessions", { method: "POST", body: {} });
      setSession(created.session_id);
    } catch {
      /* سرور در دسترس نیست؛ WS خودش retry می‌کند */
    }
  }
  Hub.connect();
}

boot().catch((error) => toast(error.message, "err"));
