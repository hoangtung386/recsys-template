# Kiến trúc hiện tại của `recsys`

Bản đồ ngắn của code **đang chạy**. Lý do chọn công cụ và lộ trình cho phần chưa làm nằm ở [THIET-KE-HE-THONG.md](THIET-KE-HE-THONG.md); khi hai tài liệu mâu thuẫn, tài liệu này và §20–§21 của tài liệu kia thắng.

## Vòng khép kín

```
request ─► serving ─► rec.served (JSONL) ──┐
                                           ▼
 train ─► gate ─► registry ─► rollout ─► active ─► monitoring ─► loop ─► (retrain)
```

| Bước | Module | Lệnh |
|---|---|---|
| Huấn luyện, backtest, cổng, đăng ký | `training/` (`job.py`) | `rec-train` |
| Trạng thái model, promote, rollback, kill, freeze, audit | `registry/sqlite.py` | `rec-admin` |
| Luật shadow/canary (hàm thuần), áp vào registry | `rollout/policy.py`, `rollout/controller.py` | `rec-admin experiment`, `rec-loop rollout-step` |
| Phục vụ `/v1/rec`, `/healthz`, `/readyz`, `/metrics` | `serving/` | `rec-serve` |
| Đo drift, skew, SRM; quyết định retrain (hàm thuần) | `monitoring/` | — |
| Thu số liệu từ log/registry, điều phối | `loop/` | `rec-loop drift-check \| rollout-step \| health-check` |
| Công cụ offline | `cli/_tools.py` | `rec-admin explain \| simulate \| diff \| ips \| validate-config \| schema` |
| MLflow (tuỳ chọn, no-op nếu không cấu hình) | `integrations/` | — |

## Các lớp và quy tắc phụ thuộc

Được **cưỡng chế bằng test** ([tests/test_layering.py](../tests/test_layering.py)); vi phạm làm `make check` đỏ.

| Lớp | Là gì | Được import |
|---|---|---|
| `engine/` | Lõi phục vụ: request, bucketing, ranking, MMR, artifact | **chỉ numpy + stdlib** |
| `monitoring/`, `rollout/` | Hàm thuần: số đo và luật quyết định | `engine`, `registry` kiểu dữ liệu; **không** `serving`, `training`, `loop`, `cli` |
| `serving/` | FastAPI, nạp artifact, ghi `rec.served` | `engine`; **không** `training`, `registry` |
| `training/`, `registry/` | Huấn luyện và vòng đời model | |
| `loop/` | Nối log + registry vào hàm thuần | |
| `cli/` | Điểm vào, ghép mọi thứ | tất cả |

## Nguồn sự thật

| Thứ | Ở đâu | Phản chiếu sang |
|---|---|---|
| Version nào đang chạy | `registry` (SQLite; PG khi production) | `active.json` cạnh artifact (API đọc), alias MLflow `production` |
| Hợp đồng request, bản ghi `rec.served` | `engine/request.py` | OpenAPI (sinh ra) |
| Gán nhánh A/B | `engine/bucketing.py` | — |
| Cấu hình engine | `EngineConfig` + `config_hash` | `schemas/engine-config.schema.json` (sinh từ code, CI chặn khi lệch) |
| Biến môi trường | `settings.py` (`RECSYS_*`) | — |
| Phân phối tham chiếu của version | `reference.json` trong artifact | artifact MLflow |

## Artifact của một version

`artifacts/<app>/<version>/` gồm mảng numpy nạp bằng mmap, `manifest.json` (lineage, gate, backtest, `config_hash`), `reference.json` (phân phối lúc train, dùng cho drift) và `_SUCCESS` ghi sau cùng. Version có dạng `YYYYMMDD-<config hash>[-rN]`, nên train lại cùng dữ liệu và cấu hình thì thoát với `exists` (dùng `--revision`).

## Chưa làm

Xem bảng "Chưa làm" trong [README](../README.md). Tóm tắt: nguồn dữ liệu thật (StarRocks), registry PostgreSQL, Kafka sink, Evidently/GrowthBook/OTel, Airflow DAG và GitOps (hạ tầng ngoài repo).
