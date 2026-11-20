#!/bin/zsh
set -e
cd "$(dirname "$0")"
if [[ ! -x .venv/bin/streamlit ]]; then
  echo '请先在项目目录执行 uv sync --frozen --extra docling'
  read 'reply?按回车退出'
  exit 1
fi
export HF_HOME="$PWD/work/huggingface"
exec .venv/bin/streamlit run app.py --server.address 127.0.0.1 --server.port 8501 --browser.gatherUsageStats false
