package io.github.nku100.impad.checker

import io.github.nku100.impad.rules.SemanticAppCatalogs
import java.io.File
import java.nio.file.Files
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertFalse
import kotlin.test.assertTrue

class SemanticCliTest {
    @Test fun `catalog command writes schema two semantic catalogs for every registered app`() {
        listOf("wechat", "qq", "wecom").forEach { appId ->
            val destination = Files.createTempFile("semantic-catalog", ".json").toFile()
            try {
                writeSemanticCatalog(RuleCatalogArguments(appId, destination))
                val document = Json.parseToJsonElement(destination.readText()).jsonObject
                val expected = semanticCatalogReport(SemanticAppCatalogs.forApp(appId))
                assertEquals("2", document.getValue("schemaVersion").jsonPrimitive.content)
                assertEquals(expected.ruleCatalogSha256, document.getValue("ruleCatalogSha256").jsonPrimitive.content)
                assertEquals(appId, document.getValue("appId").jsonPrimitive.content)
                assertFalse("definitions" in document)
            } finally { destination.delete() }
        }
    }

    @Test fun `check rules arguments preserve base apk and ordered splits`() {
        val parsed = parseRuleCheckArguments(listOf(
            "--app-id", "wechat", "--apk", "base.apk", "--abi-splits", "split_config.apk,split_feature.apk", "--report", "result.json",
        ))
        assertEquals("wechat", parsed.appId)
        assertEquals(File("base.apk"), parsed.apk)
        assertEquals(listOf(File("split_config.apk"), File("split_feature.apk")), parsed.abiSplitApks)
    }

    @Test fun `writes diagnostics before failing closed for every non selected outcome`() {
        listOf("NO_MATCH", "CONFLICT", "INCOMPLETE", "UNSUPPORTED_INPUT", "EVALUATION_ERROR").forEach { status ->
            val destination = Files.createTempFile("semantic-status", ".json").toFile()
            try {
                assertFailsWith<IllegalStateException> {
                    writeSemanticReportOrFail(status, destination) { file -> file.writeText("diagnostic:$status") }
                }
                assertEquals("diagnostic:$status", destination.readText())
            } finally { destination.delete() }
        }
    }

    @Test fun `accepts selected outcome after writing report`() {
        val destination = Files.createTempFile("semantic-selected", ".json").toFile()
        try {
            writeSemanticReportOrFail("SELECTED", destination) { it.writeText("selected") }
            assertTrue(destination.readText() == "selected")
        } finally { destination.delete() }
    }
}
