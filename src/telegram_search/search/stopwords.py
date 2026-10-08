"""Conservative RU/EN function words, applied to keyword queries only."""

import re

# Keep negations (не/ни/нет/без, no/not/never/without/neither/nor), numbers,
# names and short technical terms. Do not infer stop words from private chats.
# The caller normalizes NFKC, case and ё→е before consulting this vocabulary.
STOP_WORDS = frozenset(
    """
    а б будем будет будут будь будьте бы был была были было быть
    в вам вами вас ваш ваша ваше ваши во вот все всего вы
    где да для до его ее ей ему если есть же за зачем здесь
    и из или им ими их к как ко когда кто ли либо
    мне мной мы на над надо нам нами нас наш наша наше наши
    но ну о об обо от перед по под при про
    с со так также там те тем то того той только том ту ты
    у уж уже чем чего что чтобы эта эти это этот я
    a about above after again against all also am an and any are as at
    be because been before being below between both but by
    can could did do does doing down during each either else
    for from had has have having he her here hers herself him himself his how
    i if in into is it its itself just me more most my myself
    of on once only or other our ours ourselves out over own
    same she should so some such than that the their theirs them themselves
    then there these they this those through to too under until up us very
    was we were what when where which while who whom whose why will with would
    you your yours yourself yourselves
    i'm you're he's she's it's we're they're that's there's here's
    what's who's where's when's why's how's let's
    i've you've we've they've i'll you'll he'll she'll it'll we'll they'll
    i'd you'd he'd she'd we'd they'd
    """.split()
)
TOKEN = re.compile(r"[^\W_]+(?:['’][^\W_]+)*", re.UNICODE)
WORD = re.compile(r"[^\W_]+", re.UNICODE)


def keyword_terms(normalized: str) -> list[str]:
    words = []
    for token in TOKEN.findall(normalized):
        if token.replace("’", "'") not in STOP_WORDS:
            # Keep the existing FTS tokenization for names/negative contractions
            # such as O'Reilly and can't. Recognized stop contractions disappear
            # as a whole, without treating standalone s/t/d as stop words.
            words.extend(WORD.findall(token))
    return words
