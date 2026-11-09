# Successful candidate acceptance and reuse

Scheduled official-candidate checks must register file-backed successful runtime evidence even when no adaptation is needed. Registration commits only the v2 acceptance table and verified evidence to main, using a normal fast-forward push and refusing a changed main head. Adaptation continues recording on its PR branch. Manual verification remains a full run.

Reuse occurs after downloading and statically checking the exact candidate and running fixed regression: require identical APK inputs, complete plan, recognition and execution identity, validated archived evidence, and the same runtime validation implementation. A missing or stale record runs the full build and runtime path. Reuse is reported explicitly, never fabricated as a fresh runtime run.

Use existing immutable semantic acceptance records rather than a URL-only cache or CI artifact history. Runtime implementation identity is stored in the archived runtime report and checked against a deterministic hash of CI helpers, the runtime workflow and its DexKit preparation action. Acceptance-only commits must not invalidate it. Different execution identities for one APK can have distinct immutable records; exact duplicate validations remain rejected.

Implement in the clean personal repository checkout. Keep direct-main registration limited to trusted owner runs on main: scheduled runs or manual official-candidate adaptation runs, with no supplied APK/module artifacts. Manual verify runs never register. Do not force-push main or weaken existing runtime verification.
