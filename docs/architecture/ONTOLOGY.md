# Atomization and ontology

Status: historical exploratory hypothesis; non-authoritative where it
conflicts with the [current public architecture](README.md)
Version: 0.1

## What counts as an atom

An atom is the smallest source-grounded unit that is useful to cite, search,
compare, evaluate, connect, or revisit independently.

It passes all applicable tests:

1. **Independent addressability** — a researcher could reasonably link to or
   ask for this unit.
2. **Semantic unity** — a statement atom makes one truth-evaluable move; an
   entity atom identifies one stable referent; a question asks one resolvable
   question; a symbolic atom expresses one mathematical object or step.
3. **Local scope** — its assumptions, regime, population/system, modality, and
   important qualifications can be stated without importing hidden context.
4. **Evidence sufficiency** — one or more exact source spans actually support
   the object as written.
5. **Composability** — it can participate in typed relationships without
   requiring a reader to guess what the endpoints mean.
6. **Useful indivisibility** — splitting it further would create fragments
   that are merely grammatical or explanatory rather than independently useful.

A sentence is not automatically an atom. One sentence may contain several
claims; several sentences may be required to support one scoped claim.

## Object families

The universal `knowledge-atom-v0.1` envelope separates semantic family from
domain type.

### Entity

A stable referent that can exist independently of one claim about it.

Initial kinds:

- `concept`
- `model`
- `method`
- `experiment`
- `dataset`
- `observable`
- `parameter`
- `theorem`
- `mechanism`

### Statement

One proposition attributable to the source.

Initial kinds:

- `claim`
- `definition`
- `assumption`
- `observation`
- `result`
- `prediction`
- `criticism`
- `limitation`
- `interpretation`
- `negative_result`

The kind describes the statement's role in the paper, not whether the system
believes it.

### Question

One explicitly asked or source-grounded unresolved question.

Initial kinds:

- `research_question`
- `open_question`
- `problem`
- `evidence_gap`

### Symbolic

A mathematical object whose notation and role matter.

Initial kinds:

- `equation`
- `inequality`
- `definition_expression`
- `derivation_step`
- `objective_function`
- `constraint`

### Argument

One inferential structure connecting premises, evidence, warrants, and a
conclusion. Arguments preserve the movement that atomization would otherwise
destroy. Their premise and conclusion endpoints are other knowledge atoms or
evidence spans.

## Statement normal form

Every statement retains a faithful natural-language form. When possible it
also receives a comparison form:

```text
subject / population / system
predicate or measured relation
object, value, or outcome
polarity
quantifier
modality
conditions and assumptions
regime, scale, or parameter domain
source stance
```

The comparison form is an index, not a replacement for the faithful statement.
It enables candidate generation while preserving the source's own language.

## Equations are not strings

An equation object should contain:

- source-faithful LaTeX or another lossless representation;
- printed label and exact locator;
- equation role: definition, assumption, model law, derived result,
  constraint, approximation, or worked instance;
- symbol table with source meanings, domains, units, and local aliases;
- governing assumptions and parameter regime;
- links to the claims or argument steps the equation supports;
- relation to neighboring equations: derived-from, equivalent-to,
  specialization-of, approximation-of, or contradicts-under-shared-scope;
  and
- a visual or equivalent source-fidelity check.

Two visually similar equations are not equivalent until variable meaning,
normalization, domain, and assumptions align.

## Relationship objects

A relation is a first-class scientific judgment. Its endpoints have roles, so
the graph can represent binary edges and small hyperedges.

Initial taxonomy:

- identity: `same_referent`, `alias_of`, `version_of`, `equivalent_to`
- evidence: `supports`, `fails_to_support`, `replicates`, `fails_to_replicate`
- logical: `entails`, `contradicts`, `consistent_with`, `independent_of`
- scope: `qualifies`, `narrows`, `broadens`, `special_case_of`,
  `boundary_condition_for`
- development: `extends`, `generalizes`, `refines`, `corrects`, `supersedes`
- dependency: `assumes`, `requires`, `uses`, `derived_from`, `motivates`
- operational: `defines`, `measures`, `operationalizes`, `tests`, `predicts`
- argumentative: `objects_to`, `answers`, `leaves_open`, `reframes`
- comparative: `agrees_with`, `disagrees_with`, `apparent_tension_with`

Taxonomy labels are not enough. Every verified relationship must say what
changes in interpretation, validity, use, or the next research action.

## Contradiction protocol

Never promote a candidate directly from semantic similarity to
`contradicts`.

A verified contradiction requires:

1. specific source-local statement endpoints;
2. aligned referent, outcome, and variable meanings;
3. materially compatible scope, assumptions, regime, and quantifiers;
4. incompatible polarity, value, existence claim, or predicted outcome;
5. exact evidence for each side;
6. an explanation of the conflict dimension; and
7. explicit consideration of `qualifies`, `different_context`,
   `apparent_tension_with`, or `uses_different_definition` as alternatives.

If scope alignment is unresolved, the relation remains `needs_review`. If the
claims apply to different regimes, the correct relationship is usually a
boundary or qualification, not contradiction.

## Definitions and contested concepts

Definitions are source-local statement objects linked to entity objects.
Canonical concepts do not carry one silently synthesized definition. They
collect definition objects and expose agreements, variants, and conflicts.

This allows one paper to define measurement independence through one
conditional factorization and another to use a stronger or weaker condition
without the system flattening them into a single sentence.

## Open questions and resolution history

An open question records:

- the exact question;
- who asks or motivates it;
- presuppositions and scope;
- why it matters;
- evidence gaps that make it open;
- proposed answers;
- later objects that answer, partially answer, reframe, or invalidate it; and
- current review status for the selected corpus revision.

Questions are never marked resolved merely because a later paper discusses the
topic.

## What does not become an atom by default

- rhetorical transitions;
- generic background repeated without a source-specific contribution;
- isolated names or citations;
- decorative examples;
- every algebraic intermediate line;
- every sentence in a proof;
- bibliography entries treated as evidence for the cited work; or
- model-supplied background needed only to make prose sound complete.

These receive an explicit source-unit disposition so omission remains
auditable.
