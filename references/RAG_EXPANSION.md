# RAG 扩充 / 补全 / 增加查询 — 模板与示例

本文件是 `fix_bug` PHASE 0 的落地模板。把用户的自然语言变成「缺陷简报 + 证据索引 + 查询清单」。

---

## 1. 缺陷简报模板

直接填表，缺失标 `?`：

```
【缺陷简报】
- 平台/端：        <harmony / android / ios / 多端 / 后端>
- 受影响模块：      <feature 目录或类名，可填 ? 由检索反推>
- 复现步骤：        <用户原文，缺失则标 ? 并提问>
- 实际现象：        <具体错误表现>
- 预期行为：        <用户认为应怎样>
- 失败信号：        <报错栈 / UI 错乱 / 卡顿 / 逻辑反了>  ← 建反馈环的命门
- 证据索引：        <下文的检索命中>
```

---

## 2. 三类查询（增加查询）

每类按「先最可能、后兜底」排，拿不准就全跑：

### A. 代码检索（Grep / Glob / Read）
- 报错里的关键符号 → Grep 全仓定位定义与调用点
- 用户点名的功能 → Glob 对应 feature 目录
- 嫌疑行为（如「长按」「语音」「span」）→ Grep 关键词

### B. 文档检索（Read）
- `CONTEXT.md` / `ARCHITECTURE.md`（仓库有就读）
- 相关 ADR、`./docs/**` 下与模块相关的文档
- 缺陷单原文（若用户贴了 issue 链接/编号）

### C. 跨端参考（Grep / Glob）
- 同一功能在别的端：搜 `android/`、`ios/`、`common/` 等目录
- 找「同类守卫」如何判断（如 voice input 的 enable/disable 条件）
- 这是定位「不该这么走」的关键参照

---

## 3. 真实示例（本 skill 来源）

**用户输入（自然语言，很碎）：**
> 鸿蒙，文件侧边栏 copilot，@知识库 之后按住说话能触发语音，不应该。安卓 ios 怎么做的？

**补全后的简报：**
```
- 平台/端：        harmony（参考 android/ios）
- 受影响模块：      inputfield / CopilotInputView（检索反推）
- 复现步骤：        @知识库 → 长按输入框
- 实际现象：        进入语音输入模式
- 预期行为：        @知识库 后不应进入语音
- 失败信号：        长按守卫判空失效，语音被误触发
- 证据索引：        BaseTextArea.ets 长按守卫；CopilotLogic.ets 按钮状态
```

**三类查询：**
- A 代码：`Grep "enableLongPressToAudioInput" "userInputSpans" "getSpans"`
- B 文档：无 CONTEXT.md，读相关 .ets
- C 跨端：`Grep android "isVoiceInputEnabled" "atKnowledgeBases.isEmpty()"`；
  `Grep ios "disableDirectPressForVoice" "length > 0"`

**跨端参考结论（关键）：**
- Android：`isVoiceInputEnabled()` 直接判 `atKnowledgeBases.isEmpty()`
- iOS：`disableDirectPressForVoice()` 判 `inputText.length > 0`（富文本附件也算长度）

→ 鸿蒙的守卫只看 `userInputSpans.length`，而 @知识库 用的是 BuilderSpan，`getSpans()`
取不到它，导致判空失效。根因锁定。

---

## 4. 问答模板（只问真正阻断推进的）

```
为推进修复我需要确认两点（我的默认假设附后）：
1. 复现步骤是否是「@知识库 → 长按输入框」？默认：是。
2. 预期是否「@知识库 后任何情况都不进语音」？默认：是。
如假设不对请直接改，否则我就按默认推进。
```
