(() => {
  const demoMode = new URLSearchParams(window.location.search).get("demo") === "1";
  const el = (id) => document.getElementById(id);
  const input = el("prompt-input");
  const chatForm = el("chat-form");
  const routeButton = el("route-button");
  const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  })[char]);
  let selectedPromptId = null;

  el("mode-note").textContent = demoMode
    ? "Offline demo mode: saved examples replay cached routing choices without model calls. To route your own prompt live, open this page without ?demo=1."
    : "Live routing: enter any prompt to see which configured Muse tier the policy selects. No generated answer is shown.";

  const requestJson = async (url, options = {}) => {
    let response;
    try {
      response = await fetch(url, options);
    } catch (error) {
      throw new Error(`Could not reach MuseRoute: ${error.message}`);
    }
    let body;
    try { body = await response.json(); } catch { body = {}; }
    if (!response.ok) {
      const detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail ?? body);
      throw new Error(detail || `HTTP ${response.status}`);
    }
    return body;
  };

  const presetButton = (prompt, number) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "preset-button";
    button.textContent = `${number}. ${prompt.prompt.slice(0, 38)}${prompt.prompt.length > 38 ? "…" : ""}`;
    button.title = prompt.prompt;
    button.setAttribute("aria-pressed", "false");
    button.addEventListener("click", () => {
      selectedPromptId = prompt.id;
      input.value = prompt.prompt;
      document.querySelectorAll(".preset-button").forEach((item) => item.setAttribute("aria-pressed", "false"));
      button.setAttribute("aria-pressed", "true");
      el("request-error").textContent = "";
    });
    return button;
  };

  const loadPresets = async () => {
    const query = demoMode ? "?demo=1" : "";
    const response = await requestJson(`/api/demo-prompts${query}`);
    const groups = el("preset-groups");
    groups.replaceChildren();
    for (const band of ["easy", "medium", "hard"]) {
      const group = document.createElement("div");
      group.className = "preset-group";
      const title = document.createElement("p");
      title.className = "preset-title";
      title.textContent = band;
      group.appendChild(title);
      response.prompts.filter((prompt) => prompt.band === band).slice(0, 2).forEach((prompt, index) => {
        group.appendChild(presetButton(prompt, index + 1));
      });
      groups.appendChild(group);
    }
    el("cache-status").textContent = `${response.prompts.length} cached examples`;
  };

  const renderRecommendation = (result) => {
    const selectedTier = result.selected_tier;
    const params = result.extra_params && typeof result.extra_params === "object" ? result.extra_params : null;
    const settingText = params && Object.keys(params).length
      ? Object.entries(params).map(([key, value]) => `${key}: ${JSON.stringify(value)}`).join(" · ")
      : "Tier settings: NOT MEASURED";
    const calls = Array.isArray(result.calls) ? result.calls : [];
    el("recommendation").hidden = false;
    el("selected-model").textContent = selectedTier
      ? `${selectedTier} tier · ${result.model_id || "model_id: NOT MEASURED"}`
      : "No tier selected";
    el("selected-mode").textContent = settingText;
    el("recommendation-reason").textContent = result.reason || "NOT MEASURED";
    el("recommendation-path").textContent = calls.length
      ? calls.map((call) => `${call.tier} (${call.status})`).join(" → ")
      : "NOT MEASURED";
  };

  const runRoute = async () => {
    el("request-error").textContent = "";
    el("recommendation").hidden = true;
    const prompt = input.value.trim();
    if (!prompt) return;
    const promptId = selectedPromptId;
    routeButton.disabled = true;
    routeButton.textContent = "Checking…";
    try {
      const query = demoMode ? "?demo=1" : "";
      const body = { messages: [{ role: "user", content: prompt }] };
      if (demoMode) body.prompt_id = promptId;
      const result = await requestJson(`/api/recommend${query}`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body)
      });
      renderRecommendation(result);
    } catch (error) {
      el("recommendation").hidden = false;
      el("selected-model").textContent = "Could not select a tier";
      el("selected-mode").textContent = "";
      el("recommendation-reason").textContent = error.message;
      el("recommendation-path").textContent = "NOT MEASURED";
      el("request-error").textContent = demoMode
        ? "No saved route for this prompt. Choose one of the cached examples below."
        : "Routing request failed. See the reason above.";
    } finally {
      input.value = "";
      selectedPromptId = null;
      document.querySelectorAll(".preset-button").forEach((item) => item.setAttribute("aria-pressed", "false"));
      routeButton.disabled = false;
      routeButton.textContent = "Find best tier";
      input.focus();
    }
  };

  const showMeasured = (value) => value === null || value === undefined ? "NOT MEASURED" : escapeHtml(value);
  const showPercent = (value) => typeof value === "number" ? `${(value * 100).toFixed(2)}%` : "NOT MEASURED";
  let proofFacts = null;
  let calculatorTimer = null;
  let calculatorRequest = 0;

  const renderProof = (facts) => {
    const overall = facts.holdout?.overall || {};
    const systems = [
      ["Cheap-only", overall.cheap_only],
      ["Strong-only", overall.strong_only],
      ["MuseRoute", overall.museroute],
    ];
    el("proof-holdout-body").innerHTML = systems.map(([name, row]) => `
      <tr><th scope="row">${name}</th><td>${showPercent(row?.accuracy)}</td><td>${showPercent(row?.escalation_rate)}</td><td>${showMeasured(row?.total_tokens)}</td><td>${showMeasured(row?.cost_units)}</td></tr>`).join("");
    const bands = facts.holdout?.by_band || {};
    el("proof-bands-body").innerHTML = ["easy", "medium", "hard"].map((band) => {
      const row = bands[band] || {};
      return `<tr><th scope="row">${escapeHtml(band)}</th><td>${showPercent(row.cheap_only?.accuracy)}</td><td>${showPercent(row.strong_only?.accuracy)}</td><td>${showPercent(row.museroute?.accuracy)}</td></tr>`;
    }).join("");

    const routed = overall.museroute?.accuracy;
    const strong = overall.strong_only?.accuracy;
    const guard = el("accuracy-guard-status");
    const qualityPill = el("calculator-quality");
    if (typeof routed !== "number" || typeof strong !== "number") {
      guard.className = "guard-status";
      guard.textContent = "Accuracy guard: NOT MEASURED; holdout accuracy is missing.";
      qualityPill.className = "pill pill-muted";
      qualityPill.textContent = "Quality guard: NOT MEASURED";
    } else {
      const gap = (strong - routed) * 100;
      const passed = routed >= strong - 0.02;
      guard.className = `guard-status ${passed ? "guard-pass" : "guard-fail"}`;
      guard.textContent = passed
        ? `Accuracy guard: PASS — MuseRoute is within 2 percentage points of strong-only (gap ${gap.toFixed(2)} pp).`
        : `Accuracy guard: FAIL — MuseRoute is ${gap.toFixed(2)} percentage points below strong-only; required maximum drop is 2 pp.`;
      qualityPill.className = `pill ${passed ? "" : "pill-error"}`;
      qualityPill.textContent = `Quality guard: ${passed ? "PASS" : "FAIL"}`;
    }

    const mistakes = Array.isArray(facts.worst_holdout_mistakes) ? facts.worst_holdout_mistakes : [];
    el("worst-failures").innerHTML = mistakes.length ? mistakes.slice(0, 5).map((mistake) => `
      <li><strong>${escapeHtml(mistake.id)} · ${escapeHtml(mistake.band)}</strong>
        <span class="failure-reason">${escapeHtml(mistake.reason || "Reason: NOT MEASURED")}</span>
        <span class="failure-reason">Answer: ${escapeHtml(mistake.answer || "NOT MEASURED")} · Expected: ${escapeHtml(mistake.gold ?? "NOT MEASURED")}</span>
      </li>`).join("") : "<li>NOT MEASURED</li>";
    const requestFailures = Array.isArray(facts.holdout_request_failures) ? facts.holdout_request_failures : [];
    el("request-failures-list").innerHTML = requestFailures.length
      ? requestFailures.map((failure) => `<li><strong>${escapeHtml(failure.id)}:</strong> ${escapeHtml(failure.reason || "NOT MEASURED")}</li>`).join("")
      : "<li>NOT MEASURED</li>";
    el("proof-status").textContent = `${showMeasured(facts.holdout?.overall?.prompt_count)} holdout prompts · facts.json`;
    el("calc-escalation").textContent = showPercent(overall.museroute?.escalation_rate);
  };

  const loadProof = async () => {
    if (proofFacts) return proofFacts;
    el("proof-status").textContent = "Loading saved facts…";
    try {
      proofFacts = await requestJson("/api/facts");
      renderProof(proofFacts);
      scheduleCalculation();
      return proofFacts;
    } catch (error) {
      el("proof-status").className = "pill pill-error";
      el("proof-status").textContent = `Could not load facts: ${error.message}`;
      return null;
    }
  };

  const numericInput = (formData, name) => {
    const raw = formData.get(name);
    if (raw === null || raw === "") return null;
    const value = Number(raw);
    return Number.isFinite(value) ? value : null;
  };

  const scheduleCalculation = () => {
    clearTimeout(calculatorTimer);
    calculatorTimer = setTimeout(runCalculation, 140);
  };

  const runCalculation = async () => {
    if (!proofFacts) return;
    const requestId = ++calculatorRequest;
    const form = new FormData(el("calculator-form"));
    const payload = {};
    for (const name of [
      "monthly_requests", "average_prompt_tokens", "average_completion_tokens",
      "cheap_input_price_per_million", "cheap_output_price_per_million",
      "strong_input_price_per_million", "strong_output_price_per_million",
    ]) payload[name] = numericInput(form, name);
    try {
      const result = await requestJson("/api/calculate", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload)
      });
      if (requestId !== calculatorRequest) return;
      const formatProjection = (value) => typeof value === "number" ? value.toLocaleString(undefined, { maximumFractionDigits: 6 }) : "NOT MEASURED";
      el("calc-strong-cost").textContent = formatProjection(result.strong_only_monthly_cost);
      el("calc-route-cost").textContent = formatProjection(result.museroute_monthly_cost);
      el("calc-saving").textContent = formatProjection(result.projected_savings);
      if (result.status === "quality_drop_too_large") {
        el("calculator-message").textContent = "Quality drop too large. No saving is shown.";
      } else if (result.status === "quality_not_measured") {
        el("calculator-message").textContent = "Accuracy is NOT MEASURED; no saving is shown.";
      } else if (result.status === "inputs_not_measured") {
        el("calculator-message").textContent = "Enter all request, token, and price inputs; projection is NOT MEASURED until then.";
      } else {
        el("calculator-message").textContent = `Projected saving: ${formatProjection(result.projected_savings)} (projection only).`;
      }
    } catch (error) {
      if (requestId === calculatorRequest) el("calculator-message").textContent = `Calculator error: ${error.message}`;
    }
  };

  const activateTab = (tabName) => {
    const routeSelected = tabName === "route";
    el("route-panel").hidden = !routeSelected;
    el("proof-panel").hidden = routeSelected;
    el("route-tab").setAttribute("aria-selected", String(routeSelected));
    el("proof-tab").setAttribute("aria-selected", String(!routeSelected));
    if (!routeSelected) loadProof();
  };

  el("route-tab").addEventListener("click", () => activateTab("route"));
  el("proof-tab").addEventListener("click", () => activateTab("proof"));
  el("calculator-form").addEventListener("input", scheduleCalculation);

  chatForm.addEventListener("submit", (event) => {
    event.preventDefault();
    runRoute();
  });
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      chatForm.requestSubmit();
    }
  });
  input.addEventListener("input", () => {
    const selected = document.querySelector('.preset-button[aria-pressed="true"]');
    if (selected && input.value !== selected.title) {
      selected.setAttribute("aria-pressed", "false");
      selectedPromptId = null;
    }
  });
  loadPresets().catch((error) => {
    el("cache-status").className = "pill pill-error";
    el("cache-status").textContent = "Saved examples unavailable";
    el("request-error").textContent = `Could not load cached examples: ${error.message}`;
  });
})();
