"""Frozen exact semantics, separate from private counts and audit snapshots."""

import hashlib
import json
from dataclasses import asdict, dataclass, field

from telegram_search.search.chunking.classification import whole_word
from telegram_search.search.chunking.noise.normalization import structural_flags
from telegram_search.search.chunking.policy import DEFAULT_POLICY, ChunkPolicy
from telegram_search.shared.text import serialize

POLICY_VERSION = "telegram-episodes-noise-v3"
PROTECTION_VERSION = "whole-form-structural-context-v1"
MAX_POLICY_BYTES = 8 * 1024**2
MAX_FORMS = 50000


def exact_forms(values):
    if not isinstance(values, (list, tuple)) or len(values) > MAX_FORMS:
        raise ValueError("Invalid exact noise forms")
    for value in values:
        if not isinstance(value, str) or not 1 <= len(value) <= 64 or whole_word(value) != value:
            raise ValueError("Invalid exact noise form")
    return tuple(sorted(set(values)))


@dataclass(frozen=True)
class NoisePolicy:
    base: ChunkPolicy
    omit: tuple[str, ...] = ()
    keep: tuple[str, ...] = ()
    _omitted: frozenset[str] = field(init=False, repr=False, compare=False)
    _kept: frozenset[str] = field(init=False, repr=False, compare=False)
    _seeds: frozenset[str] = field(init=False, repr=False, compare=False)
    _serialized: str = field(init=False, repr=False, compare=False)
    _identity: str = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        if type(self.base) is not ChunkPolicy:
            raise ValueError("Noise policy requires a v2 episode policy")
        for key, default in asdict(DEFAULT_POLICY).items():
            if type(default) is int:
                value = getattr(self.base, key)
                if type(value) is not int or not 0 <= value <= default:
                    raise ValueError("Invalid base document budget")
        # Round-trip validates supported budgets without changing the v2 format.
        if ChunkPolicy.from_json(self.base.json) != self.base:
            raise ValueError("Invalid base document policy")
        if (
            self.base.normalization != "whole-unicode-word-nfkc-casefold-yo-v1"
            or self.base.support_count != 0
            or self.base.count_meaningful_captions is not True
        ):
            raise ValueError("Unsupported base document semantics")
        seeds = exact_forms(self.base.filler)
        keep, omit = exact_forms(self.keep), exact_forms(self.omit)
        if len(keep) + len(omit) > MAX_FORMS:
            raise ValueError("Exact noise policy form limit exceeded")
        # A redundant explicit seed omission and conflicting keep/omit cannot
        # change the effective hash. Manual keep always wins.
        omit = tuple(sorted(set(omit) - set(keep) - set(seeds)))
        object.__setattr__(self, "keep", keep)
        object.__setattr__(self, "omit", omit)
        object.__setattr__(self, "_kept", frozenset(keep))
        object.__setattr__(self, "_seeds", frozenset(seeds))
        object.__setattr__(self, "_omitted", frozenset((*seeds, *omit)) - self._kept)
        serialized = serialize(
            {
                "version": POLICY_VERSION,
                "base": json.loads(self.base.json),
                "omit": self.omit,
                "keep": self.keep,
                "protections": PROTECTION_VERSION,
            }
        )
        encoded = serialized.encode()
        if len(encoded) > MAX_POLICY_BYTES:
            raise ValueError("Noise policy size exceeded")
        object.__setattr__(self, "_serialized", serialized)
        object.__setattr__(self, "_identity", hashlib.sha256(encoded).hexdigest())

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "base"), name)

    @property
    def version(self):
        return POLICY_VERSION

    @property
    def filter_borrowed_context(self):
        return True

    @property
    def json(self):
        return self._serialized

    @property
    def identity(self):
        return self._identity

    def omits_form(self, form, message, parent=None):
        # Only exact normalized set membership is used at inference time.
        if form not in self._omitted:
            return False
        if message.has_attachment or message.has_photo or message.protected:
            return False
        if parent is not None and "?" in parent.text:
            return False
        if form not in self._seeds and (
            structural_flags(message.text.strip(), form) or any(char in form for char in "-'’")
        ):
            return False
        return True

    @classmethod
    def from_json(cls, value):
        if not isinstance(value, str) or len(value.encode()) > MAX_POLICY_BYTES:
            raise ValueError("Noise policy size exceeded")
        data = json.loads(value)
        if (
            not isinstance(data, dict)
            or set(data) != {"version", "base", "omit", "keep", "protections"}
            or data["version"] != POLICY_VERSION
            or data["protections"] != PROTECTION_VERSION
            or not isinstance(data["base"], dict)
            or data["base"].get("version") != "telegram-episodes-v2"
        ):
            raise ValueError("Unsupported noise policy")
        policy = cls(
            ChunkPolicy.from_json(serialize(data["base"])),
            exact_forms(data["omit"]),
            exact_forms(data["keep"]),
        )
        if policy.json != serialize(data):
            raise ValueError("Noise policy must use canonical exact semantics")
        return policy


def compile_policy(base, *, omit=(), keep=()):
    """Compile trusted accepted exact forms; caller owns scope/override checks.

    This function does not interpret scores or enable a calibrated decision gate.
    No job ID, timestamps, corpus counts or audit provenance enter its identity.
    """
    if isinstance(base, NoisePolicy):
        # Re-analysis starts from the underlying episode semantics and supplies
        # the complete effective sets, rather than growing a transitive policy.
        base = base.base
    return NoisePolicy(base, exact_forms(omit), exact_forms(keep))
