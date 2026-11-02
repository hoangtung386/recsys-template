# Đóng góp

## Môi trường

```bash
make setup      # uv sync --all-extras -> .venv (dùng đúng uv.lock)
make check      # lint + mypy --strict + test + benchmark: đúng thứ CI chạy
```

Không có `requirements.txt`; mọi dependency, công cụ và cấu hình nằm trong [pyproject.toml](pyproject.toml). Thêm dependency bằng `uv add [--optional <vai trò>] <gói>` rồi commit cả `uv.lock`.

Cài hook một lần: `uvx pre-commit install` (ruff, mypy, test nhanh, gitleaks).

## Quy trình thay đổi

1. Tạo nhánh từ nhánh chính: `feat/…`, `fix/…`, `docs/…`.
2. Commit nhỏ, thông điệp nói *vì sao*. Một MR làm một việc; **di chuyển/đổi tên file làm MR riêng**, không trộn với sửa logic.
3. Mở MR; CI phải xanh (lint, schemas-check, gitleaks, test 3.11–3.13, engine numpy 1.26/2.x, benchmark).
4. Cần ít nhất một người duyệt theo [CODEOWNERS](CODEOWNERS). Squash khi merge.

## Quy tắc code

- **Lớp phụ thuộc** ở [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) được test cưỡng chế. Lõi `engine/` chỉ numpy: đừng import gì khác vào đó.
- Mọi API công khai có docstring (kiểu Google) và type hint đầy đủ (`mypy --strict` cả `tests/`).
- Không `print()` trong thư viện (dùng `logging`; chỉ `cli/` được in).
- Hàm quyết định (`monitoring/policy.py`, `rollout/policy.py`) phải **thuần**: không I/O, không đồng hồ. Thu số liệu và I/O nằm ở `loop/`.
- Tích hợp ngoài (MLflow, Pushgateway) phải **tắt được và không bao giờ làm hỏng train/promote**.
- **Không để mã chỉ có test gọi.** Tính năng mới phải có đường vào (lệnh CLI hoặc luồng phục vụ); nếu chưa nối, đừng merge. Tìm mã chết: `uv run --with vulture vulture src --min-confidence 80`.
- Đổi `EngineConfig` thì chạy `make schemas` và commit `schemas/`.
- Đổi hợp đồng `rec.served` thì cập nhật §20.2 của tài liệu thiết kế và test `tests/loop`.

## Kiểm thử

```bash
make test-fast                 # bỏ các test train model
uv run pytest tests/engine -q  # một thư mục
```

Hiệu năng: `make bench` chặn p95 engine < 5 ms. Tải HTTP: `tests/load/rec.js` (k6), xem [docs/GAME-DAY.md](docs/GAME-DAY.md).

## Tài liệu

- Sự cố vận hành: [docs/RUNBOOK.md](docs/RUNBOOK.md). Mỗi cảnh báo `page` mới phải có một dòng ở đó.
- Quyết định kiến trúc mới: thêm vào phụ lục cuối [docs/THIET-KE-HE-THONG.md](docs/THIET-KE-HE-THONG.md) (mục mới thắng mục cũ).
