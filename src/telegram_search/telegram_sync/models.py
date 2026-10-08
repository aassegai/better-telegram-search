"""Source contracts and explicit Desktop/MTProto peer normalization."""

from dataclasses import dataclass, field

from telegram_search.shared.errors import UserError


@dataclass(frozen=True)
class Peer:
    kind: str
    id: int
    access_hash: int | None = field(default=None, repr=False)
    name: str = ""

    @property
    def key(self):
        return f"{self.kind}:{self.id}"

    @classmethod
    def from_marked(cls, value: int):
        if value < -1_000_000_000_000:
            return cls("channel", -value - 1_000_000_000_000)
        return cls("chat" if value < 0 else "user", abs(value))

    @classmethod
    def from_binding(cls, binding):
        value = binding["access_hash"]
        return cls(binding["peer_type"], binding["peer_id"], int(value) if value else None)


def desktop_peer(external_id, kind: str) -> Peer | None:
    """Desktop uses raw IDs plus type; signed Telethon IDs encode the type."""
    if external_id is None:
        return None
    try:
        value = int(external_id)
    except (TypeError, ValueError):
        return None
    if value < 0:
        return Peer.from_marked(value)
    types = {
        "personal_chat": "user",
        "bot_chat": "user",
        "private_group": "chat",
        "private_supergroup": "channel",
        "public_supergroup": "channel",
        "private_channel": "channel",
        "public_channel": "channel",
    }
    return Peer(types[kind], value) if kind in types and value > 0 else None


@dataclass(frozen=True)
class NormalizedMessage:
    peer: Peer
    data: dict = field(repr=False)
    media_identity: str | None = None
    downloadable: bool = False
    expected_size: int | None = None

    def __post_init__(self):
        if type(self.data.get("id")) is not int or self.data["id"] <= 0:
            raise UserError("У сообщения отсутствует числовой ID.")


class SourceFailure(Exception):
    """Only stable public codes cross the API boundary; never raw SDK errors."""

    def __init__(self, code: str, retry_seconds: int = 0):
        self.code = code
        self.retry_seconds = max(0, retry_seconds)
        super().__init__(code)


ERRORS = {
    "not_configured": "Разработчик ещё не настроил подключение Telegram в этой сборке.",
    "runtime_missing": "Для подключения Telegram установите дополнительный модуль telegram.",
    "auth_required": "Нужно войти в Telegram заново.",
    "invalid_code": "Код Telegram неверен или истёк. Повторите вход.",
    "invalid_password": "Неверный пароль двухэтапной проверки.",
    "phone_invalid": "Проверьте номер телефона с кодом страны.",
    "waiting_rate_limit": "Telegram ограничил частоту запросов. Дождитесь указанного времени.",
    "network": "Нет соединения с Telegram. Подключение будет повторено.",
    "access_denied": "Диалог недоступен этому аккаунту.",
    "remote_unavailable": "Сообщение или вложение больше недоступно в Telegram.",
    "storage": "Не удалось записать данные. Проверьте свободное место и права папки.",
    "media_limit": "Изображение превышает лимит размера.",
    "media_invalid": "Telegram вернул неподдерживаемое или повреждённое изображение.",
    "disk_full": "Недостаточно свободного места для загрузки фотографий.",
    "session_busy": "Сессия Telegram уже используется другим процессом.",
    "account_mismatch": "Войдите в аккаунт, с которым связаны эти диалоги.",
    "unexpected": "Синхронизация прервана. Повторите попытку.",
}
