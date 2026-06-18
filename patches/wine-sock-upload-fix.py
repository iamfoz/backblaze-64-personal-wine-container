#!/usr/bin/env python3
"""Apply the Backblaze-64 upload-throughput fixes to Wine's server/sock.c.

Two changes, both in server/sock.c, that together fix the ~140 KB/s single-stream
upload throttle that app-limited senders (libcurl, and therefore Backblaze's
bztransmit) hit under stock Wine - the reason a large bz_done checkpoint never
finishes inside the 600 s upload timeout. Root cause and validation are recorded
in the project's wine-upload-fix notes; in short:

  1. select()/IOCTL_AFD_POLL writability. Linux only reports POLLOUT once the send
     queue drains below ~2/3 of SO_SNDBUF, but Windows select() reports a socket
     writable whenever send() would accept data. We synthesize POLLOUT in the
     immediate poll inside poll_socket() when a connected stream socket's send
     buffer still has room (TIOCOUTQ < SO_SNDBUF).

  2. FD_WRITE re-enabling. Windows re-enables FD_WRITE on EVERY send() call - apps
     like libcurl issue a zero-byte send() to re-arm it before waiting - but Wine
     only cleared and reselected the event on an *unsuccessful* send. An app whose
     sends never fail therefore waited out its full poll timeout between bursts.
     We always clear + reselect on send completion.

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
    (
        "send_socket_completion_callback: re-enable FD_WRITE on every send",
        "    if (iosb->status != STATUS_SUCCESS)\n"
        "    {\n"
        "        /* send() calls only clear and reselect events if unsuccessful. */\n"
        "        sock->pending_events &= ~AFD_POLL_WRITE;\n"
        "        sock->reported_events &= ~AFD_POLL_WRITE;\n"
        "        sock_reselect( sock );\n"
        "    }\n",

        "    /* On Windows any send() call re-enables FD_WRITE: the event is recorded\n"
        "     * again on the next writable transition even when this send succeeded.\n"
        "     * Apps (e.g. libcurl) rely on that, issuing a zero-byte send() to re-arm\n"
        "     * FD_WRITE before waiting on it; clearing only on failure left them\n"
        "     * waiting out their full poll timeout whenever no send ever failed. */\n"
        "    sock->pending_events &= ~AFD_POLL_WRITE;\n"
        "    sock->reported_events &= ~AFD_POLL_WRITE;\n"
        "    sock_reselect( sock );\n",
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
