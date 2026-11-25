// Optional development smoke test (Node.js + Playwright + Chromium).
// All bridge calls are mocked with synthetic fixtures; never accesses your AstrBot server.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const http = require("node:http");
const { execFileSync } = require("node:child_process");
const { chromium } = require("playwright");

const plugin = path.resolve(__dirname, "..");
const site = path.join(plugin, "views", "dashboard");
const data = JSON.parse(
  execFileSync(
    process.env.FIYU_TEST_PYTHON || "python",
    [
      "-c",
      'import json; from astrbot_plugin_fiyu_core.core.config import SCHEMA, defaults; print(json.dumps({"schema":SCHEMA,"config":defaults()}))',
    ],
    { cwd: path.dirname(plugin), encoding: "utf8" },
  ),
);
const server = http.createServer((req, res) => {
  const key = {
    "/": "index.html",
    "/index.html": "index.html",
    "/style.css": "style.css",
    "/app.js": "app.js",
  }[req.url.split("?")[0]];
  if (!key) {
    res.writeHead(404);
    res.end();
    return;
  }
  res.setHeader(
    "Content-Type",
    key.endsWith(".js")
      ? "text/javascript"
      : key.endsWith(".css")
        ? "text/css"
        : "text/html; charset=utf-8",
  );
  res.end(fs.readFileSync(path.join(site, key)));
});

async function main() {
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  const browser = await chromium.launch({
    headless: true,
    executablePath: process.env.FIYU_CHROMIUM || "/usr/bin/chromium",
    args: ["--no-sandbox", "--disable-dev-shm-usage"],
  });
  try {
    const context = await browser.newContext({
      viewport: { width: 1280, height: 900 },
    });
    await context.addInitScript(({ schema, config }) => {
      const copy = (x) => JSON.parse(JSON.stringify(x));
      const s = {
        schema,
        config,
        revision: "rev-1",
        backups: [],
        options: {
          chat_provider: [{ id: "deepseek", label: "DeepSeek flash" }],
          embedding_provider: [],
          persona: [{ id: "肥鱼", label: "肥鱼" }],
          knowledge_bases: [{ id: "sap", name: "SAP Documents" }],
        },
      };
      const session = {
        scope: "scope-test",
        location: "group-test",
        kind: "group",
        persona: "fish",
        platform: "qq_official",
        umo: "test:GroupMessage:group-test",
      };
      const user = {
        scope: session.scope,
        uid: "user-test",
        ref: "Utest",
        name: "Alice",
        display_name: "",
        role: "member",
        affinity: 50,
        blocked: 0,
        deny_keywords: [],
        deny_reply: "不回答",
        note: "",
        historical_aliases: ["Old Alice"],
        custom_aliases: [],
      };
      const user2 = { ...user, uid: "friend-test", ref: "Ufriend", name: "Bob", historical_aliases: [] };
      const memory = {
        id: 1,
        scope: session.scope,
        subject: user.ref,
        subject_name: "Alice",
        kind: "core",
        state: "active",
        title: "生日",
        topic_key: "birthday",
        full_text: "生日7月22日",
        visible_text: "生日7月22日",
        gist: "生日7月",
        trace: "生日",
        importance: 90,
        confidence: 0.95,
        clarity: "vivid",
        updated: 1780000000,
        pinned: 0,
        evidence: [{ text: "测试生日" }],
        participants: [user],
        participant_refs: [user.ref],
        valid_until: null,
      };
      const style = {
        id: 1,
        scope: session.scope,
        kind: "slang",
        pattern: "紫猫",
        meaning: "虚无之灵",
        example: "紫猫开团",
        status: "pending",
        count: 2,
        confidence: 0.9,
      };
      window.__mock = { calls: [], state: s };
      window.confirm = () => true;
      window.AstrBotPluginView = {
        ready: async () => {},
        apiGet: async (p, q) => {
          if (p === "config") return copy(s);
          if (p === "config/defaults") return { config: copy(config) };
          if (p === "overview")
            return {
              mode: s.config.access.mode,
              totals: {
                today: { received: 12, replied: 3 },
                sessions: 1,
                people: 1,
                memories: { active: 1 },
                styles: { pending: 1 },
              },
              usage: { tokens: 360 },
              budget: 1000,
              workers: 0,
              queue: 0,
              model_running: 0,
              today: "2026-11-23",
              actions: {},
              dota: { state: "测试" },
              balance: { state: "未检查" },
            };
          if (p === "sessions") return { items: [session] };
          if (p === "users/identities") return {items:[user,user2].map(u=>({
            identity_id:"G-"+u.uid,uid:u.uid,name:u.name,display_name:"",revision:1,
            platform:"qq_official",platform_id:"test",custom_aliases:[],historical_aliases:u.historical_aliases,
            membership_count:1,group_count:1,private_count:0,blocked_count:0,memberships:[{...u,...session}],
            relationships:[{persona:session.persona,role:"member",affinity:50,familiarity:"初识",familiarity_override:"auto",revision:1}]
          }))};
          if (p === "users") return { items: [user, user2].filter(u => !q?.q || [u.uid, u.name, ...u.historical_aliases].some(n => n.includes(q.q))) };
          if (p === "memories") return { items: [memory] };
          if (p === "memories/versions") return { items: [] };
          if (p === "styles") return { items: [style] };
          if (p === "research") return {
            settings:s.config.research, overview:{today_runs:1,notes:1}, tokens:800,
            source_status:[
              {key:"steam_rss",label:"Steam 官方 RSS",state:"ok",last_source:"cs2_news",checked_at:1795500000,elapsed_ms:465},
              {key:"steam_api",label:"Steam 新闻 API",state:"failed",last_source:"cs2_news",checked_at:1795500000,
               elapsed_ms:30000,cooling:true,retry_after:1795503600,reason:"TimeoutError（请求超时）"},
            ],
            items:[{id:1,persona:"fish",topic:"测试公告",source:"dota_news",state:"active",
              created:1780000000,expires:1782000000,stale:false,
              facts:[{text:"事实内容示例",refs:["s1"]}],
              opinion:{text:"观点内容示例",refs:["s1"]},
              sources:[{id:"s1",title:"官方测试公告",date:"2026-05-28",url:"https://store.steampowered.com/news/app/570/view/123"}]}],
            runs:[]
          };
          if (p === "relay") return {items:[{id:2,scope:session.scope,alias:"刀塔群",status:"pending",created:1780000000}]};
          if (p === "usage")
            return {
              items: [
                {
                  day: "2026-11-23",
                  module: "reply",
                  provider: "deepseek",
                  calls: 1,
                  failures: 0,
                  input_other: 100,
                  input_cached: 20,
                  output: 10,
                  estimated: 0,
                  elapsed_ms: 200,
                },
              ],
              note: "测试用量",
            };
          if (p === "logs")
            return {
              days: ["2026-11-23"],
              items: [
                {
                  time: "2026-11-23T10:00:00+08:00",
                  level: "INFO",
                  module: "DECISION",
                  code: "reply",
                  reason: "测试",
                },
              ],
            };
          throw Error("unknown " + p);
        },
        apiPost: async (p, v) => {
          window.__mock.calls.push({ path: p, value: copy(v) });
          if (p === "config/save") {
            s.config = copy(v.config);
            s.revision = "rev-2";
            s.backups = ["config-test.json"];
            return { ok: true, ...copy(s) };
          }
          if (p === "users/save") {
            Object.assign(user, v);
            return { ok: true };
          }
          if (p === "styles/action") {
            Object.assign(style, v);
            return { ok: true };
          }
          return { ok: true };
        },
      };
    }, data);
    const page = await context.newPage();
    const errors = [];
    page.on("pageerror", (e) => errors.push(e.message));
    const groupOf={overview:"overview",config:"config",users:"people",memory:"people",
      learning:"learning",research:"learning",relay:"relay",usage:"operations",logs:"operations"};
    const openTab=async name=>{
      const group=page.locator('[data-module="'+groupOf[name]+'"]');
      if(await group.getAttribute("aria-current")!=="page")await group.click();
      const leaf=page.locator('#sectionTabs [data-tab="'+name+'"]');
      if(await leaf.count())await leaf.click();
    };
    await page.goto(`http://127.0.0.1:${server.address().port}/`);
    await page.getByText("今日收到消息", { exact: true }).waitFor();
    assert.equal(await page.locator("#nav button").count(),6);
    await openTab("config");
    const search = page.getByPlaceholder("搜索名称、字段、描述…");
    await search.waitFor();
    const category = page.locator(".category-picker select");
    assert.equal(await category.locator("option").count(),7);
    await search.fill("记忆");
    await page.getByText("models.memory_provider",{exact:true}).waitFor();
    await category.selectOption("memory");
    const keys=await page.locator(".field .key").allTextContents();
    assert.ok(keys.length&&keys.every(k=>/^(memory|habits|learning|research)\./.test(k)));
    await search.fill("ordinary_days");
    await page.getByText("memory.ordinary_days",{exact:true}).waitFor();
    assert.equal(await page.locator(".field").count(),1);
    await category.selectOption("");
    // Hide invalid JSON through a search filter; it must still block saving.
    await search.fill("budget.pricing");
    // Searching the field key or human description is supported; "pricing" is the actual key.
    await search.fill("pricing");
    await page.locator(".field textarea").fill("{broken");
    await category.selectOption("system");
    await search.fill("日志保留天数");
    await page.getByText("logs.retention_days", { exact: true }).waitFor();
    await page.locator("#saveConfig").click();
    await page
      .getByText("以下字段的 JSON 格式错误：budget.pricing", { exact: true })
      .waitFor();
    assert.equal(await page.evaluate(() => window.__mock.calls.length), 0);
    await category.selectOption("");
    await search.fill("pricing");
    await page.locator(".field textarea").fill("{}");
    await search.fill("日志保留天数");
    assert.equal(await page.locator(".field").count(), 1);
    await page.locator(".field input[type=number]").fill("20");
    await page.getByRole("button", { name: "JSON 编辑", exact: true }).click();
    const editor = page.locator("#jsonEditor");
    await editor.waitFor();
    assert.equal(JSON.parse(await editor.inputValue()).logs.retention_days, 20);
    const valid = JSON.parse(await editor.inputValue());
    valid.access.mode = "observe";
    valid.access.allowed_groups = ["group-test"];
    await editor.fill("{broken");
    await page.locator("#saveConfig").click();
    assert.equal(await page.evaluate(() => window.__mock.calls.length), 0);
    await editor.fill(JSON.stringify(valid, null, 2));
    await page.locator("#saveConfig").click();
    await page
      .getByText("配置已保存并生效（旧配置已备份）。", { exact: true })
      .waitFor();
    assert.equal(
      await page.evaluate(() => window.__mock.state.config.logs.retention_days),
      20,
    );
    assert.equal(
      await page.evaluate(() => window.__mock.state.config.access.mode),
      "observe",
    );
    await openTab("users");
    await page.getByText("ID：user-test", { exact: true }).waitFor();
    await page.getByRole("button", { name: "展开会话", exact: true }).first().click();
    await page.getByRole("button", { name: "会话设置", exact: true }).first().click();
    await page.locator("#dialog").waitFor();
    await page.locator("#dialog .form-line").filter({ hasText: "备注 / 称呼规则" }).locator("textarea").fill("测试备注");
    await page.locator("#dialog .form-line").filter({ hasText: "自定义别名" }).locator("textarea").fill("A姐\n阿丽丝");
    await page.locator("#dialogSubmit").click();
    await page.getByText("测试备注", { exact: true }).waitFor();
    await openTab("memory");
    await page.getByText("生日7月22日", { exact: true }).waitFor();
    await page.getByRole("button", { name: "查看/编辑", exact: true }).click();
    await page.locator("#dialog").waitFor();
    await page.getByLabel("Bob · friend-test", { exact: true }).check();
    await page.locator("#dialogSubmit").click();
    await page.waitForFunction(() => window.__mock.calls.some(c => c.path === "memories/save" && c.value.participant_refs.includes("Ufriend")));
    await openTab("learning");
    await page.getByText("紫猫", { exact: true }).waitFor();
    await page.getByRole("button", { name: "批准", exact: true }).click();
    await page.getByRole("table").getByText("已批准", { exact: true }).waitFor();
    await openTab("research");
    await page.getByText("事实内容示例",{exact:true}).waitFor();
    await page.getByText("角色观点（非事实）",{exact:true}).waitFor();
    assert.equal(await page.getByRole("link",{name:"官方测试公告 · 2026-05-28"}).first().getAttribute("href"),"https://store.steampowered.com/news/app/570/view/123");
    await page.getByText("Steam 官方 RSS",{exact:true}).waitFor();
    await page.getByText("TimeoutError（请求超时）",{exact:true}).waitFor();
    await page.getByRole("button",{name:"检测数据源",exact:true}).click();
    await page.waitForFunction(()=>window.__mock.calls.some(c=>c.path==="action"&&c.value.name==="research_probe"));
    await openTab("relay");
    await page.getByText("待确认",{exact:true}).waitFor();
    await openTab("usage");
    await page.getByText("每日用量明细", { exact: true }).waitFor();
    await openTab("logs");
    await page.getByText("独立日志", { exact: true }).waitFor();
    // Tiny embedded viewport: sidebar must scroll independently so even its
    // last module is clickable. The current subpage survives switching groups.
    for(const size of [{width:1050,height:450},{width:850,height:240}]){
      await page.setViewportSize(size);
      await openTab("overview");
      const dims=await page.locator(".sidebar").evaluate(n=>({
        client:n.clientHeight,scroll:n.scrollHeight,overflow:getComputedStyle(n).overflowY
      }));
      assert.equal(dims.overflow,"auto");
      assert.ok(dims.client<=size.height);
      if(size.height===240){
        assert.ok(dims.scroll>dims.client,"short sidebar should be scrollable");
        await page.locator(".sidebar").evaluate(n=>n.scrollTop=n.scrollHeight);
        assert.ok(await page.locator(".sidebar").evaluate(n=>n.scrollTop>0));
      }
      await page.locator('[data-module="operations"]').click();
      await page.getByText("独立日志",{exact:true}).waitFor();
      assert.equal(await page.locator('#sectionTabs [data-tab="logs"]').getAttribute("aria-pressed"),"true");
      assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth));
    }
    await page.setViewportSize({ width: 390, height: 844 });
    for(const [tab,title] of [["logs","独立日志"],["research","公开知识笔记"],["relay","受控转达"]]){
      await openTab(tab);
      await page.getByText(title,{exact:true}).waitFor();
      assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),"mobile overflow in "+tab);
    }
    assert.deepEqual(errors, []);
    console.log(
      "Dashboard: six grouped modules, all nine leaf pages, short-window scroll, retained subpage, configuration/search/save, data-source probe and mobile layout OK",
    );
  } finally {
    await browser.close();
  }
}

main()
  .catch((e) => {
    console.error(e);
    process.exitCode = 1;
  })
  .finally(() => server.close());
