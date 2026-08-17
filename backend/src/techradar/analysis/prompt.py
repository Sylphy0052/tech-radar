"""記事解析の指示文。

本文は非信頼入力として `techradar.llm.prompt` が区切りタグで囲む。
ここには「何を抽出するか」だけを書き、防御はその層に任せる。

1 回の呼び出しで全項目をまとめて生成する。項目ごとに呼び分けると
呼び出し回数がそのままコストになる（`PROJECT_SPEC.md` §24）。
"""

from __future__ import annotations

from techradar.analysis.schema import MAX_SUMMARY_LENGTH

# 上限はスキーマ側の値をそのまま埋め込む。ここへ数値を直接書くと、
# スキーマの上限を変えたときに指示だけ古い値が残る。
ANALYSIS_INSTRUCTION = f"""技術記事を解析し、指定された JSON を出力してください。

各項目の作り方:

- summary_ja: 日本語で 200 字程度に要約する。**{MAX_SUMMARY_LENGTH} 字を超えてはならない**。
  超えた分は切り捨てられ、要約が途中で終わる。記事が何を主張し、読者が何を得られるかを
  書く。前置きや「この記事では」は不要。発表や新機能の列挙が多い記事では、
  個々を並べずに全体の要点へまとめる。
  **原文が何語であっても日本語で書く**。
- translated_title: 原文タイトルの日本語訳。原文が日本語なら null にする。
- domain: 大分類。例: Generative AI / Web Frontend / Database / Security
- category: domain の中の位置づけ。例: Agentic Engineering / State Management
- topics: 記事の主題を表す語。3〜5 個。一般語ではなく記事固有の概念を選ぶ。
- technologies: 記事に登場する製品・OSS・仕様の名前。無ければ空配列。
- content_type: concept（概念解説）/ implementation（実装・手順）/
  research（研究・論文）/ news（発表・リリース）から選ぶ。
- difficulty: beginner / intermediate / advanced から選ぶ。
- technical_quality: 0.0〜1.0。具体的なコードや実測値があるか、
  出典が明示されているか、内容が表面的でないかで判断する。

出力は JSON のみ。説明文やコードフェンスを付けないでください。"""
