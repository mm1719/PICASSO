# Third-party provenance and license review

## Scope

This source-only candidate references dependencies but does not bundle dependency packages, compiled frontend output, container images, model weights, research data, or the original logo. Dependency code installed locally for validation is excluded from the deliverable. This file is an inventory, not a license grant or a complete license-compliance opinion.

No project-wide code license was present in the original tracked source. No new license has been assigned. The project owner confirmed institutional approval for source publication. No additional code-reuse license has been selected or granted by this preparation. Static inspection did not identify attributed vendored source, but it cannot establish originality or ownership of every line.

## Direct dependency references

The following upstream license texts were checked on 2026-11-14. These are project-level references from the then-current upstream default branch, **not verification of every exact pinned wheel, transitive dependency, or binary artifact**. Retain and review the license material shipped with the exact packages before redistributing them.

| Component | Upstream license reference |
| --- | --- |
| React / React DOM | [MIT, upstream license](https://github.com/react/react/blob/main/LICENSE) |
| FastAPI | [MIT, upstream license](https://github.com/fastapi/fastapi/blob/master/LICENSE) |
| Uvicorn | [BSD three-clause terms, upstream license](https://github.com/Kludex/uvicorn/blob/main/LICENSE.md) |
| PyTorch | [BSD-style terms and contributor notices, upstream license](https://github.com/pytorch/pytorch/blob/main/LICENSE) |
| timm | [Apache 2.0, upstream code license](https://github.com/huggingface/pytorch-image-models/blob/main/LICENSE) |
| Pillow | [MIT-CMU, upstream license](https://github.com/python-pillow/Pillow/blob/main/LICENSE) |

A library's code license is not a license for every pretrained checkpoint it can load. No inference checkpoint is included in this candidate, and this review makes no conclusion that dataset terms automatically attach to model weights. Code, data, and each model artifact are separate release questions.

## Frontend lockfile inventory

[Machine-readable npm metadata](docs/NPM_DEPENDENCY_METADATA.json) preserves the version, registry source, integrity field and declared license for all 180 non-root entries in `apps/web/package-lock.json`, including platform-specific optional entries. Every listed entry has a license field. The declarations include MIT, MPL-2.0, Apache-2.0, BSD variants, ISC, MIT-0, BlueOak-1.0.0, CC0-1.0 and 0BSD. These are recorded metadata, not an independent verification of package contents.

The final source archive excludes `node_modules` and `dist`. If a built web bundle or Docker image is later distributed, separately preserve the required notices and audit the contents actually bundled. Do not present this source inventory as that future distribution audit.

## Python and system dependencies

`pyproject.toml` and `requirements/runtime.lock` retain the original application and inference dependency declarations. The optional CUDA runtime includes NVIDIA packages with their own terms. No CUDA package, wheel or container base image is included, and a full transitive Python/system license audit has not been performed.

The small portable test environment is separately specified in `requirements/showcase-dev.txt`; it needs pytest and Pillow, not the inference dependency stack. The new SVG contains only a plain text wordmark and simple geometry; no original artwork or font file is redistributed.

## Release decisions still needed

- This publication is a viewable source showcase without an assigned open-source license. A future reuse-license choice needs separate approval. Do not call the current repository open source.
- Review any newly added weights, dataset samples, screenshots, binary bundle, or container image separately. None is part of this candidate.
