// Navigation and explanation only. No model, preparation, import or activation calls.
"use strict";
(() => {
  const pages = {
    home: ["Демо", "Модель пропонує, код перевіряє, людина вирішує", "Три сценарії на репозиторії курсу LLM-Ops. Оберіть один — приклад підставиться, запуск у Mock безкоштовний."],
    harness: ["Сценарії", "Паспорт і запуск", "Проєкт, дозволи, профіль, закріплений пакет і явний запуск."],
    builder: ["Лабораторія", "Один спеціаліст", "Поставте задачу одному Engine, підготуйте пакет, перегляньте його й запустіть явно."],
    chat: ["Лабораторія", "Чат", "Розмова з контекстом навчального сайту й останніх шести ходів. Пропозиція зміни йде окремим конвеєром."],
    task: ["Лабораторія", "Ручний конвеєр сайту", "Planner і Editor без паспорта: ручний вибір моделі, бази й кешу. Застосування — окремо людиною."],
    workspace: ["Лабораторія", "Контекст проєкту", "Які файли та фрагменти потрапляють у пакет дослідника, а які залишаються поза ним."],
    policy: ["Налаштування", "Політики", "Інструкції для моделі, обмеження коду й ефективні дозволи проєкту."],
    roles: ["Налаштування", "Агенти, ролі й навички", "Діюча конфігурація виконання: відповідальність, методики, інструменти та контракти."],
    packs: ["Налаштування", "Що бачить модель", "Збірка прев’ю без моделі: порядок блоків, джерела й точні повідомлення."],
    prompts: ["Налаштування", "Версії промптів", "Реєстр промптів кроків: порівняння, нова версія та явна активація."],
    config: ["Налаштування", "Технічна конфігурація", "Роутинг, контракти, інструменти й вихідні файли конфігурації."],
    approvals: ["Контроль", "Рішення людини", "Перевірка diff та явне застосування або відхилення змін демо-сайту."],
    history: ["Контроль", "Історія задач", "Результати задач, фактичний режим, модель і оцінка вартості."],
    executions: ["Контроль", "Журнал виконань", "Збережені входи, стани зовнішніх викликів і контрольні суми."],
    cost: ["Контроль", "Вартість", "Окремо реальна оцінка, синтетична вартість, кеш і невідомі витрати."],
    site: ["Контроль", "Перегляд сайту", "Порівняння демо-файлів та результатів погоджених змін."],
    injections: ["Лабораторія", "Перевірка ін’єкцій", "Явні тестові запуски для перевірки інструкцій і захисту кодом."],
    break: ["Лабораторія", "Тестові збої", "Синтетичні помилки, відмови гейтів і тестові версії промптів."],
    cache: ["Лабораторія", "Кеш", "Перегляд повторного використання відповідей і діагностика ключів."],
  };
  const flows = [
    { title: "Питання до LLM-Ops", description: "Лише читання. Перевірка посилань доводить, що рядки були в пакеті, а не правильність кожного твердження.", scenario: "read", steps: [["Контекст", "Індекс, відбір і бюджет", "code"], ["Explorer", "Відповідь із посиланнями", "model"], ["Код", "JSON, витоки й діапазони посилань", "code"]] },
    { title: "Зміна з підтвердженням", description: "Два послідовні модельні кроки. Результат — пропозиція, а не автоматичний запис.", scenario: "change", steps: [["Людина", "Описує задачу", "human"], ["Planner", "План і вибір файлів", "model"], ["Код", "Дозволені файли й версії", "code"], ["Editor", "Пропонує unified diff", "model"], ["Гейти", "Формат, шляхи, патерни, база", "code"], ["Людина", "Approve / Reject", "human"]] },
    { title: "Пастка в документі", description: "Тестовий репозиторій із підкладеною інструкцією «для агента». Mock навмисно їй підкоряється, щоб було видно роботу гейта відповіді.", scenario: "trap", steps: [["Контекст", "Файли з підкладеною нотаткою", "code"], ["Explorer", "Відповідь, що підкорилась пастці", "model"], ["Гейт reply_leak", "Дослівні рядки інструкцій і секрети", "code"], ["Результат", "blocked_by_output_gate", "code"]] },
    { title: "Лабораторія · Один спеціаліст", description: "Одна вибрана роль: Research, Architecture, Code, Critic або Documentation. Автоматичного переходу між п’ятьма Engine немає; результат Code тут не застосовується.", scenario: "builder", steps: [["Людина", "Engine, задача й профіль", "human"], ["Код", "Роль, методика, контракт, контекст", "code"], ["Людина", "Перегляд і явний запуск пакета", "human"], ["Engine", "Один виклик моделі", "model"], ["Код", "Контракт, журнал і вартість", "code"]] },
    { title: "Лабораторія · Чат", description: "Один виклик для відповіді. Якщо модель запропонувала зміну, код запускає окрему задачу з Planner і Editor; потрібне окреме рішення людини.", scenario: "chat", steps: [["Контекст", "Файли й останні 6 ходів", "code"], ["Assistant", "Відповідь і proposal", "model"], ["Код", "Перевірка відповіді", "code"], ["За потреби", "Окремий конвеєр задачі", "code"]] },
  ];
  const host = $("orchestration-flows");
  host.replaceChildren(...flows.map(flow => el("article", { class: "card flow-card" },
    el("div", { class: "row between" }, el("h2", { text: flow.title }),
      el("button", { "data-scenario": flow.scenario, text: "Відкрити сценарій" })),
    el("p", { class: "hint", text: flow.description }),
    el("ol", { class: "flow-steps" }, ...flow.steps.map(([who, what, type]) =>
      el("li", { class: "flow-node " + type }, el("b", { text: who }), el("span", { text: what })))))));
  function updatePage(tab, focus) {
    const info = pages[tab];
    if (!info) return;
    $("page-group").textContent = info[0];
    $("page-title").textContent = info[1];
    $("page-description").textContent = info[2];
    document.querySelectorAll(".tabs .tab[data-tab]").forEach(button => {
      if (button.dataset.tab === tab) button.setAttribute("aria-current", "page");
      else button.removeAttribute("aria-current");
    });
    if (focus) $("page-title").focus({ preventScroll: true });
  }
  document.addEventListener("agent:tabchange", event => updatePage(event.detail.tab, true));
  document.addEventListener("click", event => {
    const button = event.target.closest?.("[data-open]");
    if (button && pages[button.dataset.open]) showTab(button.dataset.open);
    // "How the scenarios work" lives in a collapsed block on the Demo page.
    if (event.target.closest?.("[data-flows]")) {
      showTab("home");
      const flows = $("demo-flows"); flows.open = true;
      requestAnimationFrame(() => flows.scrollIntoView({ block: "start", behavior: "auto" }));
    }
  });
  updatePage("home", false);
  // Keep wide technical tables scrollable within their card on narrow screens.
  document.querySelectorAll(".tbl").forEach(table => {
    const wrapper = el("div", { class: "table-scroll", tabindex: "0", role: "region", "aria-label": "Таблиця з горизонтальним прокручуванням" });
    table.before(wrapper); wrapper.append(table);
  });
})();
