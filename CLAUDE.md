# voice-agent 開発の恒常指示

このリポジトリで作業するエージェント（あなた）への常設ルール。

## 品質の担保（必ず守る）

- **コードを変更したら、コミット前に必ず `make check` を実行する**（ruff lint + mypy 型チェック）。
  エラーが残っているコミットはしない。
- フォーマットは `make format`（ruff）。lint/型/フォーマットの設定はすべて `pyproject.toml` に集約。
- 型チェック対象は本体（`core/` / `server/` / `config.py`）。`train_local/` は対象外。
- 型注釈は段階的に増やす方針。新規・変更コードには可能な範囲で型注釈を付ける。

この担保は `AI生成コードのQA戦略`（「型とlintは無料の担保」「必ず実行される仕組みを入れる」）に基づく。
commit 時は `.pre-commit-config.yaml` のフックでも同じチェックが自動で回る。

## 依存管理

- 実行依存も dev 依存も **`pyproject.toml` の `[dependency-groups]`** に集約（`requirements.txt` は廃止）。
- 開発環境セットアップ: `make dev-install`（`pip>=25.1` 必要。重い実行依存は入らない）。
- 設定値は `config.py` ではなく **`.env`** で変える（`config.py` は env 駆動のローダ。値を直接書き換えない）。
