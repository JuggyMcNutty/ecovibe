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
   after skipping a 24-byte preamble the client sends the username and password
   each NUL-padded to 24 bytes. `core/rfb.js` → `_negotiate_aten_auth`.
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

**One file is modified: `core/rfb.js`.** Everything else is untouched. Keep it
that way — if another fix is needed, note it here and keep the change minimal.
A re-vendor is therefore *not* a plain overwrite: reapply the patch below, or
`tests/test_console.py` will fail (deliberately).

### Patch: consume the whole 24-byte ATEN preamble (2026-08-07)

**Symptom:** the browser console failed at
`Failed when connecting: Unsupported server (Unknown SecurityResult)`.

**Cause.** The fork detects ATEN with two heuristics that reach
`_negotiate_aten_auth` having consumed *different* amounts of the preamble —
#0 (`rfb.js` ~line 1015) has read 4 bytes as `numTunnels`, #1 (~line 1044) has
read 8 (`numTunnels` + `subAuthCount`). Both then skipped a flat 16, totalling
24 via #1 but only **20 via #0**.

The preamble is 24 bytes, so the #0 path left 4 bytes queued and
`_handle_security_result` read *those* as the SecurityResult. Captured live
from OVH's BMC:

```
preamble (24B): a7f95fbe 5021020020a6000070ecefbe00704b40 102e0100
numTunnels    = 0xa7f95fbe  -> > 0x1000000, so heuristic #0 fires
leftover      = 0x102e0100  = 271450368 -> hits `default:` = "Unknown SecurityResult"
real result   = 0x00000000  (OK) -- never read
```

**Fix.** Each call site records how much it consumed in `_aten_preamble_read`,
and `_negotiate_aten_auth` skips `ATEN_PREAMBLE_LENGTH - consumed` instead of a
fixed 16, so both heuristics land on the end of the preamble. Marked in-file
with `ECOVibe` comments; `_aten_preamble_read` is initialised in the
constructor and in the per-connection reset next to `_rfb_atenikvm`.

**Not fixed (pre-existing, upstream too):** the preamble is skipped without an
`rQwait`, so a preamble split across TCP segments would misalign. Not observed
— the BMC writes all 24 bytes at once and the relay is local — and fixing it
properly needs a re-entrant init state, which is a much larger divergence.

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
