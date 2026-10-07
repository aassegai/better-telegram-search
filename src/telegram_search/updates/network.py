import json
import urllib.request
from urllib.parse import urlsplit

from telegram_search.shared.errors import UserError

REPOSITORY = "aassegai/better-telegram-search"
API = f"https://api.github.com/repos/{REPOSITORY}"
HOSTS = {
    "api.github.com",
    "github.com",
    "release-assets.githubusercontent.com",
    "objects.githubusercontent.com",
}


def validate_url(url):
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in HOSTS
        or parsed.port not in {None, 443}
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise UserError("Недопустимый адрес обновления.")
    return url


class Redirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        validate_url(newurl)
        return super().redirect_request(request, fp, code, msg, headers, newurl)


def open_url(url):
    validate_url(url)
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "BetterTelegramSearch-Updater",
            "Accept": "application/vnd.github+json"
            if url.startswith(API)
            else "application/octet-stream",
        },
    )
    return urllib.request.build_opener(Redirects()).open(request, timeout=30)


def read_json(url, limit=256_000):
    with open_url(url) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise UserError("Сведения об обновлении слишком велики.")
    try:
        value = json.loads(data)
    except (ValueError, UnicodeError) as exc:
        raise UserError("Не удалось прочитать сведения об обновлении.") from exc
    if not isinstance(value, dict):
        raise UserError("Некорректные сведения об обновлении.")
    return value
