# voice-agent 開発タスク。
# 依存・ツール設定は pyproject.toml に集約（PEP 735 dependency-groups）。
# ruff / mypy は uv 経由で実行する（dev グループのツールを自動解決）。
#
# 「型とlintは無料の担保」を必ず回す入口。コード変更後は `make check` を実行する。

.PHONY: lint format typecheck check dev-install

lint:        ## ruff で lint
	uv run --group dev ruff check

format:      ## ruff でフォーマット
	uv run --group dev ruff format

typecheck:   ## mypy で型チェック（本体のみ）
	uv run --group dev mypy

check: lint typecheck   ## commit 前の本命（lint + 型）

dev-install: ## 開発ツール（dev グループ）を導入し pre-commit を有効化
	uv sync --group dev
	uv run pre-commit install
