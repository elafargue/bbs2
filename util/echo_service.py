#!/usr/bin/env python3
"""
echo_service.py — a minimal bbs2 external-service demo.

Reads a line from the caller (stdin) and echoes it back (stdout), so you can
confirm end-to-end that an ax25d-style service route works — over the air or
over a local connection.  This is the simplest possible program to point a
`services:` route at while you're bringing the feature up.

See ../README.md (the `services:` section) and ../config/bbs.yaml.example for
the full configuration reference; a quick start is in ./README.md.

Contract (same as any bbs2 external service, mirroring ax25d):
  * The caller's input arrives on stdin; anything written to stdout goes back
    to the caller.  stderr is free for logging — it lands in the bbs2 journal.
  * The program is run with a minimal environment (a PATH only) and its argv
    is %-substituted by bbs2 — here argv[1] is "%U", the caller's callsign
    without SSID (upper-case).
  * Set `crlf: true` on the route!  AX.25 lines end in a bare CR, not LF, so a
    line-oriented program like this one blocks forever on its first read
    without translation (the connection succeeds, then goes silent).
"""
import sys


def main() -> None:
    caller = sys.argv[1] if len(sys.argv) > 1 else "OM"
    out = sys.stdout

    out.write(f"*** bbs2 echo service - hello {caller}! ***\n")
    out.write("Type a line and I'll echo it back. Send /q to disconnect.\n")
    out.flush()

    while True:
        line = sys.stdin.readline()
        if not line:                     # EOF — the caller disconnected
            break
        text = line.rstrip("\r\n")
        if text.strip().lower() == "/q":
            out.write("73!\n")
            out.flush()
            break
        out.write(f"you said: {text}\n")
        out.flush()                      # flush every line so output isn't buffered


if __name__ == "__main__":
    main()
