# goagx — the node surface on NVIDIA AGX Xavier

`goagx/` is the goSpark node distribution retargeted from the DGX Spark
(GB10) to a Jetson AGX Xavier (64 GB `agx64`, 32 GB `agx32`, `sm_72`), plus
the two-image engine split that makes that possible. It is part of the
fabric, not of either role: same contract (`contracts/`), same service graph,
same ports, same entrypoint — different silicon.

Nothing in `peer-node/`, `master-node/` or `contracts/` changes to run on
Xavier. The platform selection is a setup-script flag.

| File | Role |
|---|---|
| `sparknet.agx.docker` | the AGX node image (goSpark, compute, aux engines, bundle, stub) |
| `sparknet-llm.docker` | the AGX text engine (`llama-server` + precontext proxy) |
| `sparknet.agx.compose.yml` | same 15 services / profiles / ports as `spark.compose.yml`, AGX defaults |
| `sparknet-audio-shim.py` | OpenAI `/v1/audio/transcriptions` over whisper.cpp |
| memory service | `spark-entrypoint memory` runs goSpark's per-vault typed memory and delegate service on loopback `4110`; it is the one memory plane for OpenCode and Open WebUI |
| `bootstrap-agx.sh` | device-side entry: preflight, goSpark at the pin, hand over to the setup script |
| `GOSPARK_PIN` | the one goSpark revision this distribution builds against |
| `publish-templates/` | README + workflow templates for the standalone repo |
| `USER_MANUAL.md` | the developer-facing manual for this surface: the hardware truth table, the deltas from `spark.docker`, the image, the compose surface, the audio shim, the bootstrap, the publish and the gates |

The **master manual** for the whole system lives in the `sparknet` repository
as `USER_MANUAL.md`, and is published at the root of the `goagx` distribution
as well.

## Standalone deployment repo (reallife-spark/goagx)

`reallife-spark/goagx` is the **generated distribution** of this surface: a
private repo holding everything a fresh Xavier needs to become a fully
functioning sparknet node — both role subvaults, `contracts/`, `shared/`, the
setup script, the root files the image build COPYs, and this directory — laid
out as a clone of the fabric so every code path runs unmodified. It is
exported by `scripts/sparknet-goagx-publish.sh` (`make goagx-publish`), never
hand-edited; the next publish overwrites the tree, and `VERSIONS.json` +
`SHA256SUMS` record the sparknet SHA, the goSpark pin and the image pins.

On the device:

```sh
git clone https://github.com/reallife-spark/goagx.git && cd goagx
./goagx/bootstrap-agx.sh     # preflight (aarch64/L4T r35/docker/swap/disk),
                             # goSpark at the pin, then setup --platform agx up
```

`make goagx-drift-check` compares this tree against the published tip
(self-skipping with a notice where the private remote cannot be fetched); the
published repo's generated `verify` workflow re-runs
`sparknet-goagx-publish.sh --verify` on every push. The hardware legs — the
engine image build completing on the Xavier, real tok/s — stay where the table
below puts them: proven on silicon, not in CI.

The goSpark binaries (`bin/` via the BuildKit named context `gospark`) are
consumed **unchanged**; `goSpark` itself is not forked for AGX.

## Resource-first verdict (STYLE_GUIDE §1)

The vLLM source build was the blocker: the operator could not satisfy its
Python/PyTorch/xformers dependency stack on either Xavier host. The
resource-first review of [`dusty-nv`](https://github.com/dusty-nv)
(`references/dusty-nv-jetson-llm-containers-2026-11-13.md` in the fabric)
found the maintained Jetson runtime:

- **`jetson-containers`** — `llama_cpp` (default `b9016`), L4T `>=34.1.0`,
  CUDA build of `llama-server` with an OpenAI-compatible API;
- `ollama` (0.23.0) — the fallback and the peer's own AI-stack server;
- `mlc` is an optional experiment; `vllm`, `sglang`, `tgi`, `tensorrt_edgellm`
  are heavier or not the Xavier path.

**Verdict:** the text engine is `sparknet-llm:agx`, `FROM` the pinned
`dustynv/llama_cpp:r35.2.1` image, with `llama-server` and a thin precontext
proxy. The node image no longer builds vLLM. The stable `vllm` service name,
port 8000, OpenAI endpoints, `vllm-stub` and the aux engines remain. The
decision is `docs/adr/0021-agx-llm-engine.md`; the source review is the
reference page above.

## Hardware truth table

| | DGX Spark (the original) | AGX Xavier (this) |
|---|---|---|
| GPU / arch | GB10, `sm_121a` (Blackwell-family) | Xavier, `sm_72` (Volta) |
| Memory | 128 GB unified | 64 GB (`agx64`) / 32 GB (`agx32`), LPDDR4X (~136.5 GB/s) |
| Stack | cu128-era, torch ≥ 2.7 (base `spark.docker`) | JetPack r35 / L4T r35, **cu114**, Python 3.8, Ubuntu 20.04 (node base `nvcr.io/nvidia/l4t-pytorch:r35.2.1-pth2.0-py3`; engine base `dustynv/llama_cpp:r35.2.1`) |
| Arch build flag | `sm_121a` | `CMAKE_CUDA_ARCHITECTURES=72` in the `jetson-containers` engine image |

## What differs from `spark.docker`

1. **Two images**: the node image (`sparknet-agx-node`, base
   `l4t-pytorch:r35.2.1-pth2.0-py3`) carries goSpark, compute, the aux
   engines, the bundle and `vllm-stub`; the text engine
   (`sparknet-llm:agx`, base `dustynv/llama_cpp:r35.2.1`) carries
   `llama-server` and the precontext proxy.
2. **The text engine is `llama.cpp`**: `llama-server` serves the GGUF on
   port 8000; the precontext proxy injects the MASTERSPIKE text + fabric
   digest as a system message. `VLLM_MODEL` is the alias,
   `VLLM_MAX_MODEL_LEN` maps to `--ctx-size`, `VLLM_EXTRA_ARGS` passes
   extra flags and `SPARKNET_LLM_MODEL_FILE` names the GGUF. The historical
   `VLLM_*` names stay contract.
3. **Role-specific model files**: the setup script writes the teacher
   (Qwen3.6-35B Q4_K_M) on `agx64` and the student (Qwen3.6-27B Q4_K_M) on
   `agx32` into `spark-agx.env`; the compose mounts
   `/opt/sparknet/models` read-only. The active GGUF, tokenizer, precontext
   and KV live on the root NVMe; the vaults and the cold store stay on
   `/mnt/ai`.
4. **Rerank/embed are llama.cpp**: llama.cpp `v0.7.0` is baked with
   `llama-server -h | grep -- --port` as its build gate, built
   `BUILD_SHARED_LIBS=OFF` static, serving `/v1/embeddings` and
   `/v1/rerank` (plus `/v1/reranking` alias).
5. **Audio is whisper.cpp v1.9.5 + the shim**: whisper.cpp serves only
   `/`, `/inference`, `/load`, `/health` — there is no OpenAI transcription
   route to match. `sparknet-audio-shim.py` (fastapi/uvicorn/httpx from the
   compute venv, `python-multipart` for form parsing) exposes
   `/v1/audio/transcriptions` on `VLLM_PORT` and forwards to whisper on
   `127.0.0.1:8004`. ffmpeg is installed so whisper's `--convert` handles
   non-WAV uploads. Both are started by the entrypoint's `whisper-asr`
   subcommand and supervised with `wait -n`.
6. **Defaults fit the host**: `VLLM_MAX_MODEL_LEN=8192`, `--n-gpu-layers 99`,
   `--parallel 1`; the teacher/student file and alias are overridable in the
   env file. MAXN (`sudo nvpmodel -m 0`, `sudo jetson_clocks`) is the
   inference baseline.

Everything else — service names, container names, profiles, published
ports, the env-key surface of the core services, precontext/rag, the
stub/CPU overlay, `--engine stub` smoke — is identical by test
(`tests/test_agx_node.py` asserts the parity).

## The active Qwen3-2507 pair

The Step 110 main/teacher/student pair is:

| Set | Teacher/main (`agx64`) | Student (`agx32`) | State |
|---|---|---|---|
| `qwen3-2507` | Qwen3-30B-A3B-2507 Q4_K_M (17.3 GB) | Qwen3-4B-2507 Q4_K_M (2.33 GB) | active |
| `qwen3.6` | Qwen3.6-35B-A3B Q4_K_M (22.1 GB) | Qwen3.6-27B Q4_K_M (16.8 GB) | cold |

Both active models use the full 262,144 context with q8 KV. Select the set or
switch to the 4B candidate with:

```sh
scripts/sparknet-model-set.py --role master --set qwen3-2507 --precontext on
scripts/sparknet-model-set.py --role peer --set qwen3-2507 --precontext on
scripts/sparknet-model-set.py --role master --set qwen3-4b-candidate --precontext on
```

The helper writes `peer-node/.state/model-set.env` and recreates only the
`vllm` service; the main env file and secrets are untouched. The
`--precontext off` pass measures raw model speed; `--precontext on` measures
the real node behavior with the MASTERSPIKE text + fabric digest. The
`qwen3-4b-candidate` set is the distilled student copy synced to `agx64`.

The peer stack also carries Qwen3-Coder-30B (65k, Ollama/OpenCode),
Qwen3-Embedding-0.6B (RAG/reindex) and Qwen3-VL-4B (Ollama/Open WebUI, 65k).
They are peer models, not the fabric text-engine pair.

### Previous bake-off result (historical)

| Set | Teacher no-pc | Teacher pc | Student no-pc | Student pc | Agreement |
|---|---:|---:|---:|---:|---:|
| `qwen3.6` | 18.54 tok/s | 18.08 tok/s | 4.41 tok/s | 4.54 tok/s | 0.653 |
| `qwen3` | 3.94 tok/s | 3.65 tok/s | 76.78 tok/s | 57.33 tok/s | 0.608 |

This table is the previous Qwen3.6/Qwen3-32B comparison. Step 110 replaces
that pair with the Qwen3-2507 main/teacher/student set; the old measurements
are retained as provenance, not as the current winner.

## Historical: vLLM-era measurements (2026-10-21)

These numbers are kept as provenance for the old vLLM path; they are **not**
the current engine. The hosts were later audited as AGX Xavier 64 GB /
32 GB, not Orin. The Step 108 llama.cpp measurements replace this table when
the live checks run.

Two Jetson nodes ran the whole battery end to end: `agx64` (64 GB, peer) and
`agx32` (32 GB, master, elected signing role). Numbers below are measured,
not estimated.

| | agx64 (peer) | agx32 (master) |
|---|---|---|
| RAM | 64 GB unified | 32 GB unified |
| `VLLM_GPU_MEM_UTIL` | 0.4 | 0.8 |
| Throughput (7B, fp16, 300 completion tokens) | 61.6 s (~4.9 tok/s) | 60.2 s (~5.0 tok/s) |
| Prompt tokens after the precontext cap | 4 771 | 4 771 |
| Profiles resident at once | vLLM + embed + rerank + audio + core | vLLM + core (aux staged) |
| Steady-state RAM | ~25 GB used of 61 GB | ~27 GB used of 30 GB |

Image facts that hold on both: vLLM 0.2.7, torch
`2.0.0a0+ec3941ad.nv23.02`, transformers 4.37.2, xformers 0.0.20, jinja2
3.1.6, whisper.cpp 1.9.5, llama.cpp 0.7.0, goSpark `f547751`, goagx
`5cfe888`. These are the revisions **at measurement time**, not the current
pins; `GOSPARK_PIN` and `VERSIONS.json` carry the current ones.

Four findings that change how the node is run:

1. **`VLLM_GPU_MEM_UTIL` is a fraction of system RAM, not of the GPU.** On
   Jetson, `torch.cuda.mem_get_info()` reports unified RAM, and vLLM 0.2.7
   computes `num_gpu_blocks = (total * util - peak_memory) // block_size`
   with a *system-wide* `peak_memory`. A util of 0.85 on the 32 GB master
   asks for a cache that does not exist: the engine dies with
   `No available memory for the cache blocks`. 0.4 fits the 64 GB peer, 0.8
   fits the 32 GB master — with the aux profiles stopped.
2. **The 32 GB master cannot host Mistral-7B and the aux profiles at the
   same time.** With embed + rerank + audio resident, the engine dies mid
   generation with `torch.cuda.OutOfMemoryError` (18.04 GiB allocated,
   1.87 GiB free). The aux profiles are therefore an *on-demand window* on
   that node: bring them up for the embed/rerank/audio probes, stop them for
   the text battery. The 64 GB peer runs every profile concurrently.
3. **Ollama's keepalive is a 9.6 GB cold load** (`hack1ng-131k`), which does
   not fit beside the resident 7B model on the 32 GB master. The timer
   (`ollama-keepalive.timer`) stays stopped there while vLLM runs; the
   daemon itself is a separate unit and may stay up.
4. **The control plane's admin surface must bind to loopback.** goSpark
   refuses `0.0.0.0:8899` outright (`admin: listen "0.0.0.0:8899" is not
   loopback; the admin surface grants full registry control and must not be
   exposed`), so the compose sets `GOSPARK_ADMIN_LISTEN: 127.0.0.1:8899`
   and the host mapping is loopback-only. Reach it over an SSH tunnel.

Two more that are about the request path rather than the hardware: the chat
precontext has to be capped (`SPARKNET_PRECONTEXT_MAX_BYTES=16000`, ~4 771
prompt tokens) or the 8 192-token context overflows, and the audio shim's
`httpx.Timeout` has to set all four phases explicitly — httpx rejects a
timeout that mixes named phases with an implicit default.

## Build

```sh
# 1. goSpark binaries (unchanged, arm64):
make -C ../gospark build build-gpu          # from a sibling checkout

# 2. the two images (on an arm64 host with Docker BuildKit):
./sparknet-node-setup.sh --platform agx build
# equivalent manual forms (build context IS the fabric root):
docker buildx build --network=host \
  -f goagx/sparknet.agx.docker \
  --build-context gospark=<path-to-gospark> \
  -t sparknet-agx-node:dev --load .
docker buildx build --network=host \
  -f goagx/sparknet-llm.docker \
  -t sparknet-llm:agx --load .
```

`--network=host` is required (no DNS inside the BuildKit sandbox on this
fleet). Budget:

- **Disk**: ~12 GB for the two images plus the active GGUF; the engine base
  is the large layer.
- **Time**: the engine image is a pull plus a few small layers; the node
  image still compiles llama.cpp and whisper.cpp for the aux engines, but
  the hours-long vLLM/xformers build is gone.
- **Model**: stage the teacher/student GGUF under `/opt/sparknet/models/`
  before the first `up`; the engine image does not download a model.

## Run

```sh
./sparknet-node-setup.sh --platform agx up                # text node
./sparknet-node-setup.sh --platform agx --role master up  # + control plane
./sparknet-node-setup.sh --platform agx --engine stub up  # CPU smoke, no GPU
```

`--platform` picks `goagx/sparknet.agx.compose.yml` + `spark-agx.env`
together; the default stays `spark`, so every existing invocation and every
existing test fixture is untouched. Profiles (`embed`, `rerank`, `audio`,
`webui`, …) behave exactly as the runbook's Spark section describes.

The CPU/stub smoke works on AGX too: `spark.compose.cpu.yml` now also selects
the node image and `vllm-stub`, because the LLM image has no stub; the setup
script writes `SPARKNET_NODE_IMAGE` and `SPARKNET_NODE_DOCKERFILE` into
`spark-agx.env` for that overlay. Same warning as ever: the `gpu-sim.json`
fixture is theatre; never use it on a host with, or due to get, a real GPU.

## What is proven where

`contracts/spark-node-runbook.md` keeps its split — everything in its §1
"provable on any host" column proves packaging parity for the AGX image as
well (CPU smoke, stub agent↔engine↔compute path, containerised election);
the `confirm: user` column gains the AGX-specific physics:

- the `jetson-containers` `llama_cpp` engine image loading the teacher on
  `agx64` and the student on `agx32`, and answering the OpenAI endpoints;
- real tokens/s for Qwen3.6-35B Q4_K_M (agx64) and Qwen3.6-27B Q4_K_M
  (agx32) on `sm_72`, with MAXN set;
- the precontext present in the request and the `/health` endpoint green;
- NVML/gpu-sidecar telemetry reading Jetson's driver honestly
  (`available=true` from a driver, not a file);
- whisper.cpp `detect_language` per-request behaviour through the shim;
- `--language auto` (if ever enabled in the engine dialect) at runtime.

The machine-readable parity for services, ports, images, pins and the
setup/entrypoint wiring lives in `tests/test_agx_node.py`; it runs in the
fabric suite (`make test-all`).
