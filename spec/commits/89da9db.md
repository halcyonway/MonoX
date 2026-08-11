# 89da9db — fix(v0.7): use prompt_toolkit for input (fix backspace bug)

**Bug**：input() 在某些 terminal 下 backspace 不工作（cooked mode 行缓冲问题，输出重定向时更明显）。

**修**：

- `prompt_toolkit.PromptSession` 替换 `input()`
- 处理 EOF / Ctrl+D / Ctrl+C 干净（之前 Ctrl+C 抛 KeyboardInterrupt → runtime 半退）
- 多行：可用 `multiline=True`

依赖加 `prompt_toolkit>=3`。

副作用：prompt_toolkit 的 `patch_stdout()` 在 v0.8.2 引入新 bug（Rich ANSI 被吞），见 `185e845`。