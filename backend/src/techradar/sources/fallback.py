"""レジストリ未登録ドメインの推定（`PROJECT_SPEC.md` §10）。

レジストリに載せられるのは主要な企業・OSS だけで、実際に集まる記事の多くは
未登録ドメインから来る。すべてを `unknown` に落とすと Tier 3-4 の記事が
一律で最下位に沈むため、ドメイン一覧と控えめな推定で妥当な Tier に寄せる。

推定は当たらないこともあるので、確信のない場合は既定値へ倒す。誤って高い
authority を与えるより、低めに出して手動修正（`PATCH /api/sources`）で
引き上げるほうが安全なため。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from techradar.db.enums import SourceType
from techradar.sources.rules import (
    SourceClassification,
    is_primary_source,
    matches_domain,
    matches_path,
    split_url,
)
from techradar.sources.weights import AuthorityWeights


@dataclass(frozen=True)
class FallbackConfig:
    """未登録ドメインの推定に使う設定。

    Attributes:
        domains: ドメインから種別への対応。サブドメインも一致とみなす。
        host_prefixes: 企業 Tech Blog とみなすホストの先頭ラベル。
        path_hints: 企業 Tech Blog とみなすパス。
        default_source_type: どれにも当たらなかった場合の種別。
    """

    domains: Mapping[str, SourceType]
    host_prefixes: tuple[str, ...] = ()
    path_hints: tuple[str, ...] = ()
    default_source_type: SourceType = SourceType.UNKNOWN


def classify_fallback(
    url: str,
    config: FallbackConfig,
    weights: AuthorityWeights,
) -> SourceClassification:
    """レジストリに無い URL の情報源種別を推定する。

    明示登録されたドメインを最優先し、次にホスト・パスの手掛かりを見る。
    """
    host, path = split_url(url)
    source_type = _infer_source_type(host, path, config)
    return SourceClassification(
        source_type=source_type,
        authority_score=weights.score_for(source_type),
        is_primary_source=is_primary_source(source_type),
    )


def _infer_source_type(host: str, path: str, config: FallbackConfig) -> SourceType:
    """ホストとパスから種別を推定する。"""
    if not host:
        return config.default_source_type

    listed = _lookup_listed_domain(host, config.domains)
    if listed is not None:
        return listed

    if _looks_like_company_tech_blog(host, path, config):
        return SourceType.COMPANY_TECH_BLOG

    return config.default_source_type


def _lookup_listed_domain(host: str, domains: Mapping[str, SourceType]) -> SourceType | None:
    """一覧に載っているドメインの種別を返す。

    複数が一致する場合は、より限定的な（長い）ドメインを採る。
    """
    matched = [domain for domain in domains if matches_domain(host, domain)]
    if not matched:
        return None
    return domains[max(matched, key=len)]


def _looks_like_company_tech_blog(host: str, path: str, config: FallbackConfig) -> bool:
    """企業 Tech Blog らしいかを判定する。

    ホストの先頭ラベルはラベル単位で比較する。前方一致にすると
    `technology-news.example.com` が `tech` に当たってしまう。
    """
    first_label = host.split(".")[0]
    if first_label in config.host_prefixes:
        return True
    return any(matches_path(path, hint) for hint in config.path_hints)
