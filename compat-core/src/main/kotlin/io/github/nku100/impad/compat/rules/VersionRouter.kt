package io.github.nku100.impad.compat.rules

object VersionRouter {
    fun candidates(versionCode: Long, bindings: List<VersionBinding>): List<String> {
        require(versionCode > 0) { "versionCode must be positive" }
        validateBindings(bindings)
        if (bindings.isEmpty()) return emptyList()

        val ordered = bindings.sortedBy(VersionBinding::min)
        val containing = ordered.firstOrNull { binding ->
            versionCode >= binding.min && (binding.max == null || versionCode <= binding.max)
        }
        if (containing != null) return listOf(containing.ruleId)

        if (versionCode < ordered.first().min) return listOf(ordered.first().ruleId)

        val rightIndex = ordered.indexOfFirst { versionCode < it.min }
        if (rightIndex < 0) return emptyList()
        return listOf(ordered[rightIndex - 1].ruleId, ordered[rightIndex].ruleId).distinct()
    }

    fun validate(
        bindings: List<VersionBinding>,
        rules: List<RuleDefinition>,
        strategyIds: Set<String>,
    ) {
        validateBindings(bindings)
        require(rules.map(RuleDefinition::ruleId).toSet().size == rules.size) { "Duplicate ruleId" }
        rules.forEach { rule ->
            require(rule.ruleId.isNotBlank()) { "ruleId must not be blank" }
            require(rule.strategyId.isNotBlank() && rule.strategyId in strategyIds) {
                "Unknown strategy reference: ${rule.strategyId}"
            }
            require(rule.requiredTargetIds.isNotEmpty() && rule.requiredTargetIds.none(String::isBlank)) {
                "Rule must declare nonblank required targets"
            }
        }
        val ruleIds = rules.mapTo(mutableSetOf(), RuleDefinition::ruleId)
        require(bindings.all { it.ruleId in ruleIds }) { "Version binding references an unknown rule" }
    }

    private fun validateBindings(bindings: List<VersionBinding>) {
        require(bindings.all { it.ruleId.isNotBlank() }) { "Version binding ruleId must not be blank" }
        require(bindings.all { it.min > 0 && (it.max == null || it.max >= it.min) }) {
            "Version binding has invalid inclusive bounds"
        }
        val ordered = bindings.sortedBy(VersionBinding::min)
        require(ordered.map { it.min to it.max }.toSet().size == ordered.size) {
            "Duplicate version interval declaration"
        }
        ordered.zipWithNext().forEach { (left, right) ->
            require(left.max != null && right.min > left.max) { "Version bindings overlap or follow an open interval" }
        }
        require(ordered.isEmpty() || ordered.last().max == null) {
            "The latest version binding must have an open upper bound"
        }
    }
}
