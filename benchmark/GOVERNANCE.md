# Governance, draft v0.1

This document is a draft and its process is not yet in force. Run 2 and run 2b were run and published by the
founding authors before a maintainer group, a comment period or a vendor review window existed. Their run
directories are published without transcripts (README, Provenance).

## Origin and conflict of interest

TTRC-Bench was initiated by Parseable, a vendor of one of the backends it measures. That is disclosed on every
results page. The benchmark's job is to measure; the vendor's job is to improve its product against it.
Parseable waives any restriction on publishing benchmark results for its products.

## Home

Hosted at github.com/parseablehq/ttrc-bench at launch. Code under Apache-2.0. Tasks, corpus and results under
CC BY 4.0 (`LICENSE-DATA` names the paths). A move to a non-vendor organisation or foundation (CNCF, MLCommons or
Linux Foundation) is planned once at least two non-founding maintainers exist, expected after 18 to 24 months.

## Roles

- Maintainers: at least three, from at least two organisations, one of them academic. Decide by consensus,
  then by simple majority.
- SUT owners: each backend vendor may own its adapter and its configuration and has a four-week review
  window before any measured run of a release.
- Submitters: anyone may submit a results package. Listing requires full transcripts and passing the
  equivalence check; maintainers re-run a random sample.

## Process

1. Methodology and task suite published, with a comment period of at least four weeks, before any results.
2. Pre-registration of each release (SPEC section 12), timestamped.
3. Vendor configuration review window.
4. Measured runs, results package, publication including unfavourable results for the founding vendor.
5. Open submissions.

## Disputes and right of reply

A SUT owner may file an objection against a listed result within four weeks. Objections are resolved by
maintainer consensus, then by vote; the objection and resolution are published. Every listed result carries
a link where the SUT owner may attach a reply.

## Fair use

Anyone citing TTRC-Bench results must name the release, the division, the model snapshot and the date. Pooling
results from different divisions or model snapshots in one table, without naming them, is a fair-use violation and
is listed publicly. A registered contrast between two models on the same configuration, as in run 2 and run 2b, is
not.

## Refresh and deprecation

Each release ships a held-out split and a refresh plan. A release is deprecated when its held-out split is
exhausted, when floor baselines solve more than 10 percent of its tasks, or 18 months after publication,
whichever comes first.
