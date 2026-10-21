# 第三方组件声明

以下清单基于模块 `debugRuntimeClasspath` 的实际解析结果，并包含由锁定 DexKit AAR/native 输入额外打包的组件；许可证依据对应 POM、JAR 和固定上游源码材料核对。第三方组件保留各自原有许可证；本项目的许可选择不会覆盖这些独立组件的权利。

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
| `com.google.flatbuffers:flatbuffers-java:23.5.26` | Apache-2.0；[v23.5.26 上游许可证](https://github.com/google/flatbuffers/blob/v23.5.26/LICENSE) |
| `org.luckypray:dexkit:2.3.0` | Apache-2.0; `Core/` is LGPL-3.0 |
| `dev.rikka.ndk.thirdparty:cxx:1.2.0` | Apache-2.0 |

## 随 APK 提供的许可证材料

许可证原文位于模块 APK 的 `assets/licenses/`，仓库中的对应文件：

- [Apache License 2.0](app/src/main/assets/licenses/Apache-2.0.txt)：Apache 官方完整文本。
- [DexKit Core LGPL-3.0 License](app/src/main/assets/licenses/DexKit-LGPL-3.0.txt)：从固定的 DexKit 源码提交 `c9cd12a14b75409bebd2f73e4dfc5ff575df3eb8` 原样复制；DexKit 其余源码按 Apache-2.0 授权。
- [smali-dexlib2 原始 LICENSE](app/src/main/assets/licenses/smali-dexlib2-LICENSE.txt)：从 3.0.10 JAR 原样提取，保留其中 BSD、AOSP／Guava 版权及 Apache 声明全文。
- [Checker Framework qualifiers 原始 LICENSE](app/src/main/assets/licenses/checker-qual-LICENSE.txt)：从 3.12.0 JAR 原样提取，保留原始版权和 MIT 文本。

这些依赖的 JAR 未提供其他独立 NOTICE 文件。表中 Guava、failureaccess 和 listenablefuture 的 Apache-2.0 元数据继承自各自父 POM。清单中的空 artifact 不包含运行实现。

## 框架与开发工具

`io.github.libxposed:api:102.0.0` 使用 Apache-2.0，以 `compileOnly` 声明，由运行框架提供，不作为模块运行库打包。

Gradle／Android 构建工具、测试依赖、JADX、DexSQL、ReDroid 和运行框架的安装包属于开发或 CI 环境，不随模块 APK 分发；其原有许可证仍适用于相应工具和安装包。目标应用 APK 不随模块分发。

依赖升级时应重新解析运行依赖，核对原始许可证／NOTICE，并更新本清单和 APK 中的材料。
