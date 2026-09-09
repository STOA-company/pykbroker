---
description: "pykbroker Apache-2.0 notice, extraction and rewriting attribution, referenced vendor revisions and separate license scope."
lang: en
---

# Notice

pykbroker
Copyright 2026 STOA Company

Portions of the design and broker-call contracts in this package were extracted
and rewritten from STOA-company/quantus-trader, revision:
d14719529541f2c556795f59f2f68c45b6b7715e.
Company-specific storage, policies, and credentials are not included.

Vendor API shapes were checked against these referenced source revisions:

- STOA-company/quantus-pykis: 65b56b9d1dc292a4d9b67a9e5ca583c7e6eb7efd
- STOA-company/quantus-dbfi: ac2e92749fa610e7b75f82e8a0dc9c1efca132b8

The Pykis, Pydb and toss_openapi SDK source is not bundled or relicensed here.
Each separately obtained dependency retains its own license.

Package layout, hatchling configuration, JSON-fixture conventions and FastMCP
usage were informed by the sibling pycex project (MIT, STOA Company).
No upstream safety implementation is copied: the supplied interfaces and pure
safety logic were newly written for this package. This notice does not change
the Apache-2.0 license of pykbroker.

[한국어](../notice.md)
