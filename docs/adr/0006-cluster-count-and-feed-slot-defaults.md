# ADR 0006: 関心クラスタ数とフィード枠の既定値

- ステータス: 採用
- 日付: 2026-08-13
- 関連: `PROJECT_SPEC.md` §8, §14, §15 / Issue #75, #87 / [ADR 0004](0004-analysis-body-limit.md) / [ADR 0007](0007-embedding-based-novelty.md)
- 補足: `exploration_min_novelty` に関する記述は、翌日の [ADR 0007](0007-embedding-based-novelty.md) に置き換えられている（下記「追記」を参照）

## コンテキスト

Issue #17 の「未確定・後続で判断する事項」に挙がっていた3項目のうち、残る2つ（関心クラスタ数の決定方法とフィード構成比の縮退動作）を確定する。1つめ（LLM へ渡す本文長の上限）は [ADR 0004](0004-analysis-body-limit.md) で確定済み。

これらは実データが無いと判断できないため保留していた。Issue #73 の時点（2026-08-12）では関心記事の embedding が1件も無く、クラスタは「対象データがありません」、フィード枠は strong_interest の 55 件が全て補充由来という状態だった。

2026-08-13 に DB を作り直して 261 件を収集し、関心記事 69 件に embedding が付いたため着手条件を満たした。

## 計測

本番 DB を読み取り専用で参照して測った。再現手順は末尾に置く。母数は関心記事 69 件（すべて `origin=manual`）、採点済み候補 169 件。

### 1. クラスタ数の感度

`max_clusters` を変えたときのクラスタ数と、各クラスタの記事数。

| max_clusters | クラスタ数 | 記事数の内訳 |
| --- | --- | --- |
| 4 | 4 | 24, 18, 16, 11 |
| 6 | 6 | 22, 14, 12, 9, 6, 6 |
| 8 | 8 | 18, 18, 9, 7, 6, 5, 3, 3 |
| 10 | 10 | 15, 14, 8, 7, 7, 6, 5, 4, 2, 1 |
| 12 | 12 | 15, 13, 7, 6, 5, 5, 5, 4, 3, 2, 2, 2 |
| 16 | 16 | 11, 10, 9, 6, 6, 5, 4, 3, 3, 2, 2, 2, 2, 2, 1, 1 |

`max_clusters` の値がそのままクラスタ数になっている。69 件では `capacity_based = 69 // 3 = 23` が `max_clusters` を常に上回るため、上限が必ず効く。

### 2. クラスタの内容

`max_clusters=8`（現在の既定値）のときのラベル。

| 記事数 | weight | topics |
| --- | --- | --- |
| 18 | 0.261 | AGENTS.md への規則の外出し / AI ファシリテーションのリファインメント / AIエージェント並列開発 |
| 18 | 0.261 | AIエージェントによるコード移植 / AIコーディングエージェント / AIコーディングエージェントの併用 |
| 9 | 0.130 | AI Ready データ整備 / AIによる表編集のレビュー / Agentic Document Extract |
| 7 | 0.101 | AIガバナンス / AI安全性 / AI安全性テスト |
| 6 | 0.087 | Claude Code Skill / 8列グリッド設計 / AIへの権限設計・禁止事項の明文化 |
| 5 | 0.072 | Agent Skills / Agent Plugins / Claude Code Skills |
| 3 | 0.043 | API従量課金 / MoEアクティブパラメータ / Qwen3.8-Max |
| 3 | 0.043 | コンテキストウィンドウ / コンテキストエンジニアリング / スケーリング則 |

`max_clusters` を増やすと下位が細かく割れる。10 では「AI依存度の自己点検 / CLAUDE.md設計」とモデル系（GPT-5.6 / planモード）が分かれる一方、1 件だけのクラスタ（ChatGPT会話の窃取 / 悪性ブラウザ拡張機能）が現れる。12 ではデータベース系（ORDER BY狙いのINDEX / キーセットページネーション）も独立するが、2 件のクラスタが3つできる。

### 3. min_articles_per_cluster の感度

`max_clusters=8` のまま `min_articles_per_cluster` を 2 / 3 / 5 / 8 と変えたが、**いずれも 8 クラスタ・記事数の内訳も同一**だった。`capacity_based`（`69 // mn` で 34 / 23 / 13 / 8）が常に `max_clusters` 以上になるため、この値は結果に現れない。

### 4. フィード枠の充足

`composition.py` の `_slot_for` と同じ優先順（strong_interest → primary_source → exploration → diversity）で候補を分類した。定員より前の、条件を満たす候補の件数。

| 枠 | 定員 | バケット |
| --- | --- | --- |
| strong_interest | 55 | 67 |
| primary_source | 25 | 63 |
| exploration | 15 | 39 |
| diversity | 5 | **0** |

`interest_similarity` の分布は min 0.236 / p25 0.379 / p50 0.456 / p75 0.548 / p95 0.669 / max 0.755 で、閾値 0.5 を超えるのは 67 件。**Issue #73 時点の「strong_interest の 55 件が全て補充由来」という状態は解消した**。原因は閾値ではなく、関心プロファイルが空だったことにあった。

`novelty` は 1.0 が 153 件、0.8 が 15 件、0.6 が 1 件で、上端へ張り付いている。閾値 0.6 を全 169 件が満たすため exploration が候補を吸い尽くし、diversity のバケットが 0 件になる。

この節の `novelty` は、当時の実装（候補の topics のうち既知トピックに無いものの割合）の値である。翌日 ADR 0007 で embedding 距離へ差し替えたため、現行の実装ではこの分布にならない。

## 決定

**5つの値をいずれも据え置く。**

| 値 | 現在 | 判断 |
| --- | --- | --- |
| `max_clusters` | 8 | 据え置き |
| `min_clusters` | 2 | 据え置き |
| `min_articles_per_cluster` | 3 | 据え置き |
| `strong_interest_min_similarity` | 0.5 | 据え置き |
| `exploration_min_novelty` | 0.6 | 据え置き（ただし当時の実装では機能していない。Issue #87 で扱い、ADR 0007 が本行を置き換えた） |

### 根拠

**関心クラスタは推薦スコアに使われない。** `user_interest_clusters` を読むのは `api/interests.py` だけで、`GET /api/interests` の応答としてフロントの `InterestClusterList` が表示するために使う。`ranking.py` の `interest_similarity` は関心記事の embedding を直接加重平均しており、クラスタを経由しない。したがって `max_clusters` の選択はフィードの中身を変えず、**関心を人が眺めるときの粒度だけを決める**。値を動かす利得が小さい。

そのうえで 8 を選ぶ理由は、10 以上にすると記事 1〜2 件のクラスタが現れるため。KMeans の 1 件クラスタはラベルがその記事の topics そのままになり、「関心の束」として意味をなさない。weight も 0.014 で表示上ほぼ無視される。逆に 4 では 24 件と 18 件の塊ができて粗い。8 は最小クラスタが 3 件で、下限を割らずに済む唯一の設定でもある。

**`min_clusters` と `min_articles_per_cluster` は、この母数では死んでいる。** 3節のとおり結果に一切現れない。効くのは関心記事が `min_clusters * min_articles_per_cluster = 6` 件を下回る初期だけで、そこでの挙動（無理に分けず 1 クラスタへ丸める）は Issue #15 の自己レビューで確定済みである。実データで否定する材料が無いため据え置く。

**`strong_interest_min_similarity` を動かす理由が消えた。** Issue #75 の起票時点では「閾値が厳しすぎる可能性」を挙げていたが、バケット 67 件は定員 55 を上回っており、補充は起きていない。

**`exploration_min_novelty` は動かしても効かない。** 上位 2 枠に取られない 39 件は全て `novelty = 1.0` で、閾値を取りうる上限の 1.0 まで上げても 39 件全部が exploration へ入る。diversity のバケットは 0 件のまま変わらない。原因は `compute_novelty` が topics の文字列一致で既知判定していることにあり（known_topics 337 語と候補側 832 語の重なりが 13 語しかない）、スコアリング式そのものの見直しになる。Issue #75 のスコープ外のため **Issue #87 へ切り出した**（その後の結末は下記「追記」を参照）。

### この測定で言えないこと

- **記事が増えたときの妥当性**。69 件・169 件での結果であり、`capacity_based` が `max_clusters` を下回る規模（記事 24 件未満）は測っていない
- **クラスタが「正しい」かどうか**。意味の通りは topics の並びを人が読んで判断しただけで、外部の基準と突き合わせていない
- **関心の偏りから来る影響**。関心記事 69 件は全て手動登録で、内容が AI コーディングエージェント系へ強く偏っている。上位 2 クラスタ（18 件ずつ）が両方ともその話題なのはそのため。フィードバック（Good / Bad）で集まった関心記事では分布が変わりうる
- **diversity 枠を埋めたときのフィードの質**。Issue #87 で novelty を直した後に、改めて枠ごとの充足を測り直す必要がある（測り直しは ADR 0007 で済ませた）

## 追記: `exploration_min_novelty` の判断は置き換えられた（2026-08-14、Issue #87）

本 ADR で切り出した Issue #87 は翌日に完了し、[ADR 0007](0007-embedding-based-novelty.md) が `compute_novelty` を topics の文字列一致から embedding のコサイン距離へ差し替えた。値そのものは 0.6 のままだが、**閾値が乗る軸が変わったため、上記の「動かしても効かない」という根拠はもう成り立たない**。現行の実装での分布と閾値の選定は ADR 0007 にある。

置き換えの対象は `exploration_min_novelty` に関する記述だけである。クラスタ数の 3 値と `strong_interest_min_similarity` の判断は、`compute_novelty` の変更に依存しないため有効なまま残る。

## 再現方法

```bash
cd backend
# 関心クラスタ・フィード枠の集計（読み取り専用）
uv run python -m techradar.measure
```

`max_clusters` の感度と topics 語彙の重なりは `techradar.measure` に無い。`techradar.measure.session.read_only_session` と `techradar.interest.service.load_cluster_sources` を使い、`ClusteringSettings` を差し替えて `build_interest_clusters` を呼ぶと再現できる。
