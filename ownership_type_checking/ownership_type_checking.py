"""
CLP Ownership Type Verification Tool.

For each CLP database property flagged as having a possibly-incorrect ownership type
(APT / COA / HOA), calls an LLM (with a web search tool enabled) to research the
property online and either confirm the existing DB label or determine the correct
type -- with reasoning, sourced evidence, and a confidence/evidence-tier tag.

This mirrors the architecture of the prior duplicate-detection tool
(checkpointing keyed off a stable ID, dry-run/--limit mode, structured decisioning
via a forced tool call, deterministic post-hoc guardrails, "Not Enough Info" as a
first-class outcome) but the decision space here is simpler: three mutually
exclusive categories instead of open-ended duplicate matching. Uses OpenAI only
(the Responses API with the built-in web_search tool), per the same OPENAI_API_KEY
used for the prior property-type classification pass.

The six trigger rules (Type-Name Mismatch x3, Low/High Unit-to-Building Ratio, High
Floor Count, Has Fees, Has Leasing Info) are NOT expected in the input file -- the
tool computes which rule(s) each row matches itself, directly from the row's own
columns, before any research happens. See compute_triggers().

Overrides normally require Tier 1/2 evidence (see SYSTEM_PROMPT's evidence hierarchy).
A single narrow exception -- spec §4.1, for investor-owned single-family-rental
communities mislabeled HOA, where no Tier 1/2 evidence can ever exist -- allows an
override built on Tier 3 evidence (alone, or alongside one insufficient-alone Tier 1/2
source), but only when the model's self-reported conditions survive the deterministic
cross-checks in _enforce_tier3_override_guardrail(): all four normally, or at least
three when a single supporting Tier 1/2 source also exists (tier3_partial_tier12_support).
These cases are capped at Medium confidence and isolated in the batch summary for extra
QC scrutiny.

Usage:
    export OPENAI_API_KEY=...
    python ownership_type_checking.py --input properties.xlsx --output results.xlsx
    python ownership_type_checking.py --input properties.xlsx --output results.xlsx --batch-size 25   # dry run

Staged rollout (per spec Section 8.1): given the ~25.8K population, run this in
small, gradually-increasing batches rather than one continuous job. --batch-size
caps how many *new* (not-yet-checkpointed) properties get processed on a given
invocation -- re-running the same command with the same --output/--checkpoint
resumes where the last batch left off, keyed by property_id (RecordID), never by
row position, so batches can be re-run, expanded, or processed out of order without
double-processing. Suggested progression: 10-100 properties (spot-check in full),
then a few hundred, capping at 2,000 per run through the early stages.
"""
import argparse
import json
import os
import random
import re
import threading
import time
from pathlib import Path

import pandas as pd
import requests
from bs4 import BeautifulSoup

import openai
from openai import OpenAI

DEFAULT_MODEL = "gpt-4o"
MAX_SEARCHES_PER_PROPERTY = 8
# Bumped from 6: research_property()'s in-loop sale-search correction (see
# MAX_SALE_SEARCH_CORRECTIONS below) can consume up to 2 extra turns rejecting a premature
# APT-override submission and asking for a real sale-listing search before accepting it -- this
# leaves room for that without starving the initial research itself of turns.
MAX_TURNS = 8
URL_FETCH_TIMEOUT = 10
URL_FETCH_MAX_CHARS = 3000

# Internal decision space the model chooses from. "Structural Edge Case" is intentionally
# not a decision here -- per the output design, a property that doesn't cleanly fit the
# three-way taxonomy still gets a best-fit APT/COA/HOA determined_type and a Confirmed/
# Override decision; the edge-case nature is described in the reasoning text instead of
# surfaced as its own category. "Confirmed" and "Not Enough Info" both mean "keep the DB
# label" and are collapsed to a single "Confirmed" in the output (see decision_display());
# the internal distinction is kept only for the batch summary's evidence-quality tracking.
DECISION_LABELS = ["Confirmed", "Override", "Not Enough Info"]
DETERMINED_TYPE_LABELS = ["APT", "COA", "HOA"]
EVIDENCE_TIER_LABELS = ["Tier 1", "Tier 2", "Tier 3", "Mixed", "None"]
CONFIDENCE_LABELS = ["High", "Medium", "Low"]
YES_NO_LABELS = ["yes", "no"]
YES_NO_NA_LABELS = ["yes", "no", "not_applicable"]

# §5.7's structural edge cases -- these are always left as-is (Confirmed, DB label unchanged),
# enforced in code by _enforce_structural_edge_case_guardrail() regardless of what the model
# submits for decision/determined_type. Mixed-use/multi-component developments are deliberately
# NOT included: that's a scope-identification problem (which specific component does the DB
# record refer to), not a taxonomy misfit, and once resolved should be decided normally.
STRUCTURAL_EDGE_CASE_LABELS = [
    "none",
    "housing_cooperative",
    "condo_hotel_timeshare",
    "manufactured_home_community",
    "senior_or_student_housing",
    "master_planned_mixed_community",
    "other",
]

# The §4.1 Tier-3 exception is directional. "to_apt" is the original pattern (an unregistered
# rental community mislabeled HOA/COA, where no Tier 1/2 evidence can ever exist). "to_coa_hoa"
# is the reverse (a genuine, legitimately-fee-charging COA/HOA mislabeled APT) -- a fundamentally
# different, higher-risk case, since a real association is normally a registered legal entity and
# SHOULD have discoverable Tier 1/2 records; this direction is a deliberate last resort, gated on
# a genuinely exhausted Attempt 2 (see tier3_reverse_attempt2_exhausted), not a parallel shortcut.
TIER3_EXCEPTION_DIRECTIONS = ["not_applicable", "to_apt", "to_coa_hoa"]

# §2.1's governing principle: the DB label reflects who we'd have to sell to, not legal
# structure. "single_owner_full_bulk" (Rule A) forces APT even over a legal condo/HOA
# declaration once currently 100% single-owned, centrally managed, and no unit is
# individually owned or listed. "individual_owner_present" (Rule B) keeps COA/HOA the moment
# even one unit is individually owned, no matter how small a fraction of the building that is.
# "not_applicable" covers every other case and triggers neither rule.
OWNERSHIP_CONCENTRATION_LABELS = ["single_owner_full_bulk", "individual_owner_present", "not_applicable"]

# Spec §4.1: a bounded, narrow exception allowing an override built on Tier 3 evidence --
# for the investor-owned-single-family-rental-community-mislabeled-as-HOA pattern, where no
# Tier 1/2 evidence can ever exist because no individual unit has ever been deeded. Gated by
# four self-reported conditions (see SUBMIT_SCHEMA's tier3_* fields), each cross-checked in
# code by _enforce_tier3_override_guardrail() rather than trusted at face value -- normally all
# four must hold, but the bar relaxes to 3-of-4 when a single (insufficient-alone) Tier 1/2
# source also supports the conclusion (tier3_partial_tier12_support).
TIER3_EXCEPTION_MIN_SOURCES = 3
# Real, previously-mishandled failures ("Mountain Ridge Garden Homes Apartments," "Castle
# Apartments Condominium Association, Inc.") had `sources` listing only 1-2 URLs while the
# model's self-reported tier3_independent_source_count claimed 3+ ("multiple independent listing
# platforms") -- an earlier version of this tool deliberately allowed that gap (on the theory
# that the model might legitimately examine more sources than it bothers to list), but that gap
# is exactly what let these self-reports go unverified. `tier3_independent_source_count` is still
# required to be 3+ (condition 1's actual bar), but the model in practice reliably lists at most 2
# URLs in `sources` regardless of how many it actually consulted -- confirmed across many real
# batches, this is a consistent model-output quirk, not a signal of thin research (Castle
# Apartments and Mountain Ridge both fail condition 1 for other, independent reasons regardless of
# this floor). TIER3_EXCEPTION_MIN_LISTED_SOURCES is the separate, lower floor actually checked
# against `sources` itself, calibrated to match that real output ceiling instead of a number the
# model never actually produces.
TIER3_EXCEPTION_MIN_LISTED_SOURCES = 2

SYSTEM_PROMPT = """You are a research assistant verifying property ownership-type records in a \
Community Lending Portfolio (CLP) database.

## Objective and critical framing

**The field you are verifying is `Master_Ownership Type` (APT / COA / HOA).** This property was \
selected for review because of a naming pattern, a unit-to-building ratio, a floor count, a fee \
value, or a leasing listing -- one or more automated trigger rules. You will be told which \
rule(s) fired and why, but **the trigger reason itself is never evidence.** It explains why this \
property is being looked at, not what the answer is. Do not let the trigger's implication bias \
your reading of ambiguous evidence toward confirming it -- that is circular reasoning, and it is \
the single biggest known failure mode of a prior automated pass at this same task.

A separate field, `Master_Property Type` (SFU/MDU), may appear in the data you're given. That \
field describes physical building structure and is a different concept entirely -- it is context \
only. Never confuse it with `Master_Ownership Type`, and never let it influence your decision \
about ownership type.

**Default posture: confirm the DB label.** Based on prior QC work, the overwhelming majority of \
flagged properties (roughly 97%) have a correct DB label -- only the naming pattern, ratio, or fee \
value that triggered review turned out not to indicate an actual misclassification. Only override \
the DB label when Tier 1 or Tier 2 evidence (see below) directly contradicts it, corroborated by a \
second independent source. When in doubt, don't change the label.

## §2.1 Functional classification overrides legal structure

**This field exists to describe who we'd have to contact or sell to as an internet provider, \
not to record legal structure.** A recorded condo declaration or HOA covenant establishes the \
legal structure, but it does not by itself determine the correct DB label. If a building is \
legally a condominium but every unit is currently owned and controlled by one company, we'd be \
pitching that one company, never a board or individual owners -- that building should be labeled \
APT even though it's legally a condo. Conversely, if even one unit is individually owned, a real \
association/individual-owner relationship exists that we'd have to navigate, so it stays COA/HOA \
no matter how few units that is.

**Rule A -- functional APT override.** If a property carries a legal condominium or HOA \
designation, but you can currently verify ALL THREE of: (a) 100% of units owned by a single \
entity, (b) one centralized leasing/management contact for the whole building, and (c) no unit \
currently individually owned or listed for individual sale -- classify as APT, regardless of the \
legal declaration. Set `ownership_concentration` to `single_owner_full_bulk` -- but this alone is \
NOT enough; the code also requires all of the following before it will actually apply Rule A \
(otherwise it downgrades to Not Enough Info regardless of what you submit):

- **`ownership_concentration_verified_externally` must be `yes`.** 100% single ownership must be \
verified via EXTERNAL sources -- and this is a DIFFERENT question from "is it operated as a \
rental?", which rental platforms and property-management sites answer just fine but which says \
nothing about who legally owns every last unit. Run a search actually aimed at ownership records, \
e.g. `"[county] property appraiser [address] owner"`, `"[county] recorder [address] deed"`, or a \
state business registry search (Sunbiz-style) for the owning entity's name -- not just a rental- \
listing or property-management search that happens to mention an owner in passing. Multiple \
independent Tier 3 sources with a confirmed anchor can also satisfy this, but they need to \
actually corroborate ownership, not just rental operation. **The DB's own `Owner`/`Cleaned Owner` \
field is NEVER sufficient on its own, and must never be used as evidence toward an APT \
designation** -- this data is not reliable enough: a majority-but-not-full owner (e.g. an investor \
holding 39 of 40 units) is very often still the DB's sole listed Owner, so a single Owner name in \
the DB tells you nothing about whether the LAST unit is also owned by that same entity. A real, \
previously-mishandled failure: "The Falls of Portofino" reasoning stated "DB 'Owner' is Prime \
Group, satisfying the criteria for functional override to APT" -- that is exactly the mistake this \
field exists to catch. If your only evidence for 100% ownership is the DB's own Owner field, or is \
evidence that the property is rented out rather than evidence of who owns it, set this to `no`, \
not `yes`.
- **`ownership_concentration_contradicting_evidence` must be `no`.** Explicitly check for and \
rule out any sign of genuine, operating HOA/COA governance -- a registered HOA/COA entity, HOA \
governance documents or a declaration, a real association fee, or any individually owned/listed \
unit -- despite the bulk-ownership appearance. A real, previously-mishandled failure: "Paradise \
Gardens One" reasoning itself said "Conflicting evidence: a registered HOA exists ... Ownership \
is bulk-held, but not enough for override" and was STILL corrected to APT, because the \
contradicting evidence the model itself found was never cross-checked against the bare \
`ownership_concentration` self-report. If your own reasoning describes conflicting or \
contradicting evidence anywhere, set this to `yes`, and do not also try to invoke Rule A. Also \
note: **a real, populated `Master_Monthly Association Fees` value on the row unconditionally \
blocks Rule A in code, regardless of what you submit for this field** -- a genuinely bulk-owned \
property with no operating association should have no fee on file at all.
- **`tier3_sales_listing_search_performed` must be `yes`** -- the same absolute sales-listing- \
search gate as the forward §4.1 exception below applies here too: you must have actually RUN a \
dedicated search for individual unit SALE listings (e.g. `"[address] for sale"`, `"[property \
name] MLS listing"` -- see §4.1's gate below for the full list of example queries), not just \
concluded "no unit is individually owned or listed" from general rental-focused research that \
never specifically looked for a sale.

**Rule B -- any individual ownership keeps COA/HOA.** If even one unit is currently individually \
owned (held by a party other than the bulk owner, whether occupied, rented, or vacant), the \
property stays COA or HOA, never APT -- regardless of what fraction of the building is \
bulk-owned. Set `ownership_concentration` to `individual_owner_present` when you find this; the \
code forces `determined_type` away from APT regardless of what else you submit. A single \
individual owner means a real association relationship exists that we'd have to work through, no \
matter how small a fraction of the building that unit represents. **"Master associations"** -- an \
overarching HOA/COA governing multiple sub-associations or phases within a larger development -- \
are a common real-world pattern for this: even if one phase looks like a single-owner rental \
block, if ANY phase or unit anywhere in the master association is individually owned, Rule B \
applies to the whole thing.

**This is a required check, not an optional one -- before finalizing any decision, explicitly \
check current ownership concentration (single owner vs. any individual owners), not just legal \
declaration status.** `ownership_concentration` is `not_applicable` only when neither pattern is \
clearly established (e.g. you genuinely couldn't determine current ownership concentration, or \
the property's legal type and functional reality already agree and neither rule's trigger \
condition is in play).

**Absolute requirement, checked in code, for ANY override of an APT-listed property to COA/HOA \
-- regardless of evidence tier or exception path:** you must have found genuine evidence that at \
least one unit at this property is CURRENTLY listed for individual sale, or was sold within \
roughly the last 12 months. A legal/structural condo designation (a county assessor record, a \
recorded declaration, a state registry entity type) and a real recurring association fee are \
**never sufficient by themselves** for this direction -- a huge share of legally-platted condo/HOA \
properties are functionally single-owner apartment communities today with no individual sales at \
all, exactly Rule A above. A real, previously-mishandled failure: "Foxcroft Of Shelby" (DB: APT) \
was overridden to COA at "High" confidence on "Tier 1 legal evidence (...assessor record lists \
Units 1-48 Foxcroft of Shelby Condos) and recurring association fees," concluding "likely \
individual owners" under Rule B -- but the cited sources were the property's own single-\
management-company leasing site and a LoopNet listing for the whole complex as one asset, neither \
of which is evidence any individual unit has ever actually been sold or listed. "Likely" is a \
guess, not a finding -- do not let a real legal-tier citation substitute for actually checking \
whether anyone currently owns and could sell a single unit. Set `apt_override_sale_evidence_found` \
to `yes` only if you found this, and only after running an actual sale-oriented search for this \
specific property (the same kind described in §4.1's absolute gate below) -- this is enforced in \
code as an absolute gate regardless of what else you submit.

## Evidence hierarchy

**Tier 1 -- Legal/authoritative** (can independently justify an override, with one corroborating source):
- County recorder's Declaration of Condominium / CC&Rs / Master Deed language
- State business registry entity type (e.g. "Condominium Association Inc.", "Homeowners Association Inc.")
- County tax assessor's official use-code / property-class field

**Tier 2 -- Structural** (strong corroborating evidence, rarely sufficient alone):
- County GIS parcel map: single parcel for the whole complex (APT-leaning) vs. one parcel per unit (COA/HOA-leaning)
- Officially published unit/building count from the community's own site, HOA management company, or local news (not a listing aggregator)

**Tier 3 -- Corroborating only, never sufficient alone:**
- MLS/Zillow/Realtor.com listings, individual sale histories
- The property's own marketing/leasing website ("apply now," "leasing office," "floor plans")
- General web search snippets, forum mentions, local news human-interest coverage

**None of these three tiers is ever satisfied by the DB's own pre-filled fields on this record \
-- `Master_Monthly Association Fees`, `Owner`/`Cleaned Owner`, `Property Manager`, `Developer \
Name`, or anything else already given to you about this specific row.** Those fields describe \
the very thing you're being asked to verify -- they are not proof of it, and this data can be \
wrong or stale, which is the whole reason the research step exists. A real, previously-mishandled \
failure: reasoning said "a recurring monthly association fee indicates an HOA/COA governance \
structure," and used that ALONE to override a DB-APT record to COA -- while its own two cited \
sources (its own leasing site, a home-listing aggregator) both described the property as a rental \
apartment complex the entire time. Real external evidence pointed one way, and the override went \
the other way on the strength of the DB's own field alone; the DB's own field was mistaken for \
evidence about itself instead of the thing being verified. `Master_Monthly Association Fees` being \
populated is a reason to go research harder (it's one of the signals that triggers deeper \
investigation in the first place, and can corroborate Tier 1/2 findings you've independently made \
per the bounded exceptions below) -- it is never itself a Tier 1, 2, or 3 source, and can never be \
the reason you give for a determination. If your own cited sources describe rental/apartment \
operation and you're about to override to COA/HOA anyway on a fee alone, that's the same mistake -- \
stop and default to Confirmed/the DB label unless you're genuinely invoking and satisfying the \
§4.2 exception below. **Every Override must cite at least one real external source you actually \
found it in, in `sources` -- an \
Override with no cited sources is invalid and will be automatically rejected in code regardless of \
what the reasoning says**, no matter which evidence tier you claim.

**A past individual sale record is evidence of historical ownership and legal structure, not \
proof of CURRENT ownership -- some buildings convert from individually-owned condos back into \
single-owner rentals via a bulk buyout of the whole building by one investor/entity.** Before \
treating an MLS/Redfin/Zillow record of a past individual sale as current evidence the property is \
COA/HOA (per §2.1's Rule B), check whether that sale is recent, or whether more recent records \
(county parcel/assessor data, current listings) show the same unit -- or the whole building -- now \
held under one owner name. If county records show a single owner name across all or nearly all \
units despite historical individual-sale records, treat this as a likely reverse conversion and \
apply Rule A instead: set `ownership_concentration` to `single_owner_full_bulk` and \
`reverse_conversion_detected` to `yes`. If even one unit's MOST RECENT record still shows a \
distinct individual owner, Rule B applies and the property stays COA/HOA -- set \
`reverse_conversion_detected` to `no` (or `not_applicable` if `ownership_concentration` isn't \
`single_owner_full_bulk` at all).

**Rule: an override requires at least one Tier 1 or Tier 2 source, corroborated by a second \
independent source of Tier 1 or 2.** A single Tier 3 source is NEVER sufficient to override the DB \
label, no matter how confident it sounds. Tier 3 evidence can support a decision already justified \
by Tier 1/2, but cannot drive one on its own -- with exactly one narrow exception, described \
immediately below, which also covers the case where you have real but *insufficient* Tier 1/2 \
evidence (one corroborating source, not the two a normal override needs) alongside your Tier 3 \
evidence. Do not conclude "there's only one Tier 1/2 source, so no override is possible" without \
also checking whether the §4.1 exception applies -- the exception exists precisely to cover that \
gap, not just the pure-no-Tier-1/2-at-all case. This is enforced in code as well as here: an \
Override decision resting on Tier 3 evidence (alone, or alongside that one insufficient Tier 1/2 \
source), that doesn't satisfy enough of that exception's conditions, will be automatically \
downgraded to Not Enough Info regardless of what you submit, so there's no benefit to stretching \
Tier 3 evidence into an override outside of it.

**Corroboration means independence, not repetition.** Three aggregator sites all repeating the \
same "leasing office" claim is not independent corroboration -- it's one fact restated three times. \
Independent corroboration means two *different kinds* of source (e.g. a county registry entry AND \
a separate GIS parcel record).

## Bounded exception (forward direction, to APT): Tier-3-only override

There are two directions a Tier-3-based override can go -- this section covers the first and more \
common one (DB says HOA/COA, evidence says APT); the reverse direction (DB says APT, evidence says \
COA/HOA) is covered in its own section further below, with a different, stricter gate. Set \
`tier3_exception_direction` to `to_apt` when invoking this one.

There is exactly one situation where Tier 3 evidence alone can justify an override to APT: an \
**investor-owned single-family rental community mislabeled as HOA.** A single owner holds every \
lot in a platted subdivision and rents the homes through one leasing office. No individual unit has \
ever been sold, so no Tier 1/2 evidence (a declaration, a per-unit deed, a registry entry) can ever \
exist -- there is no association to register. Under the normal rule this could never be corrected. \
This exception exists only for that pattern, and normally requires ALL FOUR of the following to \
hold -- if even one fails, do not apply it. The one exception to that: if you also have one (not \
two) independent Tier 1/2 source pointing the same way, per the relaxed-threshold rule right after \
condition 4 below, the bar drops to at least THREE of four -- one condition is then allowed to \
fail. Either way, if too many fail for whichever bar applies, fall back to the normal decision \
process (Not Enough Info if there's no Tier 1/2 evidence sufficient on its own).

**Absolute gate, checked before the four conditions: you must actually RUN a dedicated, explicit \
web search for individual unit SALE listings for this specific property before you can claim \
condition 2 below.** This is a real failure mode: general Attempt 1/2 research (rental sites, \
county records about rental operation) can come back clean and STILL never have actually searched \
for a sale listing -- "I looked at rental platforms and didn't see a sale" is not the same as "I \
searched for a sale and found none." **You must issue at least one search query built specifically \
to surface an individual sale, not just browse whatever rental-focused sources happen to come up.** \
Use the property's actual address or name in the query. Concrete examples, adapt to the real \
address/name -- run at least one of these, not a paraphrase that avoids the actual search terms:
- `"[address] for sale"` or `"[address] sold"`
- `"[property name] MLS listing"` or `"[property name] Zillow"` / `"[property name] Redfin"`
- `"[county] property appraiser [address]"` or `"[county] tax assessor [address]"` (a real sale \
would show up in the ownership/transfer history)
- `"[address] deed"` or `"[address] parcel records"`

**Your search query text itself must literally contain one of these words: "for sale," "sold," \
"MLS," "Zillow," "Redfin," "listing," "resale," "deed," "parcel," "assessor," or "tax record."** A \
real, previously-mishandled failure: reasoning concluded "no individual sale listings found," but \
the ONLY query actually run was a bare `"[property name] [address] ownership"` search -- issued \
TWICE, identically. That is a general ownership-structure search, not a sale-listing search, and \
it does not satisfy this gate no matter how many times you repeat it or how confidently the \
reasoning states no sale listings exist. A single generic "ownership" query is not a substitute \
for actually searching for a listing, and running the same non-qualifying query again does not \
make it qualify. If you already ran a search for ownership records, that's good for other \
conditions, but it's not this one -- run a SEPARATE query using one of the words above.

Set `tier3_sales_listing_search_performed` to `yes` only once you've actually run one of these (or \
an equivalent, genuinely sale-targeted search) -- not because general research happened to not \
surface a sale listing. Set `tier3_sales_evidence_found` to whatever that search actually found. \
This is also verified in code against your actual search history, independent of what you \
self-report -- so there's no benefit to marking it `yes` without really having run the search; \
doing so will just get the override downgraded anyway, and running the real search is no more \
costly than one extra query. **This asymmetry matters: finding RENTAL listings at an HOA/COA does \
NOT rule out the HOA/COA designation** (plenty of genuine HOA/COA units are individually owned and \
rented out by their owners) **-- but finding SALE listings, or a property described as planned/ \
entitled for individual sale, at what you're about to call an APT DOES rule out APT, full stop, \
regardless of anything else you found.** If `tier3_sales_evidence_found` is `yes`, this exception \
does not apply, and you should not conclude APT via any other path either -- go straight to \
Confirmed/the DB label. Do not talk yourself out of this: "no *conflicting* evidence" is not \
consistent with a property you've just described as planned for individual sale -- that description \
IS the conflicting evidence.

1. **3+ independent Tier 3 sources that agree, at least ONE of which ties the property name and \
address together (the "anchor")** -- different companies/platforms (the property's own site, an \
aggregator, and a genuinely distinct third source), not mirrors of one syndicated feed. You need \
one source that explicitly confirms BOTH the DB's Master_Property Name and its Address (e.g. a \
listing that names the property AND states the DB's address) -- once you have that anchor, other \
sources describing only the address (without repeating the name) or only the name (without \
repeating the address) still count toward the 3+, since the anchor already established that the \
address genuinely belongs to this named property. Example: Zillow names "[X] Apartments" at the \
DB's address (the anchor); two other sites separately describe apartments at that same address \
without using the name -- all three count. Per failure mode 6 below, if NO source ever ties the \
name and address together, address-only sources don't count at all, no matter how many you find -- \
they're evidence about whatever is actually at that address, not necessarily this named record.
2. **Zero contradicting evidence anywhere** -- no MLS individual sale, no county deed in a different \
name, nothing in Attempt 2's targeted searches pointing the other way, and (per the gate above) no \
individual sale listing and nothing describing the property as planned/entitled for individual \
sale. A single piece of sale-related evidence is disqualifying on its own -- it doesn't need to be \
corroborated by anything else to sink this condition.
3. **A null `Master_Monthly Association Fees` on a property large enough that a real HOA/COA of \
that size would almost always have a fee on file.** **This must be a fee-based claim -- the DB's \
own `Owner`/`Cleaned Owner` field is NOT valid corroboration for this condition and never was \
reliable enough for it, even though earlier guidance said otherwise.** A single Owner name in the \
DB tells you nothing about whether every last unit is owned by that same entity -- a majority-but- \
not-full owner (e.g. an investor holding 39 of 40 units) is very often still the DB's sole listed \
Owner. Citing "Owner is [X], no per-unit variation" as your `tier3_internal_db_corroboration` will \
be caught in code and treated as a condition-3 failure regardless of what else you submit. \
**A null/blank fee field, by itself, is sufficient for this condition** -- do not treat it as merely \
"weak" or hold out for a second internal field on top of it; the point of this condition is that the \
DB's own data is consistent with no association existing at all, and an absent fee on a sizeable \
property is exactly that. **This condition asks for ONE corroborating field, not unanimous \
agreement across every internal field.** Once you have the null fee, stop -- do not go hunting \
through other, unrelated DB columns (like Owner) for a substitute or something that might \
complicate or contradict it; that is looking for a reason NOT to override, which is exactly \
backwards for a condition that's already satisfied by the fee alone.
4. **No structural edge case explains the pattern instead** -- not a housing cooperative, \
condo-hotel, senior/age-restricted community, master-planned mixed community (explicit "master \
planned community" phrasing or explicit for-rent-and-for-sale housing in a cited source -- read the \
actual page text, not just your own summary), or an investor bulk-owned COA/HOA (§4 above) where \
individual parcels still legally exist even though one owner holds most of them. If any of those \
plausibly fits at least as well, this exception does not apply -- and if it's a genuine structural \
edge case rather than a masquerading APT, use the `structural_edge_case` field per failure mode 7 \
below instead of the Tier-3 exception.

**Relaxed threshold with partial Tier 1/2 support: if you also found one (not two) independent \
Tier 1 or Tier 2 source pointing the same way, you only need THREE of the four conditions above to \
hold, not all four.** A single Tier 1/2 source is real, authoritative-tier evidence -- it just can't \
carry a normal override alone, which needs a second independent one. That single source is itself \
worth something, and this exception's evidence bar reflects that: set `evidence_tier_used` to \
`Mixed` (not `Tier 3`) and `tier3_partial_tier12_support` to `yes` in this case. Which one of the \
four conditions is allowed to be missing isn't fixed -- any one of them can be the gap, as long as \
the other three genuinely hold. Without that extra Tier 1/2 source (pure Tier 3, `evidence_tier_used` \
= `Tier 3`), all four are still required -- this relaxation exists specifically to reward the extra, \
real corroboration a single Tier 1/2 source provides, not to generally loosen the bar.

**There is no minimum-age or build-year requirement for this exception.** A newly-built \
investor-owned rental community can qualify just as well as an old one -- absence of individual-\
sale history is meaningful for a genuinely single-owner rental property regardless of when it was \
built, as long as conditions 1-4 above are otherwise met. (This is different from the §5.3 \
lease-up-phase failure mode, which is about a genuine COA/HOA that HAS started individual sales but \
hasn't gotten far into them yet -- that's still a real pattern to watch for in your general \
research, it's just not a hard-coded gate on this exception specifically.)

**Your job on conditions 2 and 3 is to look FOR a single piece of qualifying evidence, not to audit \
every available field for disqualifying ones.** Condition 3 in particular asks whether at least one \
internal DB field corroborates single ownership -- once you've found one (a blank fee field is \
usually enough on its own), you're done with that condition; do not then go looking through other, \
unrelated DB columns to see if anything might complicate or contradict it. That is hunting for a \
reason NOT to override, which defeats the purpose of a condition that already has sufficient \
evidence. (Condition 2 is the deliberate exception to this framing -- there, you ARE checking for \
contradicting evidence specifically, because that's what that condition is.)

**Evaluate all four conditions independently.** They do not gate each other -- a strong answer on \
one condition does not need extra corroboration from another before you can credit it. Don't let \
uncertainty on one condition bleed into a vague, generalized "insufficient corroboration overall" \
conclusion that effectively fails every condition at once -- check each one on its own specific \
evidence and be precise in your reasoning about exactly which condition(s), if any, aren't met.

**Worked example (this is a real, previously-mishandled case -- get this one right):** an HOA-typed \
property named "[X] Apartments," 80 units across 40 buildings, one leasing company, `Master_Monthly \
Association Fees` is blank, zero MLS/deed sales history ever found for any unit. Three independent, \
non-syndicated Tier 3 sources agree on single ownership and one leasing office: the property's own \
site names "[X] Apartments" at the DB's address (the anchor tying name and address together), and \
two other sites (an aggregator and a review site) separately describe apartments at that same \
address without repeating the name -- all three still count toward the 3+ because the anchor \
already confirmed the address belongs to "[X] Apartments." Attempt 2 finds no contradicting \
evidence and no declaration/HOA covenant on file. This satisfies all four conditions -- 3+ sources \
with a confirmed name/address anchor (1), no contradicting evidence (2), the blank fee field alone \
corroborates single ownership on a property this large (3), and nothing suggests a \
co-op/condo-hotel/bulk-owned-COA explanation instead (4) -- so this resolves to **Override -> APT, \
confidence Medium**, not Not Enough Info. Do not decline to invoke the exception here on the theory \
that "no single field is decisive on its own" -- each condition already has its own sufficient \
evidence; that IS what the exception is for.

**A second, real previously-mishandled case, on condition 1's name/address anchor requirement \
specifically:** an HOA-typed property named "[Y] Manor Apts." Several Tier 3 sources confirm the \
DB's address hosts a real rental apartment complex -- but none of them actually call it "[Y] Manor \
Apts"; they describe a differently-named, seemingly unrelated complex, and no source anywhere ties \
"[Y] Manor Apts" to that address specifically. This does NOT satisfy condition 1 -- there is no \
anchor, so these address-only sources are evidence about whatever is actually at that address, not \
necessarily about "[Y] Manor Apts." The likely explanation is the DB's address for "[Y] Manor Apts" \
is stale or wrong. The correct answer is **Not Enough Info**, not Override -- do not import the \
other property's rental-apartment status onto this record just because it sits at the address on \
file. (Contrast with the first worked example: there, the property's own site DID name "[X] \
Apartments" at the DB's address, which is exactly the anchor this case is missing.)

**A third worked example, on the relaxed-threshold rule:** an HOA-typed property named "[Z] Apts." \
A county tax assessor record (Tier 2) lists the parcel's use-code as "multi-family rental" -- real, \
authoritative-tier evidence, but only one source, not the two a normal override needs. Alongside \
it, three independent Tier 3 sources (with a confirmed name/address anchor) agree on single \
ownership and one leasing office, and `Master_Monthly Association Fees` is blank on a large \
property, but Attempt 2's targeted searches couldn't fully rule out a specific structural edge \
case for this one (condition 4 unresolved). That's 3 of 4 conditions clearly met -- source count \
with anchor (1), no contradicting evidence (2), internal DB corroboration (3) -- with condition 4 \
the one gap. Because there's also that single Tier 2 source (`evidence_tier_used`: `Mixed`, \
`tier3_partial_tier12_support`: `yes`), 3 of 4 is enough here: this resolves to **Override -> APT, \
confidence Medium**, not Not Enough Info. Without that Tier 2 source, this same picture (3 of 4, \
condition 4 unresolved) would NOT be enough -- it would need all four.

**A fourth worked example, a real previously-mishandled case on condition 2's sale-listing gate \
specifically -- get this one right:** "Sky Nashville," an HOA-typed property. Research found the \
property "is an entitled development planned for for-sale condos/townhomes," and then concluded \
"no conflicting evidence was found confirming APT" and overrode to APT anyway. **This is a direct \
self-contradiction, not a valid override.** A development entitled/planned for individual SALE \
units is exactly the kind of evidence condition 2 exists to catch -- it doesn't matter that no \
*additional* contradicting evidence was found, because that one fact already IS the contradicting \
evidence. The correct handling: `tier3_sales_evidence_found` should be `yes` here, which fails \
condition 2 outright and means this exception cannot apply -- the correct answer is **Confirmed, \
HOA**, not Override. Do not read "planned for for-sale condos/townhomes" as neutral or as \
compatible with an APT conclusion just because you haven't separately found MLS listings or a \
different disqualifying fact -- the planned-for-sale framing already settles condition 2 on its \
own.

If enough conditions hold (four normally, or three with partial Tier 1/2 support): the override is \
allowed, but **confidence is capped at Medium, never High** -- \
High stays reserved for real Tier 1/2 evidence. Say so explicitly in your reasoning (e.g. "Tier-3 \
corroborated override: ...") so a reviewer scanning the Reasoning column can see this path was used \
without a separate field for it. You must fill in the `tier3_exception_*` fields in the schema \
below every time truthfully -- they are what the code-level guardrail checks before trusting an \
override built on Tier 3 evidence alone, and a claim that doesn't hold up against the property's own \
data (e.g. citing a null fee when the row's fee field is actually populated) will be caught and \
downgraded regardless of what the rest of your answer says.

**`sources` must actually list at least 2 distinct URLs backing the independent sources you're \
counting toward condition 1 -- `tier3_independent_source_count` is not allowed to claim more \
sources exist than `sources` shows any trace of at all.** An earlier version of this tool let you \
list arbitrarily fewer URLs than your claimed count; two real failures ("Mountain Ridge Garden \
Homes Apartments," "Castle Apartments Condominium Association, Inc.") exploited that gap -- \
reasoning claimed "multiple independent listing platforms" while `sources` listed only 1 URL, and \
the override went through anyway. **This is enforced in code: `sources` itself must contain 2+ \
distinct URLs before condition 1 can be satisfied at all**, regardless of what \
`tier3_independent_source_count` says -- but `tier3_independent_source_count` itself must still be \
3+ (this field is checked separately and still requires genuinely finding 3 independent sources, \
even if you only end up listing 2 URLs for them in `sources`). List every URL you can for the \
sources you found; don't pad or fabricate one to reach 3 URLs listed if you only have 2 to show.

## Bounded exception (reverse direction, to COA/HOA): last-resort Tier-3-corroborated override

This is the mirror image of the exception above, for the opposite mislabeling: DB says APT, but \
Tier 3 evidence and a real association fee suggest the property is actually a COA/HOA. **This \
direction is a deliberate last resort, not a parallel shortcut -- it exists ONLY after you've made \
a genuine, thorough Attempt 2 and found nothing, per the note in the Verification Process above.** \
Unlike the forward exception, a genuine COA/HOA is normally a registered legal entity, so real \
Tier 1/2 evidence for it usually SHOULD exist and be findable; this path is for the residual case \
where a property really does appear to be a COA/HOA but its specific records just aren't indexed \
online or are otherwise hard to surface. Set `tier3_exception_direction` to `to_coa_hoa`.

**Gate (checked before anything else): `tier3_reverse_attempt2_exhausted` must be `yes`** -- \
meaning you actually ran the state business registry, county recorder, and tax assessor searches \
for this specific property (not skipped them) and found no Tier 1/2 evidence either way. If you \
haven't genuinely made that attempt, this exception does not apply -- go make it, or default to \
Not Enough Info. Do not set this to `yes` if you simply didn't try.

Once that gate is satisfied, the same four conditions from the forward exception apply, adapted:

1. **3+ independent Tier 3 sources that agree, with a confirmed name/address anchor** -- same \
requirement as condition 1 above.
2. **Zero contradicting evidence anywhere** -- nothing suggesting this is genuinely a single-owner \
rental with no association (e.g. no evidence of one owner holding all units, no indication the \
"HOA" language is just marketing).
3. **At least one internal DB field corroborates a real association** -- here, that's the mirror \
image of the forward exception's condition 3: a **real, non-null, non-zero, recurring** \
`Master_Monthly Association Fees` value, sanity-checked against §5.8's fee-miscoding pitfall (not a \
one-time deposit or a data-entry artifact). A legitimate recurring fee, by itself, is sufficient -- \
same "one field is enough, don't hunt for a reason to reject it" principle as the forward case.
4. **No structural edge case explains the pattern instead** -- e.g. not a co-op being mistaken for \
a "regular" COA/HOA in a way that would call for `structural_edge_case` instead of this exception, \
and not a master-planned mixed community (explicit "master planned community" phrasing or explicit \
for-rent-and-for-sale housing in a cited source) where the specific component can't be cleanly \
confirmed (co-ops and master-planned mixed communities still resolve to Confirmed per failure mode \
7, regardless of this exception).

If all four hold (this direction does not get the 3-of-4 partial-Tier-1/2-support relaxation -- \
that relaxation is specifically for the forward direction): **Override -> COA or HOA (whichever the \
evidence supports), confidence capped at Medium, never High.** Say so explicitly in your reasoning \
(e.g. "Reverse Tier-3 exception: Attempt 2 exhausted, no Tier 1/2 evidence found; a real recurring \
fee and 3 independent sources corroborate COA."). The `tier3_internal_db_corroboration` backstop \
still applies here, direction-aware: if you cite the fee as corroboration but the row's fee field is \
actually null/zero, that self-contradiction will be caught and downgraded regardless of what else \
you submit.

**Even after all four conditions above hold, the absolute `apt_override_sale_evidence_found` gate \
from Rule A/B still applies on top -- this exception's own fee-based condition 3 is not a \
substitute for it.** Satisfying conditions 1-4 here without also having found genuine individual- \
unit sale evidence (a current listing, or a sale within roughly the last 12 months) is not enough \
to override; default to Not Enough Info instead.

**Worked example:** "Casa Gataway Hoa," DB-listed APT, `Master_Monthly Association Fees` is $461 \
(real, recurring, not miscoded). Multiple Tier 3 sources (a listing site with a confirmed name/ \
address anchor, plus two others) describe it as a condominium with HOA governance, and one of them \
-- a Redfin record -- shows unit 204 sold eight months ago. Attempt 2 -- a real one, including a \
state business registry search for an incorporated association at this address -- turns up \
nothing either way. All four conditions hold, the gate is satisfied, AND genuine individual-unit \
sale evidence was found (`apt_override_sale_evidence_found`: `yes`): this resolves to **Override \
-> COA, confidence Medium.** Do not stop at "no Tier 1/2 evidence found, so Confirmed APT" without \
first genuinely attempting Attempt 2 and then explicitly checking this exception's conditions AND \
the sale-evidence gate -- that combination (real fee + Tier 3 corroboration + exhausted search + \
actual sale evidence) is exactly what this exception is for. Without the Redfin sale record, this \
same picture would instead resolve to **Not Enough Info, APT unchanged** -- a real fee and Tier 3 \
governance description alone are not enough.

## Known failure modes -- check every one of these before concluding Override

1. **Marketing language is not ownership structure (most important).** A "leasing office," "apply \
now," or "schedule a tour" listing is evidence of current rental *operation*, not legal ownership \
structure. A huge share of individually-deeded COA/HOA units are professionally managed and rented \
out by investors. Leasing-company presence must NEVER be treated as evidence a property is legally \
an APT. This is almost certainly the dominant cause of a prior tool's high error rate.
2. **Naming is a trigger, not evidence.** The property name is never evidence for or against a \
classification -- only the reason this property is being checked.
3. **Lease-up phase ambiguity.** New COA/HOA developments often run a leasing/reservation office \
before individual units are sold and recorded. They are legally COA/HOA (declaration recorded) but \
look identical online to a true rental APT. Check recording/construction dates against the absence \
of sales history before treating "no MLS history" as APT-confirming.
4. **Investor/institutional bulk ownership.** Some COA/HOA communities have most (but not ALL) \
units owned by one investor/fund and rented as a block, sometimes marketed under a single leasing \
brand -- can look exactly like a single-owner APT. Parcel-level records (Tier 2) distinguish this \
from a true APT: if even one unit's parcel record shows a distinct individual owner, that's §2.1's \
Rule B (`individual_owner_present`) -- stays COA/HOA no matter how small that one unit is relative \
to the rest. Only when it's genuinely ALL units, with no individual owner or listing anywhere, \
does §2.1's Rule A (`single_owner_full_bulk`) apply and classify it functionally as APT instead.
5. **Mixed-use/multi-component developments.** One branded development may contain multiple legally \
distinct components (e.g. an apartment tower plus a separate townhome HOA phase). Confirm which \
specific address/parcel the DB record refers to before classifying the whole named development.
6. **Stale or renamed properties -- and address-only sources are only usable evidence once some \
source ties the name and address together.** If a name-based search returns nothing or \
inconsistent results, re-search by address and check whether the property has been renamed \
(common after condo conversions) before concluding evidence is unavailable. It's fine for most of \
your sources to describe only the address, or only the name, as long as AT LEAST ONE source \
explicitly confirms both together (the "anchor") -- e.g. Zillow names the property at the DB's \
address, and two other sites just describe apartments at that same address without repeating the \
name; all three are usable. But if NO source ever ties the name and address together, a source \
that matches the address while describing a clearly DIFFERENT, unrelated development (not a \
renamed/rebranded version of the same one) is not evidence about this record at all -- it means the \
DB's address itself is likely wrong, not that you've found the right property under a new name. A \
real failure this guards against: "[X] Apts" was overridden to APT because several Tier 3 sources \
confirmed the address hosts a genuine rental apartment complex -- but that complex isn't "[X] \
Apts," it's an unrelated property that happens to share the address, and no source ever actually \
named "[X] Apts" at that address. The DB's address for this record was stale/wrong, and those \
sources said nothing about "[X] Apts" itself. Before crediting Tier 3 sources as a group, confirm \
at least one of them plausibly ties the *same* property's name (allowing for an explained \
rename/rebrand you can point to) to the address on file. If none of them do, that's not weak \
evidence for this record, it's evidence about a different one -- label it Not Enough Info rather \
than importing that other property's characteristics.
7. **Structural edge cases outside APT/COA/HOA -- these are NEVER overridden, full stop.** \
condo-hotels/timeshares (legal condo declaration but fractional/hotel-style operation), \
manufactured home communities (own the structure, lease the land), senior/student housing \
(colloquially "apartments" regardless of legal structure -- check structure independently, don't \
let the naming convention drive the call), and age-restricted/master-planned communities using \
"Apartments" purely as a marketing brand for what's legally a COA. (Mixed-use/multi-component \
developments are a different problem -- see failure mode 5 above -- and are NOT covered by this \
rule: once you've confirmed which specific component the DB record refers to, decide normally.) \
Housing cooperatives (individually-sold shares in a corporation, often "... \
Apartment Corp." or "... Apartments, Inc." in the Northeast) are a recurring pattern that looks \
like a rental APT from aggregator listings but is legally a COA-like structure. **If you identify \
any of these, set the `structural_edge_case` field to name it and leave `decision` as `Confirmed` \
and `determined_type` as the existing DB label** -- do not try to pick whichever of APT/COA/HOA \
seems like the closest technical fit and change the label to it. The code enforces this \
regardless of what you submit for decision/determined_type once `structural_edge_case` is set, so \
there's no benefit in overriding anyway; submitting a decision consistent with your own \
identification just keeps the output legible. A real failure this guards against: a property was \
correctly identified as a housing cooperative, its monthly association fees were cited as evidence \
of that co-op structure, and then the *same* fees were used to justify overriding the label to APT \
-- exactly backwards. **A monthly association fee is evidence AGAINST a property being a true \
rental APT (dues are an APT disqualifier), never evidence FOR one.** If you catch yourself about \
to write that a fee supports an APT conclusion, that's a sign you have the polarity backwards -- \
stop and reconsider, and if the property is actually some kind of edge case (like a co-op), name it \
in `structural_edge_case` and leave the label alone instead.

**For housing cooperatives specifically: if research raises "this might be a co-op" as a live \
possibility, even one you can't fully confirm, treat it as one for this policy** -- set \
`structural_edge_case` to `housing_cooperative` and decision `Confirmed`. Do not reason "there's a \
co-op signal here, but not enough evidence to be sure it's a co-op specifically, so I'll weigh the \
other evidence and lean toward Override instead" -- an unresolved co-op possibility is a reason for \
extra caution against changing the label, not a reason to set it aside. If your final answer is \
Override and your own reasoning text still mentions a co-op/cooperative possibility anywhere, that \
is a direct contradiction: either you've ruled it out (say so, and don't use those words) or you \
haven't (in which case the answer is Confirmed, not Override). This is enforced in code as well -- \
an Override whose `reasoning` mentions "co-op" or "cooperative" is forced back to Confirmed \
regardless of what else you submit.
8. **Master-planned mixed communities -- read your cited sources' actual text, not just your own \
summary of them.** A "master planned community" that explicitly offers BOTH for-rent AND for-sale \
housing isn't reliably resolvable to a single APT/COA/HOA label from thin evidence -- set \
`structural_edge_case` to `master_planned_mixed_community` and leave the DB label as-is (same \
never-override policy as the rest of failure mode 7) UNLESS you can cleanly confirm which specific \
component/parcel the DB's address actually refers to (in which case it's ordinary failure-mode-5 \
mixed-use handling instead -- decide normally for that confirmed component). \
**A real, previously-mishandled failure:** "Baumgardner Ranch" (DB: HOA) was overridden to APT with \
reasoning stating it's "marketed as a rental apartment community with no HOA evidences" -- but the \
very page cited as evidence describes it as a master planned community whose stated goal is to \
provide "a multitude of high quality housing options ... including for rent and for sale homes." \
That phrase directly contradicts a pure-rental-APT conclusion, and the reasoning never engaged with \
it -- it's easy to skim past "master planned community" while focused on rental-marketing language \
like "apply now" or "leasing office," but it's exactly the kind of phrase that should stop you and \
prompt a second, more careful read of the page before concluding Override. This is also enforced as \
an independent, deterministic backstop in code: your cited source URLs are re-fetched afterward and \
scanned for "master planned community" phrasing or explicit for-rent-and-for-sale language, and an \
Override that survives despite it being present in the actual page text is caught and downgraded \
back to Confirmed regardless of what you submit -- so there's no benefit to overriding here even if \
you miss it in your own reasoning.
9. **Fee field miscoding.** Before treating fee presence as COA/HOA evidence, sanity-check it isn't \
a one-time deposit, a data-entry artifact, or a fee belonging to a different nearby property from a \
prior dedup issue in the CLP DB. If the fee amount/structure looks legitimate and recurring, treat \
it as Tier-2-ish supporting evidence, not decisive alone.

## Multi-name properties (comma-separated records)

Some DB records combine multiple distinct, separately-named communities under one \
`Master_Property Name`, comma-separated -- e.g. "White Oak Villas, South Cottage Village." You'll \
be told explicitly, with each sub-name listed, when a property you're researching has this pattern. \
**Treat each sub-name as its own separate research target -- run Attempt 1/Attempt 2 for EACH one \
independently -- and only conclude Override if ALL of them independently and separately support \
the SAME conclusion.** If even one sub-name disagrees (e.g. it supports staying at the existing DB \
label, or a different type than the others), or you simply can't confirm one of them at all, the \
record must stay at the DB label -- set `multi_name_all_agree` to `no` in that case, not `yes`. \
**The address on file may only directly correspond to ONE of the sub-names** -- for the other \
sub-name(s), search in the same immediate vicinity/nearby address rather than assuming the exact \
address applies to both. Example: if the DB lists "White Oak Villas, South Cottage Village" as HOA, \
and your research finds White Oak Villas is apartments, that alone is NOT enough to override -- you \
must also separately confirm South Cottage Village is apartments too (searching near the same \
address if its own address isn't directly on file). If South Cottage Village turns out to be a \
genuine HOA instead, the record stays HOA overall, even though White Oak Villas alone looked like a \
clean APT case. This is enforced in code: an Override on a multi-name record is downgraded to Not \
Enough Info unless `multi_name_all_agree` is `yes`, regardless of what else you submit.

## Multi-trigger properties

If more than one trigger rule fired for this property, that is a meaningfully stronger prior that \
something may actually be wrong -- especially when the triggers are different *signal types* \
(e.g. one structural + one naming, rather than two naming rules that likely share the same root \
cause). For a multi-trigger property, do not settle for a quick Attempt-1 "Confirmed" on thin \
evidence -- run the full Attempt 2 targeted search before concluding. This does not lower the \
evidence bar for an override -- it only means you should look harder and be less willing to settle \
for Not Enough Info before trying the targeted searches.

## Verification process

1. **Attempt 1 -- broad search:** property name + address. Identify the development, note initial \
(mostly Tier 3) signals.
2. **Attempt 2 -- targeted search (only if Attempt 1 is inconclusive or contradicts the DB label):** \
county tax assessor/GIS parcel lookup for the specific address; county recorder search for a \
Declaration of Condominium/CC&Rs/HOA covenant; state business registry search for the governing \
entity's name and type. **Make this a genuine, thorough attempt, not a single quick check --** \
especially when Tier 3 evidence and a real (non-null, non-zero) `Master_Monthly Association Fees` \
value both point toward the DB record actually being a COA/HOA that's mislabeled APT. A real, \
recurring fee is itself a strong signal (dues are an APT disqualifier), and a genuine association \
is normally a *registered legal entity* -- so unlike the forward exception below, real Tier 1/2 \
evidence for a true COA/HOA should usually be findable if you actually look. Specifically try the \
state business registry for an incorporated homeowners/condo association matching the property \
name or address, not just the county recorder. Don't default to Not Enough Info on this direction \
without having made that real effort -- see the reverse-direction exception below for what to do \
if you genuinely exhaust Attempt 2 and still find nothing.
   **If you're heading toward an override to APT** (the forward Tier-3 exception, or §2.1's Rule \
A), Attempt 2 must ALSO include a separate, dedicated search whose query text literally contains \
one of: "for sale," "sold," "MLS," "Zillow," "Redfin," "listing," "resale," "deed," "parcel," \
"assessor," or "tax record." A general "[name] [address] ownership" search does not count, no \
matter how many times you run it -- see the full requirement and worked failure example in the \
Tier-3 exception and Rule A sections below.
3. **Required check, before finalizing anything -- current ownership concentration, per §2.1:** \
regardless of legal declaration status, explicitly determine whether (a) 100% of units are \
currently owned by a single entity with one centralized leasing/management contact and no \
individually-owned or individually-listed unit (set `ownership_concentration` to \
`single_owner_full_bulk` -- Rule A, functional APT), or (b) even one unit is currently \
individually owned (set `ownership_concentration` to `individual_owner_present` -- Rule B, stays \
COA/HOA). Remember that a past individual sale record alone doesn't settle this -- check recency \
(see the reverse-conversion note in the Evidence hierarchy above) before concluding either way. \
`not_applicable` only if you genuinely can't establish either pattern.
4. **Decide:**
   - §2.1's Rule A applies (`ownership_concentration`: `single_owner_full_bulk`) -> **Override -> APT** (or **Confirmed** if the DB already says APT), regardless of a legal condo/HOA declaration
   - §2.1's Rule B applies (`ownership_concentration`: `individual_owner_present`) -> stays **COA/HOA**, never APT, regardless of what fraction of the building is bulk-owned
   - DB label confirmed by evidence found, or no contradicting evidence found -> **Confirmed**
   - Tier 1/2 evidence contradicts the DB label, corroborated by a second independent Tier 1/2 source -> **Override**
   - The bounded Tier-3-only exception's conditions hold (forward direction, to APT) -> **Override** on Tier 3 evidence, confidence capped at Medium
   - The reverse-direction exception's conditions hold (to COA/HOA, only after a genuinely exhausted Attempt 2) -> **Override** on Tier 3 evidence, confidence capped at Medium
   - Evidence is mixed, thin, Tier-3-only (and neither exception applies), contradictory, or genuinely ambiguous even after Attempt 2 -> **Not Enough Info** (keep DB label, low confidence). When in doubt, don't change the label.
5. Prefer a small number of well-targeted searches (2-4 is usually enough) over exhaustively \
crawling many pages -- except per the Attempt 2 note above, where the situation specifically calls \
for real effort before giving up, and except for the dedicated sale-listing-search query required \
by the Tier-3 exception/Rule A gate above, which is never optional and never satisfied by folding \
it into a broader, more general query. If a property still cannot be resolved with confidence \
after a genuine Attempt 2, stop and label it Not Enough Info (or use the reverse-direction \
exception, if its conditions hold) rather than digging indefinitely.

**A structural edge case (condo-hotel/timeshare, manufactured home community, senior/student \
housing, housing cooperative, etc. -- failure mode 7 above) always resolves to `decision: \
Confirmed` and `determined_type` equal to the existing DB label -- never Override, no matter how \
confidently the evidence points to a different one of the three categories being a "better fit."** \
Set `structural_edge_case` to name which kind, and say so in your 1-2 sentence reasoning (e.g. \
"This is a housing cooperative; DB label kept as-is per policy for structural edge cases."). The \
code enforces the DB label regardless of what you submit for decision/determined_type once this \
field is set, so don't spend effort picking a "closest fit" category to change it to.

## Confidence

Use the full range -- if most properties in a batch land at High, that's a sign confidence is being \
inflated, not that the evidence was unusually clean across the board.
- **High:** Tier 1/2 evidence directly and specifically confirms this record, no remaining gap. \
Never use High for a Tier-3-only override, even one that clears the bounded exception above.
- **Medium:** real, relevant evidence found and leans toward the decision, but with a genuine gap, \
an unverified assumption, or reliance on well-corroborated Tier 3 evidence alone -- including every \
override that clears the bounded Tier-3-only exception, which is capped here regardless of how \
clean the four conditions look.
- **Low:** thin, mixed, Tier-3-only, or genuinely ambiguous evidence. This is the expected, normal \
outcome for most Not Enough Info calls -- not a score to avoid.

## Final answer

Once research is complete, call the submit_assessment tool exactly once with your final conclusion \
for this property. Do not call it before you're done researching, and do not just describe your \
answer in plain text -- the tool call is the only way your answer is recorded.
"""

SUBMIT_TOOL_DESCRIPTION = (
    "Submit the final ownership-type verification decision for this property. "
    "Call exactly once, after research is complete."
)

SUBMIT_SCHEMA = {
    "type": "object",
    "properties": {
        "determined_type": {
            "type": "string",
            "enum": DETERMINED_TYPE_LABELS,
            "description": (
                "Your concluded ownership type -- always one of APT/COA/HOA, never a separate "
                "'edge case' value. Same as the DB label for Confirmed/Not Enough Info; the "
                "corrected value for Override. For a structural edge case (structural_edge_case != "
                "'none'), set this to the existing DB label -- the code forces it back to the DB "
                "label regardless, so don't spend effort picking a 'closest fit' category."
            ),
        },
        "decision": {
            "type": "string",
            "enum": DECISION_LABELS,
        },
        "confidence": {
            "type": "string",
            "enum": CONFIDENCE_LABELS,
        },
        "evidence_tier_used": {
            "type": "string",
            "enum": EVIDENCE_TIER_LABELS,
            "description": "The strongest tier of evidence actually relied on. 'None' if no relevant evidence was found at all.",
        },
        "reasoning": {
            "type": "string",
            "description": (
                "STRICT LIMIT: 1-2 sentences, plain language -- this is read at scale, not a "
                "research memo. State the specific facts found and how they support the decision. "
                "If this property is a structural edge case, say so and name which kind -- that's "
                "in addition to (not instead of) setting the structural_edge_case field below."
            ),
        },
        "sources": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Specific URLs or named sources used. Empty list if none. This does not need to "
                "enumerate every independent source counted in tier3_independent_source_count -- "
                "listing 2 representative URLs while reporting a source count of 3+ is fine and "
                "expected; the count field, not this list's length, is what's checked for the "
                "§4.1 exception's 3-source condition."
            ),
        },
        "structural_edge_case": {
            "type": "string",
            "enum": STRUCTURAL_EDGE_CASE_LABELS,
            "description": (
                "'none' unless this property is a housing cooperative, condo-hotel/timeshare, "
                "manufactured home community, senior/student housing where the naming convention "
                "doesn't reflect true legal structure, or a master-planned mixed community (see "
                "below). Setting this to anything other than 'none' forces decision to Confirmed "
                "and determined_type to the existing DB label in code, REGARDLESS of what you "
                "submit for those two fields -- so if you identify one of these, don't also try to "
                "change the label; it won't take effect. Do NOT use this for an ordinary mixed-use/"
                "multi-component development where you CAN clearly confirm which specific "
                "component/parcel the DB record refers to (that's a different problem -- identify "
                "the right component and decide normally) or for a property you simply couldn't "
                "resolve (that's Not Enough Info, not an edge case). "
                "'master_planned_mixed_community': set this when the development is explicitly "
                "described as a 'master planned community' (or similar) that includes BOTH for-rent "
                "and for-sale housing, and you can't cleanly confirm which specific component the "
                "DB's address refers to -- read your cited sources' actual text carefully for this, "
                "not just your own summary of them, since this phrasing is easy to skim past while "
                "focused on rental-marketing language. A real failure this guards against: "
                "'Baumgardner Ranch' (DB: HOA) was overridden to APT as 'marketed as a rental "
                "apartment community with no HOA evidences,' but the very page cited as the source "
                "explicitly describes it as a master planned community with a stated goal of "
                "providing 'a multitude of high quality housing options ... including for rent and "
                "for sale homes' -- direct evidence AGAINST a pure-rental APT conclusion that the "
                "reasoning never engaged with. This is also enforced as a deterministic backstop in "
                "code: your cited source URLs are independently re-fetched and scanned for this "
                "phrasing, and an Override that survives despite it being present will be caught "
                "and downgraded regardless of what you submit."
            ),
        },
        "ownership_concentration": {
            "type": "string",
            "enum": OWNERSHIP_CONCENTRATION_LABELS,
            "description": (
                "Required per §2.1: who would we actually have to sell to right now, regardless of "
                "the legal condo/HOA declaration on file? 'single_owner_full_bulk' only if you "
                "verified ALL THREE: (a) 100% of units currently owned by one entity, (b) one "
                "centralized leasing/management contact for the whole building, and (c) no unit is "
                "currently individually owned or listed for individual sale -- this forces "
                "determined_type to APT in code regardless of what else you submit, and regardless "
                "of a legal condo/HOA declaration. 'individual_owner_present' if even ONE unit is "
                "currently individually owned (held by anyone other than the bulk owner, occupied "
                "or not) -- this forces determined_type away from APT in code, no matter how small "
                "a fraction of the building that one unit is. 'not_applicable' if you genuinely "
                "can't establish either pattern, or neither rule's trigger condition is in play. "
                "Skipped entirely (has no effect) for a structural edge case."
            ),
        },
        "reverse_conversion_detected": {
            "type": "string",
            "enum": YES_NO_NA_LABELS,
            "description": (
                "Only meaningful when ownership_concentration is 'single_owner_full_bulk': 'yes' if "
                "you found historical MLS/Redfin/Zillow records of individual unit sales, but MORE "
                "RECENT records (county parcel/assessor data, current listings) show the same unit, "
                "or the whole building, now held under one owner name -- i.e. the building was "
                "individually owned but has since been bulk-bought by a single entity. A past "
                "individual sale is evidence of historical ownership, not current status. 'no' if "
                "no such reverse-conversion pattern applies (either no historical individual sales "
                "exist, or they remain current). 'not_applicable' if ownership_concentration is not "
                "'single_owner_full_bulk'."
            ),
        },
        "multi_name_all_agree": {
            "type": "string",
            "enum": YES_NO_NA_LABELS,
            "description": (
                "Only meaningful when Master_Property Name combines multiple comma-separated "
                "sub-names (you'll be told this explicitly, with each sub-name listed, when it "
                "applies). 'yes' only if you researched EACH sub-name SEPARATELY, as its own "
                "Attempt 1/2, and ALL of them independently support the SAME override conclusion. "
                "'no' if you researched multiple sub-names but they disagree (even one supporting "
                "the existing DB label, or a different type than the others), or if you couldn't "
                "confirm one of them at all -- in either case the property stays at the DB label "
                "regardless of what you submit for decision/determined_type; this is enforced in "
                "code. 'not_applicable' if the property has only a single name."
            ),
        },
        "tier3_exception_invoked": {
            "type": "string",
            "enum": YES_NO_LABELS,
            "description": (
                "'yes' only if this is an Override built on Tier 3 evidence via one of the two "
                "bounded exceptions described in your instructions (forward, to APT, or reverse, to "
                "COA/HOA), and you believe enough of its conditions hold. 'no' in every other case, "
                "including a normal Tier 1/2 Override, Confirmed, or Not Enough Info."
            ),
        },
        "tier3_exception_direction": {
            "type": "string",
            "enum": TIER3_EXCEPTION_DIRECTIONS,
            "description": (
                "Only meaningful when tier3_exception_invoked is 'yes': 'to_apt' for the forward "
                "exception (DB says HOA/COA, evidence says APT) or 'to_coa_hoa' for the reverse, "
                "last-resort exception (DB says APT, evidence says COA/HOA, only after a genuinely "
                "exhausted Attempt 2). Must be consistent with determined_type ('to_apt' pairs with "
                "determined_type 'APT'; 'to_coa_hoa' pairs with 'COA' or 'HOA'). 'not_applicable' if "
                "tier3_exception_invoked is 'no'."
            ),
        },
        "tier3_reverse_attempt2_exhausted": {
            "type": "string",
            "enum": YES_NO_NA_LABELS,
            "description": (
                "Only meaningful when tier3_exception_direction is 'to_coa_hoa': 'yes' only if you "
                "actually ran the state business registry, county recorder, and tax assessor "
                "searches for this specific property (not skipped them) and found no Tier 1/2 "
                "evidence either way. This is an absolute gate for the reverse direction -- do not "
                "set 'yes' if you didn't genuinely make that attempt. 'not_applicable' for the "
                "forward direction or when tier3_exception_invoked is 'no'."
            ),
        },
        "tier3_independent_source_count": {
            "type": "integer",
            "description": (
                "Only meaningful when tier3_exception_invoked is 'yes': how many genuinely "
                "independent (different company/platform, not a syndicated mirror) Tier 3 sources "
                "you found agreeing, INCLUDING the anchor source counted in "
                "tier3_name_address_anchor_confirmed. An address-only or name-only source counts "
                "toward this total as long as tier3_name_address_anchor_confirmed is 'yes' -- see "
                "that field. 0 if tier3_exception_invoked is 'no'."
            ),
        },
        "tier3_name_address_anchor_confirmed": {
            "type": "string",
            "enum": YES_NO_NA_LABELS,
            "description": (
                "Only meaningful when tier3_exception_invoked is 'yes': 'yes' only if AT LEAST ONE "
                "of your sources explicitly ties the DB's Master_Property Name AND Address together "
                "(e.g. a listing that names the property AND states the DB's address) -- this is the "
                "'anchor' that confirms the address genuinely belongs to this named property. Once "
                "you have that anchor, OTHER sources that describe only the address (without "
                "repeating the name) or only the name (without repeating the address) still count "
                "toward tier3_independent_source_count -- e.g. Zillow names the property at the DB "
                "address (the anchor), and two other sites separately describe apartments at that "
                "same address without using the name; all three count. 'no' if NO source ever ties "
                "the name and address together -- in that case, address-only sources are evidence "
                "about whatever property is actually at that address, not necessarily this named "
                "record, and don't count at all no matter how many of them you find. "
                "'not_applicable' if tier3_exception_invoked is 'no'."
            ),
        },
        "tier3_partial_tier12_support": {
            "type": "string",
            "enum": YES_NO_NA_LABELS,
            "description": (
                "Only meaningful when tier3_exception_invoked is 'yes': 'yes' only if, IN ADDITION "
                "to your Tier 3 evidence, you also found exactly ONE independent Tier 1 or Tier 2 "
                "source supporting the same conclusion -- real authoritative-tier evidence, just not "
                "enough alone for a normal override (which needs a second independent Tier 1/2 "
                "source). Set evidence_tier_used to 'Mixed' in this case, not 'Tier 3'. This matters: "
                "when 'yes', the §4.1 exception's bar is relaxed from all four conditions to at "
                "least three of four, since a single corroborating Tier 1/2 source is itself "
                "meaningful even though it can't carry a normal override by itself. 'no' if no Tier "
                "1/2 evidence exists at all (pure Tier 3), or if you actually found two or more "
                "independent Tier 1/2 sources (in which case you have a normal override, not this "
                "exception, and evidence_tier_used should reflect that directly). 'not_applicable' "
                "if tier3_exception_invoked is 'no'."
            ),
        },
        "tier3_sales_listing_search_performed": {
            "type": "string",
            "enum": YES_NO_NA_LABELS,
            "description": (
                "Required whenever tier3_exception_direction is 'to_apt' OR ownership_concentration "
                "is 'single_owner_full_bulk' (§2.1's Rule A): 'yes' only if you explicitly searched "
                "for individual unit SALE listings for this specific property (MLS/Zillow/Redfin "
                "'for sale' listings, county deed/sale records) -- not just rental listings. This is "
                "an absolute gate for BOTH the forward Tier-3 exception and Rule A: you cannot claim "
                "tier3_contradicting_evidence is 'no', or that no unit is individually owned/listed "
                "for Rule A, without having actually done this search. 'not_applicable' for the "
                "reverse direction, when tier3_exception_invoked is 'no', and ownership_concentration "
                "is not 'single_owner_full_bulk'."
            ),
        },
        "ownership_concentration_verified_externally": {
            "type": "string",
            "enum": YES_NO_NA_LABELS,
            "description": (
                "Only meaningful when ownership_concentration is 'single_owner_full_bulk': 'yes' "
                "only if 100% single ownership was verified via EXTERNAL sources -- county parcel/ "
                "deed records showing one owner name across ALL units, a state business registry "
                "entry, or multiple independent Tier 3 sources with a confirmed name/address anchor. "
                "The DB's own `Owner`/`Cleaned Owner` field is NOT reliable enough to satisfy this "
                "on its own: a majority-but-not-full owner is often still the DB's sole listed "
                "Owner, so citing that field is not external verification. A real, previously-"
                "mishandled failure: 'The Falls of Portofino' reasoning stated \"DB 'Owner' is Prime "
                "Group, satisfying the criteria for functional override to APT\" -- that is exactly "
                "the citation this field exists to catch; set this to 'no' in a case like that, not "
                "'yes'. 'not_applicable' if ownership_concentration is not 'single_owner_full_bulk'."
            ),
        },
        "ownership_concentration_contradicting_evidence": {
            "type": "string",
            "enum": YES_NO_NA_LABELS,
            "description": (
                "Only meaningful when ownership_concentration is 'single_owner_full_bulk': 'no' "
                "only if you specifically checked for and found zero evidence of genuine, operating "
                "HOA/COA governance (a registered HOA/COA entity, HOA governance documents/"
                "declaration, a real association fee, or any individually owned/listed unit) despite "
                "the bulk-ownership appearance. 'yes' if any such evidence exists -- this blocks "
                "Rule A regardless of how strong the bulk-ownership signal otherwise looks, mirroring "
                "the §4.1 exception's own 'zero contradicting evidence' condition. A real, "
                "previously-mishandled failure: 'Paradise Gardens One' reasoning itself stated "
                "\"Conflicting evidence: a registered HOA exists ... Ownership is bulk-held, but not "
                "enough for override\" and STILL got corrected to APT -- do not let a bulk-ownership "
                "signal override your own finding of contradicting evidence like that. "
                "'not_applicable' if ownership_concentration is not 'single_owner_full_bulk'."
            ),
        },
        "tier3_sales_evidence_found": {
            "type": "string",
            "enum": YES_NO_NA_LABELS,
            "description": (
                "Only meaningful when tier3_exception_direction is 'to_apt': 'yes' if your sales-"
                "listing search (or anything else in your research) found ANY evidence of "
                "individual units being sold, listed for sale, or planned/entitled for individual "
                "sale -- even if the rest of the property otherwise looks like a rental. This is an "
                "absolute disqualifier: finding SALE evidence at what you're about to call APT "
                "rules out APT, full stop, regardless of anything else you found (unlike rental "
                "listings at an HOA/COA, which do NOT rule out the HOA/COA designation -- the rule "
                "is not symmetric). 'yes' here is forced in code to fail condition 2 and block the "
                "override to APT regardless of what else you submit. 'no' only if you searched and "
                "found none. 'not_applicable' for the reverse direction or when "
                "tier3_exception_invoked is 'no'."
            ),
        },
        "tier3_dual_association_search_performed": {
            "type": "string",
            "enum": YES_NO_NA_LABELS,
            "description": (
                "Only meaningful when tier3_exception_direction is 'to_apt': 'yes' if Attempt 2's "
                "search touched on BOTH kinds of association -- a Declaration of Condominium/"
                "condominium association AND an HOA covenant/homeowners association -- regardless "
                "of which one Master_Ownership Type currently lists, before concluding no "
                "association of any kind exists. HOA and COA are commonly mislabeled as EACH "
                "OTHER, not just mislabeled as APT, so it's good practice to keep this in mind "
                "rather than assuming absence of one type's evidence means absence of any "
                "association. In practice a real search for one type's records usually surfaces "
                "the other if it exists, so this is not a hard requirement -- 'no' does not by "
                "itself block an override; it's tracked for visibility only. 'not_applicable' for "
                "the reverse direction or when tier3_exception_invoked is 'no'."
            ),
        },
        "tier3_entity_name_registry_search_performed": {
            "type": "string",
            "enum": YES_NO_NA_LABELS,
            "description": (
                "Required whenever Master_Property Name contains a full formal legal-entity "
                "string -- specifically 'Condominium Association, Inc.', 'Owners Association, "
                "Inc.', or 'Condominium, Inc.' (you'll be told explicitly when this applies; this "
                "is NOT about casual use of 'condo' or 'apartments' in a name, which is never "
                "evidence either way): 'yes' only if you ran a state business registry search "
                "(Sunbiz-style) for that EXACT entity name as part of Attempt 2. This doesn't "
                "decide the outcome on its own -- if the registry search comes back empty or shows "
                "the entity dissolved, an override can still happen -- it just requires that "
                "specific, cheap, high-value check to actually run before you lean on Tier 3 "
                "evidence alone. A real, previously-mishandled failure: 'Castle Apartments "
                "Condominium Association, Inc.' was overridden to APT without ever running this "
                "search. 'not_applicable' if the name doesn't contain one of those legal-entity "
                "strings."
            ),
        },
        "tier3_contradicting_evidence": {
            "type": "string",
            "enum": YES_NO_NA_LABELS,
            "description": (
                "Only meaningful when tier3_exception_invoked is 'yes': 'no' if you specifically "
                "checked for and found zero contradicting evidence (MLS individual sale, a deed in "
                "a different name, anything from Attempt 2's targeted searches pointing the other "
                "way, and -- for the forward direction -- no individual sale listing or planned-"
                "for-sale framing per tier3_sales_evidence_found). 'yes' if any contradicting "
                "evidence exists. 'not_applicable' otherwise."
            ),
        },
        "tier3_internal_db_corroboration": {
            "type": "string",
            "description": (
                "Only meaningful when tier3_exception_invoked is 'yes': name the ONE specific "
                "internal DB field and value that corroborates your direction. For 'to_apt' "
                "(forward): this MUST be the null/blank Master_Monthly Association Fees field, "
                "e.g. 'Master_Monthly Association Fees is null despite 80 units' -- the DB's own "
                "Owner/Cleaned Owner field is never valid corroboration here, no matter how "
                "concentrated it looks, since a majority-but-not-full owner is often still the "
                "DB's sole listed Owner. For 'to_coa_hoa' (reverse): corroborates a real "
                "association, e.g. 'Master_Monthly Association Fees is $461/month, a real recurring "
                "fee'. One field is enough either way -- do not describe a search across multiple "
                "fields for agreement. Must be truthful and specific -- this is cross-checked "
                "against the property's own row data (direction-aware: a forward claim needs a "
                "null/zero fee, a reverse claim needs a real non-zero one) and, for the forward "
                "direction, must actually mention 'fee' or it's treated as a condition-3 failure. "
                "Empty string if tier3_exception_invoked is 'no'."
            ),
        },
        "tier3_structural_edge_case_ruled_out": {
            "type": "string",
            "enum": YES_NO_NA_LABELS,
            "description": (
                "Only meaningful when tier3_exception_invoked is 'yes': 'yes' only if you explicitly "
                "considered and ruled out a housing cooperative, condo-hotel, senior/age-restricted "
                "community, master-planned mixed community (explicit 'master planned community' "
                "phrasing or explicit for-rent-and-for-sale housing in a cited source), and an "
                "investor bulk-owned COA/HOA (where individual parcels still legally exist) as "
                "better explanations. 'not_applicable' otherwise."
            ),
        },
        "apt_override_sale_evidence_found": {
            "type": "string",
            "enum": YES_NO_NA_LABELS,
            "description": (
                "Required whenever you're about to conclude Override with determined_type COA or "
                "HOA on a property whose Master_Ownership Type is currently APT -- regardless of "
                "which evidence tier or exception path got you there: 'yes' only if you found "
                "genuine, specific evidence that at least one unit at THIS property is CURRENTLY "
                "listed for individual sale, or was sold within roughly the last 12 months (e.g. "
                "an active MLS/Zillow/Redfin listing, a recent county recorder deed transferring a "
                "single unit, a dated local source mentioning a specific unit sale). A legal/"
                "structural condo designation (a county assessor record, a recorded declaration, a "
                "state registry entity type) and a real recurring association fee are NOT "
                "sufficient on their own -- a huge share of legally-platted condo/HOA properties "
                "are functionally single-owner apartment communities today with no individual "
                "sales at all, and §2.1's Rule A can still apply even when strong Tier 1 legal "
                "evidence of the condo declaration exists. A real, previously-mishandled failure: "
                "'Foxcroft Of Shelby' was overridden to APT->COA on 'Tier 1 legal evidence "
                "(...assessor record lists Units 1-48 Foxcroft of Shelby Condos) and recurring "
                "association fees,' concluding 'likely individual owners' -- but the cited sources "
                "were the property's own single-management-company leasing site and a LoopNet "
                "listing for the whole complex as one asset, neither of which is evidence any "
                "individual unit has ever actually been sold or listed; 'likely' is a guess, not a "
                "finding. 'no' if you looked and found nothing, or didn't look. 'not_applicable' "
                "for every other case: any Confirmed/Not-Enough-Info decision, any override that "
                "isn't targeting COA/HOA, or any property whose Master_Ownership Type isn't "
                "already APT."
            ),
        },
    },
    "required": [
        "determined_type", "decision", "confidence", "evidence_tier_used", "reasoning", "sources",
        "structural_edge_case", "ownership_concentration", "reverse_conversion_detected",
        "multi_name_all_agree",
        "tier3_exception_invoked", "tier3_exception_direction", "tier3_reverse_attempt2_exhausted",
        "tier3_independent_source_count", "tier3_name_address_anchor_confirmed",
        "tier3_partial_tier12_support", "tier3_contradicting_evidence", "tier3_internal_db_corroboration",
        "tier3_structural_edge_case_ruled_out",
        "tier3_sales_listing_search_performed", "tier3_sales_evidence_found",
        "ownership_concentration_verified_externally", "ownership_concentration_contradicting_evidence",
        "tier3_dual_association_search_performed", "tier3_entity_name_registry_search_performed",
        "apt_override_sale_evidence_found",
    ],
    "additionalProperties": False,
}

OPENAI_WEB_SEARCH_TOOL = {"type": "web_search"}
OPENAI_SUBMIT_TOOL = {
    "type": "function",
    "name": "submit_assessment",
    "description": SUBMIT_TOOL_DESCRIPTION,
    "parameters": SUBMIT_SCHEMA,
    "strict": True,
}

RETRYABLE_OPENAI_ERRORS = (
    openai.RateLimitError,
    openai.APIConnectionError,
    openai.InternalServerError,
    openai.APITimeoutError,
)


def call_openai_with_backoff(client, **kwargs):
    delay = 2.0
    last_err = None
    for attempt in range(5):
        try:
            return client.responses.create(**kwargs)
        except RETRYABLE_OPENAI_ERRORS as e:
            last_err = e
            sleep_for = delay * (2 ** attempt) + random.uniform(0, 1)
            print(f"    API error ({e.__class__.__name__}), retrying in {sleep_for:.1f}s...", flush=True)
            time.sleep(sleep_for)
    raise last_err


# --- Trigger rule computation (Section 3 of the spec) -----------------------------------
#
# The input file does not arrive with trigger labels attached -- these six rules are
# computed here, directly from each row's own columns, before any research happens.

NAME_APT_KEYWORDS = ("apartment", "apt", "flats", "flat", "lofts", "loft")
NAME_HOA_KEYWORDS = ("homeowner", "hoa")
NAME_COA_KEYWORDS = ("condo", "coa")

SIGNAL_NAMING = "Naming"
SIGNAL_STRUCTURAL = "Structural"
SIGNAL_GOVERNANCE = "Governance-Billing"
SIGNAL_MARKETING = "Marketing"


def _norm_text(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return str(value).strip()


def _parse_number(value):
    try:
        if value is None or (isinstance(value, float) and pd.isna(value)) or str(value).strip() == "":
            return None
        return float(str(value).replace(",", "").replace("$", ""))
    except (TypeError, ValueError):
        return None


def _contains_any(text: str, keywords) -> bool:
    lowered = text.lower()
    return any(kw in lowered for kw in keywords)


def _property_name_parts(name: str) -> list:
    """Some DB records combine two (or more) distinct, separately-named communities into one
    `Master_Property Name` value, e.g. "White Oak Villas, South Cottage Village" -- comma-
    separated. Returns the individual, stripped sub-names in order (empty segments dropped); a
    single-name property returns a one-element list. Callers should treat len() >= 2 as "this
    record covers multiple distinct communities that each need their own evidence" (see
    _enforce_multi_name_guardrail)."""
    return [part.strip() for part in (name or "").split(",") if part.strip()]


# Real, previously-mishandled failure: "CASTLE APARTMENTS CONDOMINIUM ASSOCIATION, INC." was
# overridden straight through its own explicit legal-entity name. This is deliberately narrow --
# it matches a full formal entity string, not casual use of "condo" or "apartments" in a name
# (naming alone is never evidence per §5.2/the bottom of the evidence hierarchy). Detected here in
# code, not left to the model, so the required registry-search gate can't be skipped by simply
# not noticing the pattern.
LEGAL_ENTITY_NAME_RE = re.compile(
    r"condominium\s+association,?\s*inc\.?|owners\s+association,?\s*inc\.?|condominium,?\s*inc\.?",
    re.IGNORECASE,
)


def _has_legal_entity_name(name: str) -> bool:
    return bool(LEGAL_ENTITY_NAME_RE.search(name or ""))


# Real, previously-mishandled failures ("Mountain Ridge Garden Homes Apartments," "Castle
# Apartments Condominium Association, Inc.") both claimed condition 2 ("zero contradicting
# evidence") without evidence that a genuine, targeted individual-unit sale-listing search
# actually ran -- reasoning just asserted "no sale listings found" while only having looked at
# rental sites. This checks the model's ACTUAL issued search queries (extracted from the OpenAI
# response, not self-reported) for one that's sale-oriented. Deliberately broad: real model
# phrasing for "look for a sale listing" varies a lot (site names, "MLS," "resale," "tax record"
# lookups, etc.), and a second real-world batch showed the narrower original list ("for sale,"
# "sold," "assessor," "deed," "parcel," "county record," "recorder") missing plenty of genuine
# searches -- this check is meant to be a rarely-firing failsafe, not a routine occurrence, so it
# errs toward recall over precision. The real fix for the underlying behavior is the explicit,
# example-driven prompt guidance below (search for this project's SYSTEM_PROMPT text); this regex
# is the backstop for when that guidance still isn't followed.
SALE_SEARCH_QUERY_RE = re.compile(
    r"for sale|\bsale\b|\bsold\b|\bresale\b|\bassessor\b|\bdeed\b|\bparcel\b|county record"
    r"|\brecorder\b|\bmls\b|\bzillow\b|\bredfin\b|realtor\.?com|\blisting|tax record"
    r"|ownership record|property record",
    re.IGNORECASE,
)


def _genuine_sale_search_performed(row: dict, result: dict) -> bool:
    """True only if at least one of the ACTUAL search queries issued during research (see
    research_property()'s `_searched_queries`) is sale-oriented. Deliberately does NOT also
    require the query to share tokens with the row's Address/Master_Property Name: every
    research_property() call is already scoped to researching this one property (see
    build_user_message()), so any search issued during it is already about this property --
    requiring token overlap on top of that only produced false negatives from address-formatting
    mismatches in two real-world batches, exactly the kind of failsafe-firing-too-often problem
    this check exists to avoid."""
    queries = result.get("_searched_queries") or []
    return any(SALE_SEARCH_QUERY_RE.search(query) for query in queries)


def _first_present(row: dict, *keys):
    """First non-null/non-empty value among the given keys, or None."""
    for key in keys:
        val = row.get(key)
        num = _parse_number(val)
        if num is not None:
            return num
    return None


def compute_unit_building_ratio(row: dict):
    """Returns (ratio_or_None, note). Falls back from the _50+ fields to _20+ when null,
    per Section 3. Cross-checks against Building Count Bin only to surface a note for the
    model's context -- it never gates whether a rule fires."""
    units = _first_present(row, "Master_Units_50+", "Master_Units_20+")
    buildings = _first_present(row, "Master_Building Count_50+", "Master_Building Count_20+")
    if units is None or buildings is None or buildings == 0:
        return None, None
    ratio = units / buildings
    note = None
    bin_value = _norm_text(row.get("Building Count Bin"))
    if bin_value:
        note = f"Building Count Bin on file: '{bin_value}' (sanity cross-check against computed ratio)."
    return ratio, note


def compute_triggers(row: dict) -> list:
    """Returns a list of {'rule': str, 'signal_type': str} dicts for every one of the six
    trigger rules this property matches. All six are checks against Master_Ownership Type,
    never Master_Property Type (Section 2's critical framing)."""
    ownership = _norm_text(row.get("Master_Ownership Type")).upper()
    name = _norm_text(row.get("Master_Property Name"))
    triggers = []

    if ownership != "APT" and _contains_any(name, NAME_APT_KEYWORDS):
        triggers.append({"rule": "Type-Name Mismatch (APT)", "signal_type": SIGNAL_NAMING})
    if ownership != "HOA" and _contains_any(name, NAME_HOA_KEYWORDS):
        triggers.append({"rule": "Type-Name Mismatch (HOA)", "signal_type": SIGNAL_NAMING})
    if ownership != "COA" and _contains_any(name, NAME_COA_KEYWORDS):
        triggers.append({"rule": "Type-Name Mismatch (COA)", "signal_type": SIGNAL_NAMING})

    ratio, _ = compute_unit_building_ratio(row)
    if ratio is not None:
        if ownership != "HOA" and ratio < 5:
            triggers.append({"rule": "Low Unit-to-Building Ratio", "signal_type": SIGNAL_STRUCTURAL})
        if ownership not in ("COA", "APT") and ratio > 1:
            triggers.append({"rule": "High Unit-to-Building Ratio", "signal_type": SIGNAL_STRUCTURAL})

    floor_count = _parse_number(row.get("Master_Floor Count"))
    if floor_count is not None and ownership not in ("APT", "COA") and floor_count > 3:
        triggers.append({"rule": "High Floor Count", "signal_type": SIGNAL_STRUCTURAL})

    # "is not null" per the spec, with a zero value treated as "no fee on file" rather than
    # a real fee -- a $0 fee doesn't carry the governance signal a real recurring fee does.
    fee = _parse_number(row.get("Master_Monthly Association Fees"))
    if ownership not in ("HOA", "COA") and fee is not None and fee != 0:
        triggers.append({"rule": "Has Fees", "signal_type": SIGNAL_GOVERNANCE})

    leasing_present = any(
        _norm_text(row.get(col)) for col in ("Leasing Company", "Leasing Company Contact Name", "Leasing Company Phone")
    )
    if ownership != "APT" and leasing_present:
        triggers.append({"rule": "Has Leasing Info", "signal_type": SIGNAL_MARKETING})

    return triggers


def summarize_trigger_types(triggers: list) -> list:
    """Unique signal types represented, in first-seen order -- used to distinguish
    corroborating overlap (different signal types) from redundant overlap (same type,
    e.g. two naming rules) per Section 3.1."""
    seen = []
    for t in triggers:
        if t["signal_type"] not in seen:
            seen.append(t["signal_type"])
    return seen


# --- Building the research prompt --------------------------------------------------------

def fetch_url_cached(url: str, cache: dict) -> str:
    if url in cache:
        return cache[url]
    text = None
    try:
        resp = requests.get(url, timeout=URL_FETCH_TIMEOUT, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style"]):
            tag.decompose()
        text = " ".join(soup.get_text(separator=" ").split())[:URL_FETCH_MAX_CHARS]
    except Exception as e:
        text = None
        print(f"    Could not pre-fetch {url}: {e}", flush=True)
    cache[url] = text
    return text


# Only these mapped columns (Section 10.1) are shown to the model for reasoning -- the tool
# reads the full row for logging/output purposes, but reasoning is scoped to this subset so
# a stray column in the ~170-column export can't quietly influence the decision.
REASONING_FIELDS = [
    ("RecordID", "Property ID"),
    ("Address", "Address"),
    ("Master_Property Name", "Property Name"),
    ("Master_Ownership Type", "DB-listed Ownership Type (the field being verified)"),
    ("Master_Property Type", "Physical Structure Type (context only -- NOT the field being verified)"),
    ("Master_Units_50+", "Unit Count (50+ threshold)"),
    ("Master_Units_20+", "Unit Count (20+ threshold, fallback)"),
    ("Master_Building Count_50+", "Building Count (50+ threshold)"),
    ("Master_Building Count_20+", "Building Count (20+ threshold, fallback)"),
    ("Building Count Bin", "Building Count Bin (sanity cross-check)"),
    ("Master_Floor Count", "Floor Count"),
    ("Master_Original Build Year", "Original Build Year (general context, e.g. assessing §5.3 lease-up-phase ambiguity qualitatively -- there is no minimum-age requirement for the §4.1 Tier-3 exception)"),
    ("Master_Most Recent Build Year", "Most Recent Build Year (e.g. a later phase/addition; fallback if Original is blank)"),
    ("Master_Monthly Association Fees", "Monthly Association Fees"),
    ("Leasing Company", "Leasing Company"),
    ("Leasing Company Contact Name", "Leasing Company Contact"),
    ("Leasing Company Phone", "Leasing Company Phone"),
    ("Owner", "Owner"),
    ("Cleaned Owner", "Cleaned Owner"),
    ("Property Manager", "Property Manager"),
    ("Cleaned Property Manager", "Cleaned Property Manager"),
    ("Developer Name", "Developer Name"),
    ("Cleaned Developer", "Cleaned Developer"),
    ("Master_Senior Flag", "Senior Flag"),
    ("Master_Student Flag", "Student Flag"),
    ("Master_Gated HOA Flag", "Gated HOA Flag"),
    # Bulk Flag / Bulk Package Type / % Bulk Overall are deliberately NOT shown here -- despite
    # the name, this is a broadband-competition dataset and these fields describe a bulk
    # internet/TV/phone service contract with an ISP, not real-estate ownership concentration.
    # An earlier version of this tool showed them and the model hallucinated a false
    # contradiction ("% Bulk Overall: 0.5" read as "only 50% single-owned") against an otherwise-
    # satisfied §4.1 condition 4. See ownership_type_verification_spec.md §10.1.
    ("LLM_Property Name", "Prior verified research -- Property Name"),
    ("LLM_Property URL", "Prior verified research -- Property URL"),
    ("LLM_Address", "Prior verified research -- Address"),
    ("LLM_Property Contact #1", "Prior verified research -- Contact #1"),
    ("LLM_Monthly HOA/COA fees", "Prior verified research -- Monthly HOA/COA fees"),
    ("Consolidated Macro Amenities", "Amenities"),
    ("Consolidated Luxury Amenities Count", "Luxury Amenities Count"),
]


def format_property(row: dict, triggers: list, url_cache: dict) -> str:
    lines = [f"### Property (RecordID: {row.get('RecordID', '')})"]
    url_value = None
    for col, label in REASONING_FIELDS:
        value = row.get(col)
        if value is None or (isinstance(value, float) and pd.isna(value)) or _norm_text(value) == "":
            continue
        lines.append(f"- {label}: {value}")
        if col == "LLM_Property URL":
            url_value = str(value)
    if url_value:
        fetched = fetch_url_cached(url_value, url_cache)
        if fetched:
            lines.append(f"- Pre-fetched content from prior-research URL {url_value} (verify it actually belongs to this property):")
            lines.append(f"  \"{fetched}\"")
    _, ratio_note = compute_unit_building_ratio(row)
    if ratio_note:
        lines.append(f"- {ratio_note}")

    name_parts = _property_name_parts(_norm_text(row.get("Master_Property Name")))
    if len(name_parts) >= 2:
        parts_list = "; ".join(f'"{p}"' for p in name_parts)
        lines.append(
            f"- NOTE: this record's name combines {len(name_parts)} distinct sub-names: "
            f"{parts_list}. Research EACH one separately as its own Attempt 1/2 (the address on "
            f"file may only directly correspond to one of them -- search for the other(s) in the "
            f"same immediate vicinity/nearby address). Only conclude Override if ALL of them "
            f"independently and separately support the SAME conclusion; if even one disagrees or "
            f"can't be confirmed, this must stay Not Enough Info. Set `multi_name_all_agree` "
            f"accordingly."
        )

    property_name = _norm_text(row.get("Master_Property Name"))
    if _has_legal_entity_name(property_name):
        lines.append(
            f"- NOTE: this record's own name, \"{property_name}\", contains a full formal "
            f"legal-entity string. If you're considering the Tier-3-only exception (to APT), you "
            f"must run a state business registry search (Sunbiz-style) for this EXACT entity "
            f"name as part of Attempt 2 before that exception can apply -- set "
            f"`tier3_entity_name_registry_search_performed` accordingly. This doesn't decide the "
            f"outcome by itself (an empty or dissolved registry result doesn't block an override "
            f"on its own), it just requires that specific, cheap, high-value check to actually run."
        )

    lines.append("")
    lines.append(f"### Trigger rule(s) that flagged this property for review ({len(triggers)} total)")
    if triggers:
        for t in triggers:
            lines.append(f"- {t['rule']} (signal type: {t['signal_type']})")
    else:
        lines.append("- (none computed -- verify the DB label on general merits)")
    multi_trigger_types = summarize_trigger_types(triggers)
    if len(multi_trigger_types) > 1:
        lines.append(
            f"This property has {len(triggers)} trigger(s) spanning {len(multi_trigger_types)} distinct "
            f"signal types ({', '.join(multi_trigger_types)}) -- treat this as an elevated prior and run "
            f"the full Attempt 2 targeted search rather than settling for a quick Attempt-1 Confirmed."
        )
    lines.append(
        "Remember: the trigger rule(s) above explain why this property is being checked, not what the "
        "answer is. They are never themselves evidence for or against the DB label."
    )
    return "\n".join(lines)


def build_user_message(row: dict, triggers: list, url_cache: dict) -> str:
    return (
        "Research the following property and verify its Master_Ownership Type against the evidence "
        "hierarchy and process in your instructions.\n\n" + format_property(row, triggers, url_cache)
    )


def find_function_call(output_items, name):
    for item in output_items:
        if getattr(item, "type", None) == "function_call" and item.name == name:
            return item
    return None


def extract_openai_sources(output_items):
    sources = []
    for item in output_items:
        if getattr(item, "type", None) != "message":
            continue
        for part in getattr(item, "content", []) or []:
            for annotation in getattr(part, "annotations", []) or []:
                url = getattr(annotation, "url", None)
                if url:
                    sources.append(url)
    return sources


def extract_openai_search_queries(output_items):
    """Pulls the actual query text out of each web_search_call item in the response -- this is
    what lets guardrails verify a targeted search genuinely happened (e.g. a real sale-listing
    query), rather than trusting a self-reported field claiming it did.

    A real, previously-mishandled bug: the OpenAI SDK's `ActionSearch` type exposes the query
    text on TWO separate optional fields -- singular `query: Optional[str]` and plural
    `queries: Optional[List[str]]` -- and the live API can populate either one (a single-query
    search often comes back on `query`, but not always). An earlier version of this function only
    read `query`, so on any response using `queries` instead, every single search this function
    looked at came back with nothing -- which meant the code-verified sale-listing-search gate
    (see _genuine_sale_search_performed()) failed even when the model genuinely ran the search,
    downgrading good overrides across an entire batch. Both fields are read now, and non-'search'
    actions (open_page, find_in_page) are skipped since they don't carry a query at all.

    De-duplicates (preserving first-seen order) because a single search action can populate BOTH
    `query` and `queries` with the SAME text -- without de-duping, one real search shows up twice
    in `_searched_queries`, which is confusing in diagnostics (looks like the model ran the same
    query twice on purpose) even though it doesn't affect whether the sale-search gate passes."""
    queries = []
    for item in output_items:
        if getattr(item, "type", None) != "web_search_call":
            continue
        action = getattr(item, "action", None)
        if action is None or getattr(action, "type", None) != "search":
            continue
        query = getattr(action, "query", None)
        if query:
            queries.append(query)
        for query in getattr(action, "queries", None) or []:
            if query:
                queries.append(query)
    return list(dict.fromkeys(queries))


# Real, repeated failures across multiple batches ("Stratford Crossing Flats," "Pines Gardens
# Apartments," "Constance Lofts," "Dearlove Manor Apartments," and others) all concluded an
# override to APT via the forward §4.1 exception or §2.1's Rule A while the ONLY search queries
# actually issued were generic ("[name] [address] ownership type") -- never anything sale-
# oriented. Prompt wording alone (examples, an explicit literal-keyword requirement, a named
# non-example) did not reliably fix this across several rounds of tightening. This is the code-
# enforced version instead of continuing to argue with the model in the prompt: when the model
# tries to submit exactly this kind of override without a qualifying search on record,
# research_property() rejects that submission, tells it specifically what's missing and what
# query to run, and gives it a bounded number of extra turns to actually run it and resubmit. The
# existing after-the-fact guardrails (_enforce_tier3_override_guardrail,
# _enforce_functional_ownership_guardrail) remain the ultimate failsafe if the model still hasn't
# complied once the correction budget or turn budget runs out -- this just makes that failsafe
# fire far less often by giving the model a real chance to fix it mid-conversation instead of
# silently downgrading after the fact.
MAX_SALE_SEARCH_CORRECTIONS = 2


def _submission_requires_sale_search(result: dict) -> bool:
    """Mirrors the exact scoping _enforce_tier3_override_guardrail() (forward direction) and
    _enforce_functional_ownership_guardrail() (Rule A) apply after the fact: an Override to APT
    via either of those two paths needs a genuine sale-listing search on record. An ordinary
    Tier 1/2 override (never invoking either bounded path) is not scoped by this -- two
    independently-corroborated Tier 1/2 sources are already strong enough evidence on their own."""
    if result.get("decision") != "Override" or result.get("determined_type") != "APT":
        return False
    if result.get("tier3_exception_invoked") == "yes" and result.get("tier3_exception_direction") == "to_apt":
        return True
    return result.get("ownership_concentration") == "single_owner_full_bulk"


def _sale_search_correction_message(row: dict) -> str:
    address = _norm_text(row.get("Address"))
    name = _norm_text(row.get("Master_Property Name"))
    subject = address or name
    return (
        "Before I can accept that conclusion: you're overriding to APT via a path that requires "
        "an actual, dedicated search for individual unit SALE listings, and none of your search "
        "queries so far contain a sale-oriented term (\"for sale,\" \"sold,\" \"MLS,\" \"Zillow,\" "
        "\"Redfin,\" \"listing,\" \"resale,\" \"deed,\" \"parcel,\" \"assessor,\" or \"tax record\"). "
        "A general ownership or rental-operation search does not satisfy this, no matter how "
        f"thorough it was otherwise. Run one more search now, e.g. \"{subject} for sale\", "
        f"\"{subject} sold\", or \"{name} MLS listing\" -- then call submit_assessment again with "
        "your final answer (Confirmed/the DB label if that search finds nothing supporting APT, "
        "or updated if it changes your conclusion)."
    )


def research_property(client, row: dict, triggers: list, url_cache: dict, model: str) -> dict:
    input_items = [{"role": "user", "content": build_user_message(row, triggers, url_cache)}]
    tools = [OPENAI_WEB_SEARCH_TOOL, OPENAI_SUBMIT_TOOL]
    previous_response_id = None
    searched_sources = []
    searched_queries = []
    sale_search_corrections_used = 0

    for turn in range(MAX_TURNS):
        is_last_turn = turn == MAX_TURNS - 1
        tool_choice = {"type": "function", "name": "submit_assessment"} if is_last_turn else "auto"
        kwargs = dict(
            model=model,
            instructions=SYSTEM_PROMPT,
            input=input_items,
            tools=tools,
            tool_choice=tool_choice,
        )
        if previous_response_id:
            kwargs["previous_response_id"] = previous_response_id
        response = call_openai_with_backoff(client, **kwargs)
        previous_response_id = response.id
        searched_sources.extend(extract_openai_sources(response.output))
        searched_queries.extend(extract_openai_search_queries(response.output))

        submit_call = find_function_call(response.output, "submit_assessment")
        if submit_call:
            result = json.loads(submit_call.arguments)

            if (
                not is_last_turn
                and sale_search_corrections_used < MAX_SALE_SEARCH_CORRECTIONS
                and _submission_requires_sale_search(result)
                and not _genuine_sale_search_performed(row, {"_searched_queries": searched_queries})
            ):
                sale_search_corrections_used += 1
                # A function_call (submit_assessment) MUST be followed by a matching
                # function_call_output before the conversation can continue via
                # previous_response_id -- the Responses API rejects the next turn with
                # "No tool output found for function call <id>" otherwise. This is a real,
                # previously-mishandled bug: the correction loop used to send only the corrective
                # user message and skip this, since returning immediately after a submit_call
                # (the only thing this loop did before the correction mechanism existed) never
                # needed one.
                input_items = [
                    {
                        "type": "function_call_output",
                        "call_id": submit_call.call_id,
                        "output": (
                            "Rejected: this submission requires a genuine sale-listing search "
                            "that hasn't been performed yet. See the following message for what "
                            "to do next."
                        ),
                    },
                    {"role": "user", "content": _sale_search_correction_message(row)},
                ]
                continue

            if not result.get("sources"):
                result["sources"] = sorted(set(searched_sources))
            # Internal-only, not part of SUBMIT_SCHEMA -- lets guardrails verify a genuinely
            # targeted search actually happened rather than trusting a self-reported field.
            result["_searched_queries"] = list(dict.fromkeys(searched_queries))
            return result

        input_items = [{
            "role": "user",
            "content": "Please call submit_assessment now with your final conclusion based on the research so far.",
        }]

    raise RuntimeError("Model did not produce a submit_assessment call within the turn budget")


# --- Deterministic guardrails --------------------------------------------------------------
# These mirror the prior dedup tool's approach: prose instructions alone haven't reliably
# stopped certain failure patterns, so the most safety-critical rules in the spec are also
# enforced in code as a backstop, not just requested in the prompt.

def _tier3_exception_backstop_failure(row: dict, result: dict, direction: str):
    """Deterministic cross-check of the model's self-reported condition-3 corroboration against
    the property's own row data -- the one condition where a claim can actually be checked in
    code -- rather than trusting a bare self-report. Direction-aware: the forward exception
    ('to_apt') expects a claim of a null/absent fee; the reverse exception ('to_coa_hoa') expects
    the opposite, a claim of a real recurring fee. Returns a human-readable failure reason, or
    None if no backstop check fires (which does not by itself mean the exception is satisfied --
    the self-reported fields still gate it). There is deliberately no property-age backstop here:
    neither direction has a minimum-age/build-year requirement."""
    corroboration_text = _norm_text(result.get("tier3_internal_db_corroboration")).lower()
    if "fee" not in corroboration_text:
        return None
    fee = _parse_number(row.get("Master_Monthly Association Fees"))
    if direction == "to_apt" and fee is not None and fee != 0:
        return (
            f"the model cited a null/absent association fee as internal corroboration, but "
            f"Master_Monthly Association Fees is actually populated ({fee:g}) -- direct "
            f"contradiction with the row's own data"
        )
    if direction == "to_coa_hoa" and (fee is None or fee == 0):
        return (
            "the model cited the association fee as internal corroboration for a COA/HOA "
            "determination, but Master_Monthly Association Fees is actually null/zero on this "
            "row -- direct contradiction with the row's own data"
        )
    return None


def _enforce_structural_edge_case_guardrail(db_type: str, result: dict) -> dict:
    """§5.7's rule, restated as code rather than left to the model's judgment: a structural edge
    case (housing cooperative, condo-hotel/timeshare, manufactured home community, senior/student
    housing) always keeps the DB label as-is. A real failure this catches: the model correctly
    identified a property as a housing cooperative, cited its monthly association fees as evidence
    of that co-op structure, and then used those SAME fees to justify overriding the label to APT
    anyway -- exactly backwards, since fees are evidence against a true rental APT, not for one.
    Rather than trust the model to draw the right conclusion once it's flagged an edge case, this
    forces decision back to Confirmed and determined_type back to the DB label whenever
    structural_edge_case is set to anything other than "none", regardless of what else was
    submitted. Runs before _enforce_tier3_override_guardrail so an edge case can never also sneak
    through as a Tier-3-exception override."""
    edge_case = result.get("structural_edge_case")
    if not edge_case or edge_case == "none":
        return result

    result = dict(result)
    if result.get("decision") != "Confirmed" or result.get("determined_type") != db_type:
        original = result.get("reasoning", "")
        result["reasoning"] = (
            f"Automatically kept as-is: flagged as a structural edge case ({edge_case}) per §5.7, "
            f"and edge cases are never overridden regardless of which category might seem like a "
            f"closer technical fit. Original reasoning: {original}"
        )
    result["decision"] = "Confirmed"
    result["determined_type"] = db_type
    return result


COOP_MENTION_RE = re.compile(r"\bco-?ops?\b|\bcooperatives?\b", re.IGNORECASE)


def _enforce_coop_mention_guardrail(db_type: str, result: dict) -> dict:
    """A second, independent backstop for the same policy as _enforce_structural_edge_case_
    guardrail() above: co-ops are never overridden, full stop. This one doesn't rely on the model
    correctly setting structural_edge_case -- it directly scans the free-text `reasoning` for a
    co-op mention. Real failure this guards against: reasoning that raises a co-op as a live
    possibility ("this may be a cooperative, but there wasn't enough evidence to confirm") without
    setting structural_edge_case, after which a weak Override built on other, unrelated evidence
    could otherwise slip through. Any Override whose own reasoning mentions "co-op"/"cooperative"
    is forced back to Confirmed/the DB label, regardless of what triggered the mention."""
    if result.get("decision") != "Override":
        return result
    if not COOP_MENTION_RE.search(result.get("reasoning", "")):
        return result

    result = dict(result)
    original = result.get("reasoning", "")
    result["decision"] = "Confirmed"
    result["determined_type"] = db_type
    result["reasoning"] = (
        f"Automatically kept as-is: the model's own reasoning raised a housing-cooperative "
        f"possibility, and co-ops are never overridden regardless of how that possibility was "
        f"weighed against other evidence. Original reasoning: {original}"
    )
    return result


def _enforce_sales_evidence_guardrail(db_type: str, result: dict) -> dict:
    """Absolute rule per a real reported failure ('Sky Nashville'): finding evidence of
    individual unit sales, sale listings, or units planned/entitled for individual sale directly
    rules out an APT conclusion, regardless of which mechanism (ordinary Tier 1/2 override, or
    either §4.1/§4.2 Tier-3 exception direction) got there, and regardless of what else was
    found. This is deliberately NOT symmetric: finding rental listings at an HOA/COA does NOT
    rule out the HOA/COA designation (plenty of genuine HOA/COA units are individually owned and
    rented out) -- only a sale signal at what's about to be called APT triggers this.

    Real failure this guards against: reasoning stated a property "is an entitled development
    planned for for-sale condos/townhomes" and then, in the same breath, concluded "no conflicting
    evidence was found confirming APT" and overrode to APT anyway -- a direct self-contradiction
    that stood because tier3_contradicting_evidence was trusted at face value instead of being
    cross-checked against what the dedicated sales-listing search itself found. Runs before the
    other override guardrails (structural_edge_case/coop-mention aside) so an Override this
    clearly contradicted never even reaches the Tier-3 exception's condition evaluation."""
    if result.get("decision") != "Override" or result.get("determined_type") != "APT":
        return result
    if result.get("tier3_sales_evidence_found") != "yes":
        return result

    result = dict(result)
    original = result.get("reasoning", "")
    result["decision"] = "Confirmed"
    result["determined_type"] = db_type
    result["sales_evidence_override_blocked"] = True
    result["reasoning"] = (
        f"Automatically downgraded: individual unit sale listings (or planned/entitled-for-sale "
        f"framing) were found, which directly rules out an APT conclusion regardless of any other "
        f"evidence -- finding rental listings at an HOA/COA doesn't rule out HOA/COA, but finding "
        f"sale listings (or planned-for-sale framing) at an APT does rule out APT. Original "
        f"reasoning: {original}"
    )
    return result


MASTER_PLANNED_RE = re.compile(r"master[- ]planned communit", re.IGNORECASE)
FOR_RENT_AND_SALE_RE = re.compile(
    r"for rent and for sale|for sale and for rent|for-rent and for-sale|for-sale and for-rent",
    re.IGNORECASE,
)


def _enforce_master_planned_community_guardrail(row: dict, db_type: str, result: dict, url_cache: dict) -> dict:
    """A structural-edge-case backstop that doesn't rely on the model correctly setting
    structural_edge_case, and doesn't rely on the model's own reasoning either: it independently
    re-fetches the property's own cited source URLs and scans the actual fetched page text for
    "master planned community" phrasing or an explicit mix of for-rent AND for-sale housing.

    Real failure this guards against: "Baumgardner Ranch" (DB: HOA) was overridden to APT with
    reasoning claiming it's "marketed as a rental apartment community with no HOA evidences" --
    but the cited source page itself describes it as a master planned community whose stated goal
    is to provide "a multitude of high quality housing options ... including for rent and for sale
    homes." The model's own summary never engaged with this, even though it was right there in the
    page it cited. A master-planned community mixing for-rent and for-sale housing isn't reliably
    resolvable to a single APT/COA/HOA label from thin evidence -- the safe default is to leave the
    DB label as-is, the same "never override" policy as the other structural edge cases in §5.7,
    just reached via an independent content check rather than the model's self-report.

    Checks the model's own `reasoning` first (cheap, no network call), then falls back to
    re-fetching each URL in `sources` (via the same fetch_url_cached() used elsewhere, which
    already fails soft on network errors) only if decision is actually "Override" -- there's no
    reason to spend that cost on a property already Confirmed."""
    if result.get("decision") != "Override":
        return result

    combined_text = result.get("reasoning", "") or ""
    if not (MASTER_PLANNED_RE.search(combined_text) or FOR_RENT_AND_SALE_RE.search(combined_text)):
        found = False
        for url in result.get("sources", []) or []:
            fetched = fetch_url_cached(url, url_cache)
            if fetched and (MASTER_PLANNED_RE.search(fetched) or FOR_RENT_AND_SALE_RE.search(fetched)):
                found = True
                break
        if not found:
            result = dict(result)
            result["master_planned_override_blocked"] = False
            return result

    result = dict(result)
    original = result.get("reasoning", "")
    result["decision"] = "Confirmed"
    result["determined_type"] = db_type
    result["master_planned_override_blocked"] = True
    result["reasoning"] = (
        f"Automatically kept as-is: 'master planned community' phrasing (or an explicit mix of "
        f"for-rent and for-sale housing) was found in the reasoning or a cited source, and a "
        f"development like that isn't reliably resolvable to a single APT/COA/HOA label from thin "
        f"evidence -- per §5.7, defaulting to no change. Original reasoning: {original}"
    )
    return result


def _enforce_apt_override_sale_evidence_guardrail(row: dict, result: dict) -> dict:
    """Absolute, universal gate for ANY override of an APT-listed property to COA/HOA, regardless
    of evidence tier or exception path: genuine evidence that at least one unit at this property
    is currently listed for individual sale, or was sold within roughly the last 12 months, is
    required. Legal/structural evidence (a recorded declaration, a state registry entity, a county
    assessor use-code) and a real recurring association fee are never sufficient by themselves for
    this direction -- a huge share of legally-platted condo/HOA properties are functionally
    single-owner apartment communities today with no individual sales at all (§2.1's Rule A can
    still apply even when strong Tier 1 legal evidence of the condo declaration exists).

    Real failure this guards against: "Foxcroft Of Shelby" (DB: APT) was overridden to COA at
    "High" confidence on "Tier 1 legal evidence" -- a county assessor record listing "Units 1-48
    Foxcroft of Shelby Condos" -- plus recurring association fees, concluding "likely individual
    owners" under §2.1 Rule B. But the cited sources are the property's own single-management-
    company leasing site (Kaftan Communities) and a LoopNet listing for the whole 66-unit complex
    as one asset -- classic single-owner apartment-community operation, and neither one is
    evidence that any individual unit has ever actually been sold or listed. A legal condo
    designation only tells you the units are individually PLATTED, not that anyone currently
    functions as an individual owner -- that requires the same kind of check §2.1's Rule A/B
    already demand for the ownership_concentration path, just made an absolute floor here too
    since a bare legal-tier citation was otherwise sailing through unchecked.

    This supersedes the older, narrower fee-specific check that used to live here (which only
    fired when reasoning mentioned a fee and the bounded exception wasn't invoked, and which risked
    a false positive on a case with genuine sale evidence but no explicit "HOA" keyword in the
    cited source) -- requiring real sale evidence directly, universally, is both simpler and
    strictly stronger: anything the old check caught, this one catches too.

    Deliberately reuses _genuine_sale_search_performed() (the same actual-query cross-check used
    for the forward direction's absolute sale-search gate) so a bare "yes" self-report isn't
    trusted without at least one real sale-oriented search actually being issued."""
    if result.get("decision") != "Override" or result.get("determined_type") not in ("COA", "HOA"):
        return result
    if _norm_text(row.get("Master_Ownership Type")).upper() != "APT":
        return result
    if result.get("apt_override_sale_evidence_found") == "yes" and _genuine_sale_search_performed(row, result):
        return result

    result = dict(result)
    original = result.get("reasoning", "")
    result["decision"] = "Not Enough Info"
    result["confidence"] = "Low"
    result["reasoning"] = (
        "Automatically downgraded: overriding an APT-listed property to COA/HOA requires genuine "
        "evidence that at least one unit at this property is currently listed for individual "
        "sale, or was sold within roughly the last 12 months -- that wasn't confirmed here. A "
        "legal/structural condo designation (an assessor record, a recorded declaration, a state "
        "registry entity) and a real recurring association fee are not sufficient on their own, "
        "since many legally-platted condo/HOA properties are functionally single-owner apartment "
        "communities today (§2.1's Rule A can still apply even with real Tier 1 legal evidence). "
        f"Original reasoning: {original}"
    )
    return result


def _enforce_functional_ownership_guardrail(row: dict, db_type: str, result: dict) -> dict:
    """§2.1's governing principle, restated as code: the correct DB label reflects who we'd
    actually have to sell to right now, not the legal condo/HOA declaration on file.

    - Rule A ('single_owner_full_bulk'): 100% of units currently held by one entity, one
      centralized leasing/management contact, and no unit currently individually owned or
      listed -- forces determined_type to APT regardless of a legal declaration.
    - Rule B ('individual_owner_present'): at least one unit is currently individually owned --
      forces determined_type away from APT, no matter how small a fraction of the building
      that unit is.

    Rule A is gated by three real, deterministic checks -- a real failure showed the bare
    self-reported 'single_owner_full_bulk' value alone isn't enough:
    1. **A real, populated `Master_Monthly Association Fees` unconditionally blocks Rule A** --
       a genuinely bulk-owned property with no operating association should have no fee on file
       at all (the same logic as the §4.1 exception's own condition 3, just applied in reverse).
       This is a hard, code-only check -- it does not depend on any self-reported field and
       cannot be talked around. Real failure this catches: "Paradise Gardens One" was corrected
       to APT via this rule despite a real $70/month fee on file and the model's OWN original
       reasoning stating "a registered HOA exists" and "ownership is bulk-held, but not enough
       for override" -- the self-reported ownership_concentration field alone let the override
       through anyway.
    2. **`ownership_concentration_verified_externally` must be 'yes'** -- 100% single ownership
       must be verified via EXTERNAL sources (county parcel/deed records showing one owner name
       across ALL units, state business registry, or multiple independent sources), never by
       simply citing the DB's own `Owner`/`Cleaned Owner` field. That field is not reliable
       enough for this: a majority-but-not-full owner is often still the DB's sole listed Owner.
       Real failure this catches: "The Falls of Portofino" reasoning stated "DB 'Owner' is Prime
       Group, satisfying the criteria for functional override to APT" -- citing the DB's own
       field as if it were external verification.
    3. **`ownership_concentration_contradicting_evidence` must be 'no'** -- mirrors the §4.1
       exception's "zero contradicting evidence" condition: any sign of genuine HOA/COA
       governance (a registered association entity, HOA governance documents, individually
       owned/listed units) blocks Rule A regardless of the bulk-ownership appearance.
    4. **`tier3_sales_listing_search_performed` must be 'yes'** -- Rule A shares the same
       absolute sales-listing-search gate as the forward §4.1 exception (see
       _enforce_tier3_override_guardrail); a genuine search for individual sale listings must
       have been performed before concluding no individual owner exists.

    Skipped entirely for a structural edge case (housing co-op, condo-hotel, etc., whether
    flagged via structural_edge_case or caught by the co-op-mention backstop) -- those are their
    own category, resolved by "never override" (§5.7), and functional bulk-ownership evidence is
    not a reason to reopen that policy. Runs before _enforce_tier3_override_guardrail, so a Rule
    A claim resting only on Tier 3 evidence is still subject to that guardrail's normal
    restrictions -- Rule A is not a way to bypass the Tier-3-evidence rules, it mainly matters
    when the ownership-concentration evidence is itself Tier 1/2 (e.g. parcel/deed records)."""
    edge_case = result.get("structural_edge_case")
    if edge_case and edge_case != "none":
        return result
    if COOP_MENTION_RE.search(result.get("reasoning", "") or ""):
        return result

    concentration = result.get("ownership_concentration")
    result = dict(result)
    result["functional_apt_override_used"] = False
    result["reverse_conversion_used"] = False

    if concentration == "single_owner_full_bulk":
        fee = _parse_number(row.get("Master_Monthly Association Fees"))
        rule_a_failure = None
        if fee is not None and fee != 0:
            rule_a_failure = (
                f"a real, populated Master_Monthly Association Fees ({fee:g}) contradicts "
                f"'no operating association exists'"
            )
        elif result.get("ownership_concentration_verified_externally") != "yes":
            rule_a_failure = (
                "100% single ownership wasn't confirmed to be verified externally (county "
                "parcel/deed records, state business registry, or multiple independent sources) "
                "rather than by simply citing the DB's own Owner/Cleaned Owner field"
            )
        elif result.get("ownership_concentration_contradicting_evidence") == "yes":
            rule_a_failure = "contradicting evidence of genuine HOA/COA governance was found"
        elif result.get("tier3_sales_listing_search_performed") != "yes":
            rule_a_failure = "an explicit search for individual sale listings wasn't confirmed"
        elif not _genuine_sale_search_performed(row, result):
            queries = result.get("_searched_queries") or []
            rule_a_failure = (
                "no genuinely targeted individual-unit sale-listing search was found among the "
                f"actual search queries issued ({queries!r}), regardless of what tier3_sales_"
                "listing_search_performed claims"
            )

        if rule_a_failure:
            if result.get("decision") == "Override" and result.get("determined_type") == "APT":
                original = result.get("reasoning", "")
                result["decision"] = "Not Enough Info"
                result["determined_type"] = db_type
                result["reasoning"] = (
                    f"Automatically downgraded: the model invoked §2.1's Rule A (functional APT "
                    f"override), but {rule_a_failure}. Original reasoning: {original}"
                )
            return result

        if result.get("determined_type") != "APT":
            original = result.get("reasoning", "")
            result["determined_type"] = "APT"
            result["decision"] = "Confirmed" if db_type == "APT" else "Override"
            result["reasoning"] = (
                f"Automatically corrected to APT per §2.1: 100% single ownership (verified "
                f"externally), one centralized leasing/management contact, and no individually-"
                f"owned or individually-listed unit means we'd only ever be selling to one "
                f"entity, regardless of the legal condo/HOA declaration on file. Original "
                f"reasoning: {original}"
            )
        if db_type != "APT":
            result["functional_apt_override_used"] = True
            result["reverse_conversion_used"] = result.get("reverse_conversion_detected") == "yes"
    elif concentration == "individual_owner_present" and result.get("determined_type") == "APT":
        original = result.get("reasoning", "")
        result["determined_type"] = db_type
        result["decision"] = "Confirmed" if db_type in ("COA", "HOA") else "Not Enough Info"
        result["reasoning"] = (
            f"Automatically corrected per §2.1: at least one individually-owned unit was found, "
            f"so this cannot be APT no matter how small a fraction of the building is "
            f"bulk-owned -- a real individual-owner relationship exists either way. Original "
            f"reasoning: {original}"
        )

    return result


TIER3_EXCEPTION_CONDITIONS_TOTAL = 4
# When a single (insufficient-alone) Tier 1/2 source also supports the same conclusion, the bar
# relaxes from all four §4.1 conditions to at least this many -- see tier3_partial_tier12_support.
TIER3_EXCEPTION_MIN_CONDITIONS_WITH_PARTIAL_TIER12 = 3


def _tier3_exception_condition_failures(row: dict, result: dict, direction: str) -> list:
    """Evaluates each of the exception's four conditions independently (identical structure for
    both directions; only condition 3's polarity differs, handled inside the backstop) and
    returns a list of human-readable failure descriptions for the ones that DON'T hold (empty
    list if all four hold). Condition 3's evaluation folds in the deterministic backstop
    cross-check so a self-report contradicted by the property's own data counts as a failure of
    that condition, not a separate, always-fatal check -- this lets it participate correctly in
    the forward direction's partial-Tier-1/2-support relaxation (still one condition failing,
    same as any other)."""
    failures = []

    distinct_sources = set(result.get("sources", []) or [])
    if len(distinct_sources) < TIER3_EXCEPTION_MIN_LISTED_SOURCES:
        failures.append(
            f"condition 1: only {len(distinct_sources)} distinct URL(s) were actually listed in "
            f"`sources` -- the exception requires {TIER3_EXCEPTION_MIN_LISTED_SOURCES}+ actually-"
            f"listed sources, not just a self-reported count claiming that many"
        )
    elif (_parse_number(result.get("tier3_independent_source_count")) or 0) < TIER3_EXCEPTION_MIN_SOURCES:
        failures.append("condition 1: self-reported independent source count is below 3")
    elif result.get("tier3_name_address_anchor_confirmed") != "yes":
        failures.append("condition 1: no source was confirmed to tie the property name and address together")

    if direction == "to_apt" and result.get("tier3_sales_evidence_found") == "yes":
        failures.append(
            "condition 2: individual unit sale listings (or planned/entitled-for-sale framing) "
            "were found, which directly contradicts an APT conclusion regardless of what else was found"
        )
    elif result.get("tier3_contradicting_evidence") != "no":
        failures.append("condition 2: contradicting evidence was found, or this wasn't explicitly ruled out")

    corroboration_text = _norm_text(result.get("tier3_internal_db_corroboration"))
    if not corroboration_text:
        failures.append("condition 3: no internal DB field corroboration was cited")
    elif direction == "to_apt" and "fee" not in corroboration_text.lower():
        failures.append(
            "condition 3: forward-direction corroboration must be based on the null/blank "
            "Master_Monthly Association Fees field -- the DB's Owner/Cleaned Owner field is not "
            "reliable enough to use as evidence toward an APT designation (a majority-but-not-"
            "full owner is often still the DB's sole listed Owner)"
        )
    else:
        backstop_reason = _tier3_exception_backstop_failure(row, result, direction)
        if backstop_reason:
            failures.append(f"condition 3: {backstop_reason}")

    if result.get("tier3_structural_edge_case_ruled_out") != "yes":
        failures.append("condition 4: a structural edge case wasn't explicitly ruled out")

    return failures


def _tier3_exception_direction_valid(determined_type: str, direction: str) -> bool:
    if direction == "to_apt":
        return determined_type == "APT"
    if direction == "to_coa_hoa":
        return determined_type in ("COA", "HOA")
    return False


def _enforce_tier3_override_guardrail(row: dict, result: dict) -> dict:
    """Section 4's rule -- 'a single Tier 3 source is never sufficient to override the DB
    label' -- restated as code, with two narrow, directional exceptions also enforced in code
    rather than left to the model's bare word:

    - Forward ('to_apt', §4.1): an unregistered rental community mislabeled HOA/COA, where no
      Tier 1/2 evidence can ever exist. Normally requires all four self-reported conditions;
      relaxes to 3-of-4 when tier3_partial_tier12_support == "yes" (a single, insufficient-alone
      Tier 1/2 source also points the same way).
    - Reverse ('to_coa_hoa'): a genuine COA/HOA mislabeled APT. A stricter, last-resort path,
      gated on tier3_reverse_attempt2_exhausted == "yes" (a real Tier 1/2 search attempt that
      found nothing) before the same four conditions are even considered -- no partial-credit
      relaxation here, since unlike the forward case a real association should normally have
      discoverable Tier 1/2 records.

    An Override resting on Tier 3 evidence (alone, or alongside a single supporting-but-
    insufficient Tier 1/2 source) is downgraded to Not Enough Info unless one of these two paths
    is satisfied. Always sets result["tier3_exception_used"] so the batch summary can isolate
    this highest-risk override path per §4.1/§9."""
    result = dict(result)
    result["tier3_exception_used"] = False

    if result.get("decision") != "Override" or result.get("evidence_tier_used") not in ("Tier 3", "Mixed"):
        return result

    if result.get("tier3_exception_invoked") != "yes":
        # Not attempting either exception at all. A pure-Tier-3 Override can never stand
        # without one. A "Mixed"-tier Override that isn't invoking one is instead relying on
        # ordinary Tier 1/2 corroboration for a normal override -- not this guardrail's concern,
        # EXCEPT: the schema tells the model tier3_internal_db_corroboration must be an empty
        # string whenever tier3_exception_invoked is "no" (internal DB fields are only ever valid
        # corroboration inside one of the two explicitly-gated exception paths, where they're
        # cross-checked against the row's own data). A non-empty value here means the model named
        # an internal DB field as its evidence for an "ordinary" override anyway -- exactly the
        # backwards pattern a real failure exhibited ("Reserve at Falcon Point," DB: APT,
        # overridden to COA on "a recurring monthly association fee indicates an HOA/COA
        # governance structure" with tier3_exception_invoked "no" and evidence_tier_used "Mixed").
        if result.get("evidence_tier_used") == "Tier 3":
            return _downgrade_tier3_override(result, "did not invoke either bounded exception")
        if _norm_text(result.get("tier3_internal_db_corroboration")):
            return _downgrade_tier3_override(
                result,
                "the model cited internal DB field corroboration "
                f"({result.get('tier3_internal_db_corroboration')!r}) for an override that isn't "
                "invoking either bounded exception -- internal DB fields are never valid "
                "evidence for an ordinary override, only as cross-checked corroboration inside "
                "one of the two explicitly-gated exception paths",
            )
        return result

    direction = result.get("tier3_exception_direction")
    if not _tier3_exception_direction_valid(result.get("determined_type"), direction):
        return _downgrade_tier3_override(
            result,
            f"tier3_exception_direction ({direction!r}) is missing or inconsistent with "
            f"determined_type ({result.get('determined_type')!r})",
        )

    if direction == "to_coa_hoa" and result.get("tier3_reverse_attempt2_exhausted") != "yes":
        return _downgrade_tier3_override(
            result,
            "the reverse-direction exception requires a genuinely exhausted Attempt 2 (no Tier "
            "1/2 evidence found despite a real search attempt), which wasn't confirmed",
        )

    if direction == "to_apt" and result.get("tier3_sales_listing_search_performed") != "yes":
        return _downgrade_tier3_override(
            result,
            "the forward exception requires an explicit search for individual unit SALE "
            "listings (not just rental listings) before condition 2 can be claimed, which "
            "wasn't confirmed",
        )

    if direction == "to_apt" and not _genuine_sale_search_performed(row, result):
        queries = result.get("_searched_queries") or []
        return _downgrade_tier3_override(
            result,
            "no genuinely targeted individual-unit sale-listing search (an address-specific "
            "'for sale'/'sold' query, or a county assessor/deed lookup) was found among the "
            f"actual search queries issued ({queries!r}), regardless of what tier3_sales_"
            "listing_search_performed claims",
        )

    # No longer an absolute gate: requiring an explicit self-reported "yes, I searched for BOTH
    # a condo AND an HOA" was firing on real, otherwise-solid overrides where a general search
    # for one type would ordinarily have surfaced the other anyway (a real search for "no HOA at
    # this address" and a real search for "no condo association at this address" tend to return
    # the same evidence). tier3_dual_association_search_performed is still collected for manual
    # QC visibility, but no longer downgrades on its own -- the other three conditions (source
    # count/anchor, contradicting evidence, structural edge case) remain the actual safety net.
    if direction == "to_apt" and _has_legal_entity_name(row.get("Master_Property Name")) \
            and result.get("tier3_entity_name_registry_search_performed") != "yes":
        return _downgrade_tier3_override(
            result,
            "the property's own name contains a formal legal-entity string, which requires a "
            "state business registry search for that exact entity name before the forward "
            "exception can be considered, and that search wasn't confirmed",
        )

    failures = _tier3_exception_condition_failures(row, result, direction)
    passed_count = TIER3_EXCEPTION_CONDITIONS_TOTAL - len(failures)
    # The 3-of-4 partial-credit relaxation is specific to the forward direction -- a genuine
    # COA/HOA should normally have discoverable Tier 1/2 records, so the reverse direction
    # (already gated on an exhausted Attempt 2 above) always needs all four conditions.
    partial_tier12 = (
        direction == "to_apt"
        and result.get("evidence_tier_used") == "Mixed"
        and result.get("tier3_partial_tier12_support") == "yes"
    )
    required_passes = (
        TIER3_EXCEPTION_MIN_CONDITIONS_WITH_PARTIAL_TIER12 if partial_tier12 else TIER3_EXCEPTION_CONDITIONS_TOTAL
    )

    if passed_count < required_passes:
        reason = (
            f"only {passed_count}/{TIER3_EXCEPTION_CONDITIONS_TOTAL} conditions held (needed "
            f"{required_passes}{' with partial Tier 1/2 support' if partial_tier12 else ''}) -- "
            f"{'; '.join(failures)}"
        )
        return _downgrade_tier3_override(result, reason)

    # Enough conditions genuinely check out -- allow the override, but confidence is capped at
    # Medium regardless of what the model submitted, even with partial Tier 1/2 support.
    if result.get("confidence") == "High":
        result["confidence"] = "Medium"
    result["tier3_exception_used"] = True
    return result


def _downgrade_tier3_override(result: dict, failure_reason: str) -> dict:
    result = dict(result)
    original = result.get("reasoning", "")
    result["decision"] = "Not Enough Info"
    result["confidence"] = "Low"
    result["tier3_exception_used"] = False
    result["reasoning"] = (
        f"Automatically downgraded: the model concluded Override via a bounded Tier-3 exception, "
        f"but {failure_reason}. Original reasoning: {original}"
    )
    return result


def _enforce_minimum_sources_guardrail(result: dict) -> dict:
    """No Override may rest on zero cited external sources, regardless of which evidence tier or
    exception path it claims. _enforce_tier3_override_guardrail() deliberately does not check an
    Override whose evidence_tier_used is "Mixed" and that isn't invoking either bounded Tier-3
    exception -- that combination is meant to mean genuine, ordinary Tier 1/2 evidence, which
    isn't this guardrail's concern. A real, previously-mishandled failure exploited exactly that
    gap: a DB-APT record ("Reserve at Falcon Point") was overridden to COA with evidence_tier_used
    "Mixed" and reasoning citing ONLY the DB's own Master_Monthly Association Fees field ("a
    recurring monthly association fee indicates an HOA/COA governance structure") -- `sources` was
    empty, meaning no external record was ever actually found or cited. The database's own fields
    describe what's being verified, not proof of it, and must never substitute for a genuine
    external citation (see the Evidence hierarchy section of the prompt). This is deliberately the
    loosest possible bar (>=1 URL, not the Tier-3 exception's stricter 3+) -- an ordinary Tier 1/2
    override already requires at least one corroborating source per the evidence hierarchy itself;
    this just makes that a real, code-enforced floor for every Override, not only the Tier-3/Mixed
    ones the tier3 guardrail already covers."""
    if result.get("decision") != "Override":
        return result
    if set(result.get("sources", []) or []):
        return result
    result = dict(result)
    original = result.get("reasoning", "")
    result["decision"] = "Not Enough Info"
    result["confidence"] = "Low"
    result["reasoning"] = (
        "Automatically downgraded: this Override cited zero external sources. The database's own "
        "fields (e.g. Master_Monthly Association Fees, Owner) describe what's being verified, not "
        "proof of it, and can never substitute for an actual external record -- every Override "
        f"must cite at least one source it was genuinely found in. Original reasoning: {original}"
    )
    return result


def _reconcile_decision_and_type(db_type: str, result: dict) -> dict:
    """Self-contradiction check, mirroring the dedup tool's hard consistency check: an Override
    whose determined_type matches the DB label isn't actually an override (fix to Confirmed), and
    a Confirmed or Not Enough Info whose determined_type differs from the DB label is a direct
    contradiction (fix determined_type back to the DB label -- neither of those decisions changes
    the label)."""
    result = dict(result)
    decision = result.get("decision")
    determined = result.get("determined_type")
    if decision == "Override" and determined == db_type:
        result["decision"] = "Confirmed"
    elif decision in ("Confirmed", "Not Enough Info") and determined != db_type:
        result["determined_type"] = db_type
    return result


def _enforce_multi_name_guardrail(row: dict, db_type: str, result: dict) -> dict:
    """Some DB records combine multiple distinct, separately-named communities under one
    `Master_Property Name` (comma-separated, e.g. "White Oak Villas, South Cottage Village") --
    only an Override where the model researched EVERY sub-name separately and they ALL
    independently agree is allowed to stand. If even one sub-name disagrees, wasn't researched,
    or couldn't be confirmed, the record stays at the DB label -- the address on file may only
    directly correspond to one of the sub-names, so evidence for that one alone says nothing
    about the other(s). Whether this record actually has multiple sub-names is determined here in
    code (from the row's own data, via _property_name_parts()), not left to the model to notice on
    its own -- format_property() also tells the model explicitly when this applies."""
    if len(_property_name_parts(_norm_text(row.get("Master_Property Name")))) < 2:
        return result
    if result.get("decision") != "Override":
        return result
    if result.get("multi_name_all_agree") == "yes":
        return result

    result = dict(result)
    original = result.get("reasoning", "")
    result["decision"] = "Not Enough Info"
    result["determined_type"] = db_type
    result["multi_name_blocked"] = True
    result["reasoning"] = (
        f"Automatically downgraded: this record combines multiple distinct sub-names, and an "
        f"override requires every sub-name to be researched separately and agree -- that wasn't "
        f"confirmed (multi_name_all_agree was not 'yes'). Original reasoning: {original}"
    )
    return result


def _enforce_hoa_coa_naming_match(row: dict, db_type: str, result: dict) -> dict:
    """Final tiebreaker, applied only once an Override from APT to COA/HOA already stands on its
    own merits: if the property's own name contains an HOA-specific or COA-specific keyword,
    align determined_type to match the name rather than whichever of the two the model happened
    to pick. COA and HOA are similar enough in practice that once an override to "some kind of
    association" is already justified, the name itself is the most reliable signal for which of
    the two it actually is -- more reliable than the model's independent guess. This never affects
    whether an override happens or what evidence justified it, only which of COA/HOA it lands on."""
    if result.get("decision") != "Override" or db_type != "APT":
        return result
    determined = result.get("determined_type")
    if determined not in ("COA", "HOA"):
        return result
    name = _norm_text(row.get("Master_Property Name"))
    is_hoa_named = _contains_any(name, NAME_HOA_KEYWORDS)
    is_coa_named = _contains_any(name, NAME_COA_KEYWORDS)
    if is_hoa_named and not is_coa_named:
        name_type = "HOA"
    elif is_coa_named and not is_hoa_named:
        name_type = "COA"
    else:
        return result
    if determined == name_type:
        return result
    result = dict(result)
    result["determined_type"] = name_type
    result["reasoning"] = (
        f"{result.get('reasoning', '')} (Adjusted from {determined} to {name_type} to match "
        f"the property name -- COA/HOA naming is treated as authoritative for which of the two "
        f"once an override is already justified.)"
    ).strip()
    return result


def decision_display(db_type: str, result: dict) -> str:
    """The two-value decision string shown in the output: 'Confirmed' whenever the DB label
    stands (Confirmed or Not Enough Info internally -- both mean no change, and Not Enough Info's
    thin-evidence nature is already visible via Low confidence and the reasoning text), or
    'Changed from X to Y' when the label was actually overridden."""
    determined = result.get("determined_type", db_type)
    if result.get("decision") == "Override" and determined != db_type:
        return f"Changed from {db_type} to {determined}"
    return "Confirmed"


def _default_error_result(db_type: str, error: Exception) -> dict:
    """Any exception, timeout, or unparseable model output defaults to Not Enough Info and
    keeps the existing DB label -- never a forced override, per Section 8."""
    return {
        "determined_type": db_type,
        "decision": "Not Enough Info",
        "confidence": "Low",
        "evidence_tier_used": "None",
        "reasoning": f"{error.__class__.__name__}: {error}",
        "sources": [],
    }


def process_property(client, model: str, row: dict, url_cache: dict) -> dict:
    property_id = str(row.get("RecordID", ""))
    db_type = _norm_text(row.get("Master_Ownership Type")).upper()
    triggers = compute_triggers(row)
    trigger_types = summarize_trigger_types(triggers)

    try:
        result = research_property(client, row, triggers, url_cache, model)
        if result.get("decision") not in DECISION_LABELS:
            raise ValueError(f"Model returned invalid decision label: {result.get('decision')!r}")
        if result.get("determined_type") not in DETERMINED_TYPE_LABELS:
            raise ValueError(f"Model returned invalid determined_type: {result.get('determined_type')!r}")
        if result.get("structural_edge_case") not in STRUCTURAL_EDGE_CASE_LABELS:
            raise ValueError(f"Model returned invalid structural_edge_case: {result.get('structural_edge_case')!r}")
        if result.get("tier3_exception_direction") not in TIER3_EXCEPTION_DIRECTIONS:
            raise ValueError(f"Model returned invalid tier3_exception_direction: {result.get('tier3_exception_direction')!r}")
        if result.get("ownership_concentration") not in OWNERSHIP_CONCENTRATION_LABELS:
            raise ValueError(f"Model returned invalid ownership_concentration: {result.get('ownership_concentration')!r}")
        if result.get("multi_name_all_agree") not in YES_NO_NA_LABELS:
            raise ValueError(f"Model returned invalid multi_name_all_agree: {result.get('multi_name_all_agree')!r}")
        if result.get("tier3_sales_listing_search_performed") not in YES_NO_NA_LABELS:
            raise ValueError(
                f"Model returned invalid tier3_sales_listing_search_performed: "
                f"{result.get('tier3_sales_listing_search_performed')!r}"
            )
        if result.get("tier3_sales_evidence_found") not in YES_NO_NA_LABELS:
            raise ValueError(f"Model returned invalid tier3_sales_evidence_found: {result.get('tier3_sales_evidence_found')!r}")
        if result.get("ownership_concentration_verified_externally") not in YES_NO_NA_LABELS:
            raise ValueError(
                f"Model returned invalid ownership_concentration_verified_externally: "
                f"{result.get('ownership_concentration_verified_externally')!r}"
            )
        if result.get("ownership_concentration_contradicting_evidence") not in YES_NO_NA_LABELS:
            raise ValueError(
                f"Model returned invalid ownership_concentration_contradicting_evidence: "
                f"{result.get('ownership_concentration_contradicting_evidence')!r}"
            )
        if result.get("tier3_dual_association_search_performed") not in YES_NO_NA_LABELS:
            raise ValueError(
                f"Model returned invalid tier3_dual_association_search_performed: "
                f"{result.get('tier3_dual_association_search_performed')!r}"
            )
        if result.get("tier3_entity_name_registry_search_performed") not in YES_NO_NA_LABELS:
            raise ValueError(
                f"Model returned invalid tier3_entity_name_registry_search_performed: "
                f"{result.get('tier3_entity_name_registry_search_performed')!r}"
            )
        if result.get("apt_override_sale_evidence_found") not in YES_NO_NA_LABELS:
            raise ValueError(
                f"Model returned invalid apt_override_sale_evidence_found: "
                f"{result.get('apt_override_sale_evidence_found')!r}"
            )
        result = _enforce_structural_edge_case_guardrail(db_type, result)
        result = _enforce_coop_mention_guardrail(db_type, result)
        result = _enforce_sales_evidence_guardrail(db_type, result)
        result = _enforce_functional_ownership_guardrail(row, db_type, result)
        result = _enforce_tier3_override_guardrail(row, result)
        result = _enforce_minimum_sources_guardrail(result)
        result = _enforce_apt_override_sale_evidence_guardrail(row, result)
        result = _reconcile_decision_and_type(db_type, result)
        result = _enforce_hoa_coa_naming_match(row, db_type, result)
        result = _enforce_multi_name_guardrail(row, db_type, result)
        result = _enforce_master_planned_community_guardrail(row, db_type, result, url_cache)
        is_error = False
    except Exception as e:
        result = _default_error_result(db_type, e)
        is_error = True

    return {
        "property_id": property_id,
        "db_listed_type": db_type,
        "trigger_rules": [t["rule"] for t in triggers],
        "trigger_count": len(triggers),
        "trigger_types": trigger_types,
        "determined_type": result.get("determined_type", db_type),
        "decision": result.get("decision", "Not Enough Info"),
        "decision_display": decision_display(db_type, result),
        "confidence": result.get("confidence", "Low"),
        "evidence_tier_used": result.get("evidence_tier_used", "None"),
        "reasoning": result.get("reasoning", ""),
        "sources": result.get("sources", []),
        "tier3_exception_used": result.get("tier3_exception_used", False),
        "functional_apt_override_used": result.get("functional_apt_override_used", False),
        "reverse_conversion_used": result.get("reverse_conversion_used", False),
        "master_planned_override_blocked": result.get("master_planned_override_blocked", False),
        "multi_name_blocked": result.get("multi_name_blocked", False),
        "sales_evidence_override_blocked": result.get("sales_evidence_override_blocked", False),
        "is_error": is_error,
    }


# --- I/O, checkpointing, batching -----------------------------------------------------------

def load_input(path: str) -> pd.DataFrame:
    if path.lower().endswith((".xlsx", ".xls")):
        df = pd.read_excel(path, dtype=str)
    else:
        df = pd.read_csv(path, dtype=str, encoding="utf-8-sig")
    if "RecordID" not in df.columns:
        raise SystemExit("Input is missing required 'RecordID' column.")
    return df


def load_checkpoint(path: Path) -> dict:
    results = {}
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                record = json.loads(line)
                results[record["property_id"]] = record
    return results


def append_checkpoint(path: Path, record: dict, lock: threading.Lock):
    with lock:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")


ILLEGAL_XLSX_CHARS_RE = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F]")


def sanitize_for_excel(value):
    if isinstance(value, str):
        return ILLEGAL_XLSX_CHARS_RE.sub("", value)
    return value


def sanitize_df_for_excel(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for col in df.columns:
        if df[col].dtype == object:
            df[col] = df[col].map(sanitize_for_excel)
    return df


def build_output_df(df: pd.DataFrame, results_by_id: dict) -> pd.DataFrame:
    """Appends the verification result columns, in the requested order, after the original
    input columns. No property-ID or trigger/archetype columns -- those stay internal (used for
    the checkpoint and the batch summary's sanity checks) but aren't part of the reviewer-facing
    output. 'Decision' shows the two-value display string ("Confirmed" / "Changed from X to Y"),
    not the internal decision label."""
    out = df.copy()
    for col in ["DB Listed Type", "Determined Type", "Decision", "Confidence", "Evidence Tier Used",
                "Reasoning", "Sources"]:
        out[col] = pd.Series([""] * len(out), index=out.index, dtype=object)

    for idx in out.index:
        result = results_by_id.get(str(out.at[idx, "RecordID"]))
        if not result:
            continue
        out.at[idx, "DB Listed Type"] = result["db_listed_type"]
        out.at[idx, "Determined Type"] = result["determined_type"]
        out.at[idx, "Decision"] = result["decision_display"]
        out.at[idx, "Confidence"] = result["confidence"]
        out.at[idx, "Evidence Tier Used"] = result["evidence_tier_used"]
        out.at[idx, "Reasoning"] = result["reasoning"]
        out.at[idx, "Sources"] = "; ".join(result.get("sources", []))
    return out


def compute_summary(results_by_id: dict) -> dict:
    total = len(results_by_id)
    by_decision = {label: 0 for label in DECISION_LABELS}
    by_rule = {}          # rule -> {"total": n, "override": n}
    by_trigger_count = {}  # count -> {"total": n, "override": n}
    by_signal_type = {}    # signal type -> {"total": n, "override": n}
    errors = 0
    tier3_exception_overrides = 0
    functional_apt_overrides = 0
    reverse_conversion_overrides = 0
    master_planned_blocked = 0
    multi_name_blocked = 0
    sales_evidence_blocked = 0

    for result in results_by_id.values():
        by_decision[result["decision"]] = by_decision.get(result["decision"], 0) + 1
        if result.get("is_error"):
            errors += 1
        if result.get("tier3_exception_used"):
            tier3_exception_overrides += 1
        if result.get("functional_apt_override_used"):
            functional_apt_overrides += 1
        if result.get("reverse_conversion_used"):
            reverse_conversion_overrides += 1
        if result.get("master_planned_override_blocked"):
            master_planned_blocked += 1
        if result.get("multi_name_blocked"):
            multi_name_blocked += 1
        if result.get("sales_evidence_override_blocked"):
            sales_evidence_blocked += 1

        is_override = result["decision"] == "Override"
        for rule in result["trigger_rules"]:
            entry = by_rule.setdefault(rule, {"total": 0, "override": 0})
            entry["total"] += 1
            if is_override:
                entry["override"] += 1
        for stype in result["trigger_types"]:
            entry = by_signal_type.setdefault(stype, {"total": 0, "override": 0})
            entry["total"] += 1
            if is_override:
                entry["override"] += 1
        count_bucket = "3+" if result["trigger_count"] >= 3 else str(result["trigger_count"])
        entry = by_trigger_count.setdefault(count_bucket, {"total": 0, "override": 0})
        entry["total"] += 1
        if is_override:
            entry["override"] += 1

    return {
        "total_properties": total,
        "by_decision": by_decision,
        "override_rate_by_rule": by_rule,
        "override_rate_by_trigger_count": by_trigger_count,
        "override_rate_by_signal_type": by_signal_type,
        "errors": errors,
        "tier3_exception_overrides": tier3_exception_overrides,
        "functional_apt_overrides": functional_apt_overrides,
        "reverse_conversion_overrides": reverse_conversion_overrides,
        "master_planned_blocked": master_planned_blocked,
        "multi_name_blocked": multi_name_blocked,
        "sales_evidence_blocked": sales_evidence_blocked,
    }


EXPECTED_OVERRIDE_RATE = 0.0285  # ~733 / 25,800 per the spec's corrected scoping (Section 1)


def _pct(count: int, total: int) -> str:
    if not total:
        return f"0% ({count}/{total})"
    return f"{round(count / total * 100)}% ({count}/{total})"


def print_summary(label: str, summary: dict):
    total = summary["total_properties"]
    print(f"\n=== {label} ({total} properties) ===")
    for decision, count in summary["by_decision"].items():
        print(f"  {decision}: {_pct(count, total)}")
    overrides = summary["by_decision"].get("Override", 0)
    override_rate = overrides / total if total else 0
    print(f"  Overall override rate: {_pct(overrides, total)} (expected ~{EXPECTED_OVERRIDE_RATE * 100:.2f}%)")
    if total and override_rate > EXPECTED_OVERRIDE_RATE * 3:
        print(
            "  WARNING: override rate is running dramatically higher than the expected ~2.85% -- "
            "this likely means the tool is repeating the prior tool's mistake of over-trusting "
            "Tier 3 marketing evidence. Tighten the prompt before scaling up (Section 9 QC step)."
        )
    print(f"  Errors: {_pct(summary['errors'], total)}")

    tier3_exceptions = summary.get("tier3_exception_overrides", 0)
    print(f"  Tier-3 Corroborated Override (§4.1 bounded exception): {_pct(tier3_exceptions, total)} "
          f"of all properties, {_pct(tier3_exceptions, overrides)} of overrides")
    if tier3_exceptions:
        print(
            "  NOTE: this is the highest-risk override path in the tool -- it changes a label "
            "without ever finding Tier 1/2 evidence. Oversample these specifically during the §9 "
            "QC pass rather than trusting them at the same rate as ordinary overrides."
        )

    functional_apt = summary.get("functional_apt_overrides", 0)
    print(f"  Legally Condo, Functionally Apartment (§2.1 Rule A): {_pct(functional_apt, total)} "
          f"of all properties, {_pct(functional_apt, overrides)} of overrides")
    reverse_conversions = summary.get("reverse_conversion_overrides", 0)
    print(f"    of which Reverse Conversion (formerly individually owned, now bulk-owned): "
          f"{_pct(reverse_conversions, functional_apt)}")

    master_planned_blocked = summary.get("master_planned_blocked", 0)
    print(f"  Master-Planned Mixed Community overrides blocked (§5.7 backstop): "
          f"{_pct(master_planned_blocked, total)} of all properties")
    multi_name_blocked = summary.get("multi_name_blocked", 0)
    print(f"  Multi-name records blocked for lack of agreement across sub-names: "
          f"{_pct(multi_name_blocked, total)} of all properties")
    sales_evidence_blocked = summary.get("sales_evidence_blocked", 0)
    print(f"  Sale-listing overrides to APT blocked (sale evidence found): "
          f"{_pct(sales_evidence_blocked, total)} of all properties")

    print("  Override rate by trigger rule:")
    for rule, counts in sorted(summary["override_rate_by_rule"].items()):
        print(f"    {rule}: {_pct(counts['override'], counts['total'])}")

    print("  Override rate by signal type (sanity check -- Structural should run higher than Naming/Marketing):")
    for stype, counts in sorted(summary["override_rate_by_signal_type"].items()):
        print(f"    {stype}: {_pct(counts['override'], counts['total'])}")
    structural = summary["override_rate_by_signal_type"].get("Structural")
    naming = summary["override_rate_by_signal_type"].get("Naming")
    if structural and naming and structural["total"] and naming["total"]:
        structural_rate = structural["override"] / structural["total"]
        naming_rate = naming["override"] / naming["total"]
        if structural_rate < naming_rate:
            print(
                "  WARNING: Structural override rate is NOT higher than Naming override rate -- "
                "this is the opposite of what the spec expects if the tool is weighting evidence "
                "correctly (naming/leasing triggers should be least reliable)."
            )

    print("  Override rate by trigger count:")
    for count_bucket, counts in sorted(summary["override_rate_by_trigger_count"].items()):
        print(f"    {count_bucket} trigger(s): {_pct(counts['override'], counts['total'])}")


# A property's Master Source, per the same flag priority used by the prior dedup tool:
# Hotwire takes precedence over CoStar, which takes precedence over First American; a
# property matching none of the three is "Other". Column names match the CLP DB export.
MASTER_SOURCE_PRIORITY = [
    ("In HW", "Hotwire"),
    ("In Costar", "CoStar"),
    ("In FA", "First American"),
]


def _is_flag_true(value) -> bool:
    return _parse_number(value) == 1


def _master_source(row) -> str:
    for column, label in MASTER_SOURCE_PRIORITY:
        if _is_flag_true(row.get(column)):
            return label
    return "Other"


def compute_master_source_breakdown(out_df: pd.DataFrame) -> dict:
    """Master Source counts across only the CHANGED rows (Decision != "Confirmed") in the
    built output dataframe -- ordered Hotwire/CoStar/First American/Other, all four always
    present (even at 0) so a batch with, say, zero CoStar-sourced changes still shows that
    explicitly rather than omitting the row."""
    counts = {label: 0 for _, label in MASTER_SOURCE_PRIORITY}
    counts["Other"] = 0
    if "Decision" not in out_df.columns:
        return counts
    changed = out_df[out_df["Decision"] != "Confirmed"]
    for _, row in changed.iterrows():
        source = _master_source(row)
        counts[source] = counts.get(source, 0) + 1
    return counts


def compute_summary_stats(results_by_id: dict) -> dict:
    """Confirmed-vs-changed counts for the "Summary" sheet, reusing decision_display() as the
    single source of truth for what counts as a real change -- so this can never drift from what
    the per-row "Decision" column actually shows. changes_by_transition is keyed "X to Y" (DB
    label to determined type) and only contains transitions that actually occur in this batch."""
    total = len(results_by_id)
    confirmed = 0
    changed = 0
    changes_by_transition = {}
    for result in results_by_id.values():
        db_type = result.get("db_listed_type")
        if decision_display(db_type, result) == "Confirmed":
            confirmed += 1
        else:
            changed += 1
            transition = f"{db_type} to {result.get('determined_type')}"
            changes_by_transition[transition] = changes_by_transition.get(transition, 0) + 1
    return {
        "total": total,
        "confirmed": confirmed,
        "changed": changed,
        "changes_by_transition": changes_by_transition,
    }


def build_summary_stats_df(stats: dict, master_source_counts: dict) -> pd.DataFrame:
    """One row for % Confirmed and % Changed (both as a fraction of the total batch), followed
    by one row per distinct transition direction that actually occurred, each as a fraction of
    the *changed* count -- so those sub-rows sum to the "Changed" percentage above them. Then a
    Master Source breakdown, also as a fraction of the changed count, covering only the changed
    properties (per Hotwire/CoStar/First American/Other precedence)."""
    total = stats["total"]
    changed_total = stats["changed"]
    rows = [
        {"Metric": "Confirmed", "Value": _pct(stats["confirmed"], total)},
        {"Metric": "Changed", "Value": _pct(stats["changed"], total)},
    ]
    for transition, count in sorted(stats["changes_by_transition"].items()):
        rows.append({"Metric": f"  {transition}", "Value": _pct(count, changed_total)})
    rows.append({"Metric": "Master Source (changed properties)", "Value": ""})
    for _, label in MASTER_SOURCE_PRIORITY:
        rows.append({"Metric": f"  {label}", "Value": _pct(master_source_counts.get(label, 0), changed_total)})
    rows.append({"Metric": "  Other", "Value": _pct(master_source_counts.get("Other", 0), changed_total)})
    return pd.DataFrame(rows, columns=["Metric", "Value"])


def write_output(out_df: pd.DataFrame, results_by_id: dict, output_path: str):
    out_df = sanitize_df_for_excel(out_df)
    summary_df = sanitize_df_for_excel(
        build_summary_stats_df(compute_summary_stats(results_by_id), compute_master_source_breakdown(out_df))
    )
    if output_path.lower().endswith((".xlsx", ".xls")):
        with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
            summary_df.to_excel(writer, sheet_name="Summary", index=False)
            out_df.to_excel(writer, sheet_name="Results", index=False)
    else:
        out_df.to_csv(output_path, index=False)
        summary_path = str(Path(output_path).with_suffix("")) + "_summary_stats.csv"
        summary_df.to_csv(summary_path, index=False)
        print(f"Summary stats also written to {summary_path}")


def main():
    parser = argparse.ArgumentParser(description="Verify CLP ownership-type records via an LLM.")
    parser.add_argument("--input", required=True, help="Path to input CSV or XLSX file")
    parser.add_argument("--output", required=True, help="Path to output CSV or XLSX file (never overwrites input)")
    parser.add_argument("--checkpoint", default=None, help="Checkpoint JSONL path (default: <output>.checkpoint.jsonl)")
    parser.add_argument("--batch-size", type=int, default=None,
                         help="Only process up to N new (not-yet-checkpointed) properties this run (staged rollout)")
    parser.add_argument("--limit", type=int, default=None, help="Alias for --batch-size")
    parser.add_argument("--concurrency", type=int, default=1, help="Number of properties to research in parallel (default: 1)")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"OpenAI model id (default: {DEFAULT_MODEL})")
    parser.add_argument("--restart", action="store_true", help="Ignore any existing checkpoint and reprocess everything")
    args = parser.parse_args()

    if str(Path(args.input).resolve()) == str(Path(args.output).resolve()):
        raise SystemExit("Output path must differ from input path.")
    if not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("Set the OPENAI_API_KEY environment variable.")

    batch_size = args.batch_size if args.batch_size is not None else args.limit
    checkpoint_path = Path(args.checkpoint) if args.checkpoint else Path(str(args.output) + ".checkpoint.jsonl")

    df = load_input(args.input)
    all_rows = df.to_dict("records")
    total = len(all_rows)
    print(f"Loaded {args.input}: {total} property row(s).", flush=True)

    results_by_id = {} if args.restart else load_checkpoint(checkpoint_path)
    if results_by_id:
        print(f"Resuming from checkpoint: {len(results_by_id)} property(ies) already completed.", flush=True)
    if args.restart and checkpoint_path.exists():
        checkpoint_path.unlink()

    pending = [row for row in all_rows if str(row.get("RecordID")) not in results_by_id]
    if batch_size is not None:
        pending = pending[:batch_size]
    print(f"{len(pending)} property(ies) to process this run.", flush=True)

    client = OpenAI()
    url_cache = {}
    checkpoint_lock = threading.Lock()
    done_count = 0
    batch_results = {}

    def run_one(row):
        result = process_property(client, args.model, row, url_cache)
        append_checkpoint(checkpoint_path, result, checkpoint_lock)
        return result

    if args.concurrency <= 1:
        for row in pending:
            result = run_one(row)
            results_by_id[result["property_id"]] = result
            batch_results[result["property_id"]] = result
            done_count += 1
            print(f"[{done_count}/{len(pending)}] {result['property_id']}: {result['decision']} "
                  f"({result['determined_type']}, {result['confidence']})", flush=True)
    else:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            futures = {pool.submit(run_one, row): row for row in pending}
            for future in as_completed(futures):
                result = future.result()
                results_by_id[result["property_id"]] = result
                batch_results[result["property_id"]] = result
                done_count += 1
                print(f"[{done_count}/{len(pending)}] {result['property_id']}: {result['decision']} "
                      f"({result['determined_type']}, {result['confidence']})", flush=True)

    processed_ids = {str(row.get("RecordID")) for row in all_rows}
    cumulative_results = {pid: r for pid, r in results_by_id.items() if pid in processed_ids}
    out_df = build_output_df(df, cumulative_results)
    write_output(out_df, cumulative_results, args.output)

    if batch_results:
        print_summary("This batch", compute_summary(batch_results))
    print_summary("Cumulative (all properties processed so far, this input file)", compute_summary(cumulative_results))
    print(f"\nResults written to {args.output}")
    print(f"Checkpoint: {checkpoint_path}")


if __name__ == "__main__":
    main()
