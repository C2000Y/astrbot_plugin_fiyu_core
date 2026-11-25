const $ = (id) => document.getElementById(id);
const page = $("page");
const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;
const state = {
  tab: "overview",
  config: null,
  draft: null,
  dirty: false,
  invalidJson: {},
  editMode: "form",
  configSearch: "",
  configCategory: "",
  options: {},
  sessions: [],
  filters: {},
  expandedUsers: new Set(),
  moduleTabs: {},
  epoch: 0,
};
const i18n = {
  core: "核心事实",
  profile: "人物记忆",
  experience: "重要/普通经历",
  ordinary: "日常琐事",
  temporary: "临时事项",
  habit: "习惯印象",
  active: "活跃",
  archived: "存档",
  expired: "已失效",
  vivid: "清晰",
  clear: "清楚",
  fuzzy: "模糊",
  trace: "仅剩印象",
  pending: "待审核",
  approved: "已批准",
  rejected: "已拒绝",
  expression: "表达方式",
  slang: "黑话",
  off: "停用",
  observe: "观察模式",
  decision: "读空气",
  reply: "聊天回复",
  memory: "记忆学习",
  style: "语言学习",
  vision: "视觉理解",
  embedding: "向量检索",
  research: "自主学习",
  submitted: "已提交",
  failed: "失败",
  unknown: "结果未确认",
  drafting: "草稿整理中",
  sending: "发送中",
  completed: "已完成",
  skipped: "跳过",
  interrupted: "已中断",
  cancelled: "已取消",
  budget: "预算暂停",
  rss_first: "优先 RSS，API 备用",
  api_first: "优先 API，RSS 备用",
  rss_only: "仅 RSS",
  api_only: "仅 API",
  dota_news: "Dota 2 公告",
  cs2_news: "CS2 公告",
  dota_pro: "Dota 职业比赛",
  dota_meta: "Dota 胜率快照",
};
const names = {
  overview: ["运行总览", "回复、记忆与工具统一运行状态"],
  config: ["配置中心", "可视化设置和同一份 JSON 配置"],
  users: ["用户管理", "一个真实 ID 对应一个人物档案，会话规则与人格关系分别维护"],
  memory: ["记忆管理", "分层记忆、语义要旨、原始证据和历史版本"],
  learning: ["语言学习", "表达方式与黑话审核，独立于核心人格"],
  research: ["自主学习", "公开游戏见闻、主观看法、证据与每日预算"],
  relay: ["跨会话转达", "明确授权的私聊转达任务及提交状态"],
  usage: ["Token 用量", "分模型、模块、输入/缓存/输出用量"],
  logs: ["运行日志", "查看每一步决策、工具和异常，默认保留 15 天"],
};
// Keep the existing leaf page IDs, filters, API calls and cross-page links.
// Only the navigation hierarchy is changed.
const modules = {
  overview: ["overview"],
  config: ["config"],
  people: ["users", "memory"],
  learning: ["learning", "research"],
  relay: ["relay"],
  operations: ["usage", "logs"],
};
const moduleForTab = (tab) =>
  Object.keys(modules).find((key) => modules[key].includes(tab));

const configFamilies = [
  { key: "access", label: "接入与转达", sections: ["access", "relay"] },
  { key: "persona", label: "人格与表达", sections: ["persona", "social"] },
  { key: "conversation", label: "对话与回复", sections: ["decision", "reply", "interrupt"] },
  { key: "memory", label: "记忆与学习", sections: ["memory", "habits", "learning", "research"] },
  { key: "knowledge", label: "知识与工具", sections: ["tools", "dota"] },
  { key: "system", label: "模型、预算与日志", sections: ["models", "budget", "logs"] },
];

function configGroups(schema) {
  const remaining = new Map(schema.map(section => [section.key, section]));
  const groups = [];
  for (const family of configFamilies) {
    const sections = family.sections.map(key => remaining.get(key)).filter(Boolean);
    for (const section of sections) remaining.delete(section.key);
    if (sections.length) groups.push({ ...family, sections });
  }
  if (remaining.size) groups.push({ key: "other", label: "其他配置", sections: [...remaining.values()] });
  return groups;
}

async function goToConfig(section) {
  state.configCategory = configFamilies.find(group => group.sections.includes(section))?.key ?? "other";
  state.configSearch = "";
  state.editMode = "form";
  await pageChange("config");
  if (state.tab === "config") $("config-section-" + section)?.scrollIntoView?.({block: "start"});
}

function openModule(key) {
  if (!modules[key]) return;
  return pageChange(state.moduleTabs[key] || modules[key][0]);
}
function drawSectionTabs(module, tab) {
  const root = $("sectionTabs");
  const tabs = modules[module];
  root.hidden = tabs.length < 2;
  root.replaceChildren(...(tabs.length < 2 ? [] : tabs.map((id) =>
    h("button", {
      type: "button",
      class: id === tab ? "selected" : "",
      "data-tab": id,
      "aria-pressed": id === tab,
      onclick: () => pageChange(id),
    }, names[id][0])
  )));
}
const label = (v) => i18n[v] || v || "—";
const clone = (v) => JSON.parse(JSON.stringify(v));
const fmt = (v) => Number(v || 0).toLocaleString("zh-CN");
const dt = (v) =>
  v
    ? new Date(v * 1000).toLocaleString("zh-CN", {
        timeZone: "Asia/Shanghai",
        hour12: false,
      })
    : "—";

function h(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null) continue;
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on") && typeof v === "function")
      node.addEventListener(k.slice(2), v);
    else if (k === "checked" || k === "disabled" || k === "selected")
      node[k] = Boolean(v);
    else if (k === "value") node.value = v;
    else node.setAttribute(k, String(v));
  }
  for (const child of children.flat(Infinity))
    if (child !== undefined && child !== null)
      node.append(
        child instanceof Node ? child : document.createTextNode(String(child)),
      );
  return node;
}
function btn(text, action, cls = "secondary") {
  return h(
    "button",
    { type: "button", class: "button " + cls, onclick: action },
    text,
  );
}
function badge(v, cls = "") {
  return h("span", { class: "badge " + cls }, label(v));
}
function panel(title, ...children) {
  return h("div", { class: "panel" }, h("h2", {}, title), ...children);
}
function card(title, value, sub = "") {
  return h(
    "div",
    { class: "card" },
    h("div", { class: "label" }, title),
    h("div", { class: "metric" }, value),
    h("div", { class: "sub" }, sub),
  );
}
function notice(text, kind = "") {
  const e = $("notice");
  e.hidden = !text;
  e.className = "notice " + kind;
  e.textContent = text || "";
}
async function get(path, params = {}) {
  if (!bridge) throw new Error("请在 AstrBot 插件可视化视图中打开此页面。");
  return await bridge.apiGet(path, params);
}
async function post(path, body) {
  if (!bridge) throw new Error("AstrBot 未连接");
  const r = await bridge.apiPost(path, body);
  if (r?.ok === false) throw new Error(r.error || "操作未完成");
  return r;
}
async function safe(fn) {
  try {
    await fn();
  } catch (e) {
    notice(e?.message || String(e), "error");
  }
}
function modeText(m) {
  $("modeText").textContent = m === "active" ? "正式运行" : label(m);
  $("modeDot").className =
    "dot " + (m === "active" ? "live" : m === "observe" ? "observe" : "");
}
function selected(val, options, onchange) {
  const s = h("select", { onchange: (e) => onchange?.(e.target.value) });
  for (const item of options) {
    const [v, n] = Array.isArray(item) ? item : [item, label(item)];
    s.append(h("option", { value: String(v) }, n));
  }
  s.value = val ?? "";
  return s;
}
function input(value, oninput, placeholder = "", type = "text") {
  const e = h("input", {
    type,
    value: value ?? "",
    placeholder,
    oninput: (ev) => oninput?.(ev.target.value),
  });
  return e;
}
function textarea(value, oninput, rows = 4, cls = "") {
  return h(
    "textarea",
    { rows, class: cls, oninput: (e) => oninput?.(e.target.value) },
    value ?? "",
  );
}
function table(headers, rows, extraClass = "") {
  if (!rows.length) return h("div", { class: "empty" }, "暂无符合条件的数据");
  return h(
    "div",
    { class: "table-wrap" + (extraClass ? " " + extraClass : "") },
    h(
      "table",
      {},
      h(
        "thead",
        {},
        h(
          "tr",
          {},
          headers.map((x) => h("th", {}, x)),
        ),
      ),
      h(
        "tbody",
        {},
        rows.map((r) =>
          h(
            "tr",
            {},
            r.map((cell) => h("td", {}, cell)),
          ),
        ),
      ),
    ),
  );
}
function scopeName(id) {
  const s = state.sessions.find((x) => x.scope === id);
  return s
    ? `${s.kind === "group" ? "群" : "私聊"} · ${s.location} · ${s.persona}`
    : id || "—";
}
async function refreshSessions() {
  state.sessions = (await get("sessions")).items || [];
  return state.sessions;
}
function scopeSelect(value, change, all = true) {
  return selected(
    value,
    [
      ...(all ? [["", "全部会话"]] : [["", "请选择会话"]]),
      ...state.sessions.map((s) => [s.scope, scopeName(s.scope)]),
    ],
    change,
  );
}
function filterFor(name) {
  return (state.filters[name] ||= { q: "", scope: "", offset: 0, limit: 60 });
}
function pager(f, count, fn) {
  return h(
    "div",
    { class: "pager" },
    btn(
      "上一页",
      () => {
        if (f.offset) {
          f.offset = Math.max(0, f.offset - f.limit);
          fn();
        }
      },
      "small",
    ),
    `第 ${1 + Math.floor(f.offset / f.limit)} 页 · ${count} 条`,
    btn(
      "下一页",
      () => {
        if (count >= f.limit) {
          f.offset += f.limit;
          fn();
        }
      },
      "small",
    ),
  );
}
function pageChange(tab, force = false) {
  const module = moduleForTab(tab);
  if (!module) return;
  if (tab === state.tab && tab === "config" && state.dirty && !force) return;
  if (
    tab !== state.tab &&
    state.dirty &&
    !confirm("配置尚未保存，确定离开编辑页？")
  )
    return;
  if (tab !== state.tab && state.dirty) {
    state.dirty = false;
    state.config = null;
    state.draft = null;
    state.invalidJson = {};
  }
  const changed = tab !== state.tab;
  state.tab = tab;
  state.moduleTabs[module] = tab;
  for (const e of $("nav").querySelectorAll("button[data-module]")) {
    const active = e.dataset.module === module;
    e.classList.toggle("selected", active);
    if (active) e.setAttribute("aria-current", "page");
    else e.removeAttribute("aria-current");
  }
  drawSectionTabs(module, tab);
  if (changed) window.scrollTo?.(0, 0);
  $("pageTitle").textContent = names[tab][0];
  $("pageDesc").textContent = names[tab][1];
  notice("");
  const token = ++state.epoch;
  page.replaceChildren(h("div", { class: "loading" }, "加载中…"));
  return safe(async () => {
    if (tab === "overview") await renderOverview(token);
    else if (tab === "config") await renderConfig(force, token);
    else if (tab === "users") await renderUsers(token);
    else if (tab === "memory") await renderMemory(token);
    else if (tab === "learning") await renderLearning(token);
    else if (tab === "research") await renderResearch(token);
    else if (tab === "relay") await renderRelay(token);
    else if (tab === "usage") await renderUsage(token);
    else if (tab === "logs") await renderLogs(token);
  });
}
function still(token) {
  return token === state.epoch;
}
function refresh() {
  if (
    state.tab === "config" &&
    state.dirty &&
    !confirm("放弃当前未保存配置并重新加载？")
  )
    return;
  state.dirty = false;
  pageChange(state.tab, true);
}

async function runAction(name, scope) {
  await safe(async () => {
    await post("action", { name, scope: scope || undefined });
    notice("任务已开始。可在总览中查看结果。", "success");
    if (state.tab === "overview") await renderOverview(state.epoch);
  });
}

async function renderOverview(token) {
  const s = await get("overview");
  if (!still(token)) return;
  modeText(s.mode);
  const t = s.totals || {},
    today = t.today || {},
    mem = t.memories || {},
    styles = t.styles || {},
    usage = s.usage || {},
    counts = usage.modules || {};
  const cards = h(
    "div",
    { class: "cards" },
    card("今日收到消息", fmt(today.received), "实际接管/观察会话"),
    card("今日回复", fmt(today.replied), `${fmt(today.sent_parts)} 个发送段`),
    card(
      "活跃长期记忆",
      fmt(mem.active),
      `${fmt(mem.archived)} 深层存档 · ${fmt(mem.expired)} 失效`,
    ),
    card("已批准表达", fmt(styles.approved), `${fmt(styles.pending)} 待审查`),
    card(
      "今日 Token",
      fmt(usage.tokens),
      s.budget ? `预算 ${fmt(s.budget)}` : "无每日硬预算",
    ),
    card(
      "正在处理",
      fmt(s.queue),
      `${fmt(s.workers)} 会话 · ${fmt(s.model_running)} 模型请求`,
    ),
  );
  const welcome =
    s.mode === "off"
      ? h(
          "div",
          { class: "warning" },
          "首次安装：插件默认停用，不会接管任何群消息。先在配置中心设置群聊白名单、模型及知识库权限，再选择观察或正式模式。",
          h(
            "div",
            { class: "toolbar" },
            btn("前往配置", () => pageChange("config"), "primary"),
          ),
        )
      : s.mode === "observe"
        ? h(
            "div",
            { class: "warning" },
            "观察模式只记录允许的会话并模拟回复决策，不发送消息、不自动学习。现有 AstrBot 回复链仍会运行。",
          )
        : null;
  const ratio = s.budget ? usage.tokens / s.budget : 0;
  const budget = h(
    "div",
    { class: "panel" },
    h("h2", {}, "Token 预算 · " + s.today),
    h(
      "p",
      { class: "muted" },
      s.budget
        ? `${fmt(usage.tokens)} / ${fmt(s.budget)}（${(ratio * 100).toFixed(1)}%）`
        : `今日 ${fmt(usage.tokens)} Token，未设置上限。`,
    ),
    h(
      "div",
      {
        class: "progress " + (ratio >= 1 ? "bad" : ratio >= 0.8 ? "warn" : ""),
      },
      h("i", { style: `width:${Math.min(100, ratio * 100)}%` }),
    ),
    h(
      "p",
      { class: "help" },
      "接近软预算先延后后台学习。用量缺失时以明确标记的估算记入预算；实际供应商账单可能不同。",
    ),
    btn("查看完整用量", () => pageChange("usage"), "small"),
  );
  const external = h(
    "div",
    { class: "panel" },
    h("h2", {}, "外部数据"),
    h(
      "p",
      {},
      "Dota 2：",
      badge(s.dota?.state || "未同步"),
      s.dota?.date ? " " + s.dota.date : "",
    ),
    [
      ["public", "OpenDota · 公开/段位胜率"],
      ["weekly", "Valve · 分组周统计"],
      ["matchups", "Valve · 对抗/同队"],
    ].map(([key, title]) => {
      const data = s.dota?.sources?.[key];
      return h(
        "div",
        { class: "help" },
        title + "：",
        badge(data?.state || "无已校验数据"),
        data?.date ? " " + data.date + (data.date !== s.today ? "（非今日）" : "") : "",
        data?.weighted_win_rate_pct !== undefined ? ` · 加权 ${Number(data.weighted_win_rate_pct).toFixed(2)}%` : "",
        data?.reason ? h("div", {}, data.reason) : null,
      );
    }),
    h("p", {}, "Dota 版本资料：", badge(s.dota_knowledge?.state || "未同步"),
      s.dota_knowledge?.patch ? " · " + s.dota_knowledge.patch : ""),
    [["hero", "英雄与技能"], ["item", "装备与合成"]].map(([key, title]) => {
      const data = s.dota_knowledge?.[key];
      const n = Number(data?.total || 0), done = Number(data?.done || 0);
      return h("div", {class:"help"},
        title + "：",
        data?.published ? badge("已发布") : badge(n ? "同步中" : "待同步"),
        n ? " " + fmt(done) + " / " + fmt(n) : "",
        data?.metadata_only ? " · 仅目录 " + fmt(data.metadata_only) : "",
        data?.latest_published ? " · 可用版本 " + data.latest_published + "（共 " + data.versions + " 版）" : "",
        n ? h("div", {class:"progress"}, h("i", {style:"width:" + Math.min(100, done/n*100) + "%"})) : null
      );
    }),
    s.dota_knowledge?.reason ? h("div", {class:"help"}, "资料同步异常：" + s.dota_knowledge.reason) : null,
    s.dota_knowledge?.failures ? h("div", {class:"help"}, "本批失败 " + fmt(s.dota_knowledge.failures) + " 项，保留已提交资料，稍后重试。") : null,
    h(
      "p",
      {},
      "DeepSeek：",
      badge(s.balance?.state || "未检查"),
      s.balance?.amount !== undefined
        ? " " + s.balance.amount + " " + s.balance.currency
        : "",
      s.balance?.reason ? " · " + s.balance.reason : "",
    ),
    h(
      "div",
      { class: "toolbar" },
      btn("同步 Dota 统计", () => runAction("dota_sync")),
      btn("同步 Dota 版本资料", () => runAction("dota_knowledge")),
      btn("检查余额", () => runAction("balance_check")),
    ),
    h(
      "p",
      { class: "help" },
      "统计来源与英雄/装备版本资料独立校验；中断自动续传，旧版快照保留。条件查询等待完整目录发布。同数据同步、余额检查均不消耗对话模型 Token。",
    ),
  );
  const actions = s.actions || {};
  const actionRows = Object.entries(actions).map(([key, val]) => [
    key,
    badge(
      val.state,
      val.state === "error" ? "bad" : val.state === "running" ? "warn" : "good",
    ),
    h(
      "div",
      { class: "action-result" },
      JSON.stringify(val.result || val.exception || val.started || {}, null, 2),
    ),
    val.finished || "—",
  ]);
  page.replaceChildren(
    ...(welcome ? [welcome] : []),
    ...(s.config_error ? [h("div", { class: "warning" }, s.config_error)] : []),
    cards,
    h("div", { class: "grid-two" }, budget, external),
    panel(
      "数据维护",
      h(
        "p",
        { class: "muted" },
        `已记录 ${fmt(t.sessions)} 个会话、${fmt(t.people)} 位独立用户（${fmt(t.memberships ?? t.people)} 条会话身份）。待记忆整理 ${fmt(t.pending_memory)} 条，待语言学习 ${fmt(t.pending_style)} 条。`,
      ),
      h(
        "div",
        { class: "toolbar" },
        btn("立即清理/维护", () => runAction("maintenance")),
        btn("整理一批记忆", () => runAction("memory_flush")),
        btn("学习一批表达", () => runAction("style_flush")),
      ),
      table(["任务", "状态", "结果", "完成时间"], actionRows),
    ),
  );
}

function markDirty() {
  state.dirty = true;
  const e = $("dirtyStatus");
  if (e) e.textContent = "未保存修改";
}
function currentConfig() {
  if (state.editMode === "json" && $("jsonEditor")) {
    const text = $("jsonEditor").value;
    const result = JSON.parse(text);
    if (!result || Array.isArray(result) || typeof result !== "object")
      throw new Error("JSON 根必须是对象");
    return result;
  }
  const bad = Object.keys(state.invalidJson);
  if (bad.length)
    throw new Error("以下字段的 JSON 格式错误：" + bad.join("、"));
  return clone(state.draft);
}
async function loadConfig() {
  const c = await get("config");
  state.config = c;
  state.draft = clone(c.config);
  state.options = c.options || {};
  state.dirty = false;
  state.invalidJson = {};
  modeText(c.config.access.mode);
}
async function saveConfig() {
  const token = state.epoch;
  await safe(async () => {
    const draft = currentConfig();
    const save = $("saveConfig");
    if (save) save.disabled = true;
    try {
      const r = await post("config/save", {
        revision: state.config.revision,
        config: draft,
      });
      state.config = { ...state.config, ...r };
      state.draft = clone(r.config);
      state.dirty = false;
      modeText(r.config.access.mode);
      if (still(token)) {
        notice("配置已保存并生效（旧配置已备份）。", "success");
        drawConfig();
      }
    } finally {
      if (save) save.disabled = false;
    }
  });
}
function copyJson() {
  safe(async () => {
    const text = JSON.stringify(currentConfig(), null, 2);
    try {
      await navigator.clipboard.writeText(text);
      notice("已复制完整 JSON。", "success");
    } catch {
      state.draft = currentConfig();
      state.editMode = "json";
      drawConfig();
      $("jsonEditor").select();
      notice("已选中完整 JSON，请按 Ctrl+C / 复制。");
    }
  });
}
function exportJson() {
  safe(async () => {
    const text = JSON.stringify(currentConfig(), null, 2);
    const url = URL.createObjectURL(
      new Blob([text], { type: "application/json" }),
    );
    const a = h("a", { href: url, download: "Fiyu-Core-config.json" });
    document.body.append(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 2000);
  });
}
function switchMode(mode) {
  if (mode === state.editMode) return;
  safe(async () => {
    state.draft = currentConfig();
    state.editMode = mode;
    drawConfig();
  });
}
function chooseFile() {
  const f = h("input", {
    type: "file",
    accept: ".json,application/json",
    onchange: () =>
      safe(async () => {
        const file = f.files[0];
        if (!file) return;
        if (file.size > 250000) throw new Error("配置文件过大");
        const obj = JSON.parse(await file.text());
        if (!obj || typeof obj !== "object" || Array.isArray(obj))
          throw new Error("JSON 根必须是对象");
        state.draft = obj;
        state.invalidJson = {};
        state.editMode = "json";
        markDirty();
        drawConfig();
        notice("已载入草稿，检查无误后点击保存。");
      }),
  });
  f.click();
}
function optBinding(group, field, value, change) {
  const opts = state.options[field.widget] || [];
  const all = [
    [
      "",
      field.widget === "persona"
        ? "跟随会话人格"
        : field.widget === "embedding_provider"
          ? "禁用向量检索"
          : "未指定 / 按设置继承",
    ],
    ...opts.map((x) => [x.id, x.label || x.id]),
  ];
  if (value && !all.some((x) => x[0] === value))
    all.push([value, value + "（当前未列出）"]);
  return selected(value, all, change);
}
function kbEditor(values, change) {
  const outer = h("div");
  const list = Array.isArray(values) ? values : [];
  const rebuild = () => {
    outer.replaceChildren();
    for (let idx = 0; idx < list.length; idx++) {
      const x = list[idx];
      const update = (k, v) => {
        x[k] = v;
        change(list);
      };
      const kbs = state.options.knowledge_bases || [];
      const choices = [
        ["", "请选择现有知识库"],
        ...kbs.map((k) => [k.name, k.name]),
      ];
      if (x.name && !choices.some((k) => k[0] === x.name))
        choices.push([x.name, x.name + "（未加载）"]);
      const lab = (n, control) => h("label", {}, n, control);
      outer.append(
        h(
          "div",
          { class: "binding" },
          h(
            "div",
            { class: "binding-grid" },
            lab(
              "模型调用别名",
              input(x.alias, (v) => update("alias", v), "sap / dota_docs"),
            ),
            lab(
              "AstrBot 知识库",
              selected(x.name, choices, (v) => update("name", v)),
            ),
            lab(
              "用途说明",
              input(
                x.description,
                (v) => update("description", v),
                "如 SAP 官方技术文档",
              ),
            ),
            lab(
              "允许查询的群 OpenID / UMO（每行一个）",
              textarea(
                (x.allowed_groups || []).join("\n"),
                (v) =>
                  update(
                    "allowed_groups",
                    v
                      .split("\n")
                      .map((x) => x.trim())
                      .filter(Boolean),
                  ),
                2,
              ),
            ),
          ),
          h(
            "div",
            { class: "toolbar" },
            h(
              "label",
              { class: "check-line" },
              h("input", {
                type: "checkbox",
                checked: x.allow_private,
                onchange: (e) => update("allow_private", e.target.checked),
              }),
              "允许已授权私聊",
            ),
            h("div", { class: "spacer" }),
            btn(
              "移除映射",
              () => {
                list.splice(idx, 1);
                change(list);
                rebuild();
              },
              "danger small",
            ),
          ),
        ),
      );
    }
    outer.append(
      btn(
        "＋ 添加知识库映射",
        () => {
          let i = list.length + 1;
          while (list.some((x) => x.alias === "kb" + i)) i++;
          list.push({
            alias: "kb" + i,
            name: "",
            description: "",
            allowed_groups: [],
            allow_private: false,
          });
          change(list);
          rebuild();
        },
        "small",
      ),
    );
  };
  rebuild();
  return outer;
}
function fieldControl(group, f) {
  const section = (state.draft[group.key] ||= {});
  const val = section[f.key] ?? clone(f.default);
  const update = (v) => {
    section[f.key] = v;
    markDirty();
  };
  if (f.widget === "kb_bindings") return kbEditor(clone(val), update);
  if (
    f.widget === "chat_provider" ||
    f.widget === "embedding_provider" ||
    f.widget === "persona"
  )
    return optBinding(group, f, val, update);
  if (f.choices)
    return selected(
      val,
      f.key === "mode"
        ? [
            ["off", "停用"],
            ["observe", "观察模式"],
            ["active", "正式运行"],
          ]
        : f.choices,
      update,
    );
  if (f.type === "boolean")
    return h(
      "div",
      { class: "check-line" },
      h("input", {
        type: "checkbox",
        checked: val,
        onchange: (e) => update(e.target.checked),
      }),
      h("span", { class: "muted" }, "启用"),
    );
  if (f.widget === "lines")
    return textarea(
      (val || []).join("\n"),
      (v) =>
        update(
          v
            .split("\n")
            .map((v) => v.trim())
            .filter(Boolean),
        ),
      3,
    );
  if (f.widget === "json" || f.type === "object" || f.type === "array") {
    const path = group.key + "." + f.key;
    const e = textarea(
      state.invalidJson[path] ?? JSON.stringify(val, null, 2),
      (v) => {
        try {
          update(JSON.parse(v));
          delete state.invalidJson[path];
          e.setCustomValidity("");
        } catch {
          state.invalidJson[path] = v;
          e.setCustomValidity("JSON 格式错误");
          markDirty();
        }
      },
      4,
      "mono",
    );
    if (path in state.invalidJson) e.setCustomValidity("JSON 格式错误");
    return e;
  }
  if (f.widget === "textarea") return textarea(val, update, 4);
  if (f.type === "integer" || f.type === "number") {
    const e = input(
      val,
      (v) => {
        if (v !== "" && Number.isFinite(Number(v))) update(Number(v));
        else {
          section[f.key] = v;
          markDirty();
        }
      },
      "",
      "number",
    );
    e.min = f.min;
    e.max = f.max;
    e.step = f.type === "integer" ? 1 : "any";
    return e;
  }
  return input(val, update);
}
function configSection(group, q) {
  const fields = group.fields.filter(f => !q || [
    group.label, group.key, f.label, f.key, `${group.key}.${f.key}`, f.description,
  ].join(" ").toLowerCase().includes(q));
  if (!fields.length) return null;
  const nodes = fields.map(f => h(
    "div",
    { class: "field" + (f.widget === "kb_bindings" ? " wide" : "") },
    h("div", {},
      h("div", { class: "label" }, f.label),
      h("div", { class: "key" }, `${group.key}.${f.key}`),
      h("div", { class: "help" }, f.description)),
    h("div", { class: "control" }, fieldControl(group, f))));
  return h("section", { class: "config-section", id: "config-section-" + group.key },
           h("h3", {}, group.label), nodes);
}

function drawFields(container) {
  container.replaceChildren();
  const q = state.configSearch.trim().toLowerCase();
  for (const family of configGroups(state.config.schema)) {
    if (state.configCategory && state.configCategory !== family.key) continue;
    const sections = family.sections.map(group => configSection(group, q)).filter(Boolean);
    if (sections.length) container.append(
      h("section", {class: "panel config-group"}, h("h2", {}, family.label), sections));
  }
  if (!container.children.length)
    container.append(h("p", { class: "empty" }, "没有匹配的配置字段"));
}
function drawConfig() {
  const form = state.editMode === "form";
  const groupContainer = h("div");
  const picker = selected(state.configCategory,
    [["", "全部模块"], ...configGroups(state.config.schema).map(group => [group.key, group.label])],
    value => { state.configCategory = value; drawFields(groupContainer); });
  picker.setAttribute("aria-label", "配置模块分类");
  const b = btn(
    "保存配置",
    () => {
      for (const x of page.querySelectorAll("textarea,input"))
        if (!x.checkValidity()) {
          x.reportValidity();
          return;
        }
      saveConfig();
    },
    "primary",
  );
  b.id = "saveConfig";
  const dirty = h(
    "span",
    { class: "help", id: "dirtyStatus" },
    state.dirty ? "未保存修改" : "当前已保存",
  );
  const controls = h(
    "div",
    { class: "config-controls" },
    h(
      "div",
      { class: "toolbar" },
      btn("可视化", () => switchMode("form"), form ? "active" : "secondary"),
      btn(
        "JSON 编辑",
        () => switchMode("json"),
        !form ? "active" : "secondary",
      ),
      form
        ? h("div", { class: "category-picker" }, picker)
        : null,
      form
        ? h(
            "div",
            { class: "grow search" },
            input(
              state.configSearch,
              (v) => {
                state.configSearch = v;
                drawFields(groupContainer);
              },
              "搜索名称、字段、描述…",
            ),
          )
        : h("div", { class: "spacer" }),
      dirty,
      b,
    ),
    h(
      "div",
      { class: "toolbar" },
      btn("复制完整 JSON", copyJson, "small"),
      btn("导入 JSON 文件", chooseFile, "small"),
      btn("导出 JSON", exportJson, "small"),
      btn(
        "加载默认草稿",
        () =>
          safe(async () => {
            if (!confirm("加载默认配置到草稿？保存前不会生效。")) return;
            state.draft = (await get("config/defaults")).config;
            state.invalidJson = {};
            markDirty();
            drawConfig();
          }),
        "small",
      ),
    ),
  );
  const backups = state.config.backups || [];
  const restoreSelect = selected("", [
    ["", "选择历史配置"],
    ...backups.map((x) => [x, x]),
  ]);
  const backupPanel = panel(
    "历史备份",
    h(
      "div",
      { class: "toolbar" },
      h("div", { class: "grow" }, restoreSelect),
      btn("恢复选中版本", () =>
        safe(async () => {
          if (!restoreSelect.value) throw new Error("请先选择备份");
          if (!confirm("恢复选中备份会立即替换当前配置，继续？")) return;
          const r = await post("config/restore", {
            backup: restoreSelect.value,
            revision: state.config.revision,
          });
          state.config = { ...state.config, ...r };
          state.draft = clone(r.config);
          state.dirty = false;
          state.invalidJson = {};
          modeText(r.config.access.mode);
          drawConfig();
          notice("已恢复，并备份被替换的配置。", "success");
        }),
      ),
    ),
  );
  if (form) {
    drawFields(groupContainer);
    page.replaceChildren(controls, groupContainer, backupPanel);
  } else {
    const editor = textarea(
      JSON.stringify(state.draft, null, 2),
      markDirty,
      22,
      "code",
    );
    editor.id = "jsonEditor";
    editor.spellcheck = false;
    page.replaceChildren(
      controls,
      panel(
        "完整 JSON 配置",
        h(
          "p",
          { class: "help" },
          "可直接全选、复制、替换粘贴。保存时检查字段与值；缺少字段使用默认值。未授权的群聊不会被接管。",
        ),
        editor,
      ),
      backupPanel,
    );
  }
}
async function renderConfig(force, token) {
  if (force || !state.config) await loadConfig();
  if (still(token)) drawConfig();
}

function showDialog(title, fields, submit, button = "保存") {
  const dlg = $("dialog");
  $("dialogTitle").textContent = title;
  $("dialogBody").replaceChildren(...fields);
  $("dialogSubmit").textContent = button;
  $("dialogSubmit").hidden = !submit;
  $("dialogForm").onsubmit = async (e) => {
    e.preventDefault();
    if (!submit) return;
    const b = $("dialogSubmit");
    b.disabled = true;
    try {
      await submit();
      dlg.close();
    } catch (err) {
      notice(err?.message || String(err), "error");
      const prior = $("dialogError");
      if (prior) prior.remove();
      $("dialogBody").prepend(
        h(
          "p",
          { id: "dialogError", class: "error-text" },
          err?.message || String(err),
        ),
      );
    } finally {
      b.disabled = false;
    }
  };
  if (dlg.open) dlg.close();
  dlg.showModal();
}
function line(title, control, help) {
  return h(
    "div",
    { class: "form-line" },
    h("label", {}, title),
    control,
    help ? h("small", {}, help) : null,
  );
}
function check(title, checked, onchange) {
  return h(
    "label",
    { class: "check-line" },
    h("input", {
      type: "checkbox",
      checked,
      onchange: (e) => onchange(e.target.checked),
    }),
    title,
  );
}
function filterToolbar(name, extra = []) {
  const f = filterFor(name);
  const go = () => {
    f.offset = 0;
    pageChange(state.tab);
  };
  const sc = scopeSelect(f.scope, (v) => {
    f.scope = v;
  });
  const q = input(
    f.q,
    (v) => {
      f.q = v;
    },
    "关键词",
  );
  q.addEventListener("keydown", (e) => {
    if (e.key === "Enter") go();
  });
  return h(
    "div",
    { class: "toolbar" },
    h("div", { class: "grow" }, sc),
    h("div", { class: "grow" }, q),
    ...extra,
    btn("查询", go),
  );
}

async function renderUsers(token) {
  await refreshSessions();
  const f = filterFor("users");
  const res = await get("users/identities", f);
  if (!still(token)) return;
  const rows=(res.items||[]).map(r=>identityCard(r,f.scope));
  page.replaceChildren(
    panel(
      "用户档案",
      h(
        "p",
        { class: "help" },
        "同一平台实例下相同的真实发送者 ID 合并为一位用户。通用称呼与同人格的关系共用；展开会话可单独维护备注、屏蔽、习惯授权及旧称呼。聊天与记忆仍按原会话隔离。",
      ),
      filterToolbar("users"),
      h("div",{class:"identity-list"},rows.length?rows:h("div",{class:"empty"},"没有匹配的用户档案")),
      pager(f, rows.length, () => pageChange("users")),
    ),
  );
}

function membershipName(m) {
  return `${m.kind==="private"?"私聊":"群聊"} · ${m.location||m.scope} · ${m.persona||""}`;
}
function identityCard(r, selectedScope) {
  const aliases=[...new Set([...(r.custom_aliases||[]),...(r.historical_aliases||[])])];
  const members=r.memberships||[];
  const expanded=state.expandedUsers.has(r.identity_id);
  const body=h("div",{class:"identity-memberships"},members.map(m=>h("div",
    {class:"identity-membership"+(selectedScope===m.scope?" match":"")},
    h("div",{class:"identity-member-heading"},h("strong",{},membershipName(m)),
      badge(m.blocked?"该会话已屏蔽":"该会话允许",m.blocked?"bad":"good")),
    h("div",{class:"help"},"平台昵称："+(m.name||"—")+" · 称呼："+(m.display_name||r.display_name||m.name||"—")),
    h("div",{class:"help"},"会话身份："+m.ref+" · 互动 "+fmt(m.interactions)+" 次 · "+(m.habit_opt_in?"习惯已授权":"习惯未授权")),
    (m.custom_aliases||[]).length?h("div",{class:"help"},"专属别名："+m.custom_aliases.join("、")):null,
    m.note?h("div",{class:"identity-member-note"},m.note):null,
    h("div",{class:"actions"},
      btn("会话设置",()=>editUser(m),"small"),
      btn("迁移旧称呼",()=>editLegacyTitles(m),"small")))));
  body.hidden=!expanded;
  const toggle=btn(expanded?"收起会话":"展开会话",()=>{
    body.hidden=!body.hidden;
    if(body.hidden)state.expandedUsers.delete(r.identity_id);else state.expandedUsers.add(r.identity_id);
    toggle.textContent=body.hidden?"展开会话":"收起会话";
    toggle.setAttribute("aria-expanded",String(!body.hidden));
  },"small");
  toggle.setAttribute("aria-expanded",String(expanded));
  const count=Number(r.blocked_count||0), total=members.length;
  return h("article",{class:"identity-card","data-user":r.identity_id},
    h("div",{class:"identity-summary"},
      h("div",{class:"identity-person"},
        h("strong",{},r.display_name||(r.name!==r.identity_id?r.name:"未提供昵称")),
        h("div",{class:"help mono"},"ID："+r.uid),
        h("div",{class:"help"},r.platform_id||r.platform||""),
        aliases.length?h("div",{class:"help",title:aliases.join("、")},"别名："+aliases.slice(0,6).join("、")+(aliases.length>6?"…":"")):null),
      h("div",{class:"identity-scope-count"},
        badge(fmt(r.membership_count??total)+" 个会话"),
        h("div",{class:"help"},fmt(r.group_count)+" 群 · "+fmt(r.private_count)+" 私聊"),
        badge(count===0?"无会话屏蔽":count===total?"全部会话屏蔽":"部分会话屏蔽",count===0?"good":count===total?"bad":"warn")),
      h("div",{class:"identity-relations"},(r.relationships||[]).map(rel=>h("div",{class:"identity-relation"},
        h("div",{},h("strong",{},rel.persona||"默认人格")," · ",badge(rel.familiarity||"初识")),
        h("div",{class:"help"},"好感度 "+fmt(rel.affinity)+" · 累计 "+fmt(rel.interactions)+" 次 · 近7天 "+fmt(rel.recent_interactions)+" 次")))),
      h("div",{class:"actions identity-actions"},
        btn("编辑档案",()=>editGlobalUser(r),"primary small"),toggle,
        btn("关联记忆",()=>{const f=filterFor("memory");Object.assign(f,{scope:"",q:r.identity_id,kind:"",state:"",offset:0});pageChange("memory");},"small"))),
    body);
}

function editGlobalUser(r) {
  const token=state.epoch;
  const d={identity_id:r.identity_id,revision:r.revision,display_name:r.display_name||"",
    custom_aliases:clone(r.custom_aliases||[]),relationships:clone(r.relationships||[])};
  showDialog("编辑档案 · "+(r.display_name||r.name),[
    line("真实 ID / 统一身份",h("div",{class:"mono"},r.uid," / ",r.identity_id)),
    line("平台实例",h("div",{},r.platform||""," · ",r.platform_id||"")),
    line("通用称呼",input(d.display_name,v=>d.display_name=v),
      "在所有关联会话可用；已有会话专属称呼优先。留空则使用各会话原昵称。"),
    line("历史昵称（所有会话汇总）",h("div",{},(r.historical_aliases||[]).join("、")||"—"),
      "自动观察的其他会话昵称仅用于后台识别，不会自动发送给当前聊天模型。"),
    line("通用别名（每行一个）",textarea(d.custom_aliases.join("\n"),v=>d.custom_aliases=v.split("\n").map(x=>x.trim()).filter(Boolean)),
      "只填写适合所有这些会话使用的称呼；不改变真实身份。"),
    ...d.relationships.map(rel=>h("div",{class:"panel identity-relation-editor"},
      h("h3",{},rel.persona||"默认人格"),
      h("p",{class:"help"},"同人格的 "+fmt(rel.membership_count)+" 个会话共享 · 累计 "+fmt(rel.interactions)+" 次互动"),
      line("关系标签",selected(rel.role,[["member","普通群友"],["friend","朋友"]],v=>rel.role=v)),
      line("熟悉度",selected(rel.familiarity_override||"auto",
        [["auto","自动（累计真实互动）"],["stranger","初识"],["normal","一般"],["familiar","熟悉"],["close","常聊"]],
        v=>rel.familiarity_override=v)),
      line("好感度（0-100）",input(rel.affinity,v=>rel.affinity=Number(v),"","number"))))
  ],async()=>{
    const prior=new Map((r.relationships||[]).map(x=>[x.persona,x]));
    const changes=d.relationships.filter(x=>{
      const old=prior.get(x.persona);
      return ["role","familiarity_override","affinity"].some(key=>x[key]!==old?.[key]);
    }).map(x=>({persona:x.persona,revision:x.revision,role:x.role,affinity:x.affinity,familiarity_override:x.familiarity_override}));
    await post("users/identity/save",{identity_id:d.identity_id,revision:d.revision,display_name:d.display_name,
      custom_aliases:d.custom_aliases,relationships:changes});
    notice("统一用户档案已保存。会话规则未改变。","success");
    if(still(token))await renderUsers(token);
  });
}

function editUser(r) {
  const token = state.epoch;
  const d = {
    scope:r.scope,uid:r.uid,display_name:r.display_name||"",note:r.note||"",deny_reply:r.deny_reply||"",
    blocked: !!r.blocked,
    habit_opt_in:!!r.habit_opt_in,
    deny_keywords: clone(r.deny_keywords || []),
    custom_aliases: clone(r.custom_aliases || []),
  };
  showDialog(
    `会话设置 · ${r.display_name || r.name}`,
    [
      line("真实 ID · 内部身份代号", h("div", { class: "mono" }, r.uid, " / ", r.ref)),
      line("所属会话",h("div",{},membershipName(r))),
      line("当前平台昵称（自动更新）", h("div", {}, r.name || "—")),
      line(
        "本会话专属称呼",
        input(d.display_name, (v) => (d.display_name = v)),
        "留空使用通用称呼或本会话平台昵称，不影响其他会话。",
      ),
      line("历史昵称（自动记录）", h("div", {}, (r.historical_aliases || []).join("、") || "—")),
      line("自定义别名（每行一个）", textarea(d.custom_aliases.join("\n"), (v) => {
        d.custom_aliases = v.split("\n").map((x) => x.trim()).filter(Boolean);
      }), "可维护群友叫法。重复昵称不会被自动合并为同一个人。"),
      line(
        "备注 / 称呼规则",
        textarea(d.note, (v) => (d.note = v)),
      ),
      check("仅在该会话屏蔽（不回复、不学习）", d.blocked, (v) => (d.blocked = v)),
      check("本人已同意归纳习惯（撤回会删除其习惯候选和派生记录）",d.habit_opt_in,v=>d.habit_opt_in=v),
      line(
        "拒绝回答的关键词（每行一个）",
        textarea(
          d.deny_keywords.join("\n"),
          (v) =>
            (d.deny_keywords = v
              .split("\n")
              .map((x) => x.trim())
              .filter(Boolean)),
        ),
        "触发时使用固定回复，不让模型处理被拒绝的请求。",
      ),
      line(
        "拒绝回复",
        input(d.deny_reply, (v) => (d.deny_reply = v)),
      ),
    ],
    async () => {
      await post("users/save", d);
      notice("会话设置已保存", "success");
      if (still(token)) await renderUsers(token);
    },
  );
}

function editLegacyTitles(r) {
  const token=state.epoch;
  let old="老大", name=/^(老大|主人|主人大人|owner|管理员)$/i.test(r.display_name||r.name||"")?"":(r.display_name||r.name||"");
  let plan=null, chosen=new Set(), rename=true;
  const output=h("div",{class:"legacy-preview"});
  const invalidate=()=>{plan=null;chosen.clear();output.replaceChildren(h("p",{class:"help"},"名称或范围已变更，请重新预览。"));};
  const preview=async()=>safe(async()=>{
    plan=await post("users/legacy_preview",{scope:r.scope,ref:r.ref,old,name});
    chosen=new Set();
    output.replaceChildren(
      h("p",{class:"help"},"命中 "+plan.rows.length+" 条关联记忆。请逐条核对并选择。只改变派生正文；原证据、日期、人物 ID 和历史版本保留。"),
      ...plan.rows.map(item=>h("div",{class:"panel legacy-record"},
        check("修改记忆 #"+item.id,false,v=>v?chosen.add(item.id):chosen.delete(item.id)),
        ...Object.entries(item.changes).map(([key,val])=>h("div",{},
          h("div",{class:"label"},key),
          h("pre",{class:"legacy-before"},val.before),
          h("pre",{class:"legacy-after"},val.after))))))
    ;
  });
  showDialog("迁移旧称呼 · "+(r.name||r.ref),[
    line("固定身份",h("div",{class:"mono"},r.uid," / ",r.ref)),
    line("旧称呼",selected(old,["老大","主人","主人大人","owner","管理员"],v=>{old=v;invalidate()})),
    line("正常昵称",input(name,v=>{name=v;invalidate()})),
    check("同时设为此人的聊天称呼",rename,v=>rename=v),
    btn("预览替换",preview,"secondary"),output
  ],async()=>{
    if(!plan) throw new Error("请先预览并选择需要修改的记录");
    if(!chosen.size&&!rename) throw new Error("没有选中修改项");
    if(!confirm("确认按预览定向替换所选记录？")) throw new Error("已取消操作");
    const res=await post("users/legacy_apply",{scope:r.scope,ref:r.ref,old,name,revision:plan.revision,selected:[...chosen],rename_display:rename,confirm:true});
    notice("已修改 "+res.changed+" 条记忆；原始证据保留。","success");
    if(still(token)) await renderUsers(token);
  },"确认所选修改");
}

async function renderMemory(token) {
  await refreshSessions();
  const f = filterFor("memory");
  const kind = selected(
    f.kind || "",
    [
      ["", "所有类型"],
      ...["core", "profile", "experience", "ordinary", "temporary","habit"].map((x) => [
        x,
        label(x),
      ]),
    ],
    (v) => (f.kind = v),
  );
  const stat = selected(
    f.state || "",
    [
      ["", "全部状态"],
      ...["active", "archived", "expired"].map((x) => [x, label(x)]),
    ],
    (v) => (f.state = v),
  );
  const res = await get("memories", f);
  if (!still(token)) return;
  const rows = (res.items || []).map((r) => [
    h(
      "div",
      {},
      h("strong", {}, r.title),
      h("div", { class: "help" }, r.subject === "group" ? "该会话公共事实/事件" : r.subject_name || r.subject),
      ...(r.participants || []).map((p) => h("div", { class: "help identity-detail" },
        `${p.display_name || p.name} · ${p.uid}`)),
      h("div", { class: "help" }, scopeName(r.scope)),
    ),
    h(
      "div",
      { class: "statusline" },
      badge(r.kind),
      badge(r.state, r.state === "active" ? "good" : "warn"),
      badge(r.clarity),
      r.pinned ? badge("置顶", "good") : null,
    ),
    r.visible_text,
    `${r.importance} / ${Number(r.confidence).toFixed(2)}`,
    dt(r.updated),
    h(
      "div",
      { class: "actions" },
      btn("查看/编辑", () => editMemory(r), "small"),
      btn("历史", () => memoryHistory(r), "small"),
      btn(
        r.state === "active" ? "归档" : "恢复",
        () =>
          safe(async () => {
            await post("memories/action", {
              id: r.id,
              action: r.state === "active" ? "archive" : "restore",
            });
            if (still(token)) await renderMemory(token);
          }),
        "small",
      ),
      btn(
        "删除",
        () =>
          safe(async () => {
            if (
              !confirm(
                `删除「${r.title}」及其历史版本？短期原始消息仍按保留期清理。`,
              )
            )
              return;
            await post("memories/action", {
              id: r.id,
              action: "delete",
              confirm: true,
            });
            if (still(token)) await renderMemory(token);
          }),
        "small danger",
      ),
    ),
  ]);
  page.replaceChildren(
    panel(
      "长期记忆",
      h(
        "p",
        { class: "help" },
        "重要性不随时间降低；清晰度影响日常回忆的内容。深层存档保留详细事实。支持按用户ID、稳定身份代号、现用或历史昵称搜索个人及关联的多人记忆。",
      ),
      h(
        "div",
        { class: "toolbar" },
        btn("＋ 手动添加记忆", () => editMemory(null), "primary"),
        btn("整理一批", () => runAction("memory_flush", f.scope), "small"),
      ),
      filterToolbar("memory", [kind, stat]),
      table(
        [
          "记忆",
          "类型 / 状态",
          "当前可见内容",
          "重要性 / 置信度",
          "更新时间",
          "操作",
        ],
        rows,
      ),
      pager(f, rows.length, () => pageChange("memory")),
    ),
  );
}
async function editMemory(r) {
  const token = state.epoch;
  if(r?.kind==="habit"){
    const d={...r,pinned:false};
    showDialog("修正习惯印象",[
      line("人物 / 稳定主题",h("p",{},r.subject_name||r.subject," · ",r.topic_key)),
      line("有效期（随实际新证据更新）",h("p",{},dt(r.valid_until))),
      line("标题",input(d.title,v=>d.title=v)),
      line("完整印象",textarea(d.full_text,v=>d.full_text=v,4),"只允许粗略兴趣或自愿分享的聊天习惯，不记录现实时刻与行踪。"),
      line("语义要旨",textarea(d.gist,v=>d.gist=v,3)),
      line("简短印象",input(d.trace,v=>d.trace=v)),
      line("重要性",input(d.importance,v=>d.importance=Number(v),"","number")),
      line("归纳证据（只读）",h("pre",{},JSON.stringify(r.evidence||[],null,2)))
    ],async()=>{
      await post("memories/save",d);notice("已修正归纳文本；证据和有效期未改变。","success");
      if(still(token)) await renderMemory(token);
    });
    return;
  }
  await safe(async () => {
    if (!state.sessions.length)
      throw new Error("先让插件观察或接管至少一个会话。");
    const d = r
      ? { ...r, pinned: !!r.pinned, participant_refs: (r.participant_refs || []).filter((ref) => ref !== r.subject) }
      : {
          scope: filterFor("memory").scope || state.sessions[0].scope,
          subject: "group",
          participant_refs: [],
          kind: "core",
          topic_key: "",
          title: "",
          full_text: "",
          gist: "",
          trace: "",
          importance: 80,
          pinned: false,
          valid_until: null,
        };
    const subjectBox = h("div");
    const relatedBox = h("div", { class: "identity-options" });
    let found = [], lookupQ = "", revision = 0;
    const persons = new Map((r?.participants || []).map((p) => [p.ref, p]));
    function identityLabel(p) {
      return `${p.effective_display_name || p.display_name || p.common_display_name || p.name || p.ref} · ${p.uid || p.ref}`;
    }
    const redraw = () => {
      const refs = [...new Set([d.subject, ...d.participant_refs, ...found.map((p) => p.ref)])].filter((ref) => ref !== "group");
      const list = refs.map((ref) => persons.get(ref) || { ref, name: ref });
      subjectBox.replaceChildren(
        selected(
          d.subject,
          [
            ["group", "该会话公共事实/事件"],
            ...list.map((p) => [p.ref, identityLabel(p)]),
          ],
          (v) => {
            d.subject = v;
            d.participant_refs = d.participant_refs.filter((ref) => ref !== v);
            redraw();
          },
        ),
      );
      const other = list.filter((p) => p.ref !== d.subject);
      relatedBox.replaceChildren(...(other.length ? other.map((p) => check(identityLabel(p), d.participant_refs.includes(p.ref), (yes) => {
        d.participant_refs = yes ? [...new Set([...d.participant_refs, p.ref])] : d.participant_refs.filter((ref) => ref !== p.ref);
        redraw();
      })) : [h("p", { class: "help" }, "没有其他匹配人物。") ]));
    };
    const updatePersons = async () => {
      const rev = ++revision;
      const list = (await get("users", { scope: d.scope, q: lookupQ, limit: 200 })).items || [];
      if (rev !== revision) return;
      found = list;
      for (const person of list) persons.set(person.ref, person);
      redraw();
    };
    const lookup = input("", (v) => (lookupQ = v), "昵称 / 别名 / 真实 ID / 内部代号");
    lookup.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); safe(updatePersons); } });
    await updatePersons();
    const fields = [
      line(
        "会话（作用域）",
        r
          ? h("p", { class: "mono" }, scopeName(r.scope))
          : scopeSelect(
              d.scope,
              async (v) => {
                d.scope = v;
                d.subject = "group";
                d.participant_refs = [];
                found = [];
                persons.clear();
                lookupQ = "";
                lookup.value = "";
                redraw();
                await safe(updatePersons);
              },
              false,
            ),
      ),
      line("查找人物", h("div", { class: "actions" }, lookup, btn("查找", () => safe(updatePersons), "small")), "搜索此会话内的用户；结果最多200位，已选择的关联人物保留。"),
      line("记忆对象", subjectBox),
      line("其他关联人物（可多选）", relatedBox, "个人记忆自动关联主体。多人经历可以选择其他相关群友；关联通过稳定ID保存，不依赖正文昵称。"),
      line(
        "记忆类型",
        selected(
          d.kind,
          ["core", "profile", "experience", "ordinary", "temporary"],
          (v) => (d.kind = v),
        ),
      ),
      line(
        "稳定主题键",
        input(
          d.topic_key,
          (v) => (d.topic_key = v),
          "例如 birthday，空白时使用标题",
        ),
      ),
      line(
        "标题",
        input(d.title, (v) => (d.title = v)),
      ),
      line(
        "完整事实 / 经历",
        textarea(d.full_text, (v) => (d.full_text = v), 5),
      ),
      line(
        "语义要旨（清楚/模糊时可见）",
        textarea(d.gist, (v) => (d.gist = v), 3),
        "保留主旨，不能添加未经证实的细节。",
      ),
      line(
        "最短印象",
        input(d.trace, (v) => (d.trace = v)),
      ),
      line(
        "重要程度（0-100）",
        input(d.importance, (v) => (d.importance = Number(v)), "", "number"),
      ),
      check("永久置顶", d.pinned, (v) => (d.pinned = v)),
      line(
        "临时事项有效期（ISO 日期／时间）",
        input(
          d.valid_until ? new Date(d.valid_until * 1000).toISOString() : "",
          (v) => (d.valid_until = v),
          "YYYY-MM-DDTHH:mm:ss+08:00",
        ),
      ),
    ];
    if (r?.evidence?.length)
      fields.push(
        line(
          "原始证据（只读）",
          h("pre", {}, JSON.stringify(r.evidence, null, 2)),
        ),
      );
    showDialog(r ? "编辑记忆" : "添加记忆", fields, async () => {
      await post("memories/save", d);
      notice("记忆已保存", "success");
      if (still(token)) await renderMemory(token);
    });
  });
}
async function memoryHistory(r) {
  safe(async () => {
    const v = await get("memories/versions", { id: r.id });
    const items = v.items || [];
    showDialog(
      "记忆历史 · " + r.title,
      [
        h(
          "p",
          { class: "help" },
          "修订前内容作为历史保存，不会自动当成当前事实。",
        ),
        ...(items.length
          ? items.map((x) =>
              h(
                "div",
                { class: "panel" },
                h("h3", {}, dt(x.saved) + " · " + x.reason),
                x.snapshot?.participant_refs?.length ? h("p", { class: "help" }, `关联身份：${x.snapshot.participant_refs.join("、")}`) : null,
                h("pre", {}, x.snapshot?.full_text || "暂无正文"),
              ),
            )
          : [h("div", { class: "empty" }, "暂无历史版本")]),
      ],
      null,
    );
  });
}

async function renderLearning(token) {
  await refreshSessions();
  const f = filterFor("learning");
  const sel = selected(
    f.status || "",
    [
      ["", "所有状态"],
      ...["pending", "approved", "rejected"].map((v) => [v, label(v)]),
    ],
    (v) => (f.status = v),
  );
  const res = await get("styles", f);
  if (!still(token)) return;
  const rows = (res.items || []).map((r) => [
    scopeName(r.scope),
    h(
      "div",
      {},
      h("strong", {}, r.pattern),
      h("div", { class: "help" }, label(r.kind)),
    ),
    r.meaning,
    h("div", {}, r.example),
    `${Number(r.confidence).toFixed(2)} · ${r.count} 次`,
    badge(
      r.status,
      r.status === "approved"
        ? "good"
        : r.status === "rejected"
          ? "bad"
          : "warn",
    ),
    h(
      "div",
      { class: "actions" },
      btn("编辑", () => editStyle(r), "small"),
      r.status !== "approved"
        ? btn("批准", () => styleAction(r, "save", "approved"), "small")
        : null,
      r.status !== "rejected"
        ? btn("拒绝", () => styleAction(r, "save", "rejected"), "small")
        : null,
      btn("回滚", () => styleAction(r, "undo"), "small"),
      btn("删除", () => styleAction(r, "delete"), "small danger"),
    ),
  ]);
  page.replaceChildren(
    panel(
      "表达模式与黑话",
      h(
        "p",
        { class: "help" },
        "默认需要审核；仅批准的模式进入回复参考。学习结果不会改写人格，反复检测到同一模式只更新支持次数。",
      ),
      h(
        "div",
        { class: "toolbar" },
        btn("学习一批", () => runAction("style_flush", f.scope)),
      ),
      filterToolbar("learning", [sel]),
      table(
        ["会话", "表达", "含义", "示例", "置信度 / 证据批数", "状态", "操作"],
        rows,
      ),
      pager(f, rows.length, () => pageChange("learning")),
    ),
  );
}
function styleAction(r, action, status) {
  const token = state.epoch;
  safe(async () => {
    if (action === "delete" && !confirm(`删除表达「${r.pattern}」？`)) return;
    await post("styles/action", {
      id: r.id,
      action,
      status,
      confirm: action === "delete",
    });
    if (still(token)) await renderLearning(token);
  });
}
function editStyle(r) {
  const d = { ...r },
    token = state.epoch;
  showDialog(
    "编辑表达",
    [
      line(
        "表达",
        input(d.pattern, (v) => (d.pattern = v)),
      ),
      line(
        "含义",
        textarea(d.meaning, (v) => (d.meaning = v)),
      ),
      line(
        "示例",
        textarea(d.example, (v) => (d.example = v)),
      ),
      line(
        "状态",
        selected(
          d.status,
          ["pending", "approved", "rejected"],
          (v) => (d.status = v),
        ),
      ),
    ],
    async () => {
      await post("styles/action", { ...d, action: "save" });
      if (still(token)) await renderLearning(token);
    },
  );
}

async function renderUsage(token) {
  const f = filterFor("usage");
  f.days ||= 15;
  const res = await get("usage", { days: f.days });
  if (!still(token)) return;
  const rows = res.items || [];
  const total = rows.reduce(
    (s, r) => s + r.input_other + r.input_cached + r.output,
    0,
  );
  const cache = rows.reduce((s, r) => s + r.input_cached, 0);
  const calls = rows.reduce((s, r) => s + r.calls, 0);
  const estimated = rows.reduce((s, r) => s + r.estimated, 0);
  const m = {};
  for (const r of rows)
    m[r.module] =
      (m[r.module] || 0) + r.input_other + r.input_cached + r.output;
  const bars = Object.entries(m)
    .sort((a, b) => b[1] - a[1])
    .map(([k, v]) =>
      h(
        "div",
        { class: "bar-row" },
        h("span", {}, label(k)),
        h(
          "div",
          {},
          h("i", { style: `width:${total ? (v / total) * 100 : 0}%` }),
        ),
        h("span", {}, fmt(v)),
      ),
    );
  page.replaceChildren(
    h(
      "div",
      { class: "cards" },
      card("合计 Token", fmt(total)),
      card("缓存输入", fmt(cache)),
      card("API 调用", fmt(calls)),
      card("估算调用", fmt(estimated), "未收到供应商 Token 用量"),
    ),
    panel(
      "模块分布",
      h(
        "div",
        { class: "toolbar" },
        h("label", { class: "help" }, "时间范围"),
        selected(
          String(f.days),
          [
            ["7", "过去7天"],
            ["15", "过去15天"],
            ["30", "过去30天"],
            ["90", "过去90天"],
          ],
          (v) => {
            f.days = Number(v);
            pageChange("usage");
          },
        ),
      ),
      ...bars,
    ),
    panel(
      "每日用量明细",
      h("p", { class: "help" }, res.note || ""),
      table(
        [
          "日期",
          "模块",
          "Provider",
          "请求 / 错误",
          "未缓存输入",
          "缓存输入",
          "输出",
          "估算调用",
          "平均耗时",
          "估算费用 CNY",
        ],
        rows.map((r) => [
          r.day,
          label(r.module),
          r.provider,
          `${r.calls} / ${r.failures}`,
          fmt(r.input_other),
          fmt(r.input_cached),
          fmt(r.output),
          r.estimated,
          `${(r.elapsed_ms / Math.max(1, r.calls) / 1000).toFixed(2)}s`,
          r.cost_cny_estimate ?? "未配置",
        ]),
      ),
    ),
  );
}

async function renderLogs(token) {
  const f = filterFor("logs");
  f.limit = 400;
  const r = await get("logs", {
    day: f.day || "",
    q: f.q || "",
    level: f.level || "",
    module: f.module || "",
    limit: f.limit,
  });
  if (!still(token)) return;
  if (!f.day) f.day = (r.days || [])[0] || "";
  const days = [["", "今日"], ...(r.days || []).map((x) => [x, x])];
  const d = selected(f.day, days, (v) => (f.day = v));
  const level = selected(
    f.level || "",
    [["", "全部等级"], ...["DEBUG", "INFO", "WARNING", "ERROR"]],
    (v) => (f.level = v),
  );
  const mod = selected(
    f.module || "",
    [
      ["", "所有模块"],
      ...[
        "CORE",
        "CHAT",
        "DECISION",
        "REPLY",
        "MEMORY",
        "LEARNING",
        "TOKEN",
        "TOOL",
        "VISION",
        "DOTA",
        "BALANCE",
        "CONFIG",
        "USER",
        "PERSONA",
        "INTERRUPT",
        "RESEARCH",
        "RELAY",
      ],
    ],
    (v) => (f.module = v),
  );
  const q = input(f.q, (v) => (f.q = v), "搜索日志");
  q.addEventListener("keydown", (e) => {
    if (e.key === "Enter") pageChange("logs");
  });
  const rows = (r.items || []).map((x) => [
    h("span",{title:x.time},String(x.time||"—").replace("T"," ").replace(/\+08:00$/,"")),
    badge(
      x.level,
      x.level === "ERROR" ? "bad" : x.level === "WARNING" ? "warn" : "",
    ),
    x.module,
    x.code,
    x.scope || "—",
    x.reason || "",
    h(
      "span",
      { class: "mono" },
      Object.entries(x)
        .filter(
          ([k]) =>
            !["time", "level", "module", "code", "scope", "reason"].includes(k),
        )
        .map(([k, v]) => `${k}=${v}`)
        .join(" · "),
    ),
  ]);
  page.replaceChildren(
    panel(
      "独立日志",
      h(
        "p",
        { class: "help" },
        "仅列事件与简短决策原因，不存放完整提示词或密钥。每日/分片日志按配置自动清理。查看最近最多400条匹配记录。",
      ),
      h(
        "div",
        { class: "toolbar log-filters" },
        d,
        level,
        mod,
        h("div", { class: "grow" }, q),
        btn("搜索", () => pageChange("logs")),
        btn("立即维护", () => runAction("maintenance"), "small"),
      ),
      table(
        ["北京时间", "等级", "模块", "事件", "会话短标识", "原因", "统计"],
        rows,
        "logs-table",
      ),
    ),
  );
}

function sourceLink(s){
  const text=(s.title||"来源")+" · "+(s.date||"未知日期");
  if(!/^https:\/\/(?:www\.opendota\.com\/matches\/\d+|api\.opendota\.com\/api\/heroStats|store\.steampowered\.com\/news\/app\/\d+\/view\/\d+)$/.test(s.url||""))
    return h("span",{},text);
  return h("a",{href:s.url,target:"_blank",rel:"noopener noreferrer"},text);
}
async function renderResearch(token){
  const f=filterFor("research");
  const result=await get("research",{q:f.q,offset:f.offset,limit:f.limit});
  if(!still(token)) return;
  const settings=result.settings||{};
  const overview=result.overview||{};
  const q=input(f.q,v=>f.q=v,"公开主题或见闻关键词");
  const go=()=>{f.offset=0;pageChange("research");};
  q.addEventListener("keydown",e=>{if(e.key==="Enter")go();});
  const notes=(result.items||[]).map(r=>{
    const sourceMap=new Map((r.sources||[]).map(s=>[s.id,s]));
    const citations=p=>(p.refs||[]).map(ref=>sourceMap.has(ref)?h("span",{class:"help citation"},sourceLink(sourceMap.get(ref))):null);
    const op=r.opinion||{};
    const action=async act=>safe(async()=>{
      if(act==="delete"&&!confirm("删除本条公开见闻？"))return;
      await post("research/action",{id:r.id,action:act,confirm:act==="delete"});
      if(still(token))await renderResearch(token);
    });
    return h("article",{class:"panel research-note"},
      h("div",{class:"statusline"},badge(r.state),r.stale?badge("已过有效期","warn"):badge("有效期内","good"),badge(r.source)),
      h("h3",{},r.topic),
      h("p",{class:"help"},r.persona+" · 查阅 "+dt(r.created)+" · 有效期至 "+dt(r.expires)),
      h("div",{class:"label"},"资料事实"),
      ...(r.facts||[]).map(p=>h("div",{class:"research-fact"},h("p",{},p.text),citations(p))),
      op.text?h("div",{class:"research-opinion"},h("div",{class:"label"},"角色观点（非事实）"),h("p",{},op.text),citations(op)):null,
      h("div",{class:"actions"},btn(r.state==="archived"?"恢复":"存档",()=>action(r.state==="archived"?"restore":"archive"),"small"),
        btn("删除",()=>action("delete"),"danger small")));
  });
  const runs=(result.runs||[]).map(r=>[dt(r.started),r.persona,label(r.source),r.topic||"—",badge(r.state),r.reason||"—",fmt(r.tokens)]);
  const sources=(result.source_status||[]).map(s=>[
    s.label,
    label(s.last_source),
    badge(({ok:"上次可用",failed:"连接/解析失败",empty:"暂无资料",retry_due:"待重试",untested:"尚未检测"})[s.state]||s.state,
      s.state==="ok"?"good":s.state==="failed"?"bad":s.state==="untested"?"":"warn"),
    dt(s.checked_at),
    s.cooling?dt(s.retry_after):"—",
    s.elapsed_ms==null?"—":fmt(s.elapsed_ms)+" ms",
    s.reason||"—",
  ]);
  const actions=result.actions||{};
  const busy=["research_once","research_probe"].some(k=>actions[k]?.state==="running");
  const start=(name)=>safe(async()=>{
    await post("action",{name});
    if(still(token)){
      await renderResearch(token);
      notice(name==="research_probe"?"已提交网络检测，不消耗模型 Token；稍后刷新查看结果。":"已提交学习任务；受每日次数和预算约束，请稍后刷新。","success");
    }
  });
  const actionStatus=["research_once","research_probe"].map(key=>{
    const a=actions[key];
    if(!a)return null;
    return h("div",{class:"help"},key==="research_probe"?"最近检测：":"最近执行：",
      badge(a.state==="running"?"进行中":a.state==="error"?"失败":a.result?.state||a.state),
      a.result?.reason?" "+a.result.reason:"",
      a.result?.requests!=null?" · "+a.result.requests+" 次公开接口请求":"");
  });
  page.replaceChildren(
    h("div",{class:"cards"},card("今日选题次数",fmt(overview.today_runs)+" / "+fmt(settings.daily_topics),"同一任务的重试、备用选题不另计；最终失败也计一次"),
      card("今日自主学习 Token",fmt(result.tokens)+" / "+fmt(settings.daily_tokens),"独立于聊天但也计入全局预算"),
      card("公开见闻",fmt(overview.notes),settings.enabled?"自主学习已开启":"未启用")),
    panel("自主查阅",
      h("p",{class:"help"},"肥鱼自行选择允许的公开游戏主题。官方 RSS/API 可切换，失败的来源暂时冷却；最多一个备用主题。不会定时发群，不向公共数据接口发送群友聊天内容。"),
      h("div",{class:"toolbar"},
        h("button",{type:"button",class:"button primary",disabled:busy,onclick:()=>start("research_once")},"执行一次自主学习"),
        h("button",{type:"button",class:"button secondary",disabled:busy,onclick:()=>start("research_probe")},"检测数据源"),
        btn("前往配置", () => goToConfig("research"))),
      actionStatus),
    panel("数据源状态",h("p",{class:"help"},"显示最近一次真实读取/检测的状态，不代表永久在线。手动检测仅访问已配置的固定公开接口，不占每日学习次数或消耗 Token。Steam 接口状态由两个游戏共享，并标注实际测试的游戏。"),
      table(["数据源","最近使用","状态","最近检测","下次可重试","耗时","原因"],sources)),
    panel("公开知识笔记",h("div",{class:"toolbar"},h("div",{class:"grow"},q),btn("查询",go)),
      notes.length?notes:h("p",{class:"empty"},"还没有自主学习笔记。"),pager(f,notes.length,()=>pageChange("research"))),
    panel("近期学习任务",table(["时间","人格","来源","主题","状态","原因","Token"],runs)));
}
async function renderRelay(token){
  await refreshSessions();
  const r=await get("relay");
  if(!still(token))return;
  page.replaceChildren(panel("受控转达",
    h("p",{class:"help"},"只在已授权的私聊使用。群别名必须对应同一平台实例、同一人格内已观察的白名单群。清楚指定正文可直接转达；要求润色时，必须私聊确认草稿。没有接收回执时不重复发送。"),
    h("pre",{},"肥鱼，去刀塔群发：今晚九点开黑\n肥鱼，去刀塔群组织语言发：今晚九点开黑\n确认发送 任务编号\n取消发送"),
    btn("前往转达配置", () => goToConfig("relay"))),
    panel("最近转达记录（不展示私聊正文）",table(["时间","来源会话","目标别名","状态","任务编号"],
      (r.items||[]).map(x=>[dt(x.created),scopeName(x.scope),x.alias,badge(x.status==="pending"?"待确认":x.status),String(x.id)]))));
}

$("nav").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-module]");
  if (b) openModule(b.dataset.module);
});
$("refresh").addEventListener("click", refresh);
$("dialogClose").addEventListener("click", () => $("dialog").close());
$("dialogCancel").addEventListener("click", () => $("dialog").close());
window.addEventListener("beforeunload", (e) => {
  if (state.dirty) {
    e.preventDefault();
    e.returnValue = "";
  }
});
(async () => {
  try {
    if (!bridge)
      throw new Error(
        "本页面需要 AstrBot 4.29.x 的插件视图，请从 AstrBot 的插件详情进入。",
      );
    if (bridge.ready) await bridge.ready();
    await pageChange("overview");
  } catch (e) {
    page.replaceChildren(h("div", { class: "warning" }, e.message));
    notice(e.message, "error");
  }
})();
