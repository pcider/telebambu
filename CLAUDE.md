# Notes for agents

Workflow: each feature or fix gets its own branch off `prod` with one commit (for cherry-picking),
and goes through `staging` before `prod`. Open work, deploy steps and planned features are in
`TODO.md`; keep it up to date.

## Known issues (not fixed yet)

- `/print` (`BotHandlers._print_document` in `bot/handlers.py`):
  - The Telegram file download (`get_file` / `download_as_bytearray`) isn't inside a `try`. If it
    fails (network error, or `file_size` is missing so the 20 MB check is skipped), the exception
    escapes the handler and the admin gets no reply.
  - An explicit plate `0` (`/print 3 0`) is silently treated as "no plate given" by `plate or plates[0]`
    and prints the first sliced plate, instead of being rejected as invalid.

## Deliberate behaviour (don't "fix")

- `/forcereclaim` resets the print's settings (stop layer, watchers, notifications) through
  `Storage.unclaim_print`. The new claimer starts fresh on purpose.
- `/config set update_interval` has no upper limit and accepts `inf`, which stops polling until
  `config.yml` is edited by hand. This is documented in the README.
- Legacy "Layer 2 Notify" buttons (`layer2_toggle_N`, from before 1-based printer numbers) don't
  match `_LEGACY_CALLBACK` because of the digit in `layer2`. They are old and not worth handling.
- `/stop_at` sends the stop command once. How the print ended is reported by the normal state
  change (FAILED → "stopped at layer N as requested"), not when the command is sent. `stop_print()`'s
  return value isn't checked.
- `disabled_commands: staff: true` makes the owner the only role (`Storage.roles_enabled`). Admin
  and mod roles stay in `data.json` and come back when it's turned off. While it's on, roles can't
  be changed.
- `/forceunclaim` has no disable flag, so admins can always free up a printer.
- Claimers whose DM Telegram refused are remembered in memory only
  (`MessageService._dm_unreachable`, cleared by `/start`). A restart clears the list, so the bot
  tries the DM once more and may post the "DM unavailable" note in the group once more.
- Periodic camera updates never fall back to the main chat. When the DM is refused, only the
  text note is posted there, once.
- `send_print_finished` ends the session even if sending fails. A timed-out send is not retried in
  the group, so it isn't posted twice.
