package io.github.nku100.impad.checker

import io.github.nku100.impad.compat.AppCompatibilityPolicies
import io.github.nku100.impad.compat.BuildIdentity
import io.github.nku100.impad.compat.code.CodeDelegation
import io.github.nku100.impad.compat.rules.CompletePlan
import io.github.nku100.impad.compat.rules.CompletePlanSelection
import io.github.nku100.impad.compat.rules.RecognitionResult
import io.github.nku100.impad.rules.CapturedCodeSnapshot
import io.github.nku100.impad.rules.CodeLoaderInput
import io.github.nku100.impad.rules.DexKitRuntimeIdentity
import io.github.nku100.impad.rules.EffectiveCodeRuleSession
import io.github.nku100.impad.rules.HookRoleContract
import io.github.nku100.impad.rules.RecognitionContext
import io.github.nku100.impad.rules.RuleExecutionFingerprint
import io.github.nku100.impad.rules.RuleExecutionIdentityReport
import io.github.nku100.impad.rules.RuleSession
import io.github.nku100.impad.rules.RuleStrategyContract
import io.github.nku100.impad.rules.SemanticAppCatalogs
import io.github.nku100.impad.rules.SemanticRuleCatalog
import io.github.nku100.impad.rules.SemanticRuleResolver
import io.github.nku100.impad.rules.StartupCodeSnapshot
import java.io.File
import java.security.MessageDigest
import kotlinx.serialization.Serializable
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json

class SemanticRuleCheck(
    private val identityReader: (File, List<File>) -> BuildIdentity = { apk, splits -> ApkIdentityInspector().inspect(apk, splits) },
    private val sessionOpener: (CapturedCodeSnapshot) -> RuleSession = EffectiveCodeRuleSession::open,
    private val catalogProvider: (String) -> SemanticRuleCatalog = SemanticAppCatalogs::forApp,
    private val runtimeIdentityProvider: () -> DexKitRuntimeIdentity? = { null },
    private val snapshotCapture: (String, List<CodeLoaderInput>) -> CapturedCodeSnapshot = { ready, loaders ->
        StartupCodeSnapshot().capture(ready, loaders)
    },
) {
    fun check(appId: String, apkFiles: List<File>): SemanticRuleCheckReport {
        require(appId.isNotBlank()) { "appId is required" }
        require(apkFiles.isNotEmpty()) { "At least one APK input is required" }
        require(apkFiles.all(File::isFile)) { "An APK input does not exist" }
        val files = apkFiles.map(File::getCanonicalFile)
        require(files.map(File::getPath).distinct().size == files.size) {
            "Duplicate canonical APK input paths are not allowed"
        }
        val base = files.first()
        val identity = identityReader(base, files.drop(1))
        val policy = AppCompatibilityPolicies.require(appId)
        policy.validateIdentity(identity)
        val inputHashes = files.map { InputHash(it.path, sha256(it)) }
        require(identity.apkSha256 != null && identity.apkSha256.equals(inputHashes.first().sha256, ignoreCase = true)) {
            "Official base APK identity SHA-256 does not match the input file"
        }
        val catalog = catalogProvider(appId)
        require(catalog.appId == appId) { "Semantic rule catalog does not match explicit appId" }
        val capture = snapshotCapture("loader-0", listOf(CodeLoaderInput("loader-0", null, CodeDelegation.PARENT_FIRST, files)))
        try {
            require(capture.archiveManifests.size == inputHashes.size &&
                capture.archiveManifests.map { it.containerSha256.lowercase() } == inputHashes.map { it.sha256 }) {
                "APK inputs changed between identity validation and snapshot capture"
            }
            val session = sessionOpener(capture)
            try {
                val context = RecognitionContext(session, capture, catalog)
                val run = SemanticRuleResolver.resolveDetailed(identity, catalog, context)
                check(capture.inputsStillCurrent() && files.map(::sha256) == inputHashes.map { it.sha256 }) {
                    "APK source changed during semantic rule analysis"
                }
                return SemanticRuleCheckReport(
                    appId = appId,
                    identity = identity,
                    inputHashes = inputHashes,
                    ruleCatalog = semanticCatalogReport(catalog),
                    executionIdentity = RuleExecutionFingerprint.createReport(
                        catalog.sha256, RuleExecutionFingerprint.loadManifest(), runtimeIdentityProvider()),
                    snapshot = capture.toReport(),
                    evaluatedRecognizerIds = run.evaluatedRecognizerIds,
                    recognizers = catalog.queries.map { query -> recognizerReport(query.id, run.results.getValue(query.id)) },
                    selection = selectionReport(run.outcome),
                )
            } finally {
                session.close()
            }
        } finally {
            capture.close()
        }
    }
}

@Serializable
data class SemanticRuleCheckReport(
    val schemaVersion: Int = 2,
    val analysisScope: String = "APK_FILE_SNAPSHOT",
    val appId: String,
    val identity: BuildIdentity,
    val inputHashes: List<InputHash>,
    val ruleCatalog: SemanticCatalogReport,
    val executionIdentity: RuleExecutionIdentityReport,
    val snapshot: SemanticSnapshotReport,
    val evaluatedRecognizerIds: List<String>,
    val recognizers: List<SemanticRecognizerReport>,
    val selection: SemanticSelectionReport,
)

@Serializable
data class SemanticCatalogReport(
    val schemaVersion: Int = 2,
    val appId: String,
    val ruleCatalogSha256: String,
    val contractId: String,
    val contractRevision: Int,
    val requiredRoles: List<String>,
    val recognizerIds: List<String>,
    val strategies: List<SemanticStrategyReport>,
    val versionHints: List<SemanticVersionHintReport>,
)

@Serializable
data class SemanticStrategyReport(val id: String, val revision: Int, val roles: List<SemanticRoleReport>)
@Serializable
data class SemanticRoleReport(val role: String, val phase: String, val configuration: Map<String, String>)
@Serializable
data class SemanticVersionHintReport(val recognizerId: String, val min: Long, val max: Long?)

@Serializable
data class SemanticSnapshotReport(
    val protocolVersion: Int,
    val readyLoaderId: String,
    val loaders: List<SemanticLoaderReport>,
    val archives: List<SemanticArchiveReport>,
    val contentSha256: String,
)
@Serializable
data class SemanticLoaderReport(val id: String, val parentId: String?, val delegation: String, val dexSha256: List<String>)
@Serializable
data class SemanticArchiveReport(val loaderId: String, val containerIndex: Int, val kind: String, val containerSha256: String, val dexSha256: List<String>)

@Serializable
data class SemanticRecognizerReport(
    val recognizerId: String,
    val status: String,
    val plans: List<SemanticRecognizedPlanReport> = emptyList(),
    val diagnostics: List<String> = emptyList(),
    val reason: String? = null,
)
@Serializable
data class SemanticRecognizedPlanReport(val plan: CompletePlan, val executionSha256: String, val evidence: List<String>)

@Serializable
data class SemanticSelectionReport(
    val status: String,
    val plan: CompletePlan? = null,
    val executionSha256: String? = null,
    val evidenceByRecognizer: Map<String, List<String>> = emptyMap(),
    val alternatives: List<SemanticPlanAlternativeReport> = emptyList(),
    val diagnostics: Map<String, List<String>> = emptyMap(),
    val incompleteReasons: Map<String, String> = emptyMap(),
)
@Serializable
data class SemanticPlanAlternativeReport(val plan: CompletePlan, val executionSha256: String, val evidenceByRecognizer: Map<String, List<String>>)

fun semanticCatalogReport(catalog: SemanticRuleCatalog) = SemanticCatalogReport(
    appId = catalog.appId,
    ruleCatalogSha256 = catalog.sha256,
    contractId = catalog.contractId,
    contractRevision = catalog.contractRevision,
    requiredRoles = catalog.requiredRoles.sorted(),
    recognizerIds = catalog.queries.map { it.id },
    strategies = catalog.strategies.map { strategy -> SemanticStrategyReport(strategy.id, strategy.revision,
        strategy.roles.map { role -> SemanticRoleReport(role.role, role.phase.name, role.configuration.toSortedMap()) }) },
    versionHints = catalog.versionHints.map { SemanticVersionHintReport(it.ruleId, it.min, it.max) },
)

fun writeSemanticRuleCheckReport(report: SemanticRuleCheckReport, destination: File) {
    destination.absoluteFile.parentFile.mkdirs()
    destination.writeText(semanticRuleCheckReportJson(report) + "\n")
}

fun writeSemanticCatalogReport(catalog: SemanticRuleCatalog, destination: File) {
    destination.absoluteFile.parentFile.mkdirs()
    destination.writeText(semanticCatalogReportJson(semanticCatalogReport(catalog)) + "\n")
}

internal fun semanticRuleCheckReportJson(report: SemanticRuleCheckReport): String = semanticJson.encodeToString(report)
internal fun semanticCatalogReportJson(report: SemanticCatalogReport): String = semanticJson.encodeToString(report)
private val semanticJson = Json { prettyPrint = true; encodeDefaults = true }

private fun CapturedCodeSnapshot.toReport() = SemanticSnapshotReport(
    protocolVersion = metadata.protocolVersion,
    readyLoaderId = metadata.readyLoaderId,
    loaders = metadata.loaders.map { SemanticLoaderReport(it.id, it.parentId, it.delegation.name, it.dexSha256) },
    archives = archiveManifests.map { SemanticArchiveReport(it.loaderId, it.containerIndex, it.kind.name, it.containerSha256, it.dexSha256) },
    contentSha256 = metadata.contentSha256(),
)

private fun recognizerReport(id: String, result: RecognitionResult): SemanticRecognizerReport {
    fun plans(values: List<io.github.nku100.impad.compat.rules.RecognizedPlan>) = values.map {
        SemanticRecognizedPlanReport(it.plan, it.plan.executionSha256(), it.evidence)
    }
    return when (result) {
        is RecognitionResult.NoMatch -> SemanticRecognizerReport(id, "NO_MATCH", diagnostics = result.diagnostics)
        is RecognitionResult.Plans -> SemanticRecognizerReport(id, "PLANS", plans(result.plans))
        is RecognitionResult.Incomplete -> SemanticRecognizerReport(id, "INCOMPLETE", plans(result.discoveredPlans), reason = result.reason)
        is RecognitionResult.UnsupportedInput -> SemanticRecognizerReport(id, "UNSUPPORTED_INPUT", diagnostics = result.diagnostics)
        is RecognitionResult.EvaluationError -> SemanticRecognizerReport(id, "EVALUATION_ERROR", diagnostics = result.diagnostics)
    }
}

private fun selectionReport(selection: CompletePlanSelection): SemanticSelectionReport = when (selection) {
    is CompletePlanSelection.Selected -> SemanticSelectionReport("SELECTED", selection.match.plan,
        selection.match.plan.executionSha256(), selection.match.evidenceByRecognizer)
    CompletePlanSelection.NoMatch -> SemanticSelectionReport("NO_MATCH")
    is CompletePlanSelection.Conflict -> SemanticSelectionReport("CONFLICT", alternatives = selection.plans.map(::alternative))
    is CompletePlanSelection.Incomplete -> SemanticSelectionReport("INCOMPLETE",
        alternatives = selection.discoveredPlans.map(::alternative), incompleteReasons = selection.reasons)
    is CompletePlanSelection.UnsupportedInput -> SemanticSelectionReport("UNSUPPORTED_INPUT", diagnostics = selection.diagnostics)
    is CompletePlanSelection.EvaluationError -> SemanticSelectionReport("EVALUATION_ERROR", diagnostics = selection.diagnostics)
}

private fun alternative(match: io.github.nku100.impad.compat.rules.SelectedPlanMatch) =
    SemanticPlanAlternativeReport(match.plan, match.plan.executionSha256(), match.evidenceByRecognizer)

private fun sha256(file: File): String {
    val digest = MessageDigest.getInstance("SHA-256")
    val buffer = ByteArray(64 * 1024)
    file.inputStream().buffered(buffer.size).use { input ->
        while (true) {
            val count = input.read(buffer)
            if (count < 0) break
            digest.update(buffer, 0, count)
        }
    }
    return digest.digest().joinToString("") { "%02x".format(it) }
}
