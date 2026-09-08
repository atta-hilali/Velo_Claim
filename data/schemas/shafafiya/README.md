# Shafafiya Official XSDs

The `v2.0` directory is the immutable Shafafiya release package used by Velo
Claim for local payload validation. Keep every imported schema from the same
release together; transaction schemas must not use a `CommonTypes.xsd` from a
different release.

- `ClaimSubmission.xsd` validates `Claim.Submission` claim XML.
- `PriorRequest.xsd` validates both eligibility and prior-authorization
  `Prior.Request` XML; `Authorization/Type` distinguishes the transaction.
- `PriorAuthorization.xsd` validates payer eligibility and authorization
  `Prior.Authorization` responses.
- `CommonTypes.xsd`, `DataDictionary.xsd`, `PersonRegister.xsd`, and
  `RemittanceAdvice.xsd` complete the supplied release package.

All three transaction schemas import `CommonTypes.xsd` from the same directory.
Full XSD validation also requires `lxml`:

```bash
python -m pip install lxml
```

To enable validation, set the relevant `.env` values, for example:

```env
SHAFAFIYA_CLAIM_XSD_PATH=./data/schemas/shafafiya/v2.0/ClaimSubmission.xsd
SHAFAFIYA_PRIOR_REQUEST_XSD_PATH=./data/schemas/shafafiya/v2.0/PriorRequest.xsd
SHAFAFIYA_PRIOR_AUTHORIZATION_XSD_PATH=./data/schemas/shafafiya/v2.0/PriorAuthorization.xsd
SHAFAFIYA_DISPOSITION_FLAG=PTE_VALIDATE_ONLY
SHAFAFIYA_RESPONSE_DISPOSITION_FLAG=PTE_RESPONSE
```

`PTE_VALIDATE_ONLY` validates and discards a request in the public test
environment. Change it only when the payer/DOH onboarding team has issued the
correct endpoint, credentials, sender/receiver IDs, and submission approval.
