package com.tencent.tinker.loader.app

import android.content.Context

class TinkerApplication(val postAttachClassLoader: ClassLoader) : android.content.ContextWrapper(null) {
    override fun getClassLoader(): ClassLoader = postAttachClassLoader
    @Suppress("UNUSED_PARAMETER")
    fun onBaseContextAttached(context: Context, startElapsedTime: Long, startTotalTime: Long) = Unit
}
