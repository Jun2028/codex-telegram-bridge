# Specialists behind a persistent tele-agent

The Telegram agent coordinates a continuous conversation. A specialist handles
one assignment using selected context and a named, reviewable prompt. The
coordinator remains responsible for the user's intent, the quality of the
returned work, and the reply destination. Delegation is useful for substantial
tasks with a distinct working context; it is not required for every request.

## Included writer

`config/specialists/writer.json` selects GPT-6 Astra with **high** reasoning.
`writer.md` replaces the coding-oriented base prompt with an expository writer
prompt. `writer-developer.md` defines the assignment and delivery behavior.
It emphasizes argument continuity, thesis fidelity, concrete explanation,
accurate attribution, and revision without repetitive conclusions.

The prompt adapts the [official Astra guidance](https://developers.openai.com/api/docs/guides/latest-model).
Thesis fidelity and evidence rules are additional editorial choices. Prompt
configuration does not itself demonstrate writing quality; evaluate the actual
artifact on the task it was given.

## Make a handoff

Save a Markdown brief containing:

- The user's relevant instructions verbatim, including corrections.
- The deliverable, audience, and requested length or format.
- Approved decisions, separately from the coordinator's suggestions.
- Remaining questions and the limits of the supplied evidence.

Select only sources appropriate to the originating chat. A group assignment
must not receive private conversation material without the owner's permission.
Do not pass transcripts or raw logs when a reviewed excerpt would suffice.
The runner accepts UTF-8 text, with a combined 512,000-byte input limit. Retrieve
and review remote/HPC evidence in the coordinator before supplying excerpts.
The specialist cannot independently browse, run commands, or inspect files.

```bash
scripts/run_specialist.sh list
scripts/run_specialist.sh run writer \
  --brief /absolute/path/brief.md \
  --source /absolute/path/draft.md \
  --source /absolute/path/evidence.md
```

Use the repository's absolute script path when running outside its directory.
Set `TELEAGENT_INSTANCE` as for any other relay command. `relay_paths.sh` resolves
the instance's configuration and private scratch directory. Chat-only mode
rejects execution. `--prepare-only` saves the inputs and configuration without
loading authentication or invoking a model.

This foreground runner prints the job directory immediately and exits when the
assignment finishes. For long work, use the host's normal independent job
runner; never inject it into a busy agent pane. The coordinator must collect and
review the result before declaring its assignment complete.

## Inspect the result

Jobs live in `$TELEAGENT_SCRATCH/specialists/writer-<unique-id>/`:

- `brief.md`, source snapshots, and `inputs.json` preserve exact bytes and hashes.
- `role.json`, `base.md`, and `developer.md` preserve the selected role.
- `result.md` contains the requested text. `report.json` contains the handoff
  summary, sources used, and unresolved issues.
- `status.json` distinguishes prepared, running, complete, and failed states.
- Private execution logs and session records support diagnosis. Do not send
  entire job directories, prompts, or raw logs to Telegram.

Only accept a complete result after reviewing its contents. Successful process
exit alone is insufficient: the runner checks every retained actual
`turn_context` for the requested model and effort and validates the returned
report. Missing or mismatched model evidence rejects the result. Failures are
not automatically retried, resumed, or sent to another model, including Luna.
There is no background report delivery service; the calling coordinator reads
the artifacts and responds through its existing Telegram route.

## Isolation and authentication

Each run gets a fresh Codex home, workspace, and tool-disabled configuration.
The runner sends the selected text directly as input; it imports no conversation
history, repository instructions, skills, plugins, or provider configuration.
It disables project instruction discovery for this explicitly supplied brief.
The fresh home temporarily links only the owning bot's OpenAI `auth.json`, then
removes the link after execution. Credentials are never copied into the prompt.
Other account credential backends and API-key-only setups are not implemented.

This runner requires Python 3.11+ and currently supports OpenAI text specialists
with no tools. A
DeepSeek-backed coordinator may use it only if that bot's configured OpenAI
Codex home has usable file authentication. It does not transfer DeepSeek keys
or change the coordinator's model. Use a compatible Codex CLI; the implementation
targets the strict permission/configuration interface in 0.153.4 and fails if
the CLI rejects it.

## Add a role

Add a named JSON manifest and two prompt files under `config/specialists/`,
following `writer.json`. Pin the model and effort. Names contain lowercase
letters, digits, underscores, or hyphens and begin with a letter. Prompt paths
must resolve directly inside that directory. Additional roles use the same
tool-free text/report contract; tool-using researchers or coding workers need
their own reviewed execution design.

The supervisor appends `coordinator.md` to existing developer instructions on
each new full-access managed launch. This applies to main and additional bot
instances. It does not replace their personality or automatically restart an
existing agent. The script is also available for explicit use in an already
running full-access conversation.

Public exports include only the reviewed implementation and generic prompts.
Job data and local writer profiles remain private. Add new role files explicitly
to `config/public-export.txt` when they are intended for public release.
