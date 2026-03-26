"""
run.py — 전체 시스템 런처

Mock Site A(8001), Mock Site B(8002), Engine(8000)을 동시에 실행.
"""

import sys
import os
import multiprocessing

# 프로젝트 루트를 sys.path에 추가
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)

import uvicorn


def run_site_b():
    print("🟢 Starting Mock Site B on port 8002...")
    uvicorn.run("mocks.site_b:app", host="0.0.0.0", port=8002, log_level="info")


def run_engine():
    print("🧠 Starting Engine on port 8000...")
    uvicorn.run("core.engine:app", host="0.0.0.0", port=8000, log_level="info")


def run_site_a():
    print("🏨 Starting Mock Site A on port 8001...")
    uvicorn.run("mocks.site_a:app", host="0.0.0.0", port=8001, log_level="info")


if __name__ == "__main__":
    processes = [
        multiprocessing.Process(target=run_site_b),
        multiprocessing.Process(target=run_engine),
        multiprocessing.Process(target=run_site_a),
    ]

    for p in processes:
        p.start()

    try:
        for p in processes:
            p.join()
    except KeyboardInterrupt:
        print("\n⏹ Shutting down all servers...")
        for p in processes:
            p.terminate()
