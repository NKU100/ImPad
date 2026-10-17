# QQ 平板登录调用链与冒烟判据

> 历史实验记录：保留当时的观察和局限；现行自动验收使用 [ReDroid 运行环境](../environment/redroid-runtime.md)，本记录不代表当前通过状态。

日期：2026-10-09。目标为 QQ 9.3.35（15560）、TabletHook 1.0，专用 AVD `WeChatPad79LocalSmoke`。未输入账号或凭据，未提交登录请求，未扫描二维码。

## 已验证的边界

TabletHook 的成功日志发生在 Hook 注册之后，不能证明目标方法曾被业务调用，更不能证明服务端接受平板登录。模块只在 `com.tencent.mobileqq:MSF` 进程替换设备类型 Getter。

通过该 APK 的 DEX 引用和反编译，定位到以下链路：

```text
PadUtil.a(Context) → DeviceType.TABLET
  └ AppSetting.o(Context) → 是否平板
      ├ AppSetting.e() → 手机/平板 AppID
      ├ AppSetting.i() → 手机/平板 QUA token
      └ IAppSettingInject.isPadForMultiDeviceLogin(Context)
          └ KernelServiceImpl.startSession(AppRuntime, ...)
              └ InitSessionConfig.deviceType = KPAD / KPHONE
                  └ IQQNTWrapperSession.init(...)
```

AppID 和 token 必须取自 APK 的 AndroidManifest 元数据，不能使用 AppSetting 类中的初始常量。此 APK 的值是：

| 类型 | AppID | QUA token |
| --- | --- | --- |
| 手机 | 537380895 | 0FFA33E378CA9CEF |
| 平板 | 537380934 | 2DFE1F0E7A5AF28F |

## AVD 的独立观测

观察器通过 Frida 附加到 QQ 进程，读取 QQ 自身的配置，并在 `IKernelLoginService.CppProxy.initConfig` 边界记录参数和调用栈。边界观察器调用原方法，保持参数及返回值；没有替换 PadUtil，也没有调用登录方法。配置探针主动读取 AppSetting 的结果，只能证明当时配置值，不能证明自然登录请求使用了它。

| 观测位置 | TabletHook 关闭 | TabletHook 开启 |
| --- | --- | --- |
| MSF 的 AppSetting.o | false | true |
| MSF 的 AppSetting.e | 537380895 | 537380934 |
| MSF 启动时 InitLoginConfig.appid | 16 | 16 |
| MSF 启动时 InitLoginConfig.bid | 空字符串 | 空字符串 |
| MSF 启动时 InitLoginConfig.deviceType | ANDROID | ANDROID |
| MSF 启动时 InitLoginConfig.platform | PLATFORM_ANDROID | PLATFORM_ANDROID |
| MSF 启动时 InitLoginConfig.qua | 空字符串 | 空字符串 |

开启模块后另外读取主进程，`AppSetting.o=false`、`AppSetting.e=537380895`。这是模块只作用于 MSF 的实际表现。

捕获的自然初始化调用链：

```text
NtWtloginInitTask.run
  → MsfCore.initNtWtlogin
  → com.tencent.mobileqq.msf.core.auth.j.a(MsfCore)
  → j.b()
  → IKernelLoginService.CppProxy.initConfig
  → native_initConfig
```

`j.b()` 从 `BaseApplication.getContext().getMSFInterfaceAdapter()` 取得 bid/QUA。运行时 Adapter 为 `com.tencent.common.app.f`，此状态下返回空字符串。因此该初始化事件没有区分平板和手机，不能作为功能冒烟通过判据。ANDROID 是这里的平台字符串，不能据此判断 PHONE/TABLET。

另一个静态路径是 `LoginServiceImpl` 的类初始化：其 InitLoginConfig.bid 来自 `BaseApplication.getContext().getAppId()`，随后调用登录 SDK 的 initConfig。它与上述 MSF 初始化是不同路径。本次未捕获这条路径的自然执行，不能把反编译推断标为运行验证。

## 可以使用的冒烟特征

1. **登录参数被实际消费**：捕获业务自然触发的登录 SDK 调用或请求编码，确认最终使用的 bid/subappid 是该 APK 的平板 AppID，并与关闭模块的手机值对照。仅主动调用 AppSetting.e() 不满足这一断言。
2. **会话以平板类型初始化**：捕获 `IQQNTWrapperSession.init` 的真实参数，断言 `InitSessionConfig.deviceType=KPAD` 和对应平板 AppID；关闭模块时应为 KPHONE。该路径读取账号和票据，本次未登录，尚未运行验证。主进程目前仍为手机配置，因此不能预先假定此断言能够通过。
3. **服务端确认且手机共存**：登录成功后，读取服务端会话注册/在线设备信息，核对当前会话的设备身份，并验证原手机会话仍在线且可收发消息。这才足以证明平板登录的核心效果。

可追踪的服务端回包入口为 `RegisterProxySvcPackHandler.a3(nt_register_proxy.PushParams)`：读取 `online_infos` 的 `client_type`、`client_type_new`、`online_stat`、`instance_id`。QQ 自身 QLog 包含 `NT onReceive client_type` 等字段，但该日志使用级别 2，不能假定正式版一定输出。数字设备类型映射及当前实例关联需要运行验证，不能仅根据名字或一个在线记录推断平板身份。

CI 无账号环境可以验证第一层（前提是存在无需凭据即可自然触发的有效请求），以及实际调用发生的客户端配置层。不能将 Hook 注册或配置探针通过写成“平板登录成功”。真正的平板登录和手机共存，需要有凭据的集成验证。

## 原始证据与恢复

本机实验目录为 `work/qq-callchain/`，包含 DEX 交叉引用、目标类反编译、观察器及 JSONL：`pad-runtime.jsonl`、`nonpad-runtime.jsonl`、`pad-login-init-early.jsonl`、`nonpad-login-init.jsonl`、`restored-pad-runtime.jsonl`、`restored-main-runtime.jsonl`。其中文件名 early 仅为实验命名，该次执行使用 Java.perform 观察器，不代表证明了所有更早的调用均被捕获。

实验结束恢复 TabletHook 开启，重新启动 QQ 并确认 MSF 的平板配置恢复。没有修改模块实现或提交代码。

## 虚构账号密码点击实验

在用户授权后，于同一 AVD 打开账密登录页，勾选协议，输入虚构 QQ 号和测试密码，点击登录。第一次输入 `1000000000`，界面提示“请输入正确的账号”；第二种输入 `123456789`，界面提示“账号或密码错误，请重新输入或找回密码”。这些数字仅为实验输入，不代表测试账号的归属。

安装了主进程及 MSF 的 `IKernelLoginService.CppProxy.passwordLogin`、`WtloginHelper.GetStWithPasswd` 拦截器，随后补充 `WtloginServlet.service` 入口观察；观察器报告就绪，但点击时没有捕获到目标调用。尝试禁用 ART 优化的观察器也未获得有效结果。因此，不能根据错误文案判定究竟是本地校验还是服务端校验，不能证明请求没有发出，也未取得实际登录请求中的 AppID。此次未完成有效的模块开关请求参数对照，不计为登录参数冒烟通过。

证据保存在本机 `work/qq-callchain/pad-attempt-submit.jsonl`、`pad-attempt-servlet.jsonl`、`filled2.xml`、`result3.xml`。实验没有真实凭据，也没有成功登录。结束时停止 QQ 并停止临时 Frida 服务；TabletHook 保持开启。后续需要先准确定位这版 QQ 的实际账密登录入口，再进行入口覆盖和发送前拦截验证。

## AppSetting.e 的实际消费者补充

进一步静态追踪确认，这个 Getter 并非只用于展示或统计：

- `BaseApplicationImpl.getAppId()` 直接返回 `AppSetting.e()`。`LoginServiceImpl` 初始化登录配置时，将 `BaseApplication.getContext().getAppId()` 写入 `InitLoginConfig.bid`，交给 `IKernelLoginService.initConfig`。在哪个进程执行，取值就来自哪个进程的 AppSetting；当前主进程仍是手机值。
- `PhonePasswordLoginMainline.getSaltUinList()` 和 `LoginServletServiceImpl.getStViaPhonePwdLogin()` 将 `AppSetting.e()` 写入 Intent 的 `subappid`；后一条经 WtloginServlet 读取后构造登录消息。不能因为 MSF 的 Getter 是平板值，就假定主进程先构造的 subappid 自动变为平板值。
- 传统 MSF 密码登录中，`com.tencent.mobileqq.msf.core.auth.k` 的登录方法读取 `ToServiceMsg.getAppId()`，把它作为客户端 AppID 参数传入 `WtloginHelper.GetStWithPasswd`。消息 AppID 在 `com.tencent.mobileqq.msf.sdk.h.e(ToServiceMsg)` 发出 Binder 请求前，取自发起进程 SDK 保存的 appid；这与 MSF 当前主动读取的 Getter 值不是同一个观测点。
- 在 MSF 发送编码链路，`com.tencent.mobileqq.msf.core.s.e(ToServiceMsg)` 调用 `CodecWarpper.nativeEncodeRequest` 时，同时传入 `ToServiceMsg.getAppId()` 和 `BaseApplication.getContext().getAppId()`。后者直接连到 AppSetting.e；因此在 MSF 执行这条链路时，模块修改的值能够进入 native 请求编码参数。但本次尚未捕获实际账密登录请求到达此处，也没有解析 native 参数对应的线协议字段，不能宣称所有登录路径都消费了该值。

结论：静态证据确认 AppSetting.e 参与登录配置、部分登录请求参数及 MSF 编码；MSF 返回平板 AppID 是合理的配置冒烟判据。若要证明登录请求实际使用平板值，应在 nativeEncodeRequest 边界或实际密码登录入口记录参数，明确区分消息 AppID、MSF 本地 AppID、登录业务 appid=16 和 subappid/bid。
