# fix_bug

端到端修 bug 的链路式 skill。用户甩来一段碎碎念（缺陷单、群里一句话、复现步骤缺失），直接去翻代码很容易跑偏。这个 skill 的做法是先把它补全，再沿四段链路推进，每段由对应子 skill 接管。

## 链路

```
RAG 扩充补全 → diagnosing-bugs → codebase-design → ponytail → review
```

每一段都是前一段的输入；前一段没有产出，就别急着开工。

## 每段在干什么

**PHASE 0 — RAG 扩充 / 补全 / 增加查询**

把用户的自然语言变成一份信息齐全、带证据索引的缺陷简报。这一步不做代码改动，只做信息补全与检索。

核心动作：
- 抽出平台、模块、复现步骤、实际现象、预期行为、失败信号
- 生成三类检索查询：代码检索、文档检索、跨端参考
- 缺「复现步骤」或「预期行为」时才问用户，其余能推断就推断

**PHASE 1 — diagnosing-bugs**

建反馈环、复现并最小化、提假设、打点验证、修 + 回归测试、清理。

反馈环要对准「失败信号」，必须能 red、能 green。假设要 3–5 条并排序。

**PHASE 2 — codebase-design**

基于根因找「修复该落在哪个 seam」。优先用方法覆写而不是往基类塞新字段；接口要小；改动要是「深模块」式的。

**PHASE 3 — ponytail**

按 ponytail 的 ladder 落地修复。第一反应问「这改动非加不可吗」；最短 diff 获胜；不写未请求的抽象。

**PHASE 4 — review**

双轴审查：Standards（是否符合本仓库编码规范）+ Spec（是否真修掉了缺陷、有无 scope creep）。

## 为什么是这条链

- RAG 补全解决「输入太碎、易跑偏」——先把问题讲清楚再动刀
- diagnosing-bugs 保证「有证据、不靠猜」地定位根因
- codebase-design 保证「改对地方」，避免补丁叠补丁
- ponytail 保证「改得最小」，杜绝过度工程
- review 保证「改得对、改得合规」，闭环收口

## 真实示例

鸿蒙文件侧边栏 Copilot，@知识库 后长按输入误触发语音。

根因：@知识库 用的是 BuilderSpan，`getSpans()` 取不到它，导致长按守卫的 span 判空失效。

修复：基类留 `isLongPressDisabled()` 钩子、子类用已有的 `curKnowledgeMap.size>0` 覆写，三个文件的最小改动，未动基类字段。

## 安装

把整个 `fix_bug` 目录复制到 `~/.workbuddy/skills/fix_bug/`，重启 WorkBuddy 即可。

## 触发方式

在 WorkBuddy 对话中说「修 bug」「定位这个缺陷」「排查这个问题」或直接贴一段缺陷描述即可触发。

---

Made with 🛠️ by [Akeli7](https://github.com/Akeli7)
