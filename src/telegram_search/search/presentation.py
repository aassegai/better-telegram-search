"""Bounded presentation options; independent of token budgets used for indexing."""

from telegram_search.shared.errors import UserError


def search_options(settings, limit=None, chunk_size=None):
    limit = settings.search_result_limit if limit is None else limit
    chunk_size = settings.display_chunk_size if chunk_size is None else chunk_size
    for value in (limit, chunk_size):
        if type(value) is not int or not 1 <= value <= 100:
            raise UserError("Количество результатов и размер фрагмента должны быть от 1 до 100.")
    return limit, chunk_size
