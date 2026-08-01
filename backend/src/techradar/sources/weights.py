"""authority スコアの重み（`PROJECT_SPEC.md` §10, §25）。

スコアはコードに埋め込まず設定ファイルで管理する。運用しながら調整するため、
値の変更にコード修正とデプロイを伴わせない。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from techradar.db.enums import SourceType

# 設定に無い種別へ適用する値。判定を止めないためのフォールバック。
DEFAULT_UNKNOWN_SCORE = 0.35


@dataclass(frozen=True)
class AuthorityWeights:
    """情報源の種別ごとの authority スコア。"""

    by_source_type: Mapping[SourceType, float]

    def score_for(self, source_type: SourceType) -> float:
        """種別に対応するスコアを返す。

        設定漏れがあっても判定を止めず、`unknown` の値へ寄せる。
        `unknown` すら無ければ既定値を使う。
        """
        if source_type in self.by_source_type:
            return self.by_source_type[source_type]
        return self.by_source_type.get(SourceType.UNKNOWN, DEFAULT_UNKNOWN_SCORE)
