# Clinical Evidence Provenance Research Program

Status: concept foundation

## Purpose

This note records the emerging research program connecting Retinology and
Harness Labs. It is a statement of direction, not a claim of regulatory
compliance, a finalized protocol, or a product specification.

## Investigator position

The program is grounded in community vitreoretinal practice, with a prospective
academic affiliation in vision science and optometry. This position is
deliberately distinct from a conventional clinician-scientist career dominated
by protected research time, grants, laboratories, and academic promotion.

Community practice exposes the production environment of retinal care:
disconnected imaging systems, proprietary devices, fax-based referrals,
incomplete longitudinal records, duplicated testing, and large volumes of
clinically valuable data sequestered within individual clinics. Academic
partnership can contribute standards expertise, research governance, clinical
validation, and a path from local clinical utility to regional infrastructure.

## Central problem

Ophthalmology clinics routinely generate large volumes of multimodal data, but
the data are rarely interoperable, longitudinally coherent, or research-ready.
The evidentiary lineage of retrospective clinical research is often weak.
Published analyses may provide reproducible statistical code while leaving the
construction of the underlying dataset dependent on undocumented chart review,
subjective interpretation, spreadsheet editing, and irrecoverable judgment
calls.

Reproducible analysis is therefore not necessarily reproducible research. A
result is not fully auditable when its arithmetic can be rerun but its source
evidence, extraction decisions, cohort membership, and transformations cannot
be reconstructed.

## Program thesis

Artificial intelligence makes it feasible to extract structured observations
from clinical documents while preserving links to their sources. Clinical
research in the AI era should publish a machine-readable evidence manifest
alongside each paper. Within the limits of privacy and authorization, that
manifest should permit a human or agent to trace every result backward through
the complete chain of evidence:

```text
source clinical artifact
  -> extracted observation
  -> normalized variable
  -> cohort inclusion decision
  -> transformation
  -> statistical result
  -> table, figure, or claim
```

For each step, an authorized verifier should be able to determine:

- which patients and observations contributed;
- where each value originated;
- which extraction system produced it;
- which model, prompt, schema, and software version were used;
- whether and why a human corrected it;
- which inclusion and exclusion rules were applied;
- which transformations occurred; and
- whether the source supports the extracted representation.

AI is the extraction and traversal mechanism, not the ultimate trust anchor.
Trust rests on preserved source data, immutable lineage, explicit
transformations, versioned methods, authorization-aware verification, and
documented human judgment.

## Publication evidence model

Because patient-level source documents generally cannot accompany a public
paper, the evidence package should have at least two layers:

1. **Public manifest:** schemas, cohort logic, transformation lineage, software
   and model versions, analysis code, aggregate integrity evidence, and
   disclosure of human decisions.
2. **Governed verification package:** source-linked patient-level provenance
   available only inside an authorized environment.

Cryptographic commitments may demonstrate that an authorized verifier examined
the same artifacts used in the study without publicly releasing those
artifacts. They cannot, by themselves, establish that the artifacts were
interpreted correctly. Interpretation requires traceable extraction plus
targeted human or agent review.

## Initial clinical wedge

A regional inherited retinal disease service is a strong initial setting
because it requires integration of:

- longitudinal phenotype and progression;
- multimodal retinal imaging;
- electroretinography and standardized testing;
- pedigrees and family relationships;
- genetic variants and evolving classifications;
- geographically distributed care;
- referrals between community and academic settings; and
- aggregation of rare cases for research.

Although inherited retinal disease may be the initial domain, the more general
objective is a provenance-preserving clinical data layer for ophthalmology.

## Relationship between Retinology and Harness Labs

Retinology is the demanding clinical implementation. Harness Labs is the
methodological infrastructure required to build and verify it credibly.

The same evidence principle recurs at every level:

```text
clinical claim    <- traceable clinical evidence
research result   <- traceable cohort and transformations
software behavior <- traceable requirements, changes, and verification
agent action      <- traceable context, authority, and evidence
```

If Retinology's implementation has weak provenance, unverifiable
transformations, or uncontrolled agent-generated behavior, it cannot credibly
improve the provenance of clinical research. Harness Labs therefore treats
reliable AI-assisted development as part of the scientific method rather than
as a separate software concern.

## Unifying intellectual question

The program examines what happens when clean causal or computational models
meet heterogeneous real-world environments.

In genetics, the simplified model is:

```text
pathogenic genotype -> disease
```

The research question is why this relationship often fails in real
populations.

In agentic software, the simplified model is:

```text
good prompt + capable model -> correct feature
```

The harness question is why this relationship often fails in real
repositories.

Both demand attention to modifiers, ascertainment effects, missing context,
failure observability, provenance, and evidence that generalizes beyond a
controlled demonstration.

## Candidate research directions

1. Define a machine-readable provenance contract for retrospective clinical
   research.
2. Measure extraction accuracy at the level of source-supported clinical
   assertions rather than document-level summaries.
3. Represent uncertainty, disagreement, correction, and temporal change without
   erasing the original evidence.
4. Develop privacy-preserving verification methods for patient-level lineage.
5. Determine which parts of cohort construction can be reproduced
   deterministically and which require governed expert judgment.
6. Evaluate whether evidence manifests improve error detection, peer review,
   reproducibility, and reuse.
7. Establish interoperable representations for longitudinal ophthalmic
   phenotype, imaging, electrophysiology, and genetic findings.
8. Study how development provenance in AI-built clinical software affects the
   credibility of the research data it produces.

## Long-term role

The emerging role is that of an infrastructure-building surgeon: clinical
practice exposes the data problem, scientific training defines the evidentiary
standard, and AI makes it possible to build systems that conventional clinical
and academic machinery have not supplied.
