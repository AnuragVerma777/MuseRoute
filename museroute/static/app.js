(() => {
  const el = (id) => document.getElementById(id);
  const form = el("recommend-form");
  const input = el("prompt-input");
  const button = el("recommend-button");

  el("mode-note").textContent = "Automatic text-cue routing · no model calls";

  const requestJson = async (url, options) => {
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
  };

  const shortReason = (result) => {
    const reason = String(result.reason || "");
    if (result.selection_method === "text_cue_heuristic") return reason || "Routing reason was not measured.";
    if (!result.selected_tier) {
      if (/WinError 10013|Failed to establish a new connection|ConnectionError|HTTPSConnectionPool/i.test(reason)) {
        return "The configured Muse host could not be reached, so MuseRoute could not select a tier.";
      }
      if (/timed out|ReadTimeout/i.test(reason)) {
        return "The Muse host timed out, so MuseRoute could not select a tier.";
      }
      return "Neither Muse tier returned a usable routing result.";
    }
    if (reason.includes("strong_mode_selected_by_user")) {
      return "You selected strong mode directly; no model call was made.";
    }
    if (reason.includes("strong_call_error_fallback_to_cheap")) {
      return "The strong-tier check failed, so MuseRoute fell back to the cheap tier.";
    }
    if (reason.includes("cheap_call_error") && result.selected_tier === "strong") {
      return "The cheap-tier check failed, so MuseRoute tried the strong tier.";
    }
    if (reason.includes("cheap_format_check_failed")) {
      return "The cheap-tier result failed a format check, so MuseRoute escalated.";
    }
    if (reason.includes("cheap_self_consistency_disagreement")) {
      return "Two cheap-tier samples disagreed, so MuseRoute escalated.";
    }
    if (reason.includes("cheap_confidence_below_threshold")) {
      return "The cheap-tier confidence was below the routing threshold, so MuseRoute escalated.";
    }
    if (reason.includes("cheap_answer_accepted")) {
      return "The cheap tier met the routing policy checks.";
    }
    return reason || "Routing reason was not measured.";
  };

  const showRecommendation = (result) => {
    const selected = result.selected_tier;
    const params = result.extra_params;
    el("recommendation").hidden = false;
    el("recommendation").classList.toggle("result-error", !selected);
    el("prompt-assessment").textContent = result.prompt_class
      ? `Prompt assessment: ${result.prompt_class}`
      : "Prompt assessment: NOT MEASURED";
    el("selected-model").textContent = selected
      ? `${selected} tier · ${result.model_id || "model ID NOT MEASURED"}`
      : "No tier selected";
    el("selected-mode").textContent = params && Object.keys(params).length
      ? Object.entries(params).map(([key, value]) => `${key}: ${JSON.stringify(value)}`).join(" · ")
      : "Tier settings: NOT MEASURED";
    el("recommendation-reason").textContent = shortReason(result);
    el("recommendation-path").textContent = result.selection_method || "NOT MEASURED";
    el("diagnostic-text").textContent = JSON.stringify({
      prompt_class: result.prompt_class,
      selection_method: result.selection_method,
      reason: result.reason,
    }, null, 2);
    el("diagnostic-details").hidden = false;
  };

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const prompt = input.value.trim();
    if (!prompt) return;
    el("request-error").hidden = true;
    el("recommendation").hidden = true;
    button.disabled = true;
    button.textContent = "Routing…";
    const body = { messages: [{ role: "user", content: prompt }] };
    try {
      const result = await requestJson("/api/recommend", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      showRecommendation(result);
    } catch (error) {
      el("recommendation").hidden = false;
      el("recommendation").classList.add("result-error");
      el("prompt-assessment").textContent = "Prompt assessment: NOT MEASURED";
      el("selected-model").textContent = "No tier selected";
      el("selected-mode").textContent = "";
      el("recommendation-reason").textContent = "MuseRoute could not complete the routing check.";
      el("recommendation-path").textContent = "NOT MEASURED";
      el("diagnostic-text").textContent = error.message;
      el("diagnostic-details").hidden = false;
      el("request-error").textContent = "Check that the MuseRoute server is reachable.";
      el("request-error").hidden = false;
    } finally {
      button.disabled = false;
      button.innerHTML = 'Route prompt <span aria-hidden="true">↑</span>';
    }
  });

  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      form.requestSubmit();
    }
  });
})();
