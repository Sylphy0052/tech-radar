# ADR 0008: 発見済みフィードの「新着が出ない」判定

- ステータス: 採用
- 日付: 2026-08-16
- 関連: Issue #93, #105, #108, #109 / `PROJECT_SPEC.md` §12 / [ADR 0006](0006-cluster-count-and-feed-slot-defaults.md)
- 対象: `discovered_feeds` テーブルと `techradar.collectors` 配下のみ。`config/feeds.yaml` 由来の手動フィードは対象外（Issue #105 の設計判断を踏襲する）

本 ADR は設計判断のみを確定する。実装は Issue #109 で行う。

## コンテキスト

自動発見したフィード（`discovered_feeds`）には、巡回のたびに更新される健全性の指標が2つある。

- `consecutive_failures` / `last_succeeded_at`（Issue #105）— 取得・パースの成否
- `consecutive_empty_fetches`（Issue #108）— 取得には成功したがエントリを1件も配信しなかった連続回数

どちらも `MAX_DISCOVERED_FEEDS_TOTAL`（20）の枠を専有し続けるフィードを `status=DISABLED` にして枠を回収するための仕掛けである。

Issue #109 が指摘するのは、この2つでは「毎回同じ既出記事だけを返すフィード」を捕まえられないことである。判定に使う `FeedFetchResult.entry_count` が、重複・既存記事の除外より前の値だからである。

## 調査結果: `entry_count` の現在の意味

`entry_count` はDBの列ではない。`FeedFetchResult`（`backend/src/techradar/collectors/rss.py:44-68`）のフィールドで、`RssCollector` のインスタンス変数 `_feed_results` に1巡回のあいだだけ保持される。

値が入るのは3箇所すべて `backend/src/techradar/collectors/rss.py` にある。

| 箇所 | 値 | 意味 |
| --- | --- | --- |
| `rss.py:131` | `succeeded=False, entry_count=0` | `FetchError`（取得失敗）。件数は「分からない」 |
| `rss.py:145` | `succeeded=False, entry_count=0` | パース破綻（`bozo` かつエントリ0件）。件数は「分からない」 |
| `rss.py:159` | `succeeded=True, entry_count=len(candidates)` | 取得・パース成功。`candidates` は `_to_candidate` を通した件数 |

`candidates` は `_to_candidate`（`rss.py:161-186`）が `link` または `title` を欠くエントリを `None` にして弾いた後の件数であり、`parsed.entries` の生の件数ではない。

参照は1箇所だけで、`backend/src/techradar/collectors/discovery.py:332` の `_apply_empty_fetch_result` が `result.entry_count > 0` を見て `consecutive_empty_fetches` を0へ戻すか1増やすかを決める。APIには一切露出していない（`backend/openapi.json` にも `backend/src/techradar/api/` にも `discovered_feeds` は現れない）。

**したがって現在の `entry_count` は「フィードが返した総エントリ数」でも「新規に保存できた件数」でもなく、「そのフィードから候補記事オブジェクトに変換できた件数（重複・既存記事の除外より前）」である。**

`collect_candidates`（`backend/src/techradar/collectors/service.py:104-111`）は死活監視の反映（`_record_feed_health_safely`、105行目）の**後**に `filter_recent` → `_filter_by_source_domain` → `_dedupe_by_normalized_url` → `_exclude_existing_articles` → `_exclude_already_queued` → `limit_candidates` を順にかける。既出記事だけを返すフィードは `entry_count > 0` のまま `consecutive_empty_fetches` が一度も増えない。

## 決定

### 1. `entry_count` の意味は変えない

今後も「取得・パースに成功したフィードが配信した、候補記事に変換できたエントリの件数（除外前）」として扱う。改名も再定義もしない。

- 根拠: この値は Issue #108 の「記事を配信しているか」という問い（フィードが空か否か）に対する正しい入力であり、Issue #109 の問い（新着があるか）とは別の問いである。1つのフィールドに2つの意味を持たせると、`_apply_empty_fetch_result` の分岐が「どちらの意味で0なのか」を判別できなくなる
- 根拠: 除外はDBを引かないと判定できず、コレクターはDBを知らないままにする（Issue #105 の設計判断、`rss.py:74-83` の docstring）。`entry_count` をコレクター側で除外後の値にすることはそもそもできない

### 2. 「新着が出ない」の判定には、除外後・上限適用前の候補件数を使う

フィードURLごとに、`_exclude_already_queued` を通過した候補の件数を数える。最終新規保存日時は判定には使わない（診断用に列としては持つ、決定4を参照）。

- 根拠: 総エントリ数（`entry_count`）では既出記事を数えてしまう。これが Issue #109 そのものである
- 根拠: 「実際に `articles` へ保存できた件数」は使えない。`collect_candidates` が積むのは `fetch_article` ジョブであり、保存は別ジョブ・別トランザクションで後から起きる。巡回の時点では判明しない
- 根拠: `limit_candidates`（`service.py:109-111`）の**前**で数える。`max_candidates_per_run` で切られた分を「新着が無かった」と数えると、他フィードの新着が多い巡回で、上限に押し出されただけのフィードを無効化してしまう
- 根拠: 最終新規保存日時を閾値判定に使わないのは、このプロジェクトが定期スケジューラを持たず巡回がUIの実行ボタン起動である（CLAUDE.md の制約）ため。壁時計の経過時間は「新着が無い」のか「巡回していない」のかを区別できない。連続回数なら巡回した回数だけを数えるので、起動間隔に依存しない

### 3. 反映は2段階に分ける。既存の反映位置は動かさない

`collect_candidates` の処理順序に、2つめの反映を差し込む。

| 段階 | 位置 | 反映する内容 |
| --- | --- | --- |
| 第1段階（既存） | `_collect_all` の直後（`service.py:105`） | 取得・パースの成否（#105）とエントリ0件（#108） |
| 第2段階（新設） | `_exclude_already_queued` の直後、`limit_candidates` の前（`service.py:108` と `109` の間） | 新着件数（#109） |

- 根拠: 第1段階を動かさないので、Issue #108 の「無効化した枠が同じ巡回で空く」性質と、それを押さえるテスト `backend/tests/test_collectors_service.py:721` の `test_frees_a_discovery_slot_within_the_same_collect_run` はそのまま通る
- 根拠: 第2段階も新規発見（`_discover_new_feeds_safely`、`service.py:130`）より前にあるため、第2段階の無効化で空いた枠も同じ巡回で使える。処理順序を入れ替える必要はない
- 更新対象: 第2段階が触るのは第1段階で `succeeded=True` だった `feed_url` の行だけとする。取得に失敗した回は候補が0件になるのが当然であり、「新着が無かった」と数えてはならない（`consecutive_failures` の側でのみ数える。#108 が `consecutive_empty_fetches` について採った扱いと同じ）
- 無効化済みの行: `status=DISABLED` の行はスキップする。同じ `feed_url` を持つ別ドメインの行が生きている場合に巡回結果が相乗りしてくるが、取得していない行のカウンタを増やす意味は無い（`discovery.py:296-303` の既存の分岐と同じ理由。commit cdf60fc）
- `source_domain` 指定時: 第2段階を丸ごとスキップする。`_filter_by_source_domain`（`service.py:272-289`）が他ドメインの候補を全て落とすため、指定ドメイン以外の全フィードが新着0件に見える。ここを数えると、単一ドメインの再巡回を繰り返しただけで無関係なフィードが軒並み無効化される

### 4. 既存の死活監視とは独立した3つめのカウンタを足す

`consecutive_failures`（#105）・`consecutive_empty_fetches`（#108）のいずれとも重複させない。`discovered_feeds` に列を2つ追加する。

| 列 | 型 | 用途 |
| --- | --- | --- |
| `consecutive_no_new_entries` | `Integer NOT NULL DEFAULT 0` | 新着0件の連続回数。1件でも新着があれば0へ戻す。閾値到達で `status=DISABLED` / `enabled=False` |
| `last_new_entry_at` | `TIMESTAMPTZ NULL` | 直近で新着を出した時刻。判定には使わず、診断とUI向けの記録に留める |

閾値は `MAX_CONSECUTIVE_NO_NEW_ENTRIES = 30` とする（`MAX_CONSECUTIVE_FEED_FAILURES = 3` / `MAX_CONSECUTIVE_EMPTY_FETCHES = 10` とは別定数）。

- 根拠: 既出記事を返し続けること自体は異常ではない。更新頻度の低いフィードを巻き込まないよう、#108 の10回より粘る値にする（Issue #109 の「より粘る値にする」に従う）。巡回は手動起動で実時間の間隔が読めないため、時間ではなく回数で余裕を取るしかない
- 根拠: カウンタを分けるのは、無効化の理由（取得できない / 空を配信する / 新着が出ない）を列とログの両方から区別できるようにするため（Issue #109 の受入基準4つめ）
- ログのイベント名は `collectors.discovery.feed_disabled_stale` とし、既存の `feed_disabled`（#105）・`feed_disabled_empty`（#108）と区別する

### 5. 候補記事からフィードURLを逆引きできるようにする

`CandidateArticle`（`backend/src/techradar/collectors/base.py:22-40`）に `feed_url: str | None = None` を追加し、`RssCollector._to_candidate`（`rss.py:161`）で `feed.url` を入れる。第2段階の集計はこの値をキーにする。

- 根拠: 既存の `source_hint` は `feed.name`（発見済みフィードでは `load_enabled_discovered_feeds` が入れるドメイン名、`discovery.py:583`）で、`config/feeds.yaml` の手動フィードの名前と同じ空間に混ざる。`collector_name == "discovered_feeds"` との併用で絞れなくはないが、`record_feed_health` が `feed_url` をキーにしている以上、候補側も同じキーを持つのが素直
- RSS 以外のコレクター（HN / GitHub Releases / arXiv / Brave）は `None` のままとし、第2段階の集計対象から外れる

## 割り切り

- `_dedupe_by_normalized_url` で落ちた候補は、落ちた側のフィードの新着として数えない。同じ記事を配信する2つのフィードが並んだ場合、後着のフィードは新着0件と数えられ続けて最終的に無効化されうる。片方が残れば記事は拾えるため、枠の回収としては望ましい挙動だと判断する
- `filter_recent` は `published_at` を持たない候補を除外する（`filters.py:29-33`）。日付を出さないフィードは新着0件が続き、30回で無効化される。これらのフィードは実際に1件も enqueue されないため、枠を占める価値が無く、無効化して差し支えない
- カウンタは巡回の回数で数えるため、UIの実行ボタンを短時間に連打すると閾値へ早く到達する。単一ユーザー・手動起動という前提の下では実害が小さいと見て、回数以外の下限（前回反映からの経過時間など）は設けない

## 採用しなかった案

| 案 | 却下の理由 |
| --- | --- |
| `entry_count` を除外後の件数に変える | コレクターがDBを知る必要が生じ、Issue #105 の設計判断を壊す。#108 の「空のフィード」判定も同時に失われる |
| 第1段階の反映を絞り込みの後ろへ移す | Issue #108 の「同じ巡回で枠が空く」性質が壊れる（`test_frees_a_discovery_slot_within_the_same_collect_run`） |
| `consecutive_empty_fetches` を流用して閾値だけ変える | 「配信しない」と「新着が出ない」をログでも列でも区別できなくなる。Issue #109 の受入基準に反する |
| `last_new_entry_at` の経過日数で判定する | スケジューラを持たないため、経過時間が「新着が無い」のか「巡回していない」のかを表さない |
| 実際に `articles` へ保存された件数を数える | 保存は `fetch_article` ジョブで後から起きる。巡回のトランザクション内では判明しない |

## 実装時に触るファイル

| ファイル | 変更 |
| --- | --- |
| `backend/src/techradar/db/models.py` | `DiscoveredFeed` に `consecutive_no_new_entries` / `last_new_entry_at` を追加 |
| `backend/migrations/versions/` | 上記2列を足すリビジョンを追加（直近は `20260815_c3b1f4231c4b_add_discovered_feed_consecutive_empty_.py`） |
| `backend/src/techradar/collectors/base.py` | `CandidateArticle.feed_url` を追加 |
| `backend/src/techradar/collectors/rss.py` | `_to_candidate` で `feed_url` を埋める |
| `backend/src/techradar/collectors/discovery.py` | `MAX_CONSECUTIVE_NO_NEW_ENTRIES` と、第2段階の反映関数（`record_feed_novelty` 相当）を追加 |
| `backend/src/techradar/collectors/service.py` | `_exclude_already_queued` の直後に第2段階を呼ぶ（`_record_feed_health_safely` と同じく savepoint で囲む）。`source_domain` 指定時はスキップ |
