# Notes for agents

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
