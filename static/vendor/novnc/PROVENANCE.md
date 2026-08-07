# Vendored: noVNC (ATEN iKVM fork)

Source: <https://github.com/kelleyk/noVNC/tree/bmc-support>
Upstream: <https://github.com/novnc/noVNC>
Vendored: 2026-08-07, branch `bmc-support` (upstream tree dated 2017-01-16)

## Why this fork and not upstream noVNC

OVH's dedicated servers here expose their console as **ATEN iKVM**, reachable as
plain RFB 3.8 on TCP 5900 — verified live against `ns3147088.ip-51-83-10.eu`
(`server greeting: RFB 003.008`, one security type offered: `16`).

ATEN differs from stock VNC in two ways that upstream noVNC does not implement:

1. **Security type 16.** Advertised as "Tight" but it is not TightVNC auth —
   after skipping 4 + 16 bytes the client sends the username and password each
   NUL-padded to 24 bytes. `core/rfb.js` → `_negotiate_aten_auth`.
2. **ATEN video encodings** `0x57 AST2100`, `0x58 ASTJPEG`, `0x59 HERMON`,
   `0x60 YARKON`, `0x61 PILOT3`. `core/ast2100/` is a clean-room decoder for
   AST2100 by Kevin Kelley (see `core/ast2100/README.md`).

Also carries ATEN-specific input framing (`atenKeyEvent`, `atenPointerEvent`)
and `atenChangeVideoSettings`.

## What was vendored

`core/` in full, plus `LICENSE.txt`. Dropped: `core/inflator.mod.js` (the ES
module shim — this project loads the plain-script form), and everything outside
`core/` (`app/`, `tests/`, `po/`, `utils/`, the demo HTML pages) since ECOVibe
provides its own page in `templates/kvm.html`.

**These files are unmodified.** Keep it that way — if a fix is needed, note it
here and keep the change minimal so a future re-vendor is a plain overwrite.

## Licence

noVNC core is **MPL-2.0** (`LICENSE.txt`); ECOVibe is MIT. MPL-2.0 is
file-level copyleft, so these files stay MPL-2.0 with their headers intact and
the rest of the project is unaffected. Do not strip the copyright headers.

## Load order

The 2017 tree is written as plain globals with the ES-module form in `[module]`
comments, so it loads with ordinary `<script>` tags — no build step, matching
the rest of this project. Order matters and is taken from the fork's own
`vnc_auto.html`:

```
core/util.js
core/base64.js  core/websock.js  core/des.js
core/input/keysymdef.js  core/input/xtscancodes.js
core/input/util.js  core/input/devices.js
core/ast2100/ast2100.js  core/ast2100/ast2100idct.js
core/ast2100/ast2100util.js  core/ast2100/ast2100const.js
core/display.js  core/inflator.js  core/rfb.js  core/input/keysym.js
```

`core/inflator.js` and `core/des.js` are kept even though the ATEN path does not
use zlib or standard VNC auth — `core/rfb.js` references both unconditionally.
