---
name: agent-comms-voices
description: Refresh or audit ElevenLabs voice assignments in agent-comms/config.toml. Use when choosing new identity voices, replacing unapproved voices, repeating a voice refresh (including "do it again"), avoiding rejected or duplicate voice IDs, or checking for premium voice surcharges. Preserve approved assignments by default, leave replacements unapproved, and verify current pricing and model compatibility.
---

# Agent-comms voice selection and pricing

Maintain distinct voices without reusing rejected IDs,
silently approving replacements, or introducing voice-specific surcharges.

## Establish scope from the current file

1. Locate `agent-comms/config.toml` in the working repository. From this skill's
   directory, it is `../../../config.toml`. Do not accidentally edit another
   `config.toml`, and do not assume the working directory is the repository root.
2. Read and parse the on-disk TOML afresh on every invocation. The user may have
   approved or rejected voices since the preceding refresh. Treat attachments and
   prior conversation lists as context, not as the current source of truth.
3. Snapshot the original text and parsed data outside the repository for comparison.
   Inspect existing changes; do not overwrite unrelated work.
4. Determine the operation:
   - **Refresh:** replace only identities whose `voice_approved` is not true.
     A follow-up "again" repeats this against the latest file, not the previous
     batch. If none remain, report that and make no changes.
   - **Pricing audit:** inspect every assigned voice, including approved ones.
     If asked to ensure no extra-charge voices are used, replace confirmed
     surcharge assignments, even approved ones, and set those replacements false.
     If the request is read-only, report findings without editing.
   - A request to exclude the entire `professional` category is different from
     excluding surcharges. Follow the explicit restriction; clarify if ambiguous.

## Preserve these invariants

- Keep opaque identity keys, display names, speech settings, and
  unrelated configuration intact. Voice catalog names need not match identity names.
- Preserve approved voice IDs in normal refreshes. Set every new assignment's
  `voice_approved = false`; never approve a voice on the user's behalf.
- Exclude every ID in `[rejected_voices].ids`, across all identities. Do not remove
  IDs from that list or add IDs merely because you replaced them. Rejection is a
  separate user decision, not a synonym for "previously assigned" or "costly".
- Ensure assigned IDs are unique across the **entire** configuration, including
  preserved approved identities and the rest of the replacement batch.
- Choose genuinely fresh candidates: exclude the complete pre-edit assignment set
  rather than rotating old IDs between identities. Also avoid previously tried IDs
  available in the conversation or local review history when possible.
- Update adjacent `# ElevenLabs: ...` catalog-label comments to the selected voice's
  actual name, without renaming identities.

## Query ElevenLabs safely

Use authenticated HTTPS requests with `ELEVENLABS_API_KEY` from the environment.
Never print the key, persist it in configuration, place it in shell arguments, or
include it in artifacts. If unavailable, stop and report the blocker; do not guess
voice IDs or claim pricing verification from memory.

Use current API documentation if endpoint parameters or field semantics differ:

- Account catalog: `GET https://api.elevenlabs.io/v2/voices?page_size=100`.
  Follow `has_more` and `next_page_token` through **all** pages.
- Individual metadata: `GET https://api.elevenlabs.io/v1/voices/{voice_id}`.
- Library discovery: `GET https://api.elevenlabs.io/v1/shared-voices`, with supported
  search, language, accent, gender, and pagination parameters as appropriate.
- Documentation: <https://elevenlabs.io/docs/api-reference/voices/search> and
  <https://elevenlabs.io/docs/api-reference/voices/get>.

Filter results locally and display compact fields rather than dumping the entire
catalog. Select voices using catalog descriptions, language,
delivery, age, accent, and texture. Do not claim to have auditioned a voice from
metadata alone. Do not generate TTS, run `voice-review`, or incur audition costs
unless the user requests it. Normal required agent announcements are not auditions.

## Enforce standard pricing and model support

For every assigned library voice, examine current `sharing` metadata (or equivalent
documented library fields):

- Require a verified standard `rate` of `1.0` and `fiat_rate` absent/null or zero.
  A rate above one or a positive fiat rate disqualifies the candidate. A missing
  or ambiguous multiplier is unknown, not proof of standard pricing: fetch more
  metadata or choose a verified alternative.
- `category = "professional"` identifies the voice type, not premium pricing.
  Standard-rate professional voices are acceptable unless the user excludes that
  category explicitly. `free_users_allowed = false` means a plan restriction, not
  necessarily a voice-specific surcharge. Do not conflate these fields.
- For account premade/generated voices without sharing metadata, verify their
  category and current availability; do not invent a sharing multiplier.
- Require the configured `[speech].model` in `high_quality_base_model_ids`, or
  equivalent explicit API evidence of support. Never substitute a different model
  silently. Exclude disabled, unavailable, or incompatible candidates.

Audit **all** final assignments, not just the new ones. If a preserved assignment
violates pricing, uniqueness, or rejection constraints outside the requested edit
scope, disclose the conflict rather than silently changing an approved voice or
claiming the whole configuration passes. Ask for permission if needed.

Pricing checks are point-in-time checks. State that normal plan/model usage charges
still apply; do not promise free speech generation or permanently fixed pricing.

## Add library voices only when needed and authorized

Prefer suitable, unused, verified voices already in the account. If more are needed
and the user has authorized collection additions for this workflow, use:

```text
POST /v1/voices/add/{public_owner_id}/{voice_id}
Content-Type: application/json
{"new_name": "<actual catalog voice name>"}
```

Otherwise ask before adding voices. Read API responses and use the returned
`voice_id`, which may differ from the source ID. Recheck that returned ID for
rejection, uniqueness, pricing, availability, and model support in account metadata
**before** assigning it. Do not silently fall back to an incompatible voice after
an API error or collection limit. Do not delete collection voices without consent.
Disclose successful additions left unused if later validation rejects them.

## Apply a narrow edit and validate

Before writing, reread the file and compare it with the snapshot. If it changed
during research, recalculate targets, exclusions, and approvals from the new state.
Use targeted patches so comments, formatting, and user edits survive.

After editing, parse TOML and programmatically check:

1. Every intended target has a different ID and `voice_approved = false`.
2. Non-target identities and target display names are unchanged.
3. All assigned IDs are unique and none intersect `rejected_voices.ids`.
4. Every replacement exists in the account and supports the configured model.
5. All assigned voices pass the current no-surcharge check, or explicitly report
   unresolved exceptions without claiming success.
6. The rejection list and unrelated settings are unchanged.

Review the diff against the pre-operation snapshot rather than attributing existing
repository changes to this operation. If validation fails, fix only your changes
or report the blocker; never discard user work. Do not commit unless requested.

## Completion

Briefly report the file changed, replacement count, collection additions (including
unused additions), uniqueness/rejection/pricing verification, and that replacements
remain unapproved. For an audit, name any surcharge assignment replaced and whether
its prior approval was reset. Mention standard usage charges and any incomplete
verification. Follow the active harness's communication/display rules; do not hardcode
an agent identity, voice ID, or messaging integration into this skill.

Regression scenarios are in `evals/evals.json`. Use fixtures or mocked API responses
for tests; do not run these prompts against the live collection as an automatic test.
