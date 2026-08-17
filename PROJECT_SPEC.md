# 技術記事レコメンドサービス 要件定義

## この文書の読み方

実装は文書より先へ進む。どの節が現役の要件で、どの節が初期設計時の記録なのかを下の表で示す（Issue #57で全節を実装と突き合わせた結果）。記録として残す節は実装へ追随させない。読むときは「現行の参照先」を見る。

| 節 | 位置づけ | 現行の参照先 |
| --- | --- | --- |
| §1 プロジェクト概要 〜 §17 重複排除 | 現役の要件 | — |
| §18 推奨技術スタック | 現役の要件（候補の並記は初期設計時のもの） | [docs/adr/0001-technology-stack.md](docs/adr/0001-technology-stack.md) |
| §19 データモデル案 | 初期設計時の記録 | [backend/src/techradar/db/models.py](backend/src/techradar/db/models.py) |
| §20 API案 | 初期設計時の記録 | [backend/openapi.json](backend/openapi.json) |
| §21 セキュリティ要件 | 現役の要件 | SSRF対策は [backend/src/techradar/fetcher/](backend/src/techradar/fetcher/)、LLMへ渡す内容は [backend/src/techradar/llm/](backend/src/techradar/llm/)、外部検索へ送る内容は [backend/src/techradar/collectors/](backend/src/techradar/collectors/) |
| §22 MVPスコープ | 「必須」は初期設計時の記録。**「MVPでは実装しない」は現役のスコープ境界** | 「必須」はロードマップIssue #17。「MVPでは実装しない」は本節そのもの |
| §23 実装順序 | 初期設計時の記録 | ロードマップIssue #17 |
| §24 非機能要件 | 現役の要件 | — |
| §25 Claude Codeへの実装方針 | 現役の要件 | [CLAUDE.md](CLAUDE.md) |
| §26 完了条件 | 初期設計時の記録 | ロードマップIssue #17、[docs/decisions.md](docs/decisions.md) |
| §27 初回実装時に決定する必要がある事項 | 初期設計時の記録 | [docs/decisions.md](docs/decisions.md)、[docs/adr/](docs/adr/) |

§22の「MVPでは実装しない」だけは扱いが違う。詳しくは同節の注記を読む。

---

## 1. プロジェクト概要

技術記事に特化した、Google ChromeのDiscoverに近いパーソナライズド・フィードを実装する。

ユーザーが気になる技術記事のURLを登録すると、その記事内容を解析し、関連する新着記事を推薦する。

推薦記事への `Good` / `Bad` フィードバックを蓄積し、ユーザーの関心を継続的に学習する。

特に、公式ドキュメント、公式ブログ、リリースノート、論文、公式GitHubリリースなどの一次情報を強く優先する。

---

## 2. プロジェクト名

TechRadar

---

## 3. サービスの目的

以下を実現する。

1. ユーザーが気になる記事URLを登録できる
2. 登録記事を解析し、関連する新着記事を取得する
3. 直近7日以内の記事だけを推薦する
4. 記事の言語は限定しない
5. 外国語記事も日本語で要約する
6. 公式・一次情報を優先する
7. 推薦記事へGood / Badを付けられる
8. Goodされた記事を関心記事へ追加する
9. Good / Badから推薦結果を改善する
10. ユーザーが興味を持つジャンルを可視化する

---

## 4. 対象ユーザー

初期段階では単一ユーザー向けの個人利用サービスとする。

ただし、将来のマルチユーザー化を妨げないデータモデルにする。

---

## 5. 基本ユーザーフロー

```text
ユーザーが記事URLを入力
        ↓
URLと取得先を安全性検証
        ↓
記事メタデータと本文を取得
        ↓
記事を要約・分類・Embedding化
        ↓
記事内容から検索クエリを生成
        ↓
直近7日以内の関連記事を収集
        ↓
記事の公開日、品質、情報源を検証
        ↓
重複記事を除去
        ↓
関連度と一次情報優先度でランキング
        ↓
Discover形式のフィードに表示
        ↓
ユーザーがGood / Badを選択
        ↓
関心プロファイルを更新
        ↓
次回以降の推薦へ反映
```

---

## 6. 主要画面

### 6.1 ホームフィード

Google Discoverに近いカード形式のフィードを表示する。

表示項目:

* 原文タイトル
* 必要に応じた日本語タイトル
* 日本語要約
* 情報源
* 公開日時
* 原文言語
* 公式・一次情報バッジ
* 推薦スコア
* 推薦理由
* 関連トピック
* Goodボタン
* Badボタン
* 保存ボタン
* 元記事を開くボタン

フィード条件:

* 原則として公開から7日以内（フィードの絞り込みで1〜180日の範囲に変更できる。Issue #90）
* 言語制限なし
* 同一ニュースの重複を抑制
* 既にBadした記事は再表示しない
* 既読記事の再表示は抑制する
* 一次情報を強く優先する

### 6.2 URL登録

記事URLを入力して関心記事へ追加する。

状態:

```text
pending
fetching
analyzing
searching
completed
failed
```

登録記事自体は、公開から7日以内でなくてもよい。

7日制限は推薦候補に適用する。

### 6.3 関心記事一覧

以下を一覧表示する。

* 手動登録した記事
* Goodした記事
* 保存した記事
* 登録日時
* トピック
* 情報源
* 記事種別

フィルター:

* 登録方法
* ジャンル
* 情報源
* 言語
* 期間
* 公式 / 非公式

### 6.4 関心分析

以下を可視化する。

* ジャンル別関心度
* Good / Bad比率
* 関心の時間変化
* よく読む企業・OSS・技術
* 公式情報と解説記事の比率
* 概念記事、実装記事、研究、ニュースの比率
* 難易度の分布
* 抑制中のジャンル
* 複数の関心クラスタ

---

## 7. フィードバック仕様

### 7.1 Good

Goodされた記事は以下の処理を行う。

1. 関心記事へ追加
2. 記事トピックの正の重みを増加
3. 記事Embeddingを関心クラスタへ追加
4. 情報源に対する選好を少し増加
5. 類似記事の推薦スコアを増加

重みの初期値:

```text
手動URL登録: +1.0
Good:        +0.8
保存:        +0.5
全文閲覧:    +0.2
クリック:    +0.1
```

### 7.2 Bad

BadはGoodの単純な負数として扱わない。

Badされた記事と意味的に近い記事を抑制する。

```text
Bad: -0.8
```

任意でBad理由を指定できるようにする。

```text
not_interested
too_shallow
already_known
promotional
untrusted_source
too_repetitive
```

Bad理由の意味:

| 値 | 意味 |
| ---------------- | ---------- |
| not_interested | テーマに興味がない |
| too_shallow | 内容が浅い |
| already_known | 既知の内容 |
| promotional | 宣伝的 |
| untrusted_source | 情報源を信頼できない |
| too_repetitive | 同じ内容を見すぎた |

一件のBadだけでジャンル全体を抑制しない。

同一ジャンルでBadが繰り返された場合のみ、ジャンル重みを段階的に下げる。

例:

```text
同一トピックの直近5記事中3記事以上がBad
→ トピック全体の重みを低下
```

---

## 8. 関心プロファイル

単一の平均Embeddingだけでユーザーの関心を表現しない。

ユーザーの関心は複数クラスタとして保持する。

例:

```json
{
  "interest_clusters": [
    {
      "label": "AI Agent Engineering",
      "weight": 0.55,
      "topics": [
        "MCP",
        "Tool Use",
        "Harness Engineering"
      ]
    },
    {
      "label": "3D Point Cloud",
      "weight": 0.25,
      "topics": [
        "LAS",
        "CloudCompare",
        "Open3D"
      ]
    },
    {
      "label": "DevOps Automation",
      "weight": 0.20,
      "topics": [
        "GitLab CI",
        "Runner",
        "Claude Code"
      ]
    }
  ]
}
```

関心の古さを考慮し、時間減衰を適用する。

```text
effective_interest =
    explicit_weight
  × feedback_weight
  × recency_decay
  × confidence
```

---

## 9. 記事の構造化データ

記事解析後、最低限以下の構造を生成する。

```json
{
  "title": "Article title",
  "translated_title": "日本語タイトル",
  "url": "https://example.com/article",
  "canonical_url": "https://example.com/article",
  "published_at": "2026-07-31T10:00:00Z",
  "language": "en",
  "source_domain": "example.com",
  "author": "Author",
  "summary_ja": "日本語要約",
  "domain": "Generative AI",
  "category": "Agentic Engineering",
  "topics": [
    "MCP",
    "Context Engineering"
  ],
  "technologies": [
    "Claude Code",
    "Codex"
  ],
  "content_type": "implementation",
  "difficulty": "advanced",
  "source_type": "official_documentation",
  "source_authority": 1.0,
  "technical_quality": 0.85,
  "is_primary_source": true
}
```

---

## 10. 情報源の優先順位

情報源を以下のTierに分類する。

### Tier 1

* 公式ドキュメント
* API仕様
* 標準仕様
* 公式リリースノート

### Tier 2

* 公式ブログ
* 公式研究記事
* 原著論文
* 公式GitHub Release

### Tier 3

* 企業Tech Blog
* OSSメンテナー本人の記事
* 開発者本人による解説

### Tier 4

* 高品質な個人技術記事
* 技術メディア
* 実装検証記事

### Tier 5

* ニュース転載
* 内容の薄いまとめ
* SEO目的の記事
* 出典不明の記事

初期authority値:

| 情報源 | スコア |
| ---------------- | ---: |
| 公式仕様・APIドキュメント | 1.00 |
| 公式リリースノート | 1.00 |
| 公式研究・原著論文 | 0.95 |
| 公式ブログ | 0.90 |
| 公式GitHub Release | 0.90 |
| 企業Tech Blog | 0.75 |
| 高品質な個人技術記事 | 0.60 |
| 技術ニュース | 0.45 |
| まとめ・転載 | 0.20 |

公式であっても、軽微な更新やユーザーとの関連性が低い記事は上位表示しない。

---

## 11. 公式ソースレジストリ

重要な企業、OSS、研究組織ごとに公式情報源を管理する。

例:

```json
{
  "entity": "OpenAI",
  "official_domains": [
    "openai.com",
    "platform.openai.com"
  ],
  "official_github_orgs": [
    "openai"
  ],
  "source_patterns": [
    {
      "pattern": "platform.openai.com/docs",
      "type": "official_documentation",
      "authority": 1.0
    },
    {
      "pattern": "openai.com/index",
      "type": "official_blog",
      "authority": 0.9
    }
  ]
}
```

初期対象候補:

* OpenAI
* Anthropic
* Google
* Microsoft
* AWS
* Meta
* Hugging Face
* GitHub
* GitLab
* Cloudflare
* NVIDIA
* Python
* Rust
* TypeScript
* 各主要OSSプロジェクト

公式ソースレジストリはコードに埋め込まず、DBまたは設定ファイルとして管理する。

---

## 12. 記事取得戦略

候補記事の取得は以下を優先する。

1. 公式RSS / Atom
2. 公式API
3. 公式リリースノート
4. GitHub Releases
5. arXivや学会サイト
6. Web検索API
7. Hacker Newsなどの技術コミュニティ
8. 技術メディア

一次情報はWeb検索だけに依存せず、固定巡回する。

巡回はUIの実行ボタンから起動する。定期スケジューラは置かない（サーバーを常駐させないため。[CLAUDE.md](CLAUDE.md)、[backend/src/techradar/api/crawl.py](backend/src/techradar/api/crawl.py)）。

---

## 13. 推薦モード

### 13.1 記事起点推薦

ユーザーが選択した1件の記事に近い記事を推薦する。

用途:

* この記事の続きを読みたい
* 同じ技術の別実装を知りたい
* 一次情報を探したい

### 13.2 Discoverフィード

登録記事とGood記事全体から生成した関心プロファイルを使用する。

用途:

* 今の自分に合う新着記事を知りたい
* 継続的に新しいテーマを発見したい

両モードは別API、または明示的なパラメータで分離する。

---

## 14. 推薦スコア

初期スコアリング:

```text
recommendation_score =
    interest_similarity    × 0.35
  + source_authority       × 0.30
  + source_article_match   × 0.10
  + freshness              × 0.10
  + technical_quality      × 0.10
  + novelty                × 0.05
  - bad_penalty
  - duplicate_penalty
```

### 補足

`source_authority` を強くする。

ただし、公式であることだけで上位表示しない。

以下も評価する。

* ユーザー関心との一致
* 更新内容の重要度
* 技術的新規性
* 実務への影響
* 元記事に対する情報増分
* 同じ内容の重複度

---

## 15. フィード構成比

Discoverフィードでは以下を目安に候補を混ぜる。

```text
強い関心一致:             55%
一次情報・公式情報枠:     25%
関連する新規テーマ探索:   15%
多様性確保:                5%
```

比率は将来的にユーザーごとに調整可能にする。

---

## 16. 多言語仕様

* 検索対象の言語は限定しない
* 原文タイトルを保持する
* 日本語タイトルを補助表示できる
* 要約は日本語で生成する
* 原文言語を表示する
* Embeddingには多言語対応モデルを使用する
* 日本語と英語を最低限の検索クエリとして生成する
* 必要に応じて原文言語の検索クエリも生成する
* 言語そのものを推薦スコアの減点要因にしない

---

## 17. 重複排除

以下を用いて重複を判定する。

* canonical URL
* 正規化URL
* タイトル類似度
* 本文ハッシュ
* Embedding類似度
* 同一ニュースイベントのクラスタリング

同一ニュースについて、公式記事と解説記事がある場合は、原則として公式記事を優先する。

ただし、解説記事に独自検証、コード、実測値がある場合は別記事として残す。

---

## 18. 推奨技術スタック

> 各項目に候補を並べているのは初期設計時のもの。どれを採ったかは [docs/adr/0001-technology-stack.md](docs/adr/0001-technology-stack.md) が決めており、実装もその決定に従っている。ジョブ基盤にRedis / Celeryを使わずPostgreSQLのキューにした点、認証をMVPでは置かない点はADRを参照する。認証を置かないことはセキュリティ要件（§21）の適用除外ではない。無認証を前提に置いている対策（CORS許可オリジンの制限、推薦APIのレート制限）は§24にある。

### Frontend

```text
Next.js
TypeScript
```

### Backend

候補:

```text
FastAPI
Python
```

または、初期構成を単純化する場合:

```text
Next.js Route Handlers
TypeScript
```

記事取得・自然言語処理ライブラリとの相性から、MVPではFastAPIを推奨する。

### Database

```text
PostgreSQL
pgvector
```

### Article Extraction

```text
trafilatura
BeautifulSoup
readability-lxml
feedparser
```

### Queue

MVP:

```text
PostgreSQLベースのジョブ管理
```

拡張時:

```text
Redis
Dramatiq
Celery
```

### Visualization

```text
Recharts
```

### Authentication

初期は単一ユーザーのため省略可能。

将来候補:

```text
Auth.js
Supabase Auth
```

---

## 19. データモデル案

> 初期設計時の案であり、実装へ追随させていない。現行のスキーマは [backend/src/techradar/db/models.py](backend/src/techradar/db/models.py) と [backend/migrations/](backend/migrations/) を参照する。
>
> 差はテーブル数と列の両方にある。下のDDLは8テーブルだが実装は12テーブルで、`jobs`と`operation_logs`はスキーマ実装時（Issue #2）に案へ足す形で加わり、`article_registrations`はURL登録画面（Issue #12）、`user_source_preferences`は情報源の選好学習（Issue #34）で加わった。列も揃っておらず、たとえば`articles`は下のDDLが23列に対して実装は33列ある（`body` / `is_dead` / `analysis_status` / `duplicate_of_article_id` など、本文の保持と解析・重複判定のために増えた分がDDLに無い）。どのテーブルが`user_id`を持つかは`models.py`のモジュールdocstringにある。

```sql
create table articles (
    id uuid primary key,
    canonical_url text unique not null,
    original_url text not null,
    title text not null,
    translated_title text,
    summary_ja text,
    source_domain text not null,
    author text,
    language text,
    published_at timestamptz,
    fetched_at timestamptz not null,
    body_hash text,
    domain text,
    category text,
    topics jsonb not null default '[]',
    technologies jsonb not null default '[]',
    content_type text,
    difficulty text,
    source_type text,
    source_authority real not null default 0,
    technical_quality real not null default 0,
    is_primary_source boolean not null default false,
    embedding vector
);

create table user_articles (
    id uuid primary key,
    user_id uuid not null,
    article_id uuid not null references articles(id),
    origin text not null,
    interest_weight real not null,
    created_at timestamptz not null,
    unique(user_id, article_id)
);

create table article_feedback (
    user_id uuid not null,
    article_id uuid not null references articles(id),
    action text not null,
    reason text,
    created_at timestamptz not null,
    primary key (user_id, article_id)
);

create table recommendation_runs (
    id uuid primary key,
    user_id uuid not null,
    source_article_id uuid references articles(id),
    mode text not null,
    generated_at timestamptz not null
);

create table recommendations (
    run_id uuid not null references recommendation_runs(id),
    article_id uuid not null references articles(id),
    score real not null,
    reasons jsonb not null,
    rank integer not null,
    primary key (run_id, article_id)
);

create table source_registry (
    id uuid primary key,
    entity_name text not null,
    domain text not null,
    path_pattern text,
    github_org text,
    source_type text not null,
    authority_score real not null,
    verified boolean not null default false
);

create table user_interest_clusters (
    id uuid primary key,
    user_id uuid not null,
    label text not null,
    weight real not null,
    topics jsonb not null default '[]',
    centroid_embedding vector,
    updated_at timestamptz not null
);

create table user_topic_preferences (
    user_id uuid not null,
    topic text not null,
    positive_weight real not null default 0,
    negative_weight real not null default 0,
    effective_weight real not null default 0,
    updated_at timestamptz not null,
    primary key (user_id, topic)
);
```

Embeddingの次元は採用モデルに合わせて決定する。

---

## 20. API案

> 初期設計時の案であり、実装へ追随させていない。現行のAPIは [backend/openapi.json](backend/openapi.json) と [backend/src/techradar/api/](backend/src/techradar/api/) を参照する。リクエストとレスポンスの形も下の一覧では分からない。
>
> 下の一覧に無いものとして`GET /api/articles/registrations/{registration_id}`（Issue #12）・`POST /api/crawl/runs`（Issue #8）・`POST /api/articles/bulk`（Issue #39）が実装されている。`DELETE /api/articles/{article_id}/interest`・`GET /api/interests/summary`・`GET /api/health`も実装済みだが、こちらは既存機能に付随して入ったものでIssueとの対応が1対1にならないため番号を付けていない。逆に`GET /api/articles/{article_id}`は実装していない。

```text
POST   /api/articles
GET    /api/articles
GET    /api/articles/{article_id}

POST   /api/articles/{article_id}/recommendations
GET    /api/feed

POST   /api/articles/{article_id}/feedback
DELETE /api/articles/{article_id}/feedback

GET    /api/interests
GET    /api/interests/clusters
GET    /api/interests/timeline

GET    /api/sources
POST   /api/sources
PATCH  /api/sources/{source_id}

GET    /api/jobs/{job_id}
```

### URL登録

```json
POST /api/articles

{
  "url": "https://example.com/article"
}
```

### Good

```json
POST /api/articles/{article_id}/feedback

{
  "action": "good"
}
```

### Bad

```json
POST /api/articles/{article_id}/feedback

{
  "action": "bad",
  "reason": "too_shallow"
}
```

---

## 21. セキュリティ要件

外部URLと記事本文は、完全な非信頼入力として扱う。

### SSRF対策

以下へのアクセスを拒否する。

```text
localhost
127.0.0.0/8
10.0.0.0/8
172.16.0.0/12
192.168.0.0/16
169.254.0.0/16
IPv6 loopback
IPv6 private network
クラウドメタデータエンドポイント
```

以下も実施する。

* DNS解決後のIPを検証
* リダイレクト先も毎回検証
* HTTP / HTTPS以外を禁止
* 最大リダイレクト回数を制限
* Content-Typeを検証
* 最大レスポンスサイズを制限
* 接続タイムアウトを設定
* JavaScriptを実行しない
* script、iframe、objectを除去
* HTMLサニタイズを実施

### LLM対策

* 記事本文を命令として扱わない
* 記事本文をUntrusted Contentとして明示する
* 本文をツール実行権限のあるエージェントへ直接渡さない
* 記事本文から抽出したURLを自動実行しない
* ユーザー関心プロファイルを外部検索へそのまま送信しない
* APIキーや内部設定をLLMコンテキストへ含めない

---

## 22. MVPスコープ

> 下の「必須」は初期設計時の記録であり、実装へ追随させていない。挙げた項目はロードマップIssue #17のPhase 1〜5としてすべて完了している。現在どこまで進んでいるかはロードマップIssueを参照する。
>
> ただし「MVPでは実装しない」は記録ではなく、**今も守っているスコープの境界**として読む。挙げた項目はいずれも実装していない。境界の典拠はロードマップIssueではなくこの節そのもので、ここへ手を出す判断をするときは、まずこの節を更新してから着手する。ロードマップIssueへ該当する項目を足すときも同じ。

### 必須

* URL登録
* 記事本文解析
* 記事の要約と分類
* Embedding生成
* 直近7日以内の記事収集
* 多言語記事対応
* 日本語要約
* 一次情報判定
* 一次情報を強く優先した推薦
* Good
* Bad
* Good記事の関心記事化
* Bad記事の近傍抑制
* Discover形式フィード
* ジャンル別関心度の可視化
* 重複排除

### MVPでは実装しない

* マルチユーザー
* SNS機能
* コメント
* チーム共有
* 課金
* モバイルアプリ
* 独自の機械学習モデル学習
* 高度な協調フィルタリング
* 複雑なマルチエージェント構成
* ブラウザ通知
* Slack連携
* MCPサーバー

---

## 23. 実装順序

> 初期設計時の記録であり、実装へ追随させていない。実際の実装順序と進捗、Issue番号と依存関係はロードマップIssue #17が持つ。ここに書かれたPhase 1〜5は完了している。

### Phase 1: 基盤

1. リポジトリ初期化
2. Frontend / Backend構成
3. PostgreSQL接続
4. マイグレーション
5. URL取得のSSRF対策
6. 記事本文抽出
7. 記事保存

### Phase 2: 記事解析

1. 言語判定
2. 日本語要約
3. タグ・ジャンル抽出
4. 情報源分類
5. Embedding生成
6. 公式ソースレジストリ

### Phase 3: 推薦

1. 関連記事候補取得
2. 7日以内の公開日検証
3. 類似度計算
4. authorityスコア
5. 重複排除
6. ランキング
7. 推薦理由生成

### Phase 4: UI

1. URL登録
2. Discoverフィード
3. 記事カード
4. Good / Bad
5. ローディング・エラー状態
6. 関心記事一覧

### Phase 5: パーソナライズ

1. Goodによる関心更新
2. Badによる近傍抑制
3. 関心クラスタ
4. 時間減衰
5. 関心分析画面

---

## 24. 非機能要件

### 型安全性

* TypeScriptのstrict modeを有効化
* Pythonは型ヒントを必須とする
* PydanticでAPI入出力を検証する
* DBスキーマとAPI型の不整合を防ぐ

### テスト

最低限以下をテストする。

* URL正規化
* SSRF拒否
* リダイレクト先検証
* 公開日判定
* 7日フィルター
* source authority判定
* Good / Bad更新
* 重複記事判定
* ランキング
* API入力検証

### 認証を置かない前提で守る対策

認証は置かない（§18、[docs/decisions.md](docs/decisions.md)）。APIとUIを守る境界はネットワーク側にある。backend・frontend・PostgreSQLのいずれも `BIND_HOST`（既定 127.0.0.1）へ明示的にbindする（Issue #64、#65）。`run.sh` がプロセスへ渡し、[infra/docker-compose.yml](infra/docker-compose.yml) がホスト側の公開アドレスとして使う。

既定に任せると範囲が揃わない。uvicornは `--host` の既定が127.0.0.1だが、`next dev` は `--hostname` を渡さないと全インターフェースへbindし、同一LANの別端末からUIへ到達できてしまう。dockerもホスト側のアドレスを省略すると全インターフェースへ公開する。認証を置いていない以上、到達した時点で中身が見える。PostgreSQLの接続情報はローカル実行を前提にした弱い既定値のため、なおさら届く範囲を絞る。

既に起動しているコンテナは、この設定を変えても作り直すまで公開範囲が変わらない。変更を反映するには `./run.sh --stop` で一度落としてから起動し直す。食い違っている間は起動確認の共通処理（[scripts/ai-harness/lib/postgres.sh](scripts/ai-harness/lib/postgres.sh)）が警告を出す。判定できるのはcomposeから見えるコンテナだけで、dockerへ到達できないシェルや、composeを通さず立てたPostgreSQLは対象外になる。その場合は確認できなかったこと自体を出す。

PostgreSQLへ `BIND_HOST` が渡るのは `run.sh` から起動したときだけになる。`check.sh` は設定ファイルを読まないため、そちらが先にコンテナを作ると閉じた既定（127.0.0.1）で作られる。

この上に、以下の歯止めを置く。いずれも部分的な対策であり、認証の代わりにはならない。

* CORSの許可オリジンを設定で絞る（[backend/src/techradar/config.py](backend/src/techradar/config.py) の `CORS_ALLOW_ORIGINS`、[backend/src/techradar/main.py](backend/src/techradar/main.py)）。効くのはブラウザ経由の呼び出しだけで、curlのような直接アクセスは防げない
* 推薦APIにレート制限を掛ける（[backend/src/techradar/api/rate_limit.py](backend/src/techradar/api/rate_limit.py)）。掛かっているのは推薦の2つのエンドポイントだけで、他のAPIには無い
* 巡回ジョブの重複起動を防ぐ（[backend/src/techradar/api/crawl.py](backend/src/techradar/api/crawl.py)）
* 一括登録にファイルサイズとURL件数の上限を置く（[backend/src/techradar/api/bulk_import.py](backend/src/techradar/api/bulk_import.py) の `MAX_BULK_IMPORT_FILE_BYTES` / `MAX_BULK_IMPORT_URL_COUNT`）

記事の単体登録やソース登録には回数の上限が無い。`BIND_HOST` を変えて意図的に外部へ晒す構成にするなら、認証とあわせてこの節を見直す。なお `BIND_HOST` だけを広げても、画面上のJavaScriptは既定で自分自身の `localhost` へAPIを呼ぶため、UIを他の端末で使うには `NEXT_PUBLIC_API_BASE_URL` も変える必要がある。APIへの直接の到達は `BIND_HOST` だけで開く。

### 可観測性

* 構造化ログ
* ジョブ状態
* URL取得失敗理由
* LLM処理失敗理由
* 推薦スコア内訳
* 使用モデル
* トークン使用量
* 処理時間

### コスト管理

* 同一記事の再解析を避ける
* URLと本文ハッシュでキャッシュする
* LLM処理結果を保存する
* 既存記事のEmbeddingを再生成しない
* 推薦の順位付けにLLMを使わない（スコア内訳から機械的に決める。[backend/src/techradar/recommendation/ranking.py](backend/src/techradar/recommendation/ranking.py)）

---

## 25. Claude Codeへの実装方針

以下の方針で実装する。

* 実装前にリポジトリ構造と依存関係を確認する
* 要件を満たす最小構成から開始する
* 大きな変更は小さな差分へ分割する
* モジュール性と責務分離を優先する
* 型安全性を維持する
* 外部URL処理を独立モジュールへ隔離する
* LLMプロバイダーを抽象化する
* 検索プロバイダーを抽象化する
* Embeddingモデルを交換可能にする
* 推薦スコアの重みを設定ファイル化する
* source authorityを設定またはDBで管理する
* エラーを握りつぶさない
* テスト可能な純粋関数としてランキング処理を実装する
* MVP外の機能を勝手に追加しない
* 不明点はREADMEまたはADRへ記録する

---

## 26. 完了条件

> 初期設計時の記録であり、実装へ追随させていない。ここに挙げた条件はすべて満たしており、実装はその先へ進んでいる（情報源選好の学習・レート制限・保持期間など）。現行の到達点はロードマップIssue #17と [docs/decisions.md](docs/decisions.md)、APIの形は [backend/openapi.json](backend/openapi.json) を参照する。
>
> 記録扱いにするのは「MVPが完了したか」という判定であって、条件そのものではない。SSRF対策は§21、テストと可観測性は§24が現役の要件として持っており、退行があればそちら違反として扱う。

MVP完了条件:

1. URLを登録できる
2. 記事タイトル、本文、公開日、言語を抽出できる
3. 記事を日本語で要約できる
4. 記事のジャンルと技術キーワードを抽出できる
5. 直近7日以内の関連記事を取得できる
6. 一次情報を識別できる
7. 一次情報がランキングで優先される
8. Discover形式で記事を表示できる
9. Good / Badを記録できる
10. Goodした記事が関心記事へ追加される
11. Badした記事と近い候補が抑制される
12. 関心ジャンルを可視化できる
13. URL取得にSSRF対策がある
14. 主要ロジックにテストがある
15. 推薦スコアの内訳を確認できる

---

## 27. 初回実装時に決定する必要がある事項

> 初期設計時の記録であり、実装へ追随させていない。ここに挙げた項目はすべて決定済みで、決定内容は [docs/decisions.md](docs/decisions.md)（インフラ・外部サービス・フィード・データ保持・運用・認証の各表）と [docs/adr/](docs/adr/)（技術選定の根拠）にある。末尾の「推奨初期値」もdecisions.mdの記述のほうが具体的で、そちらが現行の決定である。

以下は未確定のため、実装着手前または初期段階で決定する。

### インフラ

* ローカルのみか、クラウドへデプロイするか
* PostgreSQLをDocker、Supabase、Neonなどのどれで動かすか
* FrontendとBackendを分離するか
* ジョブ実行基盤を何にするか

### 外部サービス

* Web検索プロバイダー
* Embeddingモデル
* 要約・分類用LLM
* RSS以外の記事収集方法
* 翻訳をLLMで行うか専用APIを使うか

### フィード

* 更新頻度
* 一度に表示する件数
* 無限スクロールかページングか
* 既読記事を再表示するか
* 保存とGoodを分けるか
* Bad理由を必須にするか任意にするか

### データ保持

* 記事本文を保存するか
* 本文を処理後に破棄するか
* 要約とEmbeddingだけを保存するか
* 削除済み記事やリンク切れ記事をどう扱うか

### 運用

* 公式ソースレジストリを誰が更新するか
* 誤った公式判定をどう修正するか
* LLM処理失敗時の再試行回数
* 検索APIやLLMの月額上限
* ログの保持期間

決定事項 推奨初期値
アプリ構成 Next.js + FastAPI + PostgreSQL
DB環境 Docker Composeでローカル起動
検索方式 RSS・公式フィード優先、Web検索は補完
記事本文保持 本文は内部保存、外部表示しない
認証 MVPでは単一ユーザー、認証なし
