# Interview Agent

[![CI](https://github.com/WongAnneLister/Interview-Agent/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/WongAnneLister/Interview-Agent/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

[中文](README.md) | English

A voice mock-interview agent: upload your resume and a job description (JD), take a 15–30 minute voice interview with an AI interviewer, and get an evaluation report backed by quotes from what you actually said.

- **Questions tailored to the role**: extracts 3–6 weighted competencies from the JD and combines them with your resume to build an interview plan. Stage timing is allocated in code, and you can preview and edit the questions.
- **Two-layer interviewer**: a front-line interviewer handles low-latency conversation while a background evaluator scores answers and decides on follow-ups. If the evaluator times out, the interview continues with the plan instead of stalling.
- **Evidence-backed reports**: every conclusion cites the candidate's own words, and quotes are verified in code so the model can't make them up.
- **Observable cost and tracing**: every vendor call records usage and estimated cost; traces go to a self-hosted Langfuse and metrics to Prometheus / Grafana.

Tech stack: Python (FastAPI, LiveKit Agents) + Next.js. Models come from Alibaba Cloud Model Studio (Bailian), with DeepSeek as the fallback LLM. See the [PRD](docs/PRD-语音模拟面试Agent.md) and the [development plan](docs/Claude-Code开工执行文档.md) (both in Chinese).

> Currently in v0 (personal edition): it runs only on your own machine, and features land one PR at a time in the order of the development plan.

## Development setup

Requirements: Python 3.12, [uv](https://docs.astral.sh/uv/), Git. On Windows, use Git Bash and install make (for example `winget install ezwinports.make`).

```bash
make install   # install dependencies and enable pre-commit hooks
make check     # format check + lint + type check + unit tests; must pass before merging
```

Without make, run the matching commands from the Makefile directly, for example:

```bash
uv sync --directory backend
uv run --directory backend pre-commit install
uv run --directory backend ruff format --check .
uv run --directory backend ruff check .
uv run --directory backend pyright
uv run --directory backend pytest
```

## Development dependencies (on a VPS)

Postgres, Redis, S3-compatible object storage (SeaweedFS), Prometheus and Grafana run in Docker on a VPS. Their ports bind only to the server's 127.0.0.1, and your machine reaches them through an SSH tunnel. First fill in `VPS_HOST / VPS_PORT / VPS_USER` in `.env` and set up passwordless SSH key login.

```bash
make vps-deploy   # deploy or update the dependencies on the VPS (generates random passwords on first run)
make vps-env      # write connection details into your local .env (passwords are not printed)
make tunnel       # open the SSH tunnel; keep it running while you develop
make check-env    # check that every dependency is reachable
make migrate      # migrate the database to the latest version
make seed         # create the local user and write its id to DEFAULT_USER_ID in .env (safe to re-run)
make test-db      # database integration tests: create a temp DB → migrate → test → drop it
```

After changing `backend/src/interview/db/models.py`, generate a migration with `make migration name=add_xxx`, review it by hand, then run `make migrate`.

Once the tunnel is up: Postgres `127.0.0.1:25432`, Redis `:26379`, S3 `http://127.0.0.1:29000` (file browser at `http://127.0.0.1:29001`).

While the tunnel is running, if the local API, voice worker or eval process isn't started, the terminal prints `connect_to 127.0.0.1 port 8000 / 9465 / 9466: failed` every 15 seconds. That's Prometheus on the VPS trying to scrape those processes' metrics; it doesn't affect the forward tunnels to the database and other services.

### Running the API

```bash
make api   # http://127.0.0.1:8000, auto-reloads on code changes; API docs at /docs
```

This needs the tunnel up, `make migrate` and `make seed` done, and the evaluation model (`LLM_EVAL_*`) configured in `.env`. To try the endpoints without calling a real vendor, set `LLM_EVAL_PROVIDER=fake`.
The API listens only on 127.0.0.1. v0 has no login, and every request is treated as the local `DEFAULT_USER_ID` user, so don't expose it to the internet.

### Observability (Langfuse, Prometheus, Grafana)

These live in the compose `observability` profile and can be started and stopped on their own without affecting the core services.

- **Langfuse v4 runs in Docker on your machine** (`deploy/local/docker-compose.yml`): the VPS doesn't have enough memory for ClickHouse, so the local stack brings its own Postgres, Redis, S3 (SeaweedFS) and ClickHouse, capped at about 6 GB of memory in total (give Docker Desktop at least 8 GB). It's independent of the app dependencies on the VPS. Traces are sent straight to your machine, not through the tunnel.
- **Prometheus and Grafana stay on the VPS**: they scrape metrics from the local API, voice worker and eval process through the tunnel's reverse ports.

```bash
make vps-deploy   # upload the latest config (Prometheus scrape jobs, etc.)
make obs-up       # generate random passwords and API keys for local Langfuse (written to .env), start local Langfuse and Prometheus / Grafana on the VPS
make tunnel       # Grafana / Prometheus go through the tunnel
make check-env    # Langfuse / Prometheus / Grafana show OK; Langfuse needs 2–3 minutes for database migrations on first start
make obs-down     # stop them when not in use (data is kept)
```

| Service | URL | Login |
| --- | --- | --- |
| Langfuse | `http://127.0.0.1:23100` | `LANGFUSE_ADMIN_EMAIL` / `LANGFUSE_ADMIN_PASSWORD` in `.env` |
| Grafana | `http://127.0.0.1:23000` | user admin, password is `GRAFANA_ADMIN_PASSWORD` in `.env` |
| Prometheus | `http://127.0.0.1:29090` | none |

The first `make obs-up` downloads about 1.5 GB of images (Langfuse web / worker, ClickHouse). If pulling from Docker Hub is slow on your network, configure a registry mirror in Docker Desktop first. If that still doesn't work, pull the arm64 image on the VPS and stream it back over SSH, for example:

```bash
ssh -p <port> root@<VPS> "docker pull -q --platform linux/arm64 langfuse/langfuse:4 && docker save --platform linux/arm64 langfuse/langfuse:4 | gzip" | gunzip | docker load
```

To verify the whole pipeline, set `OBSERVABILITY_ENABLED=true` in `.env` and run `make obs-smoke`. The script sends a sample trace (HTTP request → database query → LLM generation) and prints its Langfuse URL. It also exposes metrics such as `interview_smoke_total` on `EVAL_METRICS_PORT` for 60 seconds, which you can query in Grafana's Explore.

On first start, Langfuse automatically creates the organization `Interview Agent`, the project `interview-agent-dev` and that project's API keys (`LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` in `.env`), so you don't need to create them in the web UI. Grafana comes with the Prometheus data source preconfigured. When the local API, voice worker or eval process isn't running, its target shows as down in Prometheus; that's expected.

### LiveKit (runs locally)

LiveKit Server runs as a native binary on your machine (the browser and the voice worker are both local, so media never leaves it). Windows (Git Bash) and macOS are supported.

```bash
make fetch-livekit   # prepare LiveKit Server under .tools/livekit/
make livekit         # start it in the foreground; the first run generates LIVEKIT_API_KEY / SECRET and writes them to .env
```

- Windows: downloads the official release and verifies its SHA-256.
- macOS: the official releases don't include macOS builds, so the script fetches the official source at a pinned tag, verifies the commit hash and builds it with Go. You need git, Go (any version from 1.21 up; it downloads the toolchain LiveKit requires) and the Xcode command line tools (`xcode-select --install`). The first build downloads a few hundred MB of dependencies; on networks in mainland China, set `export GOPROXY=https://goproxy.cn,direct` first.

The signaling URL is `ws://127.0.0.1:7880`. The media UDP port 7882 binds to your LAN IP (WebRTC doesn't use loopback addresses as ICE candidates), so your firewall may ask whether to allow incoming connections the first time (choose "Private networks" on Windows, "Allow" on macOS). Nobody can join a room without a token.

## Vendor benchmarks

`make benchmark` tests speech recognition, speech synthesis and the LLM in turn, and writes a report to `docs/benchmarks/<date>.md` (committed to the repo for comparison).
It only needs the vendor settings in `.env`, not the tunnel. It calls the real vendors and costs a small amount (a few tenths of a yuan per run, mostly for recognition time).
To run only some of the tests: `make benchmark args="--skip-asr"`; to add an LLM candidate: `args="--model bailian:qwen3.8-plus"`.

**Preparing recognition data** (recordings and reference texts contain your voice, so they aren't committed; the directory is in .gitignore):

```text
backend/tools/benchmark/data/
  raw/
    001.m4a   original recording (m4a / mp3 / wav from Voice Memos, QuickTime, etc.)
  asr/
    001.wav   16 kHz, mono, 16-bit recording converted from raw/ by make recordings
    001.txt   reference text for the recording (exactly what was said, mixed Chinese and English as spoken), written by hand
  terms.txt   term list: one term per line, optionally "term<TAB>weight" (weight 1–5, default 4)
```

- Aim for 10–20 clips of 10–60 seconds each, close to real interview answers, with technical terms from your resume and JD mixed in.
- Put new recordings in `raw/`, numbered 001, 002, …, then run `make recordings`. It batch-converts them into same-named wav files under `asr/`
  (already converted files are skipped; `FORCE=1` reconverts everything; macOS uses the built-in afconvert, other systems use ffmpeg), and lists recordings that still lack a reference text.
- With a term list, the benchmark temporarily creates a hotword list, compares "without hotwords / with hotwords", and deletes it afterwards (each account can have at most 10 hotword lists).
- The 20 synthesized clips are saved to `backend/tools/benchmark/out/` (not committed) so you can listen to them.

## Configuration

Copy `.env.example` to `.env` and fill it in following the comments. `.env` contains secrets and must never be committed (pre-commit blocks it).

## Layout

```text
backend/   Python backend, voice worker, evaluation and benchmark tools
web/       Next.js frontend (from PR-23)
deploy/    development dependencies on the VPS (vps/) and local service config (local/)
scripts/   scripts for deployment, the tunnel, environment checks, etc.
docs/      PRD, development plan, ADRs, benchmark reports
```

## License

This project is open source under the [MIT License](LICENSE).

When you use third-party services such as Alibaba Cloud Model Studio or DeepSeek, you must follow their terms of service, and you're responsible for any charges. Interview reports are generated by AI and are for practice only.
