const byId = (id) => document.getElementById(id);
const form = byId("run-form");
const fileInput = byId("files");
const browse = byId("browse");
const sample = byId("sample");
const dropZone = byId("drop-zone");
const submit = byId("run-button");
const approve = byId("approve");
const reject = byId("reject");
const stateBadge = byId("run-state");
const storedRunKey = "tenderhive-workbench-run";
const runIdPattern = /^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/i;
const maxFile = 2 * 1024 * 1024;
const maxTotal = 5 * 1024 * 1024;
const numericFields = ["quantity", "offer_validity_workdays", "unit_cost", "offer_price", "available_quantity", "penalty_daily_percent"];
const terminalPhases = new Set(["done", "failed", "declined", "cancelled", "needs_clarification"]);
const timeFormat = new Intl.DateTimeFormat("ru", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
const sizeFormat = new Intl.NumberFormat("ru", { maximumFractionDigits: 1 });
const roleNames = {
  extraction: "Извлечение",
  risk: "Риски",
  legal: "Юридический агент",
  resource: "Ресурсы",
  estimate: "Смета",
  supply: "Поставки",
  document: "Подготовка документов",
  solver: "Решатель",
  orchestrator: "Оркестратор",
  workbench: "Рабочее пространство",
  operator: "Оператор",
};
const decisions = {
  PARTICIPATE: "Участвовать",
  CONDITIONAL: "Участвовать при выполнении условий",
  NOT_PARTICIPATE: "Не участвовать",
  PARTICIPATE_WITH_CONDITIONS: "Участвовать при выполнении условий",
  DO_NOT_PARTICIPATE: "Не участвовать",
  DECLINE: "Не участвовать",
  REVIEW: "Требуется проверка",
  NEEDS_REVIEW: "Требуется проверка",
  INCOMPLETE: "Недостаточно данных",
  NO_SOLUTION: "Решение не найдено",
};
const reasonLabels = {
  PENALTY_ABOVE_THRESHOLD: "Неустойка выше порога",
  LOW_MARGIN: "Маржа ниже порога",
  RESOURCE_GAP: "Недостаточно ресурсов",
  UNCLEAR_REQUIREMENTS: "Требования нужно уточнить",
  NO_SOLUTION: "Решение не найдено",
  FINAL_APPROVAL: "Утверждение результата перед выпуском файлов",
  LOW_CONFIDENCE: "Оценку риска нужно проверить",
};
const browserState = {
  files: [],
  current: null,
  pending: false,
  poll: null,
  messageKeys: new Set(),
  renderedRun: null,
  renderedResult: "",
  renderedCard: null,
  revision: 0,
};
function el(tag, text, className) {
  const element = document.createElement(tag);
  if (text != null) element.textContent = text;
  if (className) element.className = className;
  return element;
}

function icon(name) {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  const use = document.createElementNS(ns, "use");
  svg.setAttribute("aria-hidden", "true");
  use.setAttribute("href", "#i-" + name);
  svg.append(use);
  return svg;
}

function setError(id, message) {
  const output = byId(id);
  output.textContent = message ?? "";
  output.hidden = !message;
}

function showStatus(text, tone = "") {
  if (stateBadge.dataset.tone === tone && stateBadge.textContent === text) return;
  const dot = el("i");
  dot.setAttribute("aria-hidden", "true");
  stateBadge.dataset.tone = tone;
  stateBadge.replaceChildren(dot, document.createTextNode(text));
}

function nameFileSize(size) {
  return sizeFormat.format(size / 1024) + " КБ";
}

function renderFiles() {
  const rows = browserState.files.map((file, index) => {
    const row = el("li", null, "file");
    const remove = el("button", null, "icon-button");
    remove.type = "button";
    remove.setAttribute("aria-label", "Удалить " + file.name);
    remove.append(icon("close"));
    remove.addEventListener("click", () => {
      if (browserState.pending) return;
      browserState.files.splice(index, 1);
      renderFiles();
      const next = byId("file-list").querySelectorAll("button")[index];
      (next ?? browse).focus();
    });
    row.append(icon("file"), el("strong", file.name), el("span", nameFileSize(file.size)), remove);
    return row;
  });
  byId("file-list").replaceChildren(...rows);
}

function addFiles(files) {
  if (browserState.pending) return;
  const list = [...browserState.files];
  let total = list.reduce((size, file) => size + file.size, 0);
  let error = "";
  for (const file of files) {
    if (list.some((item) => item.name === file.name && item.size === file.size && item.lastModified === file.lastModified)) continue;
    if (!/\.(pdf|txt)$/i.test(file.name) || file.size === 0) {
      error = "Добавьте непустой файл PDF или TXT.";
      continue;
    }
    if (file.size > maxFile || list.length === 5 || total + file.size > maxTotal) {
      error = "Можно добавить до 5 файлов, до 2 МБ каждый и до 5 МБ суммарно.";
      continue;
    }
    list.push(file);
    total += file.size;
  }
  browserState.files = list;
  setError("form-error", error);
  renderFiles();
}

async function encodeFile(file) {
  const bytes = new Uint8Array(await file.arrayBuffer());
  let binary = "";
  for (let offset = 0; offset < bytes.length; offset += 8192) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + 8192));
  }
  return { name: file.name, content_type: /\.pdf$/i.test(file.name) ? "application/pdf" : "text/plain", content_base64: btoa(binary) };
}

async function request(path, payload) {
  const response = await fetch("/api/workbench" + path, payload === undefined ? {} : {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const body = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = body?.detail;
    const message = typeof detail === "string" ? detail : detail?.message;
    throw new Error(message ?? (response.status === 422
      ? "Проверьте документы, параметры и выбранный фрагмент."
      : "Стенд не ответил. Попробуйте снова."), { cause: response.status });
  }
  return body;
}

function statusOf(run) {
  const phase = String(run.phase ?? "").toLowerCase();
  const status = String(run.status ?? "").toUpperCase();
  if (["FAILED", "ERROR", "DECLINED", "REJECTED"].includes(status) || ["failed", "declined"].includes(phase)) {
    return ["Остановлен", "error"];
  }
  if (status === "DONE" || phase === "done") return ["Анализ завершён", "done"];
  if (run.card || status === "WAITING_HUMAN") return ["Ждёт согласования", "warn"];
  if (phase === "needs_clarification") return ["Нужно уточнение", "warn"];
  return ["Анализ выполняется", "running"];
}

function prettyReason(value) {
  if (typeof value === "string") return reasonLabels[value] ?? value;
  return value?.message ?? value?.reason ?? value?.code ?? "";
}

function messageRow(message) {
  const row = el("li", null, "message");
  const marker = el("i", null, "message-marker");
  marker.setAttribute("aria-hidden", "true");
  const content = el("div");
  const header = el("div", null, "message-line");
  header.append(el("strong", roleNames[message.agent] ?? message.agent ?? "Агент"));
  if (message.to) header.append(el("span", "→ " + (roleNames[message.to] ?? message.to), "recipient"));
  const messageState = { SENT: "Отправлено", DONE: "Готово", NEEDS_REVIEW: "На проверку", FAILED: "Ошибка", APPROVED: "Согласовано", REJECTED: "Отклонено" };
  const label = messageState[message.status];
  if (label) header.append(el("span", label, "message-state"));
  if (message.at) {
    const date = new Date(message.at);
    if (!Number.isNaN(date.valueOf())) {
      const stamp = el("time", timeFormat.format(date));
      stamp.dateTime = date.toISOString();
      header.append(stamp);
    }
  }
  content.append(header, el("p", message.text || message.status));
  if (message.details && Object.keys(message.details).length) {
    const details = el("details");
    details.append(el("summary", "Данные сообщения"), el("pre", JSON.stringify(message.details, null, 2)));
    content.append(details);
  }
  row.append(marker, content);
  return row;
}

function renderResult(run) {
  const marker = JSON.stringify([run.phase, run.recommendation, run.artifacts]);
  if (browserState.renderedResult === marker) return;
  browserState.renderedResult = marker;
  const result = run.recommendation;
  const placeholder = byId("outcome-empty");
  placeholder.hidden = Boolean(result);
  placeholder.textContent = isTerminal(run) ? "Рекомендация не сформирована." : "Результат появится после работы решателя.";
  byId("outcome-body").hidden = !result;
  if (result) {
    const title = byId("recommendation");
    title.textContent = decisions[result.value] ?? result.value;
    title.dataset.value = result.value;
    const reasons = [...new Set([...(result.reasons ?? []), ...(result.conditions ?? [])].map(prettyReason).filter(Boolean))];
    byId("reasons").replaceChildren(...reasons.map((reason) => el("li", reason)));
  }
  const links = [];
  for (const artifact of run.artifacts ?? []) {
    if (typeof artifact.url !== "string" || !artifact.url.startsWith("/api/workbench/")) continue;
    const item = el("li");
    const link = el("a");
    link.href = artifact.url;
    link.download = artifact.name;
    link.append(icon("file"), document.createTextNode(artifact.name));
    item.append(link);
    links.push(item);
  }
  byId("artifact-links").replaceChildren(...links);
}

function renderRun(run) {
  if (!run?.id) return;
  if (browserState.renderedRun !== run.id) {
    byId("messages").replaceChildren();
    browserState.messageKeys.clear();
    browserState.renderedRun = run.id;
  }
  browserState.current = run;
  const [state, tone] = statusOf(run);
  showStatus(state, tone);
  const messages = run.messages ?? [];
  for (const [index, message] of messages.entries()) {
    const key = message.id ?? index;
    if (browserState.messageKeys.has(key)) continue;
    browserState.messageKeys.add(key);
    byId("messages").append(messageRow(message));
  }
  byId("feed-empty").hidden = messages.length > 0;
  const count = byId("message-count");
  count.textContent = messages.length;
  count.setAttribute("aria-label", "Сообщений: " + messages.length);
  byId("run-id").textContent = "JOB " + run.id;
  byId("run-documents").textContent = (run.documents ?? []).map((document) => document.name).join(" · ");
  renderResult(run);
  const cardId = run.card?.id ?? run.card?.reason ?? null;
  if (browserState.renderedCard !== cardId) {
    browserState.renderedCard = cardId;
    byId("decision-comment").value = "";
    byId("approval").hidden = !run.card;
    if (run.card) byId("approval-reason").textContent = [prettyReason(run.card.reason), run.card.question].filter(Boolean).join(" · ");
  }
  setError("run-error", typeof run.error === "string" ? run.error : run.error?.message);
}

function stopPolling() {
  window.clearTimeout(browserState.poll);
  browserState.poll = null;
}

function isTerminal(run) {
  return terminalPhases.has(String(run.phase).toLowerCase())
    || ["DONE", "FAILED", "ERROR", "REJECTED", "CANCELLED"].includes(String(run.status).toUpperCase());
}

function queuePoll(run) {
  stopPolling();
  if (!isTerminal(run)) browserState.poll = window.setTimeout(() => poll(run.id), 1000);
}

async function poll(id) {
  const revision = browserState.revision;
  try {
    const run = await request("/runs/" + encodeURIComponent(id));
    if (revision !== browserState.revision || (browserState.current && browserState.current.id !== id)) return;
    renderRun(run);
    queuePoll(run);
  } catch (error) {
    if (revision !== browserState.revision) return;
    if (error.cause === 404) {
      sessionStorage.removeItem(storedRunKey);
      const address = new URL(location.href);
      address.searchParams.delete("run");
      history.replaceState(null, "", address);
      showStatus("Новый анализ");
      setError("run-error", "Предыдущий анализ недоступен. Запустите новый.");
      return;
    }
    if (revision !== browserState.revision || (browserState.current && browserState.current.id !== id)) return;
    setError("run-error", error.message);
    browserState.poll = window.setTimeout(() => poll(id), 3000);
  }
}

fileInput.addEventListener("change", () => { addFiles(fileInput.files); fileInput.value = ""; });
browse.addEventListener("click", () => fileInput.click());
dropZone.addEventListener("dragover", (event) => {
  event.preventDefault();
  if (!browserState.pending) dropZone.classList.add("dragging");
});
dropZone.addEventListener("dragleave", (event) => {
  if (!dropZone.contains(event.relatedTarget)) dropZone.classList.remove("dragging");
});
dropZone.addEventListener("drop", (event) => {
  event.preventDefault();
  dropZone.classList.remove("dragging");
  if (event.dataTransfer) addFiles(event.dataTransfer.files);
});

sample.addEventListener("click", () => {
  if (browserState.pending) return;
  const example = {
    title: "Хозяйственные товары · лот 3",
    quantity: "996",
    offer_validity_workdays: "60",
    unit_cost: "4.50",
    offer_price: "5577.60",
    available_quantity: "1000",
    penalty_daily_percent: "0.15",
    context: "The published draft includes a daily penalty for late delivery.",
  };
  for (const [key, value] of Object.entries(example)) form.elements.namedItem(key).value = value;
  if (!browserState.files.length) {
    addFiles([new File(["Tender 341041674, lot 3.\nRequired quantity: 996 rolls.\nOffer validity at least 60 working days.\nThe published draft includes a daily penalty for late delivery.\n"], "пример-лота-3.txt", { type: "text/plain" })]);
  }
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (browserState.pending) return;
  if (!browserState.files.length) {
    setError("form-error", "Добавьте хотя бы один документ закупки.");
    browse.focus();
    return;
  }
  const data = new FormData(form);
  if (!data.get("title").trim() || !data.get("context").trim()) {
    setError("form-error", "Введите название и фрагмент условий для оценки риска.");
    byId(!data.get("title").trim() ? "purchase-title" : "context").focus();
    return;
  }
  browserState.pending = true;
  browserState.revision++;
  stopPolling();
  submit.disabled = true;
  sample.disabled = true;
  browse.disabled = true;
  approve.disabled = true;
  reject.disabled = true;
  setError("form-error", null);
  showStatus("Запускаем анализ…", "running");
  try {
    const facts = Object.fromEntries(numericFields.map((field) => [field, data.get(field)]).filter(([, value]) => value !== ""));
    facts.offer_validity_workdays = Number(facts.offer_validity_workdays);
    const documents = await Promise.all(browserState.files.map(encodeFile));
    const run = await request("/runs", { title: data.get("title").trim(), documents, facts, context: data.get("context").trim() });
    sessionStorage.setItem(storedRunKey, run.id);
    const address = new URL(location.href);
    address.searchParams.set("run", run.id);
    history.replaceState(null, "", address);
    renderRun(run);
    queuePoll(run);
    byId("activity").focus();
  } catch (error) {
    setError("form-error", error.message);
    if (browserState.current) {
      showStatus(...statusOf(browserState.current));
      queuePoll(browserState.current);
    } else showStatus("Запуск не удался", "error");
  } finally {
    browserState.pending = false;
    submit.disabled = false;
    sample.disabled = false;
    browse.disabled = false;
    approve.disabled = false;
    reject.disabled = false;
  }
});

async function decide(decision) {
  const current = browserState.current;
  if (!current?.card || approve.disabled) return;
  const commentInput = byId("decision-comment");
  const comment = commentInput.value.trim();
  if (decision === "REJECTED" && !comment) {
    setError("run-error", "Укажите причину отклонения.");
    commentInput.focus();
    return;
  }
  stopPolling();
  browserState.revision++;
  approve.disabled = true;
  reject.disabled = true;
  setError("run-error", null);
  try {
    const run = await request("/runs/" + encodeURIComponent(current.id) + "/decision", { decision, card_id: current.card.id, comment: comment || undefined });
    if (browserState.current?.id !== current.id) return;
    if (run?.id) {
      renderRun(run);
      queuePoll(run);
    } else await poll(current.id);
  } catch (error) {
    setError("run-error", error.message);
    queuePoll(current);
  } finally {
    approve.disabled = false;
    reject.disabled = false;
  }
}

approve.addEventListener("click", () => decide("APPROVED"));
reject.addEventListener("click", () => decide("REJECTED"));
function selectNav() {
  for (const link of document.querySelectorAll(".nav-link")) {
    const active = link.hash === (location.hash || "#main");
    link.classList.toggle("selected", active);
    if (active) link.setAttribute("aria-current", "location");
    else link.removeAttribute("aria-current");
  }
}

window.addEventListener("hashchange", selectNav);
selectNav();
const linkedRun = new URL(location.href).searchParams.get("run");
const previousRun = linkedRun ?? sessionStorage.getItem(storedRunKey);
if (previousRun && runIdPattern.test(previousRun)) void poll(previousRun);
