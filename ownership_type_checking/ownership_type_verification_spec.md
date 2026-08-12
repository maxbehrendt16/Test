# Ownership Type Verification Tool — Build Spec for Claude Code

## 1. Purpose

Build a Python/Claude Code–powered research tool that reviews ~25.8K CLP database
properties flagged as having a **possibly incorrect ownership type** (APT / COA / HOA), and for
each one either confirms the existing DB label or determines the correct type — with reasoning,
sourced evidence, and a confidence/archetype tag. This mirrors the architecture of the prior
duplicate-detection tool (checkpointing, dry-run, structured decisioning, "Not Enough Info" as a
first-class outcome) but the decision space here is simpler: three mutually exclusive categories
instead of open-ended duplicate matching.

## 2. Critical framing: the prior is strongly against changing the label, and check the right column

**The field being verified is `Master_Ownership Type` (APT / COA / HOA) — not `Master_Property
Type` (SFU / MDU).** These are two different columns in the source data: `Master_Property Type`
describes physical building structure and is not the value in question. Every trigger rule, every
evidence check, and every output decision in this spec is about `Master_Ownership Type`. Getting
this column right at the very first step of processing each row is non-negotiable — conflating the
two fields was the root cause of a major miscount in scoping this project (see below), and the same
mix-up inside the tool's own reasoning would silently corrupt every downstream decision.

The scale and error-rate estimates below are corrected from an earlier, significantly overstated
scoping pass:

- **Total properties to check: ~25.8K** (not the ~123–130K originally estimated).
- **Estimated properties with an actually-incorrect `Master_Ownership Type`: ~733** (based on the
  same 2.85% HW Asset Perimeter change-rate benchmark as before, applied to the corrected 25.8K
  population).
- Naming-based triggers are a much smaller share of the population than originally scoped — see
  the corrected table in §3.

That means **the correct output for the overwhelming majority of flagged properties is still "DB
label confirmed,"** if anything more emphatically than before: roughly 733 of 25,800 properties
(~2.85%) are expected to actually need a correction.

The previous attempt at this task got the classification wrong most of the time. That failure
almost certainly came from treating "research the property" as a neutral, open-minded
investigation — which invites the model to over-weight the first plausible-sounding thing it
finds (usually marketing copy) and flip a label that was actually correct. This tool must be built
to counteract that tendency structurally, not just via a "be careful" instruction:

- **Default posture: confirm the DB label.** Only override it when Tier 1 or Tier 2 evidence (see
  §4) directly contradicts the DB label, corroborated by a second independent source.
- **A single Tier 3 source (e.g., a leasing website) is never sufficient to override the DB label**,
  regardless of how confident it sounds — except in the narrow, bounded case described in §4.1.
- **The trigger reason itself is never evidence.** The property was selected for review because of
  a naming pattern, unit ratio, floor count, fee presence, or leasing listing — that trigger
  explains *why we're looking*, not *what the answer is*. The model must not let the trigger bias
  its reading of ambiguous evidence toward "confirming the trigger's implication."
- **"Not Enough Info" / "Confirmed — Insufficient Evidence to Override" must be common outcomes**,
  not edge cases. If evidence is thin, mixed, or Tier 3-only, the correct output is "keep the DB
  label, low confidence" rather than a forced call in either direction.

## 3. The six trigger rules — and what each one actually implies

The ~25.8K properties come from six distinct rules, not one. They carry very different
reliability, and the tool should treat them differently rather than applying one uniform "check
the name" process.

**The input file does not arrive with trigger labels attached.** The tool must compute which
rule(s) each property matches itself, directly from the row's data, before reasoning about it. All
six rules are checks against `Master_Ownership Type` (never `Master_Property Type`):

| Rule | Condition (compute from these columns) | Signal type | Relative reliability | Corrected count |
|---|---|---|---|---|
| Type-Name Mismatch (APT) | `Master_Ownership Type` ≠ APT, and `Master_Property Name` contains Apartment/Apt/Flats/Lofts | Naming | **Low** — highest false-positive risk (see §5.1) | 1.9K (<1%) |
| Type-Name Mismatch (HOA) | `Master_Ownership Type` ≠ HOA, and name contains Homeowner/HOA | Naming | Low-Medium | 6.0K (2%) |
| Type-Name Mismatch (COA) | `Master_Ownership Type` ≠ COA, and name contains Condo/COA | Naming | Low-Medium | 3.3K (1%) |
| Low Unit-to-Building Ratio | `Master_Ownership Type` ≠ HOA, and (`Master_Units_50+` / `Master_Building Count_50+`) < 5 | Structural/GIS | **Higher** — physical construction pattern, not marketing | 8.2K (3%) |
| High Unit-to-Building Ratio | `Master_Ownership Type` ∉ {COA, APT}, and (`Master_Units_50+` / `Master_Building Count_50+`) > 1 | Structural/GIS | **Higher** | 1.4K (<1%) |
| High Floor Count | `Master_Ownership Type` ∉ {APT, COA}, and `Master_Floor Count` > 3 | Structural/GIS | **Higher** — a >3-floor HOA is close to a physical impossibility per the classification guide's own HOA disqualifier | 43 (<1%) |
| Has Fees | `Master_Ownership Type` ∉ {HOA, COA}, and `Master_Monthly Association Fees` is not null | Governance/billing | Medium — fee presence contradicts a true APT (dues are an APT disqualifier), but the fee field itself could be miscoded | 2.4K (1%) |
| Has Leasing Info | `Master_Ownership Type` ≠ APT, and `Leasing Company` is populated | Marketing | **Lowest** — see §5.1, this is the single biggest source of prior errors | 2.3K (1%) |

**Total unique properties to check: ~25.8K (8% of the CLP DB). Estimated actual errors: ~733
(2.85% of flagged properties).**

Fall back to `Master_Units_20+` / `Master_Building Count_20+` for the ratio calculations when the
`_50+` fields are null. Use `Building Count Bin` as a sanity cross-check on the computed ratio if
the raw counts look inconsistent with it.

**Naming rules match on substring, not whole word.** `Contains([Master_Property Name], "apt")`
will match "Captains," "Aptos," or any other name that happens to contain that letter sequence,
regardless of whether it has anything to do with apartments. This is expected behavior, not a bug
to fix — the tool should still treat it as a genuine trigger match and research the property
normally. It's simply one more reason the naming rules sit at the bottom of the reliability
ranking: a meaningful share of naming-rule matches will be coincidental substring hits rather than
real signal, and the tool's evidence-based research (not the trigger match itself) is what
determines the outcome either way.

**Implication for the tool:** Structural triggers (unit ratio, floor count) describe the physical
building and are hard to fake or misrepresent online — weight them more heavily. Naming and
leasing-info triggers describe how a property is marketed or named, which is only weakly
correlated with legal ownership structure — weight them least, and never let them alone justify a
label change.

### 3.1 Multi-trigger properties get elevated prior confidence of error

A single property can match more than one of the six rules at once (e.g., an APT-named property
that also has listed leasing info, or an HOA-typed property with both a low unit-to-building ratio
and a fee value present). Since the tool computes all six rule checks directly from each row
itself, it will naturally see the full set of matching rules for a given property in one pass — no
merge or join across files is needed for this.

Two independent rules agreeing is a meaningfully stronger prior than either alone, since they're
less likely to both be coincidental or both be marketing artifacts — especially when the two
triggers come from different signal types in the §3 table (e.g., one structural + one naming,
rather than two naming rules that likely share the same root cause).

- After computing all matching rules for a property, treat multi-trigger properties as **elevated
  priority for a full two-attempt search** rather than settling for Attempt 1 — don't let an early
  "Confirmed" from a quick check short-circuit review of a property multiple rules flagged.
- Two triggers of the *same signal type* (e.g., two naming rules, which shouldn't co-fire often
  given the categories are mutually exclusive) do not compound confidence the way two triggers of
  *different* signal types do — treat same-type overlap as redundant, not corroborating.
- This does not lower the evidence bar in §4 — a multi-trigger property still needs Tier 1/2
  corroborated evidence to actually override the DB label. It only means the tool should look
  harder and be less willing to settle for "Not Enough Info" on a thin Attempt-1 search before
  running Attempt 2.
- Add `trigger_count` and `trigger_types` (list) to the output schema in §7, computed by the tool
  itself, so batch summaries can report override rate by trigger-count as an additional sanity
  check alongside override rate by individual rule.

## 4. Evidence hierarchy (apply to every property, regardless of trigger)

**Tier 1 — Legal/authoritative (can independently justify an override, with one corroborating source):**
- County recorder's Declaration of Condominium / CC&Rs / Master Deed language
- State business registry entity type (e.g., Florida Sunbiz: "Condominium Association Inc.",
  "Homeowners Association Inc.")
- County tax assessor's official use-code / property-class field

**Tier 2 — Structural (strong corroborating evidence, rarely sufficient alone):**
- County GIS parcel map: single parcel for whole complex (→ APT-leaning) vs. one parcel per unit
  (→ COA/HOA-leaning)
- Officially published unit count / building count for the community (from the community's own
  site, HOA management company page, or local news — not from a listing aggregator)

**Tier 3 — Corroborating only, never sufficient alone:**
- MLS / Zillow / Realtor.com listing presence, individual sale histories
- Property's own marketing/leasing website ("apply now," "leasing office," "floor plans")
- General web search snippets, forum mentions, local news human-interest coverage

**Rule: an override requires at least one Tier 1 or Tier 2 source, corroborated by a second
independent source of Tier 1 or 2.** Tier 3 evidence can support a decision already justified by
Tier 1/2, but cannot drive one on its own — except in the narrow, bounded case described in §4.1.

### 4.1 Bounded exception: Tier-3-only override

Real-world pattern driving this exception: **investor-owned single-family rental communities
mislabeled as HOA.** A single owner (often an institutional investor or fund) holds every lot in a
platted single-family subdivision and rents the homes out through one on-site or regional leasing
office. There is no individual-unit sale history to find because no individual unit has ever been
sold — every source describing the property is necessarily Tier 3 (a leasing site, an aggregator, a
management company's own portfolio page), because there is no deed, declaration, or registry entry
for an association that doesn't exist. Under the strict §4 rule, a genuine APT masquerading as an
HOA in the DB can never be corrected, because the very evidence that would prove it is structurally
unavailable. The same gap also applies when there's real Tier 1/2 evidence but only **one**
independent source of it — a normal override needs a second, corroborating Tier 1/2 source (§4),
and one alone can't carry it. This section defines a narrow, bounded path to override on Tier 3
evidence — **either alone, or alongside that one insufficient Tier 1/2 source** — when the
following four conditions hold. Normally all four are required; if you also have that one
supporting Tier 1/2 source, only three of the four are required (see below). Do not conclude "only
one Tier 1/2 source was found, so no override is possible" without first checking whether this
exception applies using your Tier 3 evidence — that's exactly the gap it exists to cover.

**All four conditions required (three, with one supporting Tier 1/2 source — see below):**

1. **3+ independent Tier 3 sources that agree, at least ONE of which ties the DB's
   `Master_Property Name` and `Address` together (the "anchor").** "Independent" means different
   companies or platforms — a property's own leasing site, an aggregator (Apartments.com, Zillow
   rentals, ApartmentRatings.com, etc.), and a distinct third source (a local news rental feature, a
   different management company's portfolio listing, tenant review site) all describing the same
   single-owner rental operation. Three restatements of one syndicated feed (e.g., the same listing
   mirrored across five aggregator sites that all pull from one data provider) do not satisfy this
   — this is the same corroboration-vs-accumulation distinction as §5.9, just applied at a stricter
   threshold because Tier 3 evidence alone is carrying the whole decision here.
   **Not every source needs to state both the name and the address — only one does.** Once at least
   one source explicitly confirms both together (e.g. Zillow names the property at the DB's
   address), other sources describing only the address (without repeating the name) or only the
   name (without repeating the address) still count toward the 3+, since the anchor already
   established that the address genuinely belongs to this named property. Per the §5.6 amendment,
   if NO source ever ties the name and address together, address-only sources don't count toward
   this condition at all, no matter how many you find — they're not weaker evidence, they're
   evidence about whatever property is actually at that address, which may not be this one.
2. **Zero contradicting evidence anywhere.** No MLS record of an individual unit sale, no county
   deed showing a different owner name for any specific address within the property, nothing found
   in Attempt 2's targeted searches (county recorder, tax assessor, state business registry) that
   points the other way. A single contradicting data point anywhere is disqualifying, regardless of
   how much Tier 3 evidence agrees.
3. **At least one internal DB field corroborates single ownership.** The clearest version of this:
   a null `Master_Monthly Association Fees` on a property large enough that a real HOA/COA of that
   size would almost always have a fee on file by now — a true association this size with no fee
   ever recorded anywhere in the DB is itself a signal that no association actually exists to charge
   one. **A null/blank fee field, by itself, is sufficient for this condition** — it does not need a
   second internal field on top of it; the condition stands on its own evidence. A single
   `Owner`/`Cleaned Owner` value with no per-unit variation can also satisfy this condition — the
   point is that the DB's own data, not just external search results, independently points toward
   single ownership.
   **This condition asks for ONE corroborating field, not an audit of every internal DB field for
   agreement.** Once you have one (most commonly the blank fee field), the condition is met — stop
   there. Do not go hunting through unrelated columns looking for something that might complicate or
   contradict it. In particular, `Bulk Flag`, `Bulk Package Type`, and `% Bulk Overall` describe a
   **bulk internet/TV/phone service contract with an ISP** (this is a broadband-competition dataset)
   — they have nothing to do with real-estate ownership concentration and must never be read as "X%
   bulk-owned" or used as evidence for or against single ownership. A property showing "50% bulk"
   telecom service is not evidence of 50%-bulk real-estate ownership; it is simply not a real-estate
   signal at all and should be ignored for this condition.
4. **No structural edge case (§5.7) explains the pattern instead.** Rule this out explicitly before
   relying on the exception: a housing cooperative, a condo-hotel/timeshare, a senior/age-restricted
   or student community, or an investor/institutional *bulk-ownership* COA/HOA (§5.4 — where the
   underlying legal structure is still individually-parceled COA/HOA even though a single investor
   owns most units) would all produce the same "looks like a rental from the outside" surface
   pattern for a different underlying reason, and none of those are actually a mislabeled APT. If any
   of these plausibly fits at least as well as "no association ever existed," the exception does not
   apply.

**Relaxed threshold with partial Tier 1/2 support.** If, in addition to the Tier 3 evidence above,
one (not two) independent Tier 1 or Tier 2 source also points the same way, only **three** of the
four conditions above need to hold, not all four — any one of the four can be the gap, as long as
the other three genuinely hold. A single Tier 1/2 source is real, authoritative-tier evidence; it
just can't carry a normal override alone (that needs a second independent one), and this exception's
evidence bar reflects that it's still worth something. Without that extra source (pure Tier 3), all
four conditions are still required — this relaxation exists specifically to credit the additional,
real corroboration a lone Tier 1/2 source provides, not to generally loosen the bar. This is why
`evidence_tier_used` should be `Mixed` rather than `Tier 3` for these cases in the tool's output
schema.

**There is no minimum-age or build-year requirement.** A recently-built investor-owned rental
community qualifies exactly the same way an old one does, as long as conditions 1-4 above are
otherwise met — absence of individual-sale history is meaningful for a genuinely single-owner
rental property regardless of when it was built. (This is distinct from the §5.3 lease-up-phase
failure mode, which is about a genuine COA/HOA that HAS started individual sales but is early in
that process — still a real pattern worth watching for in general research, just not a hard-coded
age gate on this specific exception.)

**If enough conditions hold (four normally, or three with a supporting Tier 1/2 source):**

- The override is allowed, but **confidence is capped at Medium, never High** — High stays
  reserved for cases resting on two independently-corroborated Tier 1/2 sources. Even a clean case
  is inherently less certain than direct legal/structural confirmation, and the confidence field
  must reflect that regardless of how consistent the picture looks.
- Tag the decision with a new archetype flag, **"Tier-3 Corroborated Override,"** distinct from
  every other archetype in §5, so these cases are easy to isolate in batch summaries. This is the
  highest-risk override path in the whole tool — it's the one case where the tool changes a label
  without ever finding two independent Tier 1/2 sources — and it deserves to be trivially
  filterable for extra scrutiny. Specifically **oversample "Tier-3 Corroborated Override" cases
  during the §9 QC pass** relative to their share of the batch; if this archetype's real-world
  accuracy doesn't hold up under manual review, tighten or retire this exception before scaling up,
  rather than letting it run at the same trust level as the rest of the tool.

If too many of the four conditions are unclear, unverified, or only partially met — more than one,
or any at all without that supporting Tier 1/2 source — do not apply the exception; fall back to
the normal §6 decision process (which, absent sufficient Tier 1/2 evidence, lands on Not Enough
Info). This exception is meant to be rare and tightly bounded, not a general-purpose lowering of
the evidence bar for Tier 3 evidence.

## 5. Known failure modes / pitfalls — be explicit about all of these

### 5.1 Marketing language is not ownership structure (the most important pitfall)
A "leasing office," "apply now," or "schedule a tour" listing is evidence of **current rental
operation**, not legal ownership structure. A huge share of individually-deeded COA and HOA units
are professionally managed and rented out by investors, either individually or as a bulk-owned
pool. The presence of a leasing company or rental marketing site must **never** be treated as
evidence that a property is legally an APT. This is almost certainly the dominant cause of the
prior tool's high error rate, since "Has Leasing Info" and the APT naming rule both surface exactly
this kind of language.

### 5.2 Naming is a trigger, not evidence
The property was selected for review *because* its name conflicts with its DB type. The model must
not treat that same naming conflict as evidence toward resolving the conflict — that's circular.
State explicitly in the prompt: "The property name is never evidence for or against a
classification, only the reason this property is being checked."

### 5.3 Lease-up phase ambiguity
New COA/HOA developments often operate a leasing/reservation office before individual units are
sold and recorded. During this phase they are legally COA/HOA (declaration is recorded) but look
identical online to a true rental APT — single point of contact, no MLS sales history yet, no
individual owner records. Check recording/construction dates against the absence of sales history
before treating "no MLS history" as APT-confirming.

### 5.4 Investor/institutional bulk ownership
Distinct from 5.1 above: some COA/HOA communities have a majority of units owned by a single
investor or fund and rented as a block, sometimes even marketed under a single "community" leasing
brand. This can look exactly like a single-owner APT from a marketing search, but the underlying
legal structure (recorded declaration, individual parcel IDs even if commonly held) is still COA/HOA.
Parcel-level records (Tier 2) are the way to distinguish this from a true APT.

### 5.5 Mixed-use / multi-component developments
A single branded development may contain multiple legally distinct components — e.g., an apartment
tower and a separate townhome HOA phase under one marketing name. Confirm which specific
address/parcel the DB record refers to before classifying the whole named development.

### 5.6 Stale or renamed properties
If a name-based search returns nothing or inconsistent results, don't default to "cannot
determine, treat as no evidence either way" without a fallback. Re-search by address, and check
whether the property has been renamed (common after condo conversions or rebranding) before
concluding evidence is unavailable.

**Amendment: address-only or name-only sources are usable evidence only once some source ties the
name and address together (the "anchor").** Not every source needs to state both
`Master_Property Name` (allowing for an explained rename/rebrand) and `Address` — it's fine for
most sources to describe only the address, or only the name, as long as AT LEAST ONE source
explicitly confirms both together. E.g. Zillow names the property at the DB's address (the anchor);
two other sites separately describe apartments at that same address without repeating the name —
all three are usable, since the anchor already established that the address genuinely belongs to
that named property. But if NO source ever ties the name and address together, a source that
matches the address while describing a clearly different, unrelated development is not weaker
evidence for this record — it's evidence about a *different* record, and using it means the DB's
address is likely wrong, not that a name-based search simply needs to fall back to searching by
address. Real failure this amendment closes: "Dearlove Manor Apts" (DB: not APT) was overridden to
APT because several Tier 3 sources confirmed the DB's listed address hosts a genuine rental
apartment complex — but none of those sources actually call it "Dearlove Manor Apts"; they describe
a different, unrelated complex that happens to sit at that address, and no source ever tied the two
together. That's the DB's address being stale/wrong, not evidence about Dearlove Manor Apts itself.
Before crediting a group of Tier 3 sources, confirm at least one of them plausibly ties the *same*
property's name to the address on file — if none of them do, the correct output is **Not Enough
Info**, not a decision built on the other property's characteristics. This is now a named condition
of §4.1's exception (condition 1) and is enforced in code via a required
`tier3_name_address_anchor_confirmed` field, not left to the model's judgment alone.

### 5.7 Structural edge cases outside the three-way taxonomy
Some properties won't cleanly fit APT/COA/HOA:
- **Condo-hotels / timeshares** — legal condo declaration exists, but fractional/interval
  ownership and hotel-style operation make "COA" a technically-correct-but-misleading label.
  Flag as a distinct edge case rather than silently forcing into COA.
- **Manufactured home communities** — residents own the structure but lease the land. Doesn't map
  cleanly onto any of the three categories.
- **Senior living / student housing** — often colloquially called "apartments" regardless of legal
  structure (could be true rental APT, or could be an age-restricted HOA/COA). Do not let the
  "senior apartments" or "student apartments" naming convention drive the call — check legal
  structure independently.
- **Age-restricted or master-planned communities with a name like "The Apartments at [Community]"**
  used purely as a marketing brand for what's legally a COA — check the actual declaration.

**Amendment: structural edge cases are never overridden, full stop.** (Housing cooperatives,
condo-hotels/timeshares, manufactured home communities, and senior/student housing where the naming
convention doesn't reflect true legal structure.) Once one of these is identified, the decision is
always **Confirmed** and `determined_type` is always the existing DB label — never pick whichever
of APT/COA/HOA seems like the closest technical fit and override to it. This is enforced in code, not
just requested in the prompt: the tool's `structural_edge_case` output field, once set to anything
other than `none`, forces `decision` back to `Confirmed` and `determined_type` back to the DB label
regardless of what else the model submits. This amendment exists because of a real failure: a
property was correctly identified as a housing cooperative, its monthly association fees were cited
as evidence of that co-op structure, and then the *same* fees were used to justify overriding the
label to APT anyway — exactly backwards, since a monthly fee is evidence *against* a property being
a true rental APT (dues are an APT disqualifier), never evidence *for* one. Mixed-use/multi-component
developments (§5.5) are explicitly **not** covered by this amendment — that's a scope-identification
problem, not a taxonomy misfit, and once the specific component is confirmed it should be decided
normally.

**Second amendment, specifically for housing cooperatives: a suspected co-op is treated the same as
a confirmed one.** If research raises "this might be a co-op" as a live possibility that can't be
fully resolved, that is itself a reason for `decision: Confirmed`, not a reason to weigh other
evidence and lean toward Override anyway. Do not reason "there's a co-op signal here, but not
enough evidence to be sure it's a co-op specifically, so I'll go with the stronger APT/COA/HOA
signal instead" — an unresolved co-op possibility should bias toward leaving the label alone, not
be set aside once a competing signal shows up. This is enforced by a second, independent code
backstop: an Override whose own `reasoning` text mentions "co-op" or "cooperative" anywhere is
forced back to `Confirmed`/the DB label, regardless of whether `structural_edge_case` was set —
this exists because relying on that field alone assumes the model always remembers to set it
when a co-op possibility comes up, and it doesn't always.

### 5.8 Fee field miscoding
"Has Fees" as a trigger assumes the fee field is populated correctly. Before treating fee presence
as evidence of COA/HOA structure, sanity-check that the fee isn't a miscoded value (e.g., a
one-time deposit, a data entry artifact, or a fee belonging to a different nearby property from a
prior dedup issue in the CLP DB). If the fee amount and structure look legitimate and recurring,
treat it as Tier 2-ish supporting evidence, not decisive on its own.

### 5.9 Corroboration, not accumulation
Multiple Tier 3 sources repeating the same underlying fact (e.g., three different aggregator sites
all showing the same leasing office) is not the same as independent corroboration. Independent
corroboration means two *different kinds* of source (e.g., a county registry entry AND a distinct
GIS parcel record) — not multiple restatements of the same marketing claim. §4.1 applies this same
principle at a stricter threshold (3+ sources, from genuinely different companies/platforms) for
the one case where Tier 3 evidence alone is asked to carry an override decision.

## 6. Verification process (per property)

1. **Attempt 1 — broad search:** property name + address. Identify the development, note initial
   signals (Tier 3 mostly), and identify what type of trigger flagged it (adjust skepticism per §3
   table).
2. **Attempt 2 — targeted search (only if Attempt 1 is inconclusive or contradicts the DB label):**
   - County tax assessor / GIS parcel lookup for the specific address
   - County recorder search for a Declaration of Condominium / CC&Rs / HOA covenant
   - State business registry search for the governing entity name and type
3. **Decision:**
   - DB label confirmed by evidence found, or no contradicting evidence found → **Confirmed**
   - Tier 1/2 evidence contradicts DB label, corroborated by a second independent Tier 1/2 source →
     **Override — [correct type]**
   - The §4.1 bounded-exception conditions hold (all four, or three of four with a single
     supporting-but-insufficient Tier 1/2 source) → **Override — [correct type]** on Tier 3
     evidence, confidence capped at Medium, archetype flag "Tier-3 Corroborated Override"
   - Evidence is mixed, thin, Tier 3-only (and the §4.1 exception does not apply), contradictory, or
     genuinely ambiguous even after Attempt 2 (e.g., evidence points different directions, or the
     property sits in a legitimately unclear situation like a mixed-use master development) →
     **Not Enough Info — default to no change** (keep DB label, flagged low-confidence for optional
     human review). **When in doubt, don't change the label.**
   - Property doesn't fit the three-way taxonomy → **Structural Edge Case — [description]**
4. Every decision gets a **short, 1–2 sentence** plain-language reasoning and the specific evidence
   tier(s) relied on, plus source URLs. Keep it concise — this field is read at scale, not as a
   research memo.

## 7. Output schema (per property)

| Field | Description |
|---|---|
| `property_id` | CLP DB identifier |
| `db_listed_type` | Current DB value (APT/COA/HOA) |
| `trigger_rule(s)` | Which of the six rules flagged this property |
| `trigger_count` | Number of distinct rules that flagged this property |
| `trigger_types` | List of signal types represented (Naming / Structural / Governance-Billing / Marketing) — used to distinguish corroborating overlap from redundant overlap per §3.1 |
| `determined_type` | Model's conclusion: same as DB, or override type, or "Edge Case" |
| `decision` | Confirmed / Override / Not Enough Info / Structural Edge Case |
| `confidence` | High / Medium / Low |
| `evidence_tier_used` | Tier 1 / Tier 2 / Tier 3 / Mixed |
| `reasoning` | **1–2 sentences**, plain language. Short enough to scan at scale — not a research memo. |
| `sources` | List of source URLs |
| `archetype_flag` | One of the failure-mode tags from §5 if applicable (e.g., "Marketing Language Trap," "Lease-Up Phase," "Investor Bulk Ownership," "Mixed-Use Development," "Stale/Renamed," "Structural Edge Case," "Fee Miscoding"), or "Tier-3 Corroborated Override" per §4.1 |

## 8. Architecture (mirroring the prior dedup tool)

- Python + Claude Code, structured similarly to `claude_code_instructions.md` from the dedup
  project.
- **Checkpointing** every N properties (resumable given the ~25.8K scale).
- **Dry-run mode** on a small labeled sample before full run — ideally against a sample where the
  true answer is already known from the prior 2.85% error-rate QC work, to sanity check the
  false-positive rate before scaling up.
- **Two-attempt search logic** as in §6, escalating from broad to targeted rather than firing every
  search type on every property (cost/time control at ~25.8K scale).
- **Default-to-DB-label error handling**: any exception, timeout, or unparseable search result
  defaults to "Not Enough Info," never to a forced override.
- **Batch summary output**: decision breakdown by trigger rule, trigger-count, archetype-flag
  counts, override rate overall and by trigger rule (the override rate for structural triggers
  should end up higher than for naming/leasing triggers if the tool is working correctly — worth
  building as a sanity check into the summary itself). Break out "Tier-3 Corroborated Override"
  separately from other overrides in this summary per §4.1, since it's the highest-risk path and
  needs its own visibility, not just a line item inside the general archetype-flag counts.

### 8.1 Staged batch run plan

Given the ~25.8K scale, this will run in deliberately small, gradually increasing batches rather
than as one continuous job — starting at 10–100 properties, scaling up gradually, and capping at 2K
properties per run at least through the early stages. The tool should be built to support this
directly rather than assuming a single large run:

- **A `--batch-size` / input-slice parameter** so a subset of the full property list can be pulled
  for a given run without re-touching properties already processed.
- **Checkpointing must key off `property_id`, not row position**, so batches can be run
  out-of-order, re-run, or expanded without double-processing or gaps.
- **Each batch produces its own summary** (per §8, decision breakdown, override rate by trigger
  rule and trigger-count) in addition to a running cumulative summary across all batches
  processed so far — this lets override-rate drift be caught early (e.g., if batch 3 suddenly shows
  a much higher override rate than batches 1–2, that's worth pausing on before scaling further).
- **Early small batches (10–100) should be manually spot-checked in full** before moving to the
  next size tier, in the same spirit as §9's QC step — the staged run plan and the QC sample are
  complementary, not redundant: QC picks a stratified sample for accuracy; staged batching controls
  blast radius while that accuracy is still being validated.
- No hard ceiling above 2K needs to be built in now, but the batch-size parameter should make
  raising it later a config change, not a code change.

## 9. Suggested QC step before full-scale run

Given the known ~2.85% true error rate, pull a stratified sample (e.g., 150–200 properties across
all six trigger types, weighted toward the ones you're least confident about) and manually review
the tool's calls before running the full ~25.8K. Track the override rate against the expected
~2.85% (≈733 properties) — if the tool's override rate is running dramatically higher than that,
it's very likely repeating the previous tool's mistake of over-trusting Tier 3 marketing evidence,
and the prompt needs tightening before the full run.

**Oversample "Tier-3 Corroborated Override" cases specifically.** Per §4.1, this is the one path
where the tool changes a label without ever finding Tier 1/2 evidence, and it's new and unproven at
scale. Pull every such case in the QC sample if the count is small enough, or a substantially
higher proportion than its natural share of the batch otherwise — this archetype needs to
demonstrate real-world accuracy before it's trusted at full volume, not just pass the general QC
bar applied to the rest of the tool.

## 10. Input file format

Input arrives as an Excel file, one property per row, structured like the CLP DB export used for
the prior duplicate-checking project (a wide export — ~170 columns). Batches will be sent one at a
time, without any trigger labels attached — **the tool must compute which of the six rules a
property matches itself**, directly from the columns below, per §3. The tool should read the full
row per property (for logging/output purposes) but only needs to actively reason over the subset
of columns mapped here.

### 10.1 Column mapping

| Spec concept | Source column(s) |
|---|---|
| Property ID | `RecordID` |
| Address | `Address`, or the `Geocoded_*` / `CASS_*` fields for a cleaner parsed version |
| Property name | `Master_Property Name` |
| **DB-listed ownership type — the value being checked** | **`Master_Ownership Type`** (APT / COA / HOA) — this is the field every trigger rule and every decision is about |
| Physical structure type (context only — do not confuse with the field above) | `Master_Property Type` (SFU/MDU) |
| Unit count | `Master_Units_50+` (fall back to `Master_Units_20+`) |
| Building count | `Master_Building Count_50+` (fall back to `Master_Building Count_20+`); `Building Count Bin` as a sanity cross-check — drives the Low/High Unit-to-Building Ratio triggers |
| Floor count | `Master_Floor Count` — drives the High Floor Count trigger |
| Association fees | `Master_Monthly Association Fees` — drives the Has Fees trigger; cross-check against §5.8 fee-miscoding pitfall; also relevant to the §4.1 bounded-exception check |
| Leasing company presence | `Leasing Company`, `Leasing Company Contact Name`, `Leasing Company Phone` — drives the Has Leasing Info trigger; treat per §5.1, never sufficient alone |
| Owner / property manager / developer | `Owner`, `Cleaned Owner`, `Property Manager`, `Cleaned Property Manager`, `Developer Name`, `Cleaned Developer` — useful for identifying investor bulk ownership (§5.4) and distinguishing a true single-owner APT from a managed COA/HOA; also relevant to the §4.1 bounded-exception check |
| Senior / student / gated flags | `Master_Senior Flag`, `Master_Student Flag`, `Master_Gated HOA Flag` — direct pointers to the structural edge cases in §5.7 |
| Prior verified research (trustworthy) | `LLM_Property Name`, `LLM_Property URL`, `LLM_Address`, `LLM_Property Contact #*`, `LLM_Monthly HOA/COA fees`, etc. — see §10.3 |
| Amenities (secondary context) | `Consolidated Macro Amenities`, `Consolidated Luxury Amenities Count` |

**Not used:** `Future Build` / `CoStar_Building Status` are not treated as a special case. Properties
under construction or proposed should go through the same verification process as any other
property — no shortcut or separate handling based on build status. **`Bulk Flag`, `Bulk Package
Type`, and `% Bulk Overall` are also not used** — despite the name, this CLP export is a broadband-
competition dataset and these fields describe a bulk internet/TV/phone service contract with an ISP,
not real-estate ownership concentration. An earlier version of this spec incorrectly treated them as
a real-estate "bulk ownership" signal for §4.1's condition 4; that was wrong and caused the tool to
hallucinate a false contradiction (e.g. reading "% Bulk Overall: 0.5" as "only 50% single-owned")
against an otherwise-satisfied condition. Do not show these fields to the model or use them as
evidence for or against ownership type at all.

### 10.2 Sample input (from the corrected 25-record sample)

| RecordID | Property Name | Master_Property Type | **Master_Ownership Type** | Units | Building Count | Floor Count | Fees | Leasing Company |
|---|---|---|---|---|---|---|---|---|
| 605 | Barton Village - Flats I | SFU | **HOA** | 385 | 1.0 | 1.0 | — | — |
| 51537 | Captains Quarters | MDU | **COA** | 57 | 1.0 | 3.0 | $607 | — |
| 53935 | Oakwood Apartment Corp. | MDU | **COA** | 114 | 1.0 | 7.0 | — | — |
| 113862 | The Lofts at Maywood Park | MDU | **COA** | 55 | — | — | $146 | — |
| 172373 | The Lofts at Westinghouse | MDU | **COA** | 62 | 1.0 | 3.0 | $412 | R Brown Properties |
| 439607 | South Patrick Condominium Apartments, Inc | SFU | **HOA** | 161 | — | — | $344 | — |
| 446701 | Cross Creek Apartments | SFU | **HOA** | 80 | 40.0 | 1.0 | — | Advanced Precision |
| 451366 | Lake Colony Apartments | MDU | **COA** | 81 | 6.0 | 3.0 | $520 | — |

Every row in this corrected sample has a `Master_Ownership Type` of **COA or HOA** — none are
APT — while every property name contains "Apartments," "Apartment," "Flats," or "Lofts." This is
the Type-Name Mismatch (APT) rule working as intended: it fires when the name suggests APT but the
DB says otherwise, and confirming the DB label will be correct for the great majority of these
(consistent with the ~2.85% expected error rate). Two of these, RecordID 51537 ("Captains
Quarters") and 113862 ("The Lofts at Maywood Park"), don't have an obvious apartment-style keyword
at first glance — 113862 matches on "Lofts," and 51537 matches only because "Captains" happens to
contain the substring "apt" (the naming rule matches substrings, not whole words — see §3). Worked
through in full in §11. RecordID 446701 ("Cross Creek Apartments") is the property that motivated
the §4.1 bounded exception — see the dedicated worked example at the end of §11.

### 10.3 Prior LLM research columns are trustworthy

The `LLM_*` columns (e.g., `LLM_Property Name`, `LLM_Property URL`, `LLM_Property Contact #1`,
`LLM_Monthly HOA/COA fees`) reflect **verified research completed by a colleague**, not the earlier
unreliable automated pass this spec is designed to avoid repeating. Where these are populated,
treat them as a reliable input the tool can use directly — a good starting point for Attempt 1
rather than something to re-derive from scratch or treat with suspicion. If they're present and
internally consistent with the DB label, that's meaningful supporting evidence. If they're present
but silent on ownership type specifically (most of these fields describe address, contacts, fees,
amenities — not legal structure), they still help confirm the model has the right property before
it goes looking for Tier 1/2 ownership-type evidence.

## 11. Worked examples (first 5 properties in the sample, researched live)

These are real research results — not hypothetical — produced by applying this spec's methodology
by hand to the first 5 rows of the sample file. They're included so Claude Code has a concrete
target for output quality and tone, and so you can spot-check the methodology itself before it's
encoded into the tool. **Note on format:** the reasoning below is written out at length here so the
logic is inspectable, but the actual `reasoning` field the tool produces should be 1–2 sentences,
per §6/§7. Please tweak freely — this is a starting point, not a final answer.

### 605 — Barton Village - Flats I (DB: HOA)

| Field | Value |
|---|---|
| `trigger_rule(s)` | Type-Name Mismatch (APT) — name contains "Flats"; **and** High Unit-to-Building Ratio — HOA ∉ {COA, APT} and 385 units / 1 building = 385 |
| `trigger_count` / `trigger_types` | 2 / [Naming, Structural] |
| `determined_type` | HOA (no change) |
| `decision` | **Not Enough Info — default to no change** |
| `confidence` | Low |
| `evidence_tier_used` | Tier 3 only (active rental listing; developer's phase description doesn't rise to Tier 2) |
| `reasoning` | "Flats I" is actively marketed for rent, but this sits inside a mixed-use master development with both for-sale and for-lease phases and no Tier 1 legal source was found — genuinely ambiguous, so the DB label stands per §6's default. |
| `sources` | apartmenthomeliving.com; bartonvillage.com |
| `archetype_flag` | Lease-Up Phase, Mixed-Use Development |

### 51537 — Captains Quarters (DB: COA)

| Field | Value |
|---|---|
| `trigger_rule(s)` | Type-Name Mismatch (APT) — "Captains" contains the substring "apt" |
| `trigger_count` / `trigger_types` | 1 / [Naming] |
| `determined_type` | COA (confirmed) |
| `decision` | **Confirmed** |
| `confidence` | High |
| `evidence_tier_used` | Tier 3 (consistent, high-volume) |
| `reasoning` | Multiple MLS/brokerage listings confirm individually-owned, individually-sold condo units at distinct prices, matching the DB label. |
| `sources` | Zoocasa; Compass; eXp Realty |
| `archetype_flag` | None |

This one is a good example of the substring-matching behavior in §3: "Captains" tripped the rule
purely by coincidence, not because the name has anything to do with apartments. The trigger match
is still legitimate per the rule's actual logic — it's just a reminder that a matched trigger
doesn't mean the underlying signal is meaningful, which is exactly why naming sits at the bottom of
the reliability ranking.

### 53935 — Oakwood Apartment Corp. (DB: COA)

| Field | Value |
|---|---|
| `trigger_rule(s)` | Type-Name Mismatch (APT) — name contains "Apartment" |
| `trigger_count` / `trigger_types` | 1 / [Naming] |
| `determined_type` | COA (confirmed) |
| `decision` | **Confirmed** |
| `confidence` | High |
| `evidence_tier_used` | Tier 1/2 |
| `reasoning` | Independent sources identify this as a housing cooperative with individually-sold shares; rental-aggregator "leasing team" language reflects owner sublets, not ownership structure. |
| `sources` | homes.com; cross-referenced against apartments.com, forrent.com |
| `archetype_flag` | Marketing Language Trap, Structural Edge Case (Housing Cooperative) |

### 113862 — The Lofts at Maywood Park (DB: COA)

| Field | Value |
|---|---|
| `trigger_rule(s)` | Type-Name Mismatch (APT) — name contains "Lofts" |
| `trigger_count` / `trigger_types` | 1 / [Naming] |
| `determined_type` | COA (confirmed) |
| `decision` | **Confirmed** |
| `confidence` | High |
| `evidence_tier_used` | Tier 1 |
| `reasoning` | Operates as "The Lofts at Maywood Park Owners Association Inc" with individually-sold MLS units; "Lofts" describes architectural style, not ownership. |
| `sources` | ZoomInfo; LoopNet; condo.com |
| `archetype_flag` | None |

### 124179 — Rye Colony Apartment (DB: COA)

| Field | Value |
|---|---|
| `trigger_rule(s)` | Type-Name Mismatch (APT) — name contains "Apartment" |
| `trigger_count` / `trigger_types` | 1 / [Naming] |
| `determined_type` | COA (confirmed) |
| `decision` | **Confirmed** |
| `confidence` | High |
| `evidence_tier_used` | Tier 1 |
| `reasoning` | A brokerage listing confirms an individually-sold "Co-Op" unit; same housing-cooperative pattern as RecordID 53935, and consistent with the row's own trustworthy `LLM_Property Name` value ("...Inc."). |
| `sources` | Brown Harris Stevens listing |
| `archetype_flag` | Marketing Language Trap, Structural Edge Case (Housing Cooperative) |

### Pattern across all 5

All five properties confirm the DB label (no change), consistent with the ~2.85% expected error
rate. Two of the five are housing cooperatives — a structural edge case (§5.7) worth flagging to
Claude Code as a recurring pattern, especially in NY-area properties with "Apartment Corp." or
"... Apartments, Inc." naming. Barton Village - Flats I is the one genuinely ambiguous case: a
multi-trigger, lease-up-phase, mixed-use-development property where evidence leans one way but
never clears the Tier 1/2 bar — correctly resolved to "no change" per §6's default rather than a
forced call. Captains Quarters is a useful reminder that the naming rule's substring matching will
regularly produce trigger matches with no real connection to the underlying signal.

### 446701 — Cross Creek Apartments (DB: HOA) — the case that motivated §4.1

Unlike the five above, this one does **not** resolve to "no change." It's included separately
because it's the real property that motivated the §4.1 bounded Tier-3-only exception, and it's a
useful worked example of that exception actually firing, condition by condition.

**Facts:** 100 Cross Creek Drive, LaGrange, GA. DB-listed as HOA. 80 units across 40 buildings (a
ratio of 2 — consistent with a platted single-family/duplex-style subdivision, not a single large
structure). Single leasing company ("Advanced Precision") for the entire property. Built ~36 years
ago. Zero individual-unit sales found in MLS or county deed records at any point across those 36
years. `Master_Monthly Association Fees` is null.

| Field | Value |
|---|---|
| `trigger_rule(s)` | Type-Name Mismatch (APT) — name contains "Apartments"; High Unit-to-Building Ratio — HOA ∉ {COA, APT} and 80 units / 40 buildings = 2; Has Leasing Info — HOA ≠ APT and Leasing Company ("Advanced Precision") populated |
| `trigger_count` / `trigger_types` | 3 / [Naming, Structural, Marketing] |
| `determined_type` | **APT** (changed from HOA) |
| `decision` | **Override — APT**, via the §4.1 bounded Tier-3-only exception |
| `confidence` | **Medium** (capped — Tier 3 only; never High under §4.1) |
| `evidence_tier_used` | Tier 3 (three independent, non-mirrored sources; no Tier 1/2 source exists or could exist for this pattern) |
| `reasoning` | Three independent, non-syndicated sources (the property's own leasing site, Apartments.com, and ApartmentRatings.com reviews) consistently describe one owner and one leasing office for all 80 units, with no MLS or county deed record of an individual sale ever found and a null DB association fee corroborating that no real HOA exists; nothing suggests a co-op, condo-hotel, or bulk-owned COA/HOA instead. |
| `sources` | crosscreekapts.com (property's own site); Apartments.com; ApartmentRatings.com |
| `archetype_flag` | Tier-3 Corroborated Override |

**Why all four §4.1 conditions hold:**

1. **3+ independent sources, with a confirmed name/address anchor** — the property's own site
   names "Cross Creek Apartments" at the DB's address (the anchor tying name and address together);
   Apartments.com and ApartmentRatings.com are two distinct additional companies, not mirrors of one
   syndicated feed, independently describing the same single-owner, single-leasing-office operation
   at that address.
2. **Zero contradicting evidence** — Attempt 2's targeted searches (county recorder, tax assessor,
   state business registry for LaGrange/Troup County, GA) turned up no Declaration of Condominium,
   no HOA covenant, and no MLS or deed record of any individual unit ever selling separately.
3. **Internal DB corroboration** — `Master_Monthly Association Fees` is null despite the property
   being large enough (80 units) that a real HOA of this size would almost certainly have a fee on
   file by now. (This property also happens to be ~36 years old with zero sales history across
   that whole span, which independently reinforces the picture — but that age fact is descriptive
   color here, not a required condition; a newly-built version of this same property would satisfy
   condition 3 just as well on the blank fee field alone.)
4. **No structural edge case fits better** — not a housing cooperative (no share-based ownership
   language anywhere), not a condo-hotel, not senior/student housing, and not an investor
   bulk-owned COA/HOA (§5.4) — there is no evidence of individual parcels or a declaration existing
   at all, which is what would distinguish "one investor owns every COA unit" from "no COA/HOA
   exists here in the first place."

Because all four hold, this overrides to APT rather than falling back to Not Enough Info — but at
capped Medium confidence, and flagged "Tier-3 Corroborated Override" so it's isolated in the batch
summary and specifically oversampled during the §9 QC pass before this exception is trusted at
scale.
