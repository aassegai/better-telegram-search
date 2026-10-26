# Search filters and proposed required words

[Русская версия](query-filters.ru.md)

The tables below describe the current filters and options for a future product
decision. Required/excluded word controls and a modifier language are **not
implemented or approved**. Search does not interpret `+word`, `-word`, `from:` or
`in:` as operators.

## Existing controls

| Need | Current UI and job API (`SearchSpec`, version 1) | Gap or boundary |
| --- | --- | --- |
| Chat | Sidebar selection; `scope.mode: all` or `selected`, `scope.chat_ids` | Selected with no IDs means no chats. Names are labels; IDs identify chats. |
| Author | Author selector; `author_ids` | The UI selects one author; the API accepts several. Forward origin is a separate field. |
| Period | From/to date controls; `date_from`, `date_to` | UTC. The start is inclusive; a date-only end includes that entire day. |
| Exact phrase | Exact phrase checkbox; `exact_phrase` | Normalized literal phrase in a message or OCR text; visual descriptions do not prove a literal phrase. |
| Forward origin | Searchable checkboxes; `forwarded_from` | Source names are archive metadata, not necessarily Telegram account IDs. Several selected origins are alternatives. |
| Content | Content selector; `content_type` | Text/photo/service are supported by the API; audio types are part of the separate audio implementation. |
| Hide remotely deleted | Checkbox; `exclude_deleted` | Matching evidence and displayed context must respect it. |
| Required/excluded words | No dedicated control | Needs an explicit choice of interface and evidence scope below. |

Categories intersect. Values within a selected list are alternatives. A text
chunk must contain one owned message satisfying the combined author/date/type/
forward filters. Borrowed context cannot witness those filters. Image/OCR
filters apply to the message owning the attachment.

An empty query explicitly browses matching messages after a button/Enter action.
A nonempty query containing only stop words is still a search; it never silently
browses the archive. Changing filters does not execute a query.

## Three interface options for a later decision

| Option | Russian / English example | Trade-off |
| --- | --- | --- |
| Separate Required / Exclude fields | Query `поездка`, required `Казань`, exclude `самолёт`; query `trip`, required `York`, exclude `flight` | Visible, accessible conditions without a special language; takes more form space. |
| Modifiers with visible chips | `поездка +Казань -самолёт`; `trip +York -flight` | Fast keyboard entry; needs explicit quoting, escaping, ambiguous-name handling and validation. These strings currently have no modifier semantics. |
| Mark words/phrases after typing | Enter `поездка Казань самолёт`, mark `Казань` required and `самолёт` excluded | Avoids special characters; edits, repeated words and multiword phrases need clear identities and role indicators. |

All three options should compile to the same typed request if one is approved.
Their syntax must not change ranking semantics or bypass filtering before top-K.
No option is selected by this document.

## Expected examples to choose between

Suppose the evidence units are:

1. `Поездка в Казань поездом` / `A trip to York by train`.
2. `Поездка в Казань: самолёт` / `A trip to York by flight`.
3. `Поездка в Самару` / `A trip to Bath`.
4. `Казань` / `York`, with `самолёт` / `flight` in a neighbouring message.

For the proposed query `поездка` / `trip`, required `Казань` / `York`, excluded
`самолёт` / `flight`:

| Mode | Proposed behavior, subject to the evidence-scope decision |
| --- | --- |
| Words | Unit 1 qualifies if all ordinary significant query words match; 2 is excluded; 3 lacks the required word. |
| Meaning | Required/excluded words remain hard literal conditions before vector retrieval. Unit 1 can rank by meaning; 2 and 3 cannot qualify just because they are semantically close. |
| Hybrid | Apply the same hard conditions to both lexical and dense candidates, then fuse their ranks. Do not relax conditions in either branch. |

Unit 4 exposes an unresolved choice: with per-message evidence its neighbour's
word does not exclude it; with per-chunk evidence it may. Whether it ranks for
`trip` is then a separate retrieval question. Increasing the displayed neighbour
count must never change eligibility.

The product decision must also specify:

- Whether a condition is checked in the anchor message or owned chunk text.
- Whether OCR/transcripts form their own evidence unit, or share it with the
  owning message's caption. Future transcripts must not borrow another file's text.
- Whether an image proves text conditions through its caption, OCR, both, or
  neither. Image similarity alone does not prove the presence of a word.
- Whether terms mean complete tokens or literal substrings, and how phrases work.
- Whether excluded words can match borrowed reply context; recommended discussion
  point: keep displayed neighbours outside the evidence unit.

## Stop words and future parser requirements

The current ordinary lexical search requires **all** remaining significant
terms. No automatic OR relaxation is introduced. Stop words are removed only
from ordinary lexical queries, including the lexical OCR branch. They remain in
embedding input and exact phrases. Explicit required/excluded terms, if added,
must bypass stop-word removal.

Examples of existing behavior:

| Input | Expected behavior |
| --- | --- |
| `это всё про жёлтый велосипед` | Ordinary lexical query requires `желтый` and `велосипед`. |
| `where is the red bicycle` | Requires `red` and `bicycle`. |
| `не работает wi-fi 42` | Keeps the negation, technical term components and number. |
| `can't O'Reilly v2` | Keeps negative contraction/name components and the short technical term. |
| `the and is` / `и в это` | Explain that no searchable keywords remain; suggest semantic search or an exact phrase. |
| Exact phrase `the and is` | Search that phrase without removing stop words. |
| Empty text | Browse by the submitted filters without inference. |

Ordinary lexical normalization is Unicode NFKC, case folding and `ё → е`.
A future parser must handle Unicode, apostrophes, hyphens, URLs, quoted operator
text, escaping, unknown operators and invalid dates. It must not remove a
`from:` sequence from a URL or quotation merely because it resembles a modifier.
UI conditions and parsed conditions must not silently overwrite each other;
combination within a category needs an explicit rule, and applied conditions
must remain visible. Chat/author names require stable-ID resolution and an
ambiguity error or selector.
