# TechRadar

> グローバル規約 (`~/.claude/CLAUDE.md`, `~/.claude/rules/`, `~/.claude/docs/gitlab/README.md`) を継承する。本ファイルに書かれた項目のみ、それらを override する。

## プロジェクト概要と制約

技術記事に特化した Google Discover 型パーソナライズドフィード。単一ユーザー・ローカル実行が前提。

コードから読み取れない制約 (**緩和禁止**):

- **追加課金をゼロにする** — LLMは Claude Code CLI headless (サブスク枠)、Embedding はローカル実行。OpenAI/Anthropic API の直接利用は却下済み
- **サーバーを常駐させない** — 定期スケジューラを置かず、巡回は UI の実行ボタンから起動する。常駐するのは PostgreSQL コンテナのみ
- **多言語クロスリンガルが必須** — 日本語特化の埋め込みモデル (ruri-v3等) は JMTEB スコアが高くても採用しない

構成: `backend/` (Python + uv)、`frontend/` (Next.js)、`infra/` (docker-compose)、`scripts/ai-harness/`。

判断の根拠を探すとき: 要件は [PROJECT_SPEC.md](PROJECT_SPEC.md)、確定事項は [docs/decisions.md](docs/decisions.md)、技術選定の根拠は [docs/adr/](docs/adr/)。ただしPROJECT_SPEC.mdには実装へ追随させていない節がある (データモデル案 §19、API案 §20、MVPスコープ §22の「必須」、実装順序 §23、完了条件 §26、初回決定事項 §27)。どの節が現役でどの節が記録かは同ファイル冒頭の「この文書の読み方」にまとめてある。§22の「MVPでは実装しない」だけは記録ではなく現役のスコープ境界なので、そこへ手を出す前にPROJECT_SPEC.mdを更新する。現行のスキーマは [backend/src/techradar/db/models.py](backend/src/techradar/db/models.py)、現行のAPIは [backend/openapi.json](backend/openapi.json) を見る。

## 開発コマンド

- `./run.sh` — backend + frontend を起動する (PostgreSQL は自動起動、ジョブワーカーは backend プロセスに同居)
- `./run.sh --stop` — PostgreSQL コンテナも含めて停止する
- `scripts/ai-harness/check.sh` — lint / format / 型チェック / テスト / 依存脆弱性監査 / secret検知を一括実行する。PostgreSQL が未起動なら自動で立ち上げる。**手動で実行する** (2026-08-12 に commit 前の自動実行を廃止した。下記「品質チェックは手動運用」を参照)
  - 互いに独立したチェックは並列で走る (Issue #61)。出力は混ざらないよう、全ジョブの完了後にまとめて表示する
  - 依存監査と secret検知は下記「secret検知と依存脆弱性監査」を参照。audit の2つはネットワークを使う
  - pytest と vitest のワーカー数は `PYTEST_WORKERS` / `VITEST_WORKERS` で変えられる。既定はコア数の半分と 8 の小さい方 (22コア機なら 8、8コア機なら 4)。22コア機での実測では 8 + 8 が最速だった。`PYTEST_WORKERS=1` で pytest の並列化を切れる

## 品質チェックは手動運用 (Issue #76)

グローバル規約と本リポジトリの両方に効く。2026-08-12 に、commit 前の `check.sh` 自動実行を廃止した (グローバル hook `pre-bash-guard.sh` から削除。全プロジェクト対象)。commit のたびに 73〜100秒待つコストが、1名運用における検知の利得を上回るという判断による。

**lint / format / 型チェック / テストを自動で回す仕組みは、どこにも無い。** commit 前の hook も (Issue #76)、MR の CI も (Issue #82) 廃止した。壊れたことに気付くのは、次に手で `check.sh` を回したときだけである。

- 品質チェックは `scripts/ai-harness/check.sh` を**手動で実行する**
- MR を作る前に一度は全緑を確認する (推奨。機械強制はしない)
- 完了報告の Evidence には、手動実行した `check.sh` の PASS ログを使う
- commit のたびに回すかは変更内容で判断してよい (ドキュメントのみの変更など、明らかに影響しない場合は省略可)

## secret検知と依存脆弱性監査 (Issue #83)

`check.sh` は lint / 型 / テストに加えて、次の3つを毎回走らせる。CI を止めた以上 (Issue #82)、これらが走る機会は `check.sh` を手で回したときしか無い。

| ジョブ | 実体 | 単体の所要 |
| --- | --- | --- |
| `secret検知` | `detect-secrets-hook --baseline .secrets.baseline` | 約27秒 |
| `backend: uv audit` | `uv audit` (OSV を参照) | 約2秒 |
| `frontend: npm audit` | `npm audit --audit-level=high` | 約1秒 |

いずれも並列ジョブなので、壁時計の支配項である pytest より短い限り `check.sh` 全体の所要時間は変わらない。3つを追加した状態での実測は 1分46秒 (依存を取得済みの状態) だった。

**audit の2つはネットワークを使う。** `uv audit` は OSV へ、`npm audit` は npm registry へ問い合わせる。オフラインでは失敗するため、機内などで作業するときは `check.sh` が通らないことがある。ネットワーク起因の失敗と、実際に脆弱性が見つかった失敗は、出力を読んで区別する。

**`uv audit` は uv 0.12 時点で experimental である。** `--preview-features audit-command` を付けて警告を抑えている。uv の更新でオプションや出力が変わる可能性がある。

### secret の誤検知が出たとき

`detect-secrets` の走査対象は git の追跡下にあるファイルだけで、`.secrets.baseline` に載っている検出は既知として無視される。baseline に無い検出が出ると `check.sh` が落ちる。

誤検知だと確認できたら、baseline を作り直して差分を commit する。

```bash
cd <リポジトリルート>
./backend/.venv/bin/detect-secrets scan > .secrets.baseline
```

作り直す前に、**検出された箇所を1件ずつ目で見て、本物の secret が混ざっていないことを確かめる**。baseline はハッシュしか持たないため、一度取り込むと中身の再確認ができない。

現在 baseline に入っている17件は、いずれも実害が無いことを確認済みである。内訳は alembic のリビジョンID 9件 (高エントロピー文字列として誤検知)、テストのダミー資格情報 4件、テスト関数名の誤検知 2件 (長い識別子が GitHub Token パターンに当たる)、CI の使い捨て資格情報 1件、環境変数テンプレートの接続文字列 1件。

## CI は使わない (Issue #82)

CI は 2026-08-12 に停止した。同日に一度再開している (Issue #81) が、実測して割に合わないと分かったため止め直した。[.gitlab-ci.yml](.gitlab-ci.yml) の workflow rules に `- when: never` が入っており、プロジェクト設定の `builds_access_level` も `disabled` にしてある。**pipeline は merge request でも main への push でも作られない。**

止めた理由は2つある。

- **CI が走らせる検証は、MR 前に手で回す `check.sh` と同じものである。** むしろ `check.sh` の方が広く、`openapi.json` と `api-schema.d.ts` の鮮度チェックは CI 側に無い。1名運用では、事後にもう一度同じ検証を走らせる価値が薄い
- **backend のジョブが1本あたり10分前後かかっていた。** 実測は ruff-check 632秒 / ruff-format 604秒 / ty-check 600秒 / pytest 295秒で、pipeline 全体では数十分規模になる (詳細はIssue #82)。支配的なのは `uv sync` による torch-xpu 一式 (数GB) のダウンロードで、runner 202 は共有 runner (`instance_type`) のため分散キャッシュが未設定であり `cache` は一度も効いていない (ジョブログに `WARNING: Cache file does not exist`)。CI 側だけ CPU 版 torch へ切り替える高速化は可能だが、`pyproject.toml` / `uv.lock` / `run.sh` / `check.sh` の変更を伴い、得られるのは上記の重複した検証でしかない

**品質の担保は、手動の `check.sh` と MR の self review だけである。** 壊れたまま main へ入ってもそれを検知する自動の仕組みは無いので、MR を作る前に `check.sh` を全緑にする運用を守る。

再開するときは [.gitlab-ci.yml](.gitlab-ci.yml) の `- when: never` を、停止理由を書いた直上のコメントごと削除し、あわせて `builds_access_level` を `enabled` へ戻す (`jobs_enabled` は deprecated で、実体はこちら)。ジョブ定義そのものは残してあるため、この2箇所で戻る。ただし上記の所要時間はそのまま再現するので、戻す前に高速化を済ませておく。CI を無効にしている間は pipeline 系の API も応答しなくなるため、過去の実測値を取り直すこともできない。

## 開発フロー (強制)

Issue起票を経ずに実装へ着手しない。以下の順序で進める (skillの実体はグローバル規約を参照。一括実行は `/gitlab-dev-cycle`)。

1. **Issue作成** — `gitlab-issue` で起票する。着手対象のIssueが存在しない状態でコードを書き始めない
2. **着手前の衝突確認** — Issueのコメント履歴を見て、別セッションが動いていないか確かめる。実装計画のコメントが既にあれば着手しない。防げる範囲と防げない範囲は下記「セッション間で着手が衝突する」に書く

   ```bash
   glab api "projects/:id/issues/<IID>/notes" | jq '[.[] | select(.system == false) | {created_at, body: .body[:80]}]'
   ```
3. **着手時のIssue更新** — ラベルを `Todo` → `InProgress` へ付け替え、実装計画 (受入基準・変更対象・想定リスク) をIssueにコメント追記する。ブランチ作成前後のどちらでもよいが、実装開始前に完了させる
4. **ブランチ作成** — `gitlab-branch` でIssueを元に `<type>/<IID>/<slug>` を作成する (worktree隔離が既定)
5. **実装** — `implement` (TDD: RED→GREEN→REFACTOR) → `gitlab-commit`
6. **MR** — `gitlab-mr-flow` で MR作成 (Draft) → self review (`gitlab-mr-review` self) → 指摘修正 (`gitlab-mr-address`) → merge。マージ条件は下記「自己マージ許可」に従う
7. **終了時の更新** — `gitlab-cleanup` で Issue を `Done` + close、`docs/mr/` `docs/issue/` の移動、ブランチ/worktree掃除まで実行する。親ロードマップIssue (`label=roadmap`) のチェック項目も `gitlab-roadmap update` で更新する
8. **セッション終了の明示** — cleanup 完了後、以下3点を必ず出力してから応答を終える。黙って次の作業へ進まない
   - **セッション終了**であることを明示する (「1サイクル完了、セッション終了」と書く)
   - **次に着手するIssue**を提案する (`gitlab-roadmap next` の結果を根拠に、IID・タイトル・選定理由を1行ずつ)
   - 提案できるIssueが無い、または今回の作業で**新たな課題を検出した**場合は、`gitlab-issue` でIssueを新規作成してからその IID を次着手候補として提示する

### よく使うskill (グローバル定義)

| skill | 用途 |
| --- | --- |
| `gitlab-issue` | Issue の起票 / 本文整頓 / 別問題の切り出し |
| `gitlab-branch` | Issue から `<type>/<IID>/<slug>` ブランチ + worktree 作成、`InProgress` 遷移 |
| `gitlab-commit` | Conventional Commits でcommit (`check.sh` は手動実行。hook 強制は廃止済み) |
| `gitlab-mr-flow` | MR作成 (Draft) → self review → 修正 → ready 化 |
| `gitlab-mr-review` | MR diff の並列レビュー (self / other モード、先祖返り検出) |
| `gitlab-cleanup` | マージ後の整理 (Issue close、docs移動、branch/worktree掃除、roadmap更新) |
| `gitlab-roadmap` | ロードマップ Issue #17 の維持と次に着手する Issue の提示 |
| `gitlab-dev-cycle` | 上記を状態判定しながら一括実行する orchestrator (再実行で続きから) |
| `handoff` | セッションが長くなったときの引き継ぎプロンプト生成 |

### ステータスラベル運用 (有効)

本リポジトリは `Todo` / `InProgress` / `Review` / `Done` の4種ラベルを運用する。グローバル規約で「ラベル体系のあるリポジトリのみ」と緩和されているラベル遷移処理は、**本リポジトリでは全て実行する** (`gitlab-branch` のInProgress遷移、`gitlab-mr-flow` のReview遷移、`gitlab-cleanup` のDone遷移)。

`phase1`〜`phase5` および `roadmap` ラベルはロードマップ管理用。

## GitLab運用の override (本リポジトリ限定)

### 自己マージ許可

グローバル規約の「MR自己マージ禁止」は**本リポジトリでは適用しない**。以下を満たせば作成者自身が `glab mr merge <IID> --remove-source-branch` を実行してよい (理由: 1名運用でレビュアーが不在。承認待ちと冷却期間が無意味な遅延にしかならない)。

- [ ] `gitlab-mr-review` skill の self モードを実行済みで、CRITICAL/HIGH の指摘がゼロ

上記を満たせば即マージする。以下のグローバル要件は本リポジトリでは**撤廃**する:

- reviewer への承認依頼 (`gitlab-mr-flow` の「reviewer依頼note投稿」ステップは不要)
- reviewer または権限保有者による merge 実行 (`~/.claude/skills/gitlab-mr-flow/SKILL.md` L112, L134)
- self-merge 前の24時間待機・翌日見直し (`~/.claude/docs/gitlab/README.md` L98)
- **CI pipeline の完了待ち** — CI を停止しているため待つ対象が無い (Issue #82、上記「CI は使わない」)。`glab mr merge <IID> --remove-source-branch` を即実行してよい

  この根拠は3度変わっている。当初は「commit 前に check.sh が全緑であることを hook が強制済み」だったが、2026-08-12 にその強制を廃止し (Issue #76)、同日 CI を再開して (Issue #81)、同日その CI も止めた (Issue #82)。現在の担保は、マージ前の self review と手動の `check.sh` の2つだけである

  **マージした後に壊れが判明した場合は fix-forward する。** `--remove-source-branch` で元のブランチは消えているため、revert ではなく main の先端から新しいブランチを作って直す。対象の Issue がまだ open ならそれを流用してよく、閉じていれば起票する

### 維持する項目 (緩和禁止)

override はマージ主体と承認要件のみ。以下は**引き続き強制**する:

- 1 Issue 1 Branch / ブランチ命名 `<type>/<IID>/<slug>`
- MR本文への `Closes #<IID>` 必須
- `glab mr create` / `glab mr merge` への `--remove-source-branch` 必須
- `git commit --no-verify` / `-n` 禁止 (グローバル hook が機械拒否する。check.sh の自動実行は廃止したが、将来 pre-commit hook を置いたときの迂回を防ぐため禁止自体は維持する)
- MR作成後の `gitlab-mr-review` 実行そのもの (self モードでよいが、スキップは不可)
- `glab` CLI 使用 (`gh` 禁止)

## 注意点 (Gotcha)

### セッション間で着手が衝突する (Issue #68, #69)

複数のセッションを並行させていると、同じ Issue へ同時に着手することがある。実際に Issue #68 で起き、片方の実装 (テスト33件) を全部破棄した。

**ラベルと worktree とオープンMRでは検出できない。** ラベルは自分で `Todo` → `InProgress` へ付け替えるため、向こうが付ける前に自分が付けると区別できない。`git worktree list` とオープンMRは、相手がまだ作っていない段階では空を返す。#68 のときも着手直前に両方を見て空だった。

そのため開発フローの手順2でコメント履歴を見る。ただし**これで防げるのは、相手が先にコメントを投稿し終えている場合だけ**である。両者がほぼ同時に確認して、どちらにもまだコメントが無い状態なら素通りする。#68 の実際の間隔は30秒で、この手順があっても防げなかった可能性が高い。

機械的なロック (Issue へのアサイン、ブランチ名の先行 push) は今のところ入れていない。1名運用で並行させる頻度に対して仕掛けが重いため、まず確認手順だけを置いて様子を見る。

**衝突が判明したときは、MR を作っている側へ譲る。** 譲る側は commit 前に自分の変更を破棄する。共有された worktree で commit すると相手のブランチへ混入する。Issue には取り下げのコメントを残し、方式の比較と判断根拠を書いておく (計画コメント自体は消さない。経緯が追えなくなる)。

### テストの同時実行は worktree 単位・プロセス単位に分離済み (Issue #23, #33)

backend の pytest はセッション開始時にテスト用DBを DROP/CREATE する ([backend/tests/conftest.py](backend/tests/conftest.py))。DB名は `techradar_test_<8桁hash>_<pid>` で、ハッシュ部分が作業ディレクトリ（worktree）、PID 部分がプロセスを表す。worktree を分けても分けなくても、別セッションが同時に pytest を回して互いのDBを破壊し合うことはない。

check.sh は pytest を pytest-xdist で並列実行する (Issue #61)。xdist のワーカーは別プロセスなので、この PID 単位の分離がそのまま効く。ワーカー数ぶんのテスト用DBが同時に作られ、それぞれにマイグレーションが適用される。

DBが増え続けないよう、セッション終了時に自分のDBを DROP し、異常終了で残った孤児DB（生存していない PID のもの）は次回のセッション開始時に掃除する。掃除は消さない側へ倒してあり、PID が生存している・PIDとして解釈できない・接続が残っている・自分自身、のいずれかに当たるDBには手を触れない。判定ロジックは [backend/tests/db_process_isolation.py](backend/tests/db_process_isolation.py) にある。

frontend の vitest も同じ理由で `coverage.reportsDirectory` を `coverage/<pid>` に分けてある ([frontend/vitest.config.mts](frontend/vitest.config.mts))。共有していた頃は同時実行すると片方が `Something removed the coverage directory` で落ちた。孤児ディレクトリの掃除は [frontend/vitest.global-setup.ts](frontend/vitest.global-setup.ts) が行う。

worktree を削除すると、そのハッシュを持つテスト用DBはどのworktreeからも掃除されなくなる（他worktreeのDBには触らない設計のため）。この掃除は `gitlab-cleanup` skill の worktree 削除ステップから呼ばれる（Issue #60）。cleanup を通せば毎回 dry-run で候補が提示されるので、掃除の機会がその都度できる。ただし下記の理由で見送ることがあり、その場合は残る。

cleanup を経由せず worktree を消したときは `./scripts/cleanup-test-databases.sh` で確認し、`--apply` を付けて消す。生存しているworktreeのDBと、接続が残っているDBには触らない。

掃除は main workspace から、対象の worktree を削除し終えた後に実行する。worktree の中から実行すると、その worktree は生存扱いのままで掃除対象に入らない（実測で確認）。

### 削除されずに残ることがある（Issue #63）

実行中の他セッションのDBを巻き込んで消していたため、掃除に3つの保護を入れた。保護されたDBはレポートに理由付きで出る。

- **DB名のPIDが生存している** — 別セッションの pytest が使っている可能性が高いもの。ただしPIDは循環するので、作成から24時間を超えたDBではPIDを信用しない
- **作成から10分未満** — `--min-age-minutes` で変更、`0` で無効化。PIDを持たない旧形式や、実在しないPIDを使うテストのダミーはこれで守る
- **接続が残っている** — 従来どおり

そのため **worktree を消した直後の cleanup では、そのDBが猶予期間に入っていて掃除されないことがある**。溜めないことが目的なので、次回の cleanup で回収されればよい、と割り切っている。すぐに消したいときは `--min-age-minutes 0` を渡す（他セッションが動いていないことを確認してから）。

`--apply` で一度に消せる件数には上限がある（既定10件）。超えると何も削除せず止まるので、dry-run で内容を確かめてから `--max-delete N` を指定して再実行する。想定外の大量削除を機械的に止めるための仕掛け。

保護が入ったので以前ほど神経質になる必要はないが、**別セッションがテストを走らせている最中の `--apply` は避ける**のが確実（実測では2件を巻き込んで消し、別の回では実行中のDB 4件が候補に並んだ）。dry-run に心当たりのないDB名が出たら、他セッションの実行が終わってから改めて実行する。

これは Issue #59 の対応後も残る。掃除スクリプトは「生存している worktree のどれにも属さないDB」を候補にするが、[backend/tests/fake_worktree_roots.py](backend/tests/fake_worktree_roots.py) が返すのは実在しないダミーパスであり、生存 worktree のいずれとも一致しないため。テスト実行中でも、その瞬間に接続が張られていなければ候補に入る。

### テストに壁時計の絶対時間を書かない (Issue #61)

check.sh は複数のチェックを並列で走らせ、pytest 自体も複数プロセスへ分散させる。そのため「1秒以内に終わること」のような余裕の無い時間アサーションは、対象の実装が速いままでも落ちる。実測では、線形走査の ReDoS 回帰テストがカバレッジ計測のオーバーヘッド（約3倍）と CPU 競合（さらに約3倍）が重なって 0.18秒 → 1.8秒まで伸びた。壁時計を CPU 時間 (`time.process_time`) へ替えても、キャッシュやメモリ帯域の奪い合いまでは避けられない。

時間を測るなら、通す側と落とす側の実測値を両方持ったうえで、その間に桁で離して上限を引く（[backend/tests/test_bulk_import.py](backend/tests/test_bulk_import.py) の `_REDOS_CPU_SECONDS_LIMIT`）。「入力を倍にしたときの伸び率で見る」形も試したが、入力サイズでキャッシュの効き方が変わるため線形の実装でも 3.7倍を観測し、こちらは安定しなかった。時間そのものではなく回数を数えられるなら、そちらの方が確実（フロントエンドの同種の失敗は Issue #40, #41）。

### frontend の Next.js は訓練データと異なる

[frontend/AGENTS.md](frontend/AGENTS.md) の警告どおり、この Next.js は破壊的変更を含み API・規約・ファイル構成が既知のものと違う。frontend 配下のコードを書く前に `node_modules/next/dist/docs/` の該当ガイドを読む。deprecation notice に従う。
