# Starforge Workbench provenance and scope

This repository begins with a clean snapshot of Starforge Workbench at upstream
commit `676f55309cc3b1c58d16b7199e61ca909e2911d9`, recorded on October 6, 2026
at 23:07:49 UTC. The initial snapshot contains 309 tracked files and has Git tree
`0caca4fbcb31a764edda3db2beac1030110f8be5`.

The original MIT license, Randall Embry copyright notice, and third-party notices
are retained. The snapshot starts a new commit history; upstream branches,
issues, pull requests, and development history are not imported.

The public project maintains the legacy Linux tmux/Ptyxis launcher, shared work
API, collectors, and existing browser dashboard. The separate Desktop product is
outside this repository's scope. Future features enter Workbench through focused
review of their implementation, dependencies, documentation, and tests.

The historical source passed its original CI. Local verification on Python
3.12.13 and tmux 3.7c passed 874 tests, with 13 optional Docker/browser checks
skipped. The local harness restored isolated XDG directories between tests
because a historical menu test sanitizes pytest's process environment. The
snapshot's application source and tests were unchanged for that verification.

Changes after the initial snapshot document the public project boundary and
neutralize historical issue hyperlinks that would otherwise point to unrelated
issue numbers in this new repository.

Use a fresh clone for this repository. Existing development clones belong to
their original repository and must not push their branches or history here.
