# ADR 0003: Embedding の実行環境と PyTorch のビルド選択

- ステータス: **置き換え済み** — [ADR 0005](0005-embedding-on-intel-xpu.md) を参照
- 日付: 2026-08-01
- 関連: `PROJECT_SPEC.md` §16 多言語仕様 / Issue #6 / [ADR 0001](0001-technology-stack.md) / [ADR 0005](0005-embedding-on-intel-xpu.md)

> **2026-08-12 追記**: この ADR が前提とする実行環境（RTX 4050 Laptop 6GB）は現行機に存在しない。現行機は NVIDIA GPU を持たず Intel Arc Graphics（Core Ultra 7 165H の統合GPU）のみを持つため、ここで決めた CUDA 12.8 ビルドへの固定は意味を持たず、Embedding は CPU へフォールバックしていた（1 件あたり 57 秒）。実行環境と PyTorch のビルド選択は [ADR 0005](0005-embedding-on-intel-xpu.md) で置き換えた。以下は当時の記録として残す。

## コンテキスト

ADR 0001 で Embedding に `Qwen/Qwen3-Embedding-0.6B` をローカル GPU（RTX 4050 Laptop 6GB）で実行すると決めた。実装時に、**PyPI の既定 PyTorch では CUDA が使えない**ことが判明したため、その判断を記録する。

## 問題

`uv add sentence-transformers` で入る PyTorch は `2.13.0+cu130`（CUDA 13.0 ビルド）だった。この環境のドライバは 572.83 で対応 CUDA は 12.8 のため、CUDA が初期化できず CPU へフォールバックした。

```text
UserWarning: CUDA initialization: The NVIDIA driver on your system is too old
(found version 12080).
torch 2.13.0+cu130
cuda_available False
```

CPU でも動作はするが、GPU 前提で選定したモデルの利点（速度）が失われる。

## 決定

**PyTorch を CUDA 12.8 ビルドに固定する。**

```toml
[[tool.uv.index]]
name = "pytorch-cu128"
url = "https://download.pytorch.org/whl/cu128"
explicit = true

[tool.uv.sources]
torch = { index = "pytorch-cu128" }
```

あわせて `torch` を**直接依存として宣言**する。`sentence-transformers` の推移的依存のままでは `tool.uv.sources` が適用されず、PyPI の cu130 が選ばれ続けた。

バージョンは `2.9.1` に固定した。cu128 インデックスが提供する最新がこれで、`2.13.0` は cu130 のみの提供だったため。

### 結果

```text
torch 2.9.1+cu128
cuda_available True
device NVIDIA GeForce RTX 4050 Laptop GPU
vram_MiB 6140
```

## 実測

| 項目 | 値 |
| --- | --- |
| 出力次元 | 1024（`vector(1024)` と一致） |
| 同一内容の日英ペアの類似度 | 0.8747 |
| 無関係なペアの類似度 | 0.1332 |
| 差 | 0.7415 |
| 短文 3 件の処理時間 | 11373 ms（モデル読み込み込み） |
| 長文 8 件の処理時間 | 2433 ms（読み込み済み） |
| ピーク VRAM（短文） | 1.13 GiB / 6.0 GiB |
| ピーク VRAM（長文 8 件） | 3.65 GiB / 6.0 GiB |

クロスリンガル性は要件（言語を限定しない）を満たす。VRAM は長文をまとめて処理しても 6GB に収まる。

## 帰結

### 利点

- GPU で動作し、長文 8 件が約 2.4 秒で処理できる
- VRAM に余裕があり、バッチサイズを上げる余地がある

### 欠点とリスク

- **PyTorch のバージョンがドライバに縛られる**。ドライバを更新すれば新しい cu ビルドへ移れるが、それまでは 2.9.x 系に留まる
- cu128 インデックスからの取得は数百 MB〜数 GB あり、初回セットアップが長い
- モデルの初回ダウンロードに約 1.2GB かかる

### 運用上の注意

- CUDA が使えない環境では自動的に CPU へフォールバックする。動作はするが大幅に遅くなる
- 実モデルを読み込むテストは既定で実行しない。`TECHRADAR_RUN_MODEL_TESTS=1` を付けたときだけ動く。読み込みに時間がかかり、CI では GPU も使えないため
