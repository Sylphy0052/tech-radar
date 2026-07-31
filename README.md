# TechRadar

技術記事に特化したパーソナライズド・フィード。気になる記事の URL を登録すると内容を解析し、直近 7 日以内の関連記事を推薦する。公式ドキュメント・リリースノート・原著論文などの一次情報を強く優先し、Good / Bad フィードバックから関心を継続的に学習する。

要件の全体像は [PROJECT_SPEC.md](PROJECT_SPEC.md)、技術選定の根拠は [docs/adr/0001-technology-stack.md](docs/adr/0001-technology-stack.md)、決定事項の一覧は [docs/decisions.md](docs/decisions.md) を参照。

## 構成

```text
backend/    FastAPI + Python (uv 管理)。API とジョブワーカーを同居させる
frontend/   Next.js (App Router) + TypeScript
infra/      Docker Compose (PostgreSQL + pgvector)
docs/adr/   Architecture Decision Record
scripts/    開発用スクリプト (check.sh)
```

## 必要なもの

| ツール | 用途 |
| --- | --- |
| [uv](https://astral.sh/uv) | Python の依存管理・実行 |
| Node.js 22 以上 / npm | frontend のビルドと実行 |
| Docker / Docker Compose | PostgreSQL + pgvector |
| [Claude Code CLI](https://docs.claude.com/en/docs/claude-code) | 要約・分類・翻訳・推薦理由の生成 |
| NVIDIA GPU（任意） | Embedding のローカル実行。無い場合は CPU にフォールバックする |

## セットアップ

```bash
cp .env.example .env   # 必要に応じて値を編集する
./run.sh
```

`run.sh` は PostgreSQL コンテナを起動し、backend（<http://localhost:8000>）と frontend（<http://localhost:3000>）を立ち上げる。Ctrl-C で backend / frontend が停止する。

常駐するのは PostgreSQL コンテナのみ。完全に停止するには次を実行する。

```bash
./run.sh --stop
```

## 開発

lint・整形・型チェック・テストを一括実行する。commit 前に必ず緑にする。

```bash
bash scripts/ai-harness/check.sh
```

個別に実行する場合は次のとおり。

```bash
# backend
cd backend
uv run ruff check .
uv run ruff format --check .
uv run ty check
uv run pytest

# frontend
cd frontend
npm run lint
npm run typecheck
npm test
```

## 開発の進め方

- ロードマップは Issue `Roadmap: TechRadar`（#17）に集約する
- 1 Issue 1 Branch。ブランチ名は `<type>/<IID>/<slug>`
- MR 本文に `Closes #<IID>` を記載する

## ステータス

Phase 1（基盤）を実装中。実装順序は [PROJECT_SPEC.md](PROJECT_SPEC.md) §23 を参照。
