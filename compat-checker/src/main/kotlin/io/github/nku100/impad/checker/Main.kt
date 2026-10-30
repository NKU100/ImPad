package io.github.nku100.impad.checker

import io.github.nku100.impad.rules.DexKitRuntimeIdentity
import io.github.nku100.impad.rules.DexKitRuntimeLoader
import io.github.nku100.impad.rules.EffectiveCodeRuleSession
import io.github.nku100.impad.rules.SemanticAppCatalogs
import java.io.File
import kotlin.system.exitProcess

fun main(args: Array<String>) {
    try {
        when (args.firstOrNull()) {
            "check-rules" -> checkRules(parseRuleCheckArguments(args.drop(1)))
            "catalog" -> writeSemanticCatalog(parseRuleCatalogArguments(args.drop(1)))
            else -> error("Usage: check-rules --app-id <id> --apk <apk> [--abi-splits <apk1,apk2>] --report <json> | catalog --app-id <id> --report <json>")
        }
    } catch (error: Exception) {
        System.err.println("Compatibility check failed: ${error.message}")
        exitProcess(1)
    }
}

internal fun writeSemanticCatalog(arguments: RuleCatalogArguments) {
    val catalog = SemanticAppCatalogs.forApp(arguments.appId)
    writeSemanticCatalogReport(catalog, arguments.report)
    println("Semantic rule catalog report: ${arguments.report.absolutePath}")
}

private fun checkRules(arguments: RuleCheckArguments) {
    var runtimeIdentity: DexKitRuntimeIdentity? = null
    val checker = SemanticRuleCheck(
        sessionOpener = { snapshot ->
            runtimeIdentity = DexKitRuntimeLoader.load(
                File(System.getenv("DEXKIT_RUNTIME_DIR") ?: error("DEXKIT_RUNTIME_DIR is not set")),
            )
            EffectiveCodeRuleSession.open(snapshot)
        },
        runtimeIdentityProvider = { runtimeIdentity },
    )
    val report = checker.check(arguments.appId, listOf(arguments.apk) + arguments.abiSplitApks)
    writeSemanticReportOrFail(report.selection.status, arguments.report) { destination ->
        writeSemanticRuleCheckReport(report, destination)
    }
    println("Semantic rule check report: ${arguments.report.absolutePath}")
    println("APK: ${report.identity.packageName} ${report.identity.versionName} (${report.identity.versionCode})")
    println("APK SHA-256: ${report.identity.apkSha256}")
    println("Selection: SELECTED ${report.selection.executionSha256}")
}

internal fun writeSemanticReportOrFail(
    status: String,
    destination: File,
    writeReport: (File) -> Unit,
) {
    writeReport(destination)
    check(status == "SELECTED") {
        "Semantic rule selection failed with status $status; report written to ${destination.absolutePath}"
    }
}

internal fun writeRuleCheckReport(report: RuleCheckReport, destination: File) {
    RuleCheckReportWriter.write(report, destination)
    check(report.selection.status != "ERROR") { "Rule query failed; report written to ${destination.absolutePath}" }
}
