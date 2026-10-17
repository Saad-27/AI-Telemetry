# 0018: Error samples: the first 3 of each class per minute

- **Status:** accepted
- **Date:** 2026-10-17

## Context
The SDK sent every failed call as a sample, up to 100 per process per minute (ADR 0003,
brief §19.3). During a provider incident that is about 110 records per process per minute, up
from about 12, and most of them are copies: the same model, class and status.

The summaries already count every error exactly, by class, each minute. Charts, error rates and
health badges use those counts. Error samples only feed the live feed, where the useful part is
seeing each kind of error, not a hundred of the same one.

The volume matters because of the rate limit (ADR 0017): 20 records/s per account. An account
with 50 processes sends about 10 records/s normally, but about 90/s during an incident, so its
data would arrive late exactly when it matters most.

The SDK's own retries are not the cause. A provider client's internal retries happen inside the
call the SDK wraps, so three attempts are one recorded call. The SDK's sends to ingest are one
request per `Retry-After`.

## Options considered
1. **A lower flat cap**, for example 10 a minute. A flood of one class (such as 429s) could
   hide a rarer one that explains the incident.
2. **A cap per error class:** the first 3 of each class per minute per process. At most 27,
   since there are 9 classes, and usually 3 to 6.

## Decision
- **Option 2.** **(Saad)** The first 3 errors of each class per minute per process are sent as
  samples. The rest are only counted in the summary.
- An error over the cap does not mark its series as seen, so the series still gets its
  `first_of_series` sample from its next successful call. A new model that first appears
  during an incident shows up in the live feed once it works.
- **No wire format change.** The server accepts the same records. No released SDK used the old
  cap (the first release is 0.1.0, M7).

## Trade-offs
- The live feed shows the first errors of each type, not every error. It must say so, and that
  error counts come from the summaries (brief §19.6).
- During an incident a process sends about 15 records a minute instead of about 110. An
  account with 50 processes sends about 13 a second, under the rate limit.
- Only the first errors of a class each minute are seen in detail. A different
  `provider_error_code` later in that minute may only appear in a later minute's samples.
