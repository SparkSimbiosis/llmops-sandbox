// Read-only adapters. Input packets are never changed or used as a new permission source.
"use strict";
(() => {
  const text = value => typeof value === "string" ? value : value?.name || value?.id || "";
  function normalize(packet, config = {}, kind = "preview") {
    config = config || {};
    if (Array.isArray(packet?.steps)) return packet.steps.flatMap(step => normalize(step, config, kind));
    if (packet?.packet && !packet.messages) return normalize(packet.packet, config, kind);
    const p = packet || {};
    const agent = typeof p.agent === "string" ? p.agent : "";
    const definition = (config.agents || []).find(item => item.id === agent);
    const roleBlock = (p.blocks || []).find(block => block.id === "role" || /(^|[\/])roles[\/]/.test(block.source || ""));
    const roleSource = roleBlock?.source || "";
    const frozenRole = roleSource.match(/roles[\/]([^/#]+)\.md/);
    const concreteRole = value => value && !/[<>]/.test(value) ? value : "";
    const role = concreteRole(text(p.role)) || concreteRole(frozenRole?.[1]) || definition?.role || "не надано";
    const contract = text(p.contract) || definition?.contract || "не надано";
    let prompt;
    if (p.prompt?.name) prompt = p.prompt.name + " · " + (p.prompt.version || "версію не надано");
    else if (p.prompt_sha256) prompt = "Чернетка · " + p.prompt_sha256.slice(0, 12);
    else if (kind === "draft") prompt = "Не підготовлено · поточна конфігурація";
    else prompt = "Прев’ю методики · без версії реєстру";
    const contractInfo = (config.contracts || []).find(item => item.id === contract);
    return [{ agent: agent || "не надано", role, responsibility: definition?.role_description || "Відповідальність визначає роль у пакеті",
      contract, contractText: p.contract?.output || contractInfo?.output || "Перевіряється контрактом вибраного сценарію",
      prompt, model: text(p.model) || p.route?.resolved_model || "не закріплено", project: p.project || "за сценарієм",
      state: kind, hash: p.messages_sha256 || "", roleSource: roleSource || (typeof p.role === "object" ? p.role.path : "") || "" }];
  }
  function render(host, packet, config, kind = "preview") {
    if (!host) return;
    const label = { historical: "Історичний результат · поточні входи не змінює", draft: "Чернетка · не готова до виконання", preview: "Прев’ю · модель не викликалась", prepared: "Підготовлено · пакет закріплений", completed: "Виконано · збережений склад виклику" };
    host.replaceChildren(...normalize(packet, config, kind).map(item => el("article", { class: "identity-card" },
      el("div", { class: "row between" }, el("strong", { text: "Склад виклику" }), el("span", { class: "pill " + (kind === "prepared" ? "ok" : ""), text: label[kind] || kind })),
      el("dl", { class: "identity-fields" }, ...[["Агент", item.agent], ["Роль", item.role], ["Контракт", item.contract], ["Промпт", item.prompt]].flatMap(([name, value]) => [el("dt", { text: name }), el("dd", { text: value })])),
      el("p", { class: "hint", text: item.responsibility }),
      el("p", { class: "hint", text: item.contractText }),
      el("div", { class: "identity-meta" }, el("span", { text: "Модель: " + item.model }), el("span", { text: "Контрольна сума повідомлень: " + (item.hash ? item.hash.slice(0, 12) : "ще не закріплено") })))));
  }
  window.CallIdentity = { normalize, render };
})();
