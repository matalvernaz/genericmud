"""Windows in-app self-update: check GitHub releases, download, swap-in-place, verify.

Two modules:

* :mod:`genericmud.update.self_update` -- the online part: query the repo's releases,
  pick the newest, download the portable zip, and hand it to the bundled
  ``ZipExtractor.exe`` helper to overlay the install and relaunch.
* :mod:`genericmud.update.upgrade_manager` -- the transactional part: back up the
  critical install files (and record their expected hashes) before the overlay, then
  verify + roll back at next startup if the swap only partly landed.

Only the frozen Windows build can self-replace; elsewhere the UI offers the release
page instead. The download path and the rollback path are pure-Python and unit-tested;
the file swap itself needs Windows and is exercised manually / in CI.
"""
