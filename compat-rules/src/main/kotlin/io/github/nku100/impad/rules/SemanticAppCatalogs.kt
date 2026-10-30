package io.github.nku100.impad.rules

import io.github.nku100.impad.rules.qq.QqSemanticRules
import io.github.nku100.impad.rules.wechat.WeChatSemanticRules
import io.github.nku100.impad.rules.wecom.WeComSemanticRules

object SemanticAppCatalogs {
    fun forApp(appId: String): SemanticRuleCatalog = when (appId) {
        "wechat" -> WeChatSemanticRules.catalog
        "qq" -> QqSemanticRules.catalog
        "wecom" -> WeComSemanticRules.catalog
        else -> throw IllegalArgumentException("No semantic rule catalog for appId: $appId")
    }
}
