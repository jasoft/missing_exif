# 项目协作规则

## 沟通规则

- 必须使用中文与用户沟通。

## Git 自动提交流程

- 每次完成代码或文档修改后，默认自动执行以下步骤：
  1. 运行 `ruff check .`，必须通过。
  2. 运行 `pyright`，必须通过。
  3. 执行 `git add -A`。
  4. 执行 `git commit -m "<type>: <summary>"`。
  5. 执行 `git push origin main` 推送到 GitHub。
- 仅当用户明确要求“不要提交”或“不要推送”时，才跳过对应步骤。
