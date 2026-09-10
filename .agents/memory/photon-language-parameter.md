---
name: Photon language parameter
description: Records the live Photon API limitation affecting Portuguese address autocomplete.
---

Do not send `lang=pt` to the public Photon API. Omit the language parameter so Portuguese place labels are returned from the underlying OpenStreetMap data.

**Why:** The live Photon endpoint returns HTTP 400 because its supported interface currently accepts only `default`, `de`, `en`, and `fr`. Mocked tests did not expose this provider behavior.

**How to apply:** Whenever adding or changing Photon-backed address search, validate the real provider request and keep Portuguese localization outside Photon’s unsupported `lang` parameter.