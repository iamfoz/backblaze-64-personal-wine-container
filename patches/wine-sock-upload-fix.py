#!/usr/bin/env python3
"""Apply the Backblaze-64 upload-throughput fix to Wine's server/sock.c.

One change in server/sock.c that fixes the ~140 KB/s single-stream upload
throttle that app-limited senders (libcurl, and therefore Backblaze's bztransmit)
hit under stock Wine - the reason a large bz_done checkpoint never finishes inside
the 600 s upload timeout. Root cause and validation are recorded in the project's
wine-upload-fix notes; in short:

  select()/IOCTL_AFD_POLL writability. Linux only reports POLLOUT once the send
  queue drains below ~2/3 of SO_SNDBUF, but Windows select() reports a socket
  writable whenever send() would accept data. We synthesize POLLOUT in the
  immediate poll inside poll_socket() when a connected stream socket's send buffer
  still has room (TIOCOUTQ < SO_SNDBUF). libcurl does a select() writability
  pre-check before waiting on FD_WRITE: on Windows the pre-check returns writable
  and the send proceeds immediately, but on stock Wine it returns "not writable"
  while the buffer has room, so curl falls through to a full 1000 ms
  WSAWaitForMultipleEvents(FD_WRITE) per burst. Restoring Windows writability
  semantics makes curl's native fast path light up under Wine.

A second change (re-enabling FD_WRITE on every send completion) was prototyped but
DROPPED. A conformance test on real Windows confirmed Windows does NOT re-arm
FD_WRITE after a *successful* send - it re-arms only after a send fails with
WSAEWOULDBLOCK, exactly as MSDN documents - so that change would have made Wine
deviate from Windows. The writability fix above is Windows-conformant and
sufficient on its own; curl's failed-send path already arms FD_WRITE the same way
on Wine as on Windows.

The patch is anchor-based rather than line-based so it tolerates minor version
drift, and it FAILS LOUD if the surrounding code has changed - that is the signal
to re-verify the fix against the new Wine source rather than mispatch silently.

Usage:  wine-sock-upload-fix.py <path-to-server/sock.c>
"""
import sys

# (description, exact text to find, replacement text)
PATCHES = [
    (
        "poll_socket: report writable while the stream send buffer has room",
        "        pollfd.fd = get_unix_fd( sock->fd );\n"
        "        pollfd.events = poll_flags_from_afd( sock, mask );\n"
        "        if (pollfd.events >= 0 && poll( &pollfd, 1, 0 ) >= 0)\n"
        "            sock_poll_event( sock->fd, pollfd.revents );\n",

        "        pollfd.fd = get_unix_fd( sock->fd );\n"
        "        pollfd.events = poll_flags_from_afd( sock, mask );\n"
        "        if (pollfd.events >= 0 && poll( &pollfd, 1, 0 ) >= 0)\n"
        "        {\n"
        "#ifdef TIOCOUTQ\n"
        "            /* Linux only reports POLLOUT once the send queue drains below\n"
        "             * ~2/3 of SO_SNDBUF, but Windows select() reports a socket\n"
        "             * writable whenever send() would accept data. An app-limited\n"
        "             * sender that never hits WSAEWOULDBLOCK otherwise sees \"not\n"
        "             * writable\" here while its sends keep succeeding, so report\n"
        "             * writable while the send buffer still has room. */\n"
        "            if ((mask & AFD_POLL_WRITE) && !(pollfd.revents & (POLLOUT | POLLERR | POLLHUP))\n"
        "                && sock->type == WS_SOCK_STREAM && sock->state == SOCK_CONNECTED && !sock->wr_shutdown)\n"
        "            {\n"
        "                int outq = 0, sndbuf = 0;\n"
        "                socklen_t len = sizeof(sndbuf);\n"
        "                if (!ioctl( pollfd.fd, TIOCOUTQ, &outq )\n"
        "                    && !getsockopt( pollfd.fd, SOL_SOCKET, SO_SNDBUF, (char *)&sndbuf, &len )\n"
        "                    && outq < sndbuf)\n"
        "                    pollfd.revents |= POLLOUT;\n"
        "            }\n"
        "#endif\n"
        "            sock_poll_event( sock->fd, pollfd.revents );\n"
        "        }\n",
    ),
]


def main():
    if len(sys.argv) != 2:
        sys.exit("usage: wine-sock-upload-fix.py <path-to-server/sock.c>")
    path = sys.argv[1]
    with open(path, encoding="utf-8") as fh:
        src = fh.read()

    if "Linux only reports POLLOUT" in src:
        print("wine-sock-upload-fix: already applied, nothing to do")
        return

    for desc, old, new in PATCHES:
        n = src.count(old)
        if n != 1:
            sys.exit(
                f"wine-sock-upload-fix: ANCHOR FAILED ({n} matches) for: {desc}\n"
                "  The Wine source around this code has changed. Re-verify the fix\n"
                "  against this version of server/sock.c (and update this patcher)\n"
                "  before building, rather than shipping a misapplied patch."
            )
        src = src.replace(old, new)
        print(f"wine-sock-upload-fix: applied - {desc}")

    with open(path, "w", encoding="utf-8") as fh:
        fh.write(src)
    print("wine-sock-upload-fix: done")


if __name__ == "__main__":
    main()
