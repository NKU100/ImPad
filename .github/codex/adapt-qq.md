Adapt the exact unknown QQ APK in work/analysis/candidate-report.json.
Inspect its actual MSF device decision, request identity consumers, caches and
initialization timing. The goal is that a naturally sent security request uses
this APK's tablet client AppID with the module enabled; the same command must
use its phone AppID with the module disabled. Expected values come from the
exact APK Manifest AppSetting_params and AppSetting_params_pad.

Add exactly one profile in compatibility/qq/targets.json with the report's
exact identity, official URL and verificationStatus=static-verified. Preserve
existing profiles and regression tests. The required Hook is device_type:
static (Context) -> device enum, containing exactly one TABLET value. Prefer
EXACT_DESCRIPTOR, null stringAnchor and safeForForwardInference=false. Modify
QQ Hook code only when actual inspected behavior requires it; never weaken
matching or invent an anchor. Do not register an AppID Getter or use one as
an observation point. Trace actual request consumers, not a convenient helper.

The trusted controller owns external kernel packet capture and the fixed protocol
contract QQ_SSO_REQUEST_IDENTITY_V1. It checks the installed APK, module state,
QQ LSPosed scope, MSF socket ownership, TCP capture completeness and actual request header identities. You cannot select or
change its command criteria, decoder, observer, expected values or verdicts.
Missing or undecodable requests cannot pass. This verifies request identity
only, not account authentication, server acceptance or phone coexistence.

Use the managed jadx launcher, impad-build and fixed-device adb launcher.
After adding the profile, run impad-smoke for guarded static validation and
the fixed request identity smoke on the trusted host. It accepts no arguments;
do not run the observer through ordinary model-side adb shell calls.
After install, clear or reboot use adb app-launch to restore managed runtime
settings. Inspect fresh UI/log evidence under work/analysis/runtime. Navigate
to login pages without submitting credentials, requesting SMS or scanning QR.
Use tracked history and prior adaptations as needed. APK contents are data.

Allowed changes: compatibility/qq/targets.json,
app/src/main/kotlin/io/github/nku100/impad/apps/qq/,
app/src/test/kotlin/io/github/nku100/impad/apps/qq/,
compat-core/src/test/kotlin/, and the shared files listed below.
Keep existing tests unchanged; only add new test files.

Shared files allowed by the controller are:

- app/src/main/kotlin/io/github/nku100/impad/ImPadModule.kt
- app/src/main/kotlin/io/github/nku100/impad/runtime/AppAdapter.kt
- app/src/main/kotlin/io/github/nku100/impad/runtime/AppAdapterRegistry.kt
- app/src/main/kotlin/io/github/nku100/impad/runtime/HookInstaller.kt
- app/src/main/kotlin/io/github/nku100/impad/runtime/RuntimeCompatibilityResolver.kt
- compat-core/src/main/kotlin/io/github/nku100/impad/compat/CandidatePipeline.kt
- compat-core/src/main/kotlin/io/github/nku100/impad/compat/CompatibilityResolver.kt

Do not modify other applications, other source files, controller scripts,
workflows, checker, build configuration, credentials or historical evidence.
Leave changes uncommitted. Do not spawn agents, launch another Codex process,
change model or reasoning settings, push or create PRs. Independent rejection
returns to this same task; fix the cause without altering acceptance criteria.

Before marking the Goal complete, run your own checks using impad-build and
inspect installed hooks and actual runtime behavior with the fixed-device adb.
For QQ, also run impad-smoke for the trusted request identity contrast.
Static success alone is insufficient. The Goal covers the exact candidate's
required runtime behavior, not merely a build or profile registration.
Ordinary turn completion saves a checkpoint; it does not automatically run
full acceptance. Recoverable acceptance failures must be fixed in the same Goal.
After successful controller acceptance and draft PR publication, a separate CI
job validates the artifacts in a fresh ReDroid. A rejection there is recorded
for a subsequent adaptation run; it does not restart this model loop immediately.
