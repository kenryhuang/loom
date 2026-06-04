# Loom: Complete Design Specification

> First-principles design for self-evolving agent systems.
> Core axiom: **"Everything is a Loop."**

---

## Table of Contents

1. [First-Principles Derivation](#1-first-principles-derivation)
2. [The Minimal Loop](#2-the-minimal-loop)
3. [Context: The Universal Carrier](#3-context-the-universal-carrier)
4. [Uniform Interface](#4-uniform-interface)
5. [Composition Patterns](#5-composition-patterns)
6. [Loop Boundaries](#6-loop-boundaries)
7. [Evolution: The Meta-Loop](#7-evolution-the-meta-loop)
8. [Scaling: Loop Clusters](#8-scaling-loop-clusters)
9. [Trace: The Observability Primitive](#9-trace-the-observability-primitive)
10. [Design Principles](#10-design-principles)

---

## 1. First-Principles Derivation

### The Question

Unix has "everything is a file." Databases have "everything is a table." What is the single unifying primitive for agent systems — one that holds at the level of a single session, multi-agent coordination, and cross-session evolution?

### Constraints

The primitive must provide:
- **Uniform interface** — one set of operations works on everything
- **Composability** — the output of one component feeds the input of another

The primitive must work at three levels:
- **Micro** — a single agent instance in one session
- **Macro** — multiple agents coordinating
- **Temporal** — an agent improving itself over time

### Why "Everything is a File" Works

Unix's power comes from:
1. `open/read/write/close` works for devices, pipes, sockets, and actual files
2. Pipes connect stdout to stdin — programs compose via byte streams
3. A file descriptor wraps radically different things behind one interface

The pattern: find something that (a) everything can be expressed as, (b) has a small uniform set of operations, and (c) the output of one operation can be the input of another (closure property).

### What Distinguishes Agents

**Agent vs. Function:** A function is `f(x) → y` — stateless, one-shot. An agent **loops**: it observes, acts, observes the result, acts again.

**Agent vs. Thermostat:** A thermostat loops with fixed behavior. An agent **adapts** — it changes its behavior based on experience.

Two essential properties: **loop** (interaction over time) and **adaptation** (behavior changes from experience).

### Three Candidates Considered

| Candidate | Stance | Strength | Weakness |
|---|---|---|---|
| **Loop** (process-centric) | An agent IS its feedback loop | Captures what DEFINES an agent; subsumes the other two | Describes shape, not substance |
| **Experience** (data-centric) | An agent IS its accumulated experience | Makes learning first-class | Data type, not computational primitive; composition is "piling up" not "piping through" |
| **Context** (state-centric) | An agent IS its current context | Clean functional semantics; directly implementable | Too LLM-specific; doesn't capture goal-directedness |

### Conclusion

**"Everything is a Loop"** wins because it subsumes the other two. The loop PROCESSES context and PRODUCES experience. Neither context nor experience alone captures the dynamic, adaptive, goal-directed nature that defines an agent.

> **The Full Axiom:**
> "Everything is a Loop." Context is what flows through it. Experience is what it produces. Evolution is a loop that rewrites loops.

---

## 2. The Minimal Loop

### The Atom

A function is `f(x) → y` — one shot, no iteration. A loop must **potentially iterate**. The minimal loop has exactly four elements:

```
MinimalLoop = {
    identity        // WHO — role + constraints
    goal            // WHAT — objective + success criteria
    step(ctx) → (ctx', trace)   // HOW — one observe→decide→act cycle
    done(ctx) → bool            // WHEN — termination check
}
```

**Execution model:**

```
while not done(ctx):
    ctx, trace = step(ctx)
    traces.append(trace)
return ctx, traces
```

### Everything Maps to This Atom

| Component | As a Loop |
|---|---|
| Tool call | Loop with usually 1 iteration: send command → receive result |
| ReAct agent | Loop with many iterations: think → act → observe → think... |
| Sub-agent | Loop invoked by a parent loop via `nest` composition |
| Code reviewer | Loop: read file → find issue → report → next file → done when all reviewed |
| Evolution engine | Meta-loop: evaluate → analyze → mutate → evaluate... |
| Memory system | Loop: receive query → search → rank → return (1 iteration per query) |

The same four-element structure (`identity`, `goal`, `step`, `done`) describes all of them.

---

## 3. Context: The Universal Carrier

### The Five Layers

An agent makes a decision. What does it need? Exactly five things — exhaustive and orthogonal:

```
Context = {
    identity: {              // WHO am I?
        role                  //   "code reviewer", "evolution engine"
        capabilities          //   what I'm able to do
        constraints           //   what I must not do
    }

    goal: {                  // WHAT am I trying to achieve?
        objective             //   the task description
        criteria              //   how to know I succeeded
        budget                //   time / token / step limits
    }

    state: {                 // WHAT has happened so far?
        observations[]        //   what I've seen
        decisions[]           //   what I've decided + why
        pending[]             //   in-flight sub-loops
    }

    knowledge: {             // WHAT do I know?
        facts[]               //   ground truths
        heuristics[]          //   learned patterns ("when X, do Y")
        memories[]            //   distilled past experiences
    }

    affordances: {           // WHAT can I do?
        tools[]               //   available tools
        loops[]               //   available sub-loops I can invoke
        resources[]           //   files, APIs, environments
    }
}
```

**Why these five and not more?**
- Remove `identity` → the loop doesn't know what it is
- Remove `goal` → the loop doesn't know when to stop
- Remove `state` → the loop has no memory within a session
- Remove `knowledge` → the loop can't learn across sessions
- Remove `affordances` → the loop can't act

### Context Scoping Rules

When a parent loop spawns a child loop, context flows through three operations:

```
project(parent_ctx, child_goal) → child_ctx_in      // narrow
                    ↓
              child loop runs
                    ↓
emit(child_ctx_out) → child_output                   // extract
                    ↓
merge(parent_ctx, child_output) → parent_ctx'        // widen
```

Per-layer scoping:

| Layer | Parent → Child | Child → Parent |
|---|---|---|
| identity | Child gets its OWN identity | Not propagated |
| goal | Child gets a PROJECTED sub-goal | Completion status returned |
| state | Child starts EMPTY | Results and discoveries emitted |
| knowledge | INHERITED, read-only | New knowledge emitted, parent decides whether to merge |
| affordances | SUBSET of parent's | Not propagated |

This mirrors Unix process semantics: a child process inherits environment variables and file descriptors, gets its own memory space, and returns exit code + output.

---

## 4. Uniform Interface

Every loop, no matter how complex, exposes exactly the same interface — the `open/read/write/close` of the loop world:

```
create(identity, goal, affordances) → Loop      // instantiate
step(loop, ctx) → (ctx', trace)                  // advance one iteration
done(loop, ctx) → bool                           // check termination
trace(loop) → Trace[]                            // observe history
```

Four operations. Everything else — composition, scaling, evolution — is built on top.

### Design Decision: Trace is First-Class

`step()` always returns a **trace** alongside the new context. Traces are not optional logging — they are a first-class output, the same way every Unix process produces an exit code. This is what makes evolution possible: meta-loops consume traces from inner loops.

A loop without traces is like a Unix process without stdout — technically functional, but uncomposable.

---

## 5. Composition Patterns

Loops compose in exactly four ways. Each takes loops in and produces a loop out (closure property — a composed loop IS a loop).

### 5.1 Chain (Sequential)

```
chain(loop_a, loop_b) → loop_c

    ctx → [loop_a] → ctx' → [loop_b] → ctx''
```

Output context of A becomes input context of B. Like Unix pipes.

**Examples:**
- RAG pipeline: `retrieve → analyze → summarize`
- CI pipeline: `build → test → deploy`
- Agent workflow: `plan → execute → verify`

### 5.2 Nest (Hierarchical)

```
nest(outer, inner) → loop

    outer.step() internally:
        child_ctx = project(ctx, sub_goal)
        child_ctx' = run(inner, child_ctx)
        ctx' = merge(ctx, emit(child_ctx'))
```

Parent loop invokes child loop as part of its own step. Like a function calling a subroutine.

**Examples:**
- Coding agent (outer) spawns test-runner (inner) to verify an edit
- Planner (outer) delegates sub-tasks to executor (inner)
- Reviewer (outer) invokes linter (inner) for specific checks

### 5.3 Fork (Parallel)

```
fork(loop, contexts[]) → (results[], traces[])

    ctx_1 → [loop₁] → ctx_1'  ↘
    ctx_2 → [loop₂] → ctx_2'  → merge(ctxs') → ctx_merged
    ctx_3 → [loop₃] → ctx_3'  ↗
```

Same loop logic, different context slices, results merged. Like map-reduce.

**Examples:**
- 5 reviewers each examine different files, findings merged
- Parallel search: same query, different data sources
- Ensemble: same problem, different strategies, best result selected

### 5.4 Meta (Evolution)

```
meta(inner_loop, meta_loop) → evolving_loop

    round 1: run(inner) → traces₁
             meta.step(traces₁) → mutation₁
             inner' = apply(inner, mutation₁)
    round 2: run(inner') → traces₂
             meta.step(traces₂) → mutation₂
             inner'' = apply(inner', mutation₂)
    ...
```

A loop that **rewrites** another loop based on its traces. The meta-loop is itself a loop — same interface — and can itself be the target of a higher meta-loop.

**Examples:**
- AHE's evaluate→analyze→evolve cycle
- Hyperparameter tuning: train → evaluate → adjust
- Prompt optimization: run → score → refine prompt

### Closure Property

Every composition produces a loop:
- A chain of loops IS a loop (with a composite `step`)
- A nested tree IS a loop (outer's `step` invokes inner)
- A forked swarm IS a loop (`step` = fan-out + merge)
- An evolving system IS a loop (`step` = run inner + mutate)

Same `create/step/done/trace` interface all the way up.

### Context Flow Summary

| Pattern | Context in | Context out | Knowledge sharing |
|---|---|---|---|
| Chain | A receives original | B's output is final | B inherits A's state |
| Nest | Child gets projection | Parent merges child output | Child inherits parent knowledge (read-only) |
| Fork | Each copy gets a slice | Merge function combines all | Siblings isolated during execution |
| Meta | Inner gets current config | Meta modifies inner's identity/affordances | Meta accumulates cross-round traces as knowledge |

**Critical distinction:** Chain and Nest pass context forward. Fork fans out context. Meta modifies the loop itself, not just the context.

---

## 6. Loop Boundaries

A boundary is where **context changes scope**. Three rules define it:

### Rule 1: Single Goal

A loop owns exactly one goal. When the goal splits into independent sub-goals, that's a boundary — each sub-goal becomes a child loop.

```
"Fix the bug" (one loop)
    → "Find root cause" (child loop 1)
    → "Write the fix" (child loop 2)
    → "Verify the fix" (child loop 3)
```

### Rule 2: Context Isolation

A boundary exists wherever you need to NARROW context. If a sub-task shouldn't see everything the parent sees, it deserves its own loop with projected context.

```
Parent loop (full codebase access)
    → Security review loop (only sees auth files)
    → Performance loop (only sees hot paths + metrics)
```

### Rule 3: Observable Entry/Exit

Every boundary produces a trace event at entry (context projected in) and exit (output emitted). If you can't observe the transition, it's not a real boundary — it's internal logic within the parent's `step()`.

### When NOT to Create a Boundary

If a sub-task uses the same context, same affordances, and doesn't need independent tracing — keep it as internal logic within `step()`. Over-decomposition creates trace noise without value.

**Rule of thumb:** Create a boundary when you need isolation, independent tracing, or the sub-task could be reused elsewhere.

---

## 7. Evolution: The Meta-Loop

### What Gets Modified?

When a meta-loop observes failures in traces, it has three levels of intervention — from cheapest to most disruptive:

```
                            Cost / Risk
                                ↑
    Level 3: Structure      ███████████  Rewire how loops compose
    Level 2: Loop           ██████       Rewrite step() logic
    Level 1: Context        ███          Modify what the loop receives
                                ↓
                          Reversibility
```

### Level 1: Evolve Context

Change the input, not the machine.

- Add a heuristic to `knowledge` ("when you see a permission error, check file ownership first")
- Refine `identity` constraints ("you must run tests before committing")
- Improve `affordances` descriptions ("this tool returns JSON, not plain text")

The loop's `step()` function is unchanged — it just receives better context.

**Analogy:** You don't retrain a doctor. You give them better patient notes.

### Level 2: Evolve Loop

Change the machine itself.

- Rewrite a tool's implementation (the tool IS a loop — its `step()` changes)
- Add middleware that transforms context between steps
- Change the `done()` condition (e.g., add retry logic, tighten exit criteria)

**Analogy:** You retrain the doctor's diagnostic procedure.

### Level 3: Evolve Structure

Change the architecture.

- Add a new sub-loop (create a specialized sub-agent)
- Change composition pattern (chain → fork for parallelism)
- Remove a loop that's counterproductive
- Insert a validation loop after an action loop

**Analogy:** You reorganize the hospital's departments.

### Evolution Strategy

The meta-loop needs a strategy for which level to target. The principle: **always try the cheapest level first.**

```
EvolutionLoop = {
    identity: "I improve other loops by analyzing their traces"
    goal: "maximize success rate with minimal, evidence-backed changes"

    step(ctx):
        1. Observe:  read traces from inner loop
        2. Diagnose: classify failures by root cause
        3. Decide:
            - Information gap?     → Level 1 (evolve context)
            - Logic error?         → Level 2 (evolve loop)
            - Wrong architecture?  → Level 3 (evolve structure)
        4. Act:      apply minimal mutation at chosen level
        5. Predict:  which tasks should improve, which are at risk

    done(ctx):
        goal.criteria met OR budget exhausted
}
```

### How Traces Guide Evolution

The meta-loop diagnoses which level to target by reading specific trace fields:

| Trace signal | Diagnosis | Evolution level |
|---|---|---|
| `gap` is set ("context lacked X") | Information was missing | Level 1: add to knowledge |
| `surprise` is set ("expected X, got Y") | Step logic assumed wrong | Level 2: fix step function |
| Same failure across many loops | Structural mismatch | Level 3: rewire composition |
| Repeated timeout / budget exhaustion | Wrong loop boundaries | Level 3: decompose or merge |

### Recursion Depth

In practice, two evolution levels suffice:
- **Level 0:** The agent loop (does the work)
- **Level 1:** The evolution loop (improves the agent)
- **Level 2 (rare):** Evolution of the evolution strategy itself

The meta-loop can be evolved by a meta-meta-loop, but diminishing returns set in quickly. The system should default to two levels and add a third only when Level 1 evolution plateaus.

---

## 8. Scaling: Loop Clusters

Four scaling patterns, all built from the four composition primitives:

### 8.1 Pipeline (Throughput Scaling)

```
[ingest] → [transform] → [validate] → [output]
```

Each stage is a loop. Context flows left to right. Add stages to add capability. Identical to Unix pipelines.

**When to use:** Sequential processing where each stage adds value. Stages can be independently evolved.

### 8.2 Swarm (Parallel Scaling)

```
             ┌→ [worker₁] →┐
[split]  →   ├→ [worker₂] →├→  [merge]
             └→ [worker₃] →┘
```

Same loop logic, N copies, different context slices. Scale N up/down based on workload. `split` and `merge` are themselves loops.

**When to use:** Embarrassingly parallel problems. Each worker is independent. Scale by adding workers.

### 8.3 Hierarchy (Abstraction Scaling)

```
[strategist]
    ├→ [planner]
    │      ├→ [executor₁]
    │      └→ [executor₂]
    └→ [monitor]
```

Each level operates at a different abstraction. Parent loops set goals, child loops achieve them. Context narrows at each level.

**When to use:** Complex tasks requiring multiple abstraction layers. Higher levels make fewer, bigger decisions. Lower levels make many, small decisions.

### 8.4 Federation (Autonomous Scaling)

```
[agent₁] ←──→ [shared knowledge] ←──→ [agent₂]
                      ↕
                 [agent₃]
```

No central coordinator. Each agent is an autonomous loop. They share a `knowledge` context layer via read/write. New agents join by connecting to shared knowledge.

**When to use:** Resilient systems where any agent can fail without breaking the whole. Agents discover and use each other's results organically.

### Combining Patterns

These patterns compose freely:
- A hierarchy where each leaf is a swarm (hierarchical + parallel)
- A pipeline where one stage is a federation (sequential + autonomous)
- A swarm where each worker is a pipeline (parallel + sequential)

All built from the same four primitives: chain, nest, fork, meta.

---

## 9. Trace: The Observability Primitive

Every `step()` produces a trace. Traces serve two purposes:
1. **Debugging** — understand what happened and why
2. **Evolution** — provide the raw material for meta-loops to learn from

### Trace Structure

```
Trace = {
    // WHAT happened
    loop_id:      string          // which loop produced this
    step_number:  int             // which iteration
    input_ctx:    Context         // what the loop saw (snapshot)
    action:       Action          // what the loop did
    result:       Observation     // what came back

    // WHY it happened (decision observability)
    reasoning:    string          // the loop's rationale
    alternatives: Action[]        // what else was considered
    confidence:   float           // how certain the loop was

    // HOW it went (outcome observability)
    outcome:      pass | fail     // did this step succeed?
    gap:          string?         // what was missing from context?
    surprise:     string?         // what was unexpected?
    duration:     float           // wall-clock time
}
```

### Key Fields for Evolution

- **`gap`** tells the meta-loop "context was insufficient" → Level 1 fix (evolve context)
- **`surprise`** tells the meta-loop "the step logic assumed wrong" → Level 2 fix (evolve loop)
- Repeated structural patterns across traces → Level 3 fix (evolve structure)

### Trace Composition

When loops compose, traces compose too:
- **Chain:** traces concatenate in order
- **Nest:** child traces nest inside parent trace (tree structure)
- **Fork:** traces from parallel workers are collected, tagged by worker ID
- **Meta:** each evolution round's traces are grouped by round number

This produces a **trace tree** that mirrors the loop tree — observable at any level of abstraction.

---

## 10. Design Principles

### 10.1 The Loop is the Atom

Every component — tools, skills, sub-agents, memory systems, evolution engines — must be expressible as a loop with the same `create/step/done/trace` interface.

### 10.2 Context is the Carrier

All information flows through context. If a loop needs information, it must be in the context. If a loop produces information, it must emit it into context. No side channels.

### 10.3 Traces are Non-Optional

Every loop produces traces. Traces are the "experience" that enables meta-loops to work. A loop without traces is like a Unix process without stdout — functional but uncomposable and unevolvable.

### 10.4 Evolve at the Cheapest Level

When something goes wrong, try to fix it by changing context first (Level 1), loop logic second (Level 2), and structure third (Level 3). Cheaper fixes are safer and more reversible.

### 10.5 Boundaries = Scope Changes

Create a loop boundary where context needs to narrow. Don't create boundaries for organizational neatness — only where isolation, independent tracing, or reuse demands it.

### 10.6 Composition Produces Loops

A chain of loops IS a loop. A nested tree IS a loop. A forked swarm IS a loop. An evolving system IS a loop. The closure property must hold at every level.

### 10.7 Two Levels of Evolution Suffice

Agent (Level 0) + Evolution (Level 1) covers the vast majority of cases. Add Level 2 (evolution of evolution) only when Level 1 plateaus. Avoid deeper recursion — it adds complexity without proportional value.

---

## Appendix: Mapping to Existing Systems

| Existing System | Loom Interpretation |
|---|---|
| ReAct agent | A single loop: observe → think → act → observe... |
| Chain-of-Thought | Internal iterations within a single `step()` |
| Multi-agent debate | Fork composition: same goal, different identities, results merged |
| Tool use | Nested loop: agent (outer) nests tool (inner) |
| RAG pipeline | Chain composition: retrieve → rerank → generate |
| AHE | Meta composition: code-agent (inner) evolved by evolve-agent (meta) |
| Claude Code's /loop | Meta composition with fixed cadence |
| Prompt engineering | Level 1 evolution: modifying context.identity |
| Fine-tuning | Level 2 evolution: modifying the step function itself |
| Architecture search | Level 3 evolution: searching over composition structures |
