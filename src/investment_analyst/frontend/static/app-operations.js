"use strict";

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
    renderKnownAtCut(null);
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
  renderKnownAtCut(latest?.effective_known_at);

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

function renderReviewDetail() {
  reviewEnsureSelection();
  const detail = byId("review-detail");
  const empty = byId("review-detail-empty");
  const selected = reviewSelectedItem();
  detail.replaceChildren();
  if (!selected || !reviewSelection) {
    detail.hidden = true;
    empty.hidden = false;
    empty.textContent = reviewSelectionMissing
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
    const assetLabel = marketAssets[result.asset_id]?.symbol || result.asset_id;
    detail.append(createElement("p", "review-detail-kicker", `${result.rule.name_es} · ${assetLabel}`));
    reviewDetailField(fields, "Estado", REVIEW_STATUS_LABELS[event.status] || event.status);
    reviewDetailField(fields, "Activo", result.asset_id);
    reviewDetailField(fields, "Confirmaciones", formatInteger(event.confirmations));
    reviewDetailField(fields, "Vigencia", formatCalendarDate(event.as_of));
    reviewDetailField(fields, "Espera", formatInstant(event.cooldown_until));
    const conditions = createElement("ul", "candidate-condition-list review-detail-conditions");
    result.conditions.forEach((condition, index) => {
      const definition = result.rule.conditions[index];
      conditions.append(createElement("li", condition.state, formatCandidateCondition(condition, definition)));
    });
    detail.append(fields, createElement("h4", "review-detail-subtitle", "Condiciones evaluadas"), conditions);
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
    detail.append(createElement("p", "review-detail-kicker", event.title));
    reviewDetailField(fields, "Estado", REVIEW_STATUS_LABELS[event.status] || event.status);
    reviewDetailField(fields, "Activo", event.asset_id || "No especificado");
    reviewDetailField(fields, "Proveedor", event.provider);
    reviewDetailField(fields, "Dominio", event.domain);
    reviewDetailField(fields, "Última activación", formatInstant(event.last_activated_at));
    reviewDetailField(fields, "Espera", "No aplica");
    detail.append(fields, createElement("p", "review-detail-message", event.message));
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

function renderAlertInbox(payload) {
  const inbox = byId("alert-inbox");
  reviewAlertItems = Array.isArray(payload.events) ? payload.events : [];
  byId("alert-inbox-summary").textContent = reviewAlertItems.length > 0
    ? `${formatInteger(payload.total ?? reviewAlertItems.length)} registradas · separadas de candidatos`
    : "Sin alertas operativas";
  inbox.replaceChildren();
  if (reviewAlertItems.length === 0) {
    inbox.append(createElement("p", "", "No hay alertas operativas registradas."));
    renderReviewDetail();
    return;
  }
  for (const event of reviewAlertItems) {
    inbox.append(renderReviewMasterRow(
      "alert",
      event.alert_id,
      event.title,
      event.status,
      `${event.asset_id || "Activo no especificado"} · ${formatInstant(event.last_activated_at)}`,
    ));
  }
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
  try {
    renderAlertInbox(await api("/api/alerts?limit=50"));
  } catch (error) {
    reviewAlertItems = [];
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
  card.append(heading, form);
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

function renderCandidateInbox(payload) {
  const inbox = byId("candidate-inbox");
  reviewCandidateItems = Array.isArray(payload.items) ? payload.items : [];
  byId("candidate-inbox-summary").textContent = reviewCandidateItems.length > 0
    ? `${formatInteger(payload.total ?? reviewCandidateItems.length)} registrados · separados de alertas`
    : "Sin candidatos analíticos";
  inbox.replaceChildren();
  if (reviewCandidateItems.length === 0) {
    inbox.append(createElement("p", "", "No hay candidatos analíticos registrados."));
    renderReviewDetail();
    return;
  }
  for (const itemPayload of reviewCandidateItems) {
    const { event, result } = itemPayload;
    const assetLabel = marketAssets[result.asset_id]?.symbol || result.asset_id;
    inbox.append(renderReviewMasterRow(
      "candidate",
      event.candidate_id,
      `${result.rule.name_es} · ${assetLabel}`,
      event.status,
      `${formatCalendarDate(event.as_of)} · ${formatInteger(event.confirmations)} confirmación${event.confirmations === 1 ? "" : "es"}`,
    ));
  }
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
  try {
    renderCandidateInbox(await api("/api/candidates?limit=50"));
  } catch (error) {
    reviewCandidateItems = [];
    inbox.replaceChildren(
      createElement("p", "", `No se pudo consultar la bandeja: ${error.message}`),
    );
    renderReviewDetail();
  } finally {
    inbox.setAttribute("aria-busy", "false");
  }
}

function renderCandidateNotifications(payload) {
  const inbox = byId("candidate-notifications");
  inbox.replaceChildren();
  if (!Array.isArray(payload.items) || payload.items.length === 0) {
    inbox.append(createElement("p", "", "No hay notificaciones locales pendientes."));
    return;
  }
  for (const view of payload.items) {
    const notification = view.item;
    const item = createElement("article", "alert-inbox-item candidate-inbox-item");
    item.append(
      createElement("strong", "", `${notification.rule_id} · ${notification.asset_id}`),
      createElement("p", "", `Candidato local ${notification.candidate_id}`),
      createElement("time", "", formatInstant(notification.created_at)),
    );
    item.append(
      createElement(
        "span",
        `alert-inbox-status ${view.status}`,
        view.status === "acknowledged" ? "Entrega confirmada" : "Pendiente",
      ),
    );
    if (view.status === "acknowledged") {
      inbox.append(item);
      continue;
    }
    const button = createElement("button", "alert-action-button", "Confirmar entrega");
    button.type = "button";
    button.addEventListener("click", () => acknowledgeCandidateNotification(notification.notification_id, button));
    const actions = createElement("div", "alert-inbox-actions");
    actions.append(button);
    item.append(actions);
    inbox.append(item);
  }
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
