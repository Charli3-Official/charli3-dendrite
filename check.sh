#!/bin/bash
set -e

# Đường dẫn tới 2 file cần kiểm tra
DANOGO_FILE="src/charli3_dendrite/dexs/amm/danogo.py"
TEST_FILE="tests/test_danogo.py"

echo "🧪 Checking $DANOGO_FILE and $TEST_FILE..."

# 1. Định dạng code
poetry run black $DANOGO_FILE $TEST_FILE
poetry run isort $DANOGO_FILE $TEST_FILE

# 2. Kiểm tra lỗi linting
poetry run ruff check $DANOGO_FILE $TEST_FILE --fix

# 3. Kiểm tra kiểu dữ liệu (Type hint)
poetry run mypy $DANOGO_FILE $TEST_FILE

# 4. Chạy duy nhất file test này
poetry run pytest $TEST_FILE -s

echo "✅ All clear for Danogo!"