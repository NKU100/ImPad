package io.github.nku100.impad.checker

import io.github.nku100.impad.rules.DexKitRuntimeLoader
import io.github.nku100.impad.rules.RuleSession
import io.github.nku100.impad.rules.RuleCatalog
import java.io.File
import kotlin.system.exitProcess

fun main(args: Array<String>) {
    try {
        when (args.firstOrNull()) {
            "check-rules" -> checkRules(parseRuleCheckArguments(args.drop(1)))
            "catalog" -> writeRuleCatalog(parseRuleCatalogArguments(args.drop(1)))
            else -> error("Usage: check-rules --app-id <id> --apk <apk> --report <json> | catalog --app-id <id> --report <json>")
        }
    } catch (error: Exception) {
        System.err.println("Compatibility check failed: ${error.message}")
        exitProcess(1)
    }
}

private fun writeRuleCatalog(arguments: RuleCatalogArguments) {
    val catalog = RuleCatalog.forApp(arguments.appId)
    RuleCatalogReportWriter.write(
        RuleCatalogReport(
            appId = catalog.appId,
            ruleCatalogSha256 = catalog.sha256,
            definitions = catalog.definitions,
            bindings = catalog.bindings,
            strategyIds = catalog.strategyIds.sorted(),
        ),
        arguments.report,
    )
    println("Rule catalog report: ${arguments.report.absolutePath}")
}

private fun checkRules(arguments: RuleCheckArguments) {
    var runtimeIdentity: io.github.nku100.impad.rules.DexKitRuntimeIdentity? = null
    val checker = RuleCheck(
        sessionOpener = { apkFiles ->
            runtimeIdentity = DexKitRuntimeLoader.load(
                File(System.getenv("DEXKIT_RUNTIME_DIR") ?: error("DEXKIT_RUNTIME_DIR is not set")),
            )
            RuleSession.open(apkFiles)
        },
        runtimeIdentityProvider = { runtimeIdentity },
    )
    val report = checker.check(arguments.appId, listOf(arguments.apk) + arguments.abiSplitApks)
    writeRuleCheckReport(report, arguments.report)
    println("Rule check report: ${arguments.report.absolutePath}")
    println("APK: ${report.identity.packageName} ${report.identity.versionName} (${report.identity.versionCode})")
    println("APK SHA-256: ${report.identity.apkSha256}")
    println("Selected: ${report.selection.status} ${report.selection.selectedRuleId.orEmpty()} ${report.selection.strategyId.orEmpty()}")
}

internal fun writeRuleCheckReport(report: RuleCheckReport, destination: File) {
    RuleCheckReportWriter.write(report, destination)
    check(report.selection.status != "ERROR") { "Rule query failed; report written to ${destination.absolutePath}" }
}
