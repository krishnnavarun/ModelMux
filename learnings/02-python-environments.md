# Python Environments & Packaging

---

## 1. What it is

A **virtual environment** is a private directory of Python packages belonging to
one project. Without one, `pip install` writes into your system-wide Python, and
two projects needing different versions of the same library conflict.

**pip** is Python's package installer. **PyPI** is the public index it downloads
from. A **wheel** (`.whl`) is a prebuilt binary distribution — already compiled,
just unzip and go.

---

## 2. How it works

### The venv mechanism

`.venv/Scripts/python.exe` is a *separate Python binary*. When you run it, it
looks for packages in `.venv/Lib/site-packages` and nowhere else.

**That is the entire mechanism — a lookup path, not a sandbox.** It provides no
isolation of files, network, or processes. It only changes where imports resolve.

**Activation** (`.\.venv\Scripts\Activate.ps1`) puts that folder first on `PATH`
so bare `python` means the venv's Python. It is pure convenience. Calling
`.venv/Scripts/python.exe` directly does the same job.

### Wheels and the compile fallback

When you `pip install X`, pip looks for a wheel matching your platform. If none
exists, it downloads the source and **compiles it** — which needs a C toolchain
and reliably fails on Windows.

Wheel filenames encode compatibility:

```
torch-2.14.0-cp313-cp313-win_amd64.whl
      │      │     │     └─ platform: 64-bit Windows
      │      │     └─ ABI tag
      │      └─ CPython 3.13
      └─ version
```

A `-none-any.whl` suffix means **pure Python** — no compiled code, works on any
platform and any Python version.

**This is why "does library X support Python Y?" is answerable in seconds** by
querying the PyPI JSON API rather than guessing.

### Pinning

```
fastapi==0.141.1     # exact — a new release cannot silently change behaviour
fastapi>=0.141       # minimum — reproducibility not guaranteed
fastapi              # whatever is newest today. Avoid.
```

---

## 3. How we used it in ModelMux

### Setup

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install fastapi "uvicorn[standard]" httpx python-dotenv
```

`uvicorn[standard]` — the brackets are an **extra**: an optional bundle of
speed-ups (`httptools`, `websockets`, `watchfiles`). **In PowerShell it must be
quoted** — `[` and `]` are wildcard characters to the shell.

### PowerShell execution policy

Windows refuses to run unsigned `.ps1` scripts, which blocks the activation
script:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
```

`-Scope Process` affects **one shell session only** — close the terminal and it
is gone. Deliberately the narrowest option.

### requirements.txt — four lines, not twenty

```
fastapi==0.141.1
uvicorn[standard]==0.52.4
httpx==0.28.1
python-dotenv==1.2.3
PyYAML==6.0.3
tiktoken==0.14.0

# dev
pytest==9.1.1
```

We chose this over `pip freeze`, which would have written ~20 lines mixing our
real choices in with transitive dependencies (`h11`, `idna`, `certifi`,
`pydantic-core`). Both work. **The difference is that a reader of this version
can tell what you picked.** In a frozen file, intent is buried.

### The version reversal — the story to tell

I recommended rebuilding the venv on Python 3.11, reasoning: Stage 3 needs
`sentence-transformers` → PyTorch, and newest-Python often lacks wheels.

Then I checked instead of assuming:

```python
d = json.load(urllib.request.urlopen('https://pypi.org/pypi/torch/json'))
names = [f['filename'] for f in d['releases'][d['info']['version']]]
[n for n in names if 'cp313' in n and 'win_amd64' in n]
# -> ['torch-2.14.0-cp313-cp313-win_amd64.whl']
```

The wheel existed. `sentence-transformers` was pure Python. **Stayed on 3.13.**

I was one step from having the user download and install a second Python on a
generalisation I had not verified. The check cost two seconds.

---

## 4. Interview questions

**Q: What does a virtual environment actually do?**
It creates a directory with its own `site-packages`, and a Python binary that
resolves imports there instead of system-wide. It is a lookup-path change, not
a sandbox — no file, network, or process isolation.

**Q: `venv` vs `virtualenv` vs `conda` vs `poetry`?**
`venv` is stdlib, standard for pure-Python projects. `virtualenv` is the older
third-party version, faster and supports more Pythons. `conda` manages non-Python
binaries too (CUDA, MKL) — common in ML. `poetry`/`uv` add dependency resolution
and lockfiles on top.

**Q: Why pin exact versions? What's the downside?**
Reproducibility — a new upstream release cannot silently change behaviour.
Downside: you don't get security patches automatically, and pins drift stale.
Production services pin; libraries specify ranges, because a library that pins
exactly will conflict with everything.

**Q: `pip freeze` vs hand-written requirements — which and why?**
Hand-written for readability of *intent*; freeze for exact reproducibility. The
mature answer is both: a short `requirements.in` of direct dependencies, compiled
to a fully-pinned lockfile by `pip-compile` or `uv`.

**Q: What is a wheel, and what happens if there isn't one?**
A prebuilt binary distribution. Without a matching wheel, pip downloads the
source and compiles it — needing a C toolchain, which commonly fails on Windows.

**Q: How do you check whether a library supports your Python version?**
Query the PyPI JSON API (`https://pypi.org/pypi/<pkg>/json`) and look for a
wheel with your ABI tag (`cp313`) and platform (`win_amd64`), or a `-none-any`
pure-Python wheel. *(In this project that turned a plan to rebuild the whole
environment into a two-second check that said "don't".)*

**Q: What's `uvicorn[standard]`?**
An *extra* — an optional dependency group declared in the package metadata.
Here it pulls in `httptools`, `websockets`, `watchfiles` for performance. Must
be quoted in PowerShell because brackets are wildcards.

**Q: Why does `Set-ExecutionPolicy -Scope Process` matter?**
Windows blocks unsigned scripts by default. `-Scope Process` lifts it for one
shell session only, rather than machine-wide — the narrowest change that solves
the problem, so nothing persists.

**Q: You inherit a project with no requirements file. How do you rebuild it?**
`pip freeze` in the working environment if one exists. Otherwise, static import
analysis (`pipreqs`) to find top-level imports, then install and run the tests to
find what's missing. Then pin, and write down which are direct vs transitive.
