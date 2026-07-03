---
name: fix_bug_device_verify
description: >
  fix_bug 修复链路的真机校验收官阶段。fix_bug 完成代码修复后，自动读取其产出的
  缺陷简报和修复方案，优先连接真机（hdc/adb/idevice_id），构建安装、执行反馈环、
  抓取运行时日志、判定修复是否成功。真机不可用时自动回落模拟器模式。
  适用于用户说「上真机验证」「校验修复」「跑一下看修没修好」等触发场景。
license: MIT
agent_created: true
---

# fix_bug_device_verify

fix_bug 链路的最后一个阶段：把纸面上的修复丢到真机上跑一遍，拿日志说话。

核心原则：**真机优先，模拟器兜底**。不登录设备就不算修复完成。

> 使用前请先在 GUI 中输入目标 App 的进程名（如 `your.app.package`），本 skill 所有命令中的 `<process>` 占位符均指该输入。

---

## 前置条件

本 skill 必须在 fix_bug 完成全部五个阶段后调用。它依赖 fix_bug 产出的两份数据：

| 输入 | 来源 | 内容 |
|------|------|------|
| 缺陷简报 | fix_bug PHASE 0 | 平台/端、复现步骤、失败信号、预期行为 |
| 修复方案 | fix_bug PHASE 2-3 | 修复落点、改动 diff、反馈环定义 |
| 目标进程名 | 用户输入 | 如 `com.example.app`、`your.package.name` |

如果 fix_bug 未完成就去验证，本 skill 应拒绝执行并提示先完成修复。

---

## PHASE D1 — 设备检测与选择

### D1.1 真机优先探测

按客户端类型依次尝试，`<process>` 替换为用户输入的进程名：

**HarmonyOS:**
```bash
hdc list targets                                    # 列出在线设备
hdc -t <deviceId> shell pidof <process>             # 确认 App 进程存活
```

**Android:**
```bash
adb devices                                         # 列出在线设备
adb -s <serial> shell pidof <process>
```

**iOS:**
```bash
idevice_id -l                                       # 列出在线设备
idevicesyslog | grep <process>                      # 确认进程
```

### D1.2 判定与回落

```
真机在线 && App 进程存活 → 走 D2（真机模式）
真机在线 && App 未启动   → 启动 App 后走 D2
真机不在线               → 回落 D4（模拟器模式）
```

**回落时必须明确告知用户**：「真机未连接，已回落模拟器模式。模拟器无法覆盖硬件相关缺陷（音频路由、GPU 渲染、传感器），建议提供真机后重新验证。」

---

## PHASE D2 — 真机构建与安装

### D2.1 环境隔离

遵循所有真机校验 skill 的通用规范：**不污染用户家目录**。

```bash
# 隔离 HOME
export HOME=<workspace>/.workbuddy/test-home
mkdir -p $HOME

# DevEco / Android SDK 环境变量（按平台）
# HarmonyOS:
export DEVECO_SDK_HOME=/Applications/DevEco-Studio.app/Contents/sdk
export HDC_SDK_DIR=$DEVECO_SDK_HOME/default/openharmony/toolchains
export JAVA_HOME=/Applications/DevEco-Studio.app/Contents/jbr/Contents/Home

# Android:
export ANDROID_HOME=~/Library/Android/sdk
export JAVA_HOME=$(/usr/libexec/java_home)
```

### D2.2 构建

按平台选择构建命令，关键判据：`BUILD SUCCESSFUL`。

**HarmonyOS:**
```bash
hvigorw assembleHap --mode module -p product=default -p buildMode=release
```

**Android:**
```bash
./gradlew assembleDebug
```

### D2.3 安装

```bash
# HarmonyOS
hdc -t <deviceId> install -r <path/to/xxx.hap>

# Android
adb -s <serial> install -r <path/to/app-debug.apk>
```

安装后确认：
- `hdc -t <deviceId> shell pidof <process>` 返回新 PID
- 新 PID ≠ 旧 PID（确认重新安装生效）

---

## PHASE D3 — 真机反馈环执行

> 这是整个 device verify 的核心。D3 把 fix_bug PHASE 1 的反馈环搬到真机上真实执行。

### D3.1 清日志缓冲

```bash
# HarmonyOS
hdc -t <deviceId> shell hilog -r

# Android
adb -s <serial> logcat -c
```

### D3.2 注入验证日志标记

如果 fix_bug 在 PHASE 1 定义了日志标记（如 `[VERIFY-FIX-xxx]`），先确认代码里已包含。
如果没有，按以下规则**临时追加**（验证后必须删除）：

```typescript
// 在反馈环的判定点加：
const TAG = '[VERIFY-FIX]';
console.log(`${TAG} signal=${signalName} value=${actualValue} expected=${expectedValue}`);
```

标记规范：
- 前缀统一：`[VERIFY-FIX]`
- 包含：信号名、实际值、期望值
- 写在反馈环的红色/绿色判定点，不要在无关路径加

### D3.3 启动 App 并执行复现步骤

1. 启动 App：`hdc -t <deviceId> shell aa start -a <EntryAbility> -b <process>`
2. 等待 App 完全启动（3-5 秒）
3. 按照缺陷简报中的「复现步骤」在真机上操作
4. 如果复现步骤需要自动化，优先用：
   - `hdc shell uinput` 模拟触摸（HarmonyOS）
   - `adb shell input` 模拟触摸（Android）
5. 抓取日志窗口：20-40 秒

### D3.4 抓取日志

```bash
# HarmonyOS
hdc -t <deviceId> shell hilog > /tmp/verify_fix_hilog.txt

# Android
adb -s <serial> logcat -d > /tmp/verify_fix_logcat.txt
```

### D3.5 日志分析

按三层逐级检索：

**第一层 — 验证标记命中：**
```bash
grep '\[VERIFY-FIX\]' /tmp/verify_fix_hilog.txt
```

**第二层 — 失败信号是否消失：**
对照缺陷简报的「失败信号」，在日志中检索原失败模式是否仍出现：
```bash
grep -i '<原失败关键词>' /tmp/verify_fix_hilog.txt
```

**第三层 — 预期行为是否出现：**
对照缺陷简报的「预期行为」，检索正面信号：
```bash
grep -i '<预期行为关键词>' /tmp/verify_fix_hilog.txt
```

**日志分层防误判**（from 多个真机校验 skill 教训）：
- 不把应用层文本 / OCR 文本中的关键词当作系统错误
- 先看 UI/VM 层日志 → 适配层 → 连接层，不要上来就看底层
- 日志中出现「麦克风权限」「网络」等词不等于权限/网络错误

### D3.6 三条件组合判定

参考真机校验 skill 的判定模式，修复成功需**同时满足**：

| 条件 | 判定 |
|------|------|
| 原失败信号不再出现 | ✅ 信号消失 |
| 预期行为在日志中可观测 | ✅ 行为达标 |
| `[VERIFY-FIX]` 标记显示实际值 = 期望值 | ✅ 精确匹配 |

**缺一不可下结论为「已修复」。**

判定结果分三档：

```
✅ FIXED       — 三条件全部满足
❌ NOT_FIXED   — 原失败信号仍出现
⚠️ INCONCLUSIVE — 日志缺失 / 信号模糊 / 仅部分满足
```

---

## PHASE D4 — 模拟器回落模式

真机不可用时，在模拟器上做可覆盖的校验。

### D4.1 模拟器启动

按平台选择模拟器：
- **HarmonyOS**: DevEco Studio 自带模拟器
- **Android**: `emulator -avd <avd_name>`
- **iOS**: `xcrun simctl boot <device_udid>`

### D4.2 模拟器能验证什么

模拟器有效范围：
- ✅ 逻辑流程正确性（代码路径走到位）
- ✅ UI 状态流转（toast / 页面跳转 / 按钮状态）
- ✅ 数据层行为（网络请求参数、回调触发）
- ❌ 硬件相关问题（音频路由、GPU、传感器）
- ❌ 性能问题（真机 Memory/CPU 无法模拟）
- ❌ 真机特有的崩溃（OOM、系统限制）

### D4.3 模拟器判定

模拟器模式下，三条件放宽为：

| 条件 | 判定 |
|------|------|
| 代码路径正确（走的是修复后分支） | ✅ 路径正确 |
| UI 状态符合预期 | ✅ UI 达标 |
| 无逻辑崩溃 | ✅ 无 crash |

模拟器通过的结论必须附带「适用场景受限」声明，标注哪些维度无法覆盖。

---

## PHASE D5 — 清理回滚

遵循真机校验五步闭环的最后一步：**即时间滚**。

完成判定后立即：

1. **删除临时验证日志标记**：`grep -rn 'VERIFY-FIX'` → 确认无残留
2. **删除临时抓取的日志文件**：`/tmp/verify_fix_*.txt`
3. **回滚临时环境**：如果为了构建修改了 HOME / npmrc，恢复原状
4. **复核**：`grep 'VERIFY-FIX'` 在源码中无命中

**如果不清理就提交，这些临时标记会污染代码库，后续排查者会困惑「这是什么验证标记」。**

---

## 输出产物

| 产物 | 格式 | 路径 |
|------|------|------|
| 验证结论 | 文本 | 直接输出 + `fix_result.json` |
| 真机日志 | txt | `/tmp/verify_fix_<client>.txt` |
| 日志分析摘要 | json | `.workbuddy/verify_output/analysis.json` |

### 结论报告格式

```
=== fix_bug Device Verify 报告 ===
平台: <HarmonyOS/Android/iOS>
设备: <serial> (真机 / 模拟器)
修复项: <缺陷简报摘要>
复现步骤: <复现步骤>
日志窗口: <抓取起止时间>
验证标记命中: <命中数>
原失败信号: <消失/仍出现>
预期行为: <达标/未达标>
精确匹配: <实际值 vs 期望值>
判定: ✅ FIXED / ❌ NOT_FIXED / ⚠️ INCONCLUSIVE
证据附件: /tmp/verify_fix_xxx.txt
```

---

## 为什么必须是真机优先

- 模拟器不跑真实音频/视频/传感器硬件栈 → 硬件相关 bug 无法复现
- 模拟器内存与真机 OOM 阈值完全不同 → 稳定性验证无效
- 模拟器没有系统级限制（权限弹窗、后台限制）→ 集成场景验证缺失
- 真机日志（hilog/logcat）包含系统层信息，模拟器日志缺少这些

**结论：模拟器只覆盖逻辑层，不覆盖硬件层。凡涉及音频、视频、GPU、传感器、OOM、权限的 bug，模拟器验证无意义。**
