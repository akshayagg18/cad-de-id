# CAD De-ID

Length-preserving, keyed pseudonymisation of an open-source CAD corpus (Builders Buildathon 2026, Machina track
"CAD De-ID"). Every identity found in the files (person, organisation, e-mail, handle, GitHub/GitLab owner) maps to
**one** pseudonym everywhere: file contents, archive members, folder and file names. Every file keeps its extension,
its place in the tree and a valid internal structure, and there is exactly one output file per input file.

This repository is the code half of the submission. It contains no corpus, no source data, no secrets and no
private mappings.

**Status (2026-11-21):** the pipeline is implemented and passes its 55 unit and end-to-end tests on synthetic data
(invented identities only). It has not yet been run on the full corpus, so there is no official score to report here.

## Design

| Problem | Decision |
|---|---|
| STEP, IGES (Hollerith counts), SAT (counted strings), OLE property sets and ZIP/TAR names all break if a string changes length | **Every pseudonym has the same byte length and case pattern as the original.** No offset, count, size or fixed-width field is ever recomputed. |
| The same person must look identical in every file and format | Keyed HMAC-SHA256 per identity token. A phase-1 registry makes assignments collision-free for harvested names. |
| Over-redaction destroys usable files and relationships | Phase 1 harvests identities **only from identity-bearing fields**. Placeholders (`user`, `Company`, ...) and software vendors are kept. |
| The scorer must still find and open every file | One output per input, extension kept, only identifying path parts renamed, unchanged files hard-linked byte-for-byte. |
| Corrupt or unsupported files | Copied unchanged and listed as defects in the generated `README.txt`. Nothing is dropped. |

Supported formats: STEP, IGES, SAT, STL, text/SVG/XML, OLE2 property sets, PDF Info/XMP, and ZIP/FCStd/3MF/TAR/GZ
containers (recursive, depth <= 4).

## Layout

```
src/deid/   names.py (pseudonyms) | identity.py (registry, matching, path mapping) | harvest.py (phase 1)
            rewriters.py (per-format rewriters) | pipeline.py (CLI: harvest, transform, verify, readme)
tests/      fixtures.py (synthetic files) | test_names_identity.py | test_formats.py | test_e2e.py
scripts/    env.sh, 00_check_env.sh, 10_download.sh, 20_run_pipeline.sh, 30_package_code.sh, 40_upload.sh, vm_bootstrap.sh
```

## Run

Requires Linux, Python >= 3.10, a case-sensitive filesystem, and roughly 300 GB free for the full corpus. Input and
output must be on the same filesystem so unchanged files can be hard-linked.

```bash
pip install -r requirements.txt
python -m pytest tests -q

export WORK=/big/disk/work ATTEMPT=1
bash scripts/00_check_env.sh        # preflight
bash scripts/10_download.sh         # wraps the official get-input.sh
bash scripts/20_run_pipeline.sh     # harvest -> transform -> verify -> README -> package code
MACHINA_UPLOAD_TOKEN=... DEST=gs://.../attempt-1/ bash scripts/40_upload.sh
```

`python -m deid {harvest|transform|verify|readme|all}` accepts `--resume`, `--no-rename-paths` (content-only run),
`--no-rename-owners`, `--no-link` and `--no-deep-step`.

Safety gates: `verify` fails on a wrong file count (`EXPECTED_FILES`, default 188722), missing outputs, identity hits, or a
rewritten output that cannot be parsed. `python -m deid gate` runs at the end of `20_run_pipeline.sh` and again inside
`40_upload.sh`, which refuses to upload an attempt that has not passed it. `--resume` reprocesses any recorded file whose
output is missing or has changed size.

## Security

* The secret salt, the identity list, the path map and the defect log live in `private/` and are never written into
  the upload directory.
* The upload token is read from the environment only and is never written to disk by these scripts.
* `.gitignore` excludes `input/`, `out/`, `private/`, `attempt-*/`, `*.tsv` and token/env files.
* Tests use invented identities only.

## License

MIT, see `LICENSE`.
