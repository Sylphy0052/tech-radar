# 決定事項サマリ

`PROJECT_SPEC.md` §27「初回実装時に決定する必要がある事項」に対する回答。判断の根拠は [ADR 0001](adr/0001-technology-stack.md) を参照。

最終更新: 2026-08-01

## インフラ

| 項目 | 決定 |
| --- | --- |
| デプロイ先 | ローカルのみ。クラウドへは展開しない |
| PostgreSQL | Docker Compose（`pgvector/pgvector:pg17`） |
| Frontend / Backend | 分離する（`frontend/` = Next.js、`backend/` = FastAPI） |
| ジョブ実行基盤 | PostgreSQL ジョブキュー（`FOR UPDATE SKIP LOCKED`）。ワーカーは FastAPI プロセスに同居 |
| 起動方式 | `./run.sh` 一括起動。常駐は PostgreSQL コンテナのみ。`./run.sh --stop` で完全停止 |

## 外部サービス

| 項目 | 決定 |
| --- | --- |
| 要約・分類・翻訳・推薦理由 | Claude Code CLI headless（`--print --output-format json`）。ツール無効化の方法は [ADR 0002](adr/0002-llm-tool-isolation.md) |
| Embedding | `Qwen/Qwen3-Embedding-0.6B` をローカル GPU で実行。1024 次元・`max_length=8192` |
| Web 検索 | Brave Search API 無料枠（月 2000 クエリ・1 qps）。API キー未設定時は自動 skip |
| RSS 以外の収集 | Hacker News API / GitHub Releases API / arXiv API / 国内技術メディア RSS |
| 翻訳 | 専用 API は使わず LLM で行う（要約と同一の呼び出しにまとめてコストを抑える） |
| 社内チャット API | MVP では使用しない（chat 専用で embedding エンドポイントを持たないため） |

## フィード

| 項目 | 決定 |
| --- | --- |
| 更新頻度 | 定期実行なし。UI の巡回実行ボタンから起動する |
| 一度の表示件数 | 初回 20 件 |
| ページング | cursor ページングによる無限スクロール |
| 既読記事の再表示 | 再表示するがスコアを減点する |
| 保存と Good | 分ける（保存 +0.5 / Good +0.8） |
| Bad 理由 | 任意。未選択でも Bad は成立する |
| 情報源選好の推薦スコアへの合成 | 新しい重み項を足さず、`source_authority` の寄与に掛ける係数として合成する（下記） |

### 情報源選好を `source_authority` の係数として合成する理由（Issue #34）

`PROJECT_SPEC.md` §14 の式にある `source_authority` は `source_registry.authority_score`
由来のユーザー横断で静的なスコアである。これに対し Good / Bad の履歴から学習する
ユーザー固有の情報源選好（`user_source_preferences`）を、`recommendation_score` の
7 番目の重み項として足すのではなく、`source_authority` の寄与に掛ける係数として
合成する（`recommendation/ranking.py` の `SourcePreferenceGate`）。

```text
source_authority_contribution =
    source_authority
  × weights.source_authority   (0.30)
  × authority_gate_factor      (既存: 関心一致度が低い公式記事を上位に出さない補正)
  × source_preference_factor   (新規: clamp(1 + weight_scale × effective_weight, min, max))
```

理由:

- 重み項として足すと、合計 1.0 という既存の制約（`recommendation/config.py` の
  `_validate_weights_sum_to_one`）を満たすために既存 6 項目の配分を全面的に
  引き直すことになり、Issue #11 以来調整してきた重み配分と、それを固定している
  既存テストの期待値が一斉に変わる
- 合計の外側で加点・減点する形にすると、`recommendation_score` が [0, 1] の
  レンジから外れ、他の減点（`bad_penalty` 等）との大小比較の目安が崩れる
- 「同じ情報源に対する評価」という意味で `source_authority` と対象が同じであり、
  既存の `authority_gate`（同じ項に掛ける係数）と同じ流儀に揃うため、
  スコア内訳（`recommendations.reasons`）を読むときの解釈も一貫する

係数は上下限（`config/scoring.yaml` の `source_preference.min_factor` /
`max_factor`）で挟む。`positive_weight` は Good のたびに累積し続けるため上限が
無いと 1 つの情報源の寄与が青天井になり、逆に下限が無いと Bad が続いた情報源の
権威性がゼロまで落ちてしまう（「抑制はするが完全排除はしない」という §6.1 の
既読減点と同じ設計思想）。

## データ保持

| 項目 | 決定 |
| --- | --- |
| 記事本文 | DB に内部保存する。外部には表示しない |
| 本文の破棄 | しない（プロンプト改善時の再解析と重複判定に必要） |
| リンク切れ・削除済み記事 | `is_dead` フラグでソフト削除する |

## 運用

| 項目 | 決定 |
| --- | --- |
| 公式ソースレジストリの更新 | 利用者本人。YAML シーダー + `PATCH /api/sources/{id}` |
| 誤った公式判定の修正 | `PATCH /api/sources/{id}` で authority を上書きし、`verified` フラグで手動確認済みを区別する |
| LLM 処理失敗時の再試行 | 3 回（指数バックオフ） |
| 検索 API / LLM の月額上限 | 追加課金なし。Brave の無料枠 月 2000 クエリのみが実質的な上限 |
| ログの保持期間 | 90 日 |
| 推薦 run の保持期間 | 30 日。超過分は `purge_recommendation_runs` ジョブが削除する（紐づく `recommendations` は CASCADE で消える） |
| 推薦 API のレート制限 | 利用者ごとに 60 秒あたり 30 リクエスト。`GET /api/feed` と `POST /api/articles/{id}/recommendations` は同じ枠を共有する。超過時は 429 と `Retry-After` を返す |
| レート制限の適用範囲 | プロセス内メモリで数えるため、単一プロセス起動（`run.sh`）が前提。複数ワーカープロセス構成にすると実効上限がプロセス数倍に緩む |

## 認証

MVP では認証を設けない（単一ユーザー）。ただし全テーブルに `user_id` を持たせ、将来のマルチユーザー化を妨げない。

## リポジトリ運用

| 項目 | 決定 |
| --- | --- |
| Squash merge | 無効 |
| merge 後の source branch | 自動削除 |
| discussion 解決 | merge 前に必須 |
| Approvals | 0（単一ユーザーのため自己マージを許容する） |
| Pipeline 成功必須 | なし（runner 停止時に全 MR が止まるのを避ける） |
| レビュー | `gitlab-mr-review` skill による自己レビューで手順として担保する |
