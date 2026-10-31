"""
    根目录启动入口（薄封装）：app.py

    说明：项目主入口为 main.py；本文件仅为兼容「python app.py」的直觉用法。
        对齐 FoodSafetySystem 的项目结构（其根目录同样有 app.py）。
"""
import sys

from main import main

if __name__ == "__main__":
    sys.exit(main())
