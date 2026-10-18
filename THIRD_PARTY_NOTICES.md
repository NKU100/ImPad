# 第三方组件声明

以下清单来自模块 `debugRuntimeClasspath` 的实际解析结果及对应 POM、JAR 许可证材料。第三方组件保留各自原有许可证；本项目的许可选择不会覆盖这些独立组件的权利。

## 模块运行依赖

| Maven 坐标 | 许可证 |
| --- | --- |
| `org.jetbrains.kotlin:kotlin-stdlib:2.4.20` | Apache-2.0 |
| `org.jetbrains:annotations:13.0` | Apache-2.0 |
| `com.android.tools.smali:smali-dexlib2:3.0.10` | BSD-3-Clause；原文另含 Apache-2.0 组件声明 |
| `com.google.code.findbugs:jsr305:3.0.2` | Apache-2.0 |
| `com.google.guava:guava:31.1-android` | Apache-2.0 |
| `com.google.guava:failureaccess:1.0.1` | Apache-2.0 |
| `com.google.guava:listenablefuture:9999.0-empty-to-avoid-conflict-with-guava` | Apache-2.0；空的冲突消解 artifact |
| `org.checkerframework:checker-qual:3.12.0` | MIT |
| `com.google.errorprone:error_prone_annotations:2.11.0` | Apache-2.0 |
| `com.google.j2objc:j2objc-annotations:1.3` | Apache-2.0 |
| `org.jetbrains.kotlinx:kotlinx-serialization-core-jvm:1.11.0` | Apache-2.0 |
| `org.jetbrains.kotlinx:kotlinx-serialization-json-jvm:1.11.0` | Apache-2.0 |

## 随 APK 提供的许可证材料

许可证原文位于模块 APK 的 `assets/licenses/`，仓库中的对应文件：

- [Apache License 2.0](app/src/main/assets/licenses/Apache-2.0.txt)：Apache 官方完整文本。
- [smali-dexlib2 原始 LICENSE](app/src/main/assets/licenses/smali-dexlib2-LICENSE.txt)：从 3.0.10 JAR 原样提取，保留其中 BSD、AOSP／Guava 版权及 Apache 声明全文。
- [Checker Framework qualifiers 原始 LICENSE](app/src/main/assets/licenses/checker-qual-LICENSE.txt)：从 3.12.0 JAR 原样提取，保留原始版权和 MIT 文本。

这些依赖的 JAR 未提供其他独立 NOTICE 文件。表中 Guava、failureaccess 和 listenablefuture 的 Apache-2.0 元数据继承自各自父 POM。清单中的空 artifact 不包含运行实现。

## 框架与开发工具

`io.github.libxposed:api:102.0.0` 使用 Apache-2.0，以 `compileOnly` 声明，由运行框架提供，不作为模块运行库打包。

Gradle／Android 构建工具、测试依赖、JADX、DexSQL、ReDroid 和运行框架的安装包属于开发或 CI 环境，不随模块 APK 分发；其原有许可证仍适用于相应工具和安装包。目标应用 APK 不随模块分发。

依赖升级时应重新解析运行依赖，核对原始许可证／NOTICE，并更新本清单和 APK 中的材料。
