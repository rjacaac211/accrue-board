# ADR 0003: Extraction confidence from verifiable checks; recorded model calls

- **Status:** accepted
- **Date:** 2026-09-24

## Context
Routing needs an extraction confidence per document. A model's own estimate of its confidence
is poorly calibrated, and the Messages API exposes no token probabilities. We also want test
and evaluation runs that are reproducible, cost nothing to repeat, and work in CI without an
API key.

## Decision

### Confidence comes from evidence, per field
| Evidence | Confidence |
|---|---|
| PDF field found in the document's text layer | 1.0 |
| PDF field not found in the text layer | 0.5 |
| ... but the second reader agrees on it | 0.7 |
| Second reader disagrees | 0.3 |
| Image field (no text layer) and both readers agree | 0.9 |
| Value that could not be parsed | 0.0 |

Grounding normalizes print formats before comparing: currency symbols and separators,
several date styles, percentages, and punctuation in codes. A test checks that grounding never
flags a correct value on any generated layout. The document's confidence is its weakest field.

### The second pass runs only when there is doubt
It runs for images, and for a PDF with an ungrounded field, an unparsable value, or a validation
issue. It reads **the same file** with an independent-reader prompt, and its model is configured
separately (`MODEL_VERIFY`).
- **Why not a re-rendered page image:** PDF rasterizers differ between operating systems, so the
  input bytes, and therefore the replay key, would change across machines.
- **When the readers agree but validation still fails:** the document itself does not add up.
  Routing sends it to a person through the validation hard rule, not through low confidence.

### Every model call goes through one request type
Each `LLMRequest` has a canonical form (file bytes are replaced by their SHA-256) and a hash
key. `RecordingLLM` stores responses by that key and runs in one of four modes:

| Mode | Behavior |
|---|---|
| `live` | Always calls the model; never reads or writes recordings |
| `record` | Always calls the model and overwrites the recording |
| `replay` | Serves recordings only, needs no API key, and fails on a miss |
| `auto` | Replays if a recording exists, otherwise calls the model and records |

Prompt versions are part of the key, so editing a prompt forces a fresh recording.

### Structured outputs return amounts as strings
Amounts come back as decimal strings, never JSON numbers, so money never passes through a
binary float.

### Models per step (configurable)
- Classification: Haiku 4.5.
- Extraction, verification and coding: Sonnet 5.

The evaluation reports cost and accuracy per step, so these choices can be revisited with data.

## Consequences
- Unit tests use a fake model that answers from ground truth, optionally tampered with, to show
  that invented values are caught.
- Generated documents are byte-identical across operating systems (see
  `docs/synthetic-data.md`), so recordings made on one machine replay on another.
