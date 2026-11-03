# MuseRoute: judge questions and honest answers

1. **What problem does MuseRoute solve?**  
   It tests whether a cheap-first, confidence-gated route can reduce work while preserving answer quality. On the saved 45-prompt holdout, MuseRoute scored 48.89%, below cheap-only at 60.00% and strong-only at 57.78%, so this evaluation does not show that it achieved that goal.

2. **What exact models and hosts were evaluated?**  
   The facts record `meta/muse-glimmer-30b` for both cheap and strong tiers, hosted at `integrate.api.nvidia.com`. They are different tier settings of the same model ID, not two distinct models.

3. **How large and representative is the evaluation?**  
   The workload contains 90 prompts, split into 45 tune and 45 holdout prompts, with 15 holdout prompts in each band. Generalization beyond this workload is SAY: not measured.

4. **Did routing improve accuracy?**  
   No, not on the recorded holdout: MuseRoute reached 48.89%, versus 60.00% cheap-only and 57.78% strong-only. Its band scores were 60% easy, 73.33% medium, and 13.33% hard.

5. **How much money or compute did it save?**  
   Cost units are null for all three holdout systems, and MuseRoute total tokens are also null. SAY: not measured; the 20% escalation rate is not a cost measurement.

6. **Why is MuseRoute latency so high?**  
   The saved average routed latency is 452,626.58 ms, compared with 12,947.69 ms cheap-only and 26,021.18 ms strong-only. The facts artifact does not establish a cause for that latency; SAY: not measured.

7. **Why use the same model for both tiers?**  
   The facts list the same `meta/muse-glimmer-30b` model ID and host for both tiers. The facts do not specify which tier parameter caused any behavioral difference; SAY: not measured.

8. **What happens when a request fails?**  
   Six routed holdout prompts have no returned answer, and the facts record 15 tier-call errors across requests and fallbacks. The artifact does not identify a single root cause for all errors; SAY: not measured.

9. **How is MuseRoute different from OpenRouter Auto Router, Not Diamond, LiteLLM Router, or RouteLLM?**  
   The project enforces a Muse-only model policy and includes a format/confidence guard plus a held-out evaluation. The four source descriptions were not included in the brief, and this project has no comparative benchmark against them; a claim of superiority would be unsupported.

10. **Does it require a payment card, and is it ready for production?**  
    Card requirements and production reliability are absent from `results/facts.json`; SAY: not measured. The saved results include six unanswered routed prompts and worse overall holdout accuracy than either single tier, so the evidence does not support a production-readiness claim.
