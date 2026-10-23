# TODO

Last reviewed 2026-10-23. Items marked **(ask Max first)** need a design decision before any code.

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
- [ ] Merge or cherry-pick `feat/upload` (`c1a57f8`, one commit off `prod`) into `staging`, then
      `prod`. Not pushed yet.
- [ ] Test `/upload <printer> force` on a real printer while it's printing. Check that writing to
      the SD card mid-print doesn't disturb the print. Without `force`, `/upload` refuses while
      RUNNING, PAUSE or PREPARE.
- [ ] Set up CI/CD for prod on the OpenWrt router. `docs/openwrt-ci-cd.md` has setup notes, but none
      of it is set up yet. That includes pinning `requirements.txt`, including the `bambulabs_api`
      commit.
- [ ] Optional: make plain `git push` work. SSH to GitHub hangs on this network, and HTTPS through
      `gh` works. Run `gh auth setup-git` and set a global `url."https://github.com/".insteadOf
      git@github.com:`.

## Bugs

- [ ] "Filament is running low" bug. No details recorded yet: get the symptom or a log excerpt from
      Max. The check is `PrinterMonitor._check_low_filament` (`printers/monitor.py`). It alerts once
      per tray when the active tray's `remain` is 0–10%. `remain` is -1 when the printer can't tell.
- [ ] `/print` has two known issues, listed in `CLAUDE.md`: a failed download gets no reply, and
      plate `0` is treated as "no plate given".

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

## Features

- [ ] **Keep the main chat quiet.** Settings and notification commands should only work in DMs, and
      users shouldn't be able to run commands in the main IMBS supergroup.
      Today every command works in any chat; the only check is the one that deletes `/printer`
      messages containing access codes from groups.
      Overlaps with the channel whitelist below, so plan the two together.
- [ ] **Claim by DM.** Replace the inline "Claim Print" button, which claims straight away, with a
      button that opens a DM with the bot. The user then sends `/claim <printer>` there themselves.
      Today it's `ui.claim_keyboard` (a callback) plus `ui.start_dm_keyboard` (a `/start claim-N`
      deep link).
- [ ] **Anonymous prints (opt-in).** Claimers can set a default, stored in their user preferences
      next to `default_dm_preference`, so their name isn't announced in the main chat. Names are
      still shown by default. For a claimer who opted in, enforce the boundary strictly:
      - Their name must not appear anywhere other users can see it. That covers main chat messages
        (claimed, finished, failed, the status message), `/info` and other replies in groups, and
        buttons.
      - Only the log chat shows who claimed the print.
      - Enforce it in one place, e.g. a single display helper used by every message outside the
        log chat, not at each call site.
      - Add a test that fails if an anonymous claimer's name reaches the main chat.

      Places that show the name today include "Printer N has finished printing. (@user)" and the
      "DM unavailable" note.
- [ ] **Log print start/finish and claims/unclaims to the log chat.** Today only pause/resume,
      `/speed`, `/skip`, `/stop_at`, force reclaim/unclaim, restarts, `/home`, `/print`, `/upload`,
      role, config and printer changes are logged. Ordinary claims, unclaims, starts and finishes
      aren't.
- [ ] **Easter egg: `/beep <printer>`** makes an IDLE or FINISH printer beep.
- [ ] **Latency check.** If a printer's last update is more than 10 s old, log it. Today there's
      only the periodic stats report (`stats_log_interval`), with min/avg/max time between ticks.
- [ ] **Log every command per user**, behind a configurable flag.
- [ ] **Channel whitelist (ask Max first).** Limit the chats where the bot answers commands. Plan
      what makes sense with Max before building.
- [ ] **Auto-delete long informational replies (ask Max first).** Delete `/help`, `/info` and
      similar replies after 60 s. Plan with Max first.
- [ ] **`/reportbug` and `/suggestfeature`** post the user's message to a configured chat.
- [ ] **Configure everything through the bot.** The owner should be able to set every channel and
      setting with commands. Today `/config set` covers `chat_id`, `status_chat_id`, `log_chat_id`
      and `update_interval`. It doesn't cover `stats_log_interval`, `auto_restart_printers`,
      `disabled_commands`, or chats for any new features such as bug reports.
- [ ] **Bed cool-down notification.** Tell the claimer when the bed has cooled to a temperature
      that suits the loaded or reported filament, so the print can be removed.
- [ ] **"Your print failed" button.** Anyone can press it to notify the claimer that their print is
      ruined, mainly for paused or cancelled prints. The bot asks the presser, in the main chat, to
      reply with the cause. It deletes the question once they reply, or after a configurable
      timeout (default 10 s). The question must tell them to reply.
- [ ] **Remind someone when a print is done.** A command, either reused `/notify` or a new
      `/queue`, that lets anyone ask to be DMed when a printer's current print finishes. It
      shouldn't become a queue or booking system.
      `/notify_user` partly covers this, but only the claimer or an existing watcher can add people,
      and only to a claimed print.

## Housekeeping

- [ ] `docs/` is untracked. It holds log reviews, the OpenWrt notes and
      `changelog-d798b20-cfec73c.html` (published at https://claude.ai/artifact/PAPvcsz4T9AHpW9PiZ1WjJ).
      Ask Max before committing it.
