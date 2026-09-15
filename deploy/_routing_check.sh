#!/usr/bin/env bash
# Proves the eva.finance routing block works before DNS/TLS exist, by
# mounting the same handlers on a plain-HTTP localhost port, exercising
# them, then removing the temporary block. Does not touch the real sites.
set -u
CF=/etc/caddy/Caddyfile
TMP_MARK="# TEMP-ROUTING-CHECK"

cleanup() {
	sed -i "/$TMP_MARK/,/$TMP_MARK-END/d" "$CF"
	systemctl reload caddy
}
trap cleanup EXIT

cat >>"$CF" <<'EOF'
# TEMP-ROUTING-CHECK
http://localhost:8099 {
	handle_path /api/* {
		rewrite * /api/public{path}
		reverse_proxy localhost:8080
	}
	handle {
		root * /var/www/eva-web
		try_files {path} {path}/index.html {path}.html
		file_server
	}
	handle_errors {
		root * /var/www/eva-web
		rewrite * /404.html
		file_server {
			status {err.status_code}
		}
	}
}
# TEMP-ROUTING-CHECK-END
EOF

systemctl reload caddy || { echo "RELOAD FAILED"; exit 1; }
sleep 3

B=http://localhost:8099
probe() { # path expected_code label
	code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$B$1")
	if [ "$code" = "$2" ]; then s=OK; else s="FAIL(want $2)"; fi
	printf '  %-34s %-4s %s\n' "$1" "$code" "$s"
}

echo "=== clean URLs (Astro flat files) ==="
probe / 200
probe /strategies 200
probe /beta 200
probe /case-studies 200
probe /docs 200
probe /docs/whitepaper 200
probe /docs/getting-started 200
probe /docs/engine 200
probe /docs/strategies 200

echo
echo "=== assets ==="
probe /case-studies/case_study_hq_25.png 200
probe /brand/eva-wordmark-ink.svg 200
probe /favicon.png 200

echo
echo "=== unknown path falls back to the styled 404 ==="
probe /nope 404
printf '  404 body is our page: %s\n' \
	"$(curl -s --max-time 10 "$B/nope" | grep -c 'No record here' || true)"

echo
echo "=== API rewrite /api/* -> /api/public/* ==="
probe /api/strategies 200
printf '  strategies payload books: %s\n' \
	"$(curl -s --max-time 20 "$B/api/strategies" |
		python3 -c 'import json,sys; d=json.load(sys.stdin); print(",".join(k for k in ("hq","mill","yield","kalshi") if k in d))')"
curl -s --max-time 20 "$B/api/strategies" >/tmp/strat.json
python3 - <<'PY'
import json
m = json.load(open("/tmp/strat.json"))["mill"]
p, c = m["prior_bracket"], m["current_bracket"]
print(f"  mill: {m['n_ideas']} ideas since {m['since']}, re-based {m['epoch_start']}")
print(f"    prior bracket   n_closed={p['n_closed']} hit_rate={p['win_rate_pct']}")
print(f"    current bracket n_closed={c['n_closed']} hit_rate={c['win_rate_pct']}")
PY

echo
echo "=== beta POST through the rewrite ==="
code=$(curl -s -o /tmp/beta_out -w '%{http_code}' --max-time 20 \
	-X POST "$B/api/beta" -H 'Content-Type: application/json' \
	-d '{"email":"routing-check@eva.finance","note":"routing check"}')
printf '  POST /api/beta -> %s %s\n' "$code" "$(cat /tmp/beta_out)"
python3 - <<'PY'
import sqlite3
con = sqlite3.connect("/opt/eth-trading-agent/ledger.db")
n = con.execute("SELECT COUNT(*) FROM beta_signups WHERE email='routing-check@eva.finance'").fetchone()[0]
print(f"  row persisted: {n}")
con.execute("DELETE FROM beta_signups WHERE email='routing-check@eva.finance'")
con.commit()
print("  cleaned; remaining signups:",
      con.execute("SELECT COUNT(*) FROM beta_signups").fetchone()[0])
con.close()
PY
