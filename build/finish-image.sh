#!/bin/sh
# The last step of every image (Dockerfile.ubuntu24, Dockerfile.ubuntu26 and
# Dockerfile.beta), after rootfs/ is copied in: make the tools executable,
# wire the web interface into the base image's nginx, and check that what
# ships parses. One script so the images cannot drift apart. Run with
# RUN --mount=type=bind,source=build,target=/build sh /build/finish-image.sh,
# so it is not left in the image.
set -eu

chmod +x /startapp.sh \
    /usr/local/bin/bb-monitor /usr/local/bin/bb-monitor-web \
    /usr/local/bin/bb-health /usr/local/bin/bb-watchdog /usr/local/bin/bb-version \
    /usr/local/bin/bb-doctor /usr/local/bin/bb-report /usr/local/bin/bb-apikey \
    /usr/local/bin/bb-settings \
    /etc/services.d/bb-watchdog/run /etc/services.d/bb-monitor-web/run

# Wire the web interface (bb-monitor-web, proxied from loopback) into the base
# image's nginx server block. default_site.conf is included directly from
# nginx.conf and is not regenerated at container start (only the optional
# /var/tmp/nginx/*.conf fragments are), so a build-time edit survives. Python
# rather than sed, because a newline in a sed replacement is GNU-specific.
# The anchor check is the guard: if a base-image bump moves or renames that
# include line, the build fails here naming the anchor, so no image ships
# whose /monitor/ returns 404.
python3 - <<'PATCH'
import sys
site = "/opt/base/etc/nginx/default_site.conf"
anchor = "include /var/tmp/nginx/term[.]conf;"
add = """

        # Upload dashboard (bb-monitor-web), proxied from loopback.
        include /etc/nginx-bb-monitor.conf;"""
s = open(site).read()
if anchor not in s:
    sys.exit("FAIL: nginx anchor %r not found in %s; base image changed" % (anchor, site))
if "nginx-bb-monitor.conf" in s:
    sys.exit("FAIL: bb-monitor include already present; patch would double-apply")
open(site, "w").write(s.replace(anchor, anchor + add, 1))
print("nginx: bb-monitor include added after the terminal include")
PATCH
grep -q "nginx-bb-monitor.conf" /opt/base/etc/nginx/default_site.conf

# What ships must parse. The image's /bin/sh is dash, so sh -n, not bash -n.
bash -n /startapp.sh
grep -q BACKBLAZE_VERSION /startapp.sh
grep -q bb-wine-switches.sh /startapp.sh
grep -q bb-mountpoints.sh /startapp.sh
sh -n /usr/local/bin/bb-doctor
sh -n /usr/local/bin/bb-health
for part in asuser skipped drives config passes; do
    grep -q "bb-doctor-$part.sh" /usr/local/bin/bb-doctor
    sh -n "/usr/local/lib/bb-doctor-$part.sh"
done
grep -q "swap to absorb the peaks" /usr/local/bin/bb-doctor
grep -q FROZEN /usr/local/bin/bb-health
sh -n /usr/local/lib/bb-wine-switches.sh
sh -n /usr/local/lib/bb-mountpoints.sh
python3 -m py_compile /usr/local/bin/bb-report /usr/local/bin/bb-monitor-web
grep -q _bzcli_settings /usr/local/bin/bb-report
for f in /usr/local/lib/bb-monitor/*.py; do python3 -m py_compile "$f"; done
find /usr/local -name __pycache__ -type d -prune -exec rm -rf {} +
command -v setpriv >/dev/null
echo "finish-image: done"
