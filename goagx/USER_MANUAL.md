---
title: goagx — user manual
updated: 2026-10-30
---

# goagx — user manual

> The manual for the **AGX Xavier twin** of the sparknet node image surface:
> the same node contract, the same 14-service compose graph, the same role
> vaults and the same cross-role contract — on a different piece of silicon
> (Volta, `sm_72`) with a different toolchain (L4T r35, cu114, Python 3.8).
>
> `goagx/` is a **subtree of the fabric**, and the `goagx` repository is a
> **generated distribution** of it. Fixes belong upstream in the fabric; this
> tree is overwritten by the next publish.
>
> The **master manual** for the whole system lives in the `sparknet`
> repository as `USER_MANUAL.md`, and is published at the root of the `goagx`
> distribution as well.

---

## Table of contents

1. [What goagx is](#1-what-goagx-is)
2. [Orientation](#2-orientation)
3. [The hardware truth table](#3-the-hardware-truth-table)
4. [What differs from the Spark image](#4-what-differs-from-the-spark-image)
5. [The resource-first verdict](#5-the-resource-first-verdict)
6. [The image](#6-the-image)
7. [The compose surface](#7-the-compose-surface)
8. [The audio shim](#8-the-audio-shim)
9. [Bootstrap](#9-bootstrap)
10. [The goSpark pin](#10-the-gospark-pin)
11. [Publishing](#11-publishing)
12. [The gates](#12-the-gates)
13. [How to make a change correctly](#13-how-to-make-a-change-correctly)
14. [Configuration reference](#14-configuration-reference)
15. [Memory service](#15-memory-service)
16. [Troubleshooting](#16-troubleshooting)
17. [Spec vs implemented](#17-spec-vs-implemented)
18. [Glossary](#18-glossary)

---

## 1. What goagx is

The fabric ships **one node contract** on **two platforms**:

| | DGX Spark | AGX Xavier |
|---|---|---|
| Platform flag | `--platform spark` (the default) | `--platform agx` |
| Image | `../spark.docker` | `sparknet.agx.docker` |
| Compose | `../spark.compose.yml` | `sparknet.agx.compose.yml` |

The contract is identical: the same 15 services, the same container names,
the same profiles, the same published ports, the same env-key surface, the
same precontext and RAG paths, the same `--engine stub` smoke. The *engine
underneath* differs, and only that.

`--platform` selects the compose file **and** the env-file together, so a
node can never build the AGX image against the Spark compose, and switching
platforms never inherits the other's base image or model lines.

**Who this manual is for:** a developer changing the AGX image surface, the
compose file, the audio shim, the bootstrap script or the goSpark pin — and
anyone deploying a node on an Xavier and wanting to know which knobs exist
and why.

---

## 2. Orientation

| Path | What it is |
|---|---|
| `README.md` | The resource-first record: the verdict, the hardware table, the deltas, the measurements, the build and run commands. |
| `sparknet.agx.docker` | The AGX node image. The build context is the **fabric root**, not this directory. |
| `sparknet.agx.compose.yml` | The 14-service graph for the AGX. |
| `sparknet-audio-shim.py` | The OpenAI-transcriptions adapter in front of whisper.cpp. |
| `bootstrap-agx.sh` | The Xavier preflight and bootstrap: swap, disk, goSpark at the pin, then the setup script. |
| `GOSPARK_PIN` | The one goSpark revision this distribution builds against. |
| `publish-templates/` | The templates the publish renders into the distribution: the root README and the verify workflow. |
| `../contracts/` | The cross-role contract. A node cannot run without it. |
| `../peer-node/`, `../master-node/` | The two role vaults. |
| `../shared/` | The signed-authorization implementation. |
| `../scripts/` | The bundle, env-init and publish tooling. |
| `../sparknet-node-setup.sh` | The one-command bring-up. |

---

## 3. The hardware truth table

| | DGX Spark | AGX Xavier |
|---|---|---|
| GPU / arch | GB10, `sm_121a` (Blackwell family) | Xavier, `sm_72` (Volta) |
| Memory | 128 GB unified | **64 GB (`agx64`) / 32 GB (`agx32`)**, LPDDR4X (~136.5 GB/s) |
| Stack | cu128-era, torch ≥ 2.7 | JetPack r35 / L4T r35, **cu114**, **Python 3.8**, Ubuntu 20.04 |
| Node base | the DGX base | `nvcr.io/nvidia/l4t-pytorch:r35.2.1-pth2.0-py3` (arm64) |
| Engine base | prebuilt vLLM wheels | `dustynv/llama_cpp:r35.2.1` (arm64, `jetson-containers`) |
| Arch build flag | `sm_121a` | `CMAKE_CUDA_ARCHITECTURES=72` in the engine image |

The hosts are **AGX Xavier**, not AGX Orin and not 16 GB. The old 16 GB /
Orin notes are historical; the current hardware truth is the table above.

---

## 4. What differs from the Spark image

Everything below is a deliberate, tested delta. Everything *not* listed here
is identical, and the fabric's test suite asserts that.

### 4.1 Two images, one service contract

The AGX surface is two images:

| Image | Contains | Does not contain |
|---|---|---|
| `sparknet-agx-node` | goSpark, the Python compute plane, the bundle, the aux llama.cpp/whisper.cpp engines, `vllm-stub` | vLLM, the teacher/student GGUF |
| `sparknet-llm:agx` | `llama-server` from `jetson-containers` `llama_cpp`, the precontext proxy | goSpark, the vault, keys, the signer, the aux services |

The compose service name (`vllm`), port 8000, OpenAI endpoints and the
`VLLM_*` environment names are historical contract, not a vLLM dependency.

### 4.2 The engine image

`sparknet-llm:agx` is `FROM ${SPARKNET_LLM_BASE}`, pinned by digest to
`dustynv/llama_cpp:r35.2.1`. It runs `llama-server` on loopback and a small
OpenAI proxy on the published port. The proxy injects the MASTERSPIKE text and
fabric digest as the first system message (merging an existing one), because
this `llama.cpp` build has no `--system-prompt-file` flag. The image carries
no model: `SPARKNET_LLM_MODEL_FILE` is a read-only host bind from
`/opt/sparknet/models`.

### 4.3 The service contract

| Historical name | Maps to |
|---|---|
| `VLLM_MODEL` | the served alias (`llama-server --alias`) |
| `VLLM_PORT` | the proxy's published port (8000) |
| `VLLM_MAX_MODEL_LEN` | `llama-server --ctx-size` |
| `VLLM_EXTRA_ARGS` | extra `llama-server` flags |
| `SPARKNET_LLM_MODEL_FILE` | the GGUF path |
| `SPARKNET_LLM_NGL` | `--n-gpu-layers` (99) |
| `SPARKNET_LLM_PARALLEL` | `--parallel` (1) |

The CPU `vllm-stub` stays on the node image; `--engine stub` layers
`spark.compose.cpu.yml`, which selects the node image and `vllm-stub`.

### 4.4 Rerank and embed are llama.cpp, not vLLM

llama.cpp `v0.7.0` is baked with `llama-server -h | grep -- --port` as its
build gate, built `BUILD_SHARED_LIBS=OFF` (static, because upstream has no
`llama-server` install rule). It serves `/v1/embeddings` and `/v1/rerank`
(plus the `/v1/reranking` alias).

### 4.5 Audio is whisper.cpp plus a shim

See [§8](#8-the-audio-shim).

### 4.6 Role-specific GGUF

| Setting | Value | Why |
|---|---|---|
| Teacher file (`agx64`) | `/opt/sparknet/models/qwen3.6-35b-q4_k_m.gguf` | Largest same-version model that fits 64 GB at a responsive quant. |
| Student file (`agx32`) | `/opt/sparknet/models/qwen3.6-0.6b-q8_0.gguf` | Smallest same-version model at Q8_0. |
| `VLLM_MAX_MODEL_LEN` / `SPARKNET_LLM_CTX` | `8192` | Fits the context with headroom; measured before raising. |
| `SPARKNET_LLM_NGL` | `99` | All layers on the GPU. |

Every one of them is an overridable env-file value. The active GGUF,
tokenizer, precontext and KV live on the root NVMe; the vaults and the cold
model store stay on `/mnt/ai`.

---

## 5. The resource-first verdict

The standing rule in this project is **resource-first**: check the existing
resources before building something of our own, and record the verdict. The
vLLM source build was the blocker: the operator could not satisfy its
Python/PyTorch/xformers dependency stack on either Xavier host. The review of
`dusty-nv` (`references/dusty-nv-jetson-llm-containers-2026-11-13.md` in the
fabric) selected:

- **`jetson-containers` `llama_cpp`** — `llama-server`, L4T `>=34.1.0`,
  CUDA, OpenAI-compatible API; the primary engine;
- **Ollama** — the fallback and the peer's own AI-stack server;
- **MLC-LLM** — optional experiment; `vLLM`, `SGLang`, `TGI`,
  `TensorRT-Edge-LLM` are heavier or not the Xavier path.

**Verdict:** the text engine is `sparknet-llm:agx`, `FROM` the pinned
`dustynv/llama_cpp:r35.2.1` image. The node image no longer builds vLLM. The
decision is `docs/adr/0021-agx-llm-engine.md` in the fabric.

---

## 6. The images

```sh
# The build context IS the fabric root — not this directory.
cd <fabric-root>
docker buildx build --platform linux/arm64 \
  -f goagx/sparknet.agx.docker \
  --build-context gospark=<path-to-gospark> \
  -t sparknet-agx-node:dev --load .
docker buildx build --platform linux/arm64 \
  -f goagx/sparknet-llm.docker \
  -t sparknet-llm:agx --load .
```

Node image stages and pins:

| Stage / arg | Value |
|---|---|
| `L4T_BASE` | `nvcr.io/nvidia/l4t-pytorch:r35.2.1-pth2.0-py3` (pinned by digest) |
| `OPENCODE_VERSION` | `2.2.5` |
| `NATS_VERSION` | `2.14.2` |
| `LLAMA_CPP_REF` | `v0.7.0` |
| `WHISPER_CPP_REF` | `v1.9.5` |
| `TARGETARCH` | the build architecture |

Engine image:

| Arg | Value |
|---|---|
| `SPARKNET_LLM_BASE` | `dustynv/llama_cpp:r35.2.1` (pinned by digest) |

The node image carries the goSpark binaries (copied from the `gospark` stage),
the compute plane (`/opt/sparknet/compute/`), the goSpark configs, the peer
bundle tarball, the precontext builder, the audio shim, and the entrypoint at
`/usr/local/bin/spark-entrypoint`. The engine image carries `llama-server`,
the precontext proxy and the baked MASTERSPIKE/digest text.

## 7. The compose surface

`sparknet.agx.compose.yml` declares the **same 15 services** as the Spark
compose, with the same names, container names, profiles and published ports:

`nats` · `vllm` · `vllm-embed` · `vllm-rerank` · `vllm-audio` · `compute` ·
`kvrouter` · `agent` · `control` · `gpu` · `webui` · `ingest` · `backup` ·
`opencode`

Profiles: `kv` (kvrouter), `control`, `webui`, `ingest`, `backup`,
`opencode`, `embed`, `rerank`, `audio`.

The state directory is bind-mounted from the host:
`${SPARKNET_STATE_HOST:-../peer-node/.state}` → `/srv/sparknet/peer-node/.state`.

The text engine no longer needs host IPC or a large shared-memory segment:
`llama-server` is a single process, and the aux engines are CPU-only. The
state bind mount and the env-key surface of the core services are unchanged;
the `vllm` service mounts only the root model directory read-only.

The fabric's test suite asserts the parity: the same service set, the same
container names, the same profiles, the same published ports.

---

## 8. The audio shim

`sparknet-audio-shim.py` exists because of a genuine API mismatch:

- whisper.cpp's server speaks its own `POST /inference` with a multipart
  body, plus `/`, `/load`, `/health`.
- the node's audio contract — and what every OpenAI-compatible client
  actually sends — is `POST /v1/audio/transcriptions`.

The shim exposes `/v1/audio/transcriptions` on `VLLM_PORT` and forwards to
whisper on `127.0.0.1:8004`. **The engine underneath differs; the contract
does not.**

Field mapping: `file`, `response_format`, `language` and `prompt` are
understood; `model` is accepted and ignored (one baked model per image);
anything else a client sends is dropped, and whisper's own fallback ladder
handles `temperature`. Known engine responses are relayed unchanged, so
`text` unwraps to a bare string exactly as OpenAI specifies.

Two implementation notes that matter if you touch it:

- it runs from the **compute venv** (fastapi / uvicorn / httpx), with
  `python-multipart` for form parsing;
- it must stay **Python 3.8 compatible** — the L4T r35 base has Python 3.8,
  where a PEP 604 `X | None` annotation cannot survive FastAPI's runtime
  annotation evaluation, so `Optional[...]` is the correct spelling there and
  not a stale one. The file carries an explicit `ruff: noqa` for that reason,
  and the publish verifier checks the audio shim's Python 3.8 grammar.

Both whisper and the shim are started by the entrypoint's `whisper-asr`
subcommand and supervised with `wait -n`, so if either dies the pair goes
down rather than leaving a half-serving profile.

---

## 9. Bootstrap

`bootstrap-agx.sh` is the device-side entry point. It preflights the
hardware, uses `./gospark` when a fabric checkout is present (otherwise fetches
goSpark at the pin into `.cache/gospark`), builds it, and then hands over to the
fabric's setup script.

```sh
./bootstrap-agx.sh                       # preflight + goSpark (or the pin), then setup
./bootstrap-agx.sh --gospark DIR         # use an existing goSpark checkout
./bootstrap-agx.sh --allow-low-swap      # continue despite < 8 GiB swap
./bootstrap-agx.sh --allow-low-disk      # continue despite < ~25 GB free
./bootstrap-agx.sh -h                    # this text
```

### 9.1 The preflight

| Check | Floor | Override |
|---|---|---|
| Swap | 8 GiB (`8388608` KB) | `--allow-low-swap` |
| Free disk | ~25 GB (`25600` MB) | `--allow-low-disk` |

Both floors are conservative. The node image still compiles the two aux
engines (llama.cpp and whisper.cpp) and pulls the large engine base; a
machine that runs out of disk or memory mid-build fails with an error that
does not obviously point at the cause. The overrides exist for machines where
the operator has judged the risk; they are not the default.

### 9.2 What it does, in order

1. Resolve its own location, so it works from a clone at any path.
2. Read `GOSPARK_PIN` for the repository and the SHA.
3. Preflight swap and disk.
4. Fetch goSpark at the pinned SHA into the goSpark directory — the repo is
   **private**, so the device needs a fine-grained PAT or a deploy key with
   read access. Never commit one.
5. Build the goSpark binaries (`make build build-gpu`).
6. Hand over to `../sparknet-node-setup.sh --platform agx`.

---

## 10. The goSpark pin

`GOSPARK_PIN` is the single source of truth for which goSpark revision this
distribution builds against. It carries two values:

```
GOSPARK_REPO=https://github.com/reallife-spark/gospark.git
GOSPARK_SHA=91a6ef9ad28a0f4daa3a33eb1e80a026274125fa
```

Consumed by:

- `bootstrap-agx.sh` — fetch + build on the device;
- `../scripts/sparknet-goagx-publish.sh` — `VERSIONS.json` in the published
  manifest.

**A bump is a reviewed commit:** change the SHA, run the publish, and note
the goSpark changelog in the release message. The SHA is also pinned in the
published repository's `VERSIONS.json` and `SHA256SUMS`, so a distribution
always states exactly which goSpark it was built against.

---

## 11. Publishing

The `goagx` repository is **generated**. It is exported from the fabric in
one direction and is never hand-edited.

```sh
make goagx-publish        # from the fabric root: export and push
make goagx-drift-check    # fail if the published repo drifted from this tree
```

The export surface is selected with git's own view of the tree: tracked
files, plus untracked files git would not refuse to add. That keeps `.env`,
`.state`, `.obsidian` and the staged bundle tarball out automatically —
**anything gitignore hides in the fabric stays hidden in the distribution**,
so the staged tree and the published commit can never disagree on contents.

Exported subtrees: `peer-node`, `master-node`, `contracts`, `shared`,
`scripts`, `.opencode/skills`, `.opencode/agents`, `goagx`.

Root files: the entry scripts, the compose files, both dockerfiles, the
setup script, the bootstrap script, `.env.example`, `.dockerignore`,
`.gitignore`, `opencode.json`, `requirements.txt`, and the master
`USER_MANUAL.md`.

The published root README is rendered from
`publish-templates/README.md.tmpl` — never edited in place — and carries the
published timestamp, the fabric SHA, the goSpark SHA, the pins, the deploy
commands and the layout table.

The distribution ships the whole manual set, and every one of them resolves
inside the distribution:

| In the published tree | Covers |
|---|---|
| `USER_MANUAL.md` | The master manual for the whole system. |
| `goagx/USER_MANUAL.md` | This surface (this file). |
| `peer-node/USER_MANUAL.md` | The peer role. |
| `master-node/USER_MANUAL.md` | The master role. |

That is possible because the distribution carries `contracts/` and `shared/`:
the cross-repository references in those manuals are real files here, not
promises.

The distribution also ships `VERSIONS.json` and `SHA256SUMS`, and a `verify`
workflow that runs the publish tool's `--verify` mode on every push.

---

## 12. The gates

From the fabric root:

```sh
make ci                  # the full local pre-merge gate
make goagx-drift-check   # the published distribution still matches this tree
```

The AGX surface has its own dedicated test in the fabric suite, which
asserts the parity with the Spark image: the same 15 services, the same
container names, the same profiles, the same published ports, and the same
env-key surface. A change that drifts the two platforms apart fails there
rather than on an Xavier.

The publish tool's `--verify` mode checks the SHA256SUMS manifest, the pin
files, the `--help` surface of the entry scripts, the audio shim's Python 3.8
grammar, and the compose resolution.

---

## 13. How to make a change correctly

### 13.1 Changing the AGX images

- Keep every pin an `ARG` with an explicit default, so a build can be
  overridden without editing the file.
- Keep the node image and the engine image separate. The node image carries
  goSpark, compute, the aux engines, the bundle and `vllm-stub`; the engine
  image carries `llama-server` and the precontext proxy.
- Keep the engine base pinned by digest (`SPARKNET_LLM_BASE`) and the model
  file explicit (`SPARKNET_LLM_MODEL_FILE`); never bake a model into an image.
- Keep the audio shim **Python 3.8 compatible**. The linter's opinion does
  not override the base image's interpreter.
- Every `COPY` source must be in the export manifest; the publish's
  `COPY` audit fails the build if it is not.

### 13.2 Changing the compose file

- **Every change must also be made in the Spark compose**, or the parity
  test fails. That is the point of the test: the two platforms differ in the
  engine, not in the contract.
- Keep the same service names, container names, profiles and published ports.
- Keep the state bind-mount and the env-key surface identical.

### 13.3 Changing `bootstrap-agx.sh`

- Keep the preflight floors, or change them deliberately *and* record why.
- Keep the pin indirection: the script must not hard-code a goSpark SHA.
- Keep it resolving its own location, so it works from a clone at any path.

### 13.4 Changing `GOSPARK_PIN`

- Change the SHA, run the publish, and note the goSpark changelog in the
  release message.
- Do not hand-edit `VERSIONS.json` or `SHA256SUMS` in the published
  repository — they are generated.

### 13.5 Writing documentation

- One page per file, in its category directory.
- Cite sources. A distilled page without provenance is a guess — mark it as
  one.
- Check the spec/impl boundary before making a claim.

---

## 14. Configuration reference

| Thing | Where | Notes |
|---|---|---|
| Model file | `SPARKNET_LLM_MODEL_FILE` | Teacher on `agx64`, student on `agx32`; a read-only bind from `/opt/sparknet/models`. |
| Model alias | `VLLM_MODEL` | The OpenAI model id; defaults to `qwen3.6-35b` / `qwen3.6-0.6b`. |
| `VLLM_MAX_MODEL_LEN` | compose env | `8192`; maps to `llama-server --ctx-size`. |
| `SPARKNET_LLM_CTX` | compose env | Explicit context override; empty falls back to `VLLM_MAX_MODEL_LEN`. |
| `SPARKNET_LLM_NGL` | compose env | `99` GPU layers. |
| `SPARKNET_LLM_PARALLEL` | compose env | `1` server slot. |
| `SPARKNET_LLM_BASE` | build arg | The pinned `jetson-containers` `llama_cpp` image. |
| `SPARKNET_MODELS_HOST` | compose env | Host root model directory; `/opt/sparknet/models` by default. |
| `WHISPER_UPSTREAM` | shim env | `http://127.0.0.1:8004` by default. |
| `SPARKNET_IMAGE_TAG` | compose env | The image tag; `dev` by default. |
| `SPARKNET_STATE_HOST` | compose env | The host state directory; `../peer-node/.state` by default. |
| `SPARKNET_PLATFORM` | setup script | `spark` or `agx`; picks the compose and the env-file together. |
| goSpark pin | `GOSPARK_PIN` | The one goSpark revision this distribution builds against. |
| Swap floor | `bootstrap-agx.sh` | 8 GiB. |
| Disk floor | `bootstrap-agx.sh` | ~25 GB (the two images plus the active GGUF fit in ~12 GB of image layers). |

**Secrets** live on the host, never in an image layer. The device needs
credentials to read the private goSpark repository — a fine-grained PAT or a
deploy key — and those are supplied to the environment, never committed.

---

## 15. Memory service

The node runs `spark-entrypoint memory`, which starts `sparknet-memory` on
loopback `4110`. It stores one typed-memory store per vault and delegate runs;
it holds no key and signs nothing. The Python memory gateway (`4093`) is the
authority front door: a signed grant or an Open WebUI session is narrowed to one
vault before the Go store is touched. The service is part of the default
profile, so OpenCode and Open WebUI see the same memory for the same vault.

## 16. Troubleshooting

| Symptom | Likely cause | Where to look |
|---|---|---|
| The image build OOM-kills | The node image still compiles the aux engines; the machine has under 8 GiB swap. | `bootstrap-agx.sh`'s preflight; `--allow-low-swap` if you have judged the risk. |
| The image build fails on disk space | Under ~25 GB free. | `--allow-low-disk`; the disk floor. |
| `llama-server` is missing | The `vllm` service is running the node image, not `sparknet-llm:agx`. | The `vllm` service image/build; `SPARKNET_LLM_TAG`. |
| The engine starts but cannot find the model | `SPARKNET_LLM_MODEL_FILE` is unset, or the file is not under the mounted `/opt/sparknet/models`. | The env file; `SPARKNET_MODELS_HOST`; the GGUF path. |
| The GPU is not used | `--n-gpu-layers` is low, or MAXN is not set. | `SPARKNET_LLM_NGL=99`; `sudo nvpmodel -m 0`; `sudo jetson_clocks`. |
| The audio profile returns 404 | The shim is not running, or whisper is not up. | `spark-entrypoint whisper-asr`; `WHISPER_UPSTREAM`. |
| The shim fails at import on a modern host | The file is Python 3.8 grammar by design — `Optional[...]`, not `X \| None`. | The file's `ruff: noqa` header; the verifier's 3.8 grammar check. |
| The model does not fit in memory | The wrong role model is staged, or the context is too large. | The teacher/student file; `VLLM_MAX_MODEL_LEN`/`SPARKNET_LLM_CTX`; stop the aux profiles. |
| The compose does not start on the Xavier | The wrong platform's env-file was used. | `--platform agx`; `spark-agx.env`. |
| `make goagx-drift-check` fails | The published repository is behind this tree. | `make goagx-publish`. |
| The publish refuses | The private remote cannot be fetched. | It self-skips with a notice; check credentials. |
| The node comes up but the guard will not bind | No overlay, or an empty registry. The shipped registry is empty on purpose. | `make -C peer-node guard-status`; `../contracts/deployment-runbook.md`. |
| The device cannot fetch goSpark | The repository is private and the device has no credential. | A fine-grained PAT or a deploy key with read access. Never commit one. |

---

## 17. Spec vs implemented

Broadly:

- **Implemented and exercised** — the two-image AGX split (node +
  `sparknet-llm:agx`), the 14-service compose surface, the precontext proxy,
  the audio shim, the llama.cpp embed/rerank profiles, the bootstrap
  preflight and the goSpark fetch, the publish and drift-check tooling, and
  the platform parity assertions. The old vLLM battery was run on real nodes
  on 2026-10-21 and is kept as historical provenance in `README.md`; the
  Step 108 llama.cpp live checks are the current gate.
- **Contracted and scaffolding** — the master-side surfaces the AGX inherits
  from the fabric (registry push listener, scheduler, share ratification).
- **Deliberately parked** — the destructive legs of the deployment runbook
  (rollback; replace-Master re-formation) are documented and deliberately
  left unexecuted by operator decision.

The honest test: **if you cannot point at a command that exercises it, it is
specified.**

---

## 18. Glossary

| Term | Meaning |
|---|---|
| **goagx** | The AGX Xavier twin of the node image surface, and the generated distribution of it. |
| **AGX Xavier / Orin** | NVIDIA Jetson AGX, Volta `sm_72` (Orin is the newer Ampere variant). |
| **L4T r35 / JetPack 5** | The base OS branch: cu114, torch 2.0.0, Python 3.8, Ubuntu 20.04. |
| **`7.0+PTX`** | The arch build flag for this family; the `sm_72` path arrives by PTX JIT. |
| **Resource-first** | Check NVIDIA's catalog before building our own, and record the verdict. |
| **Escape hatch** | The `*_GIT_URL` / `*_GIT_REF` args that let a pinned build be redirected without forking the file. |
| **Audio shim** | The OpenAI-transcriptions adapter in front of whisper.cpp. |
| **Pin** | A recorded exact version — a SHA, a tag or a ref — that a build is bound to. |
| **Publish** | The one-way export of the fabric tree into the `goagx` repository. |
| **Drift check** | The proof that the published distribution still matches the fabric tree. |
