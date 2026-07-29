# Contributing

Thanks for looking. Please read the first section before opening a pull request —
this repository works differently from most, and knowing how will save you a
surprise.

## This repository is a mirror

Development happens on a canonical repository that is not reachable from the
public internet. What you see here is a **push mirror** of it.

Two consequences, and the second one is the surprising one:

1. **The mirror carries branches and tags only, and it overwrites.** Anything
   committed directly to this repository — a merge commit, a hotfix, a branch —
   is destroyed by the next mirror push. So nothing is ever merged here, by
   anyone, including us. **Your fork is not at risk**: its branches live in your
   repository, which the mirror never writes to. Only this repository's own refs
   are overwritten.

2. **Your pull request will be closed rather than merged, and that is not a
   rejection.** We review it here, apply the change upstream, and it arrives back
   in this repository through the mirror — carrying your commit, with you as its
   author.

We would rather tell you that up front than have you discover it from a closed PR
with no explanation.

**Your authorship survives that round trip.** The commit that lands upstream keeps
you as its author, so `git log` here credits you, not us. If a change of yours
ever appears without your name on it, that is a mistake — please say so and we
will fix it.

If this arrangement is a dealbreaker for you, that is completely reasonable.
Opening an issue costs you nothing and is genuinely useful to us.

### What happens to your pull request

1. We review it here — this repository is where that conversation happens. The
   GitHub Actions run on your PR is the same suite the upstream gate runs, so a
   green check here is a real prediction of the upstream result rather than a
   weaker parallel check.
2. We fetch your branch from your fork and apply it on the canonical side, with
   you as the commit's author.
3. The mirror pushes that upstream `main` here, so your change appears in this
   repository's `main`.
4. We close your pull request with a reference to the commit that landed.

**Steps 3 and 4 are not simultaneous, and 3 comes first.** In between, your pull
request is *open* while its code is already on `main` — and because the landed
commit is usually a different hash than the one you pushed, GitHub will not close
the PR by itself. **That state is expected, not a bug and not us ignoring you.**
If you catch us in that window, look for your change in `main`; the close, with
the commit reference, follows behind it. If it does not, say so on the PR.

### Landing a pull request (for maintainers)

Because nothing can be merged on this side, intake is fetch-and-apply. Configure
the mirror once as a **fetch-only** remote, with a refspec that brings in pull
request heads so no per-contributor remote is needed:

```bash
git remote add github https://github.com/caderuntime/blender-bridge.git
git config --add remote.github.fetch '+refs/pull/*/head:refs/remotes/github/pr/*'
git remote set-url --push github no-push://mirror-is-written-only-by-the-canonical-upstream
```

That last line is not decoration. Pushing to this remote is exactly the mistake
the whole arrangement exists to prevent, so make it fail loudly rather than rely
on remembering.

Then, for pull request `123`:

```bash
git fetch github
git switch -c pr-123 github/pr/123   # the contributor's commits, verbatim
git rebase origin/main               # replay them onto current main
# run the suite, then land it on the canonical remote (origin) and let the
# mirror carry it back here
```

`rebase` and `cherry-pick` both preserve the `Author:` field — that is what keeps
the promise above. If a squash is genuinely warranted, the contributor stays the
author of the resulting commit, or gets a `Co-authored-by:` trailer. Close the
pull request afterwards with the landed sha, promptly: the gap between the mirror
push and that close is the confusing window described above, and it is on us to
keep it short.

## Issues

GitHub Issues is the external tracker and we read it. It is deliberately **not**
synced with our internal work queue, so:

- an issue here may be worked on without the issue visibly moving, and
- you will see identifiers like `showcade-abc1` in source comments. Those are
  internal tracker references and resolve to nothing public. They are kept
  because the comment carrying one is usually explaining *why* the code is the
  way it is, and that id is where the reasoning was worked out — a dead
  reference next to a live explanation beats no reference at all.

A good bug report says which **Blender version** you are on, what you sent, and
what the addon reported. The addon's error messages are written to name a fix
rather than a status code, so quoting one exactly is usually enough.

## Running the tests

There is nothing to install. The addon and its tests are standard library only,
and you do not need Blender:

```bash
python3 -m compileall -q .                                  # syntax gate
PYTHONPATH=. python3 -m unittest discover -s test -t test   # the suite
```

Linting matches CI:

```bash
pip install ruff==0.14.2 mypy==1.18.2
ruff check .
mypy --config-file pyproject.toml
```

To try your change in Blender, symlink the working tree into Blender's extensions
directory and restart Blender after each edit:

```bash
python3 package.py link
```

## Things the code will not accept

These are constraints rather than preferences, and CI enforces most of them.

- **No third-party dependencies. None.** Blender ships no package manager for
  extensions, so a dependency is not an inconvenience — it makes the addon
  uninstallable. `transport.py`, `exporter.py`, `policy.py` and `worker.py` are
  standard library only.
- **Keep `bpy` out of the Blender-free modules.** The four above must import no
  `bpy`, which is what lets the whole transport and every send decision be tested
  without Blender in the loop. `README.md` marks which modules are which.
- **Never touch `bpy` data from the worker thread.** It runs off the main thread,
  where that is undefined behaviour. What crosses the boundary is bytes, not the
  scene.
- **All glTF compression paths stay off.** A compressed GLB is one showcade
  silently fails to load, which is the worst kind of bug to hand a user.

## The catalog contract

The addon talks to a catalog service over HTTP. That contract is published, and it
is the authority:

**<https://cade.run/api/assets-http/>**

The test suite fakes that service in-process so it can run anywhere. That fake
proves the addon agrees with an implementation written from the same reading of
the contract — which is a weaker claim than agreeing with the real service. If you
change a behaviour the fake implements, you are asserting something about the
contract, so check that page first.

## Licence

This addon is **GPL-3.0-or-later** and links against `bpy`, so contributions are
under the same licence. Every source file carries an `SPDX-License-Identifier`
header; please keep it on files you add.

**There is no CLA and no DCO.** Nothing to sign, no account to link, and no
`Signed-off-by` trailer to remember — `git commit -s` is welcome but changes
nothing. Opening a pull request offers the change under GPL-3.0-or-later, the
same licence everything here is already under: inbound matches outbound, which is
the ordinary arrangement for a GPL project. We state it rather than leave it
unsaid, because silence on the subject is what makes people ask whether a
signature is coming.
