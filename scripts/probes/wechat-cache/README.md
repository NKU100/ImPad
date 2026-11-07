# 冻结微信缓存目标审计

`FrozenCacheAudit.java` 使用 JVM 21 和项目锁定的 `smali-dexlib2` 直接读取指定 DEX。它不调用模块的语义识别器，也不运行微信或修改缓存。

先将 `smali-dexlib2`、Guava 和 Kotlin 标准库的二进制 JAR 加入 classpath，编译此文件，再运行：

```text
FrozenCacheAudit <APK> <DEX entry> <declaring class descriptor> <field name> <decision method descriptor>
```

固定输入的目标来自此前人工审计：

| profile | DEX | 声明类 | 字段 | 决策方法 |
| --- | --- | --- | --- | --- |
| wechat-3040 | classes12.dex | Lcom/tencent/mm/ui/qj; | f | Lcom/tencent/mm/ui/qj;->B(Lfd5/n0;)Z |
| wechat-3200-fixed | classes13.dex | Lcom/tencent/mm/ui/ok; | f | Lcom/tencent/mm/ui/ok;->C(Lpv5/w0;)Z |

审计要求字段唯一、静态、非 final、boxed Boolean；决策必须是一个参数的 static boolean 方法，有匹配的缓存早返回以及同字段的读取和写入。输出三个 tab 分隔整数：字段 access flags、读取数量、写入数量。缺失或不匹配的目标以非零退出。

运行前应核对完整 APK SHA-256 和指定 DEX SHA-256，分别匹配 `compatibility/semantic-migration/expectations.json` 中的输入和 snapshot。然后将输出记录到 `compatibility/wechat/startup-cache-audit.json`。新期待只在旧完整 Hook 计划上增加显式缓存目标，不重新生成旧 Hook 期待。

这份审计只提供静态 DEX 关系证据，不证明实际登录或 ART 方法来源。

当前 `compatibility/wechat/inner-after-audit.json` 保留上述缓存字段、决策方法、来源 DEX 和直接读写数量，供固定历史输入投影到内层 After 合同。两份原 APK 外层都是直接转发，没有原共享锁，修正时使用声明类作为锁；热更缓存包装若有原 monitor，必须从实际指令关系提取并验证声明，不能复用原包的同步方式。

`wechat_inner_expectations.py` 将新的内层角色加入独立期待，不修改冻结历史表或历史计划。三个实际 Hook 和缓存／monitor 描述符一起参与新执行摘要。CI 的固定回归比较新期待；历史原件仍保留。
