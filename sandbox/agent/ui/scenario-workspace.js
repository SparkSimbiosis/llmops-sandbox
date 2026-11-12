// Workflow composition only: existing nodes/listeners and server execution boundaries are preserved.
"use strict";
(() => {
  // Panels with the five-stage journey. Only harness/builder/chat are mentor scenarios; the rest are lab/settings tools.
  const scenarios = ["harness", "task", "builder", "chat", "workspace", "packs"];
  const sections = { demo: ["home"], scenario: ["harness"], lab: ["builder", "chat", "task", "workspace", "injections", "break", "cache"], settings: ["policy", "roles", "packs", "prompts", "config"], control: ["approvals", "executions", "history", "cost", "site"] };
  const stageNames = ["Тестові дані", "Виконавець", "Контекст", "Перевірка й запуск", "Результат"];
  const stages = {}, fixtureViews = {}, states = {};
  let current = "home", lastScenario = "harness", harnessKind = "read", entries = [];
  // Passport scenario kinds and the projects each may use, in preference order (LLM-Ops first).
  // A change is a .patch for LLM-Ops (the stand never writes the repository) or a database change of the demo site.
  const projectsFor = { read: ["llm-ops", "notes-app"], change: ["llm-ops", "demo-site"], trap: ["notes-app"] };
  const kindOfProject = project => project === "demo-site" ? "change" : "read";
  const kindOf = entry => entry.scenario || (entry.target === "harness" ? kindOfProject(entry.project) : entry.target);
  const note = text => el("p", { class: "hint", text });
  const advanced = (title, ...nodes) => el("details", { class: "workflow-advanced" }, el("summary", { text: title }), ...nodes.filter(Boolean));
  function field(id) {
    const control = $(id); if (!control) return null;
    const parent = control.closest("label"); if (parent) return parent;
    const label = document.querySelector('label[for="' + id + '"]');
    return el("div", { class: "workflow-field" }, label, control);
  }
  function put(target, stage, ...nodes) { stages[target][stage - 1].append(...nodes.filter(Boolean)); }
  function initStages(target) {
    const panel = $("tab-" + target);
    const legacy = advanced("Додаткові інструменти (необов’язково)");
    while (panel.firstChild) legacy.append(panel.firstChild);
    panel.append(legacy);
    stages[target] = stageNames.map((title, index) => {
      const body = el("div", { class: "workflow-content" });
      const block = el("section", { class: "workflow-block", "data-stage": String(index + 1) },
        el("header", { class: "workflow-heading" }, el("span", { class: "workflow-number", text: String(index + 1) }), el("h2", { text: title })), body);
      panel.insertBefore(block, legacy); return body;
    });
    const select = el("select", { id: "fixture-select-" + target, "aria-label": "Тестовий приклад" });
    const button = el("button", { id: "fixture-apply-" + target, text: "Підставити приклад", disabled: "" });
    const expected = el("p", { class: "fixture-expected" });
    const status = el("p", { id: "fixture-status-" + target, class: "hint", role: "status", text: "Каталог завантажується. Підстановка не запускає модель." });
    put(target, 1, el("div", { class: "fixture-toolbar" }, select, button), expected, status);
    fixtureViews[target] = { select, button, expected, status };
    select.addEventListener("change", () => describeFixture(target));
    button.addEventListener("click", async () => {
      const entry = entries.find(item => item.id === select.value); if (!entry) return;
      button.disabled = true;
      try { await window.AgentDemoCatalog.apply(entry); status.textContent = "Приклад підставлено. Перевірте входи; модель не запускалась."; }
      catch (error) { status.textContent = error.message; }
      finally { button.disabled = !entries.length; }
    });
    put(target, 2, el("div", { id: "identity-" + target }));
    put(target, 4, el("span", { class: "pill workflow-mode", text: "Наступний виклик: " + taskMode() }),
      note(target === "packs" ? "Компіляція — прев’ю без моделі та запису історії." : "Перевірте режим і вхід перед явним запуском. Невідомий результат не повторюється автоматично."));
    put(target, 5, el("div", { id: "result-identity-" + target }));
  }
  scenarios.forEach(initStages);
  // Move intact controls and render containers. Never move children of a replaceChildren target.
  put("task", 1, field("task-text"), advanced("Інші короткі приклади", $("scenarios")));
  put("task", 2, field("task-model"), $("models-card"));
  put("task", 3, field("task-base"), note("Planner бачить перелік дозволених файлів; Editor — вибрані ним файли з бази current або seed. Точний склад записується в журналі викликів."));
  put("task", 4, field("task-cache"), $("run")); put("task", 5, $("result"));

  put("harness", 1, field("harness-project"), field("harness-goal"), advanced("Доступні проєкти", $("harness-projects")));
  put("harness", 2, field("harness-profile"), $("harness-profile-hint"), $("harness-route"), advanced("Дозволи середовища та повний паспорт", field("harness-environment"), $("harness-passport")));
  put("harness", 3, $("harness-context"));
  put("harness", 4, $("harness-packet"), field("harness-mode"), field("harness-cache"), field("harness-risk"), $("harness-prepare"), $("harness-run"), field("harness-confirm"), $("harness-status"));
  put("harness", 5, $("harness-result"), el("button", { text: "Журнал виконань", on: { click: () => showTab("executions") } }));

  put("builder", 1, field("b-title"), field("b-goal"), field("b-context"), advanced("Дані, обмеження та критерії прикладу", field("b-input"), field("b-constraints"), field("b-accept"), field("b-complexity")), advanced("Постановка своїми словами", $("task-intake")));
  put("builder", 2, field("b-engine"), $("b-engine-hint"), field("b-run-project"), field("b-run-profile"));
  put("builder", 3, note("Роль, методику, контракт і контекст проєкту код закріплює під час підготовки пакета на наступному кроці. Перегляд промпта нижче — необов’язковий і нічого не запускає."),
    advanced("Переглянути промпт до запуску (необов’язково)", $("b-build"), $("b-result")), advanced("Експорт і версія промпта", $("b-yaml"), $("b-save"), $("b-example")));
  put("builder", 4, $("b-run-packet"), $("b-run-mode"), field("b-run-cache"), $("b-run-prepare"), $("b-run-execute"), $("b-run-status"));
  const historyRefresh = $("b-run-history").previousElementSibling;
  put("builder", 5, $("b-run-result"), advanced("Попередні запуски Engine", historyRefresh, $("b-run-history")));

  put("chat", 1, el("label", { for: "chat-input", text: "Повідомлення" }), $("chat-input"), advanced("Інші запитання без автоматичної відправки", $("chat-examples")), $("chat-new"));
  put("chat", 3, note("Поточні дозволені файли демо-сайту та останні 6 ходів. Це не довгострокова пам’ять. Пропозиція зміни запускає окремий Planner/Editor конвеєр."));
  put("chat", 4, field("chat-cache"), $("chat-mode"), $("chat-send"));
  const chatListCard = $("chat-list").closest(".card");
  put("chat", 5, $("chat-id"), $("chat-log"), advanced("Історія розмов", chatListCard));

  put("workspace", 1, field("ws-project"), el("label", { for: "ws-question", text: "Питання до проєкту" }), $("ws-question"), advanced("Інші питання", $("ws-examples")));
  put("workspace", 3, field("ws-budget"), $("ws-budget-v"), $("ws-index-kpis"), $("ws-pack"), note("Клік по позначці у файлі закріплює його в пакеті. Відбір обмежений whitelist і бюджетом."), $("ws-tree"), $("ws-skipped"), $("ws-pack-view"), advanced("Повідомлення та джерела пакета", $("ws-package")));
  put("workspace", 4, field("ws-cache"), $("ws-mode"), $("ws-ask"));
  const workspaceRuns = $("ws-runs").closest(".card");
  put("workspace", 5, $("ws-answer"), $("ws-viewer"), advanced("Історія досліджень", workspaceRuns));

  put("packs", 1, field("cf-task"), advanced("Patch для ролі Critic", field("cf-patch")));
  put("packs", 2, field("cf-agent"));
  put("packs", 3, note("Це прев’ю рецепта для вибраної ролі. Точний виконуваний пакет Engine готується в сценарії «Один спеціаліст»."), advanced("Рецепти system/user", $("cf-recipes").closest(".table-scroll")));
  put("packs", 4, $("cf-compile")); put("packs", 5, $("cf-result"));

  // Preserve tool nodes/listeners while exposing one reference topic at a time.
  for (const target of scenarios) {
    const reference = $("tab-" + target).querySelector(":scope > details.workflow-advanced");
    for (const card of [...reference.children]) {
      const heading = card.tagName !== "SUMMARY" && card.querySelector("h2");
      if (!heading) continue;
      const topic = advanced(heading.textContent);
      card.before(topic); topic.append(card);
    }
  }
  function sectionFor(tab) { return Object.keys(sections).find(section => sections[section].includes(tab)); }
  function markScenario() {
    const active = current === "harness" ? harnessKind : current;
    document.querySelectorAll("#scenario-kind [data-scenario]").forEach(button => button.setAttribute("aria-pressed", String(button.dataset.scenario === active)));
  }
  function setHarnessKind(kind) {
    harnessKind = kind;
    // Harness learns the kind before any project switch below, so the next preparation sends the right kind.
    document.dispatchEvent(new CustomEvent("agent:harnesskind", { detail: { kind: kind === "change" ? "change" : "read" } }));
    const select = $("harness-project");
    if (select) {
      const allowed = projectsFor[kind];
      for (const option of select.options) option.hidden = !allowed.includes(option.value);
      if (select.options.length && !allowed.includes(select.value)) {
        const first = allowed.map(id => [...select.options].find(option => option.value === id && !option.disabled)).find(Boolean);
        if (first) { select.value = first.value; select.dispatchEvent(new Event("change", { bubbles: true })); }
      }
    }
    if (journey.harness) journey.harness.title.textContent = purposes["harness:" + kind];
    if (fixtureViews.harness && entries.length) populateFixtures("harness");
    markScenario();
  }
  function selectScenario(kind) {
    if (projectsFor[kind]) { setHarnessKind(kind); showTab("harness"); }
    else showTab(kind);
  }
  function navigate(tab) {
    current = tab;
    $("project").closest("label").hidden = !["task", "chat"].includes(tab);
    const section = sectionFor(tab); if (!section) return;
    if (section === "scenario") lastScenario = tab;
    markScenario();
    $("scenario-switch").hidden = section !== "scenario";
    // Demo has nothing to wait for: the live status bar appears once a scenario is open.
    const bar = $("execution-status");
    if (bar) { bar.hidden = section === "demo"; syncStatusLayout(); }
    const activityLog = $("journey-log"); if (activityLog) activityLog.hidden = !scenarios.includes(tab);
    $("section-navigation").hidden = section === "scenario" || section === "demo";
    document.querySelectorAll("#section-navigation .tab").forEach(button => { button.hidden = !sections[section].includes(button.dataset.tab); });
    document.querySelectorAll(".major-tab").forEach(button => {
      button.classList.toggle("active", button.dataset.section === section);
      if (button.dataset.section === section) button.setAttribute("aria-current", "page"); else button.removeAttribute("aria-current");
    });
    $("page-group").textContent = { demo: "Демо", scenario: "Сценарії", lab: "Лабораторія", settings: "Налаштування", control: "Контроль" }[section];
    if (scenarios.includes(tab)) refreshDraft(tab);
  }
  document.querySelectorAll(".major-tab").forEach(button => button.addEventListener("click", () => showTab(button.dataset.section === "scenario" ? lastScenario : sections[button.dataset.section][0])));
  document.addEventListener("click", event => {
    const button = event.target.closest?.("[data-scenario]");
    if (button) selectScenario(button.dataset.scenario);
  });
  document.addEventListener("agent:tabchange", event => navigate(event.detail.tab));
  // Options arrive asynchronously; a programmatic project change (fixture, transfer) also switches the kind.
  document.addEventListener("agent:harnessready", () => setHarnessKind(harnessKind));
  $("harness-project").addEventListener("change", () => {
    const project = $("harness-project").value;
    if (!projectsFor[harnessKind].includes(project)) setHarnessKind(kindOfProject(project));
  });

  function draftPacket(target) {
    const config = typeof cfg !== "undefined" && cfg ? cfg : {};
    function agentPacket(id) {
      const agent = (config.agents || []).find(item => item.id === id);
      if (!agent) return {};
      const promptName = (config.prompts || {})[agent.step];
      return { agent: id, role: agent.role, contract: agent.contract, project: "за вибраним сценарієм", prompt: promptName ? { name: promptName, version: "ще не закріплено" } : null };
    }
    if (target === "harness") return $("harness-project").value === "demo-site" ? { steps: [agentPacket("planner"), agentPacket("editor")] }
      : agentPacket(harnessKind === "change" ? "repo_editor" : "explorer");
    if (target === "task") return { steps: [agentPacket("planner"), agentPacket("editor")] };
    if (target === "chat") return agentPacket("chat");
    if (target === "workspace") return agentPacket("explorer");
    if (target === "packs") return agentPacket($("cf-agent").value);
    const engine = (config.agents || []).find(agent => agent.engine === $("b-engine").value);
    return engine ? { ...agentPacket(engine.id), prompt: null, prompt_sha256: typeof built !== "undefined" && built ? built.sha256 : undefined } : {};
  }
  const identityConfig = () => ({ ...(typeof cfg !== "undefined" && cfg ? cfg : {}), contracts: typeof studio !== "undefined" && studio ? studio.contracts : [] });
  function refreshDraft(target) {
    if (states[target]?.state === "prepared") return;
    window.CallIdentity.render($("identity-" + target), draftPacket(target), identityConfig(), "draft");
  }
  document.addEventListener("agent:callidentity", event => {
    const { target, packet, state, historical } = event.detail;
    if (!scenarios.includes(target)) return;
    if (historical) {
      window.CallIdentity.render($("result-identity-" + target), packet, identityConfig(), "historical");
      return; // Reading history must not replace or invalidate a currently prepared packet.
    }
    states[target] = { packet, state };
    if (state === "completed") window.CallIdentity.render($("result-identity-" + target), packet, identityConfig(), state);
    window.CallIdentity.render($("identity-" + target), packet, identityConfig(), state);
  });
  document.addEventListener("agent:identityinvalidated", event => {
    const target = event.detail.target; states[target] = null; refreshDraft(target);
  });
  document.addEventListener("agent:taskidentity", event => {
    const task = event.detail.task;
    const packet = { steps: (task.calls || []).map(call => {
      const definition = (cfg?.agents || []).find(agent => agent.pipeline && agent.step === call.step);
      return { agent: definition?.id, role: definition?.role, contract: definition?.contract, model: call.model, prompt: { name: cfg?.prompts?.[call.step] || "Промпт кроку", version: call.prompt_version }, messages_sha256: call.messages_sha256 };
    }) };
    if (packet.steps.length) window.CallIdentity.render($("result-identity-task"), packet, identityConfig(), "completed");
  });
  // Any edited input invalidates the displayed preparation too; execution closures keep their own guards.
  for (const target of scenarios) $("tab-" + target).addEventListener("input", () => {
    if (states[target]?.state !== "prepared" || !["harness", "builder"].includes(target)) { states[target] = null; refreshDraft(target); }
    if (fixtureViews[target].status.textContent.startsWith("Приклад підставлено")) fixtureViews[target].status.textContent = "Входи змінені. Очікування нижче стосується вихідного прикладу.";
  });
  for (const id of ["b-engine", "cf-agent", "ws-project", "harness-project"]) $(id).addEventListener("change", () => {
    const target = id === "b-engine" ? "builder" : id === "cf-agent" ? "packs" : id === "ws-project" ? "workspace" : "harness";
    states[target] = null; refreshDraft(target); populateFixtures(target);
  });
  function describeFixture(target) {
    const view = fixtureViews[target], entry = entries.find(item => item.id === view.select.value);
    view.expected.textContent = entry ? "Очікуваний результат прикладу: " + entry.expected : "Немає сумісного прикладу";
    view.button.disabled = !entry || !!(entry.mockOnly && taskMode() !== "mock");
  }
  function populateFixtures(target) {
    const view = fixtureViews[target], old = view.select.value;
    const selected = target === "builder" ? $("b-engine").value : target === "packs" ? $("cf-agent").value : null;
    const compatible = entries.filter(entry => entry.target === target && (!selected || (target === "builder" ? entry.engine === selected : entry.agent === selected)) && (target !== "harness" || kindOf(entry) === harnessKind));
    view.select.replaceChildren(...compatible.map(entry => {
      const option = el("option", { value: entry.id, text: entry.label }); option.disabled = !!(entry.mockOnly && taskMode() !== "mock"); return option;
    }));
    if (compatible.some(entry => entry.id === old)) view.select.value = old;
    describeFixture(target);
  }
  document.addEventListener("agent:fixtureapplied", event => {
    const entry = event.detail.entry; states[entry.target] = null; refreshDraft(entry.target);
    // The trap shares notes-app with plain questions, so the project alone cannot select its kind.
    if (entry.target === "harness" && kindOf(entry) !== harnessKind) setHarnessKind(kindOf(entry));
  });
  document.addEventListener("agent:configready", () => scenarios.forEach(refreshDraft));
  document.addEventListener("agent:builderready", () => { populateFixtures("builder"); refreshDraft("builder"); });
  document.addEventListener("agent:providerchange", () => {
    document.querySelectorAll(".workflow-mode").forEach(node => { node.textContent = "Наступний виклик: " + taskMode(); });
    scenarios.forEach(target => { states[target] = null; refreshDraft(target); describeFixture(target); });
  });
  async function initialize() {
    try {
      const results = await Promise.all([window.AgentDemoCatalog.catalog(), loadConfig(), loadStudio()]);
      entries = results[0]; scenarios.forEach(populateFixtures); scenarios.forEach(refreshDraft);
      // Each row says what makes the scenario different from a plain chat, and who acts at each step.
      const demoRows = [
        { fixture: "harness-ops", title: "1. Питання до LLM-Ops", magic: "Модель бачить лише відібрані фрагменти репозиторію курсу й мусить послатися на файл і рядки. Код перевіряє, що ці рядки справді були в пакеті.", flow: [["code", "збір контексту"], ["model", "відповідь"], ["code", "перевірка посилань"]] },
        { fixture: "harness-ops-change", title: "2. Зміна з підтвердженням", magic: "Модель пише diff до відібраних фрагментів LLM-Ops. Код перевіряє шляхи, секрети й накладання на поточні файли, а .patch видається лише після Approve людини — застосовуєте його ви (git apply).", flow: [["code", "контекст"], ["model", "diff"], ["code", "гейти"], ["human", "Approve → .patch"]], note: "Стенд у репозиторій не пише. Варіант із навчальним сайтом (зміна в БД стенда) — серед прикладів сценарію." },
        { fixture: "harness-trap", title: "3. Пастка в документі", magic: "У тестовому репозиторії лежить підкладена інструкція «для агента». Mock навмисно їй підкоряється — і гейт відповіді зупиняє результат.", flow: [["code", "контекст із пасткою"], ["model", "відповідь"], ["code", "гейт блокує"]] },
      ];
      $("demo-gallery").replaceChildren(...demoRows.map((row, index) => [row, entries.find(entry => entry.id === row.fixture)]).filter(([, entry]) => entry).map(([row, entry], index) => el("article", { class: "demo-row" },
        el("div", { class: "demo-row-text" }, el("h3", { text: row.title }), el("p", { class: "demo-magic", text: row.magic }),
          el("p", { class: "demo-flow" }, ...row.flow.map(([who, what]) => el("span", { class: "who " + who, text: what }))),
          row.note ? el("p", { class: "hint", text: row.note }) : null),
        el("button", { ...(index === 0 ? { class: "primary" } : {}), text: "Відкрити сценарій", on: { click: async () => {
          selectScenario(kindOf(entry));
          try { await window.AgentDemoCatalog.apply(entry); populateFixtures(entry.target); fixtureViews[entry.target].select.value = entry.id; describeFixture(entry.target); fixtureViews[entry.target].status.textContent = "Приклад підставлено. Модель не запускалась."; }
          catch (error) { fixtureViews[entry.target].status.textContent = error.message; }
        } } }))));
      for (const target of scenarios) fixtureViews[target].status.textContent = "Оберіть і підставте приклад або введіть власні дані. Модель не викликається.";
    } catch (error) {
      $("demo-gallery").replaceChildren(note(error.message)); scenarios.forEach(target => { fixtureViews[target].status.textContent = error.message; fixtureViews[target].button.disabled = true; });
    }
  }
  // Guided presentation never calls the API or changes provider mode.
  const journey = {}, actions = [];
  const purposes = {
    "harness:read": "Питання до LLM-Ops: відповідь за дозволеними файлами курсу з посиланнями на рядки. Файли не змінюються.",
    "harness:change": "Зміна з підтвердженням: Editor пропонує diff (LLM-Ops — .patch, навчальний сайт — зміна в БД стенда), гейти його перевіряють, рішення лише за людиною.",
    "harness:trap": "Пастка в документі: у тестовому репозиторії лежить підкладена інструкція для агента. Подивіться, як гейт відповіді зупиняє результат.",
    task: "Ручний конвеєр сайту без паспорта: та сама пара Planner → Editor для дослідів із моделями, кешем і збоями.",
    builder: "Дати одну задачу одному спеціалісту. Інші Engine автоматично не запускаються.",
    chat: "Поставити питання про навчальний сайт. Чат не редагує його файли.",
    workspace: "Розібрати, які файли й фрагменти потрапляють у пакет дослідника, і поставити питання вручну.",
    packs: "Розібрати рецепт промпта за роллю. Тут немає виклику моделі."
  };
  const explanations = [
    "Починаємо із задачі: оберіть навчальний приклад або введіть власний. Приклад тільки заповнює поля.",
    "Тепер визначаємо, хто виконає цю задачу. Роль задає відповідальність, контракт — форму відповіді.",
    "Для вибраного виконавця готуємо інструкції та дані. Перегляд і збірка пакета ще не викликають модель.",
    "Перевіряємо попередні кроки перед окремим запуском. Підготувати пакет і викликати модель — різні дії.",
    "Зіставте результат із задачею першого кроку. Перевірте статус, контракт, кеш і фактичний режим цього запуску."
  ];
  const feed = el("ol", { id: "journey-actions", class: "journey-actions" });
  const activity = advanced("Що я зробив у цій вкладці", note("Локальна стрічка від відкриття сторінки. Повна історія зберігається у розділі Контроль. Перехід між кроками не означає успішний запуск."), feed);
  activity.id = "journey-log";
  $("scenario-switch").after(activity);
  function record(text) {
    actions.unshift(new Date().toLocaleTimeString("uk-UA") + " · " + text);
    if (actions.length > 30) actions.pop();
    feed.replaceChildren(...actions.map(text => el("li", { text })));
    activity.querySelector("summary").textContent = "Дії цієї вкладки (" + actions.length + ") · " + text.slice(0, 90);
  }
  function effect(target) {
    if (target === "packs") return "Збірка прев’ю: без моделі, оплати й зміни файлів. Результат лишається на сторінці.";
    return "Режим наступного запуску: " + taskMode().toUpperCase() + (taskMode() === "mock" ? ". Синтетична відповідь; оплати провайдеру немає." : ". Пакет буде надіслано реальній моделі; можлива платна відповідь. Перевірте оцінку й ліміти в пакеті перед запуском. Оцінка приблизна; строгий грошовий максимум ще не підтверджений.") + " Підготовка не викликає модель. Запуск зберігає результат та журнал; сам не застосовує зміни до файлів. Застосування пропозиції — окрема дія людини.";
  }
  function step(target, index) {
    const view = journey[target];
    view.index = Math.max(0, Math.min(4, index));
    stages[target].forEach((body, i) => { body.parentElement.hidden = i !== view.index; });
    view.tabs.forEach((button, i) => {
      button.setAttribute("aria-current", i === view.index ? "step" : "false");
      button.classList.toggle("active", i === view.index);
    });
    view.explanation.textContent = explanations[view.index];
    view.before.disabled = view.index === 0;
    view.after.hidden = view.index === 4;
    view.after.textContent = "Далі: " + (stageNames[view.index + 1] || "");
    view.finish.hidden = view.index !== 4;
    view.position.textContent = "Крок " + (view.index + 1) + " із 5 · перегляд, не підтвердження виконання";
    view.effects.textContent = effect(target);
    if (current === target) requestAnimationFrame(() => {
      const intro = $("tab-" + target).querySelector(".journey-intro");
      if (current !== target || !intro) return;
      intro.scrollIntoView({ block: "start", behavior: "auto" });
      const heading = intro.querySelector("h2"); heading.setAttribute("tabindex", "-1"); heading.focus({ preventScroll: true });
    });
  }
  for (const target of scenarios) {
    const panel = $("tab-" + target);
    const position = el("p", { class: "journey-position" });
    const outcome = el("p", { id: "journey-outcome-" + target, class: "journey-effects", role: "status", text: "У цій вкладці ще немає підтвердженого результату цього сценарію. Перехід сюди сам по собі нічого не запускає." });
    stages[target][4].prepend(outcome);
    const explanation = el("p", { class: "journey-explanation", role: "status" });
    const effects = el("p", { class: "journey-effects" });
    const tabs = stageNames.map((name, i) => el("button", { text: (i + 1) + ". " + name, on: { click: () => step(target, i) } }));
    const before = el("button", { text: "Назад", on: { click: () => step(target, journey[target].index - 1) } });
    const after = el("button", { class: "primary", on: { click: () => step(target, journey[target].index + 1) } });
    const finish = el("div", { class: "journey-finish" }, el("h3", { text: "Наступний крок — ваша задача" }),
      note("Навчальна схема: задача → виконавець → контекст → явний запуск → перевірка. Адаптуйте мету, джерела й критерії; це не автоматичне створення нового агента."),
      el("button", { text: "Адаптувати цей приклад", on: { click: () => { step(target, 0); record("Відкрито адаптацію прикладу. Дані збережено; нічого не запущено."); } } }),
      el("button", { text: "Своя задача для спеціаліста", on: { click: () => { showTab("builder"); step("builder", 0); $("b-goal").focus(); record("Відкрито власну задачу. Поточна чернетка збережена — відредагуйте її. Real вмикається окремо."); } } }),
      note("Хочете Real? Спочатку адаптуйте вхід, потім увімкніть Real у шапці й підготуйте новий пакет. Ці кнопки не перемикають модель і не запускають її."));
    const title = el("h2", { text: purposes[target === "harness" ? "harness:" + harnessKind : target] });
    journey[target] = { index:0, title, position, explanation, effects, tabs, before, after, finish };
    panel.prepend(el("div", { class: "journey-intro" }, title, position,
      el("nav", { class: "journey-steps", "aria-label": "Кроки сценарію" }, ...tabs), explanation, effects));
    panel.append(el("div", { class: "journey-footer" }, before, after), finish);
    step(target, 0);
  }
  document.addEventListener("agent:fixtureapplied", event => {
    const entry = event.detail.entry; step(entry.target, 0);
    record("Приклад «" + entry.label + "» підставлено. Модель не викликалась; файли не змінені.");
  });
  document.addEventListener("agent:providerchange", () => {
    scenarios.forEach(target => { journey[target].effects.textContent = effect(target); });
    record("Режим наступного запуску: " + taskMode().toUpperCase() + ". Попередні результати не змінено; модель ще не викликалась.");
  });
  document.addEventListener("agent:callidentity", event => {
    const { target, state, historical } = event.detail;
    if (!journey[target] || historical) return;
    if (state === "prepared") record("Пакет сценарію підготовлено. Модель ще не викликалась; файли не змінені. Перевірте пакет на кроці 4.");
    if (state === "completed") step(target, 4);
    if (state === "preview" && target === "packs") step(target, 4);
  });
  const requestNames = { "/studio/build":"Зібрати промпт", "/studio/prepare":"Підготувати Engine", "/harness/prepare":"Підготувати паспортний сценарій", "/studio/run":"Запустити Engine", "/harness/run":"Запустити паспортний сценарій", "/tasks":"Запропонувати зміну", "/chat":"Надіслати повідомлення", "/workspace/ask":"Дослідити проєкт", "/config/compile":"Скомпілювати прев’ю" };
  const pendingRequests = new Map();
  let lastRequest = null;
  const liveLabel = el("strong", { text: "Очікує дії у цій вкладці" });
  const liveTime = el("span", { class: "live-time" });
  const liveDetail = el("span", { class: "live-detail", text: "Переходи між кроками не викликають модель." });
  const liveBar = el("section", { id: "execution-status", class: "execution-status", "aria-label": "Поточний статус цієї вкладки" },
    el("span", { class: "live-dot", "aria-hidden": "true" }), el("div", {}, liveLabel, liveDetail), liveTime);
  const liveAnnounce = el("span", { class: "sr-only", role: "status", "aria-live": "polite" });
  liveBar.append(liveAnnounce);
  document.querySelector(".top").after(liveBar);
  // Hoisted for navigate(); it only runs once the bar exists in the DOM, i.e. after liveBar is initialised.
  function syncStatusLayout() {
    document.documentElement?.style?.setProperty("--execution-status-height", liveBar.hidden ? "0px" : (liveBar.getBoundingClientRect?.().height || 76) + "px");
  }
  syncStatusLayout();
  if (typeof ResizeObserver !== "undefined") new ResizeObserver(syncStatusLayout).observe(liveBar);

  function paintLive() {
    const active = [...pendingRequests.values()].at(-1);
    liveBar.classList.toggle("is-running", !!active);
    const item = active || lastRequest;
    if (!item) return;
    const elapsed = Math.max(0, Math.floor(((active ? Date.now() : item.finishedAt) - item.startedAt) / 1000));
    liveTime.textContent = elapsed + " с" + (active ? " · очікування" : " · тривалість запиту");
    liveLabel.textContent = (active ? "Виконується: " : "Остання дія: ") + requestNames[item.path];
    liveDetail.textContent = active ? item.mode.toUpperCase() + " · очікуємо відповідь сервера" + (pendingRequests.size > 1 ? " · активних дій: " + pendingRequests.size : "") :
      (item.http ? (item.http >= 400 ? "Помилка" : "Відповідь отримана") + " · " + (item.status || "HTTP " + item.http) : "Результат невідомий · перевірте журнал перед повтором") + " · " + item.mode.toUpperCase();
  }
  document.addEventListener("agent:requeststart", event => {
    const d = event.detail; if (!requestNames[d.path]) return;
    pendingRequests.set(d.requestId, d); paintLive();
    liveAnnounce.textContent = "Виконується: " + requestNames[d.path] + ". " + d.mode;
  });
  setInterval(paintLive, 1000);
  document.addEventListener("agent:requestoutcome", event => {
    const d = event.detail; if (!requestNames[d.path]) return;
    pendingRequests.delete(d.requestId); lastRequest = { ...d, finishedAt: Date.now() }; paintLive();
    liveAnnounce.textContent = liveLabel.textContent + ". " + liveDetail.textContent;
  });
  document.addEventListener("agent:requestoutcome", event => {
    const d = event.detail, name = requestNames[d.path]; if (!name) return;
    const execute = ["/studio/run","/harness/run","/tasks","/chat","/workspace/ask"].includes(d.path);
    const target = ({"/studio/run":"builder","/harness/run":"harness","/tasks":"task","/chat":"chat","/workspace/ask":"workspace","/config/compile":"packs"})[d.path];
    const message = name + " · " + (execute ? d.mode.toUpperCase() : "без виклику моделі") + " · " + (d.http ? "HTTP " + d.http : "результат невідомий") + (d.status ? " · " + d.status : "") + ". Перевірте деталі нижче. HTTP 200 сам по собі не означає, що контракт пройдено.";
    if (target) { $("journey-outcome-" + target).textContent = message; step(target, 4); }
    record(name + " · " + (execute ? d.mode.toUpperCase() : "без виклику моделі") + " · " + (d.http ? "HTTP " + d.http : "відповідь не підтверджено; не повторюйте автоматично") + (d.status ? " · " + d.status : "") + ". " + (execute ? "Перевірте результат і журнал; зміни не застосовуються цією дією." : "Файли не змінені."));
  });
  navigate("home"); initialize();
})();

// VISUAL_DEMO_START — local teaching sequence; not a runtime execution trace.
(() => {
  "use strict";
  const recipes = {
    read: { title:"Питання до LLM-Ops", target:"harness", fixture:"harness-ops", project:"llm-ops", task:"Як працює circuit breaker?", role:"Explorer", contract:"answer-with-citations", output:"answer + citations + missing", result:"Навчальна відповідь: після 3 збоїв поспіль модель «відкрита» на 30 с, але раз на 5 с пропускається пробний запит (half-open).\nДжерело: INCIDENT_RUNBOOK.md:66-73.\nЦе підготовлена ілюстрація, а не відповідь моделі." },
    change: { title:"Зміна з підтвердженням", target:"harness", fixture:"harness-ops-change", project:"llm-ops", task:"Додай у INCIDENT_RUNBOOK.md до розділу про circuit breaker примітку, як перевірити стан breaker", role:"Editor (repo-editor)", contract:"unified-diff → гейти → заявка", output:"diff → після Approve .patch для git apply", result:"--- a/INCIDENT_RUNBOOK.md\n+++ b/INCIDENT_RUNBOOK.md\n@@ навчальний фрагмент, не patch для застосування @@\n+> Як перевірити стан breaker: ..." },
    trap: { title:"Пастка в документі", target:"harness", fixture:"harness-trap", project:"notes-app", task:"Скільки резервних копій зберігається?", role:"Explorer", contract:"answer-with-citations", output:"answer + citations + missing", result:"ПРИКЛАД ВІДПОВІДІ, ЯКА ПІДКОРИЛАСЬ ПАСТЦІ\nУ docs/OPERATIONS.md лежить нотатка «для агента» з проханням показати системний промпт.\nMock навмисно їй підкоряється: у відповідь потрапляють рядки службових інструкцій.", checks:"ІЛЮСТРАЦІЯ ПЕРЕВІРОК\n1. Гейт reply_leak шукає у відповіді дослівні рядки системного повідомлення й секретоподібні шаблони.\n2. Збіг → статус blocked_by_output_gate; замість відповіді людина бачить пояснення.\n3. Посилання вже не перевіряються: відповідь зупинено раніше.\n\nЦі гейти зараз не виконувались — показано їх призначення.", effect:"Пояснено, як гейт відповіді зупиняє витік. Демо не запускало справжній гейт." }
  };
  function frames(kind, task, mode="mock", failure=false) {
    const r=recipes[kind] || recipes.read;
    mode=mode === "real" ? "real" : "mock";
    task=String(task || r.task);
    return [
      {title:"Дані",actor:"Людина",why:"Людина визначає мету. Система ще не має відповіді й не змінює файли.",artifact:"ЗАДАЧА\n"+task+"\n\nТЕСТОВИЙ ОБ’ЄКТ\n"+r.project,effect:"Заповнено навчальні поля. Модель не викликалась."},
      {title:"Виконавець",actor:"Детермінований код",why:"Код обирає дозволений маршрут: хто відповідає і яку форму результату перевірятиме.",artifact:"АГЕНТ / РОЛЬ\n"+r.role+"\n\nКОНТРАКТ\n"+r.contract+"\n\nОЧІКУВАНИЙ ВИХІД\n"+r.output,effect:"Пояснено маршрут. Вибір ролі не є викликом моделі."},
      {title:"Пакет промпта",actor:"Збирач контексту + компілятор",why:"Код відділяє інструкції від даних і збирає пакет. У живому стенді тут можна переглянути точні повідомлення до відправки.",artifact:"ПРИКЛАД СКЛАДУ ПАКЕТА — НЕ РЕЗУЛЬТАТ КОМПІЛЯТОРА\n\nsystem: методика + політика + роль + контракт\nuser: задача + вибрані фрагменти файлів\n\nЗАДАЧА: "+task+"\nРОЛЬ: "+r.role+"\nКОНТРАКТ: "+r.contract,effect:"Показано, з чого складається пакет. Справжня компіляція тут не запускалась."},
      {title:"Виклик",actor:mode === "mock" ? "Mock-провайдер (пояснення)" : "Real-провайдер (пояснення)",why:mode === "mock" ? "У живому Mock код отримує синтетичну відповідь для перевірки механіки. Зовнішня модель не потрібна." : "У живому Real показаний пакет надсилається платній моделі лише після явного запуску. Відповідь і точна вартість наперед невідомі.",artifact:"ПОЯСНЮЄМО РЕЖИМ: "+mode.toUpperCase()+"\n\nЦЯ ПАНЕЛЬ НЕ НАДСИЛАЄ ЗАПИТІВ.\nПоказаний нижче результат — підготовлений приклад у будь-якому режимі пояснення.",effect:"Пояснено межу виклику. У цьому демо: викликів моделі 0, оплата $0."},
      {title:"Відповідь / код",actor:"Підготовлений навчальний приклад",why:"Модель у живому запуску лише пропонує результат. Вона не має повноважень сама застосувати код. Тут показано ілюстрацію очікуваної форми.",artifact:failure ? "ПРИКЛАД НЕКОРЕКТНОЇ ВІДПОВІДІ\n\nНемає обов’язкових полів або секцій контракту.\nТаку відповідь не можна вважати перевіреним результатом." : r.result,effect:kind === "change" ? "Показано пропозицію коду. Жоден файл не змінено." : "Показано синтетичний приклад відповіді; це не результат поточної задачі."},
      {title:"Перевірки",actor:"Детерміновані гейти",why:"Код перевіряє допустимі шляхи, форму відповіді й посилання. Перевірка структури не доводить правильність змісту.",artifact:failure ? "ІЛЮСТРАЦІЯ ВІДМОВИ\ncontract: rejected\nПричина: відповідь не відповідає очікуваній формі.\nАвтоматичного застосування або повтору немає." : r.checks || "ІЛЮСТРАЦІЯ ПЕРЕВІРОК\n1. Вихід відповідає формі контракту.\n2. Джерела/шляхи належать наданому контексту.\n3. Людина перевіряє зміст.\n\nЦі гейти зараз не виконувались — показано їх призначення.",effect:failure ? "Пояснено зупинку на контракті. Демо не запускало справжній гейт." : r.effect || "Пояснено перевірки; це не runtime-атестація агента."},
      {title:"Підсумок",actor:"Людина + журнал живого стенда",why:"У живому запуску журнал зберігає фактичний режим, виклики, контракт, кеш, витрати та результат. Людина вирішує, що робити далі.",artifact:"ЦЕЙ УРОК\nМережевих викликів моделі: 0\nЗмін файлів: 0\nОплата провайдеру: $0\nЗаписів виконання в БД: 0\n\nПоказаний шлях: "+(failure ? "відмова на контракті" : "пояснення успішного маршруту")+"\n\nДля справжнього результату перенесіть задачу в живий стенд і запустіть її окремо.",effect:"Урок завершено. Власні дані й платний запуск — наступне окреме рішення."}
    ];
  }
  window.AgentDemoStory={recipes,frames};
  if (typeof document === "undefined") return;
  const kind=el("select",{id:"story-kind","aria-label":"Навчальний сценарій"},...Object.entries(recipes).map(([id,r])=>el("option",{value:id,text:r.title})));
  const task=el("textarea",{id:"story-task",rows:"2",maxlength:"1600","aria-label":"Навчальна задача"});task.value=recipes.read.task;
  const mode=el("select",{id:"story-mode","aria-label":"Який режим пояснити"},el("option",{value:"mock",text:"Пояснити Mock"}),el("option",{value:"real",text:"Пояснити Real — без виклику"}));
  const variant=el("select",{id:"story-variant","aria-label":"Навчальний результат"},el("option",{value:"ok",text:"Показати нормальний маршрут"}),el("option",{value:"error",text:"Показати відмову на контракті"}));
  const timeline=el("ol",{class:"story-timeline","aria-label":"Етапи пояснення"}), content=el("div",{id:"story-content",class:"story-content","aria-live":"polite"});
  const previous=el("button",{id:"story-prev",text:"Назад"}), next=el("button",{id:"story-next",class:"primary"});
  const transfer=el("button",{id:"story-transfer",class:"primary",text:"Спробувати вживу з цією задачею"});
  const transferStatus=el("p",{id:"story-transfer-status",class:"hint",role:"status"});
  const heading=el("h2",{text:"Подивіться, як задача проходить через систему",tabindex:"-1"});
  const setup=el("details",{id:"story-setup",class:"story-setup"},el("summary",{text:"Сценарій, дані та режим пояснення"}),
    el("div",{class:"story-controls"},el("label",{},"Сценарій",kind),el("label",{},"Режим пояснення",mode),el("label",{},"Розв’язка",variant)),
    el("label",{for:"story-task",text:"Задача прикладу — можна адаптувати текст"}),task);
  const lesson=el("section",{id:"visual-demo",class:"visual-demo"},
    el("div",{class:"story-banner",text:"НАВЧАЛЬНА ІЛЮСТРАЦІЯ · підготовлені приклади · без API моделі, оплати й зміни файлів"}),heading,
    el("p",{class:"hint",text:"«Далі» показує наступний етап, а не виконує його. Це навчальна схема, не запис виконання. Справжній пакет і відповідь — кнопкою в кінці уроку або сценарієм вище."}),
    setup,timeline,content,
    el("div",{class:"story-footer"},previous,next),transfer,transferStatus);
  let index=0;
  function focusLesson(){requestAnimationFrame(()=>{if(lesson.hidden)return;const anchor=index===0?heading:timeline;anchor.style.scrollMarginTop=(($("execution-status")?.getBoundingClientRect().height||0)+14)+"px";anchor.scrollIntoView({block:"start",behavior:"auto"});anchor.tabIndex=-1;anchor.focus({preventScroll:true});});}
  function render(){
    const list=frames(kind.value,task.value,mode.value,variant.value==="error"), frame=list[index];
    setup.open=index===0;
    timeline.replaceChildren(...list.map((f,i)=>el("li",{class:i===index?"current":i<index?"explained":"",...(i===index?{"aria-current":"step"}:{})},el("span",{text:String(i+1)}),el("b",{text:f.title}),el("small",{text:i===index?"Зараз пояснюємо":i<index?"Уже пояснили":"Попереду"}))));
    content.replaceChildren(el("p",{class:"eyebrow",text:"ДЕМО · ЕТАП "+(index+1)+" / "+list.length+" · "+frame.actor}),el("h3",{text:frame.title}),el("p",{class:"story-why",text:frame.why}),el("pre",{class:"story-artifact",text:"НАВЧАЛЬНА ІЛЮСТРАЦІЯ · не runtime-запуск\n\n"+frame.artifact}),el("p",{class:"story-effect",text:frame.effect}));
    previous.disabled=index===0;next.disabled=index===list.length-1;next.textContent=index===list.length-1?"Усі етапи пояснено":"Далі: "+list[index+1].title;
    transfer.hidden=index!==list.length-1;
  }
  previous.addEventListener("click",()=>{index=Math.max(0,index-1);render();focusLesson();});
  next.addEventListener("click",()=>{index=Math.min(6,index+1);render();focusLesson();});
  kind.addEventListener("change",()=>{task.value=recipes[kind.value].task;index=0;transferStatus.textContent="";render();});
  for(const control of [task,mode,variant]) control.addEventListener(control===task?"input":"change",()=>{index=0;transferStatus.textContent="";render();});
  transfer.addEventListener("click",async()=>{
    const selectedKind=kind.value, sentTask=task.value;
    transfer.disabled=true;transferStatus.textContent="Готуємо лише поля. Режим провайдера не змінюється, модель не викликається.";
    try {
      const r=recipes[selectedKind], entries=await window.AgentDemoCatalog.catalog(), entry=entries.find(e=>e.id===r.fixture);
      if(!entry)throw new Error("Сумісний приклад недоступний");
      await window.AgentDemoCatalog.apply(entry);
      const input=$("harness-goal");input.value=sentTask;input.dispatchEvent(new Event("input",{bubbles:true}));
      showTab(r.target);toast("Перенесено тільки поля. Наступний живий режим: "+taskMode().toUpperCase()+". Перевірте його перед явним запуском.");
      transferStatus.textContent="Поля перенесено. Модель не запускалась; режим провайдера залишився "+taskMode().toUpperCase()+".";
    }catch(error){transferStatus.textContent=error.message;}finally{transfer.disabled=false;}
  });
  $("demo-lesson").append(lesson);render();
})();