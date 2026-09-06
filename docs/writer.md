# A separate writing context

The writer is an ordinary Codex session with a different base prompt and a
short developer instruction. It uses GPT-6 Astra with high reasoning. The role
is developing prose from a supplied skeleton and evidence packet. The brief
supplies the thesis, section purposes, intended ending, sources, audience, and
requested deliverable. The coordinator handles evidence gathering, ideation,
planning, and the overall review cycle. The writer can inspect sources and make
targeted checks while drafting, but does not take over that entire workflow.
A local revision needs the passage and a specific editing instruction.

Copy `config/writer.config.toml.example` to `$CODEX_HOME/writer.config.toml`
(normally `~/.codex/writer.config.toml`). Set `model_instructions_file` to the
absolute path of `config/writer.md` in this checkout.

Start a fresh session:

```bash
codex --profile writer
```

For a one-off assignment supplied in a file:

```bash
codex exec --profile writer - < /absolute/path/brief.md
```

Run from the working directory appropriate to the assignment; use Codex's
usual `--cd` and `--skip-git-repo-check` options as needed. A fresh launch has
its own conversation. Do not resume or fork another conversation when a clean
context is wanted. Normal working-directory instructions still apply.

The profile changes only the model, reasoning effort, base prompt, and
developer instruction. Tools, permissions, network access, authentication, and
output handling follow the normal Codex configuration. There is no custom
runner, report protocol, separate authentication home, or tool restriction.

A tele-agent can launch this ordinary session when delegation is requested,
provide the relevant user instructions and source locations, and review its
output before replying. Preserve the user's wording where it matters; do not
silently turn the coordinator's suggestions into approved decisions.

The prose guidance adapts the [official Astra prompting guide](https://developers.openai.com/api/docs/guides/latest-model).
Thesis fidelity and evidence handling are additional editorial choices.
The [Codex configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference)
documents profiles and base-prompt replacement. Prompting does not guarantee
writing quality; judge the resulting text against the actual assignment.
