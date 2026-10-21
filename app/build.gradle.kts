import java.io.File
import java.util.Properties
import java.util.zip.ZipFile
import org.gradle.api.DefaultTask
import org.gradle.api.file.DirectoryProperty
import org.gradle.api.file.RegularFileProperty
import org.gradle.api.tasks.InputFile
import org.gradle.api.tasks.OutputDirectory
import org.gradle.api.tasks.TaskAction
import org.gradle.api.tasks.testing.Test

plugins {
    alias(libs.plugins.android.app)
    alias(libs.plugins.kotlin.serialization)
}

val signing = Properties().apply {
    val file = rootProject.file("keystore.properties")
    if (file.isFile) file.inputStream().use(::load)
}

fun signingValue(property: String, environment: String): String? =
    signing.getProperty(property) ?: System.getenv(environment)

val signingStorePath = signingValue("storeFile", "WECHATPAD_KEYSTORE")
val signingStoreFile = signingStorePath?.let { path ->
    File(path).let { if (it.isAbsolute) it else rootProject.file(path) }
}
val signingStorePassword = signingValue("storePassword", "WECHATPAD_KEYSTORE_PASSWORD")
val signingKeyAlias = signingValue("keyAlias", "WECHATPAD_KEY_ALIAS")
val signingKeyPassword = signingValue("keyPassword", "WECHATPAD_KEY_PASSWORD")
val hasStableSigningKey = signingStoreFile?.isFile == true &&
        signingStorePassword != null && signingKeyAlias != null && signingKeyPassword != null
val disposableDebugKey = System.getenv("WECHATPAD_DEBUG_KEYSTORE")?.let(::File)
require(disposableDebugKey == null || disposableDebugKey.isFile) {
    "Configured disposable debug keystore does not exist"
}

if (!hasStableSigningKey && disposableDebugKey == null) {
    logger.warn("ImPad signing key is not configured; using the default debug key")
}

abstract class GenerateRuntimeIdentityAsset : DefaultTask() {
    @get:InputFile
    abstract val fingerprintFile: RegularFileProperty

    @get:OutputDirectory
    abstract val outputDirectory: DirectoryProperty

    @TaskAction
    fun generate() {
        val outputRoot = outputDirectory.get().asFile
        outputRoot.deleteRecursively()
        val output = File(outputRoot, "runtime-identity.json")
        output.parentFile.mkdirs()
        fingerprintFile.get().asFile.copyTo(output, overwrite = true)
    }
}

android {
    namespace = "io.github.nku100.impad"
    buildFeatures {
        buildConfig = true
    }
    compileSdk = 37
    compileSdkMinor = 2
    buildToolsVersion = "37.0.0"

    defaultConfig {
        applicationId = "io.github.nku100.impad"
        minSdk = 28
        targetSdk = 37
        versionCode = 2
        versionName = "1.0.0"
    }

    signingConfigs {
        if (disposableDebugKey != null) {
            getByName("debug") {
                storeFile = disposableDebugKey
            }
        }
        if (hasStableSigningKey) {
            create("stable") {
                storeFile = signingStoreFile
                storePassword = signingStorePassword
                keyAlias = signingKeyAlias
                keyPassword = signingKeyPassword
            }
        }
    }

    buildTypes {
        debug {
            if (disposableDebugKey != null) {
                signingConfig = signingConfigs.getByName("debug")
            } else if (hasStableSigningKey) {
                signingConfig = signingConfigs.getByName("stable")
            }
        }
        release {
            isDebuggable = false
            if (hasStableSigningKey) {
                signingConfig = signingConfigs.getByName("stable")
            }
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_21
        targetCompatibility = JavaVersion.VERSION_21
    }

    packaging {
        resources {
            merges += "META-INF/xposed/*"
        }
    }
}

val runtimeLocalAssetsRoot = providers.gradleProperty("runtimeLocalAssets").orNull?.let { path ->
    File(rootProject.projectDir, path)
}
val moduleFingerprintFile = rootProject.layout.projectDirectory.file(
    "compat-rules/build/generated/ruleExecutionFingerprint/rule-execution-fingerprint.json",
)
runtimeLocalAssetsRoot?.let { localAssetsRoot ->
    require(localAssetsRoot.resolve("assets").isDirectory && localAssetsRoot.resolve("resources").isDirectory) {
        "Runtime-local asset directory must contain assets/ and resources/"
    }
    android.sourceSets.getByName("debug").assets.srcDir(File(localAssetsRoot, "assets"))
    android.sourceSets.getByName("debug").resources.srcDir(File(localAssetsRoot, "resources"))
}

androidComponents {
    onVariants(selector().all()) { variant ->
        val task = tasks.register<GenerateRuntimeIdentityAsset>(
            "generate${variant.name.replaceFirstChar(Char::uppercase)}RuntimeIdentityAsset",
        ) {
            dependsOn(":compat-rules:generateRuleExecutionFingerprint")
            fingerprintFile.set(moduleFingerprintFile)
            outputDirectory.set(layout.buildDirectory.dir("generated/runtime-identity-assets/${variant.name}"))
        }
        checkNotNull(variant.sources.assets).addGeneratedSourceDirectory(
            task,
            GenerateRuntimeIdentityAsset::outputDirectory,
        )
    }
}

dependencies {
    compileOnly(libs.libxposed.api)
    implementation(project(":compat-core"))
    implementation(project(":compat-rules"))
    implementation(
        files(rootProject.file("${rootProject.extra["dexkitOutputRoot"]}/android-arm64-v8a/dexkit.aar"))
            .builtBy(rootProject.tasks.named("prepareDexKitAndroid")),
    )
    implementation(libs.flatbuffers.java)
    implementation(libs.android.cxx)
    implementation(libs.kotlinx.serialization.json)
    testImplementation(libs.junit.jupiter)
    testImplementation(kotlin("test-junit5"))
    testRuntimeOnly(libs.junit.platform.launcher)
}

tasks.withType<Test>().configureEach {
    useJUnitPlatform()
}

val requireReleaseSigning = tasks.register("requireReleaseSigning") {
    doLast {
        check(hasStableSigningKey) { "Release APK requires a configured stable signing key" }
    }
}
tasks.matching { it.name == "packageRelease" }.configureEach {
    dependsOn(requireReleaseSigning)
}

fun registerModuleMetadataVerification(taskName: String, variant: String) = tasks.register(taskName) {
    dependsOn("assemble${variant.replaceFirstChar(Char::uppercase)}")

    doLast {
        val apk = layout.buildDirectory.file("outputs/apk/$variant/app-$variant.apk").get().asFile
        check(apk.isFile) { "$variant APK was not produced: $apk" }

        val sdkPath = System.getenv("ANDROID_HOME")
            ?: rootProject.file("local.properties").takeIf(File::isFile)?.let { localPropertiesFile ->
                Properties().apply { localPropertiesFile.inputStream().use(::load) }
                    .getProperty("sdk.dir")
            }
            ?: error("Set ANDROID_HOME or sdk.dir to inspect the packaged APK")
        val apkanalyzer = File(sdkPath, "cmdline-tools/latest/bin/apkanalyzer")
        check(apkanalyzer.isFile) { "apkanalyzer was not found: $apkanalyzer" }

        val process = ProcessBuilder(
            apkanalyzer.absolutePath,
            "manifest",
            "application-id",
            apk.absolutePath,
        ).redirectErrorStream(true).start()
        val applicationId = process.inputStream.bufferedReader().use { it.readText() }.trim()
        check(process.waitFor() == 0) { "apkanalyzer failed: $applicationId" }
        check(applicationId == "io.github.nku100.impad") {
            "Unexpected application id: $applicationId"
        }

        val debugProcess = ProcessBuilder(
            apkanalyzer.absolutePath, "manifest", "debuggable", apk.absolutePath,
        ).redirectErrorStream(true).start()
        val debuggable = debugProcess.inputStream.bufferedReader().use { it.readText() }.trim()
        check(debugProcess.waitFor() == 0) { "apkanalyzer failed to inspect debuggable: $debuggable" }
        check(debuggable == (variant == "debug").toString()) {
            "Unexpected debuggable value for $variant APK: $debuggable"
        }
        val apksigner = File(sdkPath, "build-tools/37.0.0/apksigner")
        val signatureProcess = ProcessBuilder(apksigner.absolutePath, "verify", apk.absolutePath)
            .redirectErrorStream(true).start()
        val signatureResult = signatureProcess.inputStream.bufferedReader().use { it.readText() }
        check(signatureProcess.waitFor() == 0) { "APK signature verification failed: $signatureResult" }

        val aapt = File(sdkPath, "build-tools/37.0.0/aapt")
        check(aapt.isFile) { "aapt was not found: $aapt" }
        val badgingProcess = ProcessBuilder(aapt.absolutePath, "dump", "badging", apk.absolutePath)
            .redirectErrorStream(true).start()
        val badging = badgingProcess.inputStream.bufferedReader().use { it.readText() }
        check(badgingProcess.waitFor() == 0) { "aapt failed to inspect application metadata: $badging" }
        check("application-label:'I'm Pad'" in badging) {
            "Unexpected application label in APK metadata"
        }

        ZipFile(apk).use { archive ->
            val fingerprintEntry = checkNotNull(archive.getEntry("assets/runtime-identity.json")) {
                "Build-time module fingerprint is missing from the APK"
            }
            val fingerprintBytes = archive.getInputStream(fingerprintEntry).use { it.readBytes() }
            check(fingerprintBytes.contentEquals(moduleFingerprintFile.asFile.readBytes())) {
                "APK module fingerprint differs from the Gradle-generated source identity"
            }
            val fingerprint = groovy.json.JsonSlurper().parseText(fingerprintBytes.decodeToString()) as Map<*, *>
            check(listOf("sourceTreeSha256", "routeConfigurationSha256", "dependencyLockSha256").all {
                (fingerprint[it] as? String)?.matches(Regex("[a-f0-9]{64}")) == true
            }) { "APK module fingerprint is incomplete" }

            val propertiesEntry = checkNotNull(archive.getEntry("META-INF/xposed/module.prop")) {
                "Module metadata is missing from the APK"
            }
            val module = Properties().apply {
                archive.getInputStream(propertiesEntry).use(::load)
            }
            check(module.getProperty("minApiVersion") == "102")
            check(module.getProperty("targetApiVersion") == "102")
            check(module.getProperty("staticScope") == "true")

            val scope = checkNotNull(archive.getEntry("META-INF/xposed/scope.list")) {
                "Module scope is missing from the APK"
            }
            val scopePackages = archive.getInputStream(scope).bufferedReader().use { reader ->
                reader.readLines().map(String::trim).filter(String::isNotEmpty)
            }
            val expectedScopePackages = runtimeLocalAssetsRoot?.takeIf { variant == "debug" }
                ?.resolve("resources/META-INF/xposed/scope.list")
                ?.takeIf(File::isFile)
                ?.readLines()
                ?.map(String::trim)
                ?.filter(String::isNotEmpty)
                ?: listOf("com.tencent.mm", "com.tencent.wework", "com.tencent.mobileqq")
            check(scopePackages == expectedScopePackages) {
                "Unexpected module scope: $scopePackages"
            }

            val entryPoint = checkNotNull(archive.getEntry("META-INF/xposed/java_init.list")) {
                "Module entry point is missing from the APK"
            }
            val entryClasses = archive.getInputStream(entryPoint).bufferedReader().use { reader ->
                reader.readLines().map(String::trim).filter(String::isNotEmpty)
            }
            check(entryClasses == listOf("io.github.nku100.impad.ImPadModule")) {
                "Unexpected module entry point: $entryClasses"
            }

            val compatibilityAssets = archive.entries().asSequence().map { it.name }
                .filter { it.startsWith("assets/compatibility/") || it.startsWith("assets/compatibility-local/") }
                .toList()
            check(compatibilityAssets.isEmpty()) {
                "$variant APK must not package historical targets or local compatibility assets: $compatibilityAssets"
            }
        }
    }
}

registerModuleMetadataVerification("verifyModuleMetadata", "debug")
registerModuleMetadataVerification("verifyReleaseModuleMetadata", "release")
