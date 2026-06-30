# Shafafiya Official XSDs

This folder contains the official Shafafiya schema files used by Velo Claim for
local payload validation:

- `ClaimSubmission.xsd` validates `Claim.Submission` claim XML.
- `PriorRequest.xsd` validates provider-side `Prior.Request` prior authorization
  request XML.
- `PriorAuthorization.xsd` validates payer-side `Prior.Authorization` response
  XML.
- `CommonTypes.xsd` contains the official shared Shafafiya data dictionary
  types imported by all three transaction schemas.

All three transaction schemas import `CommonTypes.xsd` from the same directory.
Full XSD validation also requires `lxml`:

```bash
python -m pip install lxml
```

To enable validation, set the relevant `.env` values, for example:

```env
SHAFAFIYA_CLAIM_XSD_PATH=./data/schemas/shafafiya/ClaimSubmission.xsd
SHAFAFIYA_PRIOR_REQUEST_XSD_PATH=./data/schemas/shafafiya/PriorRequest.xsd
SHAFAFIYA_PRIOR_AUTHORIZATION_XSD_PATH=./data/schemas/shafafiya/PriorAuthorization.xsd
```
