---
name: decomposing-tasks
description: Break large or multi-part requests into small, checkable work packages using established Work Breakdown Structure and issue-tree methods. Apply to research, comparisons, diagnosis, and multi-site workflows; keep simple tasks simple.
---

# Decomposing tasks

Use methods you already know; do not invent a new methodology or force every request into the
company-research example. For execution, use PMI's deliverable-oriented Work Breakdown Structure
(WBS): break the requested outcome into outputs, then work packages. Apply its 100% rule: child
packages collectively cover their parent, including verification, without adding unrelated scope.
For an uncertain question such as "why did this decline?", use McKinsey-style issue/logic trees:
separate plausible explanations, prioritize the useful evidence, analyze, then synthesize an answer.
Keep sibling branches nonoverlapping and collectively sufficient (MECE where appropriate).

## From the request to executable work

- Identify the final outcome, every explicit deliverable, constraints, destinations, and what evidence
  would establish completion. Preserve counts, filters, deadlines, and required final actions. Use
  available context for inputs; surface a missing essential input rather than guessing it.
- Divide by outputs or questions, not arbitrary clicks or one bot per sentence. Recursively split a
  package while it contains several independently verifiable outputs or cannot be handed to one worker
  with a clear finish condition. Stop when it has usable inputs, one accountable output, bounded work,
  and an observable acceptance check. No fixed number of branches or depth is required.
- Turn each leaf into a small action sequence. Its instruction includes the input/source, operations,
  expected output, acceptance evidence, and what to do if the required evidence is absent. Distinguish
  facts observed on pages from hypotheses. For diagnosis, gather evidence that can discriminate between
  causes; do not plan only to confirm your favorite explanation.
- Sequence actual dependencies. A writer needs the verified research, and a comparison needs its source
  results. Parallelize independent reads when useful; keep a shared-destination write in one package.
  Distinguish "part of this deliverable" from "must finish before that action".
- Include the integration work: reconcile conflicting results, combine the outputs, verify the requested
  destination or answer, and report remaining gaps. Finishing all isolated branches is insufficient if
  nobody assembles the person's final result. Detail near-term work now and refine later packages as
  evidence arrives (rolling-wave planning), preserving all requested deliverables.
- Check coverage before dispatch: every requested outcome has a responsible package and acceptance
  check; no overlapping writes, missing prerequisite, invented capability, or unsupported promise.
  Reuse a suitable saved automation for execution without treating it as proof of this task's outcome.

## Map to Mia's existing runtime

Use the supported task fields, not a new planning schema:

- `goal`: a self-contained work package with its small action sequence and output/check. Workers do not
  receive the full conversation. Include relevant constraints and any data the package needs.
- `done_when`: a concrete completion condition for a substantial package, such as all required source
  records checked and a reconciled comparison produced; distinguish exhaustive coverage from a sample.
- `needs`: zero-based indexes of earlier tasks whose outputs this task consumes. Give the integration
  task all the producer dependencies. A dependency result may be partial or failed: inspect it and keep
  the corresponding outcome incomplete rather than assuming success.
- For a read-only comparison or answer, Mia's existing final-response stage can own synthesis after
  all workers report; an extra synthesis bot is optional. State that responsibility in the reply and
  give the producers compatible outputs. Use a dependent work package when integration itself needs
  browser actions, destination writes, or independent verification.
- `kind`, `tab`, `url`: use only actual browser capabilities and known destinations, following the
  managing-bots skill. Decomposition does not authorize new external actions or bypass approvals.

The six-task dispatch limit is a resource limit, not a six-item scope limit. Group related sequential
leaves within a complete worker goal when needed; do not emit extra tasks that the parser would drop
or omit the final synthesis to fit the limit. Preserve site concurrency limits. A simple request can
stay one task. A Play Automation request stays one `build` task: include the full outcome in its goal,
then record its internal work packages through `build_plan {"steps":[...]}` before testing. Each plan
entry should name the intended output and its check; it is not a completed-script step or test proof.

Use the full scope to judge completion. If a capability, authorization, source, or dependency blocks a
package, name the missing result and preserve it. Do not quietly substitute an easier task.

## Established sources

These are adaptations to Mia's existing fields, not a new framework:

- [PMI: Applying the WBS to the project lifecycle](https://www.pmi.org/learning/library/applying-work-breakdown-structure-project-lifecycle-6979): deliverables, complete scope, work packages, activities and sequencing.
- [McKinsey: Seven-step problem solving](https://www.mckinsey.com/capabilities/strategy-and-corporate-finance/our-insights/how-to-master-the-seven-step-problem-solving-process): define, disaggregate, prioritize, plan, analyze and synthesize.
