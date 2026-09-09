# Velo Claim Neo4j Runtime Contract

Inspected read-only on the DGX on 2026-09-08. The populated database was not modified or re-imported.

## Runtime

- Compose service: `neo4j`
- Intended backend URI: `bolt://neo4j:7687`
- Database: `neo4j`
- Observed size: 102,389 nodes and 7,790 relationships
- Observed indexes: full-text indexes on `Diagnosis_Code.description_en` and `Dental_Procedure_Code.description`, plus token lookup indexes
- Observed constraints: none

Exact-match indexes and uniqueness constraints are missing for primary identifiers such as diagnosis/procedure code, payer ID, plan ID, benefit ID and rule ID. Adding them is a separate database mutation and requires an approved backup/change window.

## Labels And Identifiers

| Label | Primary observed identifiers and important properties |
| --- | --- |
| `Diagnosis_Code` | `code`, `code_system`, `version`, `description_en`, `authority`, regulator acceptance flags |
| `Dental_Procedure_Code` | `code`, `code_system`, `version`, `valid_from`, `valid_to`, `required_docs`, sensitivity flags |
| `UAE_Payer` | `payer_id`, `eclaims_payer_id`, names, `active`, `regulated_by`, `timely_filing_days` |
| `Insurance_Plan` | `plan_id`, `payer_id`, names, `active`, plan/network/limit properties |
| `Plan_Benefit` | `benefit_id`, `plan_id`, `dental_code`, `code_system`, `covered`, `prior_auth_required`, `waiting_period_days`, `coinsurance_pct`, `max_fee_aed` |
| `Prior_Authorization_Rule` | `rule_id`, `payer_id`, `procedure_code`, `code_system`, `pa_required`, `applies_to_all_plans`, turnaround/validity, source/version/evidence dates |
| `Bundling_Rule` | `rule_id`, `source_raw`, `target_raw`, `condition`, `reason`, authority/source/evidence metadata |
| `Frequency_Limit_Rule` | `rule_id`, `payer_id`, `plan_scope`, `procedure_code`, `code_system`, `limit_count`, `limit_period`, `limit_scope`, source/effective metadata |
| `Facility` | `facility_id`, `eclaims_facility_id`, license, authority, channel/HIE IDs, code system, active/expiry data |
| `Provider` | `provider_id`, `eclaims_provider_id`, license, authority, specialty, active/expiry data |
| `Payer_Network_Agreement` | `agreement_id`, `facility_id`, `payer_id`, network status, active/contract dates, billing flags |
| `Facility_Fee_Schedule` | `entry_id`, `facility_id`, `dental_code`, `code_system`, fee/currency/effective/provenance data |
| `Employer_Group_Policy` | `policy_id`, `base_plan_id`, `payer_id`, group/employer and contract/limit properties |
| `Benefit_Override` | `override_id`, `policy_id`, `dental_code`, `code_system`, PA/fee/coinsurance overrides |
| `Regulatory_Authority` | `authority_id`, jurisdictions, diagnosis/procedure systems, filing/PA/channel metadata |
| `Claims_Channel` | `channel_id`, `authority_id`, platform, claim/diagnosis/procedure formats and endpoint metadata |
| `HIE_Platform` | `hie_id`, `authority_id`, coverage and capability metadata |
| `Clinical_Decision` | `rule_id`, source/target raw codes, condition/reason and evidence metadata |

## Relationship Contract

| From | Relationship | To | Count | Runtime use |
| --- | --- | --- | ---: | --- |
| `Diagnosis_Code` | `TRIGGERS` | `Dental_Procedure_Code` | 796 | Positive diagnosis/procedure support |
| `Insurance_Plan` | `HAS_BENEFIT` | `Plan_Benefit` | 758 | Plan benefit lookup |
| `Plan_Benefit` | `GOVERNS` | `Dental_Procedure_Code` | 742 | Benefit-to-CDT applicability |
| `Prior_Authorization_Rule` | `GOVERNS_PA` | `Dental_Procedure_Code` | 101 | PA applicability |
| `Prior_Authorization_Rule` | `APPLIES_TO_PLAN` | `Insurance_Plan` | 42 | Plan-scoped PA rules |
| `Dental_Procedure_Code` | `CANNOT_BILL_WITH` | `Dental_Procedure_Code` | 1,748 | Direct bundling conflicts |
| `Dental_Procedure_Code` | `HAS_BUNDLING_RULE` | `Bundling_Rule` | 332 | Rule-mediated bundling |
| `Bundling_Rule` | `CANNOT_BILL_WITH` | `Dental_Procedure_Code` | 936 | Bundled target |
| `Frequency_Limit_Rule` | `GOVERNS_FREQUENCY` | `Dental_Procedure_Code` | 690 | Frequency-rule discovery |
| `Facility` | `HAS_FEE_SCHEDULE` | `Facility_Fee_Schedule` | 758 | Facility fee lookup |
| `Facility_Fee_Schedule` | `FOR_CODE` | `Dental_Procedure_Code` | 742 | Fee-to-CDT applicability |
| `UAE_Payer` | `OFFERS_PLAN` | `Insurance_Plan` | 3 | Payer/plan ownership |
| `Facility` | `HAS_NETWORK_AGREEMENT` | `Payer_Network_Agreement` | 1 | Network evidence |
| `Provider` | `WORKS_AT` | `Facility` | 1 | Provider/facility evidence |
| `Employer_Group_Policy` | `HAS_OVERRIDE` | `Benefit_Override` | 2 | Employer benefit override |

Other observed relationships are `CUSTOMIZED_AS`, `REGULATED_BY`, `OPERATES_CHANNEL`, `OPERATES_HIE`, `COORDINATES_WITH`, `TRIGGERS_DECISION`, and `SUGGESTS`.

Only direct `CANNOT_BILL_WITH` relationships currently carry properties. They include `authority`, `source`, `reason`, `last_verified_at`, and sensitivity flags. Other evidence is primarily stored on rule nodes.

## Runtime Semantics

- `TRIGGERS` is positive evidence. Missing `TRIGGERS` is `UNKNOWN`, not proof of incompatibility.
- A missing `Plan_Benefit` is `UNKNOWN`, not proof of non-coverage.
- PA combines applicable `Prior_Authorization_Rule` nodes with `Plan_Benefit.prior_auth_required`. Conflicting values are `CONFLICT`.
- Procedure code systems are matched explicitly. The observed procedure graph is `CDT`; it is not queried as CPT or HCPCS.
- Live patient coverage status remains an eligibility API/EHR concern. The KG answers plan-benefit questions only.
- Documentation comes from `Dental_Procedure_Code.required_docs` and `required_docs_for_submission`.
- File rules remain available for non-KG rule families/code systems. A single validation check owns each penalty.

## Known Data Gaps

- No uniqueness or exact-match indexes/constraints are present.
- The graph is UAE dental/CDT-oriented and does not establish broad CPT/HCPCS coverage.
- Frequency rules exist, but patient utilization history is not represented; compliance cannot be adjudicated from the KG alone.
- Only one facility/network agreement and one provider/facility link were observed.
- Fee data needs contract/provenance review before it is treated as an authoritative allowed amount.
- Endpoint values are metadata, not verified credentials or live-connectivity guarantees.
