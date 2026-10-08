"""Indexed OCR substrings and bounded typo tolerance, independent of embeddings."""

from telegram_search.search.stopwords import WORD, keyword_terms
from telegram_search.shared.text import normalize_text


def gram_token(gram: str) -> str:
    # Encode tokens so quotes, operators, punctuation and scripts stay literal in MATCH.
    return "g" + gram.encode("utf-8").hex()


def ocr_grams(text: str) -> str:
    return " ".join(
        sorted(
            {
                gram_token(word[index : index + size])
                for word in WORD.findall(text)
                for size in (2, 3)
                for index in range(len(word) - size + 1)
            }
        )
    )


class VerificationBudget:
    def __init__(self, characters=1_000_000):
        self.remaining = characters
        self.exhausted = False

    def charge(self, characters):
        if characters > self.remaining:
            self.exhausted = True
            return False
        self.remaining -= characters
        return True


def substring_distance(pattern: str, text: str, tolerance: int, budget=None) -> int | None:
    """Myers bit-vector approximate substring search, with a free text prefix.

    O(len(text)) for patterns up to 64 Unicode characters. One adjacent swap is
    also accepted, a common OCR/typing error. Short terms require a literal match.
    """
    if pattern in text:
        return 0
    if not tolerance or not 4 <= len(pattern) <= 64:
        return None
    for index in range(len(pattern) - 1):
        swapped = pattern[:index] + pattern[index + 1] + pattern[index] + pattern[index + 2 :]
        if swapped in text:
            return 1
    # A character edit destroys at most two original bigrams. The adjacent-swap
    # fast path above is separate (a swap can destroy three).
    grams = [pattern[index : index + 2] for index in range(len(pattern) - 1)]
    if sum(gram in text for gram in grams) < len(grams) - 2 * tolerance:
        return None
    if budget is not None and not budget.charge(len(text)):
        return None
    masks = {}
    for index, char in enumerate(pattern):
        masks[char] = masks.get(char, 0) | (1 << index)
    mask, high = (1 << len(pattern)) - 1, 1 << (len(pattern) - 1)
    positive, negative, score = mask, 0, len(pattern)
    best = tolerance + 1
    for char in text:
        equal = masks.get(char, 0)
        diagonal = (((equal & positive) + positive) ^ positive) | equal | negative
        plus = negative | ~(diagonal | positive)
        minus = positive & diagonal
        if plus & high:
            score += 1
        elif minus & high:
            score -= 1
        # No injected 1: DP's first row is zero, allowing a substring anywhere.
        plus <<= 1
        minus <<= 1
        positive = (minus | ~(diagonal | plus)) & mask
        negative = diagonal & plus
        best = min(best, score)
        if best == 1:
            return 1
    return best if best <= tolerance else None


def ocr_word_search(conn, query, version, filters, maximum, *, exact=False):
    from telegram_search.search.lexical import fts_query

    match = fts_query(query, exact)  # Keeps the shared token bound and stop-word policy.
    if match is None:
        return [], {}, []
    where, params = filters.sql("m")
    eligible = (
        "o.version=? AND o.state='ready' AND EXISTS (SELECT 1 FROM media_refs r "
        "JOIN indexable_messages m ON m.chat_id=r.chat_id AND m.message_id=r.message_id "
        "WHERE r.sha256=o.sha256 AND r.kind='photo' AND r.status='ready' "
        f"AND {where})"
    )
    extra = " AND instr(o.text_normalized,?)>0" if exact else ""
    rows = conn.execute(
        "SELECT o.rowid FROM ocr_fts JOIN ocr_cache o ON o.rowid=ocr_fts.rowid "
        f"WHERE ocr_fts MATCH ? AND {eligible}{extra} ORDER BY bm25(ocr_fts),o.rowid LIMIT ?",
        (match, version, *params, *([normalize_text(query.strip())] if exact else []), maximum),
    )
    result = [str(row[0]) for row in rows]
    evidence = {key: {"kind": "exact", "edits": 0} for key in result}
    if exact or len(result) >= maximum:
        return result, evidence, []
    terms = list(dict.fromkeys(keyword_terms(normalize_text(query))))
    if not terms:
        return result, evidence, []
    # Literal substring candidates require all bigrams. Verification removes false
    # positives caused by grams appearing at different positions in a document.
    groups = []
    for term in terms:
        grams = {gram_token(term[index : index + 2]) for index in range(len(term) - 1)}
        if grams:
            groups.append("(" + " AND ".join(sorted(grams)) + ")")
    if not groups:
        return result, evidence, []
    candidates = {}
    for row in conn.execute(
        "SELECT o.rowid,o.text_normalized,bm25(ocr_gram_fts) FROM ocr_gram_fts g "
        "JOIN ocr_cache o ON o.rowid=g.rowid "
        f"WHERE ocr_gram_fts MATCH ? AND {eligible} ORDER BY bm25(ocr_gram_fts),o.rowid LIMIT ?",
        (" AND ".join(groups), version, *params, maximum * 3),
    ):
        candidates[str(row[0])] = (row[1], row[2])
    # Broad fuzzy recall is limited to compact queries; a long query still gets
    # exact token and literal substring search, without a corpus-wide scan.
    if len(terms) <= 12 and sum(map(len, terms)) <= 160:
        grams = set()
        for term in terms:
            if 4 <= len(term) <= 64:
                grams.update(gram_token(term[index : index + 2]) for index in range(len(term) - 1))
                # Four-letter adjacent swaps can share no bigram with the source.
                if len(term) == 4:
                    for index in range(3):
                        swapped = term[:index] + term[index + 1] + term[index] + term[index + 2 :]
                        grams.update(gram_token(swapped[i : i + 2]) for i in range(3))
        if grams:
            for row in conn.execute(
                "SELECT o.rowid,o.text_normalized,bm25(ocr_gram_fts) FROM ocr_gram_fts g "
                "JOIN ocr_cache o ON o.rowid=g.rowid "
                f"WHERE ocr_gram_fts MATCH ? AND {eligible} "
                "ORDER BY bm25(ocr_gram_fts),o.rowid LIMIT ?",
                (" OR ".join(sorted(grams)), version, *params, maximum * 3),
            ):
                candidates.setdefault(str(row[0]), (row[1], row[2]))
    ranked = []
    budget = VerificationBudget()
    fuzzy_allowed = len(terms) <= 12 and sum(map(len, terms)) <= 160
    for key, (text, bm25) in candidates.items():
        if key in evidence:
            continue
        distances = []
        for term in terms:
            value = substring_distance(
                term,
                text,
                (2 if len(term) >= 8 else 1) if fuzzy_allowed else 0,
                budget,
            )
            if value is None:
                break
            distances.append(value)
        if len(distances) != len(terms):
            continue
        edits = sum(distances)
        ranked.append((edits, bm25, key))
    for edits, _bm25, key in sorted(ranked, key=lambda item: (item[0], item[1], int(item[2]))):
        if len(result) >= maximum:
            break
        result.append(key)
        evidence[key] = {"kind": "fuzzy" if edits else "substring", "edits": edits}
    return (
        result,
        evidence,
        (
            [
                "Проверена только часть неточных OCR-совпадений. "
                "Уточните запрос, чтобы расширить точную выдачу."
            ]
            if budget.exhausted
            else []
        ),
    )
