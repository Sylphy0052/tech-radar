"""記事の topics / technologies からの検索クエリ生成（`PROJECT_SPEC.md` §16）。

日本語と英語を最低限の検索クエリとして生成し、原文言語が別にあれば必要に
応じてそのクエリも生成する。LLM は使わず、純粋な文字列組み立てで実装する
（追加課金ゼロが本プロジェクトの制約のため）。生成したクエリは後続の
Web 検索（別タスク）に渡され、関連記事の収集に使われる想定（Issue #9 T11）。
"""

from __future__ import annotations

from collections.abc import Sequence

# 生成するクエリ件数の既定上限。技術名 x topic の組合せは入力次第で
# 際限なく増えうるため、検索クオータを使い切らないための安全弁として
# 名前付き定数で持つ。
DEFAULT_MAX_QUERIES = 10

JAPANESE_LANGUAGE_CODE = "ja"
ENGLISH_LANGUAGE_CODE = "en"

# 言語ごとの付加語。technologies はそのまま（Kubernetes を「クベルネテス」に
# 変換しないなど）、topics も LLM の出力表記のまま使うため、日本語クエリと
# 英語クエリは付加語を変えることで初めて意味のある差になる。ハードコードを
# 散らさないよう、言語ごとの語彙はここへ集約する。
QUERY_SUFFIXES: dict[str, tuple[str, ...]] = {
    JAPANESE_LANGUAGE_CODE: ("解説", "入門"),
    ENGLISH_LANGUAGE_CODE: ("guide", "release"),
}
# source_language が日本語・英語以外のとき用の既定付加語。その言語向けの
# 翻訳語彙は持たないため、技術名 + topic のみ（付加語なし）で組み立てる。
DEFAULT_QUERY_SUFFIXES: tuple[str, ...] = ("",)


def build_search_queries(
    *,
    topics: Sequence[str],
    technologies: Sequence[str],
    source_language: str | None = None,
    max_queries: int = DEFAULT_MAX_QUERIES,
) -> tuple[str, ...]:
    """topics / technologies から検索クエリを組み立てる。

    日本語・英語のクエリを必ず生成し、`source_language` がそのどちらでも
    ない場合は原文言語のクエリも追加する（`PROJECT_SPEC.md` §16）。
    topics・technologies がどちらも空なら、検索の手がかりが無いため
    空 tuple を返す（無意味な検索でクォータを消費しないため）。
    """
    cleaned_topics = _clean_terms(topics)
    cleaned_technologies = _clean_terms(technologies)
    if not cleaned_topics and not cleaned_technologies:
        return ()

    languages = _target_languages(source_language)

    queries: list[str] = []
    seen: set[str] = set()
    for language in languages:
        for query in _build_language_queries(
            technologies=cleaned_technologies,
            topics=cleaned_topics,
            language=language,
        ):
            # 集合をそのまま返すと反復順序が不定になりテストが不安定になる
            # ため、順序を保ったまま重複だけ除く。
            if query not in seen:
                seen.add(query)
                queries.append(query)

    return tuple(queries[:max_queries])


def _target_languages(source_language: str | None) -> tuple[str, ...]:
    """クエリを生成する言語の並び。日本語・英語は常に含み、順序を固定する。"""
    languages = [JAPANESE_LANGUAGE_CODE, ENGLISH_LANGUAGE_CODE]
    normalized = _normalize_language(source_language)
    if normalized is not None and normalized not in languages:
        languages.append(normalized)
    return tuple(languages)


def _normalize_language(source_language: str | None) -> str | None:
    """BCP-47 タグから主要言語サブタグを取り出す。

    `Article.language` は `zh-Hans-CN` のような拡張タグを持ちうるため、
    先頭のプライマリサブタグだけを比較に使う。大文字小文字は区別しない。
    """
    if source_language is None:
        return None
    cleaned = source_language.strip()
    if not cleaned:
        return None
    primary = cleaned.split("-", maxsplit=1)[0].lower()
    return primary or None


def _clean_terms(values: Sequence[str]) -> tuple[str, ...]:
    """空文字列・空白のみの要素を除き、入力順を保ったまま返す。"""
    return tuple(stripped for value in values if (stripped := value.strip()))


def _build_language_queries(
    *,
    technologies: tuple[str, ...],
    topics: tuple[str, ...],
    language: str,
) -> tuple[str, ...]:
    """1 言語分のクエリを組み立てる。

    技術名を主語に、topic を補助語として添える。technologies が空なら
    topic 自体を主語に格上げする（技術名が取れなかった記事でも検索の
    手がかりを残すため）。
    """
    subjects = technologies or topics
    if not subjects:
        return ()
    # 補助語は technologies が主語のときだけ添える。topics が主語に格上げ
    # された場合に同じ語を二重に並べないため。
    auxiliary = topics[0] if technologies and topics else None
    suffixes = QUERY_SUFFIXES.get(language, DEFAULT_QUERY_SUFFIXES)

    queries: list[str] = []
    for subject in subjects:
        for suffix in suffixes:
            terms = [term for term in (subject, auxiliary, suffix) if term]
            queries.append(" ".join(terms))
    return tuple(queries)
