# MuseRoute: judge questions and honest answers

All figures below are from `results/facts.json`. Holdout accuracy is exploratory because the checker was corrected after inspecting saved holdout answers.

1. **What does MuseRoute do?**  
   It runs a cheap-first Muse routing policy and records its decision, answer, and available usage data. The saved evaluation compares that route with each tier alone; it does not establish that the chosen tier is better for every prompt.

2. **What was the observed holdout result?**  
   The post-hoc facts report 75.56% accuracy for MuseRoute and 66.67% for both cheap-only and strong-only, with 20% routed escalation. Treat that comparison as exploratory rather than a confirmatory performance claim.

3. **How large is the holdout?**  
   There are 45 held-out prompts, 15 per band. 45 prompts: one prompt is about 2.2 points, so small gaps are noise; performance beyond this workload is SAY: not measured.

4. **Did the router save tokens or money?**  
   The complete MuseRoute token total and all cost units are null in the facts, so SAY: not measured. The saved escalation rate is not itself a cost measurement.

5. **What does the tier audit show?**  
   Cheap-only averaged 249.93 tokens per request at 66.67% accuracy; strong-only averaged 537.33 tokens at the same 66.67% accuracy. Average latency for both tiers is NOT MEASURED in the saved facts.

6. **What exact models were evaluated?**  
   The facts list `meta/muse-glimmer-30b` and host `integrate.api.nvidia.com` for both tiers. The facts do not establish two distinct model IDs or explain a causal effect for tier settings; SAY: not measured.

7. **How did accuracy vary by band?**  
   Cheap-only scored 100% easy, 80% medium, and 20% hard; strong-only scored 100%, 73.33%, and 26.67%. MuseRoute scored 60% easy, 73.33% medium, and 93.33% hard in this exploratory rescore.

8. **Why were some route results missing?**  
   The facts show six holdout prompts without a final routed answer and 15 tier-call errors. Saved failure records include request timeouts; the facts do not establish one cause for every failed call.

9. **How does it compare with the named routing products?**  
   The repo lists OpenRouter Auto Router, Not Diamond, LiteLLM Router, and RouteLLM, but the supplied factual descriptions are missing and `facts.json` contains no comparative measurements. SAY: comparative behavior not measured.

10. **Is this production-ready, and are the savings numbers reliable?**  
    Production reliability, cost, retry counts, retry sleeps, and isolated model time are NOT MEASURED in these cached results. Six unanswered routed prompts remain in the facts, so this evaluation does not support a production-readiness or savings claim.
