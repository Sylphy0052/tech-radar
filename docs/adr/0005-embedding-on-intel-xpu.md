# ADR 0005: Embedding を Intel Arc (XPU) で実行する

- ステータス: 採用
- 日付: 2026-08-12
- 関連: Issue #77 / [ADR 0001](0001-technology-stack.md), [ADR 0003](0003-embedding-runtime.md)

## コンテキスト

ADR 0001 と ADR 0003 は、実行環境を「WSL2 上のローカルマシン（RTX 4050 Laptop 6GB / i7-13700H 20コア / RAM 16GB）」として、Embedding を NVIDIA GPU で動かす前提で書かれている。ADR 0003 は PyTorch を CUDA 12.8 ビルドへ固定することで `cuda_available True` を得た記録である。

Issue #75 の着手条件を確認する過程で、**194 記事のうち embedding を持つものが 0 件**であることが分かり、原因を追ったところ実行環境が変わっていた。

## 問題

現行機に NVIDIA GPU は存在しない。

```text
$ nvidia-smi
bash: nvidia-smi: command not found

$ python -c "import torch; print(torch.__version__, torch.cuda.is_available())"
2.9.1+cu128 False

$ ls /usr/lib/wsl/lib/
libd3d12.so  libd3d12core.so  libdxcore.so     ← NVIDIA 系ライブラリが無い

$ powershell.exe -Command "Get-PnpDevice -Class Display | Select Status,FriendlyName,InstanceId"
Status       : OK
FriendlyName : Intel(R) Arc(TM) Graphics
InstanceId   : PCI\VEN_8086&DEV_7D55&SUBSYS_00E61E26&REV_08\3&11583659&1&10
```

Display クラスのデバイスは Intel Arc の 1 件のみで、無効化された NVIDIA GPU も存在しない（`VEN_10DE` が無い）。

| 項目 | ADR 0001 / 0003 の前提 | 現行機 |
| --- | --- | --- |
| GPU | RTX 4050 Laptop 6GB (NVIDIA) | Intel Arc Graphics (統合GPU) |
| CPU | i7-13700H 20コア | Core Ultra 7 165H (16コア / 22論理、Meteor Lake-H) |
| RAM | 16GB | 13GB (WSL へ割当) |

**ADR 0003 の対処（CUDA 12.8 ビルドへの固定）は現行機では意味を持たない。** CUDA は NVIDIA 専用であり、ビルドを固定しても CPU へフォールバックする。その結果 Embedding は CPU 実行となり、`embed_article` ジョブが 82 件滞留したまま 1 件も完了していなかった。

Embedding は重複排除（Issue #10）、関心プロファイルとクラスタ（#15、#20）、推薦の類似度計算（#11）の土台であり、0 件では推薦が実質的に機能しない。

## 決定

**PyTorch の XPU バックエンドで Intel Arc を使う。**

1. PyTorch を XPU ビルド（`https://download.pytorch.org/whl/xpu`、`torch==2.13.0`）へ切り替える
2. デバイス選択（`embedding/qwen.py` の `resolve_device`）の `auto` を **cuda → xpu → cpu** の順にする

CUDA を先に見るのは、NVIDIA の dGPU がある環境ではそちらが統合GPUより速いためである。現行機では cuda が使えないので xpu が選ばれる。NVIDIA 機へ戻したときは分岐がそのまま効く。

### intel-extension-for-pytorch (IPEX) は使わない

PyTorch 2.5 以降、Intel GPU (XPU) のサポートは stock PyTorch にネイティブ統合されている。IPEX は 2026年3月末で EOL のため新規に採用しない。

### sentence-transformers はデバイスを明示する必要がある

`SentenceTransformer` のデバイス自動選択は `cuda` / `mps` / `cpu` しか見ず、**XPU は候補に入らない**。既存の `load_model()` は `device=` を明示的に渡しているためそのまま動くが、将来この指定を省く変更が入ると黙って CPU へ落ちる。

## 実測 (2026-08-12)

### デバイス

```text
torch: 2.13.0+xpu
xpu_available: True
device_count: 1
device_name: Intel(R) Graphics [0x7d55]
platform: Intel(R) oneAPI Unified Runtime over Level-Zero
driver_version: 1.6.33578+15
total_memory: 16763MB (メインメモリ共有)
max_compute_units: 128 / gpu_eu_count: 128 / gpu_subslice_count: 16
has_fp16: 1 / has_fp64: 1 / is_integrated_gpu: 1
```

Level Zero ランタイム（`libze_intel_gpu.so.1`）と Intel Compute Runtime（`intel-opencl-icd 25.18.33578.15`）は導入済みで、WSL の GPU パススルー（`/dev/dxg`）も生きていた。不足していたのは PyTorch の XPU ビルドだけだった。

### Qwen3-Embedding-0.6B の所要時間

入力は 2640 文字を 3 件。暖機を 1 件はさんでから計測した。

| デバイス | モデル読み込み | 3 件 | 1 件あたり |
| --- | --- | --- | --- |
| XPU | 16.4 秒 | 3.96 秒 | **1.32 秒** |
| CPU | 8.6 秒 | 172.12 秒 | 57.37 秒 |

XPU が CPU の約 43 倍だった。滞留していた `embed_article` 82 件は、XPU なら約 110 秒、CPU なら約 78 分の見込みになる。

ADR 0003 が記録した RTX 4050 での実測（長文 8 件 2433ms）とは入力の長さと件数が違うため直接比較はできない。

### 結果の同等性

| 項目 | 値 |
| --- | --- |
| 出力次元 | (3, 1024) — CPU と一致 |
| XPU と CPU の cosine 類似度 | 0.999873 |
| 最大絶対差 | 0.002930 |

浮動小数点の差はあるが、類似度検索の用途では同等とみなせる。

## 帰結

### 利点

- Embedding が 1 件あたり 1.32 秒で動く。CPU 実行（57.37 秒）では現実的でなかった巡回のたびの埋め込み生成が成立する
- 追加課金ゼロの制約（緩和禁止）を維持したまま GPU を使える
- IPEX のような追加パッケージに依存しない

### 欠点とリスク

- **統合GPU はメインメモリを共有する。** WSL への割当が 13GB のため、バッチサイズを上げすぎるとホスト側と食い合う。ADR 0003 の RTX 4050 は専用 VRAM 6GB を持っていたので、この制約は新しい
- **`triton-xpu` を直接依存として宣言する必要がある。** torch の XPU ビルドが要求する推移依存だが PyPI には無く XPU インデックスにしか置かれていない。`explicit = true` のインデックスは直接依存しか解決しないため、宣言しないと `uv sync` が「no version of triton-xpu==3.7.2」で失敗する（ADR 0003 の torch と同じ理由）
- **NVIDIA 機と Intel 機を同時には満たせない。** uv のインデックス指定はプラットフォーム条件で分岐できないため、`pyproject.toml` はどちらか一方を選ぶ。移る際は手で差し替える。コード側の `resolve_device` は両方に対応しているので、差し替えだけで動く
- 実行時に `Can't initialize Level Zero Sysman` の警告が出る。電力や温度を取る管理 API が使えないだけで、計算には影響しない

### 前提が変わった場合

ハード構成が変わったことに気付けたのは、embedding が 0 件だったという結果からの逆算だった。`resolve_device` を環境に追随させることで、実行環境が変わっても自動で最善のデバイスを選ぶ形にしてある。ただし `pyproject.toml` の PyTorch インデックスだけは手で切り替える必要が残る。
