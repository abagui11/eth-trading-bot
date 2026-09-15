#!/usr/bin/env bash
# Post-deploy verification for the eva.finance marketing site.
set -u

echo "=== services ==="
for u in caddy eth-dashboard eth-agent; do
	printf '%-16s %s\n' "$u" "$(systemctl is-active "$u")"
done

echo
echo "=== caddy loaded config (site hosts) ==="
curl -s http://localhost:2019/config/apps/http/servers 2>/dev/null |
	python3 -c 'import json,sys
d=json.load(sys.stdin)
for name,srv in d.items():
    for r in srv.get("routes",[]):
        for m in r.get("match",[]):
            if "host" in m:
                print(f"  {name}: {m[\"host\"]}")' 2>/dev/null || echo "  (admin API unavailable)"

echo
echo "=== dashboard still healthy ==="
printf '  dashboard.eva.finance -> %s\n' \
	"$(curl -s -o /dev/null -w '%{http_code}' https://dashboard.eva.finance/)"

echo
echo "=== marketing API via dashboard (origin) ==="
printf '  /api/public/strategies -> %s\n' \
	"$(curl -s -o /dev/null -w '%{http_code}' http://localhost:8080/api/public/strategies)"

echo
echo "=== static files on disk ==="
printf '  html pages: %s\n' "$(find /var/www/eva-web -name '*.html' | wc -l)"
printf '  case studies: %s\n' "$(ls /var/www/eva-web/case-studies/ | wc -l)"
printf '  404 page: %s\n' "$([ -f /var/www/eva-web/404.html ] && echo present || echo MISSING)"

echo
echo "=== eva.finance end-to-end (needs DNS) ==="
for host in eva.finance www.eva.finance; do
	ip=$(dig +short "$host" A @1.1.1.1 | head -1)
	if [ -z "$ip" ]; then
		printf '  %-18s NO DNS A RECORD\n' "$host"
	else
		code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "https://$host/")
		printf '  %-18s A=%s  https=%s\n' "$host" "$ip" "$code"
	fi
done

echo
echo "=== recent ACME / TLS events ==="
journalctl -u caddy --since '5 minutes ago' --no-pager -o cat |
	grep -Ei 'acme|obtain|certificate|reload' |
	grep -v 'http.log.error' | tail -6 || echo "  (none)"
