# Deploying telebambu on OpenWrt

Production runs from a git checkout at `/root/telebambu` on the router. The router itself does
the deploy: a cron job runs `telebambu-deploy`, which pulls `origin/prod` and deploys a new commit
only after GitHub CI (`.github/workflows/ci.yml`) has passed for it. Nothing outside the router
needs SSH access to it.

| File | Installed as | Purpose |
| --- | --- | --- |
| `telebambu.init` | `/etc/init.d/telebambu` | procd service; restarts the bot if it dies |
| `telebambu-run` | `/root/bin/telebambu-run` | runs `main.py` in the foreground, logging to `telebambu.log` |
| `telebambu-deploy` | `/root/bin/telebambu-deploy` | cron deploy with CI gate, health check and rollback |

pip hangs on the router, so the bot uses the system Python packages: venvs are created with
`--system-site-packages --without-pip` and the deploy doesn't run pip (`PIP_INSTALL=0`). When a
commit changes `requirements.txt`, install the new packages by hand (`opkg`/`apk`, or system pip),
or the new venv's import check fails and the deploy stops without touching the bot.

What one deploy run does: fetch `origin/prod` → stop if `HEAD` already matches it → require every
GitHub check run on that commit to be complete and green (still running → try again next run) →
build a new venv only if `requirements.txt` changed → back up `config.yml` and `data.json` → check
out the commit (detached HEAD) and point the `venv` symlink at the right venv → restart through
procd → health check → on failure, check out the previous commit, restore the previous venv and
restart. A commit that failed is not retried until a newer one lands (or `FORCE=1`). Runs are
locked so two can't overlap. Everything is logged to `/root/telebambu-deploy/deploy.log`.

The health check passes when, after the restart, `Bot started!` appears in `telebambu.log` within
120 s, the procd instance is still running 20 s later, and procd hasn't respawned it. Tracebacks
in the log are ignored by default, because Telegram network timeouts log harmless ones regularly;
set `FAIL_ON_TRACEBACK=1` to fail on them. Startup is a limited gate: it doesn't prove every printer
is reachable, so still check `/info` after a deploy. Settings (paths, timeouts, `BRANCH`,
`FAIL_ON_TRACEBACK`) are environment variables at the top of `telebambu-deploy`.

This is on `prod`, so `ci.yml` runs on every push and the deploy gate has check runs to read. A
`prod` commit with no check runs is never deployed.

## 1. Prepare the router (on the router)

Check space and Python first: `cat /etc/openwrt_release`, `uname -m`, `df -h`, `free`. Install
Python 3.11+ with venv support, git with HTTPS and CA certificates from the firmware's feeds
(`opkg` or `apk`; package names vary, e.g. `python3 python3-venv git-http ca-bundle`). The deploy
queries GitHub with Python, so curl isn't needed. If the overlay is small, put the venvs on external storage. Don't copy a venv
from another machine.

Stop the old bot and remove whatever starts `start.sh` at boot (cron, `/etc/rc.local`):

```sh
cd /root/telebambu && ./stop.sh
# confirm it's gone: no output from
pgrep -af main.py
```

Only one instance may poll the bot token. Make a protected copy of `config.yml` and `data.json`.

Create the first venv and turn `venv` into a symlink (the deploy script requires the symlink):

```sh
mkdir -p /root/telebambu-venvs /root/bin /root/telebambu-backups /root/telebambu-deploy
chmod 700 /root/telebambu-backups
# --without-pip skips ensurepip, which hangs on the router; pip comes from the system
python3 -m venv --system-site-packages --without-pip /root/telebambu-venvs/bootstrap
b=/root/telebambu-venvs/bootstrap/bin
[ -e $b/python ] || ln -s python3 $b/python   # some builds only create python3
$b/python -c 'import telegram, bambulabs_api, yaml; print("ok")'
cd /root/telebambu
if [ -L venv ]; then rm venv; elif [ -e venv ]; then mv venv /root/telebambu-venv.old; fi
ln -s /root/telebambu-venvs/bootstrap venv
printf '/venv\n/venv.next\n' >> .git/info/exclude
```

The checkout must have no local changes to tracked files (`git status`), or deploys refuse to run.
The deploy leaves it on a detached HEAD; use `git log -1` to see what's running, not `git pull`.

## 2. Install the service (on the router)

Copy the files from this directory (e.g. `scp` from a machine with the repo, or from the checkout
now that this is on `prod`):

```sh
cp /root/telebambu/deploy/openwrt/telebambu.init   /etc/init.d/telebambu
cp /root/telebambu/deploy/openwrt/telebambu-run    /root/bin/telebambu-run
cp /root/telebambu/deploy/openwrt/telebambu-deploy /root/bin/telebambu-deploy
chmod 755 /etc/init.d/telebambu /root/bin/telebambu-run
chmod 700 /root/bin/telebambu-deploy
/etc/init.d/telebambu enable
/etc/init.d/telebambu start
tail -f /root/telebambu/telebambu.log   # expect "Bot started!", then try /info in Telegram
```

From now on use `/etc/init.d/telebambu start|stop|restart`, not `start.sh`/`stop.sh` (procd doesn't
use the PID file). Add `/root/bin`, `/root/telebambu-venvs` and the token file to
`/etc/sysupgrade.conf` so they survive a firmware upgrade. `telebambu.log` isn't rotated: keep an
eye on its size, especially on flash.

## 3. Optional GitHub token (on the router)

The CI check uses the public GitHub API (60 requests/hour unauthenticated; one per run while a
new commit is pending, so a 5-minute cron is fine). If the repo is private, create a fine-grained
token with read-only access to `pcider/telebambu` (Checks: read, Contents: read) and store it:

```sh
printf '%s\n' 'github_pat_...' > /root/.telebambu-github-token
chmod 600 /root/.telebambu-github-token
```

A private repo also needs read access for `git fetch` (deploy key or HTTPS token).

## 4. Try a deploy by hand, then enable cron (on the router)

```sh
/root/bin/telebambu-deploy; echo "exit $?"
tail -n 30 /root/telebambu-deploy/deploy.log
```

With nothing new on `prod` it exits silently. Once a manual deploy (and ideally a rollback, e.g.
of a test commit) has worked, add the cron job (`crontab -e`):

```
*/5 * * * * /root/bin/telebambu-deploy
```

then `/etc/init.d/cron enable && /etc/init.d/cron restart`.

To retry a commit that failed: `FORCE=1 /root/bin/telebambu-deploy`.
To pause deploys: comment out the cron line.
To see what is running: `git -C /root/telebambu log -1 --oneline` (or the `revision` in the
latest `--- BOT RESTARTED` line of `telebambu.log`).

## 5. Manual rollback (on the router)

Each deploy writes a backup directory `/root/telebambu-backups/<time>-<sha>` with the previous
revision and venv. Comment out the cron line first (or the next run redeploys `prod`; reverting
the bad commit on `prod` is the lasting fix), then:

```sh
cd /root/telebambu
b=/root/telebambu-backups/REPLACE_WITH_BACKUP_DIRECTORY
/etc/init.d/telebambu stop
git checkout -q --detach "$(cat "$b/previous-revision")"
ln -s "$(cat "$b/previous-environment")" venv.next
python3 -c 'import os; os.replace("venv.next", "venv")'
/etc/init.d/telebambu start
```

This keeps the current `config.yml` and `data.json`. Restoring those from the backup also rolls
back claims and roles since the deploy, so only do that deliberately (with the bot stopped).

Old venvs in `/root/telebambu-venvs` and backups are never deleted automatically. Keep the
current and previous venv (`readlink /root/telebambu/venv`, the latest backup's
`previous-environment`) and prune the rest by hand.

## 6. Pin requirements (router first, then a PR)

`requirements.txt` floats (`>=` and a git HEAD for `bambulabs_api`), so the same commit can
install different packages on different days. Pin it from the working router venv:

```sh
/root/telebambu/venv/bin/python -m pip freeze > /tmp/requirements.txt   # on the router
```

Copy that file into a branch off `prod` as `requirements.txt`, check that the `bambulabs_api` line
is a `git+https://...@<commit>` URL (not a local path), review it and merge it into `prod` like
any change. CI then tests exactly what the router installs, and the deploy builds a
new venv because `requirements.txt` changed.

## 7. Watchdog (on the router)

`telebambu-watchdog` runs from cron every minute and reports to the bot's log chat through the
Telegram API (HTTPS, with the bot's own token from `config.yml`), via `telebambu-notify`:

- **Down** (no bot process): reports it, restarts the service, then reports whether it came back
  (`Bot started!` within 120 s).
- **Frozen** (process running for over 3 min, but no heartbeat for 120 s): the monitor writes
  `/tmp/telebambu.heartbeat` at the start of every update. The watchdog sends `SIGUSR1`, so the bot
  writes every thread's stack to `telebambu.log`. It puts the start of that dump in the report and
  restarts.
- **Crashed** (new process, same git revision, not started by the watchdog): procd already
  restarted it. The report includes the old process's last log lines. A manual
  `/etc/init.d/telebambu restart` is reported the same way.
- **Restart limit:** at most 3 restarts an hour. After that it reports once that it's giving up,
  and stays quiet until the bot runs again.
- **Skipped** while a deploy runs, and while `/root/telebambu-deploy/watchdog.paused` exists.
- **Telegram unreachable:** reports are queued in `/root/telebambu-deploy/watchdog-pending` and
  sent on the next run.

Everything is also logged to `/root/telebambu-deploy/watchdog.log`. Settings (thresholds, paths)
are environment variables at the top of `telebambu-watchdog`.

Install (after a deploy has brought these files onto the router):

```sh
cp /root/telebambu/deploy/openwrt/telebambu-watchdog /root/bin/telebambu-watchdog
cp /root/telebambu/deploy/openwrt/telebambu-notify   /root/bin/telebambu-notify
chmod 700 /root/bin/telebambu-watchdog /root/bin/telebambu-notify
# test the Telegram report (should appear in the log chat):
/root/telebambu/venv/bin/python /root/bin/telebambu-notify "watchdog installed"
(crontab -l 2>/dev/null | grep -v telebambu-watchdog; echo '* * * * * /root/bin/telebambu-watchdog') | crontab -
/etc/init.d/cron restart
```

`/root/bin/` is already in `/etc/sysupgrade.conf` from step 2.

Before stopping the bot on purpose: `touch /root/telebambu-deploy/watchdog.paused`. Afterwards:
`rm /root/telebambu-deploy/watchdog.paused`.

To test it (each report should arrive in the log chat within about 1–3 min):

```sh
/etc/init.d/telebambu stop                 # down: "Bot is down", then "Back up"
kill -9 "$(pgrep -f 'python -u main.py')"  # crash: "restarted unexpectedly"
kill -STOP "$(pgrep -f 'python -u main.py')"  # frozen, after about 3 min: "Bot is frozen", then "Back up"
```
