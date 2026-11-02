# Runbook: cảnh báo → hành động

Mọi lệnh dưới đây là `uv run rec-admin ...` (hoặc `rec-admin` trong image). Cảnh báo `page` phải có người xử lý ngay; `ticket` xử lý trong ngày làm việc.

| Cảnh báo | Nghĩa là | Làm gì |
|---|---|---|
| `RecLatencyP95High` | p95 `/v1/rec` > 50 ms | Xem `rec_inflight`, `rec_degraded_total`. Thêm pod. **Không** tăng `REC_MAX_INFLIGHT` (engine giữ GIL, tăng thread làm giảm thông lượng). |
| `RecEmptyRateHigh` | > 20% phản hồi rỗng | `rec_empty_total` theo `reason`: `no_artifact` → kiểm `/readyz` và artifact; `kill_switch` → có ai bật? `rec-admin status`. |
| `RecOverloadHigh` | `overload`/`timeout` > 1% | Thiếu năng lực. Thêm pod (HPA đã chạm trần?). Client vẫn nhận 200 + danh sách rỗng nên không gãy, nhưng người dùng thấy thứ tự mặc định. |
| `RecDegradedRateHigh` | engine chạm deadline hoặc pool rỗng | Kiểm kích thước catalog và tải CPU; chạy `rec-admin explain` với một request thật. |
| `RecRegistryLoadFailing` / `RecRegistryStale` | Pod không nạp được artifact mới | Xem log `registry.refresh_failed`. Artifact hỏng thì pod vẫn phục vụ bản cũ. Bản mới lỗi thì `rec-admin rollback`. |
| `RecServedDropped` | `rec.served` bị bỏ vì hàng đợi đầy | Mất dữ liệu phân tích A/B. Kiểm đích ghi (đĩa, Kafka); tăng `RECSYS_SERVED_QUEUE_SIZE` chỉ là tạm thời. |
| `RecModelStale` | > 36 giờ chưa train thành công | Xem job `rec-train` / scheduler. DQ chặn? Gate từ chối? `rec-admin versions --app A`. |
| `RecMirrorDrift` | Alias MLflow ≠ registry | `rec-admin reconcile-mlflow --app A`. Đừng sửa alias bằng tay trong giao diện MLflow. |
| `RecLoopFrozen` (> 24 giờ) | Rollout hỏng đã đóng băng app | Đọc `rec-admin audit --app A`, tìm nguyên nhân. Khi chắc chắn: `rec-admin unfreeze --app A --reason "..."`. |
| `RecExposureConcentrating` | Gini phơi bày tăng | Dấu hiệu suy giảm âm thầm. So `rec_src_share` theo thời gian; `rec-admin diff` giữa hai version; cân nhắc `rollback`. |
| `DeadMansSwitch` im lặng | Chính hệ thống cảnh báo hỏng | Kiểm Prometheus/Alertmanager/kênh nhận. |

## Sự cố thường gặp

- **Dừng phục vụ nhanh:** `rec-admin kill --app A --reason "..."`. Hiệu lực sau lần refresh kế tiếp của pod (≤ `RECSYS_REFRESH_INTERVAL_S` + jitter). Bật lại: `unkill`.
- **Chặn một item:** `rec-admin block --app A <id> ...` (`unblock` để gỡ).
- **Model mới tệ sau khi promote:** `rec-admin rollback --app A --reason "..."` đưa về bản liền trước.
- **Retrain tay trong cùng ngày dữ liệu:** `rec-train --app A --revision N` (nếu không, version trùng và job thoát với `exists`).
- **Nâng mức tự động hoá:** `rec-admin autonomy --app A --level L2`; `L3` cần ≥ 14 ngày ở `L2` (hoặc `--force`, được ghi audit).
