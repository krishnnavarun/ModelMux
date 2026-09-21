# Git, Windows & Tooling

---

## 1. What it is

**Git** is a distributed version control system. Every clone is a full copy of
the history.

**`.gitignore`** lists paths git should not track.

---

## 2. How it works

### The three areas

```
  working directory  ──git add──▶  staging area  ──git commit──▶  repository
   (your files)                      (index)                       (history)
```

`git status` shows what's in each. `??` means untracked — git has never seen it.

### .gitignore and its critical limit

**git only ignores files it is not *already tracking*.**

If a secret is ever committed, adding it to `.gitignore` afterwards does
nothing. It stays in history, and **the key must be revoked**. History rewriting
(`git filter-repo`, BFG) helps only if nobody has cloned or forked.

To stop tracking something already committed:

```bash
git rm --cached path/to/file     # untrack, keep on disk
```

### What belongs in a repo

**Yes:** source, tests, config templates (`.env.example`), pinned dependency
lists, docs, small fixtures.

**No:** secrets (`.env`), virtual environments, build artefacts, caches
(`__pycache__`, `.pytest_cache`), databases, large binaries.

**Why not binaries:** git stores full copies of each version, not diffs, for
binary files. They bloat history permanently and can't be reviewed.

### Line endings: CRLF vs LF

Windows ends lines with `\r\n` (CRLF); Unix uses `\n` (LF).

- **Consistent CRLF** in a file is normal on Windows and harmless
- **A lone `\r`** in the middle of a line is corruption

`git config core.autocrlf true` on Windows checks out CRLF and commits LF.
A `.gitattributes` with `* text=auto` is the more explicit modern approach.

---

## 3. How we used it in ModelMux

### The .gitignore, with reasons

```gitignore
# Environment
.venv/
.env

# Database (also lives outside the project -- see DECISIONS.md D7)
*.db
*.db-wal
*.db-shm

# Python caches
__pycache__/
*.pyc
.pytest_cache/

# Documents -- kept on disk, not in the repo
*.docx
```

`*.db-wal` and `*.db-shm` are there because **WAL mode creates three files, not
one** — ignoring only `*.db` would leave two behind.

### The distinction that caught us out

The project lives under OneDrive, and Stage 1 put a SQLite database in it.

> **`.gitignore` stops git from committing the `.db` files. It does nothing
> whatsoever to stop OneDrive syncing them.**

Two different systems, two unrelated ignore mechanisms. It's easy to see `*.db`
in `.gitignore` and feel the problem is handled. It isn't — the fix was moving
the database to `%LOCALAPPDATA%`, which OneDrive never syncs. → DECISIONS.md D7.

### A real corruption bug: the stray `\r`

I patched `CLAUDE.md` with a Python script using a **non-raw** string containing
a Windows path:

```python
s.replace("...", ".\\.venv\\Scripts\\python.exe eval\run_routing_check.py")
#                                                     ^^ \r = CARRIAGE RETURN
```

`\r` in a normal Python string is a carriage return, not backslash-r. The line
rendered as `evalun_routing_check.py` — the CR returned the cursor to the start
of the line and overwrote it.

**Diagnosis mattered more than the fix.** Rather than assume all three files with
CRs were corrupt, I distinguished normal line endings from stray ones:

```python
crlf = b.count(b'\r\n')
lone = len(re.findall(rb'\r(?!\n)', b))
```

```
CLAUDE.md        LF     crlf=0    lone_cr=0
DECISIONS.md     CRLF   crlf=414  lone_cr=0     <- normal Windows endings
specs.md         CRLF   crlf=591  lone_cr=0     <- normal
config.yaml      CRLF   crlf=101  lone_cr=0     <- normal
```

**Only one file was actually corrupt.** The other three were a false alarm.

*Two lessons:* use raw strings (`r"..."`) for Windows paths, and when a check
flags several files, distinguish the real problem from the expected pattern
before "fixing" all of them.

### Current repo state

Nothing is committed yet beyond the initial README — everything is untracked.
That's why decisions like moving the database or restructuring `learnings/` have
been cheap: no history to rewrite.

---

## 4. Interview questions

**Q: What should never go in a git repository?**
Secrets, credentials, virtual environments, build artefacts, caches, databases,
and large binaries. Secrets because history is permanent; binaries because git
stores full copies per version and can't diff them.

**Q: You committed an API key. What do you do?**
**Revoke it first** — that's the only step that reliably matters, since the repo
may already be cloned, forked, or scraped by a bot. Then rotate, then optionally
rewrite history with `git filter-repo` or BFG and force-push, coordinating with
anyone who has a clone. Adding it to `.gitignore` afterwards does nothing.

**Q: Why doesn't adding a file to .gitignore remove it from the repo?**
`.gitignore` only affects *untracked* files. Once tracked, git keeps tracking it.
Use `git rm --cached <file>` to untrack while keeping it on disk, then commit.

**Q: What's the difference between `git rm --cached` and `git rm`?**
`--cached` removes it from the index only — the file stays on disk and becomes
untracked. Without it, git deletes the working-directory file too.

**Q: How do you handle line endings across Windows and Unix?**
`core.autocrlf=true` on Windows (checkout CRLF, commit LF), or better a
committed `.gitattributes` with `* text=auto` so the rule travels with the repo
rather than depending on each developer's config.

**Q: Difference between a lone CR and CRLF in a file?**
CRLF is a normal Windows line ending. A **lone `\r`** not followed by `\n` is
usually corruption — often from a `\r` escape in a non-raw string. *I hit exactly
that: a Windows path in a Python replacement string put a carriage return
mid-line and silently mangled it.*

**Q: What's your commit hygiene?**
Small, focused commits that each leave the tree working. Imperative subject
lines explaining *why*, not just what. Never commit secrets or generated
artefacts. Branch for anything non-trivial; keep the default branch releasable.
