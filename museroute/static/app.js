(() => {
  const el = (id) => document.getElementById(id);
  const form = el("route-form");
  const input = el("prompt-input");
  const routeButton = el("route-button");
  const state = { lastPrompt: null, factsLoaded: false };

  el("mode-note").textContent = "Enter any prompt · cached answers are reused when available; otherwise configured Muse tiers are called";

  async function requestJson(url, options = {}) {
    let response;
    try {
      response = await fetch(url, options);
    } catch (error) {
      throw new Error(`Could not reach MuseRoute service: ${error.message}`);
    }
    let body;
    try { body = await response.json(); } catch { body = {}; }
    if (!response.ok) {
      const detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail ?? body);
      throw new Error(detail || `HTTP ${response.status}`);
    }
    return body;
  }

  const jsonPost = (url, body) => requestJson(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

  function measured(value, formatter = (number) => String(number), legacyValue = null) {
    if (legacyValue !== null && legacyValue !== undefined) return "NOT MEASURED";
    if (value === null || value === undefined || typeof value !== "number" || !Number.isFinite(value)) return "NOT MEASURED";
    return formatter(value);
  }

  const percent = (value) => measured(value, (number) => `${(number * 100).toFixed(1)}%`);
  const tokens = (value) => measured(value, (number) => Math.round(number).toLocaleString());
  const latency = (value, call) => measured(value, (number) => `${Math.round(number).toLocaleString()} ms`, call?.legacy_latency_ms);

  function addText(parent, tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    node.textContent = text ?? "";
    parent.append(node);
    return node;
  }

  function addMetricLine(parent, values) {
    const line = document.createElement("p");
    line.className = "metric-line";
    line.textContent = values.join(" · ");
    parent.append(line);
  }

  function renderCall(call, index) {
    const section = document.createElement("article");
    section.className = `timeline-step ${call.status === "error" ? "step-error" : ""}`;
    const header = document.createElement("div");
    header.className = "step-heading";
    addText(header, "h3", "", `${call.tier || "Tier"} answer`);
    if (call.cached === true) addText(header, "span", "cached-badge", "cached");
    section.append(header);
    if (call.status === "error") {
      addText(section, "p", "error-message", call.error || "Tier call failed; exact error was not saved.");
    } else {
      addText(section, "p", "answer-text", call.answer || "Empty answer");
      const metrics = [`Confidence: ${percent(call.confidence)}`];
      if (typeof call.latency_ms === "number" && call.legacy_latency_ms == null) metrics.push(`Latency: ${latency(call.latency_ms, call)}`);
      metrics.push(`Prompt tokens: ${tokens(call.prompt_tokens)}`, `Completion tokens: ${tokens(call.completion_tokens)}`);
      addMetricLine(section, metrics);
    }
    if (Number.isInteger(call.retry_count) || Number.isFinite(call.retry_sleep_ms)) {
      addMetricLine(section, [
        `Retries: ${measured(call.retry_count)}`,
        `Retry sleep: ${measured(call.retry_sleep_ms, (value) => `${Math.round(value)} ms`)}`,
      ]);
    }
    return section;
  }

  function renderTimeline(result) {
    const trace = result.route_trace || {};
    const calls = Array.isArray(trace.calls) ? trace.calls : [];
    const steps = el("timeline-steps");
    steps.replaceChildren();
    const cheap = calls.find((call) => call.tier === "cheap");
    if (cheap) steps.append(renderCall(cheap, 0));

    const decision = document.createElement("article");
    decision.className = "timeline-step decision-step";
    const escalated = trace.escalated === true || trace.decision === "escalate";
    addText(decision, "div", "decision-label", escalated ? "Decision · escalate" : "Decision · stay on cheap");
    addText(decision, "p", "", trace.reason || "Routing reason: NOT MEASURED");
    steps.append(decision);

    const strong = calls.find((call) => call.tier === "strong");
    if (strong) steps.append(renderCall(strong, 1));
    else {
      const skipped = document.createElement("article");
      skipped.className = "timeline-step skipped-step";
      addText(skipped, "h3", "", "Strong answer");
      addText(skipped, "p", "muted", "Not called for this request.");
      steps.append(skipped);
    }

    el("final-tier").textContent = trace.tier_used ? `${trace.tier_used} tier` : "No tier returned an answer";
    el("final-text").textContent = result.answer || "No tier returned an answer.";
    el("final-tokens").textContent = tokens(trace.total_tokens);
    el("timeline").hidden = false;
    el("compare-result").hidden = true;
    el("compare-result").replaceChildren();
  }

  async function runRoute() {
    const prompt = input.value.trim();
    if (!prompt) return;
    state.lastPrompt = prompt;
    el("request-error").hidden = true;
    el("timeline").hidden = true;
    routeButton.disabled = true;
    routeButton.textContent = "Routing…";
    const payload = { messages: [{ role: "user", content: prompt }] };
    try {
      const result = await jsonPost("/api/route", payload);
      renderTimeline(result);
    } catch (error) {
      el("request-error").textContent = `MuseRoute request failed: ${error.message}`;
      el("request-error").hidden = false;
    } finally {
      routeButton.disabled = false;
      routeButton.innerHTML = 'Route prompt <span aria-hidden="true">↑</span>';
    }
  }

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    runRoute();
  });
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      form.requestSubmit();
    }
  });

  function addCompareCard(container, title, answer, stats, trace = null) {
    const card = document.createElement("article");
    card.className = "compare-card";
    addText(card, "h3", "", title);
    addText(card, "p", "answer-text", answer || "No answer returned.");
    let totalTokens = stats?.total_tokens;
    if (totalTokens === undefined && Number.isInteger(stats?.prompt_tokens) && Number.isInteger(stats?.completion_tokens)) {
      totalTokens = stats.prompt_tokens + stats.completion_tokens;
    }
    if (trace && totalTokens === undefined) totalTokens = trace.total_tokens;
    const timeValue = trace ? trace.total_latency_ms : (stats?.request_time_ms ?? stats?.latency_ms);
    const metrics = [`Tokens: ${tokens(totalTokens)}`];
    if (typeof timeValue === "number" && !(trace || stats)?.legacy_latency_ms) metrics.push(`Latency: ${latency(timeValue, trace || stats)}`);
    metrics.push(`Cost units: ${measured(trace ? trace.total_cost_units : stats?.cost_units)}`);
    addMetricLine(card, metrics);
    if (stats?.cached === true || trace?.cached === true) addText(card, "span", "cached-badge", "cached");
    container.append(card);
  }

  el("compare-button").addEventListener("click", async () => {
    if (!state.lastPrompt) return;
    const button = el("compare-button");
    button.disabled = true;
    button.textContent = "Comparing…";
    const output = el("compare-result");
    output.replaceChildren();
    try {
      const payload = { messages: [{ role: "user", content: state.lastPrompt }] };
      const result = await jsonPost("/api/compare", payload);
      addCompareCard(output, "Before · strong-only", result.strong_only?.answer, result.strong_only?.stats);
      addCompareCard(output, "After · MuseRoute", result.museroute?.answer, null, result.museroute?.route_trace);
      output.hidden = false;
    } catch (error) {
      addText(output, "p", "error-message", `Compare failed: ${error.message}`);
      output.hidden = false;
    } finally {
      button.disabled = false;
      button.textContent = "Compare strong-only vs MuseRoute";
    }
  });

  function addTableCell(row, value, className = "") {
    const cell = document.createElement("td");
    if (className) cell.className = className;
    cell.textContent = value;
    row.append(cell);
  }

  function renderProof(facts) {
    const overall = facts?.holdout?.overall || {};
    const tableBody = el("holdout-table").querySelector("tbody");
    tableBody.replaceChildren();
    const rows = [["Overall", overall]];
    for (const [band, values] of Object.entries(facts?.holdout?.by_band || {})) rows.push([band, values]);
    for (const [label, systems] of rows) {
      for (const name of ["cheap_only", "strong_only", "museroute"]) {
        const metrics = systems[name] || {};
        const row = document.createElement("tr");
        addTableCell(row, label);
        addTableCell(row, name.replaceAll("_", " "));
        addTableCell(row, percent(metrics.accuracy));
        addTableCell(row, percent(metrics.escalation_rate));
        addTableCell(row, tokens(metrics.average_total_tokens));
        addTableCell(row, latency(metrics.latency_ms));
        addTableCell(row, measured(metrics.cost_units));
        tableBody.append(row);
      }
    }

    const bandBody = el("band-table").querySelector("tbody");
    bandBody.replaceChildren();
    for (const [band, systems] of Object.entries(facts?.holdout?.by_band || {})) {
      const row = document.createElement("tr");
      addTableCell(row, band);
      addTableCell(row, percent(systems.cheap_only?.accuracy));
      addTableCell(row, percent(systems.strong_only?.accuracy));
      addTableCell(row, percent(systems.museroute?.accuracy));
      bandBody.append(row);
    }

    const routedAccuracy = overall.museroute?.accuracy;
    const strongAccuracy = overall.strong_only?.accuracy;
    const guard = el("guard-status");
    if (typeof routedAccuracy !== "number" || typeof strongAccuracy !== "number") {
      guard.textContent = "NOT MEASURED";
      guard.className = "guard-unknown";
    } else {
      const passed = routedAccuracy >= strongAccuracy - 0.02;
      guard.textContent = `${passed ? "PASS" : "FAIL"} · post-hoc, exploratory`;
      guard.className = passed ? "guard-pass" : "guard-fail";
    }

    const failures = el("failure-list");
    failures.replaceChildren();
    const mistakes = facts?.worst_holdout_mistakes || [];
    for (const item of mistakes.slice(0, 5)) {
      const li = document.createElement("li");
      addText(li, "strong", "", `${item.id || "Prompt"} · ${item.band || "band NOT MEASURED"}`);
      addText(li, "p", "", `Answer: ${item.answer || "No answer returned"}`);
      addText(li, "p", "muted", `Expected: ${typeof item.gold === "object" ? JSON.stringify(item.gold) : item.gold ?? "NOT MEASURED"} · ${item.reason || "Reason NOT MEASURED"}`);
      failures.append(li);
    }
    if (!mistakes.length) addText(failures, "li", "muted", "No failure cases are recorded.");
  }

  async function loadProof() {
    if (state.factsLoaded) return;
    el("proof-error").hidden = true;
    try {
      renderProof(await requestJson("/api/facts"));
      state.factsLoaded = true;
    } catch (error) {
      el("proof-error").textContent = `Could not load saved facts: ${error.message}`;
      el("proof-error").hidden = false;
    }
  }

  document.querySelectorAll("[data-tab]").forEach((button) => {
    button.addEventListener("click", () => {
      const target = button.dataset.tab;
      document.querySelectorAll("[data-tab]").forEach((tab) => tab.classList.toggle("active", tab === button));
      document.querySelectorAll(".view").forEach((view) => {
        view.hidden = view.id !== `${target}-view`;
        view.classList.toggle("active-view", !view.hidden);
      });
      if (target === "proof") loadProof();
    });
  });

  el("calculator-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const formData = new FormData(event.currentTarget);
    const payload = {};
    for (const [key, value] of formData.entries()) payload[key] = value.trim() === "" ? null : Number(value);
    el("calculator-error").hidden = true;
    const output = el("calculator-result");
    output.replaceChildren();
    try {
      const result = await jsonPost("/api/calculate", payload);
      addText(output, "h2", "", result.status === "quality_drop_too_large" ? "Quality drop too large" : result.status.replaceAll("_", " "));
      addText(output, "p", "", `Accuracy guard: ${result.quality_gate_pass === null ? "NOT MEASURED" : result.quality_gate_pass ? "PASS" : "FAIL"}`);
      addText(output, "p", "", `Strong-only projected cost: ${measured(result.strong_only_monthly_cost)}`);
      addText(output, "p", "", `MuseRoute projected cost: ${measured(result.museroute_monthly_cost)}`);
      addText(output, "p", "", `Projected savings: ${measured(result.projected_savings)}`);
      output.hidden = false;
    } catch (error) {
      el("calculator-error").textContent = `Calculation failed: ${error.message}`;
      el("calculator-error").hidden = false;
    }
  });

})();
