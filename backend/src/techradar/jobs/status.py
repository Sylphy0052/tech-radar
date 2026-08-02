"""ジョブ種別と実行中 status の対応（`PROJECT_SPEC.md` §6.2）。

ジョブ種別ごとに「今何をしているか」を表す実行中 status は異なるが、種別が
増えるたびに `JobStatus` へ値を追加すると列挙値が種別と同じペースで増え続ける。
実行中 status は「fetching / analyzing / searching」の3種類に集約できるため、
種別 → 実行中 status の写像として持たせ、`JobStatus` 自体は増やさない。
"""

from __future__ import annotations

from techradar.db.enums import JobStatus, JobType

_RUNNING_STATUS_BY_JOB_TYPE: dict[JobType, JobStatus] = {
    JobType.FETCH_ARTICLE: JobStatus.FETCHING,
    JobType.ANALYZE_ARTICLE: JobStatus.ANALYZING,
    JobType.EMBED_ARTICLE: JobStatus.ANALYZING,
    JobType.CRAWL_SOURCES: JobStatus.SEARCHING,
    JobType.GENERATE_FEED: JobStatus.SEARCHING,
    # 重複排除は記事単体の解析ではなく、推薦候補どうしの関係を見て絞り込む処理
    # （`PROJECT_SPEC.md` §17）のため、候補を探す段階と同じ searching に対応させる。
    JobType.DEDUPLICATE_ARTICLES: JobStatus.SEARCHING,
    # ログ削除は記事の取得でも解析でもない保守処理だが、実行中 status を 3 種類に
    # 集約する方針（このモジュールの冒頭）を崩さないため、fetching / analyzing の
    # どちらでもない残りの searching に対応させる。
    JobType.PURGE_OPERATION_LOGS: JobStatus.SEARCHING,
}

# claim_next で遷移しうる実行中 status の集合。reclaim_stale が
# 「実行中のまま残っている行」を判定するのに使う。
RUNNING_STATUSES: frozenset[JobStatus] = frozenset(_RUNNING_STATUS_BY_JOB_TYPE.values())


def running_status_for(job_type: JobType) -> JobStatus:
    """ジョブ種別に対応する実行中 status を返す。"""
    return _RUNNING_STATUS_BY_JOB_TYPE[job_type]
