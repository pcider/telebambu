# TODO

Last reviewed 2026-10-26. Items marked **(ask Max first)** need a design decision before any code.

## How to ship changes

- Each new feature or fix gets its own branch off the current `prod`, holding **one commit**, so it
  can be cherry-picked on its own. Keep each change self-contained so branches don't depend on each
  other.
- Changes go through `staging` before `prod`.
- Run the tests before committing:
  `uv run -q --with-requirements requirements.txt python -m unittest tests.test_log_regressions tests.test_disabled_commands tests.test_upload`.
  `tests/` has no `__init__.py`, so `unittest discover` doesn't find them.

## Deploy and ops

- [ ] Restart the prod bot so it runs `cfec73c`. It runs on the OpenWrt router, not this machine,
      so Max does this (`./stop.sh && ./start.sh` in the bot's checkout).
- [ ] Add `disabled_commands` to prod's `config.yml`. It has none yet, so every command is enabled.
      Plan: `print: true` once `/upload` is live.
- [ ] Merge `feat/upload` (`c1a57f8`, one commit off `prod`) into `prod`. It's already merged into
      `staging` (`39132df`), and both branches are pushed.
- [ ] Test `/upload <printer> force` on a real printer while it's printing. Check that writing to
      the SD card mid-print doesn't disturb the print. Without `force`, `/upload` refuses while
      RUNNING, PAUSE or PREPARE.
- [ ] CI/CD: branch `ci/openwrt-deploy` (`5df9dec`) adds GitHub Actions CI and a pull-based
      deploy for the router (procd service, cron deploy script with a CI check, venv switch and
      rollback). See `deploy/openwrt/README.md` on that branch. CI must be on `prod` before the
      cron deploy runs. The router steps need Max: install Python, git and curl, switch to procd,
      set up the cron line, then pin `requirements.txt` from `pip freeze` on the router.
- [ ] Optional: make plain `git push` work. SSH to GitHub hangs on this network, and HTTPS through
      `gh` works. Run `gh auth setup-git` and set a global `url."https://github.com/".insteadOf
      git@github.com:`.

## Bugs

- [ ] "Filament is running low" bug: alerts fire when the spool isn't low. `fix/low-filament-alert`
      (`fb02ef3`) mutes them (`low_filament_alerts: false`) and logs each match with the raw tray
      report. Debug with Max: compare those log lines with the real spools (suspect `remain` = 0
      for non-RFID or unknown spools), then fix and turn the alert back on.
- [ ] `/print` (`BotHandlers._print_document` in `bot/handlers.py`): the Telegram file download
      (`get_file` / `download_as_bytearray`) isn't inside a `try`. If it fails (network error, or
      `file_size` is missing so the 20 MB check is skipped), the exception escapes the handler and
      the admin gets no reply.
- [ ] `/print 3 0`: an explicit plate `0` is silently treated as "no plate given" by
      `plate or plates[0]` and prints the first sliced plate, instead of being rejected as invalid.

## Unfixed review findings (from the review of `fea3979`)

- [ ] `MessageService._send_claimer_dm` adds the "DM unavailable" note at the end of the text.
      Photo captions are cut to 1024 characters, so a long message can lose the note. Put the note
      first, or send it separately.
- [ ] `send_print_failed` picks the claimer-DM path by checking
      `label == 'claimer' and thread_id is None and dm_preference == 'dm'`. Store a flag or a send
      function in the `targets` tuple instead.
- [ ] `_print_name` in `printers/monitor.py` repeats the path strip that `PrinterManager.print_names()`
      already does. Normalise names in one place.
- [ ] `_CameraRetryNoiseFilter` drops every camera `Error occurred: timed out` record, so a camera
      that stays down is never logged. Log it once per outage, or rate-limit it.

## Features (built 2026-10-26, waiting for review)

Each is one commit off `prod` (`cfec73c`) on its own branch, with tests, so it can be cherry-picked
alone. `integration/2026-10-26` (`91f5e1c`) merges all of them plus `fix/low-filament-alert` with
the conflicts and cross-feature interactions resolved (153 tests pass). Nothing has been tried
against real Telegram or printers yet. Suggested path: deploy `integration/2026-10-26` to staging,
test it live, then merge into `prod`.

- [ ] `feat/dm-only-commands` (`95f0c05`): commands only work in DMs and in the status/log chats
      plus `command_chats` (`/config allowhere|denyhere`). In other groups the command is deleted
      and the user is DMed a hint. Flag `dm_only_commands` (default on). Max chose this design.
      Follow-up: the command menu still shows in every chat.
- [ ] `feat/claim-by-dm` (`4faa4b5`): "Claim in DM" URL button; `/start claim-N` tells the user to
      send `/claim N`. Old callback buttons just open the DM.
- [ ] `feat/anonymous-prints` (`c808a12`): `/anonymous on|off` plus a settings button. One helper,
      `ui.public_claimer_name`, with a leak test.
- [ ] `feat/log-print-events` (`cbda6fd`): start/finish/fail and claim/unclaim lines in the log chat.
- [ ] `feat/beep` (`49758b5`): `/beep <printer>` (IDLE/FINISH, 30 s cooldown). Check on a real
      printer that the `M1006` buzzer G-code actually plays.
- [ ] `feat/latency-log` (`b81c957`): logs when a printer has sent no MQTT message for more than
      `latency_warn_seconds` (default 10), and again when it recovers. Also logs slow monitor ticks.
      Watch for idle printers that go quiet for 10 s and cause noise.
- [ ] `feat/command-log` (`d8bc228`): `log_commands` flag (default off). `/printer` arguments are
      redacted.
- [ ] `feat/autodelete-info-replies` (`ee50e5c`): informational replies in groups are deleted after
      `info_reply_ttl` (default 60 s). DMs are never deleted. Max chose this design.
- [ ] `feat/feedback-commands` (`cc41ea4`): `/reportbug` and `/suggestfeature` post to
      `feedback_chat_id`, or the log chat if that isn't set.
- [ ] `feat/config-everything` (`9ebf082`): table-driven `/config` that covers every setting,
      `/config here`, and `/config disable|enable <command>`.
- [ ] `feat/bed-cooldown` (`4bbb473`): DMs the claimer once the bed reaches the filament's removal
      temperature (PLA 35, PETG 40, ABS/ASA 50, PC 60, default 40). There's an opt-out button.
- [ ] `feat/print-ruined-button` (`605e36c`): "Report ruined print" button on claimed prints. Asks
      the presser for the cause in the main chat; times out after `ruined_report_timeout`
      (default 10 s).
- [ ] `feat/notify-when-done` (`f92a22e`): `/queue <printer>` and `/unqueue`. A one-shot DM when
      the printer becomes free; not a booking system.

## Housekeeping

- [ ] `docs/` is untracked. It holds log reviews, the OpenWrt notes and
      `changelog-d798b20-cfec73c.html` (published at https://claude.ai/artifact/PAPvcsz4T9AHpW9PiZ1WjJ).
      Ask Max before committing it.
