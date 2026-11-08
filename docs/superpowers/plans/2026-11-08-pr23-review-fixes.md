# PR-23 review 修改 — 执行计划

> 给执行代码的 agent：在 `codex/pr23-web` 的当前工作区上改，**不要 commit**。改完跑 `make check` 和 `make web-build`，
> 结尾列出改动文件、手写代码行数变化、与本计划不一致的地方。不改后端。

## 1. 样式改用 Tailwind（主要工作）

现状：`tailwindcss` 已安装并在 `globals.css` 里 `@import`，但组件里一个 Tailwind 类都没用，全部样式都写在 432 行手写 CSS 的语义类里（`.panel`、`.stack`、`.button`、`.field` 等）。这偏离了 CLAUDE.md 的技术栈和原计划，PR-24 / PR-25 还要在此基础上加页面，现在就要统一。

要求：

- 组件里直接写 Tailwind 工具类。重复出现的组合（按钮的主要/次要/危险样式、输入框、面板）抽成 `src/components/ui/` 下的小组件（如 `Button`、`Panel`、`Field`），或 `src/lib/styles.ts` 里的类名常量；不要用 `@apply` 重建一套语义类。
- `globals.css` 只保留：`@import "tailwindcss";`、`@theme` 里的颜色/字体变量（沿用现有色值 `--ink`、`--muted`、`--accent`、`--border`、`--background`，改成 Tailwind v4 的 `--color-*` 写法，组件里用 `text-ink`、`bg-background` 这类类名）、`body` 基础样式、`:focus-visible`。目标 ≤ 60 行。
- 视觉效果保持现有设计（颜色、间距、圆角、窄屏不横向滚动），不需要像素级一致。
- 可访问性属性（`role`、`aria-*`、`label`）原样保留。

## 2. 根布局与首页改成服务端组件

- `src/app/layout.tsx` 去掉 `"use client"`，导出 `metadata`（`title: "面试练习室"`，`description` 一句话）。现在页面没有 `<title>`。`Link` 在服务端组件里可以直接用。
- `src/app/page.tsx` 去掉 `"use client"`，在服务端 `redirect("/new")`。
- 其他页面若需要不同标题，在 PR-24/25 再加，这次不做。

## 3. `useRequest.cancel()` 后 busy 不复位

`cancel()` 把 `active.current` 置空后，正在进行的那次请求在 `finally` 里发现 `active.current !== controller`，不会 `setBusy(false)`；之后如果没有新请求，页面会一直处于 busy。现在只在卸载和切换 id 时调用，所以还没暴露，但 PR-24 复用这个 hook 时会踩到。

改法：`cancel()` 里同时 `setBusy(false)`（或者在 `finally` 里改成“当前没有活动请求时复位”），并补一个 Vitest：发起请求 → cancel → busy 为 false、不设置 error、`onSuccess` 不被调用。hook 测试如果需要 React 渲染环境，可以把状态逻辑抽成不依赖 React 的小函数再测，不要为此引入 React Testing Library。

## 4. 计划页小修

- 题目区加“全部收起”（与“全部展开”并列，或做成一个切换按钮）。
- 删除题目时同时把它的 id 从 `expanded` 里移除。

## 5. 交付记录

- `docs/PR-23-交付记录.md` 不进仓库（项目里没有这类文件）。把其中“风险与待确认”“没做什么”“手动验证步骤”这几节保留在你的汇报里即可，然后删除这个文件。
- `docs/challenges.md` 的 C25、C26 和 `challenges-plain.md` 的对应段落保留。

## 交付时汇报

- 改动文件列表；`globals.css` 行数；手写代码总行数（同上次口径）。
- `make check`、`make web-build` 结果。
- 用假模型在浏览器里重走一遍主流程的结果（同上次的方式），重点看样式迁移后窄屏和桌面宽度有没有走样。
