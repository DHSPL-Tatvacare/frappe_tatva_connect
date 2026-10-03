# Local development

A local copy of the production stack, built from this repo's `Containerfile` and kept in step with the deploy repo's `compose.prod.yml`. Every difference from prod is marked `LOCAL` in `compose.yml`.

## The one rule: no copying

Code lives only in the two repos on your machine, cloned side by side:

- `frappe_tatva_connect`, mounted at `apps/tatva_connect`
- `frappe_tatva_crm` (the CRM fork), mounted at `apps/crm`

Both are bind-mounted into every bench container, so the containers run your files live. Never `docker cp` and never edit inside a container. Commit and push from your machine only. The other eight apps (frappe, payments, telephony, frappe_whatsapp, helpdesk, lms, wiki, insights) are baked into the image.

## Shortcut

Every command below runs from `.localdev/` with this function. It works in bash and zsh.

```bash
dc() { docker compose -f compose.yml --env-file .env "$@"; }
SITE=dev.localhost
```

## First time

1. Clone both repos into one folder.
2. `cp .env.example .env` and set `DB_PASSWORD` and `ADMIN_PASSWORD`. Leave the repo paths blank when the repos sit side by side.
3. Build the image (below).
4. `./first-boot.sh`. It writes the config, creates the site, installs the nine apps, migrates, builds both bundles and starts every service. Re-running it is safe.
5. Open `http://dev.localhost:8080` and log in as `Administrator` with `ADMIN_PASSWORD`.

Use `dev.localhost`, never `localhost`. nginx rewrites the socket's `Origin` to the site name (`nginx/frappe.conf.template:73`), and Frappe's realtime server refuses a connection whose `Host` and `Origin` differ (`realtime/middlewares/authenticate.js:21`).

## Build the image

Rebuild when the `Containerfile` or a pinned app in `apps.develop.json` changes. You never rebuild for a change to `tatva_connect` or `crm`: those are live. Push `develop` first, because the build clones every app from GitHub. From the repo root:

```bash
export GH_PAT=$(gh auth token)
jq -r '.[] | "\(.url) \(.branch)"' apps.develop.json | while read -r URL REF; do
  L=$(git -c "url.https://x-access-token:${GH_PAT}@github.com/.insteadOf=https://github.com/" \
    ls-remote "$URL" "refs/tags/${REF}^{}" "refs/tags/${REF}" "refs/heads/${REF}")
  SHA=$(echo "$L" | awk -v r="refs/tags/${REF}^{}" '$2==r{print $1;exit}')
  [ -z "$SHA" ] && SHA=$(echo "$L" | awk -v r="refs/tags/${REF}" '$2==r{print $1;exit}')
  [ -z "$SHA" ] && SHA=$(echo "$L" | awk -v r="refs/heads/${REF}" '$2==r{print $1;exit}')
  echo "$URL $REF $SHA"
done | jq -Rn '[inputs | split(" ") | {url: .[0], ref: .[1], commit: .[2]}]' > /tmp/apps.resolved.json
docker build \
  --build-arg APPS_RESOLVED_B64="$(base64 < /tmp/apps.resolved.json | tr -d '\n')" \
  --build-arg APP_REPO_SHA="$(git rev-parse HEAD)" \
  --secret id=gh_pat,env=GH_PAT \
  --tag tatva-frappe:local --file Containerfile .
```

Pins resolve the way Deploy UAT resolves them: tag first, then branch. After a rebuild, run `dc up -d`, then `dc exec backend bench --site $SITE migrate`, then rebuild both bundles (see "Frontend").

## Start, stop, status

```bash
dc up -d                 # start everything
dc ps                    # status
dc stop                  # stop, keeping all data
dc logs -f --tail 50 backend
```

## Reflect a change

| You changed | Run |
|---|---|
| Python in either repo | `dc restart backend` |
| Job or scheduled code | `dc restart backend scheduler workflow-scheduler queue-short queue-long queue-workflow queue-partner-bulk` |
| `hooks.py` events or overrides | `dc exec backend bench --site $SITE clear-cache`, then `dc restart backend` |
| A new `scheduler_events` entry | `dc exec backend bench --site $SITE migrate`, then `dc restart scheduler` |
| Schema, fixtures, DocType JSON, patches | `dc exec backend bench --site $SITE migrate`, then `dc restart backend` |
| The CRM frontend | see "Frontend" |
| `.env` | `dc up --no-log-prefix configurator`, then restart the bench services as for job code |

Use `restart`, not `up -d`. `up -d` recreates a container with a new IP, and nginx keeps the old one until it restarts: after any recreate of `backend`, run `dc restart frontend` or every page returns 502. A new host port is the exception: it needs `dc down` and `dc up -d`.

## Frontend

Build inside the backend container, because the CRM frontend imports `sites/common_site_config.json`, which exists only there. Build only the app you changed.

```bash
# CRM SPA (never bench build for the CRM)
dc exec backend sh -c 'cd apps/crm/frontend && [ -d node_modules ] || yarn install --frozen-lockfile'
dc exec -e NODE_OPTIONS=--max-old-space-size=6144 backend sh -c 'cd apps/crm/frontend && yarn build'

# tatva_connect assets
dc exec backend bench build --app tatva_connect
```

Use `sh -c`, not `bash -lc`: a login shell drops Node from the `PATH`. After a build, hard-refresh the browser: DevTools, Application, unregister the service worker and clear storage, then reload.

## Tests

`first-boot.sh` allows tests on the site. Run one module at a time:

```bash
dc exec backend bench --site $SITE run-tests --app tatva_connect --module tatva_connect.tests.<package>.<module>
```

## Database from the host

MariaDB listens on `127.0.0.1:3307`, user `root`, password `DB_PASSWORD`.

## File storage

Blank `STORAGE_ENV` and `STORAGE_CONN` keep every file on the local disk. To test Azure offload, get the local container name and connection string from the team, set both in `.env`, apply the `.env` change, then turn on `Storage::Azure::offload` in Desk under CRM Tatva Automation.

## Reset

```bash
dc down -v          # removes every volume: site, database, queue, signatures
./first-boot.sh     # builds it all again
```
