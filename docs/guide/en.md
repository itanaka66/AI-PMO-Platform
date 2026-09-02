# Getting started with AI-PMO

> Source: the Japanese and English versions are the originals. Other languages
> are translations of these.

---

## What is this?

A tool that hands project management (PMO) work to an AI.

For example, it can:

- turn a Teams meeting recording into **minutes, automatically**
- pull **who does what by when** out of those minutes and file them as tasks
- **chase people automatically** when a task passes its due date

You pick a "template" — a plan for the work — and it runs.
No programming needed.

---

## Who is it for?

- **Students** — learn the shape of project management while using it
- **Small businesses** — get the working patterns without a dedicated PMO
- **Large organisations** — bring scattered team practices onto shared templates

**All of it is free** — not a reduced edition, not a trial. The
templates and prompts come on the same terms. (The AI service's own
usage fees are paid to whichever provider you choose.)

---

## What you need

| | Requirements | Cost |
|---|---|---|
| **Simple setup** | A computer and an API key from an AI service | AI usage (metered, small) |
| **In-house setup** | Docker, 16GB RAM or more, ideally a GPU | Free (electricity only) |

> **Which one?**
> To try it, use the **simple setup**.
> If meeting content must not leave your organisation, use the **in-house setup**.

---

## Three steps to start

### 1. Install

Follow [INSTALL.md](../../INSTALL.md).

- **Windows** — double-click `AI-PMO-Setup.exe`
- **Mac / Linux** — run `./scripts/install.sh` in a terminal
- **Docker** — run `./scripts/install-docker.sh`

### 2. Configure

A setup screen opens after installation. Answer the questions; pressing Enter
accepts the default whenever you are unsure.

```
1) Where should the AI run?   → 1 (cloud)
2) Choose an AI provider      → 1 (OpenAI)
3) Enter your API key         → paste it
4) Organisation name          → your company, lowercase
5) Enable the database layer? → N
```

**There are four providers to choose from.** If you are unsure, pick OpenAI:
it has embeddings too, so one setting covers everything.

| Provider | Character |
|---|---|
| OpenAI | The default choice |
| Gemini | Handles long transcripts cheaply |
| Groq | Fast, but needs two keys |
| OpenRouter | One key, many models to compare |

**Getting an API key**
Create an account with your chosen provider and issue a key. It is a long
string. Do not show it to anyone.

- OpenAI — https://platform.openai.com/api-keys
- Gemini — https://aistudio.google.com

See [PROVIDERS.md](../PROVIDERS.md) for detail.

### 3. Try it

```bash
aipmo validate templates/examples/meeting_minutes.yaml
```

This means it worked:

```
OK  templates/examples/meeting_minutes.yaml  [software] ステップ 5 件
```

The `ステップ 5 件` part means “5 steps”; the tool's output is in Japanese.

---

## What a template is

A plan describing what happens, in what order. One template corresponds to one
piece of PMO work.

```yaml
name: meeting_minutes          # its name
trigger: "event:teams:meeting_ended"   # what started it (does not fire by itself)

steps:                         # what it does
  - id: fetch_transcript       # 1. fetch the meeting record
    adapter: teams

  - id: minutes                # 2. have the AI write the minutes
    llm: { profile: default }

  - id: register_jira          # 3. file the tasks
    adapter: jira
```

`event:` records what started the run. It does not fire when a meeting ends.
Pass the meeting details with `aipmo run` or from the phone screen.
For a clock, use `trigger: "schedule:..."` and `aipmo schedule`.

When the work changes, you swap the template.
**How the AI is used changes with the template too.**

---

## Common commands

```bash
aipmo setup       # run setup again
aipmo validate <file>   # check a template for mistakes
aipmo run <file>        # run it
aipmo adapters    # list the connected tools
aipmo doctor      # check that connections work
aipmo serve       # open the interface for your phone
aipmo schedule    # start running things on a schedule
```

---

## Worth knowing about safety

**Your API key is stored in `.env`,** not in `config.yaml`. Config files get
shared with colleagues and committed to Git, so the key is kept separate.

**Your data does not leave.** Each organisation's data is stored separately,
and reaching another organisation's data is not technically possible.

**Nothing is published automatically.** A template cannot write to the public
store. It can submit a candidate; that is as far as it goes.

---

## When something goes wrong

**Typing `aipmo` says "command not found"**
On Mac or Linux, run this and then reopen your terminal:
```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc
```

**Double-clicking the `.ps1` file on Windows does nothing**
Double-click `install.bat` instead.

**You forgot to enter your API key**
Run `aipmo setup` again.

**Antivirus software blocks the installer**
Unsigned files can trigger a warning. If that concerns you, use the Mac/Linux
or Docker version instead.

More detail is in [INSTALL.md](../../INSTALL.md).

---

## What to read next

- [INSTALL.md](../../INSTALL.md) — installation in detail
- [MOBILE.md](../MOBILE.md) — using it from a phone
- [PROVIDERS.md](../PROVIDERS.md) — choosing an AI provider
- [AGENTS.md](../AGENTS.md) — letting the AI decide for itself
- [TEAMS.md](../TEAMS.md) — connecting Teams meeting records
- [JIRA-SLACK.md](../JIRA-SLACK.md) — filing issues in Jira, notifying in Slack
- [SCHEDULER.md](../SCHEDULER.md) — running things automatically on a schedule
- [AGILE.md](../AGILE.md) — reporting on sprints
- [INDUSTRIES.md](../INDUSTRIES.md) — construction, marketing and other fields
- [LICENSE](../../LICENSE) — MIT (commercial use, modification and redistribution allowed)
- [README.md](../../README.md) — how it works, for developers
- `templates/examples/` — worked template examples
