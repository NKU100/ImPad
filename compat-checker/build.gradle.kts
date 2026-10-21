import org.gradle.api.tasks.JavaExec

plugins {
    alias(libs.plugins.kotlin.jvm)
    alias(libs.plugins.kotlin.serialization)
    application
}

kotlin {
    jvmToolchain(21)
}

application {
    mainClass = "io.github.nku100.impad.checker.MainKt"
}

tasks.named<JavaExec>("run") {
    workingDir = rootProject.projectDir
    environment(
        "DEXKIT_RUNTIME_DIR",
        rootProject.file("${rootProject.extra["dexkitOutputRoot"]}/${rootProject.extra["dexkitHostPlatform"]}").absolutePath,
    )
}

tasks.test {
    environment(
        "DEXKIT_RUNTIME_DIR",
        rootProject.file("${rootProject.extra["dexkitOutputRoot"]}/${rootProject.extra["dexkitHostPlatform"]}").absolutePath,
    )
}

dependencies {
    implementation(project(":compat-core"))
    implementation(project(":compat-rules"))
    implementation(
        files(rootProject.file("${rootProject.extra["dexkitOutputRoot"]}/${rootProject.extra["dexkitHostPlatform"]}/dexkit.jar"))
            .builtBy(rootProject.tasks.named("prepareDexKitForHost")),
    )
    implementation(libs.flatbuffers.java)
    implementation(libs.kotlinx.serialization.json)
    testImplementation(kotlin("test"))
}
