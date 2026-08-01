"""情報源の分類（`PROJECT_SPEC.md` §10, §11）。

URL から情報源の種別と authority を決める。判定は副作用を持たない純粋関数として
実装する（`PROJECT_SPEC.md` §25）。レジストリの中身は DB と設定ファイルで管理し、
ここには判定手続きだけを置く。
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from techradar.db.enums import SourceType

# Tier 分類（`PROJECT_SPEC.md` §10）。一次情報かどうかの判断はここに集約する。
TIER_BY_SOURCE_TYPE: dict[SourceType, int] = {
    SourceType.OFFICIAL_DOCUMENTATION: 1,
    SourceType.API_SPECIFICATION: 1,
    SourceType.STANDARD_SPECIFICATION: 1,
    SourceType.OFFICIAL_RELEASE_NOTES: 1,
    SourceType.OFFICIAL_BLOG: 2,
    SourceType.OFFICIAL_RESEARCH: 2,
    SourceType.ORIGINAL_PAPER: 2,
    SourceType.OFFICIAL_GITHUB_RELEASE: 2,
    SourceType.COMPANY_TECH_BLOG: 3,
    SourceType.MAINTAINER_ARTICLE: 3,
    SourceType.PERSONAL_ARTICLE: 4,
    SourceType.TECH_MEDIA: 4,
    SourceType.NEWS_REPOST: 5,
    SourceType.SUMMARY_REPOST: 5,
    SourceType.UNKNOWN: 5,
}

# Tier 1-2 を一次情報とする（`PROJECT_SPEC.md` §10）。
PRIMARY_SOURCE_MAX_TIER = 2

# 分類できなかった場合の値。出典が辿れない記事を上位に出さないため低くする。
UNKNOWN_AUTHORITY_SCORE = 0.35

# パス規則で 1 セグメントにマッチする記号。
PATH_WILDCARD = "*"


@dataclass(frozen=True)
class SourceRule:
    """レジストリ 1 件分の判定規則。

    `path_pattern` と `github_org` はどちらも任意で、指定された条件だけが
    追加で課される。`domain` だけの規則はそのドメイン全体にかかる。
    """

    entity_name: str
    domain: str
    source_type: SourceType
    authority_score: float
    path_pattern: str | None = None
    github_org: str | None = None
    verified: bool = False


@dataclass(frozen=True)
class SourceClassification:
    """URL 1 件の判定結果。"""

    source_type: SourceType
    authority_score: float
    is_primary_source: bool
    entity_name: str | None = None
    matched_domain: str | None = None


def tier_of(source_type: SourceType) -> int:
    """情報源の Tier を返す。未定義の種別は最下位として扱う。"""
    return TIER_BY_SOURCE_TYPE.get(source_type, 5)


def is_primary_source(source_type: SourceType) -> bool:
    """一次情報（Tier 1-2）かを判定する。"""
    return tier_of(source_type) <= PRIMARY_SOURCE_MAX_TIER


def split_url(url: str) -> tuple[str, str]:
    """URL をホストとパスに分ける。

    大文字・既定ポート・クエリ・フラグメントの違いで判定が変わらないよう、
    ホストは小文字化し、パスは末尾のスラッシュを落とす。
    ホストを取り出せない入力では空のホストを返し、判断は呼び出し側に委ねる。
    """
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    path = parts.path or "/"
    if len(path) > 1:
        path = path.rstrip("/") or "/"
    return host, path


def matches_domain(host: str, domain: str) -> bool:
    """ホストが登録ドメインに一致するかを判定する。

    サブドメインは一致とみなすが、`notexample.com` のように同じ文字列で
    終わるだけの別ドメインは一致させない。
    """
    normalized = domain.strip().lower().lstrip(".")
    if not host or not normalized:
        return False
    return host == normalized or host.endswith(f".{normalized}")


def matches_path(path: str, pattern: str | None) -> bool:
    """パスが規則に一致するかを判定する。

    区切りの境界でセグメントごとに比較する。単純な前方一致にすると
    `/docs` の規則が `/documentation` に当たってしまう。

    `*` は 1 セグメントにマッチする。GitHub の Release のように
    `/<org>/<repo>/releases/...` と可変部分を挟む形は、これがないと書けない。
    """
    if not pattern:
        return True
    expected = [segment for segment in pattern.strip().strip("/").split("/") if segment]
    if not expected:
        return True
    actual = [segment for segment in path.split("/") if segment]
    if len(actual) < len(expected):
        return False
    return all(
        want == PATH_WILDCARD or want.lower() == have.lower()
        for want, have in zip(expected, actual[: len(expected)], strict=True)
    )


def matches_github_org(path: str, github_org: str | None) -> bool:
    """GitHub の組織が一致するかを判定する。

    公式 org 以外のフォークやミラーを公式扱いしないための条件。
    `github.com/<org>/<repo>/...` の第 1 セグメントを見る。
    """
    if not github_org:
        return True
    segments = [segment for segment in path.split("/") if segment]
    if not segments:
        return False
    return segments[0].lower() == github_org.strip().lower()


def _specificity(host: str, rule: SourceRule) -> tuple[int, int, int]:
    """規則の具体性。大きいほど優先する。

    ホスト完全一致 > パスの深さ > org 指定あり の順で見る。ドメイン全体に
    かかる緩い規則が、より限定された規則を上書きしないようにする。
    """
    exact_host = 1 if host == rule.domain.strip().lower().lstrip(".") else 0
    path_depth = len(rule.path_pattern.strip("/").split("/")) if rule.path_pattern else 0
    has_org = 1 if rule.github_org else 0
    return (exact_host, path_depth, has_org)


def find_matching_rule(url: str, rules: tuple[SourceRule, ...]) -> SourceRule | None:
    """URL に一致する規則のうち、最も具体的なものを返す。"""
    host, path = split_url(url)
    if not host:
        return None
    candidates = [
        rule
        for rule in rules
        if matches_domain(host, rule.domain)
        and matches_path(path, rule.path_pattern)
        and matches_github_org(path, rule.github_org)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda rule: _specificity(host, rule))


def classify_url(url: str, rules: tuple[SourceRule, ...]) -> SourceClassification:
    """URL を分類する。

    レジストリに一致する規則があればそれを使う。無い場合は `unknown` を返し、
    推定は `techradar.sources.fallback` に委ねる（責務を分けるため）。
    """
    rule = find_matching_rule(url, rules)
    if rule is None:
        return SourceClassification(
            source_type=SourceType.UNKNOWN,
            authority_score=UNKNOWN_AUTHORITY_SCORE,
            is_primary_source=False,
        )
    return SourceClassification(
        source_type=rule.source_type,
        authority_score=rule.authority_score,
        is_primary_source=is_primary_source(rule.source_type),
        entity_name=rule.entity_name,
        matched_domain=rule.domain,
    )
