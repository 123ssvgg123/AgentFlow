# -*- coding: utf-8 -*-
"""
    过敏原衍生物对抗测试：scripts/verify_allergen_derivatives.py

    为什么需要这个脚本：
        顾客声明的往往是基础过敏原（「我对花生过敏」），而配方里写的是加工形态
        （「花生油」「花生酱」）。早期实现只对配料做字符交集，导致这类表述成片漏判——
        属安全侧漏，比误报严重得多。本脚本把这类用例固化成回归集，防止再次退化。

    覆盖两类用例：
        正向（必须检出）：配料写衍生物、顾客声明基础过敏原
        反向（不得误报）：配料全是中性食材、顾客声明某过敏原

    用法：
        python scripts/verify_allergen_derivatives.py
        python scripts/verify_allergen_derivatives.py -v      # 打印每条用例结果

    退出码：0 = 全部通过；1 = 存在漏判或误报
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from base.config import config          # noqa: E402
from tools.rule_tools import allergen_detection  # noqa: E402

# 中性食材：用于构造「配料里确实没有该过敏原」的反向用例
NEUTRAL = ["青菜", "米饭", "土豆", "冬瓜", "藕片", "米粉"]


def build_cases():
    """按衍生物表自动铺用例：每条衍生物一个正向用例，每个基础过敏原一个反向用例"""
    pos, neg = [], []
    for base, derivs in config.ALLERGEN_DERIVATIVES.items():
        for i, dv in enumerate(derivs):
            ingredients = [dv] + NEUTRAL[:2] + [NEUTRAL[2 + (i % 3)]]
            pos.append((ingredients, [base], f"{dv} 应检出 {base}"))
        neg.append((list(NEUTRAL), [base], f"中性配料不应误报 {base}"))
    return pos, neg


def run(cases, expect_flag, verbose=False):
    passed, failed = 0, []
    for ingredients, declared, tag in cases:
        result = allergen_detection(ingredients, declared, tag)
        hit = bool(result.get("has_allergen"))
        if hit == expect_flag:
            passed += 1
            if verbose:
                print(f"  PASS {tag}")
        else:
            failed.append((tag, result.get("detail", "")))
            print(f"  FAIL {tag} -> {result.get('detail', '')}")
    return passed, failed


def main():
    parser = argparse.ArgumentParser(description="过敏原衍生物对抗测试")
    parser.add_argument("-v", "--verbose", action="store_true", help="打印每条用例")
    args = parser.parse_args()

    pos, neg = build_cases()
    total_derivs = sum(len(v) for v in config.ALLERGEN_DERIVATIVES.values())
    print("=" * 62)
    print("过敏原衍生物对抗测试")
    print(f"衍生物表：{len(config.ALLERGEN_DERIVATIVES)} 个基础过敏原 / {total_derivs} 个衍生物词条")
    print("=" * 62)

    print(f"\n[正向] 配料写衍生物、顾客声明基础过敏原 —— 共 {len(pos)} 条，期望全部检出")
    ok_pos, bad_pos = run(pos, True, args.verbose)
    print(f"  召回 {ok_pos}/{len(pos)}（{ok_pos / len(pos) * 100:.1f}%）")

    print(f"\n[反向] 中性配料、顾客声明某过敏原 —— 共 {len(neg)} 条，期望不误报")
    ok_neg, bad_neg = run(neg, False, args.verbose)
    print(f"  正确 {ok_neg}/{len(neg)}（误报 {len(bad_neg)} 条）")

    print("\n" + "=" * 62)
    if bad_pos or bad_neg:
        print(f"未通过：漏判 {len(bad_pos)} 条 / 误报 {len(bad_neg)} 条")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
