# ADR 0001: 技術スタックと実行モデルの選定

- ステータス: 採用
- 日付: 2026-08-01
- 関連: `PROJECT_SPEC.md` §18, §27 / Issue #1

## コンテキスト

`PROJECT_SPEC.md` §27 に「初回実装時に決定する必要がある事項」として未確定項目が列挙されていた。実装着手前にこれらを確定する必要があった。

制約として以下があった。

- 単一ユーザーの個人利用であり、追加の月額課金を発生させたくない
- 実行環境は WSL2 上のローカルマシン（RTX 4050 Laptop 6GB VRAM / i7-13700H 20 コア / RAM 16GB）
- サーバーを常時起動しておきたくない。使うときだけ立ち上げたい
- 記事の言語を限定しないため、多言語およびクロスリンガルの検索性能が必要

## 決定

### LLM: Claude Code CLI を headless で使用する

要約・分類・翻訳・推薦理由生成には、Claude Code CLI を `--print --output-format json` で subprocess として実行する。
ツールの無効化方法は当初 `--allowedTools ""` としていたが、実測で効果がないことが判明したため
[ADR 0002](0002-llm-tool-isolation.md) で訂正した。

- 追加の API 課金が発生しない
- `--output-format json` により構造化出力をパースできる
- ツールを無効化する。記事本文は非信頼入力であり、`PROJECT_SPEC.md` §21 の「本文をツール実行権限のあるエージェントへ直接渡さない」を満たすために必須。
  **具体的な方法は [ADR 0002](0002-llm-tool-isolation.md) を参照**（`--allowedTools ""` は無効化しない）

`LLMProvider` プロトコルで抽象化し、実装を差し替え可能にする。

#### 検討したが採用しなかった案

- **OpenAI / Anthropic の API を直接利用**: 品質と安定性は高いが従量課金が発生する。無料方針から外れる
- **HEROZ ASK API**: 社内で利用可能だが chat 専用であり embedding エンドポイントを持たない。また `projectId` / `chatId` を要求する会話指向の API で、NDJSON ストリームから構造化 JSON を取り出すためのパース層が余分に必要になる。必要になった時点で `LLMProvider` の実装として追加できるため、MVP では見送る
- **Ollama によるローカル LLM**: 完全無料だが、6GB VRAM で動く規模のモデルでは日本語要約の品質が実用水準に届かない

### Embedding: Qwen3-Embedding-0.6B をローカル GPU で実行する

- 出力 1024 次元、`max_length=8192`、Apache-2.0
- 多言語 MTEB でオープンウェイト最上位帯であり、100 言語以上に対応する
- 32k トークンまで扱えるため記事本文をほぼ切らずに投入できる（実際には attention のメモリ消費を抑えるため 8192 に制限する）
- fp16 で約 1.2GB。RTX 4050 の 6GB に余裕を持って収まる
- Matryoshka 表現学習に対応しており、後から次元を落とす選択肢が残る

#### 検討したが採用しなかった案

- **ruri-v3-310m**: JMTEB 77.2 で日本語 SOTA だが日本語特化のため、英語記事と日本語クエリを突き合わせるクロスリンガル検索が弱い。本サービスは「言語制限なし」が要件なので不適
- **multilingual-e5-large**: 和英混在で実績が多いが 512 トークン制限があり、記事本文をチャンク分割する実装が追加で必要になる
- **bge-m3**: 8192 トークン対応で dense + sparse のハイブリッド検索ができるが、Qwen3 に対する明確な優位が今回の用途では見いだせなかった
- **OpenAI text-embedding-3-large**: 3072 次元で高品質だが従量課金が発生する

A / B / C いずれも 1024 次元で揃うため、DB スキーマは `vector(1024)` で固定し、`EmbeddingProvider` 抽象を通じて後から差し替えられるようにする。

### 記事収集: 固定巡回を主、Web 検索を補完とする

- 公式 RSS / Atom（feedparser）
- Hacker News Firebase API
- GitHub Releases API（`GITHUB_TOKEN` は任意。未設定でも動作する）
- arXiv API
- 国内技術メディア RSS（Zenn / Qiita / はてなブックマーク技術カテゴリ）
- Brave Search API（無料枠 月 2000 クエリ・1 qps）。**API キー未設定時はコレクターを自動 skip する**

一次情報を優先するという目的に対し、Web 検索は二次情報を引きやすい。したがって検索は補完に留める。

### ジョブ基盤: PostgreSQL キュー + 手動トリガー

- `SELECT ... FOR UPDATE SKIP LOCKED` でジョブを取得する
- 定期スケジューラ（APScheduler / cron）は導入しない。巡回は UI の実行ボタンから `POST /api/crawl/runs` で起動する
- Redis / Celery は MVP には過剰

### 起動方式: `./run.sh` 一括起動、ワーカーは backend 内蔵

- `./run.sh` が PostgreSQL コンテナ起動 → backend → frontend を順に立ち上げる
- ジョブワーカーは FastAPI の `lifespan` で asyncio タスクとして同居させる
- **常駐プロセスは PostgreSQL コンテナのみ**。Ctrl-C で backend / frontend が停止し、`./run.sh --stop` で PostgreSQL も停止する
- Embedding モデルも backend プロセスにロードされるため、初回リクエストのみ遅くなる

#### 検討したが採用しなかった案

- **全て Docker Compose 化**: 環境差異には強いが、WSL のコンテナから GPU を使うために nvidia-container-toolkit の設定が必要になりセットアップが重くなる

### フィード UI: Discover 忠実型

- 初回 20 件、cursor ページングによる無限スクロール
- 既読記事は再表示するがスコアを減点する
- 保存（+0.5）と Good（+0.8）は別アクションとして保持する（`PROJECT_SPEC.md` §7.1 の重み表に対応）
- Bad 理由は任意。未選択でも Bad は成立する

### データ保持

- 記事本文は DB に内部保存し、外部には表示しない。プロンプト改善時の再解析と重複判定に必要
- リンク切れ記事は `is_dead` フラグによるソフト削除とし、履歴と関心プロファイルを壊さない
- LLM 失敗時は 3 回リトライ（指数バックオフ）
- 構造化ログは 90 日保持

### 認証

MVP では認証を設けない。ただし全テーブルに `user_id` を持たせ、将来のマルチユーザー化を妨げない。

## 帰結

### 利点

- 追加の月額課金がゼロで運用できる
- 常駐プロセスが PostgreSQL のみで、ローカルマシンのリソースを占有しない
- Provider 抽象により、LLM / Embedding / 検索の各要素を個別に差し替えられる

### 欠点とリスク

- Claude Code CLI に依存するため、CLI の仕様変更やレート制限の影響を受ける。緩和策として `LLMProvider` 抽象を用意する
- Embedding モデルの初回ダウンロードに約 1.2GB かかる
- 定期巡回がないため、ユーザーが巡回を実行しない限りフィードは更新されない（意図した挙動）
- GPU が使えない環境では Embedding が CPU にフォールバックし、大幅に遅くなる

### 既知の未解決事項

- `frontend` の devDependency である `eslint@9` が内部で `minimatch@3` → `brace-expansion@1.x` に依存しており、`GHSA-mh99-v99m-4gvg`（glob 展開の DoS）の対象になる。1.x 系に修正版が存在しないため受容する。判断の根拠は次のとおり。
  - lint ツールのみで使われ、アプリケーションのバンドルには含まれない
  - 展開対象の glob パターンは自前の ESLint 設定であり、攻撃者が制御できない
  - `brace-expansion@2` と `@5` は package.json の `overrides` で修正版へ固定済み

- ブロックリスト方式の IP 検証（`validate_url` / `PinnedIPTransport`）を万一すり抜けた場合、
  到達した内部ホストの応答は記事本文として保存され、**利用者本人の画面には表示される**。
  外部へ漏れないというだけで、内部情報がアプリ内に取り込まれること自体は起きる

- MVP では認証を設けないため、API を実装する際に CORS 許可オリジンを限定しないと、
  悪意ある Web ページからブラウザ経由で任意の URL を送り込まれうる。
  API 実装時（Issue #12）にこの前提を再検証すること
