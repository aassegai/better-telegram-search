"""ID-preserving pairwise scoring contract shared by reranker adapters."""

import math

from telegram_search.shared.errors import UserError


def validate_scores(candidate_ids, rows):
    if not isinstance(rows, list) or len(rows) != len(candidate_ids):
        raise UserError("Модель переранжирования вернула недопустимые оценки.")
    found = {}
    for row in rows:
        if (
            not isinstance(row, dict)
            or set(row) != {"candidate_id", "score", "truncated", "query_truncated"}
            or type(row["candidate_id"]) is not str
            or row["candidate_id"] not in candidate_ids
            or row["candidate_id"] in found
            or type(row["score"]) not in (float, int)
            or not math.isfinite(row["score"])
            or type(row["truncated"]) is not bool
            or type(row["query_truncated"]) is not bool
        ):
            raise UserError("Модель переранжирования вернула недопустимые оценки.")
        found[row["candidate_id"]] = row
    return [found[key] for key in candidate_ids]
