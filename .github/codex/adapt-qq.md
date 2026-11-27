Adapt only the exact incompatible QQ APK supplied in the candidate artifact.
The trusted controller checks it against the complete accepted-v2 corpus, then
stages at most one available sample for each of up to three selected historical
behavior groups using verified cache entries or the existing download path. The
grouped regression is rerun after adaptation.

When the candidate report contains `runtimeFailure`, inspect its referenced raw
captures under `work/analysis/`. Static matching succeeded, but independent
replay proved the resolved behavior wrong; static success alone cannot close
this task.

Inspect the candidate's real tablet/device-type behavior and caller path before
changing rules. Add a new immutable QQ rule and strategy only when the shared
catalog cannot resolve the behavior. Register it additively in `RuleCatalog`;
do not change existing QQ rule IDs, queries, required targets, strategies, or
their runtime behavior. Keep QQ acceptance records unchanged. Do not recreate deprecated profile or `targets.json` files; those files are absent from the current tree.

Use `acceptance.propose_forward_binding` with the validated acceptance snapshot.
If the candidate is not newer than all accepted records, stop for routing
review. Keep every selected historical behavior group passing the shared checker.
Add focused DEX and behavior tests; preserve all existing regression tests.

Do not create profiles, edit checkers/workflows, force a selected result, change
runtime acceptance, or write acceptance records. The outer controller rechecks
candidate identity, forward routing, regression, module fingerprint, and runtime
evidence. Leave changes uncommitted and summarize the observed APK behavior and
exact changed files in the task adaptation summary.
