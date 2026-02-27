import os
import json
from datetime import datetime, timedelta, time
from typing import Dict, Any, List, Tuple

import pandas as pd
from sqlalchemy import create_engine, text
from openai import OpenAI

# =========================
# 1. 数据库连接 & 工具函数
# =========================

USER    = "root"
PASS    = "fighting"           # TODO: 填你的密码
DB      = "env_huayao"
SOCKET  = "/tmp/mysql.sock"

engine = create_engine(
    f"mysql+pymysql://{USER}:{PASS}@localhost/{DB}?unix_socket={SOCKET}",
    pool_pre_ping=True
)


LESSON_MINUTES = 90  # 一节课 1.5 小时

# 固定可用的上课时间槽（每天）
ALLOWED_SLOTS = [
    ("09:00:00", "10:30:00"),
    ("10:30:00", "12:00:00"),
    ("13:30:00", "15:00:00"),
    ("15:00:00", "16:30:00"),
    ("16:30:00", "18:00:00"),
    ("18:00:00", "19:30:00"),
]

def build_slot_dt(slot_date: datetime.date, slot: Tuple[str, str]) -> Tuple[datetime, datetime]:
    start_str, end_str = slot
    slot_start = datetime.combine(slot_date, time.fromisoformat(start_str.strip()))
    slot_end = datetime.combine(slot_date, time.fromisoformat(end_str.strip()))
    return slot_start, slot_end

def intervals_overlap(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    return not (a_end <= b_start or b_end <= a_start)

from datetime import datetime, timedelta
from typing import Any, Dict, List, Tuple, Optional

# 你原来就有的：
# ALLOWED_SLOTS = [...]
# build_slot_dt(...)

def get_conflicting_lessons_for_group(
    engine,
    teacher_id: int,
    student_ids: List[int],
    slot_start_dt: datetime,
    slot_end_dt: datetime,
) -> List[Dict[str, Any]]:
    """
    查出在 [slot_start_dt, slot_end_dt] 时间段内，
    老师 teacher_id 或 student_ids 中任意一个学生有课的所有 lesson 记录。

    返回的每个 lesson 字典至少包含：
      - lesson_id
      - lesson_start / lesson_end
      - subject_id, class_id, teacher_id, class_name
      - topic_name / topic_cn_name / topic（方便人类阅读）
    """
    start_unix = int(slot_start_dt.timestamp())
    end_unix = int(slot_end_dt.timestamp())

    results: List[Dict[str, Any]] = []

    with engine.connect() as conn:
        # 1) 老师的冲突课
        teacher_sql = text("""
            SELECT
                l.id                                                            AS lesson_id,
                IF(l.start_time = -1, 'PERMANENT', FROM_UNIXTIME(l.start_time)) AS lesson_start,
                IF(l.end_time   = -1, 'PERMANENT', FROM_UNIXTIME(l.end_time))   AS lesson_end,
                s.id                                                            AS subject_id,
                s.class_id                                                      AS class_id,
                s.teacher_id                                                    AS teacher_id,
                c.class_type                                                    AS class_name,
                t.name                                                          AS topic_name,
                t.cn_name                                                       AS topic_cn_name
            FROM lessons l
            JOIN subjects s ON l.subject_id = s.id
            JOIN classes  c ON s.class_id  = c.id
            LEFT JOIN topics t ON s.topic_id = t.id
            WHERE ((l.start_time = -1 OR l.start_time <= :end_unix)
               AND (l.end_time   = -1 OR l.end_time   >= :start_unix))
              AND s.teacher_id = :tid
        """)
        teacher_rows = conn.execute(
            teacher_sql,
            {"start_unix": start_unix, "end_unix": end_unix, "tid": teacher_id},
        ).mappings().all()
        results.extend(dict(r) for r in teacher_rows)

        # 2) 学生的冲突课（任一学生）
        if student_ids:
            student_sql = text("""
                SELECT
                    l.id                                                            AS lesson_id,
                    IF(l.start_time = -1, 'PERMANENT', FROM_UNIXTIME(l.start_time)) AS lesson_start,
                    IF(l.end_time   = -1, 'PERMANENT', FROM_UNIXTIME(l.end_time))   AS lesson_end,
                    s.id                                                            AS subject_id,
                    s.class_id                                                      AS class_id,
                    s.teacher_id                                                    AS teacher_id,
                    c.class_type                                                    AS class_name,
                    t.name                                                          AS topic_name,
                    t.cn_name                                                       AS topic_cn_name
                FROM lessons l
                JOIN subjects s ON l.subject_id = s.id
                JOIN classes  c ON s.class_id  = c.id
                JOIN student_classes sc ON sc.class_id = c.id
                LEFT JOIN topics t ON s.topic_id = t.id
                WHERE ((l.start_time = -1 OR l.start_time <= :end_unix)
                   AND (l.end_time   = -1 OR l.end_time   >= :start_unix))
                  AND sc.student_id IN :sids
            """)
            student_rows = conn.execute(
                student_sql,
                {"start_unix": start_unix, "end_unix": end_unix, "sids": tuple(student_ids)},
            ).mappings().all()
            results.extend(dict(r) for r in student_rows)

    # 3) 按 lesson_id 去重，并补一个 topic 字段
    dedup: Dict[int, Dict[str, Any]] = {}
    for row in results:
        lid = row.get("lesson_id")
        if lid is None:
            continue
        if lid not in dedup:
            topic_name = row.get("topic_name") or ""
            topic_cn_name = row.get("topic_cn_name") or ""
            row["topic"] = f"{topic_name}{topic_cn_name}"
            dedup[lid] = row

    return list(dedup.values())


def get_ids(engine, teacher_name: str, student_name: str) -> Tuple[int, int]:
    """
    把姓名映射成 teacher_id / student_id。
    staff / students 里用的是 name_search_cache。
    """
    with engine.connect() as conn:
        teacher_id = conn.execute(
            text("""
                SELECT id 
                FROM staff 
                WHERE name_search_cache = :name
                LIMIT 1
            """),
            {"name": teacher_name}
        ).scalar()

        student_id = conn.execute(
            text("""
                SELECT id 
                FROM students 
                WHERE name_search_cache = :name
                LIMIT 1
            """),
            {"name": student_name}
        ).scalar()

    return teacher_id, student_id

def check_schedule(
    engine,
    start_time_str: str,
    end_time_str: str,
    teacher_name: str,
    student_name: str,
):
    """
    原来的 check_schedule，略微改成更容易给 LLM 使用：
    - 保留原来的 SQL 逻辑
    - 多返回结构化数据，少用 print
    """
    start_dt = datetime.fromisoformat(start_time_str)
    end_dt = datetime.fromisoformat(end_time_str)
    start_unix = int(start_dt.timestamp())
    end_unix = int(end_dt.timestamp())

    teacher_id, student_id = get_ids(engine, teacher_name, student_name)

    def _fmt_time(ts):
        if ts is None:
            return "NULL"
        if ts == -1:
            return "PERMANENT(-1)"
        try:
            return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return f"RAW({ts})"

    with engine.connect() as conn:
        # ----- 学生在该时间的课 -----
        student_rows = []
        if student_id is not None:
            student_classes_sql = text("""
                SELECT l.id                                                            AS lesson_id,
                       IF(l.start_time = -1, 'PERMANENT', FROM_UNIXTIME(l.start_time)) AS lesson_start,
                       IF(l.end_time = -1, 'PERMANENT', FROM_UNIXTIME(l.end_time))     AS lesson_end,
                       s.id                                                            AS subject_id,
                       s.class_id                                                      AS class_id,
                       s.teacher_id                                                    AS teacher_id,
                       c.class_type                                                    AS class_name,
                       sc.start_time                                                   AS sc_start_time,
                       sc.end_time                                                     AS sc_end_time,
                       t.name                                                          AS topic_name,
                       t.cn_name                                                       AS topic_cn_name
                FROM lessons l
                         JOIN subjects s ON l.subject_id = s.id
                         JOIN classes c ON s.class_id = c.id
                         JOIN student_classes sc ON sc.class_id = s.class_id
                         LEFT JOIN topics t ON s.topic_id = t.id
                WHERE ((l.start_time = -1 OR l.start_time <= :end_unix)
                    AND (l.end_time = -1 OR l.end_time >= :start_unix))
                  AND ((sc.start_time = -1 OR sc.start_time <= :end_unix)
                    AND (sc.end_time = -1 OR sc.end_time >= :start_unix))
                  AND sc.student_id = :student_id
                ORDER BY l.start_time
            """)

            student_rows = conn.execute(
                student_classes_sql,
                {
                    "start_unix": start_unix,
                    "end_unix": end_unix,
                    "student_id": student_id,
                },
            ).mappings().all()

        # ----- 老师在该时间的课 -----
        teacher_rows = []
        if teacher_id is not None:
            teacher_classes_sql = text("""
                SELECT l.id                                                            AS lesson_id,
                       IF(l.start_time = -1, 'PERMANENT', FROM_UNIXTIME(l.start_time)) AS lesson_start,
                       IF(l.end_time = -1, 'PERMANENT', FROM_UNIXTIME(l.end_time))     AS lesson_end,
                       s.id                                                            AS subject_id,
                       s.class_id                                                      AS class_id,
                       s.teacher_id                                                    AS teacher_id,
                       c.class_type                                                    AS class_name,
                       t.name                                                          AS topic_name,
                       t.cn_name                                                       AS topic_cn_name
                FROM lessons l
                         JOIN subjects s ON l.subject_id = s.id
                         JOIN classes c ON s.class_id = c.id
                         LEFT JOIN topics t ON s.topic_id = t.id
                WHERE ((l.start_time = -1 OR l.start_time <= :end_unix)
                    AND (l.end_time = -1 OR l.end_time >= :start_unix))
                  AND s.teacher_id = :teacher_id
            """)

            teacher_rows = conn.execute(
                teacher_classes_sql,
                {
                    "start_unix": start_unix,
                    "end_unix": end_unix,
                    "teacher_id": teacher_id,
                },
            ).mappings().all()

    # 结构化返回
    student_time_desc = []
    student_struct = []
    if not student_rows:
        student_time_desc.append("No student course.")
    else:
        for r in student_rows:
            row = dict(r)
            topic_name = row.get("topic_name") or ""
            topic_cn_name = row.get("topic_cn_name") or ""
            row["topic"] = f"{topic_name}{topic_cn_name}"
            student_struct.append(row)
            student_time_desc.append(
                f"{row['lesson_id']}:{ _fmt_time(row['lesson_start']) } ~ { _fmt_time(row['lesson_end']) }"
            )

    teacher_time_desc = []
    teacher_struct = []
    if not teacher_rows:
        teacher_time_desc.append("No teacher course.")
    else:
        for r in teacher_rows:
            row = dict(r)
            topic_name = row.get("topic_name") or ""
            topic_cn_name = row.get("topic_cn_name") or ""
            row["topic"] = f"{topic_name}{topic_cn_name}"
            teacher_struct.append(row)
            teacher_time_desc.append(
                f"{row['topic']}:{ row['lesson_start'] } ~ { row['lesson_end'] }"
            )

    return {
        "student_struct": student_struct,
        "teacher_struct": teacher_struct,
        "student_time_desc": student_time_desc,
        "teacher_time_desc": teacher_time_desc,
        "teacher_id": teacher_id,
        "student_id": student_id,
        "start_time": start_time_str,
        "end_time": end_time_str,
    }

def direct_check_and_plan(
    engine,
    student_name: str,
    teacher_name: str,
    intent_start: str,
    intent_end: str,
) -> Dict[str, Any]:
    """
    第一步：用纯算法检查当前老师是否在该时间有冲突。
    返回：
    {
        "status": "ok" / "teacher_busy" / "student_busy" / "both_busy",
        "check_result": check_schedule(...) 的原始结果
    }
    """
    result = check_schedule(
        engine,
        start_time_str=intent_start,
        end_time_str=intent_end,
        teacher_name=teacher_name,
        student_name=student_name,
    )

    student_has_class = len(result["student_struct"]) > 0
    teacher_has_class = len(result["teacher_struct"]) > 0

    if not student_has_class and not teacher_has_class:
        status = "ok"
    elif teacher_has_class and not student_has_class:
        status = "teacher_busy"
    elif student_has_class and not teacher_has_class:
        status = "student_busy"
    else:
        status = "both_busy"

    return {
        "status": status,
        "check_result": result,
    }

def fetch_all_topics(engine) -> List[Dict[str, Any]]:
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT id, name, cn_name FROM topics")
        ).mappings().all()
    return [dict(r) for r in rows]

def llm_select_topic_ids(client, model: str, topics: List[Dict[str, Any]], requirement: str) -> List[int]:
    """
    把 topics 全表和「排课需求描述」一起丢给 LLM，让它输出一个 topic_id 列表。
    """
    prompt = f"""
你是一个排课助手。下面是 topics 表的全部行：

{json.dumps(topics, ensure_ascii=False, indent=2)}

排课需求为（自然语言）：{requirement}

请你判断，这个排课需求对应的是哪些 topic_id（可能是 1 个，也可能是多个）。
只输出一个 JSON，格式如下：

{{
  "topic_ids": [1, 2, 3]
}}

如果你不确定，就尽量根据 name / cn_name 猜最可能的几个。
"""

    resp = client.responses.create(
        model=model,
        input=prompt,
    )
    text_out = resp.output[0].content[0].text.strip()
    try:
        parsed = json.loads(text_out)
        topic_ids = parsed.get("topic_ids", [])
        topic_ids = [int(x) for x in topic_ids]
        return topic_ids
    except Exception:
        # 防御：如果解析失败，直接返回空列表
        return []

def fetch_teachers_for_topics(engine, topic_ids: List[int]) -> List[int]:
    """
    在 subjects 表里，根据 topic_ids 找到所有涉及到的 teacher_id。
    去重返回。
    """
    if not topic_ids:
        return []

    with engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT DISTINCT teacher_id
                FROM subjects
                WHERE topic_id IN :topic_ids
                  AND teacher_id IS NOT NULL
            """),
            {"topic_ids": tuple(topic_ids)}
        ).fetchall()

    teacher_ids = [r[0] for r in rows]
    return teacher_ids

def fetch_teacher_names(engine, teacher_ids: List[int]) -> Dict[int, str]:
    """
    返回 {teacher_id: name} 映射。
    这里用 staff 表里的 name_search_cache 作为老师名字。
    """
    if not teacher_ids:
        return {}

    with engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT id, name_search_cache AS name
                FROM staff
                WHERE id IN :ids
            """),
            {"ids": tuple(teacher_ids)}
        ).mappings().all()

    return {int(r["id"]): r["name"] for r in rows}

def check_all_candidate_teachers(
    engine,
    student_name: str,
    teacher_map: Dict[int, str],
    intent_start: str,
    intent_end: str,
) -> List[Dict[str, Any]]:
    """
    对候选 teacher_id/teacher_name 逐个调用 check_schedule。
    返回一个列表，每个元素是：
    {
      "teacher_id": ...,
      "teacher_name": "...",
      "check_result": {...},
      "status": "ok" / "teacher_busy" / "both_busy" / ...
    }
    """
    results = []
    for tid, tname in teacher_map.items():
        r = check_schedule(
            engine,
            start_time_str=intent_start,
            end_time_str=intent_end,
            teacher_name=tname,
            student_name=student_name,
        )
        teacher_has_class = len(r["teacher_struct"]) > 0
        student_has_class = len(r["student_struct"]) > 0

        if not teacher_has_class and not student_has_class:
            status = "ok"
        elif teacher_has_class and not student_has_class:
            status = "teacher_busy"
        elif student_has_class and not teacher_has_class:
            status = "student_busy"
        else:
            status = "both_busy"

        results.append({
            "teacher_id": tid,
            "teacher_name": tname,
            "check_result": r,
            "status": status,
        })
    return results

# def llm_choose_teacher_from_candidates(
#     client,
#     model: str,
#     requirement: str,
#     candidate_results: List[Dict[str, Any]],
# ) -> Dict[str, Any]:
#     """
#     把所有候选老师在这个时间段的空闲/忙碌情况传给 LLM，让它选一个最合适的。
#     返回：
#     {
#       "chosen_teacher_id": ... 或 None,
#       "final_text": "给管理员看的文字说明"
#     }
#     """
#     prompt = f"""
# 你是一个排课助手。排课需求：{requirement}
#
# 下面是所有候选老师在目标时间段内的排班情况（JSON）：
#
# {json.dumps(candidate_results, ensure_ascii=False, indent=2)}
#
# 请你根据这些信息，判断：
# 1. 如果存在至少一位老师 status == "ok"，请选择其中一个最合适的老师（可以考虑不要太多冲突课之类的简单原则）。
# 2. 如果所有老师都不能排（没有 "ok"），就说明无法通过换老师的方式解决。
#
# 输出 JSON，格式如下：
#
# {{
#   "chosen_teacher_id": 123,   // 如果没有合适老师，写 null
#   "final_text": "用中文给排课管理员的一段解释：包括你选了哪个老师，为什么；如果不行，说明原因。"
# }}
# """
#
#     resp = client.responses.create(
#         model=model,
#         input=prompt,
#     )
#     text_out = resp.output[0].content[0].text.strip()
#     try:
#         parsed = json.loads(text_out)
#         return parsed
#     except Exception:
#         return {
#             "chosen_teacher_id": None,
#             "final_text": f"LLM 输出无法解析，原始输出：\n{text_out}"
#         }

def change_teacher_strategy(
    engine,
    client,
    model: str,
    student_name: str,
    requirement: str,
    intent_start: str,
    intent_end: str,
) -> Dict[str, Any]:
    """
    方案一：换老师。
    返回：
    {
      "success": True/False,
      "final_text": "...",
      "chosen_teacher_name": "... 或 None"
    }
    """
    # 1. 拿 topics 全表
    topics = fetch_all_topics(engine)

    # 2. 让 LLM 从 topics 里选出相关 topic_id
    topic_ids = llm_select_topic_ids(client, model, topics, requirement)

    if not topic_ids:
        return {
            "success": False,
            "final_text": "无法从 topics 表中识别出与该排课需求对应的 topic_id，换老师策略失败。",
            "chosen_teacher_name": None,
        }

    # 3. 根据 topic_id 找所有可能的 teacher_id
    teacher_ids = fetch_teachers_for_topics(engine, topic_ids)
    teacher_map = fetch_teacher_names(engine, teacher_ids)

    if not teacher_map:
        return {
            "success": False,
            "final_text": "根据对应的 topic_id 未能找到任何老师，换老师策略失败。",
            "chosen_teacher_name": None,
        }

    # 4. 对这些老师逐个 check_schedule
    candidate_results = check_all_candidate_teachers(
        engine,
        student_name=student_name,
        teacher_map=teacher_map,
        intent_start=intent_start,
        intent_end=intent_end,
    )

    # # 5. 再交给 LLM 选一个老师（或声明不行）
    # choice = llm_choose_teacher_from_candidates(
    #     client,
    #     model,
    #     requirement,
    #     candidate_results,
    # )

    # 找所有能直接排课的老师，而不是只选一个
    available_teachers = [
        item for item in candidate_results
        if item["status"] == "ok"
    ]
    return {
        "success": len(available_teachers) > 0,
        "candidates": available_teachers,
    }

    chosen_id = choice.get("chosen_teacher_id")
    chosen_name = teacher_map.get(chosen_id) if chosen_id is not None else None
    final_text = choice.get("final_text", "")

    return {
        "success": chosen_name is not None,
        "final_text": final_text,
        "chosen_teacher_name": chosen_name,
    }

def get_class_participants(engine, class_id: int) -> Tuple[int, List[int]]:
    """
    给一个 class_id，返回：
    - 这门课的 teacher_id
    - 这门课所有学生的 student_id 列表
    """
    with engine.connect() as conn:
        # teacher_id 在 subjects 表里
        teacher_id = conn.execute(
            text("SELECT teacher_id FROM subjects WHERE class_id = :cid LIMIT 1"),
            {"cid": class_id},
        ).scalar()

        # 学生在 student_classes 表里
        rows = conn.execute(
            text("SELECT DISTINCT student_id FROM student_classes WHERE class_id = :cid"),
            {"cid": class_id},
        ).fetchall()

    student_ids = [r[0] for r in rows]
    return teacher_id, student_ids

def is_group_free(
    engine,
    teacher_id: int,
    student_ids: List[int],
    start_unix: int,
    end_unix: int,
) -> bool:
    """
    检查一个老师 + 多个学生，在给定时间段内是否都没有课。
    为了简单，用两次 SQL：
    1. 检查 teacher lessons
    2. 检查 students lessons（IN (...)）
    """
    with engine.connect() as conn:
        # 1）老师有没有课
        teacher_conflict = conn.execute(
            text("""
                SELECT COUNT(*) FROM lessons l
                JOIN subjects s ON l.subject_id = s.id
                WHERE ((l.start_time = -1 OR l.start_time <= :end_unix)
                   AND (l.end_time = -1 OR l.end_time >= :start_unix))
                  AND s.teacher_id = :tid
            """),
            {"start_unix": start_unix, "end_unix": end_unix, "tid": teacher_id},
        ).scalar()

        if teacher_conflict and teacher_conflict > 0:
            return False

        if not student_ids:
            # 没学生也可以视为没冲突
            return True

        # 2）学生有没有课
        # NOTE: 这里假设 student_classes -> classes -> subjects -> lessons 的关系；
        # 简化写法，可以根据你的真实 schema 调整。
        student_conflict = conn.execute(
            text("""
                SELECT COUNT(*) 
                FROM lessons l
                JOIN subjects s ON l.subject_id = s.id
                JOIN classes c ON s.class_id = c.id
                JOIN student_classes sc ON sc.class_id = c.id
                WHERE ((l.start_time = -1 OR l.start_time <= :end_unix)
                   AND (l.end_time = -1 OR l.end_time >= :start_unix))
                  AND sc.student_id IN :sids
            """),
            {"start_unix": start_unix, "end_unix": end_unix, "sids": tuple(student_ids)},
        ).scalar()

        if student_conflict and student_conflict > 0:
            return False

    return True

from datetime import datetime, timedelta

def find_future_slot_for_class_discrete(
    engine,
    teacher_id: int,
    student_ids: List[int],
    from_dt: datetime,
    horizon_days: int = 30,
) -> Tuple[datetime, datetime] | Tuple[None, None]:
    """
    在 from_dt 之后 horizon_days 的范围内，寻找一个所有人都空的 slot。
    slot 固定为 ALLOWED_SLOTS 中的 90 分钟时间段。
    """
    for day_offset in range(horizon_days):
        day = (from_dt + timedelta(days=day_offset)).date()

        for slot in ALLOWED_SLOTS:
            slot_start_dt, slot_end_dt = build_slot_dt(day, slot)

            # 不能排到 from_dt 之前
            if slot_end_dt <= from_dt:
                continue

            start_unix = int(slot_start_dt.timestamp())
            end_unix = int(slot_end_dt.timestamp())

            if is_group_free(engine, teacher_id, student_ids, start_unix, end_unix):
                return slot_start_dt, slot_end_dt
    return None, None


def enumerate_future_slots_for_group(
    engine,
    teacher_id: int,
    student_ids: List[int],
    from_dt: datetime,
    horizon_days: int = 7,
) -> List[Tuple[datetime, datetime]]:
    slots = []
    for day_offset in range(horizon_days):
        day = (from_dt + timedelta(days=day_offset)).date()
        if day.weekday() >= 5:  # 跳周末
            continue
        for slot in ALLOWED_SLOTS:
            s, e = build_slot_dt(day, slot)
            if e <= from_dt:
                continue
            slots.append((s, e))
    # 这里暂时不做排序优化，你可以按时间自然顺序就行
    return slots

def enumerate_future_slots(
    from_dt: datetime,
    horizon_days: int = 7,
) -> List[Tuple[datetime, datetime]]:
    """
    列出 from_dt 之后 horizon_days 内所有工作日的 ALLOWED_SLOTS。
    不检查是否空闲，只是产生候选时间段。
    """
    slots: List[Tuple[datetime, datetime]] = []
    for day_offset in range(horizon_days):
        day = (from_dt + timedelta(days=day_offset)).date()
        # 跳过周末：Monday=0,...,Sunday=6
        if day.weekday() >= 5:
            continue
        for s_str, e_str in ALLOWED_SLOTS:
            s_dt, e_dt = build_slot_dt(day, (s_str, e_str))
            # 必须在 from_dt 之后
            if e_dt <= from_dt:
                continue
            slots.append((s_dt, e_dt))
    # 自然顺序 = 时间顺序
    return slots

def clear_slot_for_group(
    engine,
    teacher_id: int,
    student_ids: List[int],
    slot_start_dt: datetime,
    slot_end_dt: datetime,
    depth: int,
    max_depth: int,
    horizon_days: int,
) -> Optional[List[Dict[str, Any]]]:
    """
    目标：让 (teacher_id, student_ids) 在 [slot_start_dt, slot_end_dt] 这个时间段空出来。
    可以通过挪走这个时间段里的冲突课程来实现，最多递归 max_depth 层。

    返回：
      - 若成功：返回 move_plan 列表
        每个元素形如：
          {
              "original_lesson": {...},
              "new_start": "YYYY-MM-DD HH:MM:SS",
              "new_end": "YYYY-MM-DD HH:MM:SS",
          }
      - 若在当前 depth~max_depth 范围内做不到：返回 None
    """

    # 1. 找出这个时间段内，teacher 或 student_ids 有哪些课挡路
    conflicts = get_conflicting_lessons_for_group(
        engine,
        teacher_id=teacher_id,
        student_ids=student_ids,
        slot_start_dt=slot_start_dt,
        slot_end_dt=slot_end_dt,
    )
    if not conflicts:
        # 已经没有冲突，不需要挪任何课
        return []

    move_plan: List[Dict[str, Any]] = []

    for les in conflicts:
        class_id = les.get("class_id")
        if class_id is None:
            # 没有 class_id，没法进一步查询参与者，保守失败
            return None

        # 这一节课真正参与的老师 + 学生
        les_teacher_id, les_student_ids = get_class_participants(engine, class_id)

        # 从这节课原时间之后开始找
        les_start_str = les.get("lesson_start")
        try:
            from_dt = datetime.fromisoformat(str(les_start_str))
        except Exception:
            from_dt = slot_start_dt  # 兜底

        # 2. 第一招：尝试一层挪课 —— 找一个所有参与者都空闲的未来 slot
        new_start_dt, new_end_dt = find_future_slot_for_class_discrete(
            engine,
            teacher_id=les_teacher_id,
            student_ids=les_student_ids,
            from_dt=from_dt,
            horizon_days=horizon_days,
        )
        if new_start_dt is not None:
            # 很好，这节课不用牵连别人就能挪走
            move_plan.append({
                "original_lesson": les,
                "new_start": new_start_dt.strftime("%Y-%m-%d %H:%M:%S"),
                "new_end": new_end_dt.strftime("%Y-%m-%d %H:%M:%S"),
            })
            continue

        # 3. 一层挪课失败，如果已经到最大 depth，就整体失败
        if depth >= max_depth:
            return None

        # 4. 第二招：在未来一周枚举一些候选时间段，把这节课挪过去，但需要先“清空”那个时间段
        candidate_slots = enumerate_future_slots(from_dt=from_dt, horizon_days=horizon_days)

        success_for_this_lesson = False

        for cand_start_dt, cand_end_dt in candidate_slots:
            # 4.1 递归尝试：清空 cand_slot
            sub_plan = clear_slot_for_group(
                engine,
                teacher_id=les_teacher_id,
                student_ids=les_student_ids,
                slot_start_dt=cand_start_dt,
                slot_end_dt=cand_end_dt,
                depth=depth + 1,
                max_depth=max_depth,
                horizon_days=horizon_days,
            )
            if sub_plan is None:
                # 这个候选时间段没法在给定 depth 范围内清空，换下一个
                continue

            # 4.2 cand_slot 已经可以被清空，把当前这节课挪过去
            move_plan.append({
                "original_lesson": les,
                "new_start": cand_start_dt.strftime("%Y-%m-%d %H:%M:%S"),
                "new_end": cand_end_dt.strftime("%Y-%m-%d %H:%M:%S"),
            })
            # 把清空 cand_slot 过程中产生的挪课计划也加进来
            move_plan.extend(sub_plan)

            success_for_this_lesson = True
            break

        if not success_for_this_lesson:
            # 所有候选时间都清不出位置给这节课
            return None

    # 所有挡路的课都成功移走了
    return move_plan

def extract_conflicting_lessons(check_result: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    从 check_schedule 的返回中，提取老师在该时间段的所有lesson。
    这些就是“候选可以被挪走的课”。
    """
    return check_result.get("teacher_struct", [])

def find_future_slots_for_class_discrete_all(
    engine,
    teacher_id: int,
    student_ids: List[int],
    from_dt: datetime,
    horizon_days: int = 7,
    max_slots: int = 20,
) -> List[Tuple[datetime, datetime]]:
    """
    在 from_dt 之后 horizon_days 的范围内，寻找所有“老师 + 学生都空闲”的 90min slot。
    - slot 固定为 ALLOWED_SLOTS 中的时间段
    - 只考虑工作日（周一~周五），不排周末
    - 最多返回 max_slots 个候选（以免结果太长）
    """
    results: List[Tuple[datetime, datetime]] = []

    for day_offset in range(horizon_days):
        day = (from_dt + timedelta(days=day_offset)).date()

        # weekday(): Monday=0, Sunday=6；5、6 是周六周日 → 跳过
        if day.weekday() >= 5:
            continue

        for slot in ALLOWED_SLOTS:
            slot_start_dt, slot_end_dt = build_slot_dt(day, slot)

            # 必须在 from_dt 之后
            if slot_end_dt <= from_dt:
                continue

            start_unix = int(slot_start_dt.timestamp())
            end_unix = int(slot_end_dt.timestamp())

            if is_group_free(engine, teacher_id, student_ids, start_unix, end_unix):
                results.append((slot_start_dt, slot_end_dt))
                if len(results) >= max_slots:
                    return results
    return results

def move_existing_lessons_strategy(
    engine,
    client,
    model: str,
    student_name: str,
    teacher_name: str,
    intent_start: str,
    intent_end: str,
    base_check_result: Dict[str, Any],
) -> Dict[str, Any]:
    """
    方案二：考虑把老师在这段时间的冲突课挪到未来某个共同空档。
    目标：只需要在 intent_start ~ intent_end 之间空出“一个完整的 90 分钟 slot”，
    而不是把所有冲突课全搬走。

    这里不再生成 LLM 文本说明，只返回结构化的候选方案：
    {
        "success": True/False,
        "candidates": [
            {
                "slot": "YYYY-MM-DD HH:MM:SS ~ YYYY-MM-DD HH:MM:SS",  # 目标新课时间
                "move_plan": [
                    {
                        "original_lesson": {...},   # 原课程记录
                        "options": [               # 这节课所有可选的新时间
                            {"new_start": "...", "new_end": "..."},
                            ...
                        ],
                    },
                    ...
                ],
            },
            ...
        ],
        "reason": "如果 success=False，会给出原因（可选）",
    }
    """
    # 1. 抽出老师在原始意向时间段内的所有课程（冲突课）
    conflicts = extract_conflicting_lessons(base_check_result)
    if not conflicts:
        return {
            "success": False,
            "candidates": [],
            "reason": "该老师在目标时间段没有任何课程记录，理论上不必挪课，但仍有冲突，可能是数据问题。",
        }

    # 2. 解析意向时间
    intent_start_dt = datetime.fromisoformat(intent_start)
    intent_end_dt = datetime.fromisoformat(intent_end)
    target_date = intent_start_dt.date()  # 假设新课只安排在同一天

    # 3. 枚举目标时间段内的所有“可用 slot”（90min固定节次）
    candidate_target_slots: List[Tuple[datetime, datetime]] = []
    for slot in ALLOWED_SLOTS:
        slot_start_dt, slot_end_dt = build_slot_dt(target_date, slot)
        # 只保留完全落在意向时间区间内的 slot
        if slot_start_dt >= intent_start_dt and slot_end_dt <= intent_end_dt:
            candidate_target_slots.append((slot_start_dt, slot_end_dt))

    if not candidate_target_slots:
        return {
            "success": False,
            "candidates": [],
            "reason": f"在 {intent_start} ~ {intent_end} 区间内没有完整的 90 分钟固定上课时间槽，无法安排新课。",
        }

    # 4. 针对每一个候选目标 slot：尝试给所有冲突课找【未来一周内所有可行新时间】
    all_possible_move_plans: List[Dict[str, Any]] = []

    for slot_start_dt, slot_end_dt in candidate_target_slots:
        # 4.1 找出“与这个 slot 有重叠”的课程（只挪这些）
        slot_conflicts: List[Dict[str, Any]] = []
        for les in conflicts:
            les_start_str = les.get("lesson_start")
            les_end_str = les.get("lesson_end")
            try:
                les_start_dt = datetime.fromisoformat(str(les_start_str))
                les_end_dt = datetime.fromisoformat(str(les_end_str))
            except Exception:
                continue

            # 判断时间段是否重叠
            if intervals_overlap(les_start_dt, les_end_dt, slot_start_dt, slot_end_dt):
                slot_conflicts.append(les)

        # 这个 slot 对老师来说如果本来就没有冲突课，那 move_plan 为空也算一个合法方案
        if not slot_conflicts:
            all_possible_move_plans.append({
                "slot": f"{slot_start_dt.strftime('%Y-%m-%d %H:%M:%S')} ~ {slot_end_dt.strftime('%Y-%m-%d %H:%M:%S')}",
                "move_plan": [],
            })
            continue

        # 4.2 对这些与 slot 冲突的课逐个尝试找未来一周内所有可行 slot
        current_move_plan: List[Dict[str, Any]] = []
        can_free_this_slot = True

        for les in slot_conflicts:
            class_id = les.get("class_id")
            if class_id is None:
                # 没有 class_id，没法继续，保守认为这个 slot 不可用
                can_free_this_slot = False
                break

            teacher_id, student_ids = get_class_participants(engine, class_id)

            # 从这节课的原时间之后开始找未来 slot
            les_start_str = les.get("lesson_start")
            try:
                from_dt = datetime.fromisoformat(str(les_start_str))
            except Exception:
                from_dt = slot_start_dt  # 兜底：用目标 slot 起始时间

            # ⭐ 拿到“未来一周内所有可行的 slot”（你之前已经实现了这个函数）
            all_slots = find_future_slots_for_class_discrete_all(
                engine,
                teacher_id=teacher_id,
                student_ids=student_ids,
                from_dt=from_dt,
                horizon_days=7,
                max_slots=20,
            )

            if not all_slots:
                # 这一节课在未来一周工作日内找不到任何可行 slot → 这个目标 slot 失败
                can_free_this_slot = False
                break

            options = [
                {
                    "new_start": s.strftime("%Y-%m-%d %H:%M:%S"),
                    "new_end": e.strftime("%Y-%m-%d %H:%M:%S"),
                }
                for (s, e) in all_slots
            ]

            current_move_plan.append({
                "original_lesson": les,
                "options": options,   # 每节课一个 options 列表
            })

        if can_free_this_slot:
            all_possible_move_plans.append({
                "slot": f"{slot_start_dt.strftime('%Y-%m-%d %H:%M:%S')} ~ {slot_end_dt.strftime('%Y-%m-%d %H:%M:%S')}",
                "move_plan": current_move_plan,
            })

    # 5. 汇总返回
    if not all_possible_move_plans:
        return {
            "success": False,
            "candidates": [],
            "reason": "在目标日期的所有固定上课时间槽中，无法在未来一周内为冲突课程找到任何替代时间。",
        }

    return {
        "success": True,
        "candidates": all_possible_move_plans,
        "reason": "",
    }

def summarize_student_week(engine, student_name: str, week_start: str, week_end: str) -> List[Dict[str, Any]]:
    """
    给 LLM 看一个学生一段时间内的完整课表（比如一周）。
    week_start / week_end: 'YYYY-MM-DD 00:00:00' ~ 'YYYY-MM-DD 23:59:59'
    这里只查 student_classes + lessons。
    """
    start_dt = datetime.fromisoformat(week_start)
    end_dt = datetime.fromisoformat(week_end)
    start_unix = int(start_dt.timestamp())
    end_unix = int(end_dt.timestamp())

    _, student_id = get_ids(engine, teacher_name="", student_name=student_name)

    if student_id is None:
        return []

    with engine.connect() as conn:
        sql = text("""
            SELECT l.id                                                            AS lesson_id,
                   IF(l.start_time = -1, 'PERMANENT', FROM_UNIXTIME(l.start_time)) AS lesson_start,
                   IF(l.end_time = -1, 'PERMANENT', FROM_UNIXTIME(l.end_time))     AS lesson_end,
                   s.id                                                            AS subject_id,
                   s.class_id                                                      AS class_id,
                   s.teacher_id                                                    AS teacher_id,
                   c.class_type                                                    AS class_name,
                   t.name                                                          AS topic_name,
                   t.cn_name                                                       AS topic_cn_name
            FROM lessons l
                     JOIN subjects s ON l.subject_id = s.id
                     JOIN classes c ON s.class_id = c.id
                     JOIN student_classes sc ON sc.class_id = s.class_id
                     LEFT JOIN topics t ON s.topic_id = t.id
            WHERE ((l.start_time = -1 OR l.start_time <= :end_unix)
                AND (l.end_time = -1 OR l.end_time >= :start_unix))
              AND ((sc.start_time = -1 OR sc.start_time <= :end_unix)
                AND (sc.end_time = -1 OR sc.end_time >= :start_unix))
              AND sc.student_id = :student_id
            ORDER BY l.start_time
        """)
        rows = conn.execute(
            sql,
            {
                "start_unix": start_unix,
                "end_unix": end_unix,
                "student_id": student_id,
            }
        ).mappings().all()

    result = []
    for r in rows:
        row = dict(r)
        topic_name = row.get("topic_name") or ""
        topic_cn_name = row.get("topic_cn_name") or ""
        row["topic"] = f"{topic_name}{topic_cn_name}"
        result.append(row)
    return result

# =========================
# 2. LLM Agent 核心
# =========================

class SchedulingAgent:
    """
    一个非常简化的「多轮 agent」：
    - LLM 只输出 JSON 指令
    - Python 根据指令调用工具（check_schedule / summarize_student_week）
    - 状态中记录每轮工具结果，继续喂给下一轮
    """
    def __init__(self, engine, client: OpenAI, model: str = "gpt-4.1"):
        self.engine = engine
        self.client = client
        self.model = model

    def run(
        self,
        student_name: str,
        teacher_name: str,
        requirement: str,
        intent_start: str,
        intent_end: str,
        week_start: str,
        week_end: str,
        max_steps: int = 6,
    ) -> str:
        # 初始化状态
        student_week = summarize_student_week(self.engine, student_name, week_start, week_end)
        state: Dict[str, Any] = {
            "student_name": student_name,
            "teacher_name": teacher_name,
            "requirement": requirement,
            "intent_interval": {
                "start": intent_start,
                "end": intent_end,
            },
            "student_week": student_week,
            "tool_history": [],   # 每一次工具调用的记录
            "plan_candidate": [], # LLM 拟定中的换课方案（可选）
        }

        # 每一轮：把 state 丢给 LLM，请它返回 JSON 指令
        for step in range(max_steps):
            llm_json = self._call_llm(state)
            action = llm_json.get("action")

            if action == "finish":
                # LLM 认为已经给出了最终方案
                final_text = llm_json.get("final_text", "（LLM 未提供 final_text）")
                return final_text

            elif action == "check_interval":
                # 让算法去查具体某个时间段的 teacher / student 冲突情况
                interval = llm_json.get("interval", {})
                start = interval.get("start")
                end = interval.get("end")
                teacher = interval.get("teacher_name", teacher_name)
                student = interval.get("student_name", student_name)

                tool_result = check_schedule(
                    self.engine,
                    start_time_str=start,
                    end_time_str=end,
                    teacher_name=teacher,
                    student_name=student,
                )
                state["tool_history"].append({
                    "tool": "check_schedule",
                    "input": interval,
                    "output": tool_result,
                })

            elif action == "clear_slot_for_group":
                # 让算法尝试通过挪课的方式，清空某个时间段
                slot = llm_json.get("slot", {})
                slot_start = slot.get("start")
                slot_end = slot.get("end")
                max_depth = llm_json.get("max_depth", 1)
                horizon_days = llm_json.get("horizon_days", 7)

                try:
                    slot_start_dt = datetime.fromisoformat(slot_start)
                    slot_end_dt = datetime.fromisoformat(slot_end)
                except Exception:
                    state["tool_history"].append({
                        "tool": "clear_slot_for_group",
                        "input": slot,
                        "output": "invalid datetime format",
                    })
                else:
                    # 目前简单地认为 group = 这位老师 + 这位学生
                    teacher_id, student_id = get_ids(self.engine, teacher_name, student_name)
                    if teacher_id is None or student_id is None:
                        move_plan = None
                    else:
                        move_plan = clear_slot_for_group(
                            self.engine,
                            teacher_id=teacher_id,
                            student_ids=[student_id],
                            slot_start_dt=slot_start_dt,
                            slot_end_dt=slot_end_dt,
                            depth=0,
                            max_depth=max_depth,
                            horizon_days=horizon_days,
                        )

                    state["tool_history"].append({
                        "tool": "clear_slot_for_group",
                        "input": {
                            "slot": slot,
                            "max_depth": max_depth,
                            "horizon_days": horizon_days,
                        },
                        "output": move_plan,
                    })

            elif action == "update_plan":
                # LLM 给出一个中间的换课方案，我们只是记录下来
                plan = llm_json.get("plan", {})
                state["plan_candidate"].append(plan)

            else:
                # 未知 action，直接记录错误信息，下一轮让 LLM 自己修正
                state["tool_history"].append({
                    "tool": "error",
                    "input": llm_json,
                    "output": "Unknown action",
                })


        # 超过 max_steps 还没 finish，就让 LLM 做最后总结
        summary = self._force_final_summary(state)
        return summary

    def _call_llm(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """
        与 LLM 交互：把当前 state 描述给它，要求只输出 JSON。
        JSON 协议（示例）：

        {
          "action": "check_interval",
          "interval": {
            "start": "2025-11-13 10:30:00",
            "end": "2025-11-13 12:00:00",
            "teacher_name": "xxx",
            "student_name": "yyy"
          }
        }

        或者：
        {
          "action": "update_plan",
          "plan": {
            "description": "先把数学课从周二晚上挪到周四晚上，再把新课放在周二晚上。",
            "steps": [...]
          }
        }

        或者：
        {
          "action": "finish",
          "final_text": "（用中文给管理员的详细排课建议）"
        }
        """
        prompt = f"""
        你是一个负责处理「连续换班 / 换课」问题的排课 AI agent。

        当前上下文 state 如下（JSON）：
        {json.dumps(state, ensure_ascii=False, indent=2)}

        任务说明：
        ...
        你可以做三种事情之一（用 JSON 表达）：
        1) 当你想检查某个时间段的老师/学生是否有课时：
           {{
             "action": "check_interval",
             "interval": {{
               "start": "2025-11-13 10:30:00",
               "end": "2025-11-13 12:00:00",
               "teacher_name": "xxx",
               "student_name": "yyy"
             }}
           }}

        2) 当你想尝试通过挪课清空某个时间段时（可以挪一次或多次）：
           {{
             "action": "clear_slot_for_group",
             "slot": {{
               "start": "2025-11-13 10:30:00",
               "end": "2025-11-13 12:00:00"
             }},
             "max_depth": 1,
             "horizon_days": 7
           }}
           说明：
           - max_depth=1 表示「最多挪一次课」；>1 表示可以连续挪多节课形成链条。
           - horizon_days 表示在未来多少天内为被挪走的课寻找新时间。

        3) 当你想给出一个中间的排课方案草稿时：
           {{
             "action": "update_plan",
             "plan": {{
               "description": "一句中文说明你的整体思路。",
               "steps": [
                 "先把 XX 课从 时间A 换到 时间B",
                 "再把 YY 课从 时间C 换到 时间D",
                 "最后在 时间E 安排新课 ZZ"
               ]
             }}
           }}

        4) 当你觉得已经完整考虑过冲突，找到了一个比较合理的整体方案时：
           {{
             "action": "finish",
             "final_text": "用中文面向排课管理员，详细说明最终建议。包括：哪些课要先删/换；换到什么时候；最后新课安排在什么时候；如果无法满足需求，也要说明原因。"
           }}

        请严格注意：
        ...
        """

        resp = self.client.responses.create(
            model=self.model,
            input=prompt,
        )

        text_out = resp.output[0].content[0].text.strip()
        try:
            parsed = json.loads(text_out)
        except Exception:
            # 如果 LLM 输出坏掉，直接返回 finish，让下一轮人工看
            return {
                "action": "finish",
                "final_text": f"LLM 输出不是合法 JSON，原始输出如下：\n{text_out}"
            }
        return parsed

    def _force_final_summary(self, state: Dict[str, Any]) -> str:
        """
        超过 max_steps 仍未 finish 的 fallback：
        再调用一次 LLM，请它直接写最终总结（不再走 JSON）。
        """
        prompt = f"""
你是一个排课 AI 助手。下面是完整的调度 state（JSON）：

{json.dumps(state, ensure_ascii=False, indent=2)}

你已经进行了多轮工具调用，但还没有给出最终方案。
现在请你直接用中文写出一个「最终排课建议」给人类排课管理员，要求：
- 说明是否有办法通过连续换班（多门课程挪动）来满足需求；
- 如果有，请按步骤列出：哪门课从什么时间换到什么时间，新课安排在哪里；
- 如果没有，请清晰说明冲突点为什么无法解决，并给出备选建议。
"""
        resp = self.client.responses.create(
            model=self.model,
            input=prompt,
        )
        return resp.output[0].content[0].text.strip()

# =========================
# 3. 命令行入口
# =========================
def main():
    client = OpenAI(api_key="")
    llm_model = "gpt-4.1"

    # ===== 一次性输入 =====
    student_name = input("请输入需要换课/加课/删课的学生名字：").strip()
    print(f"你输入的学生是：{student_name}\n")

    teacher_name = input("请输入意向老师名字：").strip()
    print(f"你输入的老师是：{teacher_name}\n")

    intent_start = input("请输入意向开始时间，格式20xx-xx-xx xx:xx:00：").strip()
    print(f"你输入的意向开始时间是：{intent_start}\n")

    intent_end = input("请输入意向结束时间，格式20xx-xx-xx xx:xx:00：").strip()
    print(f"你输入的意向结束时间是：{intent_end}\n")

    requirement = input("申请要求（例如：加哪门课/希望时间段/是否可以换老师等）：").strip()
    print(f"你输入的申请要求是：{requirement}\n")

    # ===== 第一步：当前老师是否可以直接上 =====
    base = direct_check_and_plan(
        engine,
        student_name=student_name,
        teacher_name=teacher_name,
        intent_start=intent_start,
        intent_end=intent_end,
    )

    status = base["status"]
    check_result = base["check_result"]

    if status == "ok":
        print("\n================ 排课建议 ================\n")
        print(f"学生 {student_name} 与老师 {teacher_name} 在 {intent_start} ~ {intent_end} 均无课程冲突，可以直接在该时间安排新课。")
        return

    # 是否在申请中明确写了“可以换老师”
    can_change_teacher = ("换老师" in requirement) or ("可以换老师" in requirement)

    # ===== 第二步：只要老师忙，就【总是】跑一次“换老师方案”，供管理员参考 =====
    change_res = {
        "success": False,
        "candidates": [],
        "reason": "老师在该时间段并非忙碌。",
    }
    if "teacher_busy" in status:
        change_res = change_teacher_strategy(
            engine,
            client,
            llm_model,
            student_name=student_name,
            requirement=requirement,
            intent_start=intent_start,
            intent_end=intent_end,
        )

    print("========== 方案一：换老师 ==========")
    if not can_change_teacher:
        print("（提示：学生申请中未明确写“可以换老师”，以下方案仅供管理员评估参考。）")

    if change_res["success"]:
        # 解析意向时间，只考虑同一天的 slot
        intent_start_dt = datetime.fromisoformat(intent_start)
        intent_end_dt = datetime.fromisoformat(intent_end)
        target_date = intent_start_dt.date()

        for item in change_res["candidates"]:
            tname = item["teacher_name"]
            tid = item["teacher_id"]

            available_slots = []

            # 枚举当天所有固定 timeslot
            for slot in ALLOWED_SLOTS:
                slot_start_dt, slot_end_dt = build_slot_dt(target_date, slot)

                # 只考虑落在意向时间范围内的 slot
                if not (slot_start_dt >= intent_start_dt and slot_end_dt <= intent_end_dt):
                    continue

                # 用已有的 check_schedule 检查该老师 + 学生在这个 slot 有没有课
                r = check_schedule(
                    engine,
                    start_time_str=slot_start_dt.strftime("%Y-%m-%d %H:%M:%S"),
                    end_time_str=slot_end_dt.strftime("%Y-%m-%d %H:%M:%S"),
                    teacher_name=tname,
                    student_name=student_name,
                )
                teacher_busy = len(r["teacher_struct"]) > 0
                student_busy = len(r["student_struct"]) > 0

                if (not teacher_busy) and (not student_busy):
                    # 这个 90min slot 完全空闲
                    available_slots.append(f"{slot_start_dt.strftime('%H:%M')}–{slot_end_dt.strftime('%H:%M')}")

            if available_slots:
                slots_str = "，".join(available_slots)
                print(f"- 老师：{tname}（ID {tid}）可上课时间段：{slots_str}")
            else:
                print(f"- 老师：{tname}（ID {tid}）在当前意向时间段内没有符合固定 timeslot 的空档")
    else:
        print("没有可用老师（或老师并非处于忙碌状态）")

    # ===== 第三步：只要老师忙，就尝试“挪掉当前老师的课再补上” =====
    if "teacher_busy" in status:
        move_res = move_existing_lessons_strategy(
            engine,
            client,
            llm_model,
            student_name=student_name,
            teacher_name=teacher_name,
            intent_start=intent_start,
            intent_end=intent_end,
            base_check_result=check_result,
        )
        print("\n========== 方案二：挪课 ==========")
        if move_res["success"]:
            for item in move_res["candidates"]:
                print(f"- 目标 Slot：{item['slot']}")
                print("  需要挪动的课程：")

                for les in item["move_plan"]:
                    ori = les["original_lesson"]
                    org_topic = ori.get("topic", "（无课程名）")
                    org_id = ori.get("lesson_id", "未知ID")
                    org_start = ori.get("lesson_start")
                    org_end = ori.get("lesson_end")

                    print(f"    • 课程：{org_topic}（课时ID：{org_id}）")
                    print(f"      原时间：{org_start} ~ {org_end}")
                    print(f"      可选新时间：")

                    for opt in les["options"]:
                        new_start = opt["new_start"]
                        new_end = opt["new_end"]
                        print(f"        - {new_start} ~ {new_end}")
        else:
        #     print("无法通过挪课解决冲突")
        # # 在老师繁忙、且你想尝试更聪明的连续换班时，可以启用 Agent：
        # use_agent = "agent" in requirement.lower() or "连续" in requirement
        #
        # if use_agent:
            print("\n===== 启动 SchedulingAgent（多轮智能排课）=====")

            intent_date = datetime.fromisoformat(intent_start).date()
            # 这里简单设置 week_start/week_end 为“包含意向日期的这一周”
            week_monday = intent_date - timedelta(days=intent_date.weekday())  # 周一
            week_sunday = week_monday + timedelta(days=6)

            week_start = week_monday.strftime("%Y-%m-%d 00:00:00")
            week_end = week_sunday.strftime("%Y-%m-%d 23:59:59")

            agent = SchedulingAgent(engine, client, model=llm_model)
            final_text = agent.run(
                student_name=student_name,
                teacher_name=teacher_name,
                requirement=requirement,
                intent_start=intent_start,
                intent_end=intent_end,
                week_start=week_start,
                week_end=week_end,
                max_steps=6,
            )

            print("\n===== Agent 给出的最终建议 =====\n")
            print(final_text)

        print("\n请管理员从以上方案中选择最终排课方式。")


if __name__ == "__main__":
    main()
