# chem-catalog-resolver

**A searchable catalogue of chemical products from multiple suppliers, where
the same substance is one row no matter how each supplier spelled it — plus a
list of every place the suppliers' own data is wrong.**

## What you get

Point it at a set of chemicals. It reads three supplier catalogues, works out
which listings are the same substance, and writes the answer to `reports/`.

**1. Substance clusters** (`clusters.json`) — the main output. Every supplier
listing grouped under one canonical substance. Real output from a run:

```
SUBSTANCE: Copper sulfate pentahydrate | CAS 7758-99-8 | CuH10O9S | 249.69 g/mol
   carlroth   P024.3   Copper(II) sulphate pentahydrate, 2.5 kg   ≥99,5 %, p.a., ACS
   carlroth   8175.1   Copper(II) sulphate pentahydrate, 1 kg     ≥98 %, cryst.
   carlroth   1L53.1   Copper(II) sulphate pentahydrate, 5 g      ROTI®METIC 99,999 %
   chemimpex  35293    Copper(II) sulfate pentahydrate            99.0 - 100.5% (Assay)
   chemimpex  36999    Cupric sulfate pentahydrate, Ultra pure    98.5 - 100.5% (USP)
   … 25 listings in total, across 2 suppliers

SUBSTANCE: Copper Sulfate | CAS 7758-98-7 | CuO4S | 159.61 g/mol
   carlroth   P023.2   Copper(II) sulphate, 1 kg                  ≥99 %, p.a., anhydrous
   chemimpex  35521    Copper(II) sulfate anhydrous               ≥99% (Trace metal)
   … 13 listings, across 2 suppliers
```

That is the whole point in one picture. Twenty-five listings spelled four
different ways — `sulphate`/`sulfate`, `Copper(II)`/`Cupric`, with pack sizes
and grades attached — collapsed into one substance. And **the anhydrous salt
stayed a separate cluster**, because it is a different chemical, even though
its name differs by a single word.

If you are sourcing copper sulfate pentahydrate, this is the list of everyone
who sells it and what grade they sell it at. That is the product.

**2. A row-by-row decision log** (`matches.json`) — for every SKU: which
substance it resolved to, how confidently, by what method, and the reasons.
Nothing is a black box.

**3. A review queue** (`review_queue.json`) — the cases the pipeline refused
to decide, hardest first. 15 rows out of 180 in the sample run. This is a
feature: it would rather hand you an ambiguous case than guess.

**4. A data-quality report** (`findings.json`) — every place a supplier's data
contradicts itself, another supplier, or the public reference. Missing CAS
numbers, registry numbers that fail their checksum, molar masses that don't
match the substance named.

**5. An accuracy score** (`summary.json`) — how often the matching is right,
measured against ground truth it was not shown. 99.3% precision on the main
label set.

### What it is not

It is not a live service, a website or a database server. It is a batch job:
you run one command, it writes JSON files, you read them. Wiring those files
into a search UI or a warehouse would be the next thing to build, not
something this does.

---

## How to read the rest of this

The sections below go from *why* to *how* to *how do we know it works*, and
they get more detailed as they go. You do not need all of it:

- **[Why this is hard](#why-this-is-hard)** and
  **[The problem this is built around](#the-problem-this-is-built-around)** —
  the case for the project. Two minutes.
- **[Running it](#running-it)** — the commands.
- **[Does any of it work?](#does-any-of-it-work)** — the measured accuracy,
  including a result that contradicted the design.
- Everything else is engineering detail: which sites permit crawling and why,
  how the matcher scores a pair, what broke on real data. Read it if you are
  reviewing the code; skip it otherwise.

---

## Why this is hard

The scraping is the easy half. The hard half is that **the same substance is
not the same record anywhere.** One supplier writes
`Copper(II) sulphate pentahydrate, 2.5 kg`, another writes
`Copper(II) sulfate pentahydrate`, a third writes `Coppervitriole`. One
publishes `CuSO4.5H2O`, the reference database publishes `CuH10O9S`. Three
SKUs share a CAS; two substances one token apart do not.

---

## The problem this is built around

Copper(II) sulfate is **7758-98-7**. Copper(II) sulfate pentahydrate is
**7758-99-8**. They are different substances with different molar masses —
159.6 vs 249.69 g/mol — and a buyer who receives one having specified the
other is short by 36% on the mass of copper.

Their names differ by one word. A token-similarity matcher scores them above
0.9 and merges them, and nothing downstream ever flags it.

So this pipeline treats **hydration state as a gate, not a feature**. When two
records both state a hydration state and the states differ, the pair is
rejected outright regardless of name similarity. The
[ablation](#does-any-of-it-work) measures what that gate is worth.

The governing principle throughout: **a wrong merge costs far more than an
unresolved record.** One produces a confident, invisible error; the other
costs somebody five minutes. Everything ambiguous is routed to a review queue
rather than resolved by lowering the bar.

---

## Sources

Three suppliers, chosen for genuinely different access shapes, plus a public
reference spine.

| Source | Role | Format | Contributes |
|---|---|---|---|
| [PubChem](https://pubchem.ncbi.nlm.nih.gov) | reference spine | JSON REST (PUG-REST) | canonical identity, registry numbers, formula, mass |
| [Loba Chemie](https://www.lobachemie.com) | supplier | server-rendered HTML (ASP.NET) | spec tables, impurity limits, HTML SDS in 8 languages |
| [Carl ROTH](https://www.carlroth.com) | supplier | JSON (SAP Commerce OCC API) | GHS codes, PDF SDS + spec sheets, German decimal commas |
| [Chem-Impex](https://www.chemimpex.com) | supplier | server-rendered HTML (Shopify) | CAS, formula, purity, **published PubChem CID** |

A reference spine is not optional. Without one, agreement between suppliers is
the only signal available, and two suppliers repeating the same upstream error
would look like confirmation.

### Sites deliberately not used

Most of the well-known names are unusable, and finding that out is part of the
result. Every exclusion is recorded in
[`config/targets.yaml`](config/targets.yaml) with its evidence:

| Site | Why not |
|---|---|
| ThermoFisher, TCI | Akamai returns **403 for `robots.txt` itself** to non-browser clients |
| Sigma-Aldrich | connection refused outright |
| Spectrum Chemical | DataDome CAPTCHA wall |
| Oakwood | Cloudflare block |
| Fisher Scientific | `robots.txt` serves fine but **disallows exactly the SDS and product-detail paths** |
| BASF | a producer, not a distributor — no per-product CAS + SDS catalogue |
| Fluorochem, ChemicalBook | `robots.txt` disallows the product/CAS detail pages |

Getting past the first four would mean building evasion. That is a different
project, and not one worth putting a name on.

---

## Access policy

`PoliteSession` ([`src/chemspec/http.py`](src/chemspec/http.py)) enforces
rather than consults:

- **Hosts are allow-listed.** A host not in `config/targets.yaml` cannot be
  fetched by accident; the attempt raises.
- **`robots.txt` is checked before every request**, and a disallowed URL
  raises rather than warns.
- **Status codes follow RFC 9309, with one deliberate tightening.** 404 means
  no rules exist, so fetching proceeds. **401/403 is treated as refusal** even
  though the RFC permits proceeding — a site that will not serve its own
  robots file to a non-browser client is running a bot wall.
- **Crawl-delay is over-honoured.** Loba declares `Crawl-delay: 5` in a group
  naming specific crawler agents, not in the wildcard group, so by the letter
  of the standard it does not bind this client. It is honoured anyway.
- **An API subdomain inherits its storefront's rules.** `api.carlroth.com`
  serves no `robots.txt` (404 = unrestricted), but it serves the same
  operator's catalogue, so `www.carlroth.com`'s rules are applied on top.
- **Everything is cached to disk**, so re-running costs the suppliers nothing.

### The one exception, stated plainly

PubChem's wildcard `robots.txt` group disallows `/rest/pug/` — while PUG-REST
is PubChem's documented public API with its own published rate policy, and the
same file contains `Allow: /rest/` in its named-crawler groups. The rule is
addressed to indexing crawlers.

This project takes that exception **explicitly**: declared in
`config/targets.yaml` under `robots_overrides`, scoped to one host and one
path prefix, requiring a written justification, run at roughly 2 requests/second
against a documented ceiling of 5, and **counted in every run's output** as
`http.robots_overrides_used` so it can be audited. No disguise, no evasion.

Deleting those four config lines makes the pipeline refuse the API. Nothing
else changes. The alternative needing no exception is PubChem's bulk FTP
distribution — see [Extending](#extending).

---

## Running it

```bash
python -m venv .venv && ./.venv/bin/pip install -e ".[dev]"
```

Re-verify every allow-listed host against its live `robots.txt` — worth doing
before any crawl, because permission is a moving target:

```bash
./.venv/bin/python -m chemspec check-targets
```

Harvest, resolve, check and report:

```bash
./.venv/bin/python -m chemspec run --limit 45
```

Useful flags: `--sources loba chemimpex`, `--no-sds`, `--limit 0` (uncapped),
`--accept 0.95 --review 0.80`.

Outputs land in `reports/`: `products.json`, `reference.json`, `matches.json`,
`findings.json`, `clusters.json`, `sds.json`, `review_queue.json`, and
`summary.json`.

```bash
./.venv/bin/python -m pytest
```

---

## How resolution works

**Tier 1 — exact CAS.** A registry number that passes its
[check digit](#cas-check-digits) and appears in the reference's registry list
is a link, not a guess. It is still sanity-checked: a page named as a hydrate
carrying the anhydrous CAS goes to review rather than being accepted, because
accepting it would launder the supplier's error into the output.

**Tier 2 — blocked name similarity**, for rows whose CAS is missing or invalid.

1. **Normalise.** Strip pack sizes, purity grades and pharmacopoeia
   boilerplate. Unify `sulphate`/`sulfate`, `cupric`/`copper(II)`,
   `aluminium`/`aluminum`. Parse hydration out into a structured field rather
   than leaving it as tokens. Split oxidation states so `copper(ii) sulfate`
   and `Copper Sulfate` share tokens.
2. **Block.** Bucket on the two longest name tokens, order-independent, so
   scoring is O(n · bucket) not O(n²). References are additionally indexed
   under their synonyms' keys — otherwise a substance listed only by trade
   name never reaches scoring at all.
3. **Gate.** Drop any pair whose stated hydration states disagree.
4. **Score.** Name similarity, corroborated by formula agreement (**by element
   counts, never as strings** — `CuSO4`, `CuO4S` and `CuSO4.5H2O` all parse)
   and molar mass within tolerance.
5. **Decide.** Accept above 0.92, review down to 0.78, reject below. A top
   candidate within 0.03 of the runner-up is demoted to review however high it
   scored: a near-tie means the evidence does not separate them.

### CAS check digits

A CAS registry number carries a positional mod-10 checksum in its last digit,
so transcription errors are catchable for free. `7758-99-8` is valid;
`7758-98-8` is well-formed and wrong. The pipeline distinguishes *malformed*
from *checksum-failed* from *missing*, because they are different defects.

This also makes extraction from SDS text reliable: EC and UN numbers sit right
next to the CAS and a bare regex picks them all up.

---

## Does any of it work?

Two ground-truth label sets, neither hand-annotated:

1. **Chem-Impex publishes the PubChem CID** on its product pages — a
   supplier-to-reference link produced by someone other than this pipeline.
2. **Valid CAS numbers resolve in PubChem.** For any row carrying one, the
   resolved substance is the correct answer.

The evaluation **withholds the CAS** and scores the name-only path on whether
it recovers the same substance — which is exactly the case the pipeline exists
for: a row whose identifier is missing or wrong, where the name is all you
have.

Reported per label set: how many rows had an answer, how many the matcher
*committed* to, how many it got right, and **how many it abstained on**.
Abstention is reported as a first-class outcome, not hidden in the recall
denominator, because a matcher that declines is behaving correctly.

### Correctness is substance identity, not record identity

The first version of this evaluation scored a prediction correct only if it
named the same PubChem CID as the label, and reported ~77% precision. Most of
those "errors" were not errors. **PubChem holds several CIDs per substance** —
parent records, depositor duplicates, hydrate variants — and the supplier's
published CID is one valid choice among them. The matcher was frequently
picking the better record and being marked wrong for it.

So two predictions count as the same answer when either the **InChIKey**
matches (same structure, hydration included) or their **registry numbers
overlap** (the registry itself asserting they are one substance). The report
breaks the result into `correct_exact_record` and
`correct_equivalent_record`, because that second number is itself a finding:
it measures how often a "canonical identifier" turns out not to be canonical.

Labels pointing at a CID the spine never resolved are excluded and counted as
`labels_unresolvable_in_spine` rather than scored as failures.

### Measured results

From a run of 180 SKUs across the three suppliers against a 74-substance
spine (`--limit 60`, full output in `reports/sample_summary.json`):

| Label set | Labels | Answered | Correct | Wrong | Abstained | Precision | Recall |
|---|---|---|---|---|---|---|---|
| CAS resolved via PubChem, withheld | 161 | 136 | 135 | 1 | 25 | **99.3%** | 83.9% |
| Chem-Impex published CID | 41 | 34 | 29 | 1 | 7 | **96.7%** | 78.4% |

The single CAS-set error is arguably not one: it matched
"Copper(II) trifluoromethanesulfonate" to PubChem's "Copper(II) triflate",
which is the same substance under a contracted name, held in a separate
record with no overlapping registry number.

Four of the CID rows are counted as `label_less_specific` rather than scored:
Chem-Impex cross-references PubChem's placeholder **"Calcium chloride
xhydrate"** (22691-02-7) from pages selling the dihydrate and the hexahydrate.
The matcher named the specific hydrate. It was more precise than the
supplier's own published identifier — which is the failure mode this whole
project is about, appearing in the ground truth itself.

### The ablation, and a result that contradicted the design

The same matcher runs with the hydration gate disabled — the state of the
world if `pentahydrate` were treated as a descriptive token:

| Evidence available | | Answered | Correct | Wrong | Precision |
|---|---|---|---|---|---|
| name + formula + mass | with gate | 136 | 135 | 1 | 99.3% |
| | **without gate** | 132 | 131 | 1 | 99.2% |
| name only | with gate | 135 | 123 | 11 | 91.8% |
| | **without gate** | 63 | 58 | 5 | 92.1% |

**The gate prevented zero wrong merges.** That was the thing it was built to
do, and on this data it never had to: where a supplier publishes a formula and
a molar mass, a hydrate differs from its anhydrous form by enough mass that
the wrong candidate is already penalised below threshold.

What it does instead only shows up once formula and mass are stripped away —
the case the pipeline actually exists for. There it **more than doubles the
rows the matcher can answer**, 63 → 135, gaining 65 correct answers for 6
additional errors, at unchanged precision. Without hydration to separate
them, a salt and its hydrate score so alike that the near-tie rule sends both
to review. The gate converts abstentions into answers.

That is a different claim from the one this was designed around, and it is
reported as measured rather than as intended. The constructed cases in
`tests/test_match.py` still show the gate blocking merges that nothing else
catches; this sample simply had better-populated spec fields than the
worst case.

---

## Data-quality findings

Defects only become visible when a page is held against its own SDS, against
the other suppliers, and against the reference:

| Check | Severity | What it catches |
|---|---|---|
| `cas_checksum_failed` | error | Valid-looking CAS that fails its check digit |
| `catalogue_sds_cas_conflict` | error | Supplier's page disagreeing with **its own safety data sheet** |
| `cross_supplier_cas_conflict` | error | One substance listed under different CAS numbers by different suppliers |
| `anhydrous_with_water_in_formula` | error | Named anhydrous, formula contains water |
| `hydration_name_formula_mismatch` | warning | Name says pentahydrate, formula has no water |
| `molar_mass_vs_reference` | warning | Mass off by more than 2% — usually a hydrate/anhydrous mix-up |
| `cas_not_in_reference` | warning | Valid CAS that PubChem does not associate with the matched substance |
| `cas_missing` | warning | No registry number published at all |

---

## Things the real data taught us

Each of these was a silent failure first and a fix second. They are recorded
because they are the actual texture of the work, and because each one would
have produced plausible-looking output while being wrong.

- **Loba serves PDFs from `.aspx` URLs.** Detecting format by file extension
  decoded PDF bytes as text and extracted nothing — and an SDS with no CAS in
  it looks like a *finding* rather than a bug. Format is now sniffed from the
  content's magic bytes.
- **`CuSO4` and `CuO4S` are the same compound.** Suppliers write conventional
  formulas, PubChem writes Hill notation. Comparing them as strings turned the
  strongest corroborating signal for a correct match into a penalty against
  it. Formulas are now parsed to element counts, hydrate dots and
  parenthesised groups included.
- **PubChem's anhydrous records do not say "anhydrous".** The hydrate is
  titled "…pentahydrate"; the anhydrous form is titled plainly. A gate firing
  only when *both* sides state a hydration state would never have fired
  against the record it most needed to. A hydrogen-free formula on a silent
  name is now read as positive evidence of anhydrous.
- **`Crawl-delay` can be declared for someone else.** Loba's sits in a group
  naming specific crawler agents, so it does not bind a generic client. The
  intent is obvious; it is honoured anyway.
- **A documented public API can be disallowed by its own `robots.txt`.** See
  [the exception](#the-one-exception-stated-plainly).
- **Seeds have to reach the crawler.** Loba is browsed by category, not
  searched, so the first N products in a category overlap almost nothing the
  other suppliers returned — and cross-catalogue matching with no overlap
  measures nothing at all. Discovery is now ranked by the seed set, without
  discarding the rest.
- **A dilute acid's SDS names water first.** The composition section lists
  components by proportion, so hydrochloric acid solution leads with
  7732-18-5. Taking the first CAS found and comparing it to the catalogue
  produced a confident "this supplier contradicts its own safety data sheet"
  — against a page that was entirely correct. The check now runs against
  every CAS in the document and reports the ordering separately, as
  information rather than as an error. Publishing a false accusation of a
  data defect would have been the worst output this project could produce.
- **The ground truth had the bug the project is about.** Four supplier
  cross-references pointed at PubChem's placeholder "Calcium chloride
  xhydrate" from pages selling a specific hydrate. Scored naively, the
  matcher was penalised for being right.

---

## What is not published here

The **code, schema, aggregate statistics and a small illustrative sample** —
not the harvested SDS documents or full catalogue dumps. Those are the
suppliers' copyrighted materials, and a repository that redistributes them
would be a worse liability than no repository. `data/` is gitignored; the
cache is local to whoever runs it.

---

## Extending

- **EPA CompTox** is allow-listed and unrestricted (`robots.txt` entirely
  commented out) but not yet wired; a DTXSID↔CAS crosswalk would give a second
  independent spine and turn reference disagreements into a measurable signal.
- **PubChem bulk FTP** would remove the need for the `robots_overrides`
  exception entirely, at the cost of downloading far more than this uses.
- **Certificates of analysis.** Loba links per-batch COAs; parsing them would
  let assay values be compared against the published specification rather than
  just the specification against itself.
- **More suppliers.** Add to `config/targets.yaml` and implement a module
  returning `SupplierProduct`. Check `robots.txt` first — and record the answer
  either way.

---

## Layout

```
config/targets.yaml     allow-list, declined sites with evidence, the one override
config/seeds.yaml       salt/hydrate seed set; anhydrous-hydrate CAS pairs
src/chemspec/
  http.py               PoliteSession: robots enforcement, crawl-delay, caching
  cas.py                registry-number validation and extraction
  normalize.py          name/hydration/formula/number normalisation
  match.py              blocking, hydration gate, scoring, review routing
  sds.py                GHS-section parsing for HTML and PDF safety data sheets
  quality.py            cross-source data-quality checks
  evaluate.py           held-out scoring and the hydration-gate ablation
  pipeline.py           orchestration
  sources/              pubchem, loba, carlroth, chemimpex
tests/                  including every hydrate trap as an assertion
```
