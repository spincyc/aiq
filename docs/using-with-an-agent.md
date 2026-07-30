# Using AIQ with a coding agent

This page is for the person who installed AIQ and will never type an `aiq`
command. Your coding agent runs them. What you do is talk to the agent, and
read what shows up in your transcript.

Everything else in this documentation describes the CLI to whoever invokes it.
This page describes the same system from the other side: what to say, what the
agent will do, and how to read the result.

## What AIQ gives you

Two things, both of which you would otherwise have to do by hand.

| Without AIQ | With AIQ |
|---|---|
| A request lives only in the conversation, and is lost when context is trimmed or the session ends | Every turn-starting prompt is recorded in a local journal before the model sees it |
| You remember what the agent still owes you, and chase it | The agent is stopped from ending its turn while runnable work remains |

The second one is the part worth internalizing. The installed hook includes a
completion gate, so an agent that says "done" while a task it accepted is still
open does not get to stop — the gate hands it back the list. **You do not have
to police the agent.** If it forgot something it recorded, the transcript will
show it being sent back.

AIQ never calls a model and never does the work. It records requests, derives
tasks, and hands them out one holder at a time.

Every AIQ message quoted on this page is emitted as a single line. They are
wrapped here to fit the page.

## Setup check

Two things must be true before any of this happens. Ask your agent:

> Check whether AIQ capture is working in this repo.

It should run `aiq doctor` and read you the `capture` line. Working looks like
`capture ok`. Not working usually looks like this:

```text
capture   warn   prompt capture is inactive: repo journal not initialized;
                 run aiq journal init --scope repo to opt in
```

Repository capture is deliberately opt-in. In a Git repository with no AIQ
journal, the installed hook exits silently, records nothing, and creates no
storage — AIQ does not start following you around every repository you happen
to open. To opt this repository in, say:

> Start tracking work in this repo with AIQ.

The agent runs `aiq journal init --scope repo`. From that point every prompt
you send in this repository is captured. To opt back out, ask it to run
`aiq journal destroy`.

The other precondition is the hook itself, installed once per machine. If
`doctor` reports the integration as missing or drifted, see the
[Claude Code](integrations/claude.md) or [Codex](integrations/codex.md)
integration page. Both hosts snapshot hook configuration when a session starts,
so restart the session after installing or repairing.

### One gap to know about

The host fires the capture event only for the prompt that *starts* a turn. A
message you send while the agent is already working never reaches the hook, so
nothing is captured automatically. The packaged agent guidance obligates the
agent to record those by hand with `aiq ingest --if-new`, which deduplicates,
so a mid-turn instruction is recorded once whether the agent ingests it or not.

If a mid-turn correction matters, it is fair to say so:

> Make sure that last message got into AIQ.

## Everyday phrases

Nothing here is an incantation. These are ordinary sentences; the agent maps
them onto commands. The point of the table is to show you what happens
underneath, so the transcript is legible.

| Say | The agent runs | Result |
|---|---|---|
| "Add that to the queue, don't do it yet." | `aiq enqueue TITLE` | One task recorded as ready. Nothing is executed. |
| "Queue those three, and the last one depends on the first." | `aiq enqueue TITLE --requires TASK-ID` | A dependency; the dependent stays out of the queue until its prerequisite is done. |
| "What's outstanding?" | `aiq status` | Counts by state, plus the ready and blocked tasks by name. |
| "Show me everything, including what's finished." | `aiq list --all` | Every task, terminal states included. |
| "What's next in the queue?" | `aiq queue peek` | Read-only. Changes nothing and takes no role. |
| "Take the next one and do it." | `aiq dequeue` then the work then `aiq task done` | See [Three ways to run the queue](#three-ways-to-run-the-queue). |
| "Why hasn't TASK-4 started?" | `aiq task explain TASK-4` | The eligibility answer, including what it waits on. |
| "What did we decide on TASK-4?" | `aiq task history TASK-4` | The recorded revisions and summaries. |
| "I never answered that question — here's the answer." | `aiq inbox claim MESSAGE` on the parked message | Resumes a message parked for your input. |
| "That's an AIQ bug, file it." | `aiq report --summary TEXT` | A defect report, if a report repository is configured. |

Filing work is never gated. Any number of sessions may `ingest`, `enqueue`,
and `report` at once, so "write this down for later" always succeeds, even
while another session is draining the queue.

### Filing without doing

The most useful phrase in the list is the first one. Agents default to acting;
AIQ gives you a place to put work that you do not want acted on yet.

> Don't implement this now. Put it in the queue with the other cleanup items.

The agent records the task and moves on. It stays ready until something claims
it, in this session or a later one — the journal is local and durable, so
tomorrow's session sees today's queue.

## Three ways to run the queue

When you ask the agent to *work* the queue, be explicit about how much. The
three modes differ in their stop condition, and an agent that does not know
which one you meant will guess.

Underneath all three is one rule: **many writers, one reader.** Any session may
add work; exactly one session at a time may consume it. The first consuming
command takes that reader role implicitly, so a single working session never
notices the rule exists. See [Concepts](concepts.md#many-writers-one-reader).

### 1. One task

> Take the top item off the AIQ queue, do it, and stop. Just the one.

```text
$ aiq dequeue
TASK-1	active	r1	0	clm_9d6c783e5d184f5b9a39456a44512da2	Write the release notes

$ aiq task done TASK-1 --summary "Drafted the notes"
TASK-1	done	r2

$ aiq reader release
status	released
replayed	False
```

The stop condition is the release. `aiq reader release` means "I am no longer
draining this queue", and it is the only way this session can stop with ready
work deliberately left behind:

```text
AIQ: not blocking: runnable work remains (2 ready tasks) but this session
released the reader role — aiq reader status
```

Without the release, the gate would have blocked and pushed the agent back to
the two remaining tasks. That is the difference between "do one" and "do them
all", and it is enforced, not merely requested.

### 2. A fixed batch

> Work through three AIQ tasks and then stop for review.

Identical to the first mode, looped a fixed number of times, with the same
single release at the end:

```text
$ aiq dequeue
TASK-1	active	r1	0	clm_039200289abc4dd69f814cdd7a44e851	Write the release notes
$ aiq task done TASK-1 --summary "Drafted the notes"
TASK-1	done	r2

$ aiq dequeue
TASK-2	active	r1	0	clm_012783c11236459ab2a985303ed35344	Update the changelog
$ aiq task done TASK-2 --summary "Changelog updated"
TASK-2	done	r2

$ aiq reader release
status	released
replayed	False

AIQ: not blocking: runnable work remains (1 ready task) but this session
released the reader role — aiq reader status
```

Settle each task as you finish it, not all at the end. A summary recorded at
completion is what `aiq task history` will show later.

### 3. Until the queue is empty

> Drain the AIQ queue — keep taking tasks until there's nothing runnable left.

Here the loop ends when the queue hands back nothing:

```text
$ aiq dequeue --json
{"items":[{...,"task":{...,"task_id":"TASK-1","title":"Write the release notes"}}],
 "reader_acquired":true,"v":1}
$ aiq task done TASK-1 --summary "Handled"
TASK-1	done	r2

  … TASK-2, TASK-3 …

$ aiq dequeue --json
{"items":[],"reader_acquired":false,"v":1}
```

`items == []` is the stop condition. No release is needed: with nothing
runnable, the gate exits 0 silently and the session ends on its own.

But `items == []` is ambiguous on its own — it means "nothing was handed out",
which covers both a finished queue and a queue where everything left is
blocked. `aiq status --json` separates them:

| `tasks.blocked` | Meaning |
|---|---|
| `0` | The queue is genuinely empty. Done. |
| `> 0` | Work remains that nothing can start. Not done — stalled. |

In the second case the `blocked[]` array names each stalled task, and
`blocked_by` names the prerequisites causing it:

```json
{"blocked": [{"blocked_by": [], "priority": 0, "task_id": "TASK-1",
              "title": "Get the vendor signoff"},
             {"blocked_by": ["TASK-1"], "priority": 0, "task_id": "TASK-2",
              "title": "Ship the integration"}],
 "tasks": {"active": 0, "blocked": 2, "done": 1, "queued": 0, "ready": 0}}
```

An empty `blocked_by` means the task was blocked outright for a stated reason;
a populated one names the failed prerequisites. A good agent reports the
difference back to you rather than announcing an empty queue — the same
information is in the human-readable `aiq status`:

```text
tasks     queued=0  ready=1  active=0  blocked=2  done=0  canceled=0  superseded=0
ready     [DEMO: TASK-3]	p0	Write the release notes
blocked   [DEMO: TASK-1]	p0	Get the vendor signoff
blocked   [DEMO: TASK-2]	p0	Ship the integration	blocked by TASK-1
```

### Choosing

| You want | Say | Stops on |
|---|---|---|
| A single unit of work, then review | "just the one" | the reader release |
| A bounded batch | "three tasks, then stop" | the reader release |
| Everything that can be done | "drain the queue" | `items == []` |

Any mode you did not name is a guess. "Work on AIQ tasks" is the ambiguous
phrasing; add the bound.

## Reading the transcript

AIQ speaks to you through a handful of fixed lines. The gate's lines all start
with `AIQ:`; a CLI refusal starts with `aiq:`.

### The completion gate blocked the agent

In Claude Code this appears in your transcript as **Stop hook feedback**. It
means the agent tried to end its turn with work outstanding, and was sent back:

```text
AIQ: runnable work remains: 1 ready task, 2 active claims: [DEMO: TASK-3]
"Tag the release" (open 1m) — settle finished work: aiq task done TASK-3
--summary TEXT — or: aiq status
```

The counts come first, then up to three named tasks, then the command that
settles them. The host feeds this back to the model and the turn continues, so
you will normally see the agent immediately act on it. **This is the system
working**, not an error. You do not need to do anything.

If you see the same block line repeatedly with no progress between them, that
is worth a look — usually the agent is finishing work but not recording it.
"Settle what you've finished in AIQ" is the fix.

### The gate declined to block

An exit-0 `not blocking:` notice means runnable work exists but this session is
legitimately allowed to stop. There are exactly two reasons, and the notice
says which:

```text
AIQ: not blocking: runnable work remains (2 ready tasks) but this session
released the reader role — aiq reader status
```

The agent finished a bounded run on purpose. This is what you should see at the
end of modes 1 and 2.

```text
AIQ: not blocking: runnable work remains (1 ready task) but reader
"host-4242" holds the reader lease — aiq reader status
```

A different live session is draining the queue. This session only filed work,
so holding it open would accomplish nothing. Expect this when you are running
two agents at once.

In every other case the gate blocks. Standing down requires proof — a lease
whose recorded holder is on this host, still alive, and not this session — so
an abandoned lease from a crashed session does not quietly switch enforcement
off.

### A message is waiting on you

A message the agent parked with `needs-input` is waiting for *you*, not for the
agent, so it never blocks stopping. It is still surfaced, because a session
should not end with your question unanswered. Appended to a block line:

```text
AIQ: runnable work remains: 1 ready task: [DEMO: TASK-4] "Rotate the signing
key" (open 0m); 1 parked message awaits user input — settle finished work:
aiq task done TASK-4 --summary TEXT — or: aiq status
```

Or on its own, when nothing else is runnable:

```text
AIQ: no runnable work; 1 parked message awaits user input — aiq inbox list
```

That one is your cue. Ask what the question was; answering it lets the agent
resume the message and apply it.

### `reader_held`

```text
aiq: reader lease is held by owner "other-session" reader "host-4242" until
2026-07-30T19:22:42Z; ingest and enqueue remain open
```

Exit code 4. Another live session holds the single reader role, so this one
cannot consume. It is not a failure to retry into — the second half of the line
is the important part: filing work still works. If you are deliberately running
two agents, let the second one queue work and let the first drain it.

### `[DEMO: TASK-3]`

The bracketed prefix on every task reference is the journal's project label
plus the task ID. The label comes from `aiq journal init --label TEXT` and
exists so that a task ID from one project is not mistaken for another's in a
transcript that touches several. Bare `TASK-3` in a command is the same task —
the settle command in the gate line deliberately drops the prefix so it can be
copied straight into a shell.

## Looking at the state yourself

You can read everything without an agent. All of these are read-only.

| Command | Answers |
|---|---|
| `aiq status` | What is outstanding right now, by state, with ready and blocked tasks named |
| `aiq list` | Every non-terminal task; add `--all` for finished ones |
| `aiq queue peek` | What would be handed out next, without handing it out |
| `aiq reader status` | Who holds the reader role, and the lease state — `held`, `released`, `expired`, `stale` (holder provably gone), or `absent` |
| `aiq inbox list` | Captured messages not yet applied, including parked ones |
| `aiq doctor` | Whether capture, configuration, and the integrations are healthy |
| `aiq journal path` | Which journal this directory resolves to |

Two of these are worth a habit. `aiq status` after a session tells you what the
agent left behind. `aiq reader status` explains any confusing `reader_held`.

Journal content is local, private, and never committed. See
[Privacy](privacy.md) for exactly what is captured and retained, and
[Recovery](recovery.md) when something looks wrong.
