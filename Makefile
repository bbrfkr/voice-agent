# voice-agent 開発タスク。
# 依存・ツール設定は pyproject.toml に集約（PEP 735 dependency-groups, pip>=25.1）。
#
# 「型とlintは無料の担保」を必ず回す入口。コード変更後は `make check` を実行する。

.PHONY: lint format typecheck check dev-install

lint:        ## ruff で lint
	python3 -m ruff check

format:      ## ruff でフォーマット
	python3 -m ruff format

typecheck:   ## mypy で型チェック（本体のみ）
	python3 -m mypy

check: lint typecheck   ## commit 前の本命（lint + 型）

dev-install: ## 開発ツールを導入し pre-commit を有効化（macOS で重い実行依存は入らない）
	python3 -m pip install --upgrade "pip>=25.1"
	python3 -m pip install --group dev
	python3 -m pre_commit install
	@echo "pip<25.1 で --group が使えない場合: python3 -m pip install ruff mypy pre-commit"
