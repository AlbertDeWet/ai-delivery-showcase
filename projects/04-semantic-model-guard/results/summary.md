# Demo results (synthetic Contoso Insurance models)

Sizes are ESTIMATES (dictionary + bit-packed index from synthetic data stats).

| Env | Est. MB before | Est. MB after | Fact cols before -> after | Columns | Measures | Broken | Reports passing | Regression checks | Unexpected | Untested | Status |
|---|---|---|---|---|---|---|---|---|---|---|---|
| DEV | 32.889 | 1.535 | 32 -> 7 | 43 -> 17 | 26 -> 16 | 10 -> 0 | 3/3 | 108 | 0 | 4 | pending human sign-off (4 untested) |
| UAT | 48.732 | 2.102 | 33 -> 7 | 44 -> 17 | 24 -> 16 | 8 -> 0 | 3/3 | 100 | 0 | 4 | pending human sign-off (4 untested) |
| PROD | 64.651 | 2.659 | 32 -> 7 | 43 -> 17 | 25 -> 16 | 9 -> 0 | 3/3 | 104 | 0 | 4 | pending human sign-off (4 untested) |

Regression classification per env:

- DEV: EXPECTED: broken before; removed by trim = 40, IDENTICAL = 64, UNTESTED = 4
- UAT: EXPECTED: broken before; removed by trim = 32, IDENTICAL = 64, UNTESTED = 4
- PROD: EXPECTED: broken before; removed by trim = 36, EXPECTED: fresh data = 50, IDENTICAL = 14, UNTESTED = 4

Drift items across DEV/UAT/PROD: before 5, after 1 ([Avg Paid per Claim])
