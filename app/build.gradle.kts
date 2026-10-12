import java.io.File
import java.util.Properties
import java.util.zip.ZipFile
import org.gradle.api.DefaultTask
import org.gradle.api.file.DirectoryProperty
import org.gradle.api.file.RegularFileProperty
import org.gradle.api.tasks.InputDirectory
import org.gradle.api.tasks.InputFile
import org.gradle.api.tasks.OutputDirectory
import org.gradle.api.tasks.PathSensitive
import org.gradle.api.tasks.PathSensitivity
import org.gradle.api.tasks.TaskAction
import org.gradle.api.tasks.testing.Test

plugins {
    alias(libs.plugins.android.app)
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

abstract class GenerateCompatibilityTargets : DefaultTask() {
    @get:InputDirectory
    @get:PathSensitive(PathSensitivity.RELATIVE)
    abstract val sourceDirectory: DirectoryProperty

    @get:OutputDirectory
    abstract val outputDirectory: DirectoryProperty

    @TaskAction
    fun generate() {
        outputDirectory.get().asFile.deleteRecursively()
        val compatibilityRoot = sourceDirectory.get().asFile
        compatibilityRoot.listFiles()?.filter { it.isDirectory }?.sortedBy { it.name }?.forEach { appDirectory ->
            require(Regex("[a-z][a-z0-9_-]*").matches(appDirectory.name)) {
                "Invalid application policy directory: ${appDirectory.name}"
            }
            val source = appDirectory.resolve("targets.json")
            if (source.isFile) {
                val destination = outputDirectory.file("compatibility/${appDirectory.name}/targets.json").get().asFile
                destination.parentFile.mkdirs()
                source.copyTo(destination, overwrite = true)
            }
        }
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
        versionCode = 1
        versionName = "0.1.0"
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
runtimeLocalAssetsRoot?.let { localAssetsRoot ->
    require(localAssetsRoot.resolve("assets").isDirectory && localAssetsRoot.resolve("resources").isDirectory) {
        "Runtime-local asset directory must contain assets/ and resources/"
    }
    android.sourceSets.getByName("debug").assets.srcDir(File(localAssetsRoot, "assets"))
    android.sourceSets.getByName("debug").resources.srcDir(File(localAssetsRoot, "resources"))
}

androidComponents {
    onVariants(selector().all()) { variant ->
        val task = tasks.register<GenerateCompatibilityTargets>(
            "generate${variant.name.replaceFirstChar(Char::uppercase)}CompatibilityTargets",
        ) {
            sourceDirectory.set(rootProject.layout.projectDirectory.dir("compatibility"))
            outputDirectory.set(layout.buildDirectory.dir("generated/compatibility-assets/${variant.name}"))
        }
        checkNotNull(variant.sources.assets).addGeneratedSourceDirectory(
            task,
            GenerateCompatibilityTargets::outputDirectory,
        )
    }
}

dependencies {
    compileOnly(libs.libxposed.api)
    implementation(project(":compat-core"))
    implementation(libs.kotlinx.serialization.json)
    testImplementation(libs.junit.jupiter)
    testImplementation(kotlin("test"))
    testRuntimeOnly(libs.junit.platform.launcher)
}

tasks.withType<Test>().configureEach {
    useJUnitPlatform()
}

tasks.register("verifyModuleMetadata") {
    dependsOn("assembleDebug")

    doLast {
        val apk = layout.buildDirectory.file("outputs/apk/debug/app-debug.apk").get().asFile
        check(apk.isFile) { "Debug APK was not produced: $apk" }

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
            val expectedScopePackages = runtimeLocalAssetsRoot?.resolve("resources/META-INF/xposed/scope.list")
                ?.takeIf(File::isFile)
                ?.readLines()
                ?.map(String::trim)
                ?.filter(String::isNotEmpty)
                ?: listOf("com.tencent.mm")
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

            val sourceTargets = rootProject.file("compatibility").walkTopDown()
                .filter { it.isFile && it.name == "targets.json" && it.parentFile.parentFile == rootProject.file("compatibility") }
                .toList()
            val packagedTargets = archive.entries().asSequence()
                .filter { it.name.matches(Regex("assets/compatibility/[a-z][a-z0-9_-]*/targets\\.json")) }
                .associateBy { it.name.removePrefix("assets/") }
            check(packagedTargets.keys == sourceTargets.map { "compatibility/${it.parentFile.name}/targets.json" }.toSet()) {
                "Packaged compatibility policy directories differ from compatibility/"
            }
            for (source in sourceTargets) {
                val assetPath = "compatibility/${source.parentFile.name}/targets.json"
                val packagedBytes = archive.getInputStream(packagedTargets.getValue(assetPath)).use { it.readBytes() }
                check(packagedBytes.contentEquals(source.readBytes())) {
                    "Packaged profiles differ from $assetPath"
                }
            }
        }
    }
}
