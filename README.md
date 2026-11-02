# Recommendation System template

Hệ gợi ý khép kín: **huấn luyện → cổng đánh giá → đăng ký → phát hành (shadow/canary) → phục vụ → giám sát drift → quyết định retrain**.

Dự án này được dựng lại từ đầu theo [docs/THIET-KE-HE-THONG.md](docs/THIET-KE-HE-THONG.md); không kế thừa mã cũ. **Đọc [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) trước**: tài liệu thiết kế là lộ trình, nhiều mục trong đó mô tả cấu trúc cũ. Đóng góp: [CONTRIBUTING.md](CONTRIBUTING.md).

- **Một file cấu hình duy nhất: [pyproject.toml](pyproject.toml)** — dependency, extras theo vai trò, entry point, ruff, mypy, pytest, coverage.
- Môi trường ảo do `uv` tạo từ `pyproject.toml` + `uv.lock`. Không có `requirements.txt`, `setup.py`, `setup.cfg`, `ruff.toml`...
- Lõi phục vụ `recsys.engine` **chỉ phụ thuộc numpy** (có test cưỡng chế).

## Bắt đầu nhanh

```bash
make setup          # uv sync --all-extras  -> .venv
make check          # lint + mypy --strict + test + benchmark (đúng thứ CI chạy)

make train          # huấn luyện trên dữ liệu tổng hợp, qua cổng đánh giá, đăng ký
uv run rec-admin versions --app demo
uv run rec-admin promote  --app demo --version <version> --reason "launch"
make serve          # http://localhost:8080  (/v1/rec /healthz /readyz /metrics /docs)

curl -s localhost:8080/v1/rec -d '{"app":"demo","device_id":"d1","recent":["i00001"],"limit":5}'
```

Môi trường chỉ cho một vai trò: `uv sync --extra serve` (API), `--extra train`, `--extra tracking` (MLflow).
Mặc định, trạng thái nằm ở `artifacts/` (xem `RECSYS_ARTIFACT_DIR`, `RECSYS_REGISTRY_DB`); mọi biến `RECSYS_*` được liệt kê trong [settings.py](src/recsys/settings.py).

## Vận hành vòng khép kín

```bash
rec-train      --app A [--revision N]                  # train -> gate -> publish -> register
rec-admin      promote|rollback|kill|unkill|block|freeze|unfreeze|autonomy|audit|status ...
rec-admin      experiment shadow|canary|end ...        # phát hành model theo giai đoạn
rec-admin      explain|simulate|diff|validate-config|schema   # công cụ offline
rec-admin      reconcile-mlflow --app A                # alias MLflow có khớp registry không
rec-loop       drift-check  --app A --served S --interactions I [--execute]
rec-loop       rollout-step --app A --served S --interactions I
rec-loop       health-check --app A --served S [--user-attr users.json]   # SRM của A/B + skew; thoát 1 khi báo động
rec-admin      ips --app A --served S --interactions I [--version V]      # đánh giá offline IPS/SNIPS
```

`rec-loop` đọc `rec.served` và `interactions` (JSON-lines, định dạng ở [§20.3](docs/THIET-KE-HE-THONG.md)), nên cả vòng chạy được khi chưa có kho dữ liệu. Cảnh báo → hành động: [docs/RUNBOOK.md](docs/RUNBOOK.md). Diễn tập: [docs/GAME-DAY.md](docs/GAME-DAY.md).

## Cấu trúc

```
pyproject.toml            # nguồn sự thật duy nhất của môi trường và công cụ
src/recsys/
  engine/                 # CHỈ numpy: request, bucketing, ranking, MMR, artifact, state, explain
  training/               # synthetic data, dataset, fit (SVD + i2i), backtest, gate, reference, ips, job
  registry/               # state machine model, promote/rollback, kill, rollout, autonomy, freeze, audit
  serving/                # FastAPI app, service, registry nền, sinks, metrics, health (Gini/coverage)
  monitoring/             # PSI/Gini, drift, skew, SRM (ab.py), decide() (hàm thuần), đọc log, metric batch
  rollout/                # luật shadow/canary (hàm thuần) + controller áp vào registry
  loop/                   # điều phối: thu số liệu từ log/registry; drift-check, rollout-step, health-check
  integrations/           # MLflow tracking (no-op nếu không cấu hình), mirror, wiring, reconcile
  cli/                    # rec-train, rec-admin, rec-loop, rec-serve
  settings.py logging_config.py
schemas/                  # JSON Schema của EngineConfig (sinh từ code, CI chặn khi lệch)
tests/                    # 201 test, gồm game day tự động và test kiến trúc
benchmarks/engine_bench.py  # chặn hồi quy: p95 < 5 ms với 8.308 item
deploy/                   # k8s/serve.yaml, k8s/train (Kustomize stg/prod), prometheus, grafana
docs/                     # ARCHITECTURE (bản đồ code), thiết kế (lộ trình), RUNBOOK, GAME-DAY
Dockerfile                # --target serve | train | monitor
.gitlab-ci.yml  .pre-commit-config.yaml  Makefile
```

## Quy ước chất lượng

| Việc | Công cụ | Cấu hình |
|---|---|---|
| PEP 8, import, đặt tên | `ruff` (E, W, F, I, N) | `[tool.ruff]`, độ dài dòng **88** (tương thích Black) |
| PEP 257 docstring | `ruff` (D, kiểu Google) | mọi API công khai đều có docstring |
| Bug phổ biến, bảo mật, độ phức tạp | `ruff` (B, S, PL, SIM, PERF, RUF, ARG, T20) | |
| Kiểu | `mypy --strict` (+ plugin pydantic) | cả `src`, `tests`, `benchmarks` |
| Kiến trúc | `tests/test_layering.py` | engine chỉ numpy+stdlib; serving không import training/registry; monitoring độc lập |
| Hiệu năng | `make bench` | p95 engine < 5 ms (đo thực tế ≈ 1–4 ms tùy `limit`) |

`docs/` bị loại khỏi ruff để công cụ không viết lại tài liệu.

## Các quyết định thiết kế được giữ nguyên

- **Fail-open**: mọi lỗi nghiệp vụ là HTTP 200 + `items: []` + `reason`; **mọi** request (kể cả control/holdout/lỗi) ghi một dòng `rec.served` để phân tích ITT.
- **Gán nhánh tất định** (`engine/bucketing.py`): holdout 5% vĩnh viễn, canary và shadow là lát cắt lưu lượng.
- **Không xếp hàng**: hết slot → `reason=overload` ngay; `REC_MAX_INFLIGHT=2` (engine giữ GIL, thêm thread làm *giảm* thông lượng). Timeout trả lời client một lần, `rec.served` ghi đúng một lần (`Ticket`).
- **Một nguồn sự thật**: registry quyết định version nào chạy và xuất bản `active.json`; MLflow chỉ là bản phản chiếu, lỗi của nó không bao giờ làm hỏng train hay promote.
- **Vòng không tự hại**: drift **và** metric tụt **và** mọi chốt an toàn mới retrain; rollout hỏng → rollback + **đóng băng** cho tới khi người `rec-admin unfreeze --reason ...`.

## Chưa làm (có chủ đích) — so với tài liệu thiết kế

Còn lại đều cần hạ tầng hoặc đội ngoài, hoặc tài liệu chưa đặc tả:

| Hạng mục | Lý do / việc cần làm |
|---|---|
| Nguồn dữ liệu thật (StarRocks/Parquet), SQL `gold_*`, DQ0–DQ11, Pandera, Great Expectations | Hiện chỉ có `SyntheticSource` và đọc JSON-lines. Cài `DataSource` ([synthetic.py](src/recsys/training/synthetic.py)) cho kho dữ liệu thật |
| Registry PostgreSQL | `SqliteRegistry` có cùng bộ phương thức; cần bản Postgres khi nhiều tiến trình ghi |
| Kafka sink cho `rec.served`, `rec-agg` + Redis | Giao diện `ServedSink` đã có; hiện có JSONL/Memory/Null |
| `/search`, `/sort_lessons` | Tài liệu không đặc tả hợp đồng |
| Metrics khi chạy nhiều worker/pod | Metrics theo từng tiến trình: dùng **1 worker/pod, scale bằng pod**, hoặc bật Prometheus multiprocess mode |
| OpenTelemetry, Evidently, GrowthBook | Mới có `trace_id` truyền qua header → `rec.served`; drift bằng PSI tự viết |
| Airflow DAG, Argo CD/Rollouts, Helm, External Secrets, `sql-staging` | Hạ tầng Nexus/Infra. `rec-loop` là phần DAG gọi vào; game day #8 cần Argo |
| Nghiệm thu tải thật | k6 đã có và đã chạy thử trên 1 process (xem [GAME-DAY](docs/GAME-DAY.md)): p95 13,7 ms nhưng 12% overload ở 10× đỉnh. Cần chạy trên nhiều pod với artifact thật |
| `torch`, `lightfm`, `recommenders`, `cornac` | Bỏ có chủ đích: mô hình là SVD + item-item chỉ dùng numpy/scipy |

Giới hạn đã biết của số liệu vòng: CTR là theo **yêu cầu** (không theo người dùng); lỗi và rỗng của canary tính trên chính nhánh đó. Các ngưỡng đánh dấu *provisional* là giá trị tạm theo tài liệu, cần chỉnh sau khoảng hai tuần dữ liệu thật.

## Giấy phép

Xem [LICENSE](LICENSE).
