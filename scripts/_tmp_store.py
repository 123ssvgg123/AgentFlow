"""
    验证脚本临时目录助手：scripts/_tmp_store.py

    职责：给「会真实写长期记忆」的验证/自检脚本提供一个**独立临时目录**，
        保证脚本「跑完不留痕」——不再往 data/memory_store/ 里丢验证产物。

    工作原理（不改任何业务逻辑）：
        memory/store.py 的 FileStore 支持用环境变量 FOODAGENT_MEMORY_STORE_DIR
        覆盖记忆目录；本模块在脚本启动最早阶段把该变量指向一个临时目录，
        并注册 atexit 清理，脚本退出时整目录删除。

    优先级（便于 CI / 排障时人工指定）：
        1) 调用方显式传入 dir_path
        2) 环境变量 FOODAGENT_MEMORY_STORE_DIR（已存在则沿用，不接管、不删除）
        3) 项目 data/_verify_tmp/<脚本名>_<pid>（沙箱/受限环境下也可写）
        4) 系统临时目录（tempfile.mkdtemp，兜底）

    用法（必须在 import memory.store / import memory 之前调用）：
        from _tmp_store import temp_memory_dir
        temp_memory_dir("verify_fsm")
"""
# 导入临时目录与清理工具
import atexit
import os
import shutil
import sys
import tempfile


def temp_memory_dir(tag: str = "verify", dir_path: str = "") -> str:
    """
    准备并返回供本次验证使用的临时记忆目录。

    :param tag:  目录名后缀标识（一般传脚本名），便于排查是哪个脚本创建的
    :param dir_path: 显式指定目录（优先级最高，传空则按上面的优先级自动选择）
    :return: 临时记忆目录的绝对路径
    """
    # 已有环境变量（人工指定或上层脚本已设置）→ 沿用并**不**接管清理
    existing = os.getenv("FOODAGENT_MEMORY_STORE_DIR")
    if not dir_path and existing:
        os.makedirs(existing, exist_ok=True)
        print(f"[临时记忆目录] 沿用 FOODAGENT_MEMORY_STORE_DIR={existing}")
        return existing

    if dir_path:
        target = dir_path
    else:
        # 项目根目录 = scripts/ 的上一级
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        target = os.path.join(root, "data", "_verify_tmp", f"{tag}_{os.getpid()}")

    try:
        os.makedirs(target, exist_ok=True)
    except OSError:
        # 项目内目录不可写（只读挂载等）→ 退回系统临时目录
        target = tempfile.mkdtemp(prefix=f"foodagent_{tag}_")

    # 设置环境变量（FileStore 构造时读取）
    os.environ["FOODAGENT_MEMORY_STORE_DIR"] = target

    def _cleanup() -> None:
        """进程退出时彻底删除临时目录（保证「跑完不留痕」）"""
        if os.path.isdir(target):
            shutil.rmtree(target, ignore_errors=True)

    atexit.register(_cleanup)
    print(f"[临时记忆目录] 本次验证产物全部落在 {target}"
          f"（脚本退出时自动清理）")
    return target


if __name__ == '__main__':
    # 自检：仅验证目录创建与清理逻辑（不依赖项目其它模块）
    path = temp_memory_dir("tmp_store_selftest")
    print("目录存在：", os.path.isdir(path))
    with open(os.path.join(path, "_probe.json"), "w", encoding="utf-8") as fh:
        fh.write("{}")
    print("写入探针文件：", os.path.isfile(os.path.join(path, "_probe.json")))
    print("sys.path[0] =", sys.path[0] if sys.path else "")
    print("自检通过（退出时将自动清理该目录）。")
