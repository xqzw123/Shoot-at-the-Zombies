---
name: project-evolve
description: >-
  项目版本与记忆自进化。用户要求提交收尾、修完缺陷收尾、或明确发版/打包时使用。
  发版才 bump VERSION；多数提交不改 PROJECT_MEMORY，仅沉淀有价值的偏好与坑。
---

# Project Evolve

跟仓库走，不依赖 Cursor/Codex。配套文档：

- `VERSION` — 单一版本源
- `docs/CHANGELOG.md` — 仅发版追加
- `docs/PROJECT_MEMORY.md` — 偏好 / 易踩坑 / 刻意不做
- 设计：`docs/superpowers/specs/2026-11-27-project-version-and-memory-design.md`

## 何时 bump 版本

**仅当**用户明确说「发版」「打包」「release」等：

```bash
python packaging/build_release.py --bump-patch --check
# 通过后再正常 build / build_release.bat
```

或调用 `app_version.release_bump_patch()`（写 `VERSION` + 同步 `installer.iss`）。

默认 **patch +1**。用户指定 minor/major 时按其说的改，不要擅自升。

日常缺陷修复提交：**禁止**改 `VERSION` / iss 版本 / CHANGELOG（除非同时在发版）。

发版时还要：

1. 在 `docs/CHANGELOG.md` 顶部追加一节（中文短列表，用户可感知变更）。
2. 按下方标准决定是否更新 `PROJECT_MEMORY.md`。
3. 再跑打包。

## 何时沉淀 MEMORY（多数提交应跳过）

看**本次 diff + 说明**，对每条潜在内容问：

1. 是否表达了**用户偏好**或长期产品取向？
2. 是否是**反复出现**或跨模式易再踩的坑？
3. 是否记录了**刻意不做**或重要防回归约束？

- 任一为「是」→ 追加/改写 `docs/PROJECT_MEMORY.md` 对应分类，保持短句。
- 全为「否」→ **不改** MEMORY。

### 不要沉淀

- 单次局部 bug 的逐步排查过程
- 纯测试/格式/依赖 bump
- 已在 MEMORY 里、无新信息的重复叙述

## 提交收尾检查清单

- [ ] 若用户要求发版：已 bump + CHANGELOG；否则 VERSION 未动
- [ ] 按三条标准判断 MEMORY；无价值则跳过
- [ ] GUI 标题仍来自 `app_version.window_title()`（勿写死版本号）
- [ ] 不把流程写进 Cursor 专有配置作为唯一来源

## 反模式

- 未获发版指令却 bump
- 每次修 bug 都改 MEMORY 或 CHANGELOG
- 在多个文件手写互不同步的版本号
