// Focused identity UI data-flow test. Tiny DOM fixture; no browser, network or dependencies.
// Covers actual view functions, event handlers and request payloads, not visual layout.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

class NodeFixture {
  constructor(tag, text = "") {
    this.tag = tag;
    this.children = [];
    this.text = text;
    this.events = {};
    this.value = "";
  }
  get textContent() { return this.text + this.children.map(x => x.textContent).join(""); }
  set textContent(v) { this.text = String(v); this.children = []; }
  get dataset() {
    return Object.fromEntries(Object.entries(this).filter(([k])=>k.startsWith("data-")).map(([k,v])=>[k.slice(5),v]));
  }
  get classList() {
    return {toggle:(name,on)=>{
      const classes=new Set((this.className||"").split(/\s+/).filter(Boolean));
      if(on)classes.add(name);else classes.delete(name);
      this.className=[...classes].join(" ");
    }};
  }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.text = ""; this.children = children; }
  addEventListener(k, fn) { this.events[k] = fn; }
  setAttribute(k, v) { this[k] = v; }
  removeAttribute(k) { delete this[k]; }
  querySelectorAll(selector) {
    if(selector==="button[data-module]")return this.children.filter(n=>n.tag==="button"&&n["data-module"]);
    throw Error("unsupported fixture selector "+selector);
  }
  setCustomValidity(v) { this.validationMessage = v; }
  fire(k, v) {
    if (k === "change" && this.type === "checkbox") this.checked = v;
    else if (v !== undefined) this.value = v;
    return this.events[k]?.({target: this, preventDefault() {}});
  }
}
const ids = new Map();
const document = {
  getElementById(id) { if (!ids.has(id)) ids.set(id, new NodeFixture("div")); return ids.get(id); },
  createElement(tag) { return new NodeFixture(tag); },
  createTextNode(text) { return new NodeFixture("#text", String(text)); },
};
const copy = x => JSON.parse(JSON.stringify(x));
const scope = "scope-1";
const sessions = [{scope, kind: "group", location: "group-1", persona: "fish"}, {scope: "scope-2", kind: "group", location: "group-2", persona: "fish"},
  {scope:"scope-private",kind:"private",location:"test-a",persona:"fish"}];
const people = [
  {scope, uid: "test-a", ref: "Uaaa", name: "小蓝", display_name: "", historical_aliases: ["旧蓝"], custom_aliases: [], role: "member", affinity: 50, blocked: 0, note: "", deny_keywords: [], deny_reply: "不回答"},
  {scope, uid: "test-b", ref: "Ubbb", name: "小白", display_name: "", historical_aliases: ["旧白"], custom_aliases: [], role: "member", affinity: 50, blocked: 0, note: "", deny_keywords: [], deny_reply: "不回答"},
  {scope: "scope-2", uid: "test-a", ref: "Uother", name: "小蓝", display_name: "", historical_aliases: [], custom_aliases: []},
  {scope: "scope-private", uid:"test-a",ref:"Uprivate",name:"私聊蓝",display_name:"",note:"私聊限定备注",historical_aliases:[],custom_aliases:[]},
];
const profiles=()=>["test-a","test-b"].map(uid=>{
  const members=people.filter(p=>p.uid===uid).map(p=>({...p,...sessions.find(s=>s.scope===p.scope)}));
  return {identity_id:"G-"+uid,uid,name:members[0].name,display_name:"",revision:1,platform:"qq_official",platform_id:"test-bot",
    custom_aliases:[],historical_aliases:members.flatMap(m=>m.historical_aliases),membership_count:members.length,
    group_count:members.filter(m=>m.kind==="group").length,private_count:members.filter(m=>m.kind==="private").length,
    blocked_count:0,memberships:members,relationships:[{persona:"fish",revision:1,role:"member",affinity:50,
      familiarity_override:"auto",familiarity:"初识",interactions:0,recent_interactions:0,membership_count:members.length}]};
});
const memory = {id: 1, scope, subject: "Uaaa", subject_name: "小蓝", kind: "core", title: "生日", topic_key: "birthday", full_text: "生日7月22日", visible_text: "生日7月22日", gist: "生日7月", trace: "生日", importance: 95, confidence: 0.96, updated: 1780000000, state: "active", clarity: "vivid", pinned: 0, participant_refs: ["Uaaa"], participants: [people[0]], evidence: []};
const calls = [];
const bridge = {
  async apiGet(p, q) {
    if (p === "overview") return {
      mode: "active", today: "2026-11-23", budget: 0, totals: {}, usage: {}, actions: {},
      dota: {state: "部分同步成功", sources: {
        public: {state: "同步成功", date: "2026-11-23", weighted_win_rate_pct: 50.00001},
        weekly: {state: "同步失败，保留旧数据", date: "2026-11-22", reason: "选取率总和异常",
          failure_kind: "invalid_structure", last_attempt: "2026-11-23T09:00:00+08:00",
          next_attempt: "2026-11-23T13:00:00+08:00", next_manual_attempt: "2026-11-23T09:01:00+08:00"},
        matchups: {state: "今日已同步，部分配对缺失", date: "2026-11-23",
          expected_pairs: 56007, valid_pairs: 56006, coverage_pct: 99.9982, last_complete_date: "2026-11-22"},
      }},
      dota_samples: [{source:"opendota_pro",state:"有已验证样本"}],
      dota_knowledge: {state:"同步装备资料",patch:"7.42",hero:{published:true,done:127,total:127,
          latest_published:"7.42",versions:1},item:{published:false,done:160,total:525,metadata_only:3,
          latest_published:"7.41f",versions:1}},
    };
    if (p === "sessions") return {items: copy(sessions)};
    if (p === "users/identities") return {items: copy(profiles().filter(u=>!q?.scope||u.memberships.some(m=>m.scope===q.scope)))};
    if (p === "users") return {items: copy(people.filter(u => (!q?.scope || u.scope === q.scope) && (!q?.q || [u.uid, u.name, ...u.historical_aliases].some(n => n.includes(q.q)))))};
    if (p === "memories") return {items: [copy(memory)]};
    if (p === "personas/merge_options") return {personas: [
      {id: "old", sessions: [{scope: "old-scope", platform: "qq_official", platform_id: "bot", kind: "group", location: "group-1"}]},
      {id: "new", sessions: [{scope: "new-scope", platform: "qq_official", platform_id: "bot", kind: "group", location: "group-1"}]},
    ]};
    if (p === "research") return {
      settings:{enabled:true,daily_topics:1,daily_tokens:10000},
      tokens:280, overview:{today_runs:1,notes:1},
      items:[{id:5,persona:"fish",state:"active",source:"dota_news",topic:"测试版本",
        created:1780000000,expires:1782600000,stale:false,
        facts:[{text:"官方测试事实",refs:["s1"]}],opinion:{text:"我的测试观点",refs:["s1"]},
        sources:[{id:"s1",title:"测试公告",date:"2026-05-28",url:"https://store.steampowered.com/news/app/570/view/123"}]}],
      runs:[{started:1780000000,persona:"fish",topic:"测试版本",state:"completed",tokens:280}],
      source_status:[
        {key:"steam_rss",label:"Steam 官方 RSS",last_source:"cs2_news",state:"ok",elapsed_ms:234,checked_at:1780000000},
        {key:"steam_api",label:"Steam 新闻 API",last_source:"cs2_news",state:"failed",cooling:true,
         retry_after:1780003600,checked_at:1780000000,reason:"TimeoutError（请求超时）",elapsed_ms:30000},
        {key:"local_pro_sample",label:"本地职业比赛样本",last_source:"dota_pro_cache",state:"ok",
         reason:"近 30 天 2 局有效职业样本",checked_at:1780000000},
        {key:"cs2_esports",label:"CS2 结构化赛事",state:"unavailable",
         reason:"暂无已验证的授权赛事接口。赛事动态可查 Steam 官方公告。"},
      ],
    };
    if (p === "relay") return {items:[{id:6,scope,alias:"刀塔群",status:"pending",created:1780000000}]};
    if(p==="logs")return {days:["2026-11-23"],items:[{time:"测试",level:"INFO",module:"RESEARCH",code:"source_ok"}]};
    throw Error("unexpected GET " + p);
  },
  async apiPost(p, value) {
    calls.push([p, copy(value)]);
    if (p === "config/save") return {config: copy(value.config), revision: "saved-r2", backups: []};
    if(p==="users/legacy_preview")return {revision:"preview-r1",rows:[
      {id:1,changes:{full_text:{before:"老大生日",after:"小蓝生日"}}},
      {id:2,changes:{full_text:{before:"老大喜欢游戏",after:"小蓝喜欢游戏"}}}
    ]};
    if(p==="users/legacy_apply")return {ok:true,changed:value.selected.length};
    if(p==="personas/merge_preview")return {
      revision: "digest-1", sessions: [{scope: "old-scope", platform: "qq_official", platform_id: "bot", kind: "group", location: "group-1"}],
      items: [
        {id: "people:A", kind: "people", status: "copy", scope: "old-scope", title: "人物"},
        {id: "memories:1", kind: "memories", status: "conflict", scope: "old-scope", title: "事实",
          source: {title: "事实", full_text: "旧事实", gist: "旧摘要", trace: "旧印象"},
          target: {title: "事实", full_text: "新事实", gist: "新摘要", trace: "新印象"}},
      ], skipped: {memory_permission: 1},
    };
    if(p==="personas/merge_apply")return {ok:true, backup:"merge-fixture", counts:{people:1, memories:1}, conflicts_resolved:1};
    return {ok:true};
  },
};
const context = vm.createContext({document, Node: NodeFixture, window: {AstrBotPluginView: bridge}, console, confirm: () => true});
const source = fs.readFileSync(path.join(__dirname, "../views/dashboard/app.js"), "utf8");
const beforeBoot = source.indexOf('$("nav").addEventListener("click"');
assert.ok(beforeBoot > 0);
vm.runInContext(source.slice(0, beforeBoot) + `
showDialog = (title, fields, submit) => { $("dialog").open = true; globalThis.dialog = {title, fields, submit}; };
globalThis.ui = {state, renderOverview, renderUsers, renderMemory, editUser, editGlobalUser, editMemory,
  drawConfig, currentConfig, saveConfig, renderResearch, renderRelay, editLegacyTitles, pageChange, openModule, modules};
`, context);

function field(title) {
  const f = context.dialog.fields.find(n => n.children[0]?.textContent === title);
  assert.ok(f, "Missing field: " + title);
  return f.children[1];
}
function findNode(node, condition) {
  if(condition(node))return node;
  for(const child of node.children) {
    const found=findNode(child,condition);
    if(found)return found;
  }
  return undefined;
}
function allNodes(node,condition) {
  return [...(condition(node)?[node]:[]),...node.children.flatMap(child=>allNodes(child,condition))];
}
async function main() {
  const {ui} = context;
  await ui.renderOverview(0);
  const overview = ids.get("page").textContent;
  assert.ok(overview.includes("OpenDota · 公开/段位胜率"));
  assert.ok(overview.includes("加权 50.00%"));
  assert.ok(overview.includes("Valve · 分组周统计"));
  assert.ok(overview.includes("2026-11-22（非今日）"));
  assert.ok(overview.includes("选取率总和异常"));
  assert.ok(overview.includes("最近尝试 2026-11-23 09:00:00+08:00"));
  assert.ok(overview.includes("自动重试最早 2026-11-23 13:00:00+08:00"));
  assert.ok(overview.includes("再次手动同步最早 2026-11-23 09:01:00+08:00"));
  assert.ok(overview.includes("Valve · 对抗/同队"));
  assert.ok(overview.includes("今日已同步，部分配对缺失"));
  assert.ok(overview.includes("99.9982%"));
  assert.ok(overview.includes("最近完整快照 2026-11-22"));
  assert.ok(overview.includes("OpenDota 职业样本"));
  assert.ok(overview.includes("同步 Dota 统计"));
  assert.ok(overview.includes("Dota 版本资料"));
  assert.ok(overview.includes("127 / 127"));
  assert.ok(overview.includes("160 / 525"));
  assert.ok(overview.includes("仅目录 3"));
  assert.ok(overview.includes("可用版本 7.41f"));
  assert.ok(overview.includes("同步 Dota 版本资料"));
  ui.state.sessions = sessions;
  await ui.renderUsers(0);
  assert.ok(ids.get("page").textContent.includes("旧蓝"));
  const cards=allNodes(ids.get("page"),n=>n.className==="identity-card");
  assert.equal(cards.length,2);
  const alice=cards.find(n=>n["data-user"]==="G-test-a");
  assert.ok(alice.textContent.includes("3 个会话")&&alice.textContent.includes("2 群 · 1 私聊"));
  const members=findNode(alice,n=>n.className==="identity-memberships");
  assert.equal(members.hidden,true);
  const toggle=findNode(alice,n=>n.tag==="button"&&n.textContent==="展开会话");
  toggle.fire("click");
  assert.equal(members.hidden,false);
  assert.equal(toggle["aria-expanded"],"true");
  assert.equal(members.children.length,3);
  findNode(alice,n=>n.tag==="button"&&n.textContent==="编辑档案").fire("click");
  field("通用称呼").fire("input","小鲸");
  field("通用别名（每行一个）").fire("input","鲸鱼");
  const affinity=allNodes({children:context.dialog.fields},n=>n.children?.[0]?.textContent==="好感度（0-100）")[0];
  affinity.children[1].fire("input","75");
  await context.dialog.submit();
  assert.equal(calls.at(-1)[0],"users/identity/save");
  assert.equal(calls.at(-1)[1].identity_id,"G-test-a");
  assert.equal(calls.at(-1)[1].display_name,"小鲸");
  assert.equal(calls.at(-1)[1].relationships[0].affinity,75);
  assert.ok(!("scope" in calls.at(-1)[1])&&!JSON.stringify(calls.at(-1)[1]).includes("私聊限定备注"));
  // The expanded state is retained after a global edit reload.
  const after=allNodes(ids.get("page"),n=>n.className==="identity-card").find(n=>n["data-user"]==="G-test-a");
  assert.equal(findNode(after,n=>n.className==="identity-memberships").hidden,false);
  const local=allNodes(after,n=>n.className==="identity-membership").find(n=>n.textContent.includes("Uprivate"));
  findNode(local,n=>n.tag==="button"&&n.textContent==="会话设置").fire("click");
  field("备注 / 称呼规则").fire("input","仅私聊备注");
  await context.dialog.submit();
  assert.equal(calls.at(-1)[0],"users/save");
  assert.equal(calls.at(-1)[1].scope,"scope-private");
  assert.ok(!("affinity" in calls.at(-1)[1])&&! ("role" in calls.at(-1)[1]));
  ui.state.filters.users.scope="scope-2";
  await ui.renderUsers(0);
  assert.equal(allNodes(ids.get("page"),n=>n.className==="identity-card").length,1);
  assert.ok(ids.get("page").textContent.includes("3 个会话"));
  ui.state.filters.users.scope="";
  ui.editUser(people[0]);
  field("自定义别名（每行一个）").fire("input", "蓝蓝\n小鲸");
  await context.dialog.submit();
  assert.deepEqual(calls.at(-1)[1].custom_aliases, ["蓝蓝", "小鲸"]);
  await ui.renderMemory(0);
  assert.ok(ids.get("page").textContent.includes("小蓝 · test-a"));
  await ui.editMemory(memory);
  const box = field("其他关联人物（可多选）");
  const bob = box.children.find(n => n.textContent.includes("test-b"));
  assert.ok(bob);
  bob.children[0].fire("change", true);
  const lookup = field("查找人物");
  lookup.children[0].fire("input", "旧蓝");
  await lookup.children[1].fire("click");
  // The selected identity survives search filtering even when not in the result list.
  assert.ok(box.children.some(n => n.textContent.includes("test-b") && n.children[0].checked));
  await context.dialog.submit();
  assert.deepEqual(calls.at(-1)[1].participant_refs, ["Ubbb"]);
  assert.equal(calls.at(-1)[1].subject, "Uaaa");
  await ui.editMemory(null);
  const sessionSelect = field("会话（作用域）");
  const other = field("其他关联人物（可多选）");
  other.children[0].children[0].fire("change", true);
  await sessionSelect.fire("change", "scope-2");
  assert.ok(other.children.some(n => n.textContent.includes("test-a") && !n.children[0].checked));
  await context.dialog.submit();
  assert.deepEqual(calls.at(-1)[1].participant_refs, []);
  assert.equal(calls.at(-1)[1].scope, "scope-2");
  // Legacy replacement requires explicit selected records; original evidence is not submitted.
  ui.editLegacyTitles(people[0]);
  const preview=context.dialog.fields.find(n=>n.tag==="button"&&n.textContent==="预览替换");
  await preview.fire("click");
  assert.equal(calls.at(-1)[0],"users/legacy_preview");
  const chosen=findNode(context.dialog.fields.at(-1),n=>n.tag==="label"&&n.textContent==="修改记忆 #1");
  assert.ok(chosen);
  chosen.children[0].fire("change",true);
  await context.dialog.submit();
  assert.equal(calls.at(-1)[0],"users/legacy_apply");
  assert.deepEqual(calls.at(-1)[1].selected,[1]);
  assert.equal(calls.at(-1)[1].revision,"preview-r1");
  // Merge must use a confirmed preview, preserve target conflicts by default,
  // and submit only the chosen scope, records and edited fields.
  await ui.renderMemory(ui.state.epoch);
  await findNode(ids.get("page"), n => n.tag === "button" && n.textContent === "人格数据管理").fire("click");
  assert.equal(context.dialog.title, "人格数据管理");
  await assert.rejects(context.dialog.submit(), /先预览/);
  field("来源人格").fire("change", "old");
  field("目标人格").fire("change", "new");
  field("来源会话").fire("change", "old-scope");
  const mergePreview = context.dialog.fields.find(n => n.tag === "button" && n.textContent === "只读预览");
  await mergePreview.fire("click");
  assert.equal(calls.at(-1)[0], "personas/merge_preview");
  const previewRows = context.dialog.fields.at(-1);
  assert.ok(previewRows.textContent.includes("新事实"));
  const conflict = findNode(previewRows, n => n.tag === "select" && n.value === "target");
  assert.ok(conflict);
  conflict.fire("change", "manual");
  const note = findNode(previewRows, n => n.tag === "textarea" && n.textContent === "新事实");
  note.fire("input", "合并后完整事实");
  await context.dialog.submit();
  assert.equal(calls.at(-1)[0], "personas/merge_apply");
  assert.equal(calls.at(-1)[1].confirm, true);
  assert.equal(calls.at(-1)[1].revision, "digest-1");
  assert.equal(calls.at(-1)[1].scope, "old-scope");
  assert.equal(calls.at(-1)[1].actions.find(a => a.id === "memories:1").values.full_text, "合并后完整事实");
  assert.ok(ids.get("notice").textContent.includes("merge-fixture"));
  // Category filtering and the original keyword filter intersect without changing JSON.
  ui.state.config={schema:[
    {key:"memory",label:"分层记忆",fields:[
      {key:"enabled",label:"启用记忆",description:"启用记忆系统",type:"boolean",default:true},
      {key:"ordinary_days",label:"琐事保留",description:"短期记忆保留天数",type:"integer",default:3}]},
    {key:"habits",label:"习惯印象",fields:[
      {key:"enabled",label:"记录习惯",description:"归纳多日重复行为",type:"boolean",default:true}]},
    {key:"models",label:"模型分工",fields:[
      {key:"memory_provider",label:"记忆模型",description:"记忆提取",type:"string",default:"mock"}]},
    {key:"social",label:"独立人格与表达",fields:[
      {key:"style_prompt",label:"表达风格规范",description:"文风规则",type:"string",widget:"textarea",default:"自然"},
      {key:"negative_examples",label:"自定义风格反例",description:"反例编辑",type:"array",widget:"json",default:[]}]},
    {key:"dota",label:"Dota 2 数据工具",fields:[
      {key:"knowledge_auto_sync",label:"自动构建版本化英雄与装备资料",description:"版本资料",type:"boolean",default:true},
      {key:"skill_aliases",label:"自定义技能别名",description:"技能别名",type:"object",widget:"json",default:{}}]}
  ],backups:[]};
  ui.state.draft={memory:{enabled:true,ordinary_days:7},habits:{enabled:true},models:{memory_provider:"mock"},
    social:{style_prompt:"自然",negative_examples:[]},dota:{knowledge_auto_sync:true,skill_aliases:{}}};
  const draft=copy(ui.state.draft);
  ui.drawConfig();
  let p=ids.get("page");
  const picker=findNode(p,n=>n.tag==="select"&&n.textContent.includes("全部模块"));
  const search=findNode(p,n=>n.tag==="input"&&n.placeholder==="搜索名称、字段、描述…");
  assert.ok(picker&&search);
  search.fire("input","记忆");
  assert.ok(p.textContent.includes("memory.enabled")&&p.textContent.includes("models.memory_provider"));
  picker.fire("change","memory");
  assert.ok(p.textContent.includes("memory.enabled")&&!p.textContent.includes("models.memory_provider"));
  assert.ok(!p.textContent.includes("habits.enabled"));
  search.fire("input","");
  assert.ok(p.textContent.includes("habits.enabled")&&p.textContent.includes("memory.enabled"));
  search.fire("input","琐事");
  assert.ok(p.textContent.includes("memory.ordinary_days")&&!p.textContent.includes("memory.enabled"));
  picker.fire("change","system");
  assert.ok(p.textContent.includes("没有匹配的配置字段"));
  picker.fire("change","");
  search.fire("input","");
  assert.ok(p.textContent.includes("memory.enabled")&&p.textContent.includes("models.memory_provider"));
  assert.deepEqual(copy(ui.currentConfig()),draft);
  picker.fire("change","persona");
  search.fire("input","反例");
  assert.ok(p.textContent.includes("social.negative_examples")&&!p.textContent.includes("dota.skill_aliases"));
  const neg = findNode(p,n=>n.tag==="textarea");
  const rule=[{scene:"casual",user:"困",avoid:"守着",prefer:"晚安"}];
  neg.fire("input",JSON.stringify(rule));
  assert.deepEqual(copy(ui.currentConfig().social.negative_examples),rule);
  search.fire("input","");
  picker.fire("change","knowledge");
  search.fire("input","技能别名");
  assert.ok(p.textContent.includes("dota.skill_aliases")&&!p.textContent.includes("dota.knowledge_auto_sync"));
  const aliases={antimage_mana_break:["烧蓝"]};
  findNode(p,n=>n.tag==="textarea").fire("input",JSON.stringify(aliases));
  assert.deepEqual(copy(ui.currentConfig().dota.skill_aliases),aliases);
  assert.deepEqual(copy(ui.currentConfig().memory),draft.memory);
  // The new pages separate facts and opinions, use a whitelisted citation link,
  // and show an audit state without forwarding private text into the UI.
  await ui.renderResearch(0);
  p=ids.get("page");
  assert.ok(p.textContent.includes("官方测试事实")&&p.textContent.includes("角色观点（非事实）"));
  assert.ok(findNode(p,n=>n.tag==="a"&&n.href==="https://store.steampowered.com/news/app/570/view/123"));
  assert.ok(p.textContent.includes("Steam 官方 RSS")&&p.textContent.includes("TimeoutError（请求超时）"));
  assert.ok(p.textContent.includes("本地职业比赛样本")&&p.textContent.includes("2 局有效职业样本"));
  assert.ok(p.textContent.includes("CS2 结构化赛事")&&p.textContent.includes("未接入"));
  await findNode(p,n=>n.tag==="button"&&n.textContent==="检测数据源").fire("click");
  assert.equal(calls.at(-1)[0],"action");
  assert.equal(calls.at(-1)[1].name,"research_probe");
  await ui.renderRelay(0);
  assert.ok(p.textContent.includes("刀塔群")&&p.textContent.includes("待确认"));
  assert.ok(p.textContent.includes("不展示私聊正文"));
  // Exercise real grouped routing, including an old leaf link, a return to
  // a remembered leaf, and the fixed-height independent sidebar scroll rule.
  const html=fs.readFileSync(path.join(__dirname,"../views/dashboard/index.html"),"utf8");
  const css=fs.readFileSync(path.join(__dirname,"../views/dashboard/style.css"),"utf8");
  const groupIds=[...html.matchAll(/data-module="([^"]+)"/g)].map(x=>x[1]);
  assert.equal(groupIds.length,6);
  assert.deepEqual(groupIds,Object.keys(ui.modules));
  const sidebarRule=css.match(/\.sidebar\s*\{([^}]+)\}/)[1];
  assert.match(sidebarRule,/height\s*:\s*100vh/);
  assert.match(sidebarRule,/overflow-y\s*:\s*auto/);
  assert.match(sidebarRule,/align-self\s*:\s*start/);
  assert.match(css,/#nav\s*\{[^}]*flex\s*:\s*0 0 auto/);
  const nav=document.getElementById("nav");
  const buttons=groupIds.map(k=>{const n=new NodeFixture("button");n["data-module"]=k;return n;});
  nav.replaceChildren(...buttons);
  await ui.pageChange("logs");
  assert.equal(ui.state.tab,"logs");
  assert.equal(buttons.find(n=>n["data-module"]==="operations")["aria-current"],"page");
  const tabs=ids.get("sectionTabs");
  assert.equal(tabs.hidden,false);
  assert.deepEqual(tabs.children.map(x=>x["data-tab"]),["usage","logs"]);
  assert.equal(tabs.children[1]["aria-pressed"],"true");
  await ui.openModule("overview");
  assert.equal(tabs.hidden,true);
  await ui.openModule("operations");
  assert.equal(ui.state.tab,"logs");
  assert.ok(ids.get("page").textContent.includes("独立日志"));
  await ui.pageChange("memory");
  assert.equal(buttons.find(n=>n["data-module"]==="people")["aria-current"],"page");
  assert.equal(tabs.children[1]["data-tab"],"memory");
  // Render the exact published backend schema, edit a setting while filtered,
  // save the whole document, and follow both shortcuts from their real pages.
  const plugin=path.resolve(__dirname,"..");
  const real=JSON.parse(require("node:child_process").execFileSync(
    process.env.FIYU_TEST_PYTHON || "python", ["-c",
      "import json; from astrbot_plugin_fiyu_core.core.config import SCHEMA,defaults; print(json.dumps([SCHEMA, defaults()]))"],
    {cwd:path.dirname(plugin), encoding:"utf8"}));
  ui.state.config={schema:real[0],config:real[1],revision:"all-r1",backups:[]};
  ui.state.draft=copy(real[1]);
  ui.state.dirty=false;
  ui.state.configCategory="";
  ui.state.configSearch="";
  await ui.pageChange("config");
  p=ids.get("page");
  const groups=allNodes(p,n=>n.className==="panel config-group");
  const paths=allNodes(p,n=>n.className==="key").map(n=>n.textContent);
  assert.equal(groups.length,6);
  assert.deepEqual(paths.sort(),real[0].flatMap(g=>g.fields.map(f=>g.key+"."+f.key)).sort());
  const categories=findNode(p,n=>n["aria-label"]==="配置模块分类");
  assert.equal(categories.children.length,7);
  categories.fire("change","conversation");
  assert.deepEqual(allNodes(p,n=>n.className==="config-section").map(n=>n.id),
    ["config-section-decision","config-section-reply","config-section-interrupt","config-section-sessions"]);
  const filter=findNode(p,n=>n.tag==="input"&&n.placeholder==="搜索名称、字段、描述…");
  filter.fire("input","reply.split_chars");
  assert.equal(allNodes(p,n=>n.className==="key").length,1);
  findNode(p,n=>n.type==="number").fire("input","420");
  await ui.saveConfig();
  const [route, saved]=calls.at(-1);
  assert.equal(route,"config/save");
  assert.equal(saved.revision,"all-r1");
  const edited=copy(real[1]); edited.reply.split_chars=420;
  assert.deepEqual(saved.config,edited);
  ui.state.config.presets = {questions: {decision: {ordinary_check_probability: 0, dynamic_enabled: false}}};
  ui.drawConfig();
  const presetsButton = findNode(ids.get("page"), n => n.tag==="button" && n.textContent==="场景预设与会话覆盖");
  await presetsButton.fire("click");
  const scenarioScope = field("应用范围");
  const selectedScope = ui.state.sessions[0].scope;
  scenarioScope.fire("change", selectedScope);
  field("预设").fire("change", "questions");
  assert.ok(context.dialog.fields.at(-1).textContent.includes("当前来源"));
  await context.dialog.submit();
  assert.equal(ui.state.draft.sessions.overrides[selectedScope].decision.ordinary_check_probability, 0);
  assert.equal(ui.state.draft.decision.ordinary_check_probability, edited.decision.ordinary_check_probability);
  assert.deepEqual(copy(ui.state.draft.access), edited.access);
  await ui.saveConfig();
  assert.equal(calls.at(-1)[1].config.sessions.overrides[selectedScope].decision.dynamic_enabled, false);

  for(const [tab,caption,category,section] of [
    ["research","前往配置","memory","research"],["relay","前往转达配置","access","relay"]]) {
    await ui.pageChange(tab);
    await findNode(ids.get("page"),n=>n.tag==="button"&&n.textContent===caption).fire("click");
    assert.equal(ui.state.configCategory,category);
    assert.equal(ui.state.configSearch,"");
    assert.ok(findNode(ids.get("page"),n=>n.id==="config-section-"+section));
  }
  console.log("Dashboard: six grouped modules, retained leaf routing, independent sidebar scroll rules, user and memory edits, category/JSON, source status and probe passed.");
}
main().catch(e => { console.error(e); process.exitCode = 1; });
