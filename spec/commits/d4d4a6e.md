# d4d4a6e — chore: switch to uv for dependency management

迁移依赖管理工具：pip / requirements.txt → uv / pyproject.toml + uv.lock。

- 删除 `requirements.txt`
- `pyproject.toml` 用 PEP 621 标准 `[project]` + `[project.optional-dependencies]`
- `uv.lock` 入库（lock file 锁定版本）
- README 更新安装步骤：`uv sync` 一行完成

理由：uv 安装快、lock 可靠、跨平台一致。MonoX 仍 `python>=3.10`。