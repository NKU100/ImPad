package io.github.nku100.impad.compat.rules

object RuleSelector {
    fun select(results: Map<String, RuleResolution>): SelectionOutcome {
        val errors = results.toSortedMap().mapNotNull { (ruleId, result) ->
            (result as? RuleResolution.Error)?.let { ruleId to it }
        }.toMap()
        if (errors.isNotEmpty()) return SelectionOutcome.Error(errors)

        val ambiguous = results.toSortedMap().mapNotNull { (ruleId, result) ->
            (result as? RuleResolution.Ambiguous)?.let { ruleId to it }
        }.toMap()
        if (ambiguous.isNotEmpty()) return SelectionOutcome.Ambiguous(ambiguous)

        val unique = results.toSortedMap().mapNotNull { (ruleId, result) ->
            (result as? RuleResolution.Unique)?.let { ruleId to it }
        }
        if (unique.size > 1) return SelectionOutcome.RuleConflict(unique.map { it.first })
        if (unique.size == 1) return SelectionOutcome.Selected(unique.single().second.plan)
        return SelectionOutcome.NoMatch
    }
}
