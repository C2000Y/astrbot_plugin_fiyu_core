// DOM-level management page smoke test, for environments without a browser process.
// Requires Node.js + jsdom; all requests are local mocks.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { execFileSync } = require("node:child_process");
const { JSDOM } = require("jsdom");
const plugin = path.resolve(__dirname, "..");
const src = path.join(plugin, "views/dashboard");
const raw = JSON.parse(
  execFileSync(
    process.env.FIYU_TEST_PYTHON || "python",
    [
      "-c",
      'import json; from astrbot_plugin_fiyu_core.core.config import SCHEMA, defaults; print(json.dumps({"config":defaults(),"schema":SCHEMA}))',
    ],
    { cwd: path.dirname(plugin), encoding: "utf8" },
  ),
);
const copy = (x) => JSON.parse(JSON.stringify(x));
const defaults = copy(raw.config);
const cfg = {
  ...raw,
  revision: "1",
  backups: [],
  options: {
    chat_provider: [{ id: "deepseek", label: "DeepSeek Flash" }],
    embedding_provider: [],
    persona: [],
    knowledge_bases: [],
  },
};
const session = {
  scope: "scope",
  kind: "group",
  location: "test-group",
  persona: "fish",
};
const user = {
  scope: "scope",
  uid: "user-test",
  ref: "Utest",
  name: "Alice",
  display_name: "",
  note: "",
  role: "member",
  affinity: 50,
  blocked: 0,
  deny_keywords: [],
  deny_reply: "不回答",
  historical_aliases: ["Old Alice"],
  custom_aliases: [],
};
const user2 = { ...user, uid: "friend-test", ref: "Ufriend", name: "Bob", historical_aliases: [] };
const memory = {
  id: 1,
  scope: "scope",
  subject: "Utest",
  subject_name: "Alice",
  kind: "core",
  title: "生日",
  topic_key: "birthday",
  full_text: "生日7月22日",
  visible_text: "生日7月22日",
  gist: "生日7月",
  trace: "生日",
  state: "active",
  clarity: "vivid",
  importance: 90,
  confidence: 0.99,
  updated: 1780000000,
  pinned: 0,
  valid_until: null,
  evidence: [],
  participant_refs: [user.ref],
  participants: [user],
};
const style = {
  id: 1,
  scope: "scope",
  kind: "slang",
  pattern: "紫猫",
  meaning: "虚无之灵",
  example: "紫猫开团",
  confidence: 0.95,
  count: 2,
  status: "pending",
};
const dom = new JSDOM(fs.readFileSync(path.join(src, "index.html"), "utf8"), {
  url: "http://local.test/",
  runScripts: "outside-only",
  pretendToBeVisual: true,
});
const w = dom.window;
const doc = w.document;
const calls = [];
w.confirm = () => true;
if (!w.HTMLDialogElement.prototype.showModal)
  w.HTMLDialogElement.prototype.showModal = function () {
    this.setAttribute("open", "");
  };
if (!w.HTMLDialogElement.prototype.close)
  w.HTMLDialogElement.prototype.close = function () {
    this.removeAttribute("open");
  };
w.AstrBotPluginView = {
  ready: async () => {},
  apiGet: async (p) => {
    if (p === "config") return copy(cfg);
    if (p === "config/defaults") return { config: copy(defaults) };
    if (p === "overview")
      return {
        mode: cfg.config.access.mode,
        today: "2026-11-23",
        totals: {
          today: { received: 12 },
          sessions: 1,
          people: 1,
          memories: { active: 1 },
          styles: { pending: 1 },
        },
        usage: { tokens: 123 },
        budget: 0,
        workers: 0,
        queue: 0,
        actions: {},
      };
    if (p === "sessions") return { items: [session] };
    if (p === "users/identities") return {items:[user,user2].map(u=>({
      identity_id:"G-"+u.uid,uid:u.uid,name:u.name,display_name:"",revision:1,
      platform:"qq_official",platform_id:"test",custom_aliases:[],historical_aliases:u.historical_aliases,
      membership_count:1,group_count:1,private_count:0,blocked_count:0,memberships:[{...u,...session}],
      relationships:[{persona:session.persona,role:"member",affinity:50,familiarity:"初识",familiarity_override:"auto",revision:1}]
    }))};
    if (p === "users") return { items: [user, user2] };
    if (p === "memories") return { items: [memory] };
    if (p === "memories/versions") return { items: [] };
    if (p === "styles") return { items: [style] };
    if(p === "research") return {items:[],runs:[],settings:cfg.config.research,overview:{today_runs:0,notes:0},tokens:0};
    if(p === "relay") return {items:[]};
    if (p === "usage")
      return {
        items: [
          {
            day: "2026-11-23",
            module: "reply",
            provider: "deepseek",
            calls: 1,
            failures: 0,
            input_other: 10,
            input_cached: 20,
            output: 3,
            estimated: 0,
            elapsed_ms: 100,
          },
        ],
      };
    if (p === "logs")
      return {
        days: ["2026-11-23"],
        items: [
          { time: "test", level: "INFO", module: "DECISION", code: "reply" },
        ],
      };
    throw Error("Unknown " + p);
  },
  apiPost: async (p, v) => {
    calls.push([p, copy(v)]);
    if (p === "config/save") {
      cfg.config = copy(v.config);
      cfg.revision = "2";
      cfg.backups = ["backup.json"];
      return { ok: true, ...copy(cfg) };
    }
    if (p === "users/save") Object.assign(user, v);
    if (p === "styles/action") Object.assign(style, v);
    return { ok: true };
  },
};
const $ = (s) => doc.querySelector(s);
const btn = (text) =>
  [...doc.querySelectorAll("button")].find(
    (x) => x.textContent.trim() === text,
  );
function fill(node, value) {
  assert.ok(node);
  node.value = value;
  node.dispatchEvent(new w.Event("input", { bubbles: true }));
}
function tab(name) {
  const modules={overview:"overview",config:"config",users:"people",memory:"people",
    learning:"learning",research:"learning",relay:"relay",usage:"operations",logs:"operations"};
  const group=$('[data-module="'+modules[name]+'"]');
  if(group.getAttribute("aria-current")!=="page")group.click();
  const leaf=$('#sectionTabs [data-tab="'+name+'"]');
  if(leaf)leaf.click();
}
async function until(fn) {
  for (let i = 0; i < 100; i++) {
    if (fn()) return;
    await new Promise((r) => setTimeout(r, 10));
  }
  throw Error(
    "Timed out: " +
      doc.getElementById("notice").textContent +
      " / " +
      doc.getElementById("page").textContent.slice(0, 300),
  );
}
async function submit() {
  doc
    .querySelector("#dialogForm")
    .dispatchEvent(new w.Event("submit", { bubbles: true, cancelable: true }));
  await until(() => !$("#dialog").open);
}
async function main() {
  w.scrollTo=()=>{};
  w.eval(fs.readFileSync(path.join(src, "app.js"), "utf8"));
  await until(() => $(".cards"));
  tab("config");
  await until(() => $("#saveConfig"));
  const search = $('input[placeholder="搜索名称、字段、描述…"]');
  fill(search,"记忆");
  assert.ok($("#page").textContent.includes("models.memory_provider"));
  const category=$(".category-picker select");
  assert.equal(category.options.length,7);
  category.value="memory";
  category.dispatchEvent(new w.Event("change",{bubbles:true}));
  assert.ok([...doc.querySelectorAll(".field .key")].every(k=>/^(memory|habits|learning|research)\./.test(k.textContent)));
  fill(search,"");
  assert.deepEqual([...doc.querySelectorAll(".config-section")].map(n=>n.id),
    ["memory","habits","learning","research"].map(k=>"config-section-"+k));
  category.value="";
  category.dispatchEvent(new w.Event("change",{bubbles:true}));
  const paths=[...doc.querySelectorAll(".field .key")].map(k=>k.textContent);
  assert.deepEqual(paths.sort(),raw.schema.flatMap(g=>g.fields.map(f=>g.key+"."+f.key)).sort());
  fill(search, "budget.pricing");
  await until(() => $(".field textarea"));
  fill($(".field textarea"), "{broken");
  fill(search, "logs.retention_days");
  assert.equal(doc.querySelectorAll(".field").length, 1);
  $("#saveConfig").click();
  await until(() => $("#notice").textContent.includes("budget.pricing"));
  assert.equal(calls.length, 0);
  fill(search, "pricing");
  assert.equal($(".field textarea").value, "{broken");
  fill($(".field textarea"), "{}");
  fill(search, "日志保留天数");
  fill($(".field input[type=number]"), "20");
  btn("JSON 编辑").click();
  await until(() => $("#jsonEditor"));
  assert.equal(JSON.parse($("#jsonEditor").value).logs.retention_days, 20);
  const newcfg = JSON.parse($("#jsonEditor").value);
  newcfg.access.mode = "observe";
  newcfg.access.allowed_groups = ["test-group"];
  fill($("#jsonEditor"), "{broken");
  $("#saveConfig").click();
  await until(() => !$("#notice").hidden);
  assert.equal(calls.length, 0);
  fill($("#jsonEditor"), JSON.stringify(newcfg, null, 2));
  $("#saveConfig").click();
  await until(
    () => cfg.revision === "2" && $("#modeText").textContent === "观察模式",
  );
  assert.equal(cfg.config.logs.retention_days, 20);
  tab("users");
  await until(() => btn("展开会话"));
  btn("展开会话").click();
  btn("会话设置").click();
  await until(() => $("#dialog").open);
  const area = (label) => [...doc.querySelectorAll("#dialog .form-line")].find(x => x.textContent.includes(label)).querySelector("textarea");
  fill(area("备注 / 称呼规则"), "测试备注");
  fill(area("自定义别名"), "阿丽丝");
  await submit();
  await until(() => user.note === "测试备注");
  tab("memory");
  await until(() => btn("查看/编辑"));
  btn("查看/编辑").click();
  await until(() => $("#dialog").open);
  assert.ok($("#dialogBody").textContent.includes("完整事实"));
  [...doc.querySelectorAll("#dialog .identity-options .check-line")].find(x => x.textContent.includes("friend-test")).querySelector("input").click();
  await submit();
  assert.ok(calls.some(([p, v]) => p === "memories/save" && v.participant_refs.includes("Ufriend")));
  tab("learning");
  await until(() => btn("批准"));
  btn("批准").click();
  await until(() => style.status === "approved");
  tab("research");
  await until(()=>$("#page").textContent.includes("公开知识笔记"));
  tab("relay");
  await until(()=>$("#page").textContent.includes("受控转达"));
  tab("usage");
  await until(() => $("#page").textContent.includes("每日用量明细"));
  tab("logs");
  await until(() => $("#page").textContent.includes("独立日志"));
  tab("overview");
  await until(() => $(".cards"));
  assert.equal($("#page").textContent.includes("null"), false);
  console.log(
    "DOM management UI: six grouped modules and all nine pages, category/keyword search, invalid JSON prevention, save, user edit, memory edit dialog and style approval passed.",
  );
}
main()
  .catch((e) => {
    console.error(e);
    process.exitCode = 1;
  })
  .finally(() => dom.window.close());
