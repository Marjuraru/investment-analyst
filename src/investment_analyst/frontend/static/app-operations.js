"use strict";

let reviewCandidateItemsLoaded = false;
let reviewAlertItemsLoaded = false;
let reviewCandidateVisibleCount = PROGRESSIVE_COLLECTION_PAGE_SIZE;
let reviewAlertVisibleCount = PROGRESSIVE_COLLECTION_PAGE_SIZE;
let reviewCandidateServerTotal = 0;
let reviewAlertServerTotal = 0;
let reviewCandidateServerTruncated = false;
let reviewAlertServerTruncated = false;
let candidateNotificationPayload = null;
let candidateNotificationVisibleCount = PROGRESSIVE_COLLECTION_PAGE_SIZE;

function reviewFamilyIsLoaded(family) {
  return family === "candidate" ? reviewCandidateItemsLoaded : reviewAlertItemsLoaded;
}

function applyOverview(payload) {
  if (payload.schema_version === "operational-overview-snapshot-v1") {
    badge(
      byId("health-badge"),
      translated(payload.operational_status, STATUS_LABELS, payload.operational_status),
      statusTone(payload.operational_status),
    );
    byId("workspace-status").textContent = translated(
      payload.workspace_status,
      STATUS_LABELS,
      payload.workspace_status,
    );
    byId("workspace-counts").textContent = "Resumen operativo compacto";
    byId("run-status").textContent = payload.latest_run_status
      ? translated(payload.latest_run_status, STATUS_LABELS, payload.latest_run_status)
      : "Sin registro operativo";
    byId("run-time").textContent = "Sin lectura de historial";
    byId("traceability-status").textContent = "Sin verificación reciente";
    renderKnownAtCut(byId("report-known-at").value.trim());
    if (!payload.scheduler_enabled) {
      byId("schedule-status").textContent = "Desactivada";
      byId("schedule-next").textContent = "Solo actualización manual";
      return;
    }
    if (payload.scheduled_blocked_count > 0 || payload.scheduled_failed_count > 0) {
      byId("schedule-status").textContent = `${formatInteger(payload.scheduled_blocked_count || payload.scheduled_failed_count)} con fallo`;
    } else if (payload.scheduled_retry_wait_count > 0) {
      byId("schedule-status").textContent = `${formatInteger(payload.scheduled_retry_wait_count)} esperando reintento`;
    } else if (payload.scheduled_incomplete_count > 0) {
      byId("schedule-status").textContent = `${formatInteger(payload.scheduled_incomplete_count)} incompletos`;
    } else if (payload.scheduled_stale_count > 0) {
      byId("schedule-status").textContent = `${formatInteger(payload.scheduled_stale_count)} desactualizados`;
    } else if (payload.scheduled_running_count > 0) {
      byId("schedule-status").textContent = `${formatInteger(payload.scheduled_running_count)} en curso`;
    } else {
      byId("schedule-status").textContent = `${formatInteger(payload.scheduled_current_count)} actuales de ${formatInteger(payload.scheduled_job_count)}`;
    }
    byId("schedule-next").textContent = payload.scheduled_next_retry_at
      ? `Reintento: ${formatInstant(payload.scheduled_next_retry_at)}`
      : payload.scheduled_next_run_at
        ? formatInstant(payload.scheduled_next_run_at)
        : "Sin próxima ejecución";
    return;
  }
  const operational = payload.operational;
  const workspace = operational.workspace;
  const latest = operational.latest_run;
  const scheduler = payload.scheduler;
  const alerts = payload.alerts || { enabled: false };
  const candidates = payload.candidates || { enabled: false };
  const notifications = payload.notifications || { enabled: false };

  badge(
    byId("health-badge"),
    translated(operational.status, STATUS_LABELS, operational.status),
    statusTone(operational.status),
  );
  byId("workspace-status").textContent = translated(workspace.status, STATUS_LABELS, workspace.status);
  byId("workspace-counts").textContent = `${formatInteger(workspace.counts.observations)} obs. · ${formatInteger(workspace.counts.metric_results)} métricas`;

  byId("run-status").textContent = latest
    ? translated(latest.status, STATUS_LABELS, latest.status)
    : "Sin registro operativo";
  byId("run-time").textContent = latest
    ? formatInstant(latest.completed_at || latest.started_at)
    : "Datos históricos disponibles";
  byId("traceability-status").textContent = latest?.traceability_verified
    ? "Verificada"
    : "Sin verificación reciente";
  if (scheduler.enabled) {
    if (Array.isArray(scheduler.jobs)) {
      const total = scheduler.jobs.length;
      if (scheduler.failed_count > 0) {
        byId("schedule-status").textContent = `${formatInteger(scheduler.failed_count)} con fallo`;
      } else if (scheduler.incomplete_count > 0) {
        byId("schedule-status").textContent = `${formatInteger(scheduler.incomplete_count)} incompletos`;
      } else if (scheduler.stale_count > 0) {
        byId("schedule-status").textContent = `${formatInteger(scheduler.stale_count)} desactualizados`;
      } else if (scheduler.due_count > 0) {
        byId("schedule-status").textContent = `${formatInteger(scheduler.due_count)} pendientes`;
      } else if (scheduler.running_count > 0) {
        byId("schedule-status").textContent = `${formatInteger(scheduler.running_count)} en curso`;
      } else {
        byId("schedule-status").textContent = `${formatInteger(total)} trabajos automáticos`;
      }
      const nextJob = scheduler.jobs
        .slice()
        .sort((left, right) => left.next_run_at.localeCompare(right.next_run_at))[0];
      byId("schedule-next").textContent = formatInstant(
        scheduler.next_run_at,
        nextJob?.definition?.timezone || DEFAULT_TIME_ZONE,
      );
    } else {
      const config = scheduler.config;
      const scheduleStatus = byId("schedule-status");
      scheduleStatus.replaceChildren();
      if (scheduler.due) {
        scheduleStatus.append(
          renderAbsenceMark("overdue", "Vencida", `Próxima ejecución programada: ${formatInstant(scheduler.next_run_at, config.timezone)}`),
        );
      } else {
        scheduleStatus.textContent = `Automática · ${config.run_at}`;
      }
      byId("schedule-next").textContent = formatInstant(scheduler.next_run_at, config.timezone);
    }
  } else {
    byId("schedule-status").textContent = "Desactivada";
    byId("schedule-next").textContent = "Solo actualización manual";
  }

  if (alerts.enabled) {
    byId("alert-status").textContent = alerts.new_count > 0
      ? `${formatInteger(alerts.new_count)} nuevas`
      : "Sin alertas";
    byId("alert-latest").textContent = alerts.latest_alert_at
      ? formatInstant(alerts.latest_alert_at)
      : "Modo silencioso";
    byId("alert-inbox-summary").textContent = alerts.alert_count > 0
      ? `${formatInteger(alerts.alert_count)} registradas · modo silencioso`
      : "Sin incidencias · modo silencioso";
  } else {
    byId("alert-status").textContent = "Desactivadas";
    byId("alert-latest").textContent = "Monitor no configurado";
    byId("alert-inbox-summary").textContent = "Monitor no configurado";
  }

  if (candidates.enabled) {
    byId("candidate-status").textContent = candidates.new_count > 0
      ? `${formatInteger(candidates.new_count)} nuevos`
      : "Sin candidatos";
    byId("candidate-latest").textContent = candidates.latest_candidate_at
      ? formatInstant(candidates.latest_candidate_at)
      : `${formatInteger(candidates.result_count)} evaluaciones`;
    byId("candidate-inbox-summary").textContent = candidates.candidate_count > 0
      ? `${formatInteger(candidates.candidate_count)} registrados · modo silencioso`
      : `${formatInteger(candidates.result_count)} evaluaciones · sin candidatos`;
  } else {
    byId("candidate-status").textContent = "Desactivados";
    byId("candidate-latest").textContent = "Monitor no configurado";
    byId("candidate-inbox-summary").textContent = "Monitor no configurado";
  }

  if (notifications.enabled) {
    byId("candidate-notification-summary").textContent = notifications.pending_count > 0
      ? `${formatInteger(notifications.pending_count)} pendientes de ${formatInteger(notifications.total)}`
      : `${formatInteger(notifications.total)} entregadas localmente`;
  } else {
    byId("candidate-notification-summary").textContent = "Outbox local no configurado";
  }

  if (latest?.effective_known_at) byId("report-known-at").value = latest.effective_known_at;
  renderKnownAtCut(byId("report-known-at").value.trim());
  if (["quarterly", "annual"].includes(latest?.request?.fundamental_frequency)) {
    selectFundamentalFrequency(latest.request.fundamental_frequency);
  }
  operationalIssues = [...(operational.issues || []), ...(scheduler.issues || [])].map(localizedIssue);
  setMessage(operationalIssues.join(" · "), operationalIssues.length > 0);
}

const REVIEW_STATUS_LABELS = Object.freeze({
  new: "Nueva",
  seen: "Vista",
  dismissed: "Descartada",
  resolved: "Resuelta",
  silenced: "Silenciada",
});

function reviewItemId(family, item) {
  return String(family === "candidate" ? item.event.candidate_id : item.alert_id);
}

function reviewSelectedItem() {
  if (!reviewSelection) return null;
  if (!reviewFamilyIsLoaded(reviewSelection.family)) return null;
  const items = reviewSelection.family === "candidate" ? reviewCandidateItems : reviewAlertItems;
  const item = items.find((candidate) => reviewItemId(reviewSelection.family, candidate) === reviewSelection.id);
  if (item) return item;
  reviewSelectionMissing = true;
  reviewSelection = null;
  return null;
}

function reviewSelectItem(family, id) {
  reviewSelection = { family, id: String(id) };
  reviewSelectionMissing = false;
  const items = family === "candidate" ? reviewCandidateItems : reviewAlertItems;
  const selectedIndex = items.findIndex((item) => reviewItemId(family, item) === reviewSelection.id);
  if (selectedIndex >= 0) {
    if (family === "candidate") {
      reviewCandidateVisibleCount = progressiveCollectionVisibleCount(
        items.length,
        reviewCandidateVisibleCount,
        selectedIndex,
      );
      renderCandidateInboxRows();
    } else {
      reviewAlertVisibleCount = progressiveCollectionVisibleCount(
        items.length,
        reviewAlertVisibleCount,
        selectedIndex,
      );
      renderAlertInboxRows();
    }
  }
  renderReviewDetail();
}

function reviewEnsureSelection() {
  if (reviewSelection || reviewSelectionMissing) return;
  const firstCandidate = reviewCandidateItems[0];
  if (firstCandidate) {
    reviewSelection = { family: "candidate", id: reviewItemId("candidate", firstCandidate) };
    return;
  }
  const firstAlert = reviewAlertItems[0];
  if (firstAlert) reviewSelection = { family: "alert", id: reviewItemId("alert", firstAlert) };
}

function reviewActionTargets(status) {
  return status === "new"
    ? [["seen", "Marcar vista"], ["dismissed", "Descartar"], ["resolved", "Resolver"]]
    : status === "seen"
      ? [["dismissed", "Descartar"], ["resolved", "Resolver"]]
      : ["dismissed", "silenced"].includes(status)
        ? [["resolved", "Resolver"]]
        : [];
}

function reviewDetailField(container, label, value) {
  container.append(createElement("dt", "", label), createElement("dd", "", value));
}

function reviewAssetNavigationButton(assetId) {
  const normalized = String(assetId ?? "").trim();
  if (!normalized) return null;
  const button = createContextualNavigationButton(
    "Abrir en Activo",
    { board: "activo", assetId: normalized, subtab: "mercado" },
    "button secondary compact review-asset-navigation",
  );
  button.setAttribute("aria-label", "Abrir " + assetDisplayLabel(normalized) + " en Activo");
  return button;
}

function renderReviewDetail() {
  reviewEnsureSelection();
  const detail = byId("review-detail");
  const empty = byId("review-detail-empty");
  const selected = reviewSelectedItem();
  detail.replaceChildren();
  if (!selected || !reviewSelection) {
    detail.hidden = true;
    empty.hidden = false;
    empty.textContent = reviewSelection
      ? "Consultando la identidad exacta en la bandeja…"
      : reviewSelectionMissing
        ? "La selección ya no está disponible en la bandeja actual."
        : "Selecciona un elemento para revisar sus datos y acciones.";
    updateReviewMasterSelection();
    return;
  }
  detail.hidden = false;
  empty.hidden = true;
  const family = reviewSelection.family;
  const title = createElement("h3", "review-detail-title", family === "candidate" ? "Detalle del candidato" : "Detalle de la alerta");
  title.id = "review-detail-title";
  detail.append(title);
  const fields = createElement("dl", "review-detail-fields");
  if (family === "candidate") {
    const { event, result } = selected;
    const assetLabel = assetDisplayLabel(result.asset_id);
    detail.append(createElement("p", "review-detail-kicker", `${result.rule.name_es} · ${assetLabel}`));
    reviewDetailField(fields, "Estado", REVIEW_STATUS_LABELS[event.status] || event.status);
    reviewDetailField(fields, "Activo", assetLabel);
    reviewDetailField(fields, "Confirmaciones", formatInteger(event.confirmations));
    reviewDetailField(fields, "Vigencia", formatCalendarDate(event.as_of));
    reviewDetailField(fields, "Espera", formatInstant(event.cooldown_until));
    const conditions = createElement("ul", "candidate-condition-list review-detail-conditions");
    result.conditions.forEach((condition, index) => {
      const definition = result.rule.conditions[index];
      conditions.append(createElement("li", condition.state, formatCandidateCondition(condition, definition)));
    });
    detail.append(fields, createElement("h4", "review-detail-subtitle", "Condiciones evaluadas"), conditions);
    const assetNavigation = reviewAssetNavigationButton(result.asset_id);
    if (assetNavigation) detail.append(assetNavigation);
    const actions = createElement("div", "alert-inbox-actions review-detail-actions");
    for (const [target, label] of reviewActionTargets(event.status)) {
      const button = createElement("button", "alert-action-button", label);
      button.type = "button";
      button.addEventListener("click", () => transitionCandidate(event.candidate_id, target, button));
      actions.append(button);
    }
    if (actions.childElementCount > 0) detail.append(actions);
  } else {
    const event = selected;
    const assetLabel = event.asset_id ? assetDisplayLabel(event.asset_id) : "No especificado";
    detail.append(createElement("p", "review-detail-kicker", event.title));
    reviewDetailField(fields, "Estado", REVIEW_STATUS_LABELS[event.status] || event.status);
    reviewDetailField(fields, "Activo", assetLabel);
    reviewDetailField(fields, "Proveedor", event.provider);
    reviewDetailField(fields, "Dominio", event.domain);
    reviewDetailField(fields, "Última activación", formatInstant(event.last_activated_at));
    reviewDetailField(fields, "Espera", "No aplica");
    detail.append(fields, createElement("p", "review-detail-message", event.message));
    const assetNavigation = reviewAssetNavigationButton(event.asset_id);
    if (assetNavigation) detail.append(assetNavigation);
    const actions = createElement("div", "alert-inbox-actions review-detail-actions");
    for (const [target, label] of reviewActionTargets(event.status)) {
      const button = createElement("button", "alert-action-button", label);
      button.type = "button";
      button.addEventListener("click", () => transitionAlert(event.alert_id, target, button));
      actions.append(button);
    }
    if (actions.childElementCount > 0) detail.append(actions);
  }
  updateReviewMasterSelection();
}

function updateReviewMasterSelection() {
  document.querySelectorAll("[data-review-family][data-review-id]").forEach((row) => {
    const selected = reviewSelection
      && row.dataset.reviewFamily === reviewSelection.family
      && row.dataset.reviewId === reviewSelection.id;
    row.setAttribute("aria-pressed", selected ? "true" : "false");
    row.classList.toggle("selected", selected);
  });
}

function renderReviewMasterRow(family, id, title, status, meta) {
  const row = createElement("button", `review-master-row ${family === "candidate" ? "candidate-master-row" : "alert-master-row"}`);
  row.type = "button";
  row.dataset.reviewFamily = family;
  row.dataset.reviewId = String(id);
  row.setAttribute("aria-pressed", "false");
  row.append(
    createElement("strong", "review-master-title", title),
    createElement("span", `alert-inbox-status ${status}`, REVIEW_STATUS_LABELS[status] || status),
    createElement("small", "review-master-meta", meta),
  );
  row.addEventListener("click", () => reviewSelectItem(family, id));
  return row;
}

function renderReviewServerCoverageNote(inbox, serverTotal, returnedCount, truncated) {
  if (!truncated && serverTotal <= returnedCount) return;
  inbox.append(
    createElement(
      "small",
      "collection-note",
      `Servidor: ${formatInteger(returnedCount)} de ${formatInteger(serverTotal)} elementos devueltos; resultado truncado.`,
    ),
  );
}

function renderAlertInboxRows() {
  const inbox = byId("alert-inbox");
  inbox.replaceChildren();
  if (reviewAlertItems.length === 0) {
    inbox.append(createElement("p", "", "No hay alertas operativas registradas."));
    return;
  }
  const selectedIndex = reviewSelection?.family === "alert"
    ? reviewAlertItems.findIndex((item) => reviewItemId("alert", item) === reviewSelection.id)
    : -1;
  const visibleCount = progressiveCollectionVisibleCount(
    reviewAlertItems.length,
    reviewAlertVisibleCount,
    selectedIndex,
  );
  reviewAlertVisibleCount = visibleCount;
  for (const event of reviewAlertItems.slice(0, visibleCount)) {
    inbox.append(renderReviewMasterRow(
      "alert",
      event.alert_id,
      event.title || "Incidencia operativa",
      event.status,
      `${event.asset_id ? assetDisplayLabel(event.asset_id) : "Activo no especificado"} · ${formatInstant(event.last_activated_at)}`,
    ));
  }
  const status = createElement("p", "collection-status", "");
  const controls = createElement("div", "collection-controls");
  inbox.append(status, controls);
  renderProgressiveCollectionControls(
    status,
    controls,
    reviewAlertItems.length,
    visibleCount,
    {
      label: "alertas",
      onMore: () => {
        reviewAlertVisibleCount += PROGRESSIVE_COLLECTION_PAGE_SIZE;
        renderAlertInboxRows();
      },
      onLess: () => {
        reviewAlertVisibleCount = PROGRESSIVE_COLLECTION_PAGE_SIZE;
        renderAlertInboxRows();
      },
    },
  );
  renderReviewServerCoverageNote(
    inbox,
    reviewAlertServerTotal,
    reviewAlertItems.length,
    reviewAlertServerTruncated,
  );
  updateReviewMasterSelection();
}

function renderAlertInbox(payload) {
  reviewAlertItems = Array.isArray(payload.events) ? payload.events : [];
  reviewAlertItemsLoaded = true;
  reviewAlertVisibleCount = PROGRESSIVE_COLLECTION_PAGE_SIZE;
  reviewAlertServerTotal = Number.isInteger(payload.total) ? payload.total : reviewAlertItems.length;
  reviewAlertServerTruncated = Boolean(payload.truncated);
  byId("alert-inbox-summary").textContent = reviewAlertItems.length > 0
    ? `${formatInteger(reviewAlertServerTotal)} registradas · separadas de candidatos`
    : "Sin alertas operativas";
  renderAlertInboxRows();
  renderReviewDetail();
}

async function transitionAlert(alertId, status, button) {
  button.disabled = true;
  try {
    await api("/api/alerts/transition", {
      method: "POST",
      body: JSON.stringify({ alert_id: alertId, status }),
    });
    await Promise.all([loadAlertInbox(), refreshOverview()]);
  } catch (error) {
    setMessage(`No se pudo actualizar la alerta: ${error.message}`, true);
    button.disabled = false;
  }
}

async function loadAlertInbox() {
  const inbox = byId("alert-inbox");
  inbox.setAttribute("aria-busy", "true");
  reviewAlertItemsLoaded = false;
  try {
    renderAlertInbox(await api("/api/alerts?limit=50"));
  } catch (error) {
    reviewAlertItems = [];
    reviewAlertItemsLoaded = true;
    inbox.replaceChildren(
      createElement("p", "", `No se pudo consultar la bandeja: ${error.message}`),
    );
    renderReviewDetail();
  } finally {
    inbox.setAttribute("aria-busy", "false");
  }
}

const SCREENING_STATE_LABELS = Object.freeze({
  draft: "Borrador · solo replay",
  silent: "Monitoreo silencioso",
  active: "Activa · bandeja local",
  paused: "Pausada",
});

const SCREENING_OPERATOR_LABELS = Object.freeze({
  gt: ">",
  gte: "≥",
  lt: "<",
  lte: "≤",
  eq: "=",
});

const DESCRIPTIVE_OUTCOME_RULE_IDS = new Set([
  "market.technical.rsi-incremental-low-review",
  "market.technical.macd-incremental-positive-histogram-review",
]);
const DESCRIPTIVE_OUTCOME_MAX_BYTES = 262144;
const DESCRIPTIVE_OUTCOME_DEFAULT_DAYS = 30;
const DESCRIPTIVE_OUTCOME_MAX_DAYS = 90;
const DESCRIPTIVE_OUTCOME_WARNING =
  "Revisión descriptiva de condiciones; no mide rentabilidad ejecutable ni valida predicción.";
const DESCRIPTIVE_OUTCOME_GROUP_LABELS = Object.freeze({
  matched: "Cumplen",
  not_matched: "No cumplen",
  not_evaluable: "No evaluables",
});
const DESCRIPTIVE_OUTCOME_STATUS_LABELS = Object.freeze({
  available: "Disponible",
  pending_window: "Ventana abierta",
  insufficient_observed_bars: "Barras insuficientes",
  ambiguous_close: "Cierre ambiguo",
  invalid_close: "Cierre inválido",
  condition_not_evaluable: "Condición no evaluable",
});
const DESCRIPTIVE_OUTCOME_ERROR_MESSAGES = Object.freeze({
  screening_outcome_fingerprint_changed:
    "La configuración guardada cambió. Recarga las reglas antes de consultar.",
  screening_outcome_budget_exceeded:
    "La consulta excede el límite de 90 días, 4096 métricas o 256 KiB.",
  screening_outcome_unsupported:
    "Esta regla o activo no admite resultados posteriores.",
  screening_outcome_unavailable:
    "La evidencia disponible no permite construir la revisión descriptiva.",
});
const descriptiveOutcomeStates = new Map();

function descriptiveOutcomeState(ruleId) {
  let state = descriptiveOutcomeStates.get(ruleId);
  if (!state) {
    state = {
      generation: 0,
      controller: null,
      requestKey: null,
      payload: null,
      text: null,
      container: null,
      download: null,
    };
    descriptiveOutcomeStates.set(ruleId, state);
  }
  return state;
}

function invalidateDescriptiveOutcome(ruleId) {
  const state = descriptiveOutcomeStates.get(ruleId);
  if (!state) return;
  state.generation += 1;
  state.requestKey = null;
  state.payload = null;
  state.text = null;
  if (state.controller) {
    state.controller.abort();
    state.controller = null;
  }
  if (state.download) state.download.disabled = true;
  if (state.container) {
    state.container.replaceChildren(
      createElement(
        "p",
        "screening-rule-error",
        "Consulta invalidada por un cambio de activo, corte o regla.",
      ),
    );
  }
}

function invalidateScreeningOutcomes() {
  for (const ruleId of descriptiveOutcomeStates.keys()) {
    invalidateDescriptiveOutcome(ruleId);
  }
}

function descriptiveOutcomeDateFromCut(cut) {
  const parsed = Date.parse(String(cut || "").trim());
  if (Number.isNaN(parsed)) return null;
  return new Date(parsed).toISOString().slice(0, 10);
}

function descriptiveOutcomeShiftDate(value, days) {
  const [year, month, day] = value.split("-").map(Number);
  return new Date(Date.UTC(year, month - 1, day + days)).toISOString().slice(0, 10);
}

function descriptiveOutcomeCalendarDays(start, end) {
  const startMs = Date.parse(`${start}T00:00:00Z`);
  const endMs = Date.parse(`${end}T00:00:00Z`);
  if (!Number.isFinite(startMs) || !Number.isFinite(endMs)) return null;
  return Math.floor((endMs - startMs) / 86400000) + 1;
}

function formatDescriptiveOutcomeReturn(value) {
  const parsed = numericValue(value);
  if (parsed === null) return "—";
  const prefix = parsed > 0 ? "+" : "";
  return `${prefix}${formatNumber(parsed, {
    style: "percent",
    minimumFractionDigits: 0,
    maximumFractionDigits: 2,
  })}`;
}

function formatDescriptiveOutcomeDuration(seconds) {
  const parsed = numericValue(seconds);
  if (parsed === null) return "—";
  if (parsed >= 86400) {
    return `${formatNumber(parsed / 86400, { maximumFractionDigits: 1 })} d`;
  }
  if (parsed >= 3600) {
    return `${formatNumber(parsed / 3600, { maximumFractionDigits: 1 })} h`;
  }
  return `${formatNumber(parsed / 60, { maximumFractionDigits: 0 })} min`;
}

function descriptiveOutcomeConditionLabel(condition) {
  if (condition.state === "not_evaluable") {
    return `No evaluable · ${condition.reason || "sin evidencia"}`;
  }
  return condition.state === "met" ? "Cumple" : "No cumple";
}

function renderDescriptiveOutcomeEvidence(row, currency) {
  const details = document.createElement("details");
  details.append(createElement("summary", "", "Evidencia"));
  const condition = row.condition;
  details.append(
    createElement(
      "small",
      "",
      `Condición: ${condition.metric_key} · ${condition.state} · valor ${condition.observed_value ?? "—"} · as_of ${formatInstant(row.period_as_of)}`,
    ),
    createElement(
      "small",
      "",
      `Métrica ${condition.metric_result_id ?? "—"} · disponible ${condition.available_at ? formatInstant(condition.available_at) : "—"} · calculada ${condition.computed_at ? formatInstant(condition.computed_at) : "—"}`,
    ),
  );
  if (condition.explanation_es) {
    details.append(createElement("small", "", condition.explanation_es));
  }
  if (row.outcome.reason) {
    details.append(createElement("small", "", `Resultado: ${row.outcome.reason}`));
  }
  for (const close of row.outcome.closes || []) {
    details.append(
      createElement(
        "small",
        "",
        `Cierre ${formatInstant(close.period_start)}–${formatInstant(close.period_end)} · ${close.close} ${currency} · disponible ${formatInstant(close.available_at)} · ${close.quality}`,
      ),
    );
  }
  return details;
}

function renderDescriptiveOutcomeReport(container, payload) {
  container.replaceChildren();
  if (!payload || payload.schema_version !== "descriptive-condition-outcomes-v1") {
    container.append(
      createElement("p", "screening-rule-error", "La revisión descriptiva no tiene el contrato esperado."),
    );
    return;
  }
  const summary = createElement("div", "screening-backtest-grid");
  const metrics = [
    [
      "Períodos observados",
      `${formatInteger(payload.observed_periods)} de ${formatInteger(payload.requested_calendar_days)}`,
    ],
    ["Cumplen", formatInteger(payload.matched_count)],
    ["No cumplen", formatInteger(payload.not_matched_count)],
    ["No evaluables", formatInteger(payload.not_evaluable_count)],
  ];
  for (const [label, value] of metrics) {
    const item = createElement("span", "screening-backtest-metric");
    item.append(createElement("small", "", label), createElement("strong", "", value));
    summary.append(item);
  }
  container.append(
    summary,
    createElement(
      "small",
      "screening-backtest-range",
      `Corte ${formatInstant(payload.request.known_at)} · ${payload.request.start_date}–${payload.request.end_date} · configuración ${payload.rule_fingerprint}`,
    ),
    createElement("small", "screening-backtest-limitation", DESCRIPTIVE_OUTCOME_WARNING),
  );
  for (const group of payload.groups || []) {
    if (group.denominator === 0) continue;
    const block = createElement("div", "screening-backtest-result");
    block.append(
      createElement(
        "strong",
        "",
        `${DESCRIPTIVE_OUTCOME_GROUP_LABELS[group.group] || group.group} · n=${formatInteger(group.denominator)}`,
      ),
      createElement(
        "small",
        "",
        `Disponibles ${formatInteger(group.available_count)} · ventana abierta ${formatInteger(group.pending_window_count)} · barras insuficientes ${formatInteger(group.insufficient_observed_bars_count)} · cierres ambiguos ${formatInteger(group.ambiguous_close_count)} · cierres inválidos ${formatInteger(group.invalid_close_count)} · no evaluables ${formatInteger(group.condition_not_evaluable_count)}`,
      ),
    );
    if (group.available_count > 0) {
      block.append(
        createElement(
          "small",
          "",
          `R medio ${formatDescriptiveOutcomeReturn(group.mean_return_5_intervals)} · mediana ${formatDescriptiveOutcomeReturn(group.median_return_5_intervals)} · positivos ${formatInteger(group.positives)} · negativos ${formatInteger(group.negatives)} · ceros ${formatInteger(group.zeros)}`,
        ),
      );
    } else {
      block.append(createElement("small", "", "Sin retornos observados; media y mediana ausentes."));
    }
    container.append(block);
  }
  const table = document.createElement("table");
  table.className = "preferences-table";
  const head = document.createElement("thead");
  const headRow = document.createElement("tr");
  for (const label of ["Período", "Condición", "Resultado", "R (5 intervalos)", "Duración"]) {
    headRow.append(createElement("th", "", label));
  }
  head.append(headRow);
  const body = document.createElement("tbody");
  for (const row of (payload.rows || []).slice(0, DESCRIPTIVE_OUTCOME_MAX_DAYS)) {
    const tableRow = document.createElement("tr");
    tableRow.append(
      createElement("td", "", formatInstant(row.period_as_of)),
      createElement("td", "", descriptiveOutcomeConditionLabel(row.condition)),
      createElement(
        "td",
        "",
        DESCRIPTIVE_OUTCOME_STATUS_LABELS[row.outcome.status] || row.outcome.status,
      ),
      createElement(
        "td",
        "",
        formatDescriptiveOutcomeReturn(row.outcome.return_5_intervals),
      ),
      createElement(
        "td",
        "",
        formatDescriptiveOutcomeDuration(row.outcome.elapsed_seconds),
      ),
    );
    const evidenceRow = document.createElement("tr");
    const evidenceCell = document.createElement("td");
    evidenceCell.colSpan = 5;
    evidenceCell.append(renderDescriptiveOutcomeEvidence(row, payload.currency));
    evidenceRow.append(evidenceCell);
    body.append(tableRow, evidenceRow);
  }
  table.append(head, body);
  const tableWrapper = createElement("div", "chart-table-scroll");
  tableWrapper.append(table);
  container.append(tableWrapper);
  if ((payload.rows || []).length === 0) {
    container.append(
      createElement(
        "p",
        "",
        "Sin períodos observados en el rango y corte solicitados; la ausencia es explícita.",
      ),
    );
  }
}

async function fetchDescriptiveOutcomes(query, signal) {
  const response = await fetch(`/api/v1/screening/outcomes?${query.toString()}`, {
    cache: "no-store",
    signal,
  });
  const text = await response.text();
  if (new TextEncoder().encode(text).byteLength > DESCRIPTIVE_OUTCOME_MAX_BYTES) {
    throw new Error("La respuesta supera 256 KiB; no se descarga ni se trunca.");
  }
  let payload;
  try {
    payload = JSON.parse(text);
  } catch (error) {
    throw new Error("La respuesta de resultados posteriores no es JSON válido.");
  }
  if (!response.ok) {
    const error = payload.error || {};
    throw new Error(
      DESCRIPTIVE_OUTCOME_ERROR_MESSAGES[error.code]
        || ERROR_MESSAGES[error.code]
        || error.message
        || `Error HTTP ${response.status}`,
    );
  }
  return { payload, text };
}

function downloadDescriptiveOutcomeJson(configuration, button) {
  const state = descriptiveOutcomeState(configuration.rule.rule_id);
  if (!state.payload || !state.text) {
    button.disabled = true;
    return;
  }
  if (new TextEncoder().encode(state.text).byteLength > DESCRIPTIVE_OUTCOME_MAX_BYTES) {
    state.text = null;
    state.payload = null;
    button.disabled = true;
    return;
  }
  const symbol = marketAssets[selectedMarketAsset]?.symbol || "activo";
  const start = state.payload.request.start_date;
  const end = state.payload.request.end_date;
  downloadText(
    `${safeFilePart(symbol)}-resultados-${safeFilePart(configuration.rule.rule_id)}-${start}-${end}.json`,
    state.text,
    "application/json",
  );
}

async function runDescriptiveOutcomes(configuration, startInput, endInput, container, download, button) {
  const ruleId = configuration.rule.rule_id;
  const state = descriptiveOutcomeState(ruleId);
  const knownAt = byId("report-known-at").value.trim() || defaultKnownAtCut();
  const start = startInput.value;
  const end = endInput.value;
  const days = descriptiveOutcomeCalendarDays(start, end);
  if (!start || !end || days === null || days < 1 || days > DESCRIPTIVE_OUTCOME_MAX_DAYS) {
    container.replaceChildren(
      createElement(
        "p",
        "screening-rule-error",
        "El rango debe contener entre 1 y 90 días calendario inclusivos.",
      ),
    );
    return;
  }
  const requestKey = JSON.stringify({
    ruleId,
    assetId: selectedMarketAsset,
    fingerprint: configuration.fingerprint,
    start,
    end,
    knownAt,
  });
  state.generation += 1;
  const generation = state.generation;
  if (state.controller) state.controller.abort();
  const controller = new AbortController();
  state.controller = controller;
  state.requestKey = requestKey;
  state.payload = null;
  state.text = null;
  download.disabled = true;
  container.replaceChildren(createElement("p", "", "Leyendo condiciones y cierres persistidos…"));
  setButtonBusy(button, true, "Consultando…", "Resultados posteriores");
  try {
    const query = new URLSearchParams({
      asset_id: selectedMarketAsset,
      rule_id: ruleId,
      expected_rule_fingerprint: configuration.fingerprint,
      start_date: start,
      end_date: end,
      known_at: knownAt,
    });
    const response = await fetchDescriptiveOutcomes(query, controller.signal);
    if (generation !== state.generation || state.requestKey !== requestKey) return;
    state.payload = response.payload;
    state.text = response.text;
    renderDescriptiveOutcomeReport(container, response.payload);
    download.disabled = false;
  } catch (error) {
    if (error.name === "AbortError" || generation !== state.generation || state.requestKey !== requestKey) {
      return;
    }
    container.replaceChildren(createElement("p", "screening-rule-error", error.message));
    download.disabled = true;
  } finally {
    if (generation === state.generation && state.requestKey === requestKey) {
      state.controller = null;
      setButtonBusy(button, false, "Consultando…", "Resultados posteriores");
    }
  }
}

function renderDescriptiveOutcomeAction(configuration, card) {
  const ruleId = configuration.rule.rule_id;
  if (!DESCRIPTIVE_OUTCOME_RULE_IDS.has(ruleId)) return;
  const state = descriptiveOutcomeState(ruleId);
  const panel = createElement("section", "screening-backtest-result screening-outcomes");
  const knownAt = byId("report-known-at").value.trim() || defaultKnownAtCut();
  const endDate = descriptiveOutcomeDateFromCut(knownAt)
    || new Date().toISOString().slice(0, 10);
  const startDate = descriptiveOutcomeShiftDate(
    endDate,
    -(DESCRIPTIVE_OUTCOME_DEFAULT_DAYS - 1),
  );
  const startInput = document.createElement("input");
  startInput.type = "date";
  startInput.value = startDate;
  startInput.dataset.descriptiveOutcomeStart = "true";
  const endInput = document.createElement("input");
  endInput.type = "date";
  endInput.value = endDate;
  endInput.dataset.descriptiveOutcomeEnd = "true";
  const controls = createElement("div", "screening-rule-actions");
  const run = createElement("button", "button secondary compact", "Resultados posteriores");
  run.type = "button";
  const download = createElement("button", "button secondary compact", "Descargar JSON");
  download.type = "button";
  download.disabled = true;
  const result = createElement("div", "screening-outcomes-result");
  result.append(
    createElement("p", "", "Resultados aún no consultados para el activo y corte actuales."),
  );
  controls.append(
    screeningField("Desde", startInput),
    screeningField("Hasta", endInput),
    run,
    download,
  );
  panel.append(
    createElement(
      "small",
      "screening-backtest-range",
      `Corte ${knownAt} · configuración guardada ${configuration.fingerprint}`,
    ),
    controls,
    result,
  );
  state.container = result;
  state.download = download;
  if (state.payload) {
    renderDescriptiveOutcomeReport(result, state.payload);
    download.disabled = !state.text;
  }
  startInput.addEventListener("change", () => invalidateDescriptiveOutcome(ruleId));
  endInput.addEventListener("change", () => invalidateDescriptiveOutcome(ruleId));
  run.addEventListener("click", () => {
    void runDescriptiveOutcomes(
      configuration,
      startInput,
      endInput,
      result,
      download,
      run,
    );
  });
  download.addEventListener("click", () => downloadDescriptiveOutcomeJson(configuration, download));
  card.append(panel);
}

function screeningField(labelText, control) {
  const label = createElement("label", "field screening-rule-field");
  label.append(createElement("span", "", labelText), control);
  return label;
}

function screeningRulePayload(configuration, form, sourceRule = null) {
  const rule = sourceRule || configuration.rule;
  if (sourceRule) {
    return {
      schema_version: "analytical-rule-configuration-update-v1",
      rule_id: configuration.rule.rule_id,
      expected_fingerprint: configuration.fingerprint,
      state: sourceRule.state,
      confirmations_required: sourceRule.confirmations_required,
      cooldown_seconds: sourceRule.cooldown_seconds,
      conditions: sourceRule.conditions.map((condition) => ({
        condition_id: condition.condition_id,
        threshold: condition.threshold,
        exit_threshold: condition.exit_threshold,
      })),
    };
  }
  const state = form.querySelector("[data-screening-state]")?.value;
  const confirmations = Number(
    form.querySelector("[data-screening-confirmations]")?.value,
  );
  const cooldownHours = Number(
    form.querySelector("[data-screening-cooldown-hours]")?.value,
  );
  if (
    !Object.hasOwn(SCREENING_STATE_LABELS, state)
    || !Number.isInteger(confirmations)
    || confirmations < 1
    || confirmations > 20
    || !Number.isFinite(cooldownHours)
    || cooldownHours < 0
  ) {
    throw new Error("Revisa el estado, las confirmaciones y la espera configurada.");
  }
  const controls = new Map(
    Array.from(form.querySelectorAll("[data-screening-condition]")).map((control) => [
      control.dataset.screeningCondition,
      control,
    ]),
  );
  const conditions = rule.conditions.map((condition) => {
    const group = controls.get(condition.condition_id);
    if (!group) throw new Error("La edición no coincide con el contrato de la regla.");
    const threshold = group.querySelector("[data-screening-threshold]")?.value.trim();
    const exitValue = group.querySelector("[data-screening-exit-threshold]")?.value.trim();
    if (!threshold) throw new Error("Cada condición requiere un umbral.");
    return {
      condition_id: condition.condition_id,
      threshold,
      exit_threshold: exitValue || null,
    };
  });
  return {
    schema_version: "analytical-rule-configuration-update-v1",
    rule_id: configuration.rule.rule_id,
    expected_fingerprint: configuration.fingerprint,
    state,
    confirmations_required: confirmations,
    cooldown_seconds: Math.round(cooldownHours * 3600),
    conditions,
  };
}

function renderScreeningBacktest(container, payload) {
  container.replaceChildren();
  const evaluated = payload.evaluations.length;
  const range = `${formatCalendarDate(payload.first_known_at)}–${formatCalendarDate(payload.last_known_at)}`;
  const grid = createElement("div", "screening-backtest-grid");
  const metrics = [
    ["Cortes", `${formatInteger(evaluated)} de ${formatInteger(payload.total_available_cuts)}`],
    [
      "Coincidencias",
      `${formatInteger(payload.matched_count)} · ${formatUnsignedPercentage(payload.match_rate)}`,
    ],
    ["Candidatos simulados", formatInteger(payload.candidate_activation_count)],
    ["No evaluables", formatInteger(payload.not_evaluable_count)],
  ];
  for (const [label, value] of metrics) {
    const item = createElement("span", "screening-backtest-metric");
    item.append(createElement("small", "", label), createElement("strong", "", value));
    grid.append(item);
  }
  container.append(
    grid,
    createElement(
      "small",
      "screening-backtest-range",
      `${range}${payload.truncated ? " · muestra limitada a los cortes más recientes" : ""}`,
    ),
    createElement(
      "small",
      "screening-backtest-limitation",
      "Replay descriptivo: no mide rentabilidad posterior ni precisión predictiva.",
    ),
  );
}

async function runScreeningBacktest(configuration, container, button) {
  setButtonBusy(button, true, "Calculando…", `Replay de ${marketAssets[selectedMarketAsset]?.symbol || "activo"}`);
  container.replaceChildren(createElement("p", "", "Leyendo snapshots persistidos…"));
  try {
    const query = new URLSearchParams({
      rule_id: configuration.rule.rule_id,
      asset_id: selectedMarketAsset,
      max_cuts: "200",
    });
    renderScreeningBacktest(
      container,
      await api(`/api/screening-backtest?${query.toString()}`),
    );
  } catch (error) {
    container.replaceChildren(
      createElement("p", "screening-rule-error", error.message),
    );
  } finally {
    setButtonBusy(
      button,
      false,
      "Calculando…",
      `Replay de ${marketAssets[selectedMarketAsset]?.symbol || "activo"}`,
    );
  }
}

async function updateScreeningRule(configuration, form, button, sourceRule = null) {
  const idleLabel = sourceRule ? "Restaurar valores iniciales" : "Guardar regla";
  invalidateDescriptiveOutcome(configuration.rule.rule_id);
  setButtonBusy(button, true, "Guardando…", idleLabel);
  try {
    const payload = screeningRulePayload(configuration, form, sourceRule);
    const outcome = await api("/api/screening-rules/update", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    setMessage(
      outcome.changed
        ? "Regla versionada. Se aplicará a la próxima evidencia nueva."
        : "La regla ya tenía esos valores.",
      false,
    );
    await loadScreeningRules();
  } catch (error) {
    setMessage(`No se pudo guardar la regla: ${error.message}`, true);
    button.disabled = false;
  }
}

function renderScreeningRule(configuration) {
  const { rule, default_rule: defaultRule } = configuration;
  const card = createElement("article", "screening-rule-card");
  const heading = createElement("div", "screening-rule-heading");
  const title = createElement("div");
  title.append(
    createElement("strong", "", rule.name_es),
    createElement(
      "small",
      "",
      `${rule.domain === "market" ? "Mercado" : "Fundamentales"} · v${rule.rule_version}`,
    ),
  );
  heading.append(
    title,
    createElement(
      "span",
      `screening-rule-badge${configuration.customized ? " customized" : ""}`,
      configuration.customized ? "Personalizada" : "Inicial",
    ),
  );
  const form = createElement("form", "screening-rule-form");
  const state = document.createElement("select");
  state.dataset.screeningState = "true";
  for (const [value, label] of Object.entries(SCREENING_STATE_LABELS)) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    option.selected = rule.state === value;
    state.append(option);
  }
  const confirmations = document.createElement("input");
  confirmations.type = "number";
  confirmations.min = "1";
  confirmations.max = "20";
  confirmations.step = "1";
  confirmations.value = String(rule.confirmations_required);
  confirmations.dataset.screeningConfirmations = "true";
  const cooldown = document.createElement("input");
  cooldown.type = "number";
  cooldown.min = "0";
  cooldown.max = "8760";
  cooldown.step = "0.25";
  cooldown.value = String(rule.cooldown_seconds / 3600);
  cooldown.dataset.screeningCooldownHours = "true";
  const controls = createElement("div", "screening-rule-controls");
  controls.append(
    screeningField("Estado", state),
    screeningField("Confirmaciones", confirmations),
    screeningField("Espera (horas)", cooldown),
  );
  const conditionGrid = createElement("div", "screening-condition-grid");
  for (const condition of rule.conditions) {
    const group = createElement("fieldset", "screening-condition");
    group.dataset.screeningCondition = condition.condition_id;
    const legend = createElement(
      "legend",
      "",
      `${condition.label_es} ${SCREENING_OPERATOR_LABELS[condition.operator] || condition.operator}`,
    );
    const threshold = document.createElement("input");
    threshold.type = "text";
    threshold.inputMode = "decimal";
    threshold.value = condition.threshold;
    threshold.dataset.screeningThreshold = "true";
    group.append(legend, screeningField("Umbral de entrada", threshold));
    const exitThreshold = document.createElement("input");
    exitThreshold.type = "text";
    exitThreshold.inputMode = "decimal";
    exitThreshold.value = condition.exit_threshold ?? "";
    exitThreshold.dataset.screeningExitThreshold = "true";
    group.append(screeningField("Umbral de salida", exitThreshold));
    if (condition.unit === "ratio") {
      group.append(
        createElement(
          "small",
          "",
          condition.metric_key === "market.history.relative_volume"
            ? "Múltiplo; por ejemplo, 1.5×."
            : "Ratio decimal; por ejemplo, 0.60 = 60%.",
        ),
      );
    }
    conditionGrid.append(group);
  }
  const actions = createElement("div", "screening-rule-actions");
  const save = createElement("button", "button primary compact", "Guardar regla");
  save.type = "submit";
  const reset = createElement(
    "button",
    "button secondary compact",
    "Restaurar valores iniciales",
  );
  reset.type = "button";
  reset.disabled = !configuration.customized;
  const backtest = createElement(
    "button",
    "button secondary compact",
    `Replay de ${marketAssets[selectedMarketAsset]?.symbol || "activo"}`,
  );
  backtest.type = "button";
  actions.append(save, reset, backtest);
  const replay = createElement("div", "screening-backtest-result");
  replay.append(createElement("p", "", "Replay aún no ejecutado para el activo seleccionado."));
  form.append(controls, conditionGrid, actions, replay);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    void updateScreeningRule(configuration, form, save);
  });
  reset.addEventListener("click", () => {
    void updateScreeningRule(configuration, form, reset, defaultRule);
  });
  backtest.addEventListener("click", () => {
    void runScreeningBacktest(configuration, replay, backtest);
  });
  form.addEventListener("input", () => invalidateDescriptiveOutcome(rule.rule_id));
  form.addEventListener("change", () => invalidateDescriptiveOutcome(rule.rule_id));
  card.append(heading, form);
  renderDescriptiveOutcomeAction(configuration, card);
  return card;
}

function renderScreeningRules(payload) {
  const container = byId("screening-rules");
  container.replaceChildren();
  if (!Array.isArray(payload.configurations) || payload.configurations.length === 0) {
    container.append(createElement("p", "", "No hay reglas de screening configuradas."));
    byId("screening-rules-summary").textContent = "Registro no configurado";
    return;
  }
  byId("screening-rules-summary").textContent =
    `${formatInteger(payload.configurations.length)} reglas · ${formatInteger(payload.total_revisions)} revisiones locales`;
  for (const configuration of payload.configurations) {
    container.append(renderScreeningRule(configuration));
  }
}

async function loadScreeningRules() {
  const container = byId("screening-rules");
  container.setAttribute("aria-busy", "true");
  try {
    screeningRuleSnapshot = await api("/api/screening-rules");
    renderScreeningRules(screeningRuleSnapshot);
  } catch (error) {
    container.replaceChildren(
      createElement("p", "screening-rule-error", `No se pudieron cargar las reglas: ${error.message}`),
    );
  } finally {
    container.setAttribute("aria-busy", "false");
  }
}

function formatCandidateCondition(condition, definition) {
  if (condition.state === "not_evaluable") return `${definition.label_es}: sin evidencia`;
  const parsed = numericValue(condition.observed_value);
  if (parsed === null) return `${definition.label_es}: —`;
  const percentageKeys = new Set([
    "fundamental.liabilities_to_assets",
    "fundamental.net_margin",
    "fundamental.revenue_yoy_growth",
  ]);
  const value = percentageKeys.has(condition.metric_key)
    ? formatUnsignedPercentage(parsed)
    : condition.unit === "ratio"
      ? `${formatNumber(parsed, { maximumFractionDigits: 2 })}×`
      : `${formatNumber(parsed, { maximumFractionDigits: 2 })} ${condition.unit}`;
  return `${definition.label_es}: ${value}`;
}

function renderCandidateInboxRows() {
  const inbox = byId("candidate-inbox");
  inbox.replaceChildren();
  if (reviewCandidateItems.length === 0) {
    inbox.append(createElement("p", "", "No hay candidatos analíticos registrados."));
    return;
  }
  const selectedIndex = reviewSelection?.family === "candidate"
    ? reviewCandidateItems.findIndex((item) => reviewItemId("candidate", item) === reviewSelection.id)
    : -1;
  const visibleCount = progressiveCollectionVisibleCount(
    reviewCandidateItems.length,
    reviewCandidateVisibleCount,
    selectedIndex,
  );
  reviewCandidateVisibleCount = visibleCount;
  for (const itemPayload of reviewCandidateItems.slice(0, visibleCount)) {
    const { event, result } = itemPayload;
    const assetLabel = assetDisplayLabel(result.asset_id);
    inbox.append(renderReviewMasterRow(
      "candidate",
      event.candidate_id,
      `${result.rule.name_es} · ${assetLabel}`,
      event.status,
      `${formatCalendarDate(event.as_of)} · ${formatInteger(event.confirmations)} confirmación${event.confirmations === 1 ? "" : "es"}`,
    ));
  }
  const status = createElement("p", "collection-status", "");
  const controls = createElement("div", "collection-controls");
  inbox.append(status, controls);
  renderProgressiveCollectionControls(
    status,
    controls,
    reviewCandidateItems.length,
    visibleCount,
    {
      label: "candidatos",
      onMore: () => {
        reviewCandidateVisibleCount += PROGRESSIVE_COLLECTION_PAGE_SIZE;
        renderCandidateInboxRows();
      },
      onLess: () => {
        reviewCandidateVisibleCount = PROGRESSIVE_COLLECTION_PAGE_SIZE;
        renderCandidateInboxRows();
      },
    },
  );
  renderReviewServerCoverageNote(
    inbox,
    reviewCandidateServerTotal,
    reviewCandidateItems.length,
    reviewCandidateServerTruncated,
  );
  updateReviewMasterSelection();
}

function renderCandidateInbox(payload) {
  reviewCandidateItems = Array.isArray(payload.items) ? payload.items : [];
  reviewCandidateItemsLoaded = true;
  reviewCandidateVisibleCount = PROGRESSIVE_COLLECTION_PAGE_SIZE;
  reviewCandidateServerTotal = Number.isInteger(payload.total) ? payload.total : reviewCandidateItems.length;
  reviewCandidateServerTruncated = Boolean(payload.truncated);
  byId("candidate-inbox-summary").textContent = reviewCandidateItems.length > 0
    ? `${formatInteger(reviewCandidateServerTotal)} registrados · separados de alertas`
    : "Sin candidatos analíticos";
  renderCandidateInboxRows();
  renderReviewDetail();
}

async function transitionCandidate(candidateId, status, button) {
  button.disabled = true;
  try {
    await api("/api/candidates/transition", {
      method: "POST",
      body: JSON.stringify({ candidate_id: candidateId, status }),
    });
    await Promise.all([loadCandidateInbox(), refreshOverview()]);
  } catch (error) {
    setMessage(`No se pudo actualizar el candidato: ${error.message}`, true);
    button.disabled = false;
  }
}

async function loadCandidateInbox() {
  const inbox = byId("candidate-inbox");
  inbox.setAttribute("aria-busy", "true");
  reviewCandidateItemsLoaded = false;
  try {
    renderCandidateInbox(await api("/api/candidates?limit=50"));
  } catch (error) {
    reviewCandidateItems = [];
    reviewCandidateItemsLoaded = true;
    inbox.replaceChildren(
      createElement("p", "", `No se pudo consultar la bandeja: ${error.message}`),
    );
    renderReviewDetail();
  } finally {
    inbox.setAttribute("aria-busy", "false");
  }
}

function candidateNotificationTitle(notification) {
  return notification.payload?.rule_name_es
    || notification.payload?.operating_title
    || notification.title
    || "Notificación analítica";
}

function renderCandidateNotificationRows() {
  const inbox = byId("candidate-notifications");
  inbox.replaceChildren();
  const items = Array.isArray(candidateNotificationPayload?.items)
    ? candidateNotificationPayload.items
    : [];
  if (items.length === 0) {
    inbox.append(createElement("p", "", "No hay notificaciones locales pendientes."));
    return;
  }
  const visibleCount = progressiveCollectionVisibleCount(
    items.length,
    candidateNotificationVisibleCount,
  );
  candidateNotificationVisibleCount = visibleCount;
  for (const view of items.slice(0, visibleCount)) {
    const notification = view.item;
    const item = createElement("article", "alert-inbox-item candidate-inbox-item");
    const assetLabel = assetDisplayLabel(notification.asset_id);
    item.append(
      createElement("strong", "", `${candidateNotificationTitle(notification)} · ${assetLabel}`),
      createElement(
        "p",
        "",
        `Candidato analítico · ${view.status === "acknowledged" ? "Entrega confirmada" : "Pendiente"}`,
      ),
      createElement(
        "span",
        `alert-inbox-status ${view.status}`,
        view.status === "acknowledged" ? "Entrega confirmada" : "Pendiente",
      ),
      createElement("time", "", formatInstant(notification.created_at)),
    );
    if (view.status !== "acknowledged") {
      const button = createElement("button", "alert-action-button", "Confirmar entrega");
      button.type = "button";
      button.addEventListener("click", () => acknowledgeCandidateNotification(notification.notification_id, button));
      const actions = createElement("div", "alert-inbox-actions");
      actions.append(button);
      item.append(actions);
    }
    inbox.append(item);
  }
  const status = createElement("p", "collection-status", "");
  const controls = createElement("div", "collection-controls");
  inbox.append(status, controls);
  renderProgressiveCollectionControls(
    status,
    controls,
    items.length,
    visibleCount,
    {
      label: "notificaciones",
      onMore: () => {
        candidateNotificationVisibleCount += PROGRESSIVE_COLLECTION_PAGE_SIZE;
        renderCandidateNotificationRows();
      },
      onLess: () => {
        candidateNotificationVisibleCount = PROGRESSIVE_COLLECTION_PAGE_SIZE;
        renderCandidateNotificationRows();
      },
    },
  );
  renderReviewServerCoverageNote(
    inbox,
    Number.isInteger(candidateNotificationPayload.total)
      ? candidateNotificationPayload.total
      : items.length,
    items.length,
    Boolean(candidateNotificationPayload.truncated),
  );
}

function renderCandidateNotifications(payload) {
  candidateNotificationPayload = payload;
  candidateNotificationVisibleCount = PROGRESSIVE_COLLECTION_PAGE_SIZE;
  renderCandidateNotificationRows();
}

async function acknowledgeCandidateNotification(notificationId, button) {
  button.disabled = true;
  try {
    await api("/api/v1/candidate-notifications/acknowledge", {
      method: "POST",
      body: JSON.stringify({ notification_id: notificationId }),
    });
    await Promise.all([loadCandidateNotifications(), refreshOverview()]);
  } catch (error) {
    setMessage(`No se pudo confirmar la notificación: ${error.message}`, true);
    button.disabled = false;
  }
}

async function loadCandidateNotifications() {
  const inbox = byId("candidate-notifications");
  inbox.setAttribute("aria-busy", "true");
  try {
    renderCandidateNotifications(await api("/api/v1/candidate-notifications"));
  } catch (error) {
    inbox.replaceChildren(
      createElement("p", "", `No se pudieron consultar las notificaciones: ${error.message}`),
    );
  } finally {
    inbox.setAttribute("aria-busy", "false");
  }
}

let desktopNotificationPayload = null;

const DESKTOP_NOTIFICATION_STATUS_LABELS = Object.freeze({
  pending: "Pendiente",
  attempting: "Intentando",
  accepted: "Aceptado por Windows",
  retry_wait: "Esperando reintento",
  unknown: "Estado desconocido",
  cancelled: "Cancelado",
  failed: "Fallido",
});

const DESKTOP_NOTIFICATION_REASON_LABELS = Object.freeze({
  channel_disabled: "canal desactivado",
  candidate_missing: "candidato ausente",
  candidate_not_new: "el candidato ya no está nuevo",
  candidate_acknowledged: "entrega confirmada por el usuario",
  candidate_evidence_mismatch: "evidencia del candidato incompatible",
  notification_missing: "aviso local ausente",
  notification_evidence_mismatch: "evidencia del aviso incompatible",
  result_missing: "resultado de activación ausente",
  rule_configuration_missing: "regla no configurada",
  rule_not_active_at_activation: "la regla no estaba activa al activarse",
  rule_not_active_now: "la regla no está activa ahora",
  rule_fingerprint_changed: "la regla cambió de forma incompatible",
  notification_before_activation: "aviso anterior a la activación",
  budget_exceeded: "presupuesto del canal agotado",
  interrupted_after_invocation: "interrupción tras invocar el envío",
  bridge_unavailable: "puente WSL a Windows no disponible",
  powershell_missing: "Windows PowerShell no disponible",
  script_missing: "script fijo del release ausente",
  session_unavailable: "sesión interactiva de Windows no disponible",
  winrt_unavailable: "API de avisos de Windows no disponible",
  registration_failed: "identidad propia no registrada",
  timeout: "tiempo de espera agotado",
  invalid_result: "respuesta del transporte ilegible",
  process_exit: "el proceso de transporte terminó con error",
  output_limit: "salida del transporte excedida",
  send_error: "Windows rechazó el aviso",
  probe_failed: "sonda de capacidad fallida",
});

function desktopNotificationReasonLabel(reason) {
  if (!reason) return "";
  return DESKTOP_NOTIFICATION_REASON_LABELS[reason] || reason;
}

function renderDesktopNotifications(payload) {
  desktopNotificationPayload = payload;
  byId("desktop-notification-summary").textContent = payload.enabled
    ? `${formatInteger(payload.accepted_count)} aceptados · ${formatInteger(payload.pending_count)} pendientes`
    : "Desactivado · sin envío automático";
  const capability = payload.capability || {};
  const capabilityTarget = byId("desktop-notification-capability");
  if (capability.available === true) {
    capabilityTarget.textContent =
      "Capacidad verificada: Windows disponible e identidad propia registrada.";
  } else if (capability.available === false) {
    capabilityTarget.textContent =
      `Capacidad no disponible: ${desktopNotificationReasonLabel(capability.reason)}.`;
  } else {
    capabilityTarget.textContent =
      "Capacidad sin consultar; esta lectura no ejecuta sondeos ni registros.";
  }
  byId("desktop-notification-toggle").textContent = payload.enabled
    ? "Desactivar avisos Windows"
    : "Activar avisos Windows";
  byId("desktop-notification-test").disabled = !payload.enabled;
  const counts = byId("desktop-notification-counts");
  counts.replaceChildren();
  const counters = [
    ["Pendientes", payload.pending_count],
    ["Reintentos", payload.retry_wait_count],
    ["Aceptados", payload.accepted_count],
    ["Desconocidos", payload.unknown_count],
    ["Fallidos", payload.failed_count],
    ["Cancelados", payload.cancelled_count],
  ];
  for (const [label, value] of counters) {
    const block = createElement("span", "screening-backtest-metric");
    block.append(
      createElement("small", "", label),
      createElement("strong", "", formatInteger(value)),
    );
    counts.append(block);
  }
  byId("desktop-notification-budget").textContent =
    `Estado ${formatInteger(payload.state_bytes)} de ${formatInteger(payload.budget_bytes)} bytes · `
    + `${formatInteger(payload.total)} de ${formatInteger(payload.retained_limit)} entregas`
    + (payload.budget_exceeded ? " · admisión y lotes pausados por presupuesto" : "");
  const inbox = byId("desktop-notifications");
  inbox.replaceChildren();
  const deliveries = Array.isArray(payload.deliveries) ? payload.deliveries : [];
  if (deliveries.length === 0) {
    inbox.append(createElement("p", "", "Sin entregas registradas para este workspace."));
  }
  for (const delivery of deliveries) {
    const item = createElement("article", "alert-inbox-item");
    item.append(
      createElement(
        "strong",
        "",
        `${delivery.title_es} · ${assetDisplayLabel(delivery.asset_id)}`,
      ),
      createElement("p", "", delivery.body_es),
      createElement(
        "span",
        `alert-inbox-status ${delivery.status}`,
        DESKTOP_NOTIFICATION_STATUS_LABELS[delivery.status] || delivery.status,
      ),
    );
    if (delivery.reason) {
      item.append(
        createElement("small", "", `Causa: ${desktopNotificationReasonLabel(delivery.reason)}`),
      );
    }
    item.append(
      createElement(
        "time",
        "",
        `${formatInstant(delivery.updated_at)} · intento ${formatInteger(delivery.attempt_count)}`,
      ),
    );
    inbox.append(item);
  }
  renderReviewServerCoverageNote(
    inbox,
    Number.isInteger(payload.total) ? payload.total : deliveries.length,
    deliveries.length,
    Boolean(payload.truncated),
  );
}

async function loadDesktopNotifications() {
  const inbox = byId("desktop-notifications");
  inbox.setAttribute("aria-busy", "true");
  try {
    renderDesktopNotifications(await api("/api/v1/desktop-notifications?limit=50"));
  } catch (error) {
    inbox.replaceChildren(
      createElement("p", "", `No se pudieron consultar los avisos Windows: ${error.message}`),
    );
  } finally {
    inbox.setAttribute("aria-busy", "false");
  }
}

async function toggleDesktopNotifications() {
  const toggle = byId("desktop-notification-toggle");
  const enabled = desktopNotificationPayload?.enabled !== true;
  toggle.disabled = true;
  byId("desktop-notification-status").textContent = enabled
    ? "Registrando identidad y activando…"
    : "Desactivando…";
  try {
    await api("/api/v1/desktop-notifications/configure", {
      method: "POST",
      body: JSON.stringify({ enabled }),
    });
    byId("desktop-notification-status").textContent = enabled
      ? "Avisos Windows activados; la frontera excluye candidatos anteriores."
      : "Avisos Windows desactivados; pendientes cancelados sin borrar evidencia.";
    await loadDesktopNotifications();
  } catch (error) {
    byId("desktop-notification-status").textContent =
      `No se pudo cambiar el canal: ${error.message}`;
  } finally {
    toggle.disabled = false;
  }
}

async function sendDesktopNotificationTest() {
  const button = byId("desktop-notification-test");
  button.disabled = true;
  byId("desktop-notification-status").textContent = "Enviando prueba de transporte…";
  try {
    const outcome = await api("/api/v1/desktop-notifications/test", {
      method: "POST",
      body: JSON.stringify({ confirm: true }),
    });
    byId("desktop-notification-status").textContent = outcome.accepted
      ? "Prueba aceptada por Windows; no demuestra que el aviso se haya visto."
      : `Prueba no aceptada: ${desktopNotificationReasonLabel(outcome.reason) || "sin causa declarada"}.`;
    await loadDesktopNotifications();
  } catch (error) {
    byId("desktop-notification-status").textContent =
      `No se pudo ejecutar la prueba: ${error.message}`;
  } finally {
    button.disabled = desktopNotificationPayload?.enabled !== true;
  }
}

let revisarExploreBound = false;

function decimalPercentToRatioString(rawPercent) {
  const parsed = parseDecimalString(rawPercent);
  if (!parsed) return null;
  const paddedInt = parsed.intDigits.padStart(3, "0");
  const shiftIdx = paddedInt.length - 2;
  const newInt = paddedInt.slice(0, shiftIdx).replace(/^0+(?=\d)/, "") || "0";
  const newFrac = (paddedInt.slice(shiftIdx) + parsed.fracDigits).replace(/0+$/, "");
  const sign = parsed.sign < 0 ? "-" : "";
  return newFrac ? `${sign}${newInt}.${newFrac}` : `${sign}${newInt}`;
}

function renderRevisarExploreResults() {
  const host = byId("review-explore-results");
  const statusEl = byId("review-filter-status");
  if (!host || !statusEl) return;
  const knownAt = byId("report-known-at")?.value.trim() || "";
  const totalCatalog = Object.keys(marketAssets || {}).length;
  const loadedAssets = Array.from(
    sameIsoTimestamp(investorOverviewState.knownAt, knownAt) ? investorOverviewState.byAssetId.values() : [],
  );

  const rawPe = byId("review-filter-pe-max")?.value.trim() || "";
  const rawMargin = byId("review-filter-margin-min")?.value.trim() || "";
  const rawLev = byId("review-filter-leverage-max")?.value.trim() || "";

  if ((rawPe && !parseDecimalString(rawPe)) || (rawMargin && !parseDecimalString(rawMargin)) || (rawLev && !parseDecimalString(rawLev))) {
    statusEl.textContent = "Entrada numérica inválida: usa números decimales válidos (sin texto).";
    host.replaceChildren();
    return;
  }
  const marginRatio = rawMargin ? decimalPercentToRatioString(rawMargin) : null;

  let excludedByAbsence = 0;
  const matches = [];
  for (const ov of loadedAssets) {
    let ok = true;
    if (rawPe) {
      const pe = ov.valuation?.price_to_earnings;
      if (!pe || pe.state !== "available" || pe.value === null) {
        excludedByAbsence += 1;
        ok = false;
      } else if (compareDecimalStrings(pe.value, "0") <= 0 || compareDecimalStrings(pe.value, rawPe) > 0) {
        ok = false;
      }
    }
    if (ok && marginRatio !== null) {
      const nm = ov.fundamental?.net_margin;
      if (!nm || nm.state !== "available" || nm.value === null) {
        excludedByAbsence += 1;
        ok = false;
      } else if (compareDecimalStrings(nm.value, marginRatio) < 0) {
        ok = false;
      }
    }
    if (ok && rawLev) {
      const lev = ov.fundamental?.liabilities_to_assets;
      if (!lev || lev.state !== "available" || lev.value === null) {
        excludedByAbsence += 1;
        ok = false;
      } else if (compareDecimalStrings(lev.value, rawLev) > 0) {
        ok = false;
      }
    }
    if (ok) matches.push(ov);
  }

  matches.sort((a, b) => String(a.asset_id).localeCompare(String(b.asset_id)));
  statusEl.textContent = `Catálogo: ${totalCatalog} · Cargados al corte: ${loadedAssets.length} · Coinciden: ${matches.length} · Excluidos por ausencia: ${excludedByAbsence}`;
  host.replaceChildren();
  if (!loadedAssets.length) {
    host.append(createElement("p", "chart-status", "No hay datos cargados al corte. Carga lotes en Mesa o pulsa Aplicar."));
    return;
  }
  if (!matches.length) {
    host.append(createElement("p", "chart-status", "Ningún activo cargado cumple todos los filtros."));
    return;
  }
  for (const ov of matches) {
    const row = createElement("div", "review-explore-item");
    const peRaw = ov.valuation?.price_to_earnings?.value;
    const nmRaw = ov.fundamental?.net_margin?.value;
    const levRaw = ov.fundamental?.liabilities_to_assets?.value;
    const peText = formatDecimalMultiple(peRaw, 2);
    const nmText = formatDecimalPercentFromRatio(nmRaw, { decimals: 2, signed: true });
    const levText = formatDecimalRatio(levRaw, 2);
    const metricsEl = createElement(
      "span",
      "review-explore-metrics",
      `P/E: ${peText} · Margen neto: ${nmText} · Pasivos/Activos: ${levText}`,
    );
    const exactSummary = [
      peRaw !== null && peRaw !== undefined && String(peRaw).trim() !== "" ? `P/E exacto: ${peRaw}` : null,
      nmRaw !== null && nmRaw !== undefined && String(nmRaw).trim() !== ""
        ? `Margen neto exacto: ${formatExactDecimalText(nmRaw)}`
        : null,
      levRaw !== null && levRaw !== undefined && String(levRaw).trim() !== "" ? `Pasivos/Activos exacto: ${levRaw}` : null,
    ].filter(Boolean);
    if (exactSummary.length) {
      const exactText = exactSummary.join(" · ");
      metricsEl.title = exactText;
      metricsEl.setAttribute(
        "aria-label",
        `P/E: ${peText}, Margen neto: ${nmText}, Pasivos/Activos: ${levText} (${exactText})`,
      );
    }
    row.append(
      createElement("strong", "", `${ov.symbol} · ${ov.name}`),
      metricsEl,
      createContextualNavigationButton(
        "Abrir activo",
        { board: "activo", assetId: ov.asset_id, subtab: "mercado" },
        "button secondary compact",
      ),
    );
    host.append(row);
  }
}

function bindRevisarExploreControls() {
  if (revisarExploreBound) return;
  revisarExploreBound = true;
  byId("review-explore-form")?.addEventListener("submit", (e) => {
    e.preventDefault();
    renderRevisarExploreResults();
  });
  byId("review-filter-reset")?.addEventListener("click", () => {
    if (byId("review-filter-pe-max")) byId("review-filter-pe-max").value = "";
    if (byId("review-filter-margin-min")) byId("review-filter-margin-min").value = "";
    if (byId("review-filter-leverage-max")) byId("review-filter-leverage-max").value = "";
    renderRevisarExploreResults();
  });
}

async function loadRevisarOverview() {
  bindRevisarExploreControls();
  renderRevisarExploreResults();
}
