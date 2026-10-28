// Project passport: prepare -> inspect frozen packet -> explicit run. All API text uses textContent.
"use strict";
(() => {
  const root = $("harness-root");
  if (!root) return;
  let preparation = null;
  let generation = 0;
  let passportRequest = 0;
  let busy = false;
  let expiresAt = 0;
  let expiryTimer;
  let realAvailable = false;
  let projects = [];
  // Task kind comes from the scenario switch: a question (read) or a change; demo-site is always a change.
  let taskKind = "read";

  const select = (id, values) => el("select", { id }, ...values.map(([value, label]) =>
    el("option", { value, text: label })));
  const project = select("harness-project", []);
  const profile = select("harness-profile", [["quick", "Коротко"], ["thorough", "Детально"]]);
  const profileNames = { quick: "Коротко", thorough: "Детально" };
  const environment = select("harness-environment", [["lab", "Lab · зміни дозволені (сайт, .patch)"], ["staging", "Dev · лише читання"], ["production", "Prod · лише читання"]]);
  const risk = select("harness-risk", [["normal", "Звичайна"], ["critical", "Критична · потрібне підтвердження"]]);
  const cache = el("input", { id: "harness-cache", type: "checkbox" });
  cache.checked = true;
  const mode = select("harness-mode", [["mock", "mock — синтетична модель"], ["real", "real — платний DeepSeek"]]);
  mode.options[1].disabled = true;
  const goal = el("textarea", { id: "harness-goal", rows: "3", maxlength: "2000", placeholder: "Який результат потрібен?" });
  const confirmation = el("input", { id: "harness-confirm", type: "checkbox" });
  const confirmationBox = el("label", { class: "check", hidden: "hidden" }, confirmation,
    "Підтверджую Critical дію для цього підготовленого пакета");
  const prepareButton = el("button", { id: "harness-prepare", class: "primary", text: "1. Підготувати пакет" });
  const runButton = el("button", { id: "harness-run", text: "2. Виконати підготовлене" });
  const status = el("p", { id: "harness-status", class: "hint", role: "status", text: "Оберіть проєкт і мету. Підготовка не запускає модель." });
  const cards = el("div", { id: "harness-projects", class: "harness-projects" });
  const passportView = el("div", { id: "harness-passport", class: "contracts" });
  const packetView = el("div", { id: "harness-packet" });
  const resultView = el("div", { id: "harness-result", "aria-live": "polite" });
  const profileHint = el("p", { id: "harness-profile-hint", class: "hint", text: "Обсяг змінює лише ліміти контексту й відповіді; модель та сама." });
  const routeView = el("div", { id: "harness-route", class: "explain model" });
  const contextView = el("div", { id: "harness-context", class: "explain code" });
  let classModels = {};
  const field = (label, control) => el("label", { class: "harness-field" }, el("span", { class: "muted small", text: label }), control);
  const json = (value) => el("pre", { class: "body-view harness-json", text: JSON.stringify(value ?? {}, null, 2) });
  const details = (title, value) => el("details", { class: "tech" }, el("summary", { text: title }), json(value));
  const stageTitles = { planner: "Planner · план і вибір файлів", editor: "Editor · пропозиція diff", repo_editor: "Editor · diff до фрагментів репозиторію",
    context_builder: "Збирач контексту · відбір файлів",
    explorer: "Explorer · відповідь із посиланнями", checker: "Гейти · формат, шляхи, посилання", human: "Approve / Reject окремою дією" };
  const stageKinds = { model: ["model", "Модель"], deterministic: ["code", "Код"], approval: ["human", "Людина"] };
  const usd = (value) => value == null ? "невідомо" : "$" + Number(value).toFixed(4);
  // Human-readable view of the frozen packet; the exact JSON stays available below it.
  function packetStep(step, index, total, executionMode) {
    const estimate = step.estimate || {}, blocks = step.blocks || [], gate = step.outgoing_gate || {};
    const contract = typeof step.contract === "string" ? step.contract : step.contract?.title || step.contract?.id;
    const rows = [["Агент", step.agent], ["Контракт", contract],
      ["Промпт", step.prompt ? step.prompt.name + " · " + step.prompt.version : "методика без версії реєстру"],
      ["Модель", (step.route?.resolved_model || "—") + (step.route?.chosen_model_class ? " · клас " + step.route.chosen_model_class : "")],
      ["Вхід", (estimate.prompt_tokens ?? "—") + " токенів · блоків: " + blocks.length],
      ["Оцінка", executionMode === "real" ? "до " + usd(estimate.real_upper_usd) + " при виході до " + (estimate.real_max_tokens ?? "—") + " токенів"
        : "Mock без оплати · умовна синтетична ціна " + usd(estimate.test_input_usd)],
      ["Вихідний гейт", gate.ok ? "пройдено: секретів і заборонених шаблонів немає" : "спрацював: " + (gate.hits || []).map((hit) => hit.rule).join(", ")]];
    return el("article", { class: "identity-card" },
      el("strong", { text: (total > 1 ? (index + 1) + ". " : "") + (stageTitles[step.agent] || step.title || step.agent || "Крок") }),
      el("dl", { class: "identity-fields" }, ...rows.flatMap(([name, value]) => [el("dt", { text: name }), el("dd", { text: String(value ?? "—") })])),
      blocks.length ? el("details", { class: "tech" }, el("summary", { text: "Що саме піде в модель: " + blocks.length + " блоків" }),
        el("ol", { class: "log" }, ...blocks.map((block) => el("li", { text: block.where + " · " + (block.title || block.id) + " · " + block.trust +
          " · " + (block.tokens ?? "—") + " ток." + (block.ref ? " · " + block.ref : "") })))) : null);
  }
  function renderPacket(prepared, executionMode) {
    const packet = prepared.packet || {}, steps = Array.isArray(packet.steps) ? packet.steps : [packet], limits = packet.execution_limits || {};
    packetView.replaceChildren(...[el("h3", { text: "Підготовлений пакет перед запуском" }),
      el("p", { class: "hint", text: "Дійсний до " + new Date(expiresAt).toLocaleTimeString("uk-UA") + ", одноразовий. Зміна будь-якого налаштування скасовує цю підготовку." }),
      el("ol", { class: "flow-steps" }, ...(prepared.stages || []).map((stage) => {
        const [css, who] = stageKinds[stage.kind] || ["code", stage.kind];
        return el("li", { class: "flow-node " + css }, el("b", { text: who }), el("span", { text: stageTitles[stage.role] || stage.role }));
      })),
      Array.isArray(packet.steps) ? el("p", { class: "hint", text: "Набір файлів для Editor обирає Planner під час запуску; версії промптів і база закріплені вже зараз." }) : null,
      ...steps.map((step, index) => packetStep(step, index, steps.length, executionMode)),
      el("div", { class: "kpis" }, kpi("бюджет контексту", limits.context_budget ?? "—"), kpi("макс. вихід", limits.max_tokens ?? "—"), kpi("повтори", limits.max_retries ?? "—")),
      details("Точний пакет (JSON)", packet)].filter(Boolean)); // replaceChildren would print null as text
  }
  // Plain-language selection rules before preparation; the frozen packet's real numbers after it.
  const isSite = () => project.value === "demo-site";
  const isChange = () => isSite() || taskKind === "change";
  const modelsOf = (cls) => "Mock — " + (classModels[cls]?.test || "—") + ", Real — " + (classModels[cls]?.real || "—");
  const explainer = (title, items, tail) => [el("h3", { text: title }), el("ol", {}, ...items.map((text) => el("li", { text }))),
    tail ? el("p", { class: "hint", text: tail }) : null].filter(Boolean);
  const fragment = (item) => item.path + ":" + item.start + "-" + item.end + (item.heading ? " · " + item.heading : "") +
    " · бали " + item.score + (item.pinned ? " · закріплено" : "") + (item.hits?.length ? " · слова: " + item.hits.join(", ") : "");
  function explainRules() {
    routeView.replaceChildren(...explainer("Яка модель і чому", isSite()
      ? ["Planner — службовий крок, тому завжди клас Fast (" + modelsOf("fast") + ").",
         "Editor — за таблицею routing.yaml: довга задача або Planner оцінив її як складну → клас Balanced (" + modelsOf("balanced") + "), інакше Fast.",
         "У Real обидва кроки йдуть на одну модель із паспорта — так тримається бюджет превʼю."]
      : isChange()
      ? ["Editor робить один виклик моделі; Planner тут немає — файли вже відібрав збирач контексту.",
         "Клас — за таблицею routing.yaml: довга задача → Balanced (" + modelsOf("balanced") + "), інакше Fast (" + modelsOf("fast") + ").",
         "У Real крок іде на модель із паспорта — так тримається бюджет превʼю."]
      : ["Explorer робить один виклик моделі. Модель обирає код за таблицею routing.yaml, а не сама модель.",
         "Відповідь за файлами — службовий крок, тому клас Fast (" + modelsOf("fast") + ").",
         "Обсяг (Коротко / Детально) змінює лише ліміти, модель та сама."],
      "Після «Підготувати пакет» тут буде правило, яке спрацювало для вашої задачі."));
    contextView.replaceChildren(...explainer("Як збирається контекст", isChange() && !isSite()
      ? ["Фрагменти відбираються за словами задачі тим самим конструктором, що й для питань, у межах бюджету обсягу.",
         "Змінювати можна лише файли, фрагменти яких потрапили в пакет, і лише рядки, які модель бачила (з номерами).",
         "Гейти коду: формат diff, шляхи, секрети, розмір і dry-run на поточних файлах робочої копії.",
         "Пройдений diff стає заявкою; після Approve — .patch для git apply. Сам репозиторій стенд не змінює."]
      : isSite()
      ? ["Planner бачить перелік дозволених файлів сайту (база current) і вибирає, що змінювати.",
         "Editor отримує тільки вибрані Planner файли; решта сайту в модель не йде.",
         "Версії файлів і промптів закріплюються під час підготовки: якщо сайт зміниться, пакет доведеться підготувати знову."]
      : ["Беруться лише файли з білого списку проєкту; .env, ключі й секрети виключені завжди.",
         "Файли ріжуться на фрагменти: розділи Markdown, функції й класи Python, решта — по 40 рядків.",
         "Фрагмент отримує бали за слова питання в тексті, заголовку й шляху; рідкісне слово важить більше.",
         "Фрагменти додаються від найкращого, доки вистачає бюджету обсягу; що не влізло, лишається у звіті.",
         "Рядки пронумеровані: модель мусить послатися на файл і рядки, а код перевіряє, що вони були в пакеті."],
      "Після «Підготувати пакет» тут буде, що саме відібрано для вашого питання."));
  }
  function explainPrepared(prepared, executionMode) {
    const packet = prepared.packet || {}, steps = Array.isArray(packet.steps) ? packet.steps : [packet];
    routeView.replaceChildren(...explainer("Яка модель і чому", steps.map((step) => {
      const route = step.route || {};
      return (stageTitles[step.agent] || step.agent || "Крок") + ": клас " + (route.chosen_model_class || "—") + " → " +
        (route.resolved_model || "—") + ". Правило: " + (route.reason || "—") + ".";
    }), isSite() ? (executionMode === "real" ? "У Real обидва кроки йдуть на модель паспорта." : "Клас Editor уточниться після Planner: складна задача → Balanced.") : null));
    const c = prepared.context;
    if (!c) return; // site changes: Planner picks the files at run time, the rules above stay accurate
    const items = c.items || [];
    const asked = isChange() ? "задачі" : "питання";
    contextView.replaceChildren(...[el("h3", { text: "Як зібрано контекст для цієї " + (isChange() ? "задачі" : "відповіді") }),
      el("p", { text: "Слова " + asked + " (основи): " + ((c.terms || []).join(", ") || "немає") }),
      el("p", { class: "context-funnel", text: c.files_total + " файлів у білому списку → " + c.chunks_total + " фрагментів → " +
        c.candidates + " зі збігами → " + items.length + " у пакеті (" + c.used_tokens + " з " + c.budget_tokens + " токенів)" +
        (c.dropped_total ? " · " + c.dropped_total + " не влізли в бюджет" : "") }),
      el("ol", { class: "log" }, ...items.map((item) => el("li", { text: fragment(item) }))),
      (c.dropped || []).length ? el("details", { class: "tech" }, el("summary", { text: "Не влізли в бюджет: " + c.dropped_total }),
        el("ol", { class: "log" }, ...c.dropped.map((item) => el("li", { text: fragment(item) })))) : null,
      c.dropped_total && profile.value === "quick" ? el("p", { class: "hint", text: "Обсяг «Детально» дає більший бюджет — у пакет потрапить більше фрагментів." }) : null].filter(Boolean));
  }
  const settings = () => ({ project: project.value, goal: goal.value.trim(), profile: profile.value,
    environment: environment.value, risk: risk.value, mode: mode.value, cache: cache.checked, kind: isChange() ? "change" : "read" });
  const available = () => projects.some((p) => p.id === project.value && p.available);

  function sync() {
    const valid = preparation && Date.now() < expiresAt;
    prepareButton.disabled = busy || !available() || !goal.value.trim() || (mode.value === "real" && !realAvailable);
    runButton.disabled = busy || !valid || (risk.value === "critical" && !confirmation.checked);
    confirmationBox.hidden = risk.value !== "critical";
  }
  function invalidate(message) {
    generation += 1;
    preparation = null;
    expiresAt = 0;
    clearTimeout(expiryTimer);
    confirmation.checked = false;
    packetView.replaceChildren();
    explainRules();
    document.dispatchEvent(new CustomEvent("agent:identityinvalidated", { detail: { target: "harness" } }));
    if (message) status.textContent = message;
    sync();
  }
  function renderPassport(passport) {
    const selectedProfile = passport.effective_profile || passport.profile;
    const profileName = typeof selectedProfile === "string" ? selectedProfile : selectedProfile?.name || selectedProfile?.id || "—";
    const permissionNames = { read_workspace: "Читання робочого простору", prepare_prompts: "Підготовка промптів",
      propose_demo_changes: "Пропозиції змін demo-сайту", human_apply_demo: "Застосування demo-змін людиною",
      propose_patches: "Пропозиції .patch для репозиторію",
      actual_repository_apply: "Запис у справжній репозиторій" };
    const permissions = Object.entries(passport.permissions || {}).map(([name, allowed]) =>
      el("li", { text: (permissionNames[name] || name) + ": " + (typeof allowed === "boolean" ? (allowed ? "дозволено" : "заборонено") : JSON.stringify(allowed)) }));
    const roles = Array.isArray(passport.roles) ? passport.roles : Object.values(passport.roles || {});
    profileHint.textContent = "Контекст до " + (passport.limits?.context_budget ?? "—") + " токенів, відповідь до " +
      (passport.limits?.max_tokens ?? "—") + ". Модель та сама — змінюються лише ліміти.";
    passportView.replaceChildren(el("h3", { text: "Ефективний паспорт проєкту" }),
      el("div", { class: "kpis" }, kpi("обсяг", profileNames[profileName] || profileName), kpi("версія", passport.version ?? "—"),
        kpi("fingerprint", short(passport.config_sha256 || passport.fingerprint)),
        kpi("контекст", passport.limits?.context_budget ?? "—"), kpi("макс. вихід", passport.limits?.max_tokens ?? "—"),
        kpi("повтори", passport.limits?.max_retries ?? "—")),
      el("h3", { text: "Ефективні дозволи" }), el("ul", { class: "log" }, ...permissions),
      el("h3", { text: "Ролі та версії" }), el("ul", { class: "log" }, ...roles.map((role) =>
        el("li", { text: typeof role === "string" ? role : [role.name, short(role.version), role.path].filter(Boolean).join(" · ") }))),
      details("Профіль і модель", selectedProfile), details("Дозволи", passport.permissions),
      details("Ліміти контексту, виходу та бюджету", passport.limits), details("Ролі й версії інструкцій", passport.roles),
      details("Повний паспорт", passport));
  }

  async function loadPassport() {
    const revision = ++passportRequest;
    if (!project.value) return;
    passportView.replaceChildren(el("p", { class: "muted", text: "Завантаження паспорта…" }));
    const query = new URLSearchParams({ profile: profile.value, environment: environment.value });
    try {
      const response = await api("GET", "/projects/" + encodeURIComponent(project.value) + "/passport?" + query);
      if (revision !== passportRequest) return;
      if (response.status !== 200) throw new Error("HTTP " + response.status);
      renderPassport(response.data);
    } catch (error) {
      if (revision === passportRequest) passportView.replaceChildren(el("p", { class: "hint", text: "Паспорт недоступний: " + error.message }));
    }
  }
  function changed() {
    invalidate("Налаштування змінено. Підготуйте новий пакет перед запуском.");
    loadPassport();
  }
  for (const control of [project, profile, environment, risk, cache]) control.addEventListener("change", changed);
  goal.addEventListener("input", () => invalidate("Мету змінено. Підготуйте новий пакет."));
  confirmation.addEventListener("change", sync);

  prepareButton.addEventListener("click", async () => {
    sync();
    if (prepareButton.disabled) return;
    invalidate();
    const revision = generation;
    const input = settings();
    busy = true;
    sync();
    status.textContent = "Підготовка замороженого пакета…";
    resultView.replaceChildren();
    try {
      const response = await api("POST", "/harness/prepare", input);
      if (revision !== generation) return;
      if (response.status !== 200) throw new Error(response.data?.error || "HTTP " + response.status);
      if (!response.data.preparation_id) throw new Error("Відсутній preparation_id");
      preparation = response.data;
      expiresAt = Date.now() + Math.max(0, Number(preparation.expires_in_seconds) || 0) * 1000;
      renderPassport(preparation.passport);
      document.dispatchEvent(new CustomEvent("agent:callidentity", { detail: { target: "harness", packet: preparation.packet, state: "prepared" } }));
      renderPacket(preparation, input.mode);
      explainPrepared(preparation, input.mode);
      status.textContent = input.mode === "real" ? "Підготовлено real. Наступна кнопка викличе платну модель." : "Підготовлено mock. Перегляньте пакет і виконайте його окремою кнопкою.";
      expiryTimer = setTimeout(() => invalidate("Термін пакета минув. Підготуйте його знову."), Math.max(0, expiresAt - Date.now()));
    } catch (error) {
      if (revision === generation) { preparation = null; status.textContent = "Підготовку не завершено: " + error.message; }
    } finally { busy = false; sync(); }
  });

  runButton.addEventListener("click", async () => {
    sync();
    if (runButton.disabled) return;
    const preparationId = preparation.preparation_id;
    const sentPacket = preparation.packet;
    const sentGeneration = generation;
    const executionMode = mode.value;
    const confirmed = risk.value === "critical" && confirmation.checked;
    preparation = null; // single-use in this UI, including network errors with an uncertain outcome
    clearTimeout(expiryTimer);
    busy = true;
    sync();
    status.textContent = "Виконується підготовлений пакет…";
    try {
      const response = await api("POST", "/harness/run", { preparation_id: preparationId, confirmed });
      if (response.status !== 200) throw new Error(response.data?.error || "HTTP " + response.status);
      const output = response.data;
      document.dispatchEvent(new CustomEvent("agent:callidentity", { detail: { target: "harness", packet: sentPacket, state: "completed", historical: sentGeneration !== generation } }));
      const result = output.result || {};
      const approvalId = result.approval?.approval_id;
      resultView.replaceChildren(el("h3", { text: "Результат: " + (output.status || "—") }),
        ...(result.answer ? [el("h3", { text: "Відповідь за наданими файлами" }), el("div", { class: "msg agent", text: result.answer })] : []),
        ...(!result.answer && result.message ? [el("div", { class: "msg agent", text: result.message })] : []),
        ...(output.kind === "change" && (result.gates || []).length ? [el("h3", { text: "Гейти коду" }), el("ul", { class: "log" },
          ...result.gates.map((g) => el("li", { text: (g.ok ? "✓ " : "✗ ") + g.gate + (g.recounted ? " · лічильники @@ перераховано" : "") })))] : []),
        ...(result.patch?.diff ? [el("h3", { text: approvalId ? "Diff у заявці (канонічний, саме він піде в .patch)" : "Diff моделі" }),
          typeof diffView === "function" ? diffView(result.patch.diff) : el("pre", { class: "diff", text: result.patch.diff })] : []),
        ...(approvalId ? [el("button", { class: "primary", text: "Відкрити заявку " + short(approvalId) + " → Approve / Reject",
          on: { click: () => (typeof openApprovalTab === "function" ? openApprovalTab(approvalId) : showTab("approvals")) } })] : []),
        el("div", { class: "kpis" }, kpi("вартість", priced(result.cost_usd, executionMode)),
          kpi("спроби провайдера", result.accounting?.provider_attempts ?? (typeof result.calls === "number" ? result.calls : "—")),
          kpi("кеш", result.accounting?.cache_hits ?? result.cache_hits ?? "—"),
          kpi("невідома ціна: спроб", result.accounting?.unknown_cost_calls ?? "—")),
        details("Ефективний паспорт виконання", output.passport), details("Виконані етапи", output.stages),
        details("Результат, виклики, токени та оцінки вартості", output.result));
      status.textContent = sentGeneration !== generation ? "Історичний результат: налаштування змінені під час виконання. Показано попередній пакет; підготуйте новий для поточних входів." : "Виконання завершилося. Перевірте результат та журнал; бюджет показує оцінку, не рахунок провайдера.";
      refreshToday();
    } catch (error) {
      status.textContent = "Результат не підтверджено: " + error.message + ". Звірте журнал виконань перед новим запуском; автоматичного повтору немає.";
    } finally { busy = false; sync(); }
  });

  root.replaceChildren(el("div", { class: "card" }, el("h2", { text: "Паспорт проєкту · підготовка й запуск" }),
    el("p", { class: "hint", text: "Обсяг (Коротко / Детально) — та сама модель з різними лімітами контексту й відповіді. Середовище (Lab / Dev / Prod) задає дозволи, а не виконує deploy. Критична дія вимагає окремого підтвердження." }),
    cards, el("div", { class: "harness-controls" }, field("Проєкт", project), field("Обсяг", profile),
      field("Середовище (дозволи)", environment), field("Ризик дії", risk), field("Модель", mode)),
    profileHint, routeView, contextView,
    el("label", { class: "check" }, cache, "Кеш точних кроків (зніміть позначку для свіжого виклику)"),
    field("Мета", goal), el("div", { class: "row" }, prepareButton, runButton), confirmationBox, status),
    el("div", { class: "grid2" }, el("div", { class: "card" }, passportView), el("div", { class: "card" }, packetView)),
    el("div", { class: "card" }, resultView, el("button", { class: "link", text: "Відкрити журнал виконань →", on: { click: () => showTab("executions") } })));
  explainRules();
  sync();

  document.addEventListener("agent:harnesskind", (event) => {
    const next = event.detail?.kind === "change" ? "change" : "read";
    if (taskKind === next) return;
    taskKind = next;
    invalidate("Тип задачі змінено. Підготуйте новий пакет.");
  });
  document.addEventListener("agent:providerchange", (event) => {
    const next = event.detail?.mode === "real" ? "real" : "mock";
    if (mode.value === next) return;
    mode.value = next;
    invalidate("Режим моделі змінено; підготуйте пакет заново.");
    loadPassport();
  });
  mode.addEventListener("change", () => {
    if (typeof setProvider === "function") setProvider(mode.value);
    mode.value = typeof taskMode === "function" ? taskMode() : mode.value;
    changed();
  });
  async function initialize() {
    const responses = await Promise.allSettled([api("GET", "/harness/projects"), api("GET", "/models")]);
    const modelResponse = responses[1].status === "fulfilled" ? responses[1].value : null;
    realAvailable = modelResponse?.status === 200 && (modelResponse.data.models || []).some((m) => m.side === "real" && m.available);
    for (const m of modelResponse?.status === 200 ? modelResponse.data.models || [] : [])
      for (const cls of m.classes || []) (classModels[cls] ||= {})[m.side] = m.model;
    mode.options[1].disabled = !realAvailable;
    mode.value = typeof taskMode === "function" ? taskMode() : "mock";
    const response = responses[0].status === "fulfilled" ? responses[0].value : null;
    if (!response || response.status !== 200) { status.textContent = "Список проєктів недоступний. Запуск заблоковано."; sync(); return; }
    projects = response.data.projects || [];
    project.replaceChildren(...projects.map((p) => {
      const option = el("option", { value: p.id, text: p.title + (p.available ? "" : " · недоступний") });
      option.disabled = !p.available;
      return option;
    }));
    const first = projects.find((p) => p.available);
    if (first) project.value = first.id;
    cards.replaceChildren(...projects.map((p) => {
      const button = el("button", { class: "harness-project", text: p.title + " · " + p.kind + (p.available ? "" : " · недоступний"),
        on: { click: () => { project.value = p.id; project.dispatchEvent(new Event("change", { bubbles: true })); } } });
      button.disabled = !p.available;
      return button;
    }));
    sync();
    explainRules();
    loadPassport();
    document.dispatchEvent(new CustomEvent("agent:harnessready"));
  }
  initialize().catch((error) => { status.textContent = "Ініціалізацію не завершено: " + error.message; sync(); });
})();
