// Deterministic task intake. Never invokes a model, saves or activates a prompt.
"use strict";
(() => {
  const root = $("task-intake");
  if (!root) return;
  let staged = null;
  let revision = 0;
  let busy = false;
  let ownDraftChange = false;
  const field = (label, input) => el("label", { class: "lbl" }, el("span", { text: label }), input);
  const text = (id, rows, placeholder) => el("textarea", { id, rows: String(rows), maxlength: "1000", placeholder });
  const goal = text("intake-task", 3, "Що потрібно зробити своїми словами?");
  const context = text("intake-context", 2, "Для якого проєкту, кого і в яких умовах?");
  const input = text("intake-input", 2, "Текст, факти або фрагмент коду; можна залишити порожнім");
  const criteria = text("intake-criteria", 2, "Як ви перевірите результат? По одному критерію в рядку");
  const constraints = text("intake-constraints", 2, "Відомі обмеження; по одному в рядку; можна залишити порожнім");
  const complexity = el("select", { id: "intake-complexity" },
    el("option", { value: "", text: "складність не оцінена" }),
    el("option", { value: "simple", text: "simple — моя оцінка" }),
    el("option", { value: "complex", text: "complex — моя оцінка" }));
  const structure = el("button", { id: "intake-structure", class: "primary", text: "Структурувати задачу" });
  const apply = el("button", { id: "intake-apply", text: "Перенести в чернетку й зібрати промпт" });
  const status = el("p", { id: "intake-status", class: "hint", role: "status", text: "Введіть задачу. Модель не викликається; наявна чернетка поки не змінюється." });
  const questions = el("div", { id: "intake-questions" });
  const preview = el("div", { id: "intake-preview" });
  const list = (value) => value.split("\n").map((line) => line.trim()).filter(Boolean);
  function scope() {
    const engine = $("b-engine")?.value || "";
    const entry = typeof studio !== "undefined" ? studio?.engines.find((item) => item.id === engine) : null;
    return { engine, project: $("b-run-project")?.value || entry?.example_project || (engine === "research_engine" ? "notes-app" : "demo-site") };
  }
  function answers() {
    return { task_goal: goal.value.trim(), background_context: context.value.trim(), input_data: input.value.trim(),
      constraints: list(constraints.value), acceptance_criteria: list(criteria.value), complexity: complexity.value || null };
  }
  function identity() { return JSON.stringify({ scope: scope(), answers: answers() }); }
  function sync() { structure.disabled = busy; apply.disabled = busy || !staged; }
  function invalidate(message, clearQuestions = false) {
    revision += 1; staged = null; preview.replaceChildren();
    if (clearQuestions) questions.replaceChildren();
    if (message) status.textContent = message;
    sync();
  }
  for (const control of [goal, context, input, criteria, constraints]) control.addEventListener("input", () => invalidate("Відповіді змінено. Натисніть «Структурувати задачу»; чернетку не перезаписано."));
  complexity.addEventListener("change", () => invalidate("Оцінку складності змінено. Структуруйте задачу заново."));
  document.addEventListener("agent:builderchange", () => {
    if (!ownDraftChange) invalidate("Engine або чернетку змінено. Перевірте відповіді й структуруйте задачу заново.", true);
  });
  document.addEventListener("agent:builderexample", () => invalidate("Завантажено приклад іншого контексту. Перевірте постановку задачі.", true));
  document.addEventListener("change", (event) => {
    if (event.target?.id === "b-run-project") invalidate("Проєкт змінено. Перевірте контекст і структуруйте задачу заново.", true);
  });
  structure.addEventListener("click", () => {
    if (busy) return;
    invalidate(null, true);
    const draft = answers(), selected = scope(), missing = [];
    if (!selected.engine) missing.push("Який Engine ви хочете використати? Оберіть його в конструкторі.");
    if (!draft.task_goal) missing.push("Який конкретний результат потрібно отримати? Заповніть задачу своїми словами.");
    if (!draft.background_context) missing.push("Для якого проєкту та в яких умовах виконуємо задачу? Додайте контекст; вибране джерело: " + selected.project + ".");
    if (!draft.acceptance_criteria.length) missing.push("За якими ознаками ви приймете результат? Додайте хоча б один власний критерій.");
    if ([goal, context, input, criteria, constraints].some((control) => control.value.length > 1000)) missing.push("Скоротіть кожне поле до 1000 символів перед перенесенням у конструктор.");
    if (missing.length) {
      questions.replaceChildren(el("h3", { text: "Потрібно уточнити" }), el("ul", { class: "log" }, ...missing.map((question) => el("li", { text: question }))));
      status.textContent = "Заповніть відповіді у полях вище й натисніть «Структурувати задачу» ще раз. Вимоги не вигадуються.";
      return;
    }
    const preparedDraft = { title: draft.task_goal.split("\n")[0].slice(0, 200), ...draft };
    staged = { identity: identity(), scope: selected, draft: preparedDraft };
    preview.replaceChildren(el("h3", { text: "Структура лише з ваших відповідей" }),
      el("p", { class: "hint", text: "Engine: " + selected.engine + "; джерело: " + selected.project + ". Порожні необов’язкові поля залишаться порожніми." }),
      el("pre", { class: "diff body-view", text: JSON.stringify(preparedDraft, null, 2) }));
    status.textContent = "Уточнення заповнені. Наступна кнопка замінить поля чернетки вашими відповідями та лише збере промпт — без виклику моделі.";
    sync();
  });
  apply.addEventListener("click", async () => {
    if (apply.disabled || !staged) return;
    if (staged.identity !== identity()) { invalidate("Відповіді або Engine змінилися. Структуруйте задачу заново.", true); return; }
    const prepared = staged;
    staged = null; busy = true; sync();
    const fields = { title: "b-title", task_goal: "b-goal", background_context: "b-context", input_data: "b-input",
      constraints: "b-constraints", acceptance_criteria: "b-accept", complexity: "b-complexity" };
    for (const [key, id] of Object.entries(fields)) $(id).value = Array.isArray(prepared.draft[key]) ? prepared.draft[key].join("\n") : prepared.draft[key] ?? "";
    ownDraftChange = true;
    try { invalidateBuilder("Перенесено ваші відповіді з локального майстра"); }
    finally { ownDraftChange = false; }
    const submitted = revision;
    status.textContent = "Збираю промпт вибраного Engine локальним компілятором; модель не викликається…";
    try {
      await buildPrompt();
      if (revision !== submitted || prepared.identity !== identity()) return;
      questions.replaceChildren();
      status.textContent = typeof built !== "undefined" && built
        ? "Чернетку перенесено, структурований промпт показано нижче. Engine ще не виконувався; збереження та активація не запускались."
        : "Чернетку перенесено. Збірка не підтверджена — перегляньте повідомлення конструктора нижче.";
    } catch (error) {
      status.textContent = "Чернетку перенесено, але компілятор не відповів: " + error.message + ". Модель не викликалась.";
    } finally { busy = false; sync(); }
  });
  root.replaceChildren(el("div", { class: "card" }, el("h2", { text: "Постановка задачі своїми словами" }),
    el("p", { class: "hint", text: "Локальний майстер, без виклику моделі. Він розкладає ваші відповіді по полях, запитує відсутнє й не додає вигаданих вимог. Engine обираєте ви. Наявна чернетка змінюється лише кнопкою перенесення." }),
    field("Задача своїми словами", goal), field("Контекст", context), field("Вхідні дані (необов’язково)", input),
    field("Як перевірити результат", criteria), field("Обмеження (необов’язково)", constraints), field("Ваша оцінка складності", complexity),
    el("div", { class: "row" }, structure, apply), status, questions, preview));
  sync();
})();
