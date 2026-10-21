package io.github.nku100.impad.checker

import io.github.nku100.impad.compat.AppCompatibilityPolicies
import io.github.nku100.impad.compat.BuildIdentity
import io.github.nku100.impad.compat.rules.MethodShape
import io.github.nku100.impad.compat.rules.ResolvedPlan
import io.github.nku100.impad.compat.rules.RuleResolution
import io.github.nku100.impad.compat.rules.SelectionOutcome
import io.github.nku100.impad.compat.rules.RuleDefinition
import io.github.nku100.impad.compat.rules.VersionBinding
import io.github.nku100.impad.rules.RuleCatalog
import io.github.nku100.impad.rules.DexKitRuntimeIdentity
import io.github.nku100.impad.rules.RuleExecutionFingerprint
import io.github.nku100.impad.rules.RuleExecutionIdentityReport
import io.github.nku100.impad.rules.RuleSession
import io.github.nku100.impad.rules.SharedRuleResolver
import java.io.File
import java.security.MessageDigest
import kotlinx.serialization.Serializable
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json

class RuleCheck(
    private val identityReader: (File, List<File>) -> BuildIdentity = { apk, splits ->
        ApkIdentityInspector().inspect(apk, splits)
    },
    private val sessionOpener: (List<File>) -> RuleSession = RuleSession::open,
    private val catalogProvider: (String) -> RuleCatalog = RuleCatalog::forApp,
    private val runtimeIdentityProvider: () -> DexKitRuntimeIdentity? = { null },
) {
    fun check(appId: String, apkFiles: List<File>): RuleCheckReport {
        require(appId.isNotBlank()) { "--app-id is required" }
        require(apkFiles.isNotEmpty()) { "At least one APK input is required" }
        require(apkFiles.all(File::isFile)) { "An APK input does not exist" }
        val orderedFiles = apkFiles.map(File::getAbsoluteFile)
        val baseApk = orderedFiles.first()
        val abiSplits = orderedFiles.drop(1)
        val policy = AppCompatibilityPolicies.require(appId)
        val identity = identityReader(baseApk, abiSplits)
        policy.validateIdentity(identity)
        val catalog = catalogProvider(appId)
        require(catalog.appId == appId) { "Rule catalog does not match explicit appId" }
        val inputHashes = orderedFiles.map { InputHash(it.path, sha256(it)) }

        val sharedRun = sessionOpener(orderedFiles).use { session ->
            SharedRuleResolver.resolveDetailed(identity, catalog, session)
        }
        return RuleCheckReport(
            appId = appId,
            analysisScope = "STATIC_ONLY",
            identity = identity,
            inputHashes = inputHashes,
            ruleCatalogSha256 = catalog.sha256,
            ruleCatalog = RuleCatalogReport(
                appId = catalog.appId,
                ruleCatalogSha256 = catalog.sha256,
                definitions = catalog.definitions,
                bindings = catalog.bindings,
                strategyIds = catalog.strategyIds.sorted(),
            ),
            executionIdentity = RuleExecutionFingerprint.createReport(
                catalog.sha256,
                RuleExecutionFingerprint.loadManifest(),
                runtimeIdentityProvider(),
            ),
            routeCandidates = sharedRun.routeCandidates,
            rules = sharedRun.results.map { (ruleId, resolution) ->
                val query = requireNotNull(catalog.query(ruleId))
                resolutionReport(query.definition.strategyId, ruleId, resolution)
            },
            selection = selectionReport(sharedRun.outcome),
        )
    }
}

@Serializable
data class RuleCheckReport(
    val appId: String,
    val analysisScope: String,
    val identity: BuildIdentity,
    val inputHashes: List<InputHash>,
    val ruleCatalogSha256: String,
    val ruleCatalog: RuleCatalogReport,
    val executionIdentity: RuleExecutionIdentityReport,
    val routeCandidates: List<String>,
    val rules: List<RuleResultReport>,
    val selection: RuleSelectionReport,
)

@Serializable
data class RuleCatalogReport(
    val schemaVersion: Int = 1,
    val appId: String,
    val ruleCatalogSha256: String,
    val definitions: List<RuleDefinition>,
    val bindings: List<VersionBinding>,
    val strategyIds: List<String>,
)

object RuleCatalogReportWriter {
    private val json = Json { prettyPrint = true; encodeDefaults = true }

    fun write(report: RuleCatalogReport, destination: File) {
        destination.absoluteFile.parentFile.mkdirs()
        destination.writeText(json.encodeToString(report) + "\n")
    }
}

data class RuleCatalogArguments(val appId: String, val report: File)

internal fun parseRuleCatalogArguments(args: List<String>): RuleCatalogArguments {
    val values = parseOptions(args)
    require(values.keys == setOf("--app-id", "--report")) {
        "Usage: catalog --app-id <id> --report <json>"
    }
    return RuleCatalogArguments(values.getValue("--app-id"), File(values.getValue("--report")))
}

@Serializable
data class InputHash(val path: String, val sha256: String)

@Serializable
data class RuleResultReport(
    val ruleId: String,
    val strategyId: String,
    val status: String,
    val candidateCounts: Map<String, Int>,
    val diagnostics: List<String>,
    val targets: Map<String, ResolvedTargetReport>,
)

@Serializable
data class ResolvedTargetReport(
    val descriptor: String,
    val shape: MethodShapeReport,
)

@Serializable
data class MethodShapeReport(
    val parameters: List<String>,
    val returnType: String,
    val isStatic: Boolean,
)

@Serializable
data class RuleSelectionReport(
    val status: String,
    val selectedRuleId: String? = null,
    val strategyId: String? = null,
    val targets: Map<String, ResolvedTargetReport> = emptyMap(),
    val diagnostics: List<String> = emptyList(),
)

object RuleCheckReportWriter {
    private val json = Json { prettyPrint = true; encodeDefaults = true }

    fun write(report: RuleCheckReport, destination: File) {
        destination.absoluteFile.parentFile.mkdirs()
        destination.writeText(json.encodeToString(report) + "\n")
    }
}

data class RuleCheckArguments(
    val appId: String,
    val apk: File,
    val abiSplitApks: List<File>,
    val report: File,
)

internal fun parseRuleCheckArguments(args: List<String>): RuleCheckArguments {
    val values = parseOptions(args)
    val allowed = setOf("--app-id", "--apk", "--abi-splits", "--report")
    require(values.keys.containsAll(setOf("--app-id", "--apk", "--report")) && values.keys.all { it in allowed }) {
        "Usage: check-rules --app-id <id> --apk <apk-path> [--abi-splits <apk1,apk2>] --report <json>"
    }
    return RuleCheckArguments(
        appId = values.getValue("--app-id"),
        apk = File(values.getValue("--apk")),
        abiSplitApks = values["--abi-splits"]?.split(',')?.filter(String::isNotBlank)?.map(::File).orEmpty(),
        report = File(values.getValue("--report")),
    )
}

internal fun reportJson(report: RuleCheckReport): String =
    Json { prettyPrint = true; encodeDefaults = true }.encodeToString(report)

private fun parseOptions(args: List<String>): Map<String, String> {
    require(args.size % 2 == 0) { "Expected option/value pairs" }
    val pairs = args.chunked(2)
    require(pairs.all { it.first().startsWith("--") && it.last().isNotBlank() }) { "Expected nonempty option/value pairs" }
    val values = pairs.associate { it.first() to it.last() }
    require(values.size == pairs.size) { "Duplicate command-line option" }
    return values
}

private fun resolutionReport(strategyId: String, ruleId: String, resolution: RuleResolution): RuleResultReport {
    val plan = (resolution as? RuleResolution.Unique)?.plan
    return RuleResultReport(
        ruleId = ruleId,
        strategyId = strategyId,
        status = when (resolution) {
            is RuleResolution.Missing -> "MISSING"
            is RuleResolution.Unique -> "UNIQUE"
            is RuleResolution.Ambiguous -> "AMBIGUOUS"
            is RuleResolution.Error -> "ERROR"
        },
        candidateCounts = resolution.candidateCounts,
        diagnostics = resolution.diagnostics,
        targets = plan?.targets?.mapValues { (_, target) -> target.toReport() }.orEmpty(),
    )
}

private fun selectionReport(outcome: SelectionOutcome): RuleSelectionReport = when (outcome) {
    is SelectionOutcome.Selected -> RuleSelectionReport(
        status = "SELECTED",
        selectedRuleId = outcome.plan.rule.ruleId,
        strategyId = outcome.plan.rule.strategyId,
        targets = outcome.plan.toReportTargets(),
    )
    SelectionOutcome.NoMatch -> RuleSelectionReport("NO_MATCH")
    is SelectionOutcome.Ambiguous -> RuleSelectionReport(
        status = "AMBIGUOUS",
        diagnostics = outcome.rules.flatMap { (ruleId, result) -> result.diagnostics.map { "$ruleId: $it" } },
    )
    is SelectionOutcome.RuleConflict -> RuleSelectionReport(
        status = "RULE_CONFLICT",
        diagnostics = listOf("Multiple complete rules matched: ${outcome.ruleIds.joinToString()}"),
    )
    is SelectionOutcome.Error -> RuleSelectionReport(
        status = "ERROR",
        diagnostics = outcome.rules.flatMap { (ruleId, result) -> result.diagnostics.map { "$ruleId: $it" } },
    )
}

private fun ResolvedPlan.toReportTargets() = targets.mapValues { (_, target) -> target.toReport() }

private fun io.github.nku100.impad.compat.rules.ResolvedTarget.toReport() = ResolvedTargetReport(
    descriptor = descriptor,
    shape = shape.toReport(),
)

private fun MethodShape.toReport() = MethodShapeReport(parameters, returnType, isStatic)

private fun sha256(file: File): String {
    val digest = MessageDigest.getInstance("SHA-256")
    file.inputStream().buffered().use { input ->
        val buffer = ByteArray(DEFAULT_BUFFER_SIZE)
        while (true) {
            val count = input.read(buffer)
            if (count < 0) break
            digest.update(buffer, 0, count)
        }
    }
    return digest.digest().joinToString("") { "%02x".format(it) }
}
