"""
CLP Duplicate Property Research Tool.

For each candidate duplicate pair in an input spreadsheet, calls an LLM (with a
web search tool enabled) to research both properties online and decide whether
they are a genuine database duplicate. See the spec this was built from for the
full research procedure and false-positive ruleset embedded in SYSTEM_PROMPT
below. Supports either Anthropic (Claude) or OpenAI as the backend — pick
whichever provider you have an API key for.

Usage:
    export ANTHROPIC_API_KEY=...      # if using Claude
    export OPENAI_API_KEY=...         # if using OpenAI
    python duplicate_research.py --input pairs.xlsx --output results.xlsx
    python duplicate_research.py --input pairs.csv --output results.csv --limit 10   # dry run

The provider is auto-detected from whichever API key env var is set. If both are
set, pass --provider explicitly. Model defaults to DEFAULT_MODELS[provider]; override
with --model.
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

import anthropic
import openai
from openai import OpenAI

DEFAULT_MODELS = {
    "anthropic": "claude-sonnet-5",
    "openai": "gpt-4o",
}
MAX_SEARCHES_PER_PAIR = 8
MAX_TURNS = 6
URL_FETCH_TIMEOUT = 10
URL_FETCH_MAX_CHARS = 3000

DECISION_LABELS = ["Duplicate", "Not Duplicate", "Not Enough Info"]

SYSTEM_PROMPT = """You are a research assistant for a real estate database team investigating \
candidate duplicate property pairs from a Community Lending Portfolio (CLP) database.

## Objective

You will be given two database records (a "pair") that a separate matching workflow flagged as a \
likely duplicate based on name, address, distance, and unit-count similarity. That similarity score \
is only a weak starting signal — it reflects surface-level similarity on those four inputs alone, \
not a validated duplicate. Your job is to independently research both properties online and decide \
whether they are genuinely the same property (a database duplicate) or two distinct properties that \
were incorrectly flagged as a match.

The default posture is skeptical of "Duplicate." The team would rather leave an ambiguous pair \
unresolved than wrongly remove a real, distinct property. For every pair, exhaustively check the pair \
against the False Positive Ruleset below — actively looking for reasons the two records are NOT the \
same property — before concluding that they are. A shared attribute already present in the database \
(name, unit count, address, property type) is a starting signal, never proof on its own. It is fine to \
conclude "Duplicate" when the evidence genuinely supports it, but that conclusion must survive having \
been checked against every applicable false-positive pattern first.

Never conclude Duplicate based solely on matching fields already present in the database — independent, \
external corroboration is required every time.

**This skepticism cuts both ways.** Being skeptical of "Duplicate" does not mean being credulous of "Not \
Duplicate" — a specific false-positive archetype (especially Parent/Child Mismatch or Separate Children \
Within One Complex) needs the same kind of direct, confirmed evidence that Duplicate does. It is just as \
wrong to invent a confident-sounding but unconfirmed story for why two records must be different (e.g. \
"likely in different sections of the community," "consistent with one being the master association and \
the other a specific building") as it is to invent one for why they must be the same. If you don't have \
real evidence for either specific conclusion, the honest answer is Not Enough Info — not whichever \
direction happens to sound more cautious.

## Research procedure

For each pair:

1. Search each record independently — by property name AND by address separately (don't assume the \
database's name is correct). Look for: the property's own official website or HOA/condo association \
site; county property/tax records; real estate listing platforms (Zillow, Redfin, Realtor.com, Compass, \
Apartments.com, Homes.com); HOA/condo association directories; local news or developer press coverage.
2. **Always specifically try to find an independent, third-party source stating a unit count** for the \
property/complex — this is one of your standard searches for every pair, not something you only look for \
opportunistically, and it deserves real effort before you give up on it. Try multiple angles if the first \
search doesn't turn one up: "[name] total units," "[name] how many units," the HOA/condo association's own \
site or registry filing, county property appraiser or tax assessor records (which often state a unit count \
for the parcel), and local news or developer coverage of the original construction. This is the single most \
decisive test in the whole ruleset, so treat "I didn't find one" as a last resort, not a first guess.
   **The source matters as much as the number.** A number is only a real "total for the complex/HOA" if it \
comes from something describing the SPECIFIC GOVERNING ENTITY the records belong to — the HOA/condo \
association's own site, its registry filing, a declaration/plat document, or news coverage of that specific \
association. General real estate marketing or neighborhood-overview pages (e.g. "homes for sale in \
[neighborhood]" on a realty site, a builder's marketing page for a broader master-planned community) are \
NOT reliable for this, even when they use the same name — they often describe a larger area than the \
specific HOA/association in your two records (e.g. an entire multi-phase development built by one builder, \
of which your specific HOA is only one section). If the only source you find is this kind of page, that is \
functionally the same as not having found a total at all — do not treat its number as if it were an \
authoritative total. (This is why a search once turned up "790 units" for Grand Creek from a realty listings \
page about homes in the Grand Creek neighborhood, when the actual records were for the ~260-268 unit Grand \
Creek Property Owners Association specifically — a different, smaller scope entirely.)
   If you find one, compare it against BOTH records, not just one: the test is whether that total is \
reasonably close to *both* paired records' unit counts. A total that closely matches only ONE of the two \
records, while the other record's count is substantially different, is NOT evidence that the two records \
are the same property. If, after a genuine multi-angle attempt, you still cannot find any independent \
unit-count source at all, say so explicitly and concisely in the evidence summary — that absence should \
meaningfully lower your confidence (see the Guardrails below), especially when the two records' own unit \
counts don't already closely agree with each other. Two records with substantially different unit counts \
and no independent total to reconcile them is weak evidence for Duplicate, not something to wave past with \
an unverified generalization (e.g. "this type of property often varies by phase/section") — if anything, a \
real possibility that the two addresses represent different phases or sections is a reason to suspect they \
may be distinct entities, not a reason to conclude they're the same one.
3. Determine if there is one governing entity or two. Search for the legal HOA/condo association \
name(s) tied to each address.
4. Check geographic plausibility. When DISTANCE_MILES is provided (already computed — do not recalculate \
it), a large distance combined with different cities/states is a red flag pointing toward a coincidental \
match or data error, not a real duplicate. Some datasets don't include DISTANCE_MILES at all — when it's \
missing, judge geographic plausibility from Lat/Lon or the addresses themselves instead; its absence is \
never by itself a reason to conclude Not Enough Info.
5. **Before writing your final evidence summary, cross-check every number in it.** For any unit count or \
total you're about to cite as support, confirm two things: (a) the source you're citing actually states \
that number in its own text — never restate a record's own input field back as if an independent source \
had separately confirmed it, and never invent a number that doesn't trace to something you actually read; \
(b) if that number is meant to represent a shared total, check it against BOTH input records' own stated \
unit counts as given to you — if it doesn't correspond to either one, it does not confirm anything about \
this pair, and you should say so rather than using it as evidence. A source-derived number that contradicts \
both input records is a sign you're looking at the wrong source or misremembering it, not evidence of \
anything about this pair.

Prefer a small number of well-targeted searches over exhaustively crawling many pages — 2-4 searches \
per record is usually enough if well chosen (property name + city, address alone, "[name] homeowners \
association", "[name] units"). If a pair cannot be resolved with confidence after reasonable searching, \
stop and label it Not Enough Info rather than continuing to dig indefinitely.

## False Positive Ruleset (check exhaustively before concluding "Duplicate")

Before invoking any of the three multi-building archetypes below (Parent/Child Mismatch, Separate Children \
Within One Complex, Multi-Use Building) — all of which assume the complex actually consists of more than one \
physical building — check whether the complex is actually multi-building. The database's own Building Count \
field(s) (e.g. Master_Building Count_50+), if present, are a useful starting signal and cross-check worth \
looking at, but they are not the decisive evidence and a value of 1 does not by itself rule out these \
archetypes: a record's own Building Count describes that ONE record's building, and in a genuine "Tower \
1"/"Tower 2" pair each individual record could reasonably show a Building Count of 1 (each tower IS one \
building) while the pair still correctly represents two separate buildings in one complex. The decisive \
evidence should be independent, online research confirming how many physical buildings the complex actually \
has — not the DB field read in isolation, and not external search results accepted uncritically either. If \
you find an online source (not a DB field) that specifically confirms the property is a single building, \
that is strong evidence against these three archetypes; treat that as more decisive than the DB's own \
Building Count field, which is only a prompt to go verify, not a substitute for verifying.

1. **Parent/Child Mismatch** — one record refers to a specific building while the other encompasses the \

1. **Parent/Child Mismatch** — one record refers to a specific building while the other encompasses the \
full multi-building complex. This archetype requires BOTH of the following to be independently confirmed \
— if either is missing, do not use it:
   - **Magnitude**: one record's unit count is a small FRACTION of the other's (e.g. a single ~50-unit \
building within a ~700-unit complex) — not merely "somewhat different." Two counts that are within the \
same order of magnitude of each other (e.g. 200 vs. 251) do not satisfy this.
   - **Naming/documentary signal**: something explicitly marks one record as the sub-building and the \
other as the whole — e.g. one name carries a specific building/section designation ("Building A," "Bldg \
1") that the other lacks, or you independently confirm one record is registered as the master association \
while the other is a specifically identified sub-unit within it. A name that's simply a variant or \
formatted differently (e.g. "X" vs. "X Association, Inc.") is NOT this signal.
   If you can't independently confirm both, this archetype does not apply — search "[complex name] \
[Building A]" for that building's own confirmed unit count to try to establish it, but if you can't, prefer \
Not Enough Info (if the picture is genuinely unclear) or Duplicate/Separate Buildings (if the two records' \
counts are each already reasonably close to each other or to a found total) over guessing Parent/Child.
   When you do conclude this archetype, set `flagged_record_id` to whichever record's RecordID is the \
child/specific-building one (not the parent/master-association one) — the two records aren't peers here, \
so say which is which rather than leaving it to be inferred from prose.
   **A record's Address being one specific point (a house, lot, or street number) within a larger HOA/\
community's boundaries is NOT a naming/documentary signal, and does NOT mean that record "represents a \
subset" or "a specific sub-block" of the community.** For a single-family-home HOA especially, the address \
on file for a whole-community record is very often just one representative location within the \
association's boundaries (an entrance, an office, or simply one of the member lots) — not evidence of a \
smaller scope. Do not write reasoning like "Record B's address aligns with a specific sub-block, indicating \
it represents a subset of the total units" — that inference does not follow from an address alone, and \
asserting it does not create the naming/documentary signal this archetype actually requires. If a cited \
total unit count is close to BOTH records individually (not just one), that is the "Separate Buildings" \
signature (see Duplicate Archetypes below), not Parent/Child Mismatch, regardless of how different the two \
addresses look — do not let an address-based story override arithmetic that says otherwise.
2. **Separate Children Within One Complex** — two genuine peer buildings (e.g. "Tower 1" vs "Tower 2," \
"Building A" vs "Building B"). Like Parent/Child Mismatch, this requires BOTH of the following:
   - **Naming/documentary signal**: independent confirmation that two separately identified, physically \
distinct buildings actually exist — e.g. explicit "Tower 1"/"Tower 2" or "Building A"/"Building B" \
designations confirmed via a site plan, HOA/condo registry, or news coverage, not just inferred from the \
two DB records' own addresses.
   - **Magnitude relative to a found TOTAL for the whole complex** (not relative to each other): when an \
authoritative total is found, each record's own count should be a plausible fraction of that total — not \
already close to the full total on its own. A record whose count is already close to the confirmed total \
for the ENTIRE complex is describing the whole property by itself, not one child within it — that's \
Duplicate/Separate Buildings instead (see below).
   **Important:** the two records' unit counts matching (or nearly matching) EACH OTHER is NOT disqualifying \
by itself, and is not the test — do not treat it as automatic evidence of Duplicate. Sibling buildings built \
to the same or a similar design commonly have the same or similar unit counts as one another; a confirmed \
"Tower 1"/"Tower 2" pair with matching counts is still perfectly consistent with two genuine siblings. What \
actually distinguishes the two situations is the naming/documentary signal above, plus whether a found total \
for the complex corresponds to a SINGLE record (Duplicate) or is roughly consistent with combining both \
records rather than either one alone (genuine siblings).
   Do not conclude Not Duplicate here merely because the two records list different building-level \
addresses, and do not invent an unconfirmed "different section" or "different sub-association" story to \
explain a unit-count gap (or a unit-count match) you can't actually verify — that is exactly the kind of \
speculative reasoning the Objective above forbids. If you can't confirm the naming/documentary signal, \
prefer Not Enough Info or Duplicate over guessing this archetype.
   Watch for this exact trap: both records share the IDENTICAL property name (no "Tower 1"/"Tower 2" or \
"Building A"/"Building B" distinction at all), and the only thing you found is that a "total" for the \
complex doesn't match either record individually — e.g. records of 74 and 120 units, with a found "total" \
of 277 that doesn't match either one AND doesn't match their sum (194) either. Do the actual arithmetic \
before writing anything about this: 74 is ~27% of 277, 120 is ~43% of 277, and 74+120=194 is ~70% of 277 — \
none of that is "fitting as fractions of the total" in any meaningful sense, it's just three numbers that \
don't relate to each other. Do not reach for this archetype here: there is no naming signal (both records \
are named identically), and the numbers don't even cohere internally, which means the "277" you found \
probably doesn't reliably describe this specific pair at all. That combination — no naming signal, \
non-additive/incoherent numbers — is a Not Enough Info situation, not a confident Separate Children (or \
confident anything) call. If you catch yourself writing a phrase like "fits as a fraction of the total" or \
"aligns with the total," stop and check: does the actual percentage support that phrase, or are you writing \
a conclusion-shaped sentence without having verified it? Only the former is acceptable.
3. **Separate Property Types Within a Master Association** — a master complex comprised of separate \
sub-properties sharing a name but with different property types (e.g. a SFU/HOA section and a separate \
COA section under one community brand). First confirm whether a master association actually exists \
(search "[name] master association", "[name] community association", or the HOA/condo registry) — do \
not assume one exists just from similar names. A confirmed master association does NOT by itself mean \
the two records are the same entity — it may have separate COA/HOA sub-entities underneath with similar \
but not identical unit counts. Only after confirming whether a master association exists should you apply \
the unit-count test: a total close to *both* records points toward one entity (Mislabeled Property), \
while confirmed evidence of two separately named/managed sub-associations supports this archetype. The \
type mismatch alone is never sufficient.
4. **Coincidental Name Match** — two properties share name/unit-count/other factors purely coincidentally \
while being fundamentally different, unrelated developments. Search "[property name] [city 1]" and \
"[property name] [city 2]" independently — unrelated management companies/websites on each side support \
coincidence. A property-type/ownership mismatch alongside a matching name and unit count is a strong tell.
5. **Mislabeled Property** (a.k.a. "Incorrect Property Name") — this archetype means TWO real, distinct \
properties exist, and one record's Property Name was incorrectly copied from/confused with the other. The \
deciding question is: does independent research on the "wrong" record's address turn up a *different, \
unrelated, confirmed real property* — i.e. a second genuine, independently identifiable property that \
just happens to have gotten the wrong name? If yes, that's this archetype (Not Duplicate). Search the \
address directly (not the name) to find that property's real, independently confirmed name/type. Set \
`flagged_record_id` to that record's RecordID (the one with the wrong name) — the two records aren't \
symmetric here, so identify the specific one rather than leaving it to be inferred from prose.
   Do NOT use this archetype for a bad *address* rather than a bad *name*: if the "wrong" record's address \
doesn't correspond to any real, distinct second property at all (e.g. it doesn't exist, or every source \
you find for it redirects back to the SAME single building as the paired record), there is only ONE real \
property here, not two — that's "Same Building" under Duplicate Archetypes below, a Duplicate, not a false \
positive. The distinction is not the word "mislabeled" — an address error and a name error can both \
reasonably be called "mislabeled" in casual language — the distinction is whether independent research \
turns up a second real property (Mislabeled Property, Not Duplicate) or not (Same Building, Duplicate).
   Also do NOT use this archetype just because the two Property Names are similar-but-not-identical \
variants of each other (e.g. "Pelican Cove" vs. "Pelican Cove Condominium", or the presence/absence of \
"Association, Inc."); that kind of naming-convention difference is not a mislabel, and if the two records' \
addresses turn out to be two different buildings/addresses within the same complex, the correct archetype \
is "Separate Buildings" under Duplicate Archetypes below, not this one.
   **Hard consistency check, because this has been gotten wrong before:** if your own evidence_summary \
says something like "Record A's address does not correspond to any independently identifiable property" \
and "both records describe the same physical property," your Decision MUST be "Duplicate" with Archetype \
"Same Building" — not "Not Duplicate" with Archetype "Mislabeled Property." Writing that reasoning and then \
labeling it Not Duplicate/Mislabeled Property is a direct self-contradiction: you have just described the \
"Same Building" scenario (one real property, one address is a data-entry error) in your own words, and then \
mislabeled it as the opposite scenario (two real properties, one misnamed). Before finalizing, reread your \
own evidence_summary and check which scenario it actually describes; make the Decision and Archetype match \
what you wrote, not just whatever label the surface pattern reminds you of.
6. **Multi-Use Building** — a single building has multiple properties with different managers, possibly \
different property types (e.g. residential tower over separately-owned commercial/retail). Check: \
identical/near-identical address; different ownership type or drastically different unit counts at the \
same address; different property managers. Search "[building name] condo declaration" or "[address] \
residential commercial".

This list is not exhaustive — if a pair doesn't fit any of these but you find genuine evidence it isn't \
a match, describe the reasoning in your own words as a new archetype rather than forcing it into one of \
the categories above (note that it's a new archetype).

## Duplicate Archetypes (recognizing genuine matches, not just false positives)

Just as the ruleset above describes patterns that only *look* like a duplicate, genuine duplicates also \
tend to fall into a few recurring patterns. When the Decision is "Duplicate," use one of these (or your \
own accurate label if none fit):

- **Separate Buildings** — the two records list different addresses/buildings within a single \
multi-building complex, but they represent the same overall property, not two distinct entities. The core \
test is whether an authoritative source states a total for the whole complex that is close to *both* \
records (as a rule of thumb, within about 10-15% of each) — that is the signature of "two entry points \
into one property." This applies even when the Property Names are variants of each other (e.g. "Pelican \
Cove" vs. "Pelican Cove Condominium") — a naming-convention difference is not by itself evidence of two \
different properties, and is not grounds for "Mislabeled Property" either.
  Both paired records showing the same (or nearly the same) unit count as each other, with NO confirmed \
naming/documentary signal of distinct siblings (see "Separate Children Within One Complex" below), is also \
a signature of this archetype — absent such a signal, there's no other reason two "different" records \
would coincidentally match, so matching counts point to one property counted twice. But when there IS a \
confirmed naming signal for genuine siblings (e.g. independently verified "Tower 1"/"Tower 2" designations), \
matching or near-identical counts between the two records does NOT by itself indicate Duplicate — siblings \
built to the same design commonly share the same unit count. In that situation, look instead at whether a \
found total for the whole complex corresponds to a SINGLE record on its own (Duplicate/Separate Buildings) \
or is roughly consistent with combining both records rather than matching either alone (genuine siblings — \
see "Separate Children Within One Complex," which requires that naming signal plus each record being a \
plausible fraction of any found total, not the two records simply differing or matching each other).
  A found total that is off from BOTH paired records by a wide margin (e.g. more than roughly 20-30%) is \
NOT "close enough," and does not support this archetype — it more likely describes a different, unrelated \
property. Do not round a distant number down to "close" just because it's the best match your search \
turned up.
  You do not need every field independently confirmed to use this archetype — the unit-count test is the \
decisive signal, not a checklist. If a third-party source exactly (or nearly exactly) confirms ONE \
record's unit count and the OTHER record's count is merely in the same ballpark (not off by more than \
roughly 20-30%), and the two records otherwise share a clearly matching name and are geographically close, \
that is sufficient for "Separate Buildings" — just use a lower confidence to reflect that only one side \
was independently confirmed, rather than downgrading the decision itself to Not Enough Info. Reserve Not \
Enough Info for when the found evidence doesn't correspond to either record at all (wrong address, wildly \
different count — see the high-bar guardrail below), not for "confirmed on one side, plausible on the \
other." A modest, non-catastrophic gap between an unconfirmed record's count and the confirmed one is not \
itself suspicious — it's ordinary and expected for real database records, most often just a stale entry \
(e.g. the count as of an earlier renovation or a prior year) or a small data error, not evidence the record \
describes something else. Treat it exactly the way you'd treat any other minor data-quality noise: worth a \
lower confidence, not a reason to reach for a different archetype or a different decision.
- **Same Building** — the two records' addresses look different as plain text (different formatting, an \
alternate entrance, a unit/suite suffix, an old vs. new street-numbering convention, or a plain data-entry \
error) but independent research confirms they are literally the same physical building/address, not two \
addresses within a larger complex. This includes the case where one record's address doesn't correspond \
to any real, distinct second property at all — every source you can find for it redirects back to the \
same single building as the paired record. Use "Same Building" here (a Duplicate), not "Mislabeled \
Property" (a Not Duplicate false positive, reserved for when a second REAL property actually exists — see \
that ruleset entry) and not "Separate Buildings" (reserved for cases where there truly are two distinct \
addresses inside one larger complex).
  Do NOT treat a lack of independent confirmation for the erroneous address as a reason for Not Enough \
Info here — that absence is exactly what this archetype predicts, not a gap that undermines it. A \
data-entry-error address won't correspond to anything real by definition, so failing to find a source for \
it is expected, not concerning. What actually matters is whether the CORRECT record (name, HOA, unit \
count, fee) is well-confirmed as one real building — if so, that supports Same Building at a reasonable \
confidence (per the confidence guardrails below), not a downgrade to Not Enough Info just because the \
wrong address predictably came up empty.
- Other genuine duplicate patterns (e.g. a straightforward data-entry duplicate with no complicating \
factor) — describe in your own words.

## Archetype Labels (use these exact strings)

Archetypes exist so results can be filtered and counted across thousands of pairs — that only works if \
the same underlying pattern gets the exact same label every time. When a pair matches one of the patterns \
below, use the label EXACTLY as written here — not a paraphrase, and not a version with extra qualifiers, \
parentheticals, or pair-specific details tacked on (e.g. write "Separate Buildings," never "Separate \
Buildings within same complex" or "Mislabeled Copies of Same Property – incomplete unit counts"; write \
"Same Building," never "Same Building (Mislabeled Property)"):

- If Decision is "Not Duplicate": Parent/Child Mismatch, Separate Children Within One Complex, Separate \
Property Types Within a Master Association, Coincidental Name Match, Mislabeled Property, or Multi-Use \
Building.
- If Decision is "Duplicate": Separate Buildings or Same Building.

Only write a new, free-text archetype when a pair genuinely fits none of the above (for either decision) \
— and even then, phrase it as a short, reusable description of the *pattern* (so a later pair with the \
same underlying situation would get the identical label), not a description of this one pair's specific \
facts.

## Guardrails

- **Do not weigh differing Master_Monthly Association Fees or Master_Build Year between the two paired \
records as evidence against Duplicate.** HOA/condo fees change over time and different data sources capture \
them at different points, so two records showing different fee figures tells you nothing about whether \
they're the same property — it's normal database noise, not a signal. Build years are similarly unreliable \
as a false-positive signal: they commonly vary across buildings within the same multi-building HOA/complex \
(phased construction, additions, renovations), so a build-year mismatch is expected noise for a many-building \
community, not evidence the two records describe different entities. Neither field should be cited as a \
reason for Not Duplicate or Not Enough Info by itself, and citing "differing fees" or "differing build \
years" as your primary evidence for either of those decisions is a sign you're missing the actual point of \
comparison (name, address, unit count, ownership type, governing entity) and should keep looking instead.
- **Confidence scale — use the whole range, not just the top of it.** Across a batch of many pairs, \
confidence should vary widely based on how clean the evidence actually is. If most or all pairs in a batch \
land at 7 or above, that is a red flag that confidence is being inflated, not a sign the evidence was \
unusually strong across the board — real-world research is rarely that clean. Use this as your anchor:
  - 1-3: little to no real corroboration behind the decision; close to a guess.
  - 4-6: real, relevant evidence was found and does lean toward the decision, but with a genuine gap, an \
unverified assumption, or a loose end somewhere. **This is the normal, expected range for most pairs** — \
not a consolation score you settle for when you wanted higher.
  - 7-8: reserved for evidence with no remaining gap on the decisive test (the unit-count comparison and/or \
the naming/documentary signal), even if some peripheral detail is still unconfirmed.
  - 9-10: rare. Multiple independent sources directly and unambiguously confirm both specific records with \
no gaps or contradictions anywhere.
  Before settling on 7 or higher, explicitly check: is there any loose end, contradiction, or unverified \
assumption left anywhere in this evidence? If yes, that alone should pull you down into the 4-6 range — do \
not round up just because the overall story feels plausible or coheres into a satisfying narrative. \
Plausibility is not confirmation. When genuinely torn between two adjacent confidence values, take the \
lower one — the cost of overstating confidence in a tool like this is higher than the cost of understating \
it.
  Whenever your evidence summary cites a "total" or other aggregate number in support of a conclusion, \
show the actual arithmetic (the specific numbers or percentages compared), not just a qualitative claim \
like "fits as a fraction" or "aligns with the total." If you can't honestly write out numbers that support \
the qualitative claim, don't make the claim — and don't let a confident-sounding sentence substitute for \
having actually checked the math.
- **Confidence must reflect how directly and specifically the sources you found confirm THESE EXACT \
records — not general plausibility.** If any number central to your reasoning doesn't trace to something \
actually stated in a source you read, or contradicts either input record's own stated value, that is \
disqualifying: do not assign high confidence (8+) to a conclusion resting on it. Confidence 9-10 should be \
rare, reserved for cases where multiple independent sources directly and unambiguously confirm both \
specific records with no gaps or contradictions.
  Distinguish two different kinds of "not fully confirmed" evidence — they deserve very different \
confidence levels, and conflating them is a common mistake:
  - **One side has zero independent corroboration.** A source confirms one record's address/count \
exactly, but the other record's address was never found in any source at all — you're relying on \
name/proximity plausibility alone for that side. This is genuinely partial: mid-range confidence (4-6) on \
whichever decision the totality of evidence favors. This is NOT, by itself, a reason to answer Not Enough \
Info — reserve that for evidence that's genuinely ambiguous, contradictory, or too thin to favor either \
decision.
  - **A found total already covers both records, just not identically to each.** E.g. an authoritative \
total of 768 confirmed units, where one paired record matches it exactly and the other is within ~4%. \
This is NOT partial confirmation — a single total that's reasonably close to both paired records' own \
counts is doing exactly the job the unit-count test exists for, for both records at once. This deserves \
confidence toward the high end (7-9, scaling with how tight the match is), not a mid-range score. Do not \
describe a close-but-inexact match to a shared total as "partial" the way you would a record with zero \
corroboration at all — a ~4% gap under one found total is a good match, not a weak one.
  When the numeric match to a found total is looser (say, 15-25% off) but other independent signals are \
strong and consistent — the same governing HOA/condo association independently confirmed for both \
addresses, and no naming/documentary signal suggesting the two addresses are genuinely separate \
sub-buildings — treat that combination as solid support too, in the upper-middle range (6-8), not as thin \
evidence to be capped in the middle just because the unit-count fit isn't exact.
  On the other hand, when the numbers don't make internal sense at all — e.g. a "total" that corresponds \
to neither individual record NOR to combining them — that's a sign the evidence itself is unreliable or \
describes something other than this specific pair. That should push confidence low regardless of which \
decision you lean toward (and often toward Not Enough Info), not toward a confident score on whichever \
archetype the surface pattern superficially resembles.
  Separately: not finding any independent third-party unit-count source at all is its own, separate reason \
to keep confidence out of the high range (below roughly 7), even when every other signal (name match, \
address, HOA identity, property type) lines up cleanly and points to a clear decision. The unit-count \
comparison is the ruleset's single most decisive test; a conclusion reached without ever running it — \
however clean the rest of the picture looks — is missing its most important check and should not score as \
if it weren't. This is different from the found-total scenarios above, where the test WAS run and produced \
a real (if imperfect) result. This penalty compounds when the two records' own unit counts don't already \
closely agree with each other: no independent total AND a real gap between the two records' own counts is \
weak evidence for a unit-count-based Duplicate conclusion (Separate Buildings) or a unit-count-based false \
positive (Separate Children), and should push confidence well below 7, not just "not quite as high." Note \
that this specific combination — no cited total, and a real gap between the records — is also checked and \
capped automatically after you submit, so there's no benefit to rating it higher than the evidence supports.
- **The bar for concluding "Duplicate" must be high — but "high" means the evidence must actually \
correspond to these two records, not that every field must be independently re-confirmed one by one.** \
Only conclude Duplicate when your evidence — taken as a whole (name match, geographic proximity, and the \
unit-count test) — actually corresponds to THESE EXACT queried addresses, not a nearby address on the same \
street, a similarly-named development, or a general community page that doesn't check out numerically. A \
source describing a different street number AND a substantially different unit count than both of your \
records does NOT satisfy this, even if it's the closest or only result your search turned up — proximity \
or name similarity to a real nearby development is not evidence that development IS one of your two \
records. For example: if your two records are at 52 and 10 Country Club Drive with 72 units each, and the \
only source you find describes a 160-unit complex at 1200 Country Club Drive, that source does not \
confirm anything about your two records — the address doesn't match either one and the unit count is off \
by more than double. Do not use it as support for "Separate Buildings" or any other Duplicate archetype; \
label the pair Not Enough Info and say so plainly in the evidence summary.
  Contrast that with a source that DOES correspond to one of your two records specifically (matching name, \
matching or near-matching unit count, right location) even if the other record wasn't independently \
found — that is a real, if partial, match, and should lead to Duplicate at reduced confidence per the \
guardrail above, not Not Enough Info. The test is whether your evidence corresponds to these records at \
all, not whether it corresponds completely.
- When a stated total unit count is found, it only counts as strong evidence of a single shared \
community when it is close to *both* paired records — not just one. "Close" means within roughly 10-15%; \
farther off than that does not count, no matter how confident the source otherwise seems.
- Before using any found source (a stated unit count, a confirmed name, a total) as evidence, confirm \
that source's address actually corresponds to the specific record's address you searched for. If the most \
relevant source you can find describes a different address, a subset of the complex, or an unclear scope \
relative to the two paired addresses, do not force a Duplicate or Not Duplicate conclusion from it — that \
mismatch is itself a reason to prefer Not Enough Info over a guess.
- **A Master_Ownership Type mismatch between the two input records (e.g. one is HOA, the other COA) is a \
real red flag against Duplicate, not a detail to note in passing.** It's exactly the kind of signal \
"Separate Property Types Within a Master Association" exists to check for — it means the two records may \
describe legally distinct entities, even if they're geographically close. When you see this mismatch, hold \
Duplicate to a higher bar than usual: you need independent confirmation that the record with the \
differing ownership type is specifically, officially part of the SAME development/association as the \
other record — not just that it's nearby. Shared parcel data, a shared block/lot number, or general \
proximity does NOT clear this bar by itself: adjacent or even overlapping parcels can legally belong to \
distinct associations, especially in dense developments, so "same parcel" is suggestive, not dispositive, \
when ownership types disagree. If you can't find that specific confirmation, prefer Not Enough Info (or \
Mislabeled Property, if you separately confirm the differing-ownership record is a genuinely distinct, \
real association) over concluding Duplicate on proximity alone.
- When in doubt between two decisions, prefer the more conservative one (the one less likely to result \
in a real property being wrongly removed from the database).
- Do not fabricate or guess at sources — if a claim can't be tied to something actually found online, \
don't include it in the evidence summary.
- A pre-fetched URL from the database (labeled "Pre-fetched content") is a head start, not proof — verify \
it actually belongs to the property in question before relying on it.

## Decision labels

Use exactly one of: Duplicate, Not Duplicate, Not Enough Info.

## Final answer

Once you have completed your research, call the submit_assessment tool exactly once with your final \
conclusion for the pair as a whole (not per-record — both records in the pair share the same decision, \
archetype, confidence, evidence summary, and sources). Do not call it before you're done researching, \
and do not just describe your answer in plain text — the tool call is the only way your answer is \
recorded.
"""

SUBMIT_TOOL_DESCRIPTION = (
    "Submit the final duplicate-property research decision for this candidate pair. "
    "Call exactly once, after research is complete."
)

SUBMIT_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {
            "type": "string",
            "enum": DECISION_LABELS,
        },
        "archetype": {
            "type": "string",
            "description": (
                "One of the exact canonical strings from the system prompt's 'Archetype Labels' "
                "section whenever the pair matches that pattern -- never a paraphrase or a version "
                "with extra qualifiers/parentheticals appended. For 'Not Duplicate': 'Parent/Child "
                "Mismatch', 'Separate Children Within One Complex', 'Separate Property Types Within "
                "a Master Association', 'Coincidental Name Match', 'Mislabeled Property', or "
                "'Multi-Use Building'. For 'Duplicate': 'Separate Buildings' or 'Same Building'. Do "
                "not default to 'Mislabeled Property' just because the two Property Names differ; "
                "reserve that one for a genuinely different, independently confirmed name. Only use "
                "a new free-text label (for either decision) when the pair genuinely fits none of "
                "the above, and keep it as a short, reusable pattern description rather than "
                "pair-specific detail. For 'Not Enough Info', briefly describe what's missing."
            ),
        },
        "confidence": {
            "type": "integer",
            "minimum": 1,
            "maximum": 10,
            "description": "1 = pure guess, 10 = certain, directly confirmed by an authoritative source.",
        },
        "evidence_summary": {
            "type": "string",
            "description": (
                "STRICT LIMIT: 2-4 sentences, no more -- this is a summary, not a research writeup. "
                "Plain prose only: no markdown links, no inline citations or footnotes, no multiple "
                "paragraphs or line breaks. Source URLs belong in the separate `sources` field, not "
                "inline here. State the specific facts found and how they support the conclusion; if "
                "you're tempted to go past 4 sentences, cut detail rather than add a paragraph break."
            ),
        },
        "sources": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Specific URLs or named sources used. Empty list if none.",
        },
        "flagged_record_id": {
            "type": "string",
            "description": (
                "For archetypes where ONE specific record is the anomaly and the other is the "
                "normal reference point -- 'Mislabeled Property' (the record with the wrong "
                "name/address) and 'Parent/Child Mismatch' (the record that's the child/specific-"
                "building one, not the parent/master-association one) -- set this to that record's "
                "exact RecordID (copy it verbatim from the input, not 'Record A' or 'Record B'). "
                "Leave as an empty string for every other archetype, including symmetric ones like "
                "'Separate Buildings', 'Same Building', 'Coincidental Name Match', or 'Separate "
                "Children Within One Complex', where neither record is more anomalous than the other."
            ),
        },
        "cited_total_units": {
            "type": "string",
            "description": (
                "If your reasoning for 'Separate Buildings' or 'Separate Children Within One "
                "Complex' relies on an authoritative third-party TOTAL unit count for the whole "
                "complex, write that exact number here as a plain string (e.g. '768'). This is "
                "cross-checked in code against both records' own unit counts, so it must be the "
                "literal number you found and used -- not a rounded or paraphrased figure. Leave "
                "as an empty string if no such total was used in your reasoning."
            ),
        },
    },
    "required": ["decision", "archetype", "confidence", "evidence_summary", "sources", "flagged_record_id",
                 "cited_total_units"],
    "additionalProperties": False,
}

# --- Anthropic tool shapes ---
ANTHROPIC_WEB_SEARCH_TOOL = {
    "type": "web_search_20250305",
    "name": "web_search",
    "max_uses": MAX_SEARCHES_PER_PAIR,
}
ANTHROPIC_SUBMIT_TOOL = {
    "name": "submit_assessment",
    "description": SUBMIT_TOOL_DESCRIPTION,
    "input_schema": SUBMIT_SCHEMA,
}

# --- OpenAI (Responses API) tool shapes ---
OPENAI_WEB_SEARCH_TOOL = {"type": "web_search"}
OPENAI_SUBMIT_TOOL = {
    "type": "function",
    "name": "submit_assessment",
    "description": SUBMIT_TOOL_DESCRIPTION,
    "parameters": SUBMIT_SCHEMA,
    "strict": True,
}

RETRYABLE_ANTHROPIC_ERRORS = (
    anthropic.RateLimitError,
    anthropic.APIConnectionError,
    anthropic.InternalServerError,
    anthropic.APITimeoutError,
)

RETRYABLE_OPENAI_ERRORS = (
    openai.RateLimitError,
    openai.APIConnectionError,
    openai.InternalServerError,
    openai.APITimeoutError,
)


def call_anthropic_with_backoff(client, **kwargs):
    delay = 2.0
    last_err = None
    for attempt in range(5):
        try:
            return client.messages.create(**kwargs)
        except RETRYABLE_ANTHROPIC_ERRORS as e:
            last_err = e
            sleep_for = delay * (2 ** attempt) + random.uniform(0, 1)
            print(f"    API error ({e.__class__.__name__}), retrying in {sleep_for:.1f}s...", flush=True)
            time.sleep(sleep_for)
    raise last_err


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


def fetch_url_cached(url: str, cache: dict) -> str | None:
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


INTERNAL_COLUMNS = {"_row_index"}
# Excluded outright rather than just told to ignore in the prompt -- the model should never
# even see these fields, so a misread can't happen. Master_Units_50+ is the authoritative unit
# count; Master_Units_20+ uses a different, less-relevant threshold and must never be used instead.
EXCLUDED_COLUMNS = {"master_units_20+"}


def format_record(row: dict, label: str, url_cache: dict) -> str:
    lines = [f"### {label} (RecordID: {row.get('RecordID', '')})"]
    url_value = None
    for key, value in row.items():
        if key in INTERNAL_COLUMNS or key.strip().lower() in EXCLUDED_COLUMNS or pd.isna(value) or value == "":
            continue
        lines.append(f"- {key}: {value}")
        if "url" in key.lower() and not url_value:
            url_value = str(value)
    if url_value:
        fetched = fetch_url_cached(url_value, url_cache)
        if fetched:
            lines.append(f"- Pre-fetched content from {url_value} (verify it actually belongs to this property):")
            lines.append(f"  \"{fetched}\"")
    return "\n".join(lines)


def build_user_message(record_a: dict, record_b: dict, distance, url_cache: dict) -> str:
    parts = [
        "Research the following candidate duplicate pair and determine whether the two records "
        "describe the same real property.",
        "",
        format_record(record_a, "Record A", url_cache),
        "",
        format_record(record_b, "Record B", url_cache),
        "",
    ]
    has_distance = distance is not None and not pd.isna(distance) and str(distance).strip() != ""
    if has_distance:
        parts.append(f"DISTANCE_MILES between the two records (already computed, do not recalculate): {distance}")
    else:
        parts.append(
            "DISTANCE_MILES was not provided for this pair (this dataset doesn't include that column). "
            "If Lat/Lon fields are present above, use those instead to judge geographic plausibility. "
            "The absence of DISTANCE_MILES specifically is never on its own a reason to conclude Not "
            "Enough Info."
        )
    return "\n".join(parts)


def find_tool_use(content_blocks, name):
    for block in content_blocks:
        if getattr(block, "type", None) == "tool_use" and block.name == name:
            return block
    return None


def extract_sources_from_search(content_blocks):
    sources = []
    for block in content_blocks:
        if getattr(block, "type", None) == "web_search_tool_result":
            result = getattr(block, "content", None)
            if isinstance(result, list):
                for item in result:
                    url = getattr(item, "url", None)
                    if url:
                        sources.append(url)
    return sources


def research_pair_anthropic(client, record_a: dict, record_b: dict, distance: str, url_cache: dict, model: str) -> dict:
    messages = [{"role": "user", "content": build_user_message(record_a, record_b, distance, url_cache)}]
    tools = [ANTHROPIC_WEB_SEARCH_TOOL, ANTHROPIC_SUBMIT_TOOL]
    searched_sources = []

    for turn in range(MAX_TURNS):
        is_last_turn = turn == MAX_TURNS - 1
        tool_choice = {"type": "tool", "name": "submit_assessment"} if is_last_turn else {"type": "auto"}
        response = call_anthropic_with_backoff(
            client,
            model=model,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
        )
        searched_sources.extend(extract_sources_from_search(response.content))
        submit_block = find_tool_use(response.content, "submit_assessment")
        if submit_block:
            result = dict(submit_block.input)
            if not result.get("sources"):
                result["sources"] = sorted(set(searched_sources))
            return result

        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason == "end_turn":
            messages.append({
                "role": "user",
                "content": "Please call submit_assessment now with your final conclusion based on the research so far.",
            })
        # otherwise (e.g. stop_reason == "tool_use" for the server-side web_search tool having
        # already executed within this response) just loop and let the model continue.

    raise RuntimeError("Model did not produce a submit_assessment call within the turn budget")


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


def research_pair_openai(client, record_a: dict, record_b: dict, distance: str, url_cache: dict, model: str) -> dict:
    input_items = [{"role": "user", "content": build_user_message(record_a, record_b, distance, url_cache)}]
    tools = [OPENAI_WEB_SEARCH_TOOL, OPENAI_SUBMIT_TOOL]
    previous_response_id = None
    searched_sources = []

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

        submit_call = find_function_call(response.output, "submit_assessment")
        if submit_call:
            result = json.loads(submit_call.arguments)
            if not result.get("sources"):
                result["sources"] = sorted(set(searched_sources))
            return result

        input_items = [{
            "role": "user",
            "content": "Please call submit_assessment now with your final conclusion based on the research so far.",
        }]

    raise RuntimeError("Model did not produce a submit_assessment call within the turn budget")


def research_pair(provider: str, client, record_a: dict, record_b: dict, distance: str, url_cache: dict, model: str) -> dict:
    if provider == "anthropic":
        return research_pair_anthropic(client, record_a, record_b, distance, url_cache, model)
    return research_pair_openai(client, record_a, record_b, distance, url_cache, model)


def load_input(path: str) -> pd.DataFrame:
    if path.lower().endswith((".xlsx", ".xls")):
        df = pd.read_excel(path, dtype=str)
    else:
        df = pd.read_csv(path, dtype=str, encoding="utf-8-sig")
    return df


def group_pairs(df: pd.DataFrame):
    """Yield (group_id, [row_dict, ...], error_reason_or_None) for each Group Number."""
    if "Group Number" not in df.columns:
        raise SystemExit("Input is missing required 'Group Number' column.")
    for group_id, group_df in df.groupby("Group Number", sort=False):
        rows = group_df.to_dict("records")
        error = None
        if len(rows) != 2:
            error = f"Group {group_id} does not have exactly 2 records (found {len(rows)})."
        else:
            for row in rows:
                if not row.get("RecordID") or pd.isna(row.get("RecordID")):
                    error = f"Group {group_id} has a record missing RecordID."
                elif not row.get("Address") or pd.isna(row.get("Address")):
                    error = f"Missing Address field for RecordID {row.get('RecordID')} (Group {group_id})."
        yield str(group_id), rows, error


def load_checkpoint(path: Path) -> dict:
    results = {}
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                record = json.loads(line)
                results[record["group"]] = record
    return results


def append_checkpoint(path: Path, record: dict, lock: threading.Lock):
    with lock:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")


TOTAL_ARITHMETIC_ARCHETYPES = {"Separate Buildings", "Separate Children Within One Complex"}
TOTAL_ARITHMETIC_MISMATCH_THRESHOLD = 0.25


def _verify_cited_total_arithmetic(record_a: dict, record_b: dict, result: dict) -> dict:
    """Deterministic safety net for a recurring failure: the model citing a "total" unit count
    in support of Separate Buildings / Separate Children Within One Complex that, on the actual
    arithmetic, doesn't correspond to either record individually OR to their sum (e.g. records of
    74 and 120 units "supported" by a cited total of 277 -- 27%, 43%, and 70% off respectively).
    Prose instructions alone haven't reliably stopped this, so it's now checked in code: if the
    cited total is off by more than the threshold from ALL THREE reference points, the conclusion
    is overridden to Not Enough Info rather than trusting a confident-sounding but unsupported claim.
    """
    archetype = result.get("archetype", "")
    if archetype not in TOTAL_ARITHMETIC_ARCHETYPES:
        return result
    total = _parse_number(result.get("cited_total_units"))
    if not total:
        return result
    unit_a = _parse_number(record_a.get("Master_Units_50+"))
    unit_b = _parse_number(record_b.get("Master_Units_50+"))
    if unit_a is None or unit_b is None:
        return result
    ratios = [abs(total - ref) / total for ref in (unit_a, unit_b, unit_a + unit_b)]
    if min(ratios) <= TOTAL_ARITHMETIC_MISMATCH_THRESHOLD:
        return result  # at least one reference point is a reasonable match -- let it stand

    result = dict(result)
    result["decision"] = "Not Enough Info"
    result["archetype"] = "Cited total does not match either record or their sum"
    result["confidence"] = min(int(result.get("confidence", 1)), 3)
    original_evidence = result.get("evidence_summary", "")
    result["evidence_summary"] = (
        f"Automatically overridden: the model concluded '{archetype}' citing a total of "
        f"{total:g} units, but this is not close to Record A's count ({unit_a:g}), Record B's "
        f"count ({unit_b:g}), or their sum ({unit_a + unit_b:g}) -- off by "
        f"{min(ratios) * 100:.0f}% at best. This number likely doesn't reliably describe this "
        f"pair. Original evidence: {original_evidence}"
    )
    return result


NO_TOTAL_CONFIDENCE_CAP = 5
NO_TOTAL_MISMATCH_THRESHOLD = 0.15


def _cap_confidence_when_total_missing(record_a: dict, record_b: dict, result: dict) -> dict:
    """Deterministic safety net for the complementary failure: no third-party total was found at
    all (cited_total_units is empty), the two records' own unit counts don't closely agree with
    each other, and the model still scores confidence high anyway -- e.g. Crestview Park's 74 vs
    120 units (a 38% gap) explained away with an unverified "townhouse complexes vary by phase"
    generalization, at confidence 8. Caps confidence rather than changing the decision, since the
    archetype/decision call may still be reasonable -- it's specifically the confidence that's
    unsupported when the ruleset's most decisive test was never actually run.
    """
    archetype = result.get("archetype", "")
    if archetype not in TOTAL_ARITHMETIC_ARCHETYPES:
        return result
    if _parse_number(result.get("cited_total_units")):
        return result  # a total was cited -- handled by _verify_cited_total_arithmetic instead
    unit_a = _parse_number(record_a.get("Master_Units_50+"))
    unit_b = _parse_number(record_b.get("Master_Units_50+"))
    if unit_a is None or unit_b is None:
        return result
    larger = max(unit_a, unit_b)
    if larger == 0:
        return result
    relative_diff = abs(unit_a - unit_b) / larger
    if relative_diff <= NO_TOTAL_MISMATCH_THRESHOLD:
        return result  # the two records already agree closely -- no total needed to confirm that
    current_confidence = int(result.get("confidence", 1))
    if current_confidence <= NO_TOTAL_CONFIDENCE_CAP:
        return result

    result = dict(result)
    result["confidence"] = NO_TOTAL_CONFIDENCE_CAP
    result["evidence_summary"] = (
        f"{result.get('evidence_summary', '')} [Confidence capped at {NO_TOTAL_CONFIDENCE_CAP}: no "
        f"independent third-party unit-count total was found, and the two records' own counts "
        f"({unit_a:g} vs {unit_b:g}) differ by {relative_diff * 100:.0f}%, too large a gap to treat "
        f"as confirmed without one.]"
    )
    return result


CHILD_ARCHETYPES = {"Parent/Child Mismatch", "Separate Children Within One Complex"}
CHILD_VS_TOTAL_CLOSE_THRESHOLD = 0.15


def _correct_child_archetype_when_total_supports_duplicate(record_a: dict, record_b: dict, result: dict) -> dict:
    """Deterministic correction for a recurring failure: the model concludes Parent/Child Mismatch
    or Separate Children Within One Complex, but its own cited total is close to BOTH records
    individually (e.g. Costa del Sol: a confirmed 768-unit total, matching Record A's 768 exactly
    and Record B's 739 within ~4%) -- that pattern is the definition of Separate Buildings/Duplicate
    (each record independently describes the whole property), not a child being a smaller fraction
    of the whole. A cited total close to only ONE record (or neither) is left alone here -- that's
    genuinely ambiguous or supports the child archetype, not something to override.
    """
    archetype = result.get("archetype", "")
    if archetype not in CHILD_ARCHETYPES:
        return result
    total = _parse_number(result.get("cited_total_units"))
    if not total:
        return result
    unit_a = _parse_number(record_a.get("Master_Units_50+"))
    unit_b = _parse_number(record_b.get("Master_Units_50+"))
    if unit_a is None or unit_b is None:
        return result
    close_to_a = abs(total - unit_a) / total <= CHILD_VS_TOTAL_CLOSE_THRESHOLD
    close_to_b = abs(total - unit_b) / total <= CHILD_VS_TOTAL_CLOSE_THRESHOLD
    if not (close_to_a and close_to_b):
        return result

    result = dict(result)
    result["decision"] = "Duplicate"
    result["archetype"] = "Separate Buildings"
    original_evidence = result.get("evidence_summary", "")
    result["confidence"] = min(int(result.get("confidence", 1)), 6)
    result["evidence_summary"] = (
        f"Automatically corrected: the model labeled this '{archetype}', but its own cited total "
        f"({total:g}) is close to BOTH Record A's count ({unit_a:g}) and Record B's count "
        f"({unit_b:g}) -- that's the signature of both records independently describing the whole "
        f"property, not one being a smaller fraction/child of the other. Original evidence: "
        f"{original_evidence}"
    )
    return result


MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\((?:https?://|www\.)[^)]+\)")
MAX_EVIDENCE_SUMMARY_CHARS = 700


def _clean_evidence_summary(text: str) -> str:
    """Deterministic cleanup for a recurring complaint: evidence summaries running to multiple
    paragraphs with inline markdown citations despite the schema asking for 2-4 plain sentences.
    Strips markdown links down to their label text (URLs belong in the separate `sources` field),
    collapses paragraph breaks into a single line, and hard-caps length at a sentence boundary as
    a last resort when the model still runs long.
    """
    if not text:
        return text
    text = MARKDOWN_LINK_RE.sub(r"\1", text)
    text = " ".join(text.split())
    if len(text) <= MAX_EVIDENCE_SUMMARY_CHARS:
        return text
    truncated = text[:MAX_EVIDENCE_SUMMARY_CHARS]
    cutoff = max(truncated.rfind(". "), truncated.rfind(".—"))
    if cutoff > MAX_EVIDENCE_SUMMARY_CHARS // 2:
        truncated = truncated[: cutoff + 1]
    return truncated.rstrip() + " [truncated for length]"


def process_group(provider, client, model, group_id, rows, error, url_cache):
    if error:
        return {
            "group": group_id,
            "record_ids": [r.get("RecordID") for r in rows],
            "decision": "Not Enough Info",
            "archetype": "Malformed input",
            "confidence": 1,
            "evidence_summary": error,
            "sources": [],
            "flagged_record_id": "",
            "is_error": True,
        }

    record_a, record_b = rows[0], rows[1]
    distance = record_a.get("DISTANCE_MILES", "")
    try:
        result = research_pair(provider, client, record_a, record_b, distance, url_cache, model)
        decision = result.get("decision")
        if decision not in DECISION_LABELS:
            raise ValueError(f"Model returned invalid decision label: {decision!r}")
        result = dict(result)
        result["evidence_summary"] = _clean_evidence_summary(result.get("evidence_summary", ""))
        result = _verify_cited_total_arithmetic(record_a, record_b, result)
        result = _correct_child_archetype_when_total_supports_duplicate(record_a, record_b, result)
        result = _cap_confidence_when_total_missing(record_a, record_b, result)
        decision = result["decision"]
        record_ids = [record_a.get("RecordID"), record_b.get("RecordID")]
        flagged_record_id = str(result.get("flagged_record_id") or "").strip()
        if flagged_record_id and flagged_record_id not in {str(rid) for rid in record_ids}:
            # Model named something other than one of this pair's actual RecordIDs (e.g.
            # hallucinated, or wrote "Record A" literally) -- drop it rather than mislead.
            flagged_record_id = ""
        return {
            "group": group_id,
            "record_ids": record_ids,
            "decision": decision,
            "archetype": result.get("archetype", ""),
            "confidence": int(result.get("confidence", 1)),
            "evidence_summary": result.get("evidence_summary", ""),
            "sources": result.get("sources", []),
            "flagged_record_id": flagged_record_id,
            "is_error": False,
        }
    except Exception as e:
        return {
            "group": group_id,
            "record_ids": [record_a.get("RecordID"), record_b.get("RecordID")],
            "decision": "Not Enough Info",
            "archetype": "Processing error",
            "confidence": 1,
            "evidence_summary": f"{e.__class__.__name__}: {e}",
            "sources": [],
            "flagged_record_id": "",
            "is_error": True,
        }


UNIT_COUNT_CLOSE_THRESHOLD = 10
DATABASE_FLAG_FIELDS = {
    "Both In Hotwire": "In HW",
    "Both In CoStar": "In Costar",
    "Both In First American": "In FA",
}
MASTER_SOURCE_PRIORITY = [
    ("In HW", "Hotwire"),
    ("In Costar", "CoStar"),
    ("In FA", "First American"),
]


def _normalize_text(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return str(value).strip().lower()


def _values_match(a, b) -> str:
    """'Yes'/'No', or '' when either side is missing so a match can't be determined."""
    na, nb = _normalize_text(a), _normalize_text(b)
    if not na or not nb:
        return ""
    return "Yes" if na == nb else "No"


def _parse_number(value):
    try:
        if value is None or (isinstance(value, float) and pd.isna(value)) or str(value).strip() == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _units_within_threshold(a, b) -> str:
    na, nb = _parse_number(a), _parse_number(b)
    if na is None or nb is None:
        return ""
    return "Yes" if abs(na - nb) <= UNIT_COUNT_CLOSE_THRESHOLD else "No"


def _is_flag_true(value) -> bool:
    return _parse_number(value) == 1


def _both_flag_true(a, b) -> str:
    return "Yes" if _is_flag_true(a) and _is_flag_true(b) else "No"


def _master_source(row: dict) -> str:
    for column, label in MASTER_SOURCE_PRIORITY:
        if _is_flag_true(row.get(column)):
            return label
    return "Other"


def _values_differ(a, b) -> str:
    """'Yes'/'No', or '' when either side is missing so a difference can't be determined."""
    match = _values_match(a, b)
    if match == "":
        return ""
    return "No" if match == "Yes" else "Yes"


def _same_master_source(source_a: str, source_b: str) -> str:
    """'Yes' only when both records' Master Source match AND that shared value isn't 'Other' --
    two unmatched "Other" records don't share a real source, so that case is 'No', not 'Yes'."""
    if source_a == "Other" or source_b == "Other":
        return "No"
    return "Yes" if source_a == source_b else "No"


def build_output_df(df: pd.DataFrame, results_by_group: dict) -> pd.DataFrame:
    out = df.copy()
    out["Decision"] = pd.Series([""] * len(out), index=out.index, dtype=object)
    out["Archetype"] = pd.Series([""] * len(out), index=out.index, dtype=object)
    out["Confidence"] = pd.Series([None] * len(out), index=out.index, dtype=object)
    out["Evidence Summary"] = pd.Series([""] * len(out), index=out.index, dtype=object)
    out["Sources"] = pd.Series([""] * len(out), index=out.index, dtype=object)
    out["Flagged As Anomaly"] = pd.Series([""] * len(out), index=out.index, dtype=object)
    out["Address Match"] = pd.Series([""] * len(out), index=out.index, dtype=object)
    out["Name Match"] = pd.Series([""] * len(out), index=out.index, dtype=object)
    out["Unit Counts Within 10"] = pd.Series([""] * len(out), index=out.index, dtype=object)
    out["Different Ownership Type"] = pd.Series([""] * len(out), index=out.index, dtype=object)
    for flag_col in DATABASE_FLAG_FIELDS:
        out[flag_col] = pd.Series([""] * len(out), index=out.index, dtype=object)
    out["Same Master Source"] = pd.Series([""] * len(out), index=out.index, dtype=object)
    out["Master Source"] = pd.Series([""] * len(out), index=out.index, dtype=object)

    for group_id, group_df in out.groupby("Group Number", sort=False):
        result = results_by_group.get(str(group_id))
        if not result:
            continue
        flagged_record_id = str(result.get("flagged_record_id") or "")
        for idx in group_df.index:
            out.at[idx, "Decision"] = result["decision"]
            out.at[idx, "Archetype"] = result["archetype"]
            out.at[idx, "Confidence"] = result["confidence"]
            out.at[idx, "Evidence Summary"] = result["evidence_summary"]
            out.at[idx, "Sources"] = "; ".join(result.get("sources", []))
            if flagged_record_id and str(out.at[idx, "RecordID"]) == flagged_record_id:
                out.at[idx, "Flagged As Anomaly"] = "Yes"

        # Master Source is a per-record field: computed independently for each row,
        # not mirrored across the pair like the other Duplicate-only fields below.
        for idx in group_df.index:
            out.at[idx, "Master Source"] = _master_source(out.loc[idx].to_dict())

        if result["decision"] != "Duplicate" or len(group_df) != 2:
            continue
        row_a, row_b = group_df.iloc[0], group_df.iloc[1]
        address_match = _values_match(row_a.get("Address"), row_b.get("Address"))
        name_match = _values_match(row_a.get("Master_Property Name"), row_b.get("Master_Property Name"))
        units_close = _units_within_threshold(row_a.get("Master_Units_50+"), row_b.get("Master_Units_50+"))
        ownership_differs = _values_differ(row_a.get("Master_Ownership Type"), row_b.get("Master_Ownership Type"))
        idx_a, idx_b = group_df.index[0], group_df.index[1]
        same_master_source = _same_master_source(
            out.at[idx_a, "Master Source"], out.at[idx_b, "Master Source"]
        )
        flag_values = {
            flag_col: _both_flag_true(row_a.get(source_col), row_b.get(source_col))
            for flag_col, source_col in DATABASE_FLAG_FIELDS.items()
        }
        for idx in group_df.index:
            out.at[idx, "Address Match"] = address_match
            out.at[idx, "Name Match"] = name_match
            out.at[idx, "Unit Counts Within 10"] = units_close
            out.at[idx, "Different Ownership Type"] = ownership_differs
            out.at[idx, "Same Master Source"] = same_master_source
            for flag_col, value in flag_values.items():
                out.at[idx, flag_col] = value
    return out


def compute_summary(results_by_group: dict) -> dict:
    total = len(results_by_group)
    by_decision = {label: 0 for label in DECISION_LABELS}
    archetype_counts = {}
    confidences = []
    errors = 0
    for result in results_by_group.values():
        by_decision[result["decision"]] = by_decision.get(result["decision"], 0) + 1
        archetype_counts[result["archetype"]] = archetype_counts.get(result["archetype"], 0) + 1
        confidences.append(result["confidence"])
        if result.get("is_error"):
            errors += 1
    avg_confidence = sum(confidences) / len(confidences) if confidences else 0
    return {
        "total_pairs": total,
        "by_decision": by_decision,
        "average_confidence": round(avg_confidence, 2),
        "archetype_counts": archetype_counts,
        "errors": errors,
    }


DUPLICATE_FLAG_COLUMNS = [
    "Address Match",
    "Name Match",
    "Unit Counts Within 10",
    "Different Ownership Type",
    "Both In Hotwire",
    "Both In CoStar",
    "Both In First American",
    "Same Master Source",
]


def compute_duplicate_flag_summary(out_df: pd.DataFrame) -> dict:
    """Yes/No/Unknown breakdown of the Duplicate-only cross-check flags, one count per pair
    (not per row) -- shows how confirmed duplicates in this batch break out across those checks."""
    dup_df = out_df[out_df.get("Decision", "") == "Duplicate"]
    if "Group Number" in dup_df.columns:
        dup_df = dup_df.drop_duplicates(subset="Group Number")
    flags = {}
    for col in DUPLICATE_FLAG_COLUMNS:
        if col not in dup_df.columns:
            continue
        counts = dup_df[col].value_counts(dropna=False)
        flags[col] = {
            "Yes": int(counts.get("Yes", 0)),
            "No": int(counts.get("No", 0)),
            "Unknown": int(counts.get("", 0)),
        }
    return {"total_duplicate_pairs": len(dup_df), "flags": flags}


def print_summary(summary: dict):
    print("\n=== Run Summary ===")
    print(f"Total pairs processed: {summary['total_pairs']}")
    for label, count in summary["by_decision"].items():
        print(f"  {label}: {count}")
    print(f"Average confidence: {summary['average_confidence']}")
    print(f"Errors: {summary['errors']}")
    print("Archetype breakdown:")
    for archetype, count in sorted(summary["archetype_counts"].items(), key=lambda kv: -kv[1]):
        print(f"  {count:>4}  {archetype}")
    flag_summary = summary.get("duplicate_flags")
    if flag_summary and flag_summary["total_duplicate_pairs"]:
        print(f"\nDuplicate pair flag breakdown (of {flag_summary['total_duplicate_pairs']} duplicate pair(s)):")
        for col, counts in flag_summary["flags"].items():
            line = f"  {col}: Yes={counts['Yes']}  No={counts['No']}"
            if counts["Unknown"]:
                line += f"  Unknown={counts['Unknown']}"
            print(line)


# Characters illegal in XML 1.0 (and therefore in .xlsx cell values) -- LLM output can
# occasionally include stray control characters (e.g. from an unusual token or a copied
# source snippet) that crash openpyxl's writer if left in. Tab/newline/carriage-return are
# valid XML and kept; everything else in the C0 control range is stripped.
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


def write_output(out_df: pd.DataFrame, summary: dict, output_path: str):
    out_df = sanitize_df_for_excel(out_df)
    flag_summary = summary.get("duplicate_flags")
    if output_path.lower().endswith((".xlsx", ".xls")):
        summary_rows = [{"Metric": "Total pairs processed", "Value": summary["total_pairs"]}]
        for label, count in summary["by_decision"].items():
            summary_rows.append({"Metric": f"Decision: {label}", "Value": count})
        summary_rows.append({"Metric": "Average confidence", "Value": summary["average_confidence"]})
        summary_rows.append({"Metric": "Errors", "Value": summary["errors"]})
        for archetype, count in sorted(summary["archetype_counts"].items(), key=lambda kv: -kv[1]):
            summary_rows.append({"Metric": f"Archetype: {archetype}", "Value": count})
        if flag_summary and flag_summary["total_duplicate_pairs"]:
            summary_rows.append({
                "Metric": "Duplicate pairs (flag breakdown below)",
                "Value": flag_summary["total_duplicate_pairs"],
            })
            for col, counts in flag_summary["flags"].items():
                summary_rows.append({"Metric": f"{col}: Yes", "Value": counts["Yes"]})
                summary_rows.append({"Metric": f"{col}: No", "Value": counts["No"]})
                if counts["Unknown"]:
                    summary_rows.append({"Metric": f"{col}: Unknown", "Value": counts["Unknown"]})
        summary_df = sanitize_df_for_excel(pd.DataFrame(summary_rows))
        with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
            summary_df.to_excel(writer, sheet_name="Summary", index=False)
            out_df.to_excel(writer, sheet_name="Results", index=False)
    else:
        out_df.to_csv(output_path, index=False)
        summary_path = str(Path(output_path).with_suffix("")) + "_summary.txt"
        with open(summary_path, "w", encoding="utf-8") as f:
            f.write(f"Total pairs processed: {summary['total_pairs']}\n")
            for label, count in summary["by_decision"].items():
                f.write(f"  {label}: {count}\n")
            f.write(f"Average confidence: {summary['average_confidence']}\n")
            f.write(f"Errors: {summary['errors']}\n")
            f.write("Archetype breakdown:\n")
            for archetype, count in sorted(summary["archetype_counts"].items(), key=lambda kv: -kv[1]):
                f.write(f"  {count:>4}  {archetype}\n")
            if flag_summary and flag_summary["total_duplicate_pairs"]:
                f.write(f"\nDuplicate pair flag breakdown (of {flag_summary['total_duplicate_pairs']} "
                        f"duplicate pair(s)):\n")
                for col, counts in flag_summary["flags"].items():
                    line = f"  {col}: Yes={counts['Yes']}  No={counts['No']}"
                    if counts["Unknown"]:
                        line += f"  Unknown={counts['Unknown']}"
                    f.write(line + "\n")
        print(f"Summary also written to {summary_path}")


def resolve_provider(explicit_provider: str | None) -> str:
    # bool(...) rather than an `in os.environ` check: CI providers (e.g. GitHub Actions
    # referencing an unset secret) can set the env var to an empty string rather than
    # omitting it, which should still count as "not configured".
    has_anthropic_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    has_openai_key = bool(os.environ.get("OPENAI_API_KEY"))

    provider = explicit_provider
    if provider is None:
        if has_anthropic_key and has_openai_key:
            raise SystemExit(
                "Both ANTHROPIC_API_KEY and OPENAI_API_KEY are set — pass --provider anthropic|openai explicitly."
            )
        if has_anthropic_key:
            provider = "anthropic"
        elif has_openai_key:
            provider = "openai"
        else:
            raise SystemExit(
                "Set ANTHROPIC_API_KEY or OPENAI_API_KEY (or pass --provider once one of them is set)."
            )

    if provider == "anthropic" and not has_anthropic_key:
        raise SystemExit("--provider anthropic requires the ANTHROPIC_API_KEY environment variable to be set.")
    if provider == "openai" and not has_openai_key:
        raise SystemExit("--provider openai requires the OPENAI_API_KEY environment variable to be set.")
    return provider


def main():
    parser = argparse.ArgumentParser(description="Research candidate duplicate property pairs via an LLM.")
    parser.add_argument("--input", required=True, help="Path to input CSV or XLSX file")
    parser.add_argument("--output", required=True, help="Path to output CSV or XLSX file (never overwrites input)")
    parser.add_argument("--checkpoint", default=None, help="Checkpoint JSONL path (default: <output>.checkpoint.jsonl)")
    parser.add_argument("--limit", type=int, default=None, help="Only process the first N pairs (dry-run mode)")
    parser.add_argument("--concurrency", type=int, default=1, help="Number of pairs to research in parallel (default: 1)")
    parser.add_argument("--provider", choices=["anthropic", "openai"], default=None,
                         help="LLM backend to use (default: auto-detected from whichever API key env var is set)")
    parser.add_argument("--model", default=None,
                         help="Model id (default: DEFAULT_MODELS[provider], e.g. claude-sonnet-5 or gpt-4o)")
    parser.add_argument("--restart", action="store_true", help="Ignore any existing checkpoint and reprocess everything")
    args = parser.parse_args()

    if str(Path(args.input).resolve()) == str(Path(args.output).resolve()):
        raise SystemExit("Output path must differ from input path.")

    provider = resolve_provider(args.provider)
    model = args.model or DEFAULT_MODELS[provider]
    print(f"Using provider={provider}, model={model}", flush=True)

    checkpoint_path = Path(args.checkpoint) if args.checkpoint else Path(str(args.output) + ".checkpoint.jsonl")

    df = load_input(args.input)
    all_groups = list(group_pairs(df))
    if args.limit is not None:
        all_groups = all_groups[: args.limit]
    total = len(all_groups)
    print(f"Loaded {args.input}: {total} candidate pair(s) to process.", flush=True)

    results_by_group = {} if args.restart else load_checkpoint(checkpoint_path)
    if results_by_group:
        print(f"Resuming from checkpoint: {len(results_by_group)} pair(s) already completed.", flush=True)
    if args.restart and checkpoint_path.exists():
        checkpoint_path.unlink()

    pending = [(gid, rows, err) for gid, rows, err in all_groups if gid not in results_by_group]
    print(f"{len(pending)} pair(s) remaining to process.", flush=True)

    client = anthropic.Anthropic() if provider == "anthropic" else OpenAI()
    url_cache = {}
    checkpoint_lock = threading.Lock()
    done_count = 0

    def run_one(item):
        group_id, rows, error = item
        result = process_group(provider, client, model, group_id, rows, error, url_cache)
        append_checkpoint(checkpoint_path, result, checkpoint_lock)
        return result

    if args.concurrency <= 1:
        for item in pending:
            result = run_one(item)
            results_by_group[result["group"]] = result
            done_count += 1
            print(f"[{done_count}/{len(pending)}] Group {result['group']}: {result['decision']} "
                  f"({result['archetype']})", flush=True)
    else:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            futures = {pool.submit(run_one, item): item for item in pending}
            for future in as_completed(futures):
                result = future.result()
                results_by_group[result["group"]] = result
                done_count += 1
                print(f"[{done_count}/{len(pending)}] Group {result['group']}: {result['decision']} "
                      f"({result['archetype']})", flush=True)

    processed_group_ids = {gid for gid, _, _ in all_groups}
    scoped_results = {gid: r for gid, r in results_by_group.items() if gid in processed_group_ids}
    out_df = df[df["Group Number"].astype(str).isin(processed_group_ids)] if args.limit is not None else df
    out_df = build_output_df(out_df, scoped_results)
    summary = compute_summary(scoped_results)
    summary["duplicate_flags"] = compute_duplicate_flag_summary(out_df)
    write_output(out_df, summary, args.output)
    print_summary(summary)
    print(f"\nResults written to {args.output}")


if __name__ == "__main__":
    main()
