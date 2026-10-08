# Text fragments

Open **Text fragment construction** in a conversation's indexing settings.
Select **Conversation with single-word noise filtering** and explicitly click
**Apply and rebuild text index**. This rebuilds only that conversation's text
fragments, preserving pauses, devices, images and recognized OCR. New semantic
results become available as daily segments finish; keyword search covers the archive.

The limits are **8 meaningful messages and 480 tokens**, including the model prefix,
author labels and special tokens. Retained short replies, standalone terms, numbers,
links and emoji use tokens without using message slots. Meaningful attachment captions
count as regular messages. A separate limit of 32 source messages bounds each window.

Approved single-word noise is omitted only from model input. Those words inside
sentences remain untouched. Captions, structured entities and answers to questions
are protected. Original messages, keyword search, exact phrases and the lexical
branch of combined search remain available. Captionless photos do not create a
text embedding from a lone photo marker. Message filtering never applies to OCR.

Short replies can borrow up to two parents from the same conversation, at most
30 days old, within a shared 128-token context budget. Without a reply link,
a preceding message within two minutes can be labeled as neighboring context.
Context may cross UTC midnight. Edits, deletion, late parent arrival and author
exclusions invalidate dependent daily segments. Borrowed text cannot satisfy the
main message's author or date filters.

Long messages split at paragraph, sentence and word boundaries with bounded token
overlap and original Unicode ranges. Source and model provenance remain separate.
Preview neighbors do not suppress independent search evidence.

**Original windows** retain previous behavior: every message uses a slot and
single-word noise is not filtered. Existing indices and checkpoints keep this policy
until you explicitly switch. Changing document rules creates a new generation;
changing execution devices or batch sizes does not. Untagged bot-command filtering
is deferred.

To analyze an explicitly chosen export locally:

```sh
uv run --no-sync python scripts/analyze_chunking.py /path/to/result.json
```

Streaming analysis does not open the app's database. Reports stay inside ignored
`workspace/analysis/chunking/`, or an explicitly selected directory under `workspace/`
or `plans/`. Frequency alone never updates the policy. Reports contain word forms
and are intended for local use.
