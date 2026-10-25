# ex-USSR Kinship Research

![ex-USSR Kinship Research cover](docs/cover.jpg)

**An evidence-first genealogy skill for AI agents (Codex, Claude Code and other Agent Skills runtimes).** It turns an agent into a careful family-history researcher for the former USSR, the Soviet Union and the Russian Empire. The agent reads church books and civil records, military, deportation and repression files, and unindexed archive scans. It keeps every find traceable, separates records from interpretation and says plainly what is proven and what is not.

The skill's instructions are written in Russian, but **the agent talks to you in your language**: English, Ukrainian, Russian, Polish, German and so on. It quotes sources in their original spelling with a translation, and builds search queries in the languages of the records.

---

## What it does

- **Case intake and scope.** One research question, known facts, who may be alive, what is off-limits.
- **Reproducible registers.** Persons, artifacts, claims, relationships and a full search log (CSV), plus a final report template.
- **Historical jurisdiction model.** It resolves every event to the state, province, district, parish or civil registry *as of the event date*, not today's map.
- **Name-variant matrices.** Pre-reform Russian, Ukrainian, Polish, Romanian, German and Yiddish forms, patronymics, maiden names and transliterations.
- **Source strategy.** Originals first, then official catalogs, indexes, published document collections, and only then trees and forums as leads.
- **Evidence protocol.** Claims get explainable statuses, not percentages: `hypothesis`, `single-source`, `corroborated`, `strongly-corroborated`, `contradicted`, `unresolved`. Copies and mirrors of one record share an `independence_group`, so they never count as independent proof.
- **Identity resolution.** Namesakes stay separate `person_id`s until distinguishing evidence appears: house number, parents, godparents, exact birth date or military unit.
- **Audit script.** It checks referential integrity and flags weak corroboration before you write conclusions. An independence group counts only when a cited artifact belongs to it, so corroboration cannot be declared into existence. It also checks the final report: every cited ID must exist in the registers and every status must match them. A summary shows what is still uncorroborated or marked illegible.
- **Final report.** Every relationship is laid out edge by edge, with contradictions, failed searches and concrete next steps.

## New in 1.3.0

General rules for a complete first pass, photo search and a readable PDF for the family. They were prompted by a real case (a WWII officer), where a family memorial page with a certified service-record extract and photos turned up only after the report was "done", but they apply to any line: peasant, urban, repressed, emigrant or Jewish families, and women's lines.

- **Ask the family first.** Before searching, the agent asks once whether relatives have researched this person before, which trees and pages the family keeps (FamilySearch, Geni, MyHeritage, VGD forum threads, and memorial bases that fit the case: Immortal Regiment, Bessmertny Barak, Posledny Adres, Yad Vashem testimonies) and who holds documents, letters, albums and family records. The answer goes to `prior_family_research` in the intake.
- **Coverage checklists.** `references/source-strategy.md` section 12 has checklists for church books, revision lists and censuses, civil registration after 1918, repression and deportation, evacuation, POWs and Ostarbeiter, emigration, Jewish communities and military service. Every item must be closed in `search-log.csv` as `found`, `not-found`, `blocked` or `skipped` before the report. A rich first database no longer ends the search.
- **Name variants in every database.** е/ё, -ев/-ов, female forms, maiden and married surnames, Polish, Yiddish, German and Romanian spellings, church and everyday first names.
- **Site-internal search for family and memorial pages.** An empty web search does not cover a site's internal database; the agent uses the site's own search when available.
- **Grade materials, not websites.** A scan of an original on a family page is an `image-of-original` artifact with its own provenance; the page's text is family testimony.
- **Photos in the main pass.** `references/photo-search.md`: where to look (documents, albums, family pages, school and workplace groups, gravestones, files that may contain photos), identification by caption and provenance only (no face recognition), dating by studio stamps and mounts or by uniform as a hypothesis, maximum available resolution, `not-found` limited to the sources actually checked, a `photos/` folder with a README (`assets/photos-readme.template.md`). `init_case.py` now creates `photos/`.
- **Readable PDF for the family.** `assets/report-pdf.template.html` and `scripts/build_pdf.py`: a clean A4 layout with plain-language reliability labels instead of internal statuses. Required sections: summary, the person and their family, a "how we are related" chain with a label on every parent–child link, timeline, documents, contradictions, where we searched without result, next steps and sources. Optional blocks: places and jurisdictions, military service with awards, repression and rehabilitation, migration and emigration, photos and document scans. The script refuses to build if internal IDs, English statuses or unfilled placeholders remain in the visible text, then prints via headless Chrome, Chromium or Edge.
- **Military database notes** (field lessons): award sheets have higher commanders' resolutions on the reverse side; 1944–1957 long-service awards are distinguished from combat awards by the decree title.

## Field lessons (new in 1.1.0)

Version 1.1.0 adds `references/field-lessons.md`, distilled from a real multi-week research case. Personal data from that case is not included.

- **Unindexed scans.** Most ex-USSR church books on FamilySearch are images only. The file shows how to find the right film (including archive-reference-to-film mapping), map it, skim the key column and zoom on candidates. It also covers what to log.
- **Reading cues that link records.**
  - An exact birth date in a marriage register can match a baptism day for day.
  - House numbers persist for decades.
  - Stillbirths are listed among births.
  - Patronymic formulas: Ukrainian possessive forms, Romanian *fiul lui / n.*, Church Slavonic, German.
  - Clerk name swaps, such as Grigorie and Gheorghe.
- **Marginal annotations.** Later Soviet-era notes on old registers show when a certificate was issued, which is evidence that the person or their family was alive and requesting documents.
- **Repression and deportation records.**
  - Published document collections and their typical contents.
  - How rehabilitation books group people.
  - How to read a "basis" column such as a 25-year sentence.
  - Why a family is linked to a deportation operation, not to the local incident used to justify it.
  - How to draft an archive request.
- **Military databases.** WWII loss records (survivors are absent), "surname nests" built from birthplace clusters, and which portals put up CAPTCHAs. Austro-Hungarian WWI casualty lists help for Galicia and Bukovina.
- **Places that no longer exist.** Liquidated hamlets, redrawn districts, and the difference between "born in" and "grew up in". Also archives in occupied or war-damaged areas.
- **Family materials as sources.** Passports, oral-history recordings with timestamps, and the user's own notes, all tagged by source type.
- **Browser technique.** Deep-zoom tile crops, composite column views for fast skimming, keeping long jobs in their own tab, polite crawling and retries.
- **Knowing when to stop.** When public sources are exhausted, the agent says so once, prepares questions for living relatives and request drafts for archives, and does not invent or loop.

Also in 1.1.0:

- The agent replies in the user's language; the intake template defaults to `requested_output_language: auto`.
- The skill description is bilingual, so it triggers for English-speaking users too.
- **CAPTCHAs are handed to a human.** The agent never solves or bypasses them, and the user signs in to sites themselves.
- More seed sources, each with a reliability class and role.
- New eval cases: language matching, unindexed scans, house-number identity, birthplace conflicts, exhausted sources, repression lists.

## Wider source coverage (new in 1.2.0)

The seed catalog previously leaned heavily on Russia; this release expands coverage of the other claimed jurisdictions; archive coverage and current access still need checking for each case:

- **Central imperial/Soviet archives of Russia:** RGIA (St. Petersburg), RGADA, RGVIA, RGAVMF, GARF, plus digitized provincial address-calendars (Presidential Library).
- **Ukraine and Belarus:** CDIAL (Lviv), CDIAK (Kyiv), Ukraine's national archives portal, and the National Historical Archive of Belarus (Minsk).
- **Moldova:** Bessarabian metric books 1820–1924 at the National Archives, with online certificate requests (the domain changed to `arhiva.gov.md`).
- **The Baltics:** Latvia's Raduraksti (church books, revision lists, 1935 census), Lithuania's ePaveldas, Estonia's Saaga.
- **Poland and the Habsburg layer:** Szukajwarchiwach, Geneteka (volunteer index covering Polish, Lithuanian, Belarusian and western-Ukrainian parishes), AGAD (historical eastern-territory collections), Matricula Online, Mapire (Habsburg cadastral and military maps), Kriegsarchiv and successor-state archives (military personnel records; custody and survival depend on period and origin).
- **Jewish families of the region:** JewishGen, JRI-Poland, Yad Vashem, Routes to Roots Foundation (which archive holds which town's surviving records).

New reference **`references/habsburg-galicia-bukovina.md`**: the jurisdiction chain 1772–1945, Austrian-era metric books and consistorial copies, calendar verification by confession, period, and individual register (a 12- or 13-day discrepancy is a hypothesis to investigate), Habsburg conscription records, Galician land cadastres (Josephine and Franciscan records — household and landholding evidence that helps disambiguate namesakes), and a storage map for each record type. New eval cases cover the Galician jurisdiction chain and the 13-day date conflict.

## Install

Clone the repository into your agent's skills folder:

```bash
# Codex
git clone https://github.com/DenisSergeevitch/ex-ussr-kinship-research ~/.codex/skills/ex-ussr-kinship-research

# Claude Code (personal skills)
git clone https://github.com/DenisSergeevitch/ex-ussr-kinship-research ~/.claude/skills/ex-ussr-kinship-research
```

For a single project, put the folder in `.claude/skills/` inside the repository. The scripts need only Python 3 and the standard library; `build_pdf.py` also needs a Chromium-based browser.

## Use

Just describe the family question; the skill triggers on genealogy requests. For example:

- *"My great-grandfather was exiled to Kazakhstan in the 1930s. Help me find his parents in church records."*
- *"Here are scans of a marriage certificate and a soldier card. Check whether they are the same person and build the evidence chain."*
- *"Find the parish and the right FamilySearch film for a village in Podolia in the 1890s and read the births for the surname X."*

Keep private case data outside the skill's Git checkout. From the skill folder:

```bash
python scripts/init_case.py --case-dir ~/kinship-cases/my-case --research-question "Who were Maria's parents?" --subject-label "Maria"
python scripts/hash_artifact.py ~/kinship-cases/my-case/scans/marriage-1902.jpg   # SHA-256 for every saved artifact
python scripts/audit_evidence.py --case-dir ~/kinship-cases/my-case             # check registers before the final report
python scripts/build_pdf.py --html ~/kinship-cases/my-case/pdf-source/report.html --out ~/kinship-cases/my-case/report.pdf
```

`init_case.py` refuses to create a case inside the skill folder, including with
`--force`. Git ignores common legacy case-folder names as an additional safeguard;
check staged files before contributing, especially if moving an existing case.

`build_pdf.py` needs Chrome, Chromium or Edge installed (or `CHROME_PATH`). With `--png-dir` it also renders pages to PNG for review if `pypdfium2` is available.

Use static HTML with local or embedded images. Active HTML and network image sources are rejected; source links remain clickable. A failed render preserves any existing output PDF.

## Repository layout

```
SKILL.md                         main instructions (workflow, hard rules, language policy)
references/
  source-strategy.md             source hierarchy, query patterns, catalogs, archive requests
  evidence-protocol.md           artifact assessment, statuses, identity resolution, conflicts
  ex-ussr-locale-guide.md        jurisdictions, name variants, calendars, record types by period
  browser-and-captcha.md         browsing, sign-in, CAPTCHA hand-off, rate limits
  privacy-and-safety.md          living people, minors, DNA, sensitive findings
  seed-sources.md                curated starting resources with reliability classes
  field-lessons.md               practical lessons from a real case (new in 1.1.0)
  photo-search.md                finding, identifying, dating and storing photos (new in 1.3.0)
  habsburg-galicia-bukovina.md   Galicia and Bukovina under the Habsburgs and after (new in 1.2.0)
assets/                          case intake, CSV registers, final report template,
                                 photos README template, readable PDF template (report-pdf.template.html)
scripts/                         init_case.py, hash_artifact.py, audit_evidence.py, build_pdf.py
evals/discovery-and-logic.yaml   trigger and reasoning test cases
tests/                           unit tests for audit_evidence.py (python3 -m unittest discover -s tests)
```

## Responsible use

- **Family history, not surveillance.** The skill refuses to look up current addresses, phones or accounts of living people, and it minimizes their data in reports.
- **No bypassing.** No CAPTCHA solving, no paywall, geo-block or rate-limit evasion, no leaked databases. Sign-ins are done by the user.
- **No uploads of your documents or DNA data** to third-party services without your explicit consent.
- **Sensitive findings** (deportation, repression, adoption, DNA surprises) are reported neutrally and only as far as the question requires.
- **Archive access rules change.** Always check the current official procedure before sending a request; the skill only drafts requests, it does not send them.

## Contributing

New sources are welcome. Add them to `references/seed-sources.md` with these fields: owner, jurisdiction, collection type, originals or index, stable IDs, access, restrictions, typical errors, date last checked. Please keep personal data out of examples.

## License

[MIT](LICENSE) © 2026 Denis Shiryaev.

---

Maintained by Denis Shiryaev ([@DenisSergeevitch](https://github.com/DenisSergeevitch)).
