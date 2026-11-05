// Selected Engine execution: prepare exact messages, inspect, then explicitly invoke.
"use strict";
(() => {
  const root = $("b-runtime");
  if (!root) return;
  let prepared = null;
  let revision = 0;
  let expiresAt = 0;
  let timer;
  let busy = false;
  let availableProjects = [];
  const options = (id, entries) => el("select", { id }, ...entries.map(([value, text]) => el("option", { value, text })));
  const project = options("b-run-project", [["demo-site", "demo-site"], ["notes-app", "notes-app"], ["llm-ops", "llm-ops"]]);
  const profile = options("b-run-profile", [["quick", "Quick · 600 токенів відповіді"], ["thorough", "Thorough · 1600 токенів відповіді"]]);
  const cache = el("input", { id: "b-run-cache", type: "checkbox" });
  cache.checked = true;
  const modeView = el("span", { id: "b-run-mode", class: "pill" });
  const prepare = el("button", { id: "b-run-prepare", class: "primary", text: "1. Підготувати Engine" });
  const run = el("button", { id: "b-run-execute", text: "2. Виконати пакет" });
  const status = el("p", { id: "b-run-status", class: "hint", role: "status", text: "Оберіть Engine і заповніть чернетку. Підготовка не викликає модель." });
  const packet = el("div", { id: "b-run-packet" });
  const result = el("div", { id: "b-run-result", "aria-live": "polite" });
  const history = el("div", { id: "b-run-history" });
  const details = (label, value) => el("details", { class: "tech" }, el("summary", { text: label }),
    el("pre", { class: "diff body-view", text: JSON.stringify(value, null, 2) }));
  const name = (value) => typeof value === "string" ? value : value?.name || value?.id || "—";
  function sync() {
    modeView.textContent = "наступний Engine: " + taskMode();
    const engine = $("b-engine").value;
    const known = availableProjects.some((p) => p.id === project.value && p.available);
    prepare.disabled = busy || !studio || !engine || !draft().task_goal.trim() || !known || (engine === "code_engine" && project.value !== "demo-site");
    run.disabled = busy || !prepared || Date.now() >= expiresAt;
  }
  function invalidate(message) {
    revision += 1;
    prepared = null;
    expiresAt = 0;
    clearTimeout(timer);
    packet.replaceChildren();
    document.dispatchEvent(new CustomEvent("agent:identityinvalidated", { detail: { target: "builder" } }));
    if (message) status.textContent = message;
    sync();
  }
  function settings() {
    return { engine: $("b-engine").value, draft: draft(), mode: taskMode(), profile: profile.value,
      project: project.value, cache: cache.checked };
  }
  for (const control of [project, profile, cache]) control.addEventListener("change", () => invalidate("Параметри виконання змінено. Підготуйте новий пакет."));
  document.addEventListener("agent:builderexample", (event) => {
    if ([...project.options].some((option) => option.value === event.detail?.project)) project.value = event.detail.project;
    invalidate("Завантажено приклад і відповідний проєкт. Підготуйте пакет заново.");
  });
  document.addEventListener("agent:builderchange", () => invalidate("Чернетку або Engine змінено. Старий пакет не виконується."));
  document.addEventListener("agent:providerchange", () => invalidate("Mock / Real змінено. Підготуйте пакет у новому режимі."));
  document.addEventListener("agent:builderready", () => { sync(); loadRuns(); });
  prepare.addEventListener("click", async () => {
    if (prepare.disabled) return;
    const input = settings();
    invalidate();
    const submitted = revision;
    busy = true; sync();
    status.textContent = "Підготовка " + input.engine + " · " + input.mode + " без виклику моделі…";
    try {
      const response = await api("POST", "/studio/prepare", input);
      if (submitted !== revision) return;
      if (response.status !== 200) throw new Error(response.data?.error || "HTTP " + response.status);
      prepared = response.data;
      expiresAt = Date.now() + Number(prepared.expires_in_seconds || 1800) * 1000;
      timer = setTimeout(() => invalidate("Пакет застарів. Підготуйте його заново."), expiresAt - Date.now());
      const p = prepared.packet;
      document.dispatchEvent(new CustomEvent("agent:callidentity", { detail: { target: "builder", packet: p, state: "prepared" } }));
      packet.replaceChildren(el("h3", { text: "Пакет обраного Engine" }),
        el("div", { class: "kpis" }, kpi("Engine", name(p.engine)), kpi("роль", name(p.role)),
          kpi("контракт", name(p.contract)), kpi("модель", name(p.model)), kpi("пакет", short(p.messages_sha256))),
        details("Точні повідомлення, ролі, контракт і контрольні суми", p));
      status.textContent = "Підготовлено " + input.engine + " · " + input.mode + ". Перегляньте пакет; модель запускається другою кнопкою.";
    } catch (error) { if (submitted === revision) status.textContent = "Не підготовлено: " + error.message; }
    finally { busy = false; sync(); }
  });
  function renderResult(output) {
    result.replaceChildren(el("h3", { text: "Engine: " + output.engine + " · " + output.status }),
      el("div", { class: "kpis" }, kpi("роль", name(output.role)), kpi("контракт", name(output.contract)),
        kpi("модель", name(output.model)), kpi("спроб провайдера", output.calls ?? "—"),
        kpi("кеш", output.cache_hits ?? 0), kpi("оцінка вартості", priced(output.cost_usd, output.mode))),
      el("p", { class: "hint", text: output.mode === "mock" ? "Синтетичний результат перевіряє механіку стенда, не якість реальної моделі." : "Реальна модель. Контракт перевіряє структуру відповіді, не її семантичну правильність." }),
      el("div", { class: "gates" }, ...(output.checks || []).map((check) => el("span", { class: "pill " + (check.ok ? "ok" : "bad"),
        text: (check.gate || check.check || "контракт") + ": " + (check.ok ? "пройдено" : "не пройдено") }))),
      el("pre", { class: "diff body-view", text: output.output || "—" }), details("Виконання та сумісність сховища", output));
  }
  run.addEventListener("click", async () => {
    if (run.disabled || !prepared) return;
    const id = prepared.preparation_id;
    const sentPacket = prepared.packet;
    const submittedMode = taskMode();
    const submitted = revision;
    prepared = null; expiresAt = 0; clearTimeout(timer);
    busy = true; sync();
    status.textContent = "Виконується підготовлений Engine · " + submittedMode + "…";
    try {
      const response = await api("POST", "/studio/run", { preparation_id: id });
      if (response.status !== 200) throw new Error(response.data?.error || "HTTP " + response.status);
      renderResult(response.data);
      document.dispatchEvent(new CustomEvent("agent:callidentity", { detail: { target: "builder", packet: sentPacket, state: "completed", historical: submitted !== revision } }));
      status.textContent = "Запуск завершився · " + submittedMode + (submitted !== revision ? ". Настройки вже змінені; результат належить попередньому пакету." : ". Перевірте контракт і витрати.");
      refreshToday(); loadRuns();
    } catch (error) {
      status.textContent = "Результат не підтверджено: " + error.message + ". Автоповтору немає; перегляньте історію та журнал.";
      loadRuns();
    } finally { busy = false; sync(); }
  });
  async function loadRuns() {
    const response = await api("GET", "/studio/runs");
    if (response.status !== 200) { history.replaceChildren(el("p", { class: "hint", text: "Історія Engine недоступна" })); return; }
    const runs = response.data.runs || [];
    history.replaceChildren(...runs.map((r) => el("button", { class: "link", text: [r.engine, r.mode, r.status, short(r.run_id)].filter(Boolean).join(" · "),
      on: { click: async () => {
        const detail = await api("GET", "/studio/runs/" + encodeURIComponent(r.run_id));
        if (detail.status === 200) {
          renderResult(detail.data);
          document.dispatchEvent(new CustomEvent("agent:callidentity", { detail: { target: "builder", packet: detail.data.package || detail.data, state: "completed", historical: true } }));
        }
        else toast("Виконання Engine недоступне");
      } } })));
    if (!runs.length) history.append(el("p", { class: "muted", text: "Запусків Engine ще немає" }));
  }
  root.replaceChildren(el("div", { class: "card" }, el("h2", { text: "Виконання Engine · роль і контракт" }),
    el("p", { class: "hint", text: "Це окремий явний виклик вибраного Engine. Сайт і git-файли автоматично не змінюються. code_engine перевіряє diff лише для demo-site. Режим Mock / Real — спільний із чатом і паспортом проєкту, перемикач у шапці." }),
    el("div", { class: "row" }, el("label", {}, "Проєкт ", project), el("label", {}, "Профіль ", profile), modeView),
    el("label", { class: "check" }, cache, "Кеш точного пакета; зніміть позначку для свіжої відповіді"),
    el("div", { class: "row" }, prepare, run), status, packet, result,
    el("h3", { text: "Історія запусків Engine" }), el("button", { class: "link", text: "Оновити історію", on: { click: loadRuns } }), history));
  sync();
  api("GET", "/harness/projects").then((response) => {
    availableProjects = response.status === 200 ? response.data.projects || [] : [];
    for (const option of project.options) option.disabled = !availableProjects.some((p) => p.id === option.value && p.available);
    sync();
  }).catch(() => { status.textContent = "Перелік джерел недоступний; підготовка заблокована."; sync(); });
})();
