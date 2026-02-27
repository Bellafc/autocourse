"""
排课系统测试用例集
=====================
覆盖场景：
  1. 无冲突 — 直接排课
  2. 仅老师有冲突
  3. 仅学生有冲突
  4. 师生双冲突
  5. 可换老师（同科目有替代老师）
  6. 满课日（6 节全排满）
  7. 跨学科学生（选课最多的学生）
  8. 边界时间段（早/晚）

使用方式：
  python test_cases.py              # 列出所有测试用例
  python test_cases.py --run N      # 自动运行第 N 个用例
  python test_cases.py --run all    # 依次运行所有用例（需要 OpenAI key）
"""

import sys
import os

# ──────────────────────────────────────────────
# 测试用例定义
# ──────────────────────────────────────────────

TEST_CASES = [
    # ============ 1. 无冲突场景 ============
    {
        "id": 1,
        "name": "无冲突 — 直接排课",
        "description": "学生和老师在目标时间段都没课，应直接排课成功",
        "student_name": "华小曜",
        "teacher_name": "林紫敏",
        "date": "2025-12-01",      # 12月课很少，大概率空闲
        "slot_index": 0,           # 09:00-10:30
        "requirement": "加数学P3课",
        "expected_behavior": "直接安排，无需启动 Agent",
    },
    {
        "id": 2,
        "name": "无冲突 — 下午时段",
        "description": "选一个课少的日期和下午晚时段",
        "student_name": "易鸣之",
        "teacher_name": "姜若闲",
        "date": "2025-12-03",
        "slot_index": 4,           # 16:30-18:00
        "requirement": "加会计课",
        "expected_behavior": "直接安排，无需启动 Agent",
    },

    # ============ 2. 仅老师有冲突 ============
    {
        "id": 3,
        "name": "老师满课 — 金鹏周四（物理）",
        "description": "金鹏在 2025-03-06 全天 6 节课都排满，学生当天可能空闲",
        "student_name": "朱芊芊",
        "teacher_name": "金鹏",
        "date": "2025-03-06",
        "slot_index": 0,           # 09:00-10:30 金鹏有 AS Physics
        "requirement": "排物理课",
        "expected_behavior": "老师有课学生无课 → Agent 建议换老师(黄婷婷/唐智凡等)或换时间",
    },
    {
        "id": 4,
        "name": "老师有课 — 林紫敏数学（可换郎筱煜）",
        "description": "林紫敏在 2025-09-11 上午排满，同科目有郎筱煜可替代",
        "student_name": "朱梓轩",
        "teacher_name": "林紫敏",
        "date": "2025-09-11",
        "slot_index": 0,           # 09:00-10:30 林紫敏有P2备考班
        "requirement": "排数学P2",
        "expected_behavior": "老师有课 → Agent 建议换老师(郎筱煜)或换时间",
    },

    # ============ 3. 仅学生有冲突 ============
    {
        "id": 5,
        "name": "学生有课 — 殷舒窈（选课最多学生）",
        "description": "殷舒窈有28个老师的课，在忙碌日她大概率有课",
        "student_name": "殷舒窈",
        "teacher_name": "胡雅雯",
        "date": "2025-05-21",
        "slot_index": 2,           # 13:30-15:00
        "requirement": "加心理课",
        "expected_behavior": "学生有课老师空闲 → Agent 建议挪学生的课或换时间",
    },
    {
        "id": 6,
        "name": "学生有课 — 刘一宏化学时间",
        "description": "刘一宏在林楚煜的化学课时间有别的安排",
        "student_name": "刘一宏",
        "teacher_name": "林楚煜",
        "date": "2025-04-14",
        "slot_index": 2,           # 13:30-15:00
        "requirement": "排化学补课",
        "expected_behavior": "学生有课 → Agent 尝试挪学生已有课或换时间段",
    },

    # ============ 4. 师生双冲突 ============
    {
        "id": 7,
        "name": "双冲突 — 杨嘉鑫 & 乔子淇（周二全忙）",
        "description": "杨嘉鑫周二满课，乔子淇也有 ESL 词汇课（杨嘉鑫教）",
        "student_name": "乔子淇",
        "teacher_name": "杨嘉鑫",
        "date": "2025-08-19",
        "slot_index": 2,           # 13:30-15:00 杨嘉鑫有ESL词汇课（含乔子淇）
        "requirement": "加 ESL 口语课",
        "expected_behavior": "双冲突 → Agent 需同时解决师生冲突，探索换老师+换时间组合",
    },
    {
        "id": 8,
        "name": "双冲突 — 黄婷婷 & 李文睿（物理繁忙日）",
        "description": "黄婷婷全天排满物理，李文睿也在她的课上",
        "student_name": "李文睿",
        "teacher_name": "黄婷婷",
        "date": "2025-05-26",
        "slot_index": 1,           # 10:30-12:00 黄婷婷有AS物理
        "requirement": "排物理备考课",
        "expected_behavior": "双冲突(学生本身在老师的另一节课里) → 复杂调度",
    },

    # ============ 5. 可换老师场景 ============
    {
        "id": 9,
        "name": "换老师 — IG数学（5位老师可选）",
        "description": "IG数学(topic 331)有许斯睿/武子煜/俞斐帆/杜彦儒/章韵可选",
        "student_name": "迟珵钰",
        "teacher_name": "许斯睿",
        "date": "2025-03-27",
        "slot_index": 0,           # 09:00-10:30
        "requirement": "排IG数学",
        "expected_behavior": "如许斯睿有课，可从武子煜/俞斐帆/杜彦儒/章韵中选替代老师",
    },
    {
        "id": 10,
        "name": "换老师 — CIE IG Chemistry（4位老师可选）",
        "description": "IG化学(topic 353)有林楚煜/姚鑫/叶轩/沈雨琪可选",
        "student_name": "何嘉涛",
        "teacher_name": "林楚煜",
        "date": "2025-05-27",
        "slot_index": 1,           # 10:30-12:00 林楚煜有化学预学班
        "requirement": "排IG化学补课",
        "expected_behavior": "林楚煜忙 → 可选姚鑫/叶轩/沈雨琪替代",
    },

    # ============ 6. CIE Physics 多老师场景 ============
    {
        "id": 11,
        "name": "换老师 — CIE Physics（6位老师可选）",
        "description": "CIE Physics(topic 356)有金鹏/黄婷婷/唐智凡/曹久青/鲍圣蓉/叶轩",
        "student_name": "徐正伦",
        "teacher_name": "金鹏",
        "date": "2025-06-03",
        "slot_index": 0,           # 09:00-10:30 金鹏有AS Physics
        "requirement": "排IG物理",
        "expected_behavior": "金鹏全天满 → Agent 应推荐黄婷婷/唐智凡等替代",
    },

    # ============ 7. 经济学换老师 ============
    {
        "id": 12,
        "name": "换老师 — IG经济（姜若闲 ↔ 许嘉荟）",
        "description": "IG经济(topic 367)有姜若闲和许嘉荟两位老师",
        "student_name": "李沐霖",
        "teacher_name": "姜若闲",
        "date": "2025-03-27",
        "slot_index": 1,           # 10:30-12:00 姜若闲有IG经济
        "requirement": "排IG经济",
        "expected_behavior": "姜若闲有课 → 可以换许嘉荟",
    },

    # ============ 8. 满课日复杂调度 ============
    {
        "id": 13,
        "name": "满课日调度 — 杨嘉鑫6节全满",
        "description": "杨嘉鑫 2025-09-02 全天6节课满，需要挤进新课",
        "student_name": "张禹泽",
        "teacher_name": "杨嘉鑫",
        "date": "2025-09-02",
        "slot_index": 3,           # 15:00-16:30
        "requirement": "加雅思口语",
        "expected_behavior": "老师全天满 → Agent 必须换时间(其他天)或换老师(宋明璇/陈颖)",
    },
    {
        "id": 14,
        "name": "满课日调度 — 林楚煜化学满课",
        "description": "林楚煜 2025-04-14 全天6节化学，试图再加课",
        "student_name": "朱秋颐",
        "teacher_name": "林楚煜",
        "date": "2025-04-14",
        "slot_index": 3,           # 15:00-16:30
        "requirement": "排IG化学预习",
        "expected_behavior": "全天满 → 换其他天或换老师(姚鑫/叶轩/沈雨琪)",
    },

    # ============ 9. 边界时间段 ============
    {
        "id": 15,
        "name": "最晚时段 — 晚上 18:00-19:30",
        "description": "测试最晚一节课的排课",
        "student_name": "贾舒涵",
        "teacher_name": "段伟",
        "date": "2025-03-20",
        "slot_index": 5,           # 18:00-19:30
        "requirement": "排ESL英语",
        "expected_behavior": "测试最晚时段是否正常处理",
    },
    {
        "id": 16,
        "name": "最早时段 — 早上 09:00-10:30",
        "description": "测试最早一节课的排课",
        "student_name": "崔子言",
        "teacher_name": "武子煜",
        "date": "2025-04-10",
        "slot_index": 0,           # 09:00-10:30
        "requirement": "排IG数学",
        "expected_behavior": "测试最早时段是否正常处理",
    },

    # ============ 10. 挪课连锁场景 ============
    {
        "id": 17,
        "name": "挪课 — 需要移动他人课程",
        "description": "目标时段老师有小班课，可尝试把小班课移到其他空闲时段",
        "student_name": "张萌",
        "teacher_name": "姚鑫",
        "date": "2025-05-26",
        "slot_index": 0,           # 09:00-10:30
        "requirement": "排IG生物",
        "expected_behavior": "Agent 尝试将姚鑫当前课挪到其他时段",
    },
    {
        "id": 18,
        "name": "挪课 — 挪学生自己的课",
        "description": "学生在目标时段有一节不太重要的课，可以挪走",
        "student_name": "王之翼",
        "teacher_name": "姜若闲",
        "date": "2025-10-08",
        "slot_index": 2,           # 13:30-15:00 姜若闲有IG经济
        "requirement": "排IG经济，必须是姜若闲",
        "expected_behavior": "学生有课 → Agent 尝试挪学生的其他课为新课腾位",
    },

    # ============ 11. 商科/会计场景 ============
    {
        "id": 19,
        "name": "商科排课 — 丁佳卉 IG Business",
        "description": "丁佳卉教商科(topic 364)，蔡人杰也教",
        "student_name": "秦乐",
        "teacher_name": "丁佳卉",
        "date": "2025-09-08",
        "slot_index": 1,           # 10:30-12:00
        "requirement": "排IG商科",
        "expected_behavior": "如丁佳卉有课可换蔡人杰",
    },
    {
        "id": 20,
        "name": "会计排课 — 多老师选择",
        "description": "IAL会计(topic 423)有姜若闲/朱昂明/刘奕冰/许嘉荟/蔡人杰/唐翰林6位可选",
        "student_name": "瞿嘉言",
        "teacher_name": "姜若闲",
        "date": "2025-09-08",
        "slot_index": 0,           # 09:00-10:30 姜若闲有会计课
        "requirement": "排IAL会计补课",
        "expected_behavior": "姜若闲有课 → 从5位替代老师中选空闲的",
    },

    # ============ 12. 特殊学生场景 ============
    {
        "id": 21,
        "name": "选课最多学生 — 殷舒窈排新课",
        "description": "殷舒窈有28位不同老师的课，调度非常复杂",
        "student_name": "殷舒窈",
        "teacher_name": "许斯睿",
        "date": "2025-04-10",
        "slot_index": 3,           # 15:00-16:30
        "requirement": "排IG高数",
        "expected_behavior": "复杂调度 — 学生课表极为密集，Agent需要仔细找空位",
    },
    {
        "id": 22,
        "name": "多科目学生 — 刘一宏加课",
        "description": "刘一宏有27位老师的课(化学/历史/ESL/ICT等)，再加一门",
        "student_name": "刘一宏",
        "teacher_name": "金鹏",
        "date": "2025-03-06",
        "slot_index": 2,           # 13:30-15:00
        "requirement": "排IG物理",
        "expected_behavior": "师生可能双忙 → 多策略探索",
    },

    # ============ 13. 心理学 / 小众科目 ============
    {
        "id": 23,
        "name": "心理学排课 — 胡雅雯",
        "description": "心理学老师相对少，换老师选择有限",
        "student_name": "易鸣之",
        "teacher_name": "胡雅雯",
        "date": "2025-05-14",
        "slot_index": 2,           # 13:30-15:00 胡雅雯有AS心理
        "requirement": "排AS心理",
        "expected_behavior": "心理学老师少，换老师困难 → Agent 主要建议换时间",
    },

    # ============ 14. 同一老师同一天换时段 ============
    {
        "id": 24,
        "name": "同天换时段 — 姜若闲上午→下午",
        "description": "姜若闲上午满但下午可能有空",
        "student_name": "沈奕帆",
        "teacher_name": "姜若闲",
        "date": "2025-03-27",
        "slot_index": 2,           # 13:30-15:00 姜若闲有IAL会计
        "requirement": "排IG会计",
        "expected_behavior": "当天换时段 → Agent 找同一天其他空闲时段",
    },

    # ============ 15. 重复排课检测 ============
    {
        "id": 25,
        "name": "学生已在该课中 — 检测重复",
        "description": "学生已经在老师的某节课里，不应重复排",
        "student_name": "朱梓轩",
        "teacher_name": "林紫敏",
        "date": "2025-09-11",
        "slot_index": 1,           # 10:30-12:00 朱梓轩已在林紫敏M2备考班
        "requirement": "排数学M2",
        "expected_behavior": "朱梓轩已在林紫敏M2班中，应检测到重复并提示",
    },
]


# ──────────────────────────────────────────────
# 显示 & 运行逻辑
# ──────────────────────────────────────────────

SLOT_LABELS = [
    "0: 09:00-10:30",
    "1: 10:30-12:00",
    "2: 13:30-15:00",
    "3: 15:00-16:30",
    "4: 16:30-18:00",
    "5: 18:00-19:30",
]


def print_all_cases():
    """打印所有测试用例"""
    print("=" * 70)
    print("  排课系统测试用例集  —  共 {} 条".format(len(TEST_CASES)))
    print("=" * 70)

    for tc in TEST_CASES:
        print(f"\n{'─' * 60}")
        print(f"  #{tc['id']:02d}  {tc['name']}")
        print(f"  描述: {tc['description']}")
        print(f"  学生: {tc['student_name']}")
        print(f"  老师: {tc['teacher_name']}")
        print(f"  日期: {tc['date']}  时段: {SLOT_LABELS[tc['slot_index']]}")
        print(f"  需求: {tc['requirement']}")
        print(f"  预期: {tc['expected_behavior']}")

    print(f"\n{'=' * 70}")
    print("运行方式:")
    print("  python test_cases.py --run 1        # 运行第1个用例")
    print("  python test_cases.py --run all      # 运行全部用例")
    print("  python test_cases.py --dry 3        # 模拟第3个用例(仅查课表,不调Agent)")
    print(f"{'=' * 70}")


def run_case(tc, dry_run=False):
    """运行单个测试用例"""
    from agent_main import engine, query_lesson_schedule, get_ids, print_schedule_result

    print(f"\n{'#' * 60}")
    print(f"  运行测试 #{tc['id']:02d}: {tc['name']}")
    print(f"{'#' * 60}")
    print(f"  学生: {tc['student_name']}")
    print(f"  老师: {tc['teacher_name']}")
    print(f"  日期: {tc['date']}  时段: {SLOT_LABELS[tc['slot_index']]}")
    print(f"  需求: {tc['requirement']}")
    print(f"  预期: {tc['expected_behavior']}")
    print()

    # 解析 ID
    teacher_id, student_id = get_ids(engine, tc["teacher_name"], tc["student_name"])
    if teacher_id is None:
        print(f"  [ERROR] 找不到老师: {tc['teacher_name']}")
        return
    if student_id is None:
        print(f"  [ERROR] 找不到学生: {tc['student_name']}")
        return

    print(f"  已识别: 学生(ID:{student_id}), 老师(ID:{teacher_id})")

    # 查询课表
    schedule = query_lesson_schedule(engine, tc["date"], tc["slot_index"], teacher_id, student_id)
    if "error" in schedule:
        print(f"  [ERROR] 查询失败: {schedule['error']}")
        return

    print_schedule_result(schedule)

    student_busy = schedule["student_has_class"]
    teacher_busy = schedule["teacher_has_class"]

    # 冲突判断
    if not student_busy and not teacher_busy:
        print(f"\n  [结果] 无冲突 — 可直接排课")
    elif teacher_busy and student_busy:
        print(f"\n  [结果] 双冲突 — 老师和学生都有课")
    elif teacher_busy:
        print(f"\n  [结果] 老师有课，学生空闲")
    else:
        print(f"\n  [结果] 学生有课，老师空闲")

    if dry_run:
        print(f"\n  [DRY RUN] 跳过 Agent 调度")
        return

    if not student_busy and not teacher_busy:
        print(f"  无需 Agent，直接排课即可")
        return

    # 启动 Agent
    print(f"\n  启动 RescheduleAgent ...")
    from openai import OpenAI
    from agent_main import RescheduleAgent, print_proposals

    client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY", ""))
    agent = RescheduleAgent(engine, client, model="gpt-4.1")
    result = agent.run(
        student_name=tc["student_name"],
        teacher_name=tc["teacher_name"],
        student_id=student_id,
        teacher_id=teacher_id,
        requirement=tc["requirement"],
        date_str=tc["date"],
        slot_index=tc["slot_index"],
        max_steps=12,
    )

    print(f"\n  探索步数: {result['steps_used']}")
    print(f"  方案数: {len(result['proposals'])}")
    print_proposals(result["proposals"])
    print(f"\n  Agent 总结: {result['summary']}")


def main():
    if len(sys.argv) < 2:
        print_all_cases()
        return

    mode = sys.argv[1]

    if mode == "--run" and len(sys.argv) >= 3:
        target = sys.argv[2]
        if target == "all":
            for tc in TEST_CASES:
                run_case(tc)
        else:
            idx = int(target)
            tc = next((t for t in TEST_CASES if t["id"] == idx), None)
            if tc is None:
                print(f"找不到测试用例 #{idx}")
                return
            run_case(tc)

    elif mode == "--dry" and len(sys.argv) >= 3:
        target = sys.argv[2]
        if target == "all":
            for tc in TEST_CASES:
                run_case(tc, dry_run=True)
        else:
            idx = int(target)
            tc = next((t for t in TEST_CASES if t["id"] == idx), None)
            if tc is None:
                print(f"找不到测试用例 #{idx}")
                return
            run_case(tc, dry_run=True)

    else:
        print("用法:")
        print("  python test_cases.py              # 列出所有用例")
        print("  python test_cases.py --run N       # 运行第N个用例")
        print("  python test_cases.py --run all     # 运行所有用例")
        print("  python test_cases.py --dry N       # 仅查课表不调Agent")
        print("  python test_cases.py --dry all     # 全部仅查课表")


if __name__ == "__main__":
    main()
