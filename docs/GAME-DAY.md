# Game day

Bài diễn tập đóng vòng. Các kịch bản dưới đây được **tự động hoá** trong [tests/test_game_day.py](../tests/test_game_day.py): lưu lượng đi qua app FastAPI thật, ghi `rec.served` ra JSON-lines, `rec-loop` đọc lại và áp phán quyết vào registry. Chạy bằng `make test` (nằm trong CI).

| # | Kịch bản (tài liệu §16.4) | Trạng thái |
|---|---|---|
| 1 | Đổi phân phối `country_group` → drift được phát hiện | Tự động |
| 2 | Drift + chốt đủ → retrain ngoài lịch với `--revision` | Tự động cho đường train `-r1`; nhánh `retrain` của `decide` có test riêng |
| 3 | Version mới đi hết shadow → canary; alias `challenger` | Tự động |
| 4 | Người duyệt (L2): registry, `active.json`, alias `production` cùng đổi | Tự động (`reconcile` báo khớp) |
| 5 | Model tệ bị rollback ở canary; đóng băng; ≤ 60 giây | Tự động (đo `rec_loop_rollback_seconds`) |
| 6 | Đang đóng băng, drift lần nữa → không retrain | Tự động |
| 7 | Quá tải → `overload`/`timeout`, không 5xx | Tự động (`tests/serving/test_api.py`); tải thật bằng k6 |
| 8 | Bản code chậm → Argo Rollouts abort | **Cần cụm K8s + Argo**, chưa tự động |
| 9 | MLflow sập → train/promote vẫn chạy | Tự động |

Cần chạy lại ở staging với dữ liệu thật và ghi số đo vào tài liệu thiết kế trước khi bật mức tự động cao hơn L1.

## Tải HTTP (k6)

```bash
uv run rec-serve --app demo --port 8080 &
k6 run -e BASE_URL=http://localhost:8080 -e APP=demo -e RATE=90 tests/load/rec.js   # SHORT=1: 5 giây/giai đoạn
```

Lần chạy thử đầu tiên (SHORT=1, **một process**, máy dev, ramp tới 10× = 900 req/s): p95 = 13,7 ms, 0 lỗi 5xx, nhưng 12,3% `overload/degraded` (ngưỡng 1%). Một process không gánh được 10× đỉnh; đúng như thiết kế, phải scale bằng pod. Đây chưa phải số đo nghiệm thu (cần artifact thật, nhiều pod, và mạng thật).
