import org.gradle.api.tasks.Exec

buildscript {
    dependencies {
        classpath(libs.kotlin.gradle.plugin)
    }
}

plugins {
    alias(libs.plugins.android.app) apply false
    alias(libs.plugins.kotlin.jvm) apply false
    alias(libs.plugins.kotlin.serialization) apply false
}

val dexkitHostPlatform = run {
    val osName = System.getProperty("os.name").lowercase()
    val architecture = System.getProperty("os.arch").lowercase()
    if (osName.contains("linux")) {
        when (architecture) {
            "x86_64", "amd64", "x64" -> "linux-x86_64"
            "aarch64", "arm64" -> "linux-aarch64"
            else -> error("DexKit checker is unsupported on Linux/$architecture")
        }
    } else {
        "android-arm64-v8a"
    }
}
extra["dexkitHostPlatform"] = dexkitHostPlatform
val dexkitOutputRoot = providers.gradleProperty("dexkitOutputRoot")
    .orElse(layout.buildDirectory.dir("dexkit").get().asFile.absolutePath)
    .get()
extra["dexkitOutputRoot"] = dexkitOutputRoot

val dexkitPrepareScript = layout.projectDirectory.file("scripts/ci/prepare_dexkit.py")
val dexkitLockFile = layout.projectDirectory.file("gradle/dexkit-lock.json")

fun registerDexKitPreparation(name: String, platform: String) = tasks.register<Exec>(name) {
    val destination = File(dexkitOutputRoot, platform)
    inputs.file(dexkitPrepareScript)
    inputs.file(dexkitLockFile)
    commandLine(
        "python3",
        dexkitPrepareScript.asFile.absolutePath,
        "--lock", dexkitLockFile.asFile.absolutePath,
        "--output", destination.absolutePath,
        "--platform", platform,
    )
}

val prepareDexKitForHost = registerDexKitPreparation("prepareDexKitForHost", dexkitHostPlatform)
val prepareDexKitAndroid = tasks.register<Exec>("prepareDexKitAndroid") {
    if (dexkitHostPlatform == "android-arm64-v8a") {
        dependsOn(prepareDexKitForHost)
        onlyIf { false }
    } else {
        val destination = File(dexkitOutputRoot, "android-arm64-v8a")
        inputs.file(dexkitPrepareScript)
        inputs.file(dexkitLockFile)
        commandLine(
            "python3",
            dexkitPrepareScript.asFile.absolutePath,
            "--lock", dexkitLockFile.asFile.absolutePath,
            "--output", destination.absolutePath,
            "--platform", "android-arm64-v8a",
        )
    }
}

tasks.register("verifyModuleMetadata") {
    dependsOn(":app:verifyModuleMetadata")
}
