# Demo results

Contoso Insurance (fictional sample data). Produced by `python -m envalign demo`.

| pair | type | before identical | before aligned* | after identical | after aligned* |
|---|---|---|---|---|---|
| dev->uat | view | 36 of 38 | 36 of 38 | 38 of 38 | 38 of 38 |
| dev->uat | config_table | 27 of 30 | 28 of 30 | 29 of 30 | 30 of 30 |
| dev->uat | job | 0 of 5 | 4 of 5 | 0 of 5 | 5 of 5 |
| dev->uat | bundle | 7 of 7 | 7 of 7 | 7 of 7 | 7 of 7 |
| uat->prod | view | 33 of 38 | 33 of 38 | 38 of 39 | 38 of 39 |
| uat->prod | config_table | 22 of 30 | 23 of 30 | 29 of 30 | 30 of 30 |
| uat->prod | job | 0 of 5 | 2 of 5 | 0 of 5 | 5 of 5 |
| uat->prod | bundle | 5 of 7 | 5 of 7 | 7 of 7 | 7 of 7 |
| dev->prod | view | 32 of 39 | 32 of 39 | 38 of 39 | 38 of 39 |
| dev->prod | config_table | 20 of 30 | 21 of 30 | 29 of 30 | 30 of 30 |
| dev->prod | job | 0 of 5 | 3 of 5 | 0 of 5 | 5 of 5 |
| dev->prod | bundle | 5 of 7 | 5 of 7 | 7 of 7 | 7 of 7 |

*aligned = identical + expected per-environment difference (e.g. schedule paused outside PROD).

## Promotions

- dev->uat (risks not accepted): `plan-dev-to-uat-1afd3ac9-a.json` items=4 lint=[] -> **applied**
- dev->uat (risks not accepted): `plan-dev-to-uat-1afd3ac9-b.json` items=1 lint=['R1'] -> **refused** (HIGH risk not accepted: ['R1:004'] (after review, pass --accept-risk RULE:ITEM for each one))
- dev->uat (risks accepted per item after review): `plan-dev-to-uat-1358d021-b.json` items=1 lint=['R1'] -> **applied**
- uat->prod: `plan-uat-to-prod-fa9f287b-a.json` items=15 lint=['R5', 'R4', 'R5', 'R5', 'R5', 'R4', 'R5', 'R5', 'R5', 'R5', 'R5'] -> **applied**
- uat->prod: `plan-uat-to-prod-fa9f287b-b.json` items=3 lint=['R1', 'R5', 'R1', 'R5', 'R1', 'R5'] -> **applied**

## Protected team (their own fingerprint query)

- dev: differences before vs after = 0
- uat: differences before vs after = 0
- prod: differences before vs after = 0

## Rollback drill

- {'status': 'rolled_back', 'failures': ['T1_core_views_queryable'], 'restore_verified': True, 'views_before': 39, 'views_after_rollback': 39}

Demo elapsed time (seed to summary, this machine): 74.5 s
