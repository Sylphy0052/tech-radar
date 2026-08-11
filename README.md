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

`run.sh` は PostgreSQL コンテナを起動し、backend（<http://localhost:18700>）と frontend（<http://localhost:13700>）を立ち上げる。Ctrl-C で backend / frontend が停止する。

ポートは `.env` の `BACKEND_PORT` / `FRONTEND_PORT` で変更できる。変更する場合は CORS 許可オリジン（`CORS_ALLOW_ORIGINS`）と frontend の API ベース URL（`NEXT_PUBLIC_API_BASE_URL`）も揃える。

listen するインターフェースは `.env` の `BIND_HOST`（既定 `127.0.0.1`）で変更できる。認証を置いていないため、`0.0.0.0` などへ広げると同じ LAN の誰でも API と UI に触れる状態になる。広げる前に [PROJECT_SPEC.md](PROJECT_SPEC.md) の §24「認証を置かない前提で守る対策」を読むこと。

別の端末のブラウザから UI を開くつもりなら `NEXT_PUBLIC_API_BASE_URL` も揃える。この値は画面上の JavaScript が API を呼ぶ宛先で、既定の `http://localhost:18700` のままだと、その端末自身の localhost を見にいって失敗する。

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
