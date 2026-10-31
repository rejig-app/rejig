# Third-party notices

Rejig's repository contains source code only. The installer downloads its
dependencies from their publishers; each comes under its own license.

## Fetched by the installer

- [ios-homescreen](https://github.com/YakupEmreYerli/ios-homescreen) by Yakup Emre Yerli,
  MIT, pinned at `97649547dbc44295e451c6bf130a87404481e10c`. It supplies the layout engine,
  the editor shown in the window and Demo mode. It bundles Preact (MIT), htm (Apache-2.0)
  and IBM Plex Sans (SIL OFL 1.1), with their notices in the installed package.
- [pymobiledevice3](https://github.com/doronz88/pymobiledevice3) by doronz88 and contributors,
  GPL-3.0-or-later, 11.26.0. It talks to the iPhone and runs inside the engine's Python process.
- [srptools](https://github.com/idlesign/srptools) by Igor Starikov, BSD-3-Clause, 1.0.1.
  Used for Wi-Fi pairing.
- About 100 further packages listed with exact versions and hashes in `requirements.lock`,
  taken from ios-homescreen's tested lockfile. Most are MIT, BSD, ISC or Apache-2.0;
  several pymobiledevice3 companions are GPL-3.0-or-later, pylzss is LGPL-3.0, and
  certifi and tqdm are MPL-2.0.
- The [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) (`mcp` and
  `mcp-types` 2.3.0, MIT), for the MCP server, and the 11 packages it adds, listed with
  exact versions and hashes in `requirements-mcp.lock`: attrs, jsonschema,
  jsonschema-specifications, PyJWT, referencing, rpds-py and truststore (MIT); httpcore2,
  httpx2 and sse-starlette (BSD-3-Clause); python-multipart (Apache-2.0).

## Modified upstream code

`patches/wifi-reconnect.patch` modifies `ios_homescreen/device.py`,
`patches/device-identity.patch` modifies `ios_homescreen/ui/session.py` and
`ios_homescreen/ui/errors.py`, and `patches/offloaded-apps.patch` modifies
`ios_homescreen/device.py`, `planner.py` and `report.py`, and
`patches/mcp-companion.patch` modifies `ios_homescreen/api.py`, `ui/session.py`,
`ui/app.py` and `ui/static/js/actions.js`. Each contains lines from those files. The installer applies
them to the user's own copy. ios-homescreen's license:

```text
MIT License

Copyright (c) 2026 Yakup Emre Yerli

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
