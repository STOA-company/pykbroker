# KB official samples (offline only)

Source ZIP: https://openapi.kbsec.com/api/kbs/guide/json/b2c
Received: 2026-10-04 00:56:26 KST (recorded file time).
ZIP sha256: 7a8d337fd03ef9b4e008bae119c0d54f09630af9e1ffbfde2d0f8818c2bf10e0
Schema: https://openapi.kbsec.com/api/kbs/guide/excel/b2c
Received: 2026-10-04 00:57:07 KST (recorded file time).
XLSX sha256: ba4db8910857e1dfed0a29a0347736c0d6a7a46cfc6fbaf22eda54992c3a6de3
Auth: https://github.com/kbsecurities/kb-openapi/blob/5120174652ed63cb45f062b3b6d4b9f49a0702d0/example/python/auth_example.py
Existing GitHub checkout retrieval time was not recorded; no new download.

Only customer/name/account/order/credential/device identity values are replaced; response shape, prices, quantities, counts and contradictory SPQM2226 data are preserved.
All fixture identities are fake. Mutated test cases are synthetic, not additional official responses.

| File | Original member sha256 | Sanitized fixture sha256 |
| --- | --- | --- |
| SSQM2952.json | f08abf70e82f9bc30eb4a5a50c405bb6e3ed4b50040a2f876b4287dd94c49e00 | ec0d1c5b23588e05d867731966cf8da212e7463a81eab60802f6969603f96a14 |
| SKQM3350.json | f9b88c02d5b2c30db4b158a581c0bbcee54f72354be47cd1299d41d172c91a8e | cea9910a17bc0c13394414740c5c75ce6c491eb57126b140638bf37586d850ad |
| SPQM2226.json | 35916fd33af6c5058b5e2ff8fa936632b2bc3eae20dd437078bef1a718bcd2e2 | 08a0a56ea6420a960cf0e17e3dce9b5c4070a7bbf9c7c808661b9b5c1997ec54 |
| SSAM1802.json | d3e67a6fcae516aabe0b729363dc420c289fa2724d6b57ac8791d227d1f52940 | 85db283c267fa542d0220d2d9a5b04cf801bffa6835a5b31683124e79aa1d043 |
| SSAM1801.json | 85a4e76196a346d36ccd3b2f2ae9df888a19b9c280c89fd9cad0031cf6f4bf32 | f9598d1e8b47c26c1e67122c1bc6c5deb6df8e975dc83b49dce44555bd29e0e4 |
| SSAM1806.json | 25d67df522f89275b46042a5a038b6a0767f15141135436c3341ee858a214d7b | 3ffc5f7c2d0a64bce0d45bb8928767e8731b0c7fef8819a8f6408db9bfd16a95 |
| SKAM2101.json | 4d5e051eb49be7f45fd0b00d33cee6d388622dd1b09ef72985c26b834b56d4f3 | 28e4c99319853490ef50b2a7122576c7fc83a0339b16e5e0e088671fa36cea78 |
| SKAM2102.json | 76d7c7220c96271a33968c85c4e45c6f45ab3a32732d9e4f3753a70ca97114c9 | 2b0288cd22475f12ad4660055d4bb69f65c4a1a11cf03d5195369375f9d8841c |
| SPQM2204.json | 97760587c65a2e49c5cceedf858daa51b4cd3b0d59d675889086b173d0ec7377 | d8d8d78cfb9fdd41ef384153f65ea94a49921611f2e2f6505a7d53e2d2b631e1 |
| token.json | 9b0df5092de10eec26874f8cb7d5d18b58e6ba131918f7c913e77f8bd79e7bc9 | 7ec07a5f0284fca12ffc07535916f2d87ce42ef3fd4622448dc5feb19ac6f4a3 |

SPQM2226 is inconsistent: grid_cnt2=1, Record2=[], positive securities valuation; Record1 exceeds its schema capacity of 5 and contains displaced TSLA text. It cannot establish an empty account or justify dropping a missing holding.
XLSX only labels grid counts; no definition of cumulative totals or cn_f values. Continuation follows nxt_key, each nonempty page must remain complete. No guessed cn_f value is sent.
KR holdings use ec_q (settlement quantity), not hld_q plus ec_q; SSQM2952 sample blank currency belongs to the domestic KRX row.
SSAM1806 sheet28:13 describes 1=일부정정, 2=전부정정; its explicit cancel JSON uses 1 with quantity and reports successful cancellation. Use that demonstrated quantity-specific request, never infer value 2.
SKAM2101 sheet63:15 and SKAM2102 sheet64:14 specify P4 String(15). The input sample has integer prices; live fractional input acceptance remains unverified.
SPQM2204 sheet71:11-12 has no timezone definition. Preserve New York default and explicit order_date; live rollover must be checked.
