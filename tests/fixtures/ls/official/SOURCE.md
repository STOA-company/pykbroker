# LS official TR excerpts

- Source: https://openapi.ls-sec.co.kr/api/apis/public/tr-guides.json?download=true
- Guide version: 20261007070834325
- Received (KST): 2026-10-08T09:08:06.978070+09:00
- Original SHA256: `07e14d740de55502caf0ecfe6860047c8690018e628b3231cf7a7ba86324c5fa`
- Original retained outside git: `/home/quantus/runs/broker-expansion-1008/w3-ls/tr-guides.json`.
- Extraction: selected transactions verbatim, with their API path, inherited defaults and commonHeaders; no response data was generated from these definitions.
- Token contains only public field definitions, no token issuance or broker API call.
- Each TR retains request/response continuation header descriptions where present; commonHeaders apply where inherited.
- CSPAT00601/00801 IsuNo: stock code or A+stock code; mock requires A+stock code. ELW J/ETN Q are outside this stock adapter change.
- t0424/t0425 additionally copy the returned body CTS field into the next request.
- No order rsp_cd success-code table or order response example is defined here. 00039/00040 remain unverified; never infer acceptance from an order number alone.
- Existing all-zero success behavior comes from the official token-present client cited in `docs/status/2026-10-04-pack-ls-us.md`, not from a TR success table.
- COSAT00301 request has no date field. COSAQ00102 OrdDt is an eight-character order date with no timezone/day-selection description; explicit broker date remains required locally for reconciliation.

## Extract integrity (SHA256)

- `COSAQ00102.json`: `fb03d1202018568c276217f90d9acdd068fa2d0b657acd26e64e7a0647c14fda`
- `COSAT00301.json`: `dfbd3c131eee46bdf3002d81903459030c0bd119dced0166d898b18c9ea5c76b`
- `COSOQ00201.json`: `7b8056375af5df4a5d61eedcee857f7213533a7171a043583424a1eab7e60301`
- `CSPAQ12200.json`: `a7a076ddba807b34eab0e65594bc2a877c0565fc94db6bf1c9805094969786e7`
- `CSPAT00601.json`: `1c4c1c6d2b282b53928a5b3fe9afc08ebeeeb8bffb453dbfb936074268cb9073`
- `CSPAT00701.json`: `771c946b3e11fa92dcdd823dc93c4941812e9c42c49cf26ceaddc68293f514f6`
- `CSPAT00801.json`: `3f89c23ed5b6bd67a4163cf1250b9255163c653f3c8c1d0d28d2061fb65f2e6e`
- `t0424.json`: `417ebe86fcfd58487f7d47d35e9fcbf3e20e5a18186e3fc4feafd49336f532d3`
- `t0425.json`: `74b90088373314f58af8d792f88e1e650050fba8f9b23e1167aebcaf6f213745`
- `token.json`: `e35a7c9bd06a857739cfa146ab241e0a72b215a109ad398eca67aa38e31de0e4`
