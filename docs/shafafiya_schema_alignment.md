# Shafafiya v2.0 Alignment

## Implemented

The official files supplied in `SchemaRelease-shafafya/` are preserved byte for
byte in `data/schemas/shafafiya/v2.0/`. Runtime defaults always load claim,
request, response, and common types from this single versioned directory.

| Transaction | Wire document | Runtime behavior |
| --- | --- | --- |
| Claim | `Claim.Submission` | Built and validated with `ClaimSubmission.xsd` |
| Eligibility request | `Prior.Request`, `Authorization/Type=Eligibility` | Built and validated with `PriorRequest.xsd` |
| Eligibility response | `Prior.Authorization` | XSD-validated, normalized, cached, and attached as `Encounter/EligibilityIDPayer` after rebuild |
| PA request | `Prior.Request`, `Authorization/Type=Authorization` | Built, XSD-validated, stored once, and correlated with one internal request ID |
| PA response | `Prior.Authorization` | XSD-validated, normalized, persisted, date/code checked, and attached as `Activity/PriorAuthorizationID` after rebuild |

The builders now preserve encounter times and financial values, use the
official Activity Type mapping (CPT 3, HCPCS 4, Trade Drug 5, Dental 6,
Service 8, IR-DRG 9, Generic Drug 10), and reject unsupported values. The
`DispositionFlag` is environment-controlled and defaults to
`PTE_VALIDATE_ONLY`.

PostgreSQL PA response persistence now has one implementation, adapts to both
the original and aligned table layouts, and stores the complete normalized
response. Migration `003_shafafiya_release_alignment.sql` adds the PA display
identifier and allows standalone PA construction before a claim is linked.

## Deployment

Apply migrations in order on the DGX database:

```bash
docker exec -i velo-claim-postgres \
  psql -U velo_claim -d velo_claim -v ON_ERROR_STOP=1 \
  < velo_claim/migrations/003_shafafiya_release_alignment.sql
```

Keep these values in the deployed environment:

```env
SHAFAFIYA_CLAIM_XSD_PATH=./data/schemas/shafafiya/v2.0/ClaimSubmission.xsd
SHAFAFIYA_PRIOR_REQUEST_XSD_PATH=./data/schemas/shafafiya/v2.0/PriorRequest.xsd
SHAFAFIYA_PRIOR_AUTHORIZATION_XSD_PATH=./data/schemas/shafafiya/v2.0/PriorAuthorization.xsd
SHAFAFIYA_DISPOSITION_FLAG=PTE_VALIDATE_ONLY
SHAFAFIYA_RESPONSE_DISPOSITION_FLAG=PTE_RESPONSE
```

## Still External

XSD validation proves document structure and primitive formats. It cannot
prove that facility, payer, clinician, member, or Emirates IDs are registered
and active; that an Emirates ID checksum is valid; or that payer business rules
and medical necessity are satisfied.

The controlled submission adapter now supports manual exchange and WSDL-bound
PTE delivery, exact-payload approval, a durable submission journal, response
import, and reconciliation. It remains disabled by default, and production
submission remains blocked while the coding KG and payer rules use mock data.
Live PTE operation still requires the contracted endpoint, authentication,
certificates or credentials, registered sender/receiver IDs, and successful
integration testing against the payer environment.

The supplied release does not validate Dubai eClaimLink payloads or NPHIES
FHIR profiles. Those need their own official schema/profile packages and
validators.
