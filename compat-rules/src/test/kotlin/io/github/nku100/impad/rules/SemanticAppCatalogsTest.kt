package io.github.nku100.impad.rules

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith

class SemanticAppCatalogsTest {
    @Test
    fun eachAppUsesSharedCatalogAndVersionOnlyOrdersEveryRecognizer() {
        val roles = mapOf("wechat" to setOf("tablet", "login"), "qq" to setOf("device_type"),
            "wecom" to setOf("device_type", "app_pad", "service_pad"))
        roles.forEach { (app, required) ->
            val catalog = SemanticAppCatalogs.forApp(app)
            assertEquals(app, catalog.appId)
            assertEquals(required, catalog.requiredRoles)
            for (version in listOf(1L, 3040L, 3200L, Long.MAX_VALUE)) {
                assertEquals(catalog.queries.map { it.id }.toSet(), catalog.orderedQueries(version).map { it.id }.toSet())
            }
        }
        assertFailsWith<IllegalArgumentException> { SemanticAppCatalogs.forApp("unknown") }
    }
}
