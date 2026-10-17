---
name: entity-investigation
description: Build evidence-backed entity relationships from local documents and imported datasets. Use when investigating a company, person or organisation and distinguishing namesakes.
version: 1.0.0
category: research
tags: [entities, evidence, companies, graph, entidades, empresas, expediente]
status: published
source: imported
---

## When to Use

Investigating relationships between entities using an existing local
collection. A useful result identifies the entity, states the relationship
and allows the reader to inspect the source that supports it.

## Procedure

1. Define the subject and question. Record stable identifiers, jurisdiction,
   legal name, known aliases and the relevant period. Keep a brand distinct
   from a legal entity until a source establishes their relationship.
2. Inventory imported documents and datasets with their dates and locations.
   Extract candidate names, identifiers, addresses, domains and officers.
   Preserve the original source text and the exact supporting passage.
3. Resolve candidates using identifiers and context. A matching name may
   suggest a candidate; conflicting jurisdictions or registry identifiers
   require separate entities. Record uncertain matches as hypotheses.
4. Add a relationship only with its subject, predicate, object, source and
   temporal scope. Distinguish ownership, employment, shared address and
   mention; none implies the others. Keep conflicting accounts separately
   visible instead of silently choosing the latest imported document.
5. Follow relationships relevant to the question and generate a dossier
   whose claims link back to local evidence. State what the collection
   cannot establish and which missing evidence would resolve the question.
   The workflow must remain usable without a hosted enrichment service.
6. Review [the namesake scenario](references/scenario.json) before merging
   entities on a name or attributing a domain to an organisation.

## Pitfalls

- Shared addresses and domains do not prove common ownership.
- Publication date and the date of a relationship can differ.
- Importing the same source twice does not create independent evidence.

## Verification

- Namesakes with different identifiers remain separate.
- Each factual relationship opens its supporting local passage.
- Dates and uncertainty survive graph and dossier exports.
- Removing an unsupported claim leaves unrelated evidence intact.

## Provenance

Method reference: https://github.com/reconurge/flowsint at
4c05849bc6ca5055ad187d056ef26e61b3bc2fea (Apache-2.0).
Original local procedure; hosted enrichment and paid lookups are unnecessary.
