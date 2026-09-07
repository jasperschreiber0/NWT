# NWT production deployment

Production is the Linux checkout at `/home/northworld/trading`. Deploy only a
known GitHub commit. Do not copy individual files from a Windows workspace.

1. Run reconciliation and require `RECON CLEAN` before changing code.
2. Record the current commit, branch, working tree, and create a database
   backup plus a recoverable checkout snapshot.
3. Fetch the approved commit into a separate worktree; do not copy individual
   files over the live checkout.
4. Install only declared dependencies and run compilation plus the full test
   suite before switching production.
5. Apply only the migrations required by that commit, recording the migration
   result.
6. Stop or pause scheduled workers for the switch and confirm no duplicate
   worker is active.
7. Atomically switch the production checkout or update it with a fast-forward.
8. Verify the deployed SHA, cron paths, environment-file names, and worker
   entrypoints.
9. Restart only the intended service processes, if any, and verify their
   command lines and healthbeats.
10. Run reconciliation again and require `RECON CLEAN` before enabling normal
    paper flow.
11. Roll back to the recorded commit and restore the previous scheduler state
    if compilation, tests, health checks, migration, or reconciliation fails.
12. After rollback, run reconciliation again before allowing any paper flow.
   reconciliation fail.

The deployment must not alter existing broker positions. Keep
`mutation_frozen` enabled until the controlled paper open/close acceptance
test has completed and attribution is verified.

