# 项目版本号与记忆自进化 — 设计

日期：2026-11-27  
状态：待实现  
范围：版本单一源、发版 bump、CHANGELOG、PROJECT_MEMORY、项目内 evolve skill；GUI 标题显示版本。

## 目标

1. **发版可追溯**：安装包与 GUI 共用同一版本号，方便对照客户反馈。
2. **不绑 Agent 软件**：流程以仓库内文档 + skill 为准；Cursor 等仅可索引，不作为唯一载体。
3. **有选择地沉淀**：不是每次提交都改记忆；只记有价值的问题项与用户偏好。
4. **发版才 bump**：日常修缺陷提交不自动升版本；仅当用户明确「可以发版/打包」时 patch +1。

## 非目标

- 不做强制 git hook 自动 bump（避免误升、噪音）。
- 不把记忆绑在 Cursor User Rules / Codex 专有存储。
- 不要求每次提交都写文档。
- 本期不改 major/minor 语义（默认只 patch）；需要 minor/major 时由人在发版指令里显式说明。

## 单一版本源

| 项 | 约定 |
|---|---|
| 源文件 | 仓库根目录 `VERSION`，单行 semver，如 `1.0.0` |
| 初值 | 与当前 `packaging/installer.iss` 中 `MyAppVersion` 对齐（现为 `1.0.0`） |
| 消费方 | `launcher`（或 GUI 入口）读入后设窗口标题 `向僵尸开炮助手 v{VERSION}`；打包脚本把同一值注入 Inno `#define MyAppVersion` |
| 禁止 | 在 iss / GUI / 多处手写互不同步的版本号 |

开发态（源码直接跑）：读仓库根 `VERSION`。  
打包态：构建时把 `VERSION` 打进产物（复制到 `_internal` 或写入生成的小模块），运行时仍读同一内容。

## 发版流程（bump 时机）

仅当用户明确说「可以发版」「打包」「release」等时执行：

1. 读 `VERSION`，默认 **patch +1**（`1.0.0` → `1.0.1`）；若用户指定 minor/major 则按指定。
2. 写回 `VERSION`；同步 `packaging/installer.iss` 的 `MyAppVersion`（或构建时由脚本注入，避免双写漂移——实现时优先「构建注入、iss 用占位/生成」，若改动过大则「脚本同时改 VERSION + iss」亦可）。
3. 根据自上次发版以来的有意义变更，在 `docs/CHANGELOG.md` 追加一节（中文短列表）。
4. 若本次发版含应沉淀的偏好/坑，同步更新 `docs/PROJECT_MEMORY.md`。
5. 再跑既有 `build_release.bat` / 打包链路。

日常缺陷修复提交：**不**改 `VERSION`，**不**强制改 CHANGELOG；可选由 evolve skill 决定是否只更新 MEMORY。

## 文档形态

### `docs/CHANGELOG.md`

- 按版本倒序。
- 每节：版本号、日期、3～10 条用户可感知变更（修了什么 / 加了什么）。
- 不写实现细节堆砌；细节留给 commit 与代码。

### `docs/PROJECT_MEMORY.md`

活文档，只收**长期有用**的条目，分类建议：

- **偏好**：用户明确的产品/交互取向（例：发版才 bump；失败页必须先认文案再点返回）。
- **易踩坑**：跨模式差异、曾反复修过的坑（例：元素 mode=5 不能无条件走通用 return；衍生 OCR 勿绑 `band_roi`；4选2 卡顶 ROI 需单独标定）。
- **刻意不做**：明确拒绝的方案，避免下次又提（例：不做描述→衍生名映射表）。

每条尽量一行结论 + 必要时一行「为何」；控制篇幅，定期可合并重复项。

## 项目内 skill：自我进化

路径（实现时落地）：`skills/project-evolve/SKILL.md`（随仓库；不依赖 Cursor 全局 skill 目录）。

### 触发

- 用户要求提交 / 修完收尾 / 明确发版打包时，执行本 skill。
- Agent 无关工具链也可人工按 skill 清单操作。

### 沉淀判定（多数提交应为「跳过」）

对当前 diff + 说明逐项问：

1. 是否表达了**用户偏好**或长期产品取向？
2. 是否是**反复出现**或跨模式易再踩的坑？
3. 是否记录了**刻意不做**或重要约束（防回归设计）？

任一为「是」→ 追加/改写 `PROJECT_MEMORY.md` 对应条。  
全为「否」→ **不改**记忆文档。

发版触发时另做：bump + CHANGELOG；记忆仍按上面三条筛选，不强行凑字数。

### 反模式

- 把每次 bug 修复过程全文贴进 MEMORY。
- 无用户偏好、无跨场景约束的纯局部改动也写文档。
- 未获「发版/打包」指令却 bump VERSION。

## GUI 与安装包

- 主窗口标题：`{产品名} v{VERSION}`（选 A）。
- 安装包 `AppVersion` / `VersionInfoVersion` 与 `VERSION` 一致。
- 不在本期做独立「关于」对话框（YAGNI）；标题足够回溯。

## 组件与职责

| 单元 | 职责 |
|---|---|
| `VERSION` | 唯一版本字符串 |
| 小读取辅助（可在 `launcher.py` 内函数） | 解析版本；标题拼接；缺文件时回退 `0.0.0-dev` 并告警 |
| `packaging/build_release.py`（或薄封装） | 发版 bump、注入 iss、可选校验 VERSION 与产物一致 |
| `docs/CHANGELOG.md` | 发版履历 |
| `docs/PROJECT_MEMORY.md` | 偏好与坑 |
| `skills/project-evolve/SKILL.md` | 何时 bump、何时沉淀的检查清单 |

## 测试要点

- 读取 `VERSION` → 标题含该字符串。
- bump 辅助：给定 `1.2.3` → patch 得 `1.2.4`（纯函数可单测）。
- 构建预检或单测：iss 中版本与 `VERSION` 一致（若采用双写同步策略）。
- skill 无自动单测；靠清单与一次人工演练（模拟「普通提交跳过」vs「发版 bump」）。

## 成功标准

- 用户说打包时，版本号 +1，CHANGELOG 有对应节，安装包与 GUI 标题同号。
- 普通缺陷提交不升版本；仅当判定有价值时才改 PROJECT_MEMORY。
- 换一台机器 / 不用 Cursor，只要打开仓库 skill + docs 仍能按同一流程做。

## 实现顺序（供后续 plan 拆分）

1. 引入 `VERSION` + GUI 标题读取；iss 与源对齐。
2. 建 `CHANGELOG.md`、`PROJECT_MEMORY.md` 初稿（可从近几次已知坑 seeded）。
3. 发版 bump/注入脚本接入现有 `build_release`。
4. 写入 `skills/project-evolve/SKILL.md`。
5. 回归：标题、版本一致性测试。
